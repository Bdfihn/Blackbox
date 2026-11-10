import logging
import zoneinfo
from datetime import datetime

from .base import Chunk, floor_dt, fmt_duration
from .iphone_backup import from_apple_secs, open_backup_db, to_apple_secs

log = logging.getLogger(__name__)

WINDOW_MINUTES = 60

# data_type constants (verified against healthdb_secure.sqlite unit strings and
# value ranges; e.g. 172 is environmental audio dB, NOT HRV)
_STEPS_TYPE       = 7    # HKQuantityTypeIdentifierStepCount
_HR_TYPE          = 5    # HKQuantityTypeIdentifierHeartRate (beats/sec)
_DISTANCE_TYPE    = 8    # HKQuantityTypeIdentifierDistanceWalkingRunning (meters)
_BASAL_CAL_TYPE   = 9    # HKQuantityTypeIdentifierBasalEnergyBurned (kcal)
_ACTIVE_CAL_TYPE  = 10   # HKQuantityTypeIdentifierActiveEnergyBurned (kcal)
_SLEEP_TYPE       = 63   # HKCategoryTypeIdentifierSleepAnalysis
_HRV_TYPE         = 139  # HKQuantityTypeIdentifierHeartRateVariabilitySDNN (ms)
_RESTING_HR_TYPE  = 118  # HKQuantityTypeIdentifierRestingHeartRate (bpm)
_RESP_RATE_TYPE   = 61   # HKQuantityTypeIdentifierRespiratoryRate (breaths/sec)
_VO2_MAX_TYPE     = 124  # HKQuantityTypeIdentifierVO2Max (ml/kg/min)
_WALKING_HR_TYPE  = 137  # HKQuantityTypeIdentifierWalkingHeartRateAverage (bpm)
_EXERCISE_MIN_TYPE = 75  # HKQuantityTypeIdentifierAppleExerciseTime (min)
_DAYLIGHT_TYPE    = 279  # HKQuantityTypeIdentifierTimeInDaylight (min)
_EFFORT_TYPE      = 286  # HKQuantityTypeIdentifierPhysicalEffort (METs)

# HKCategoryValueSleepAnalysis values
_SLEEP_AWAKE = 1
_SLEEP_CORE  = 3
_SLEEP_DEEP  = 4
_SLEEP_REM   = 5

# HKWorkoutActivityType raw values (Apple HealthKit enum)
_WORKOUT_NAMES = {
    1:  "American Football", 2:  "Archery",          3:  "Australian Football",
    4:  "Badminton",        5:  "Baseball",          6:  "Basketball",
    7:  "Bowling",          8:  "Boxing",            9:  "Climbing",
    10: "Cricket",          11: "Cross Training",    12: "Curling",
    13: "Cycling",          14: "Dance",             16: "Elliptical",
    17: "Equestrian Sports", 18: "Fencing",          19: "Fishing",
    20: "Functional Strength Training", 21: "Golf",  22: "Gymnastics",
    23: "Handball",         24: "Hiking",            25: "Hockey",
    26: "Hunting",          27: "Lacrosse",          28: "Martial Arts",
    29: "Mind & Body",      30: "Mixed Metabolic Cardio", 31: "Paddle Sports",
    32: "Play",             33: "Preparation & Recovery", 34: "Racquetball",
    35: "Rowing",           36: "Rugby",             37: "Running",
    38: "Sailing",          39: "Skating",           40: "Snow Sports",
    41: "Soccer",           42: "Softball",          43: "Squash",
    44: "Stair Climbing",   45: "Surfing",           46: "Swimming",
    47: "Table Tennis",     48: "Tennis",            49: "Track & Field",
    50: "Strength Training", 51: "Volleyball",       52: "Walking",
    53: "Water Fitness",    54: "Water Polo",        55: "Water Sports",
    56: "Wrestling",        57: "Yoga",              58: "Barre",
    59: "Core Training",    60: "Cross Country Skiing", 61: "Downhill Skiing",
    62: "Flexibility",      63: "HIIT",              64: "Jump Rope",
    65: "Kickboxing",       66: "Pilates",           67: "Snowboarding",
    68: "Stairs",           69: "Step Training",     70: "Wheelchair Walk Pace",
    71: "Wheelchair Run Pace", 72: "Tai Chi",        73: "Mixed Cardio",
    74: "Hand Cycling",     75: "Disc Sports",       76: "Fitness Gaming",
    77: "Cardio Dance",     78: "Social Dance",      79: "Pickleball",
    80: "Cooldown",         82: "Swim Bike Run",     83: "Transition",
    84: "Underwater Diving", 3000: "Other",
}


_QUANTITY_FROM_WHERE = (
    "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
    "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ?"
)


def _agg_quantity(conn, agg: str, data_type: int, apple_start: float, apple_end: float) -> float | None:
    """Aggregate (AVG/SUM) of a quantity type over the window, or None."""
    row = conn.execute(
        f"SELECT {agg}(qs.quantity) {_QUANTITY_FROM_WHERE}",
        (data_type, apple_start, apple_end),
    ).fetchone()
    return row[0] if row else None


def _first_or_last_quantity(conn, data_type: int, apple_start: float, apple_end: float, latest: bool = False) -> tuple[float, float] | None:
    """(start_date, quantity) of the first (or latest) sample in the window, or None."""
    order = "DESC" if latest else "ASC"
    return conn.execute(
        f"SELECT s.start_date, qs.quantity {_QUANTITY_FROM_WHERE} ORDER BY s.start_date {order} LIMIT 1",
        (data_type, apple_start, apple_end),
    ).fetchone()


class IPhoneHealthSource:
    def __init__(self, backup, local_tz: zoneinfo.ZoneInfo):
        self._backup = backup
        self._local_tz = local_tz

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        chunks = []
        apple_start = to_apple_secs(start)
        apple_end   = to_apple_secs(end)

        with open_backup_db(self._backup, "Health/healthdb_secure.sqlite") as conn:
            if conn is None:
                raise FileNotFoundError("healthdb_secure.sqlite not found in backup")
            chunks.extend(self._activity_chunks(conn, apple_start, apple_end))
            chunks.extend(self._sleep_chunks(conn, apple_start, apple_end))
            chunks.extend(self._vitals_chunks(conn, apple_start, apple_end))
            chunks.extend(self._workout_chunks(conn, apple_start, apple_end))

        log.info(f"  healthdb: {len(chunks)} chunks")
        return chunks

    # ── Hourly steps + heart rate ─────────────────────────────────────────────

    def _activity_chunks(self, conn, apple_start: float, apple_end: float) -> list[Chunk]:
        rows = conn.execute(
            "SELECT s.start_date, qs.quantity, s.data_type "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type IN (?, ?, ?) AND s.start_date >= ? AND s.start_date < ?",
            (_STEPS_TYPE, _HR_TYPE, _EFFORT_TYPE, apple_start, apple_end),
        ).fetchall()

        hourly_steps:  dict[datetime, float] = {}
        hourly_hr:     dict[datetime, list[float]] = {}
        hourly_effort: dict[datetime, list[float]] = {}

        for start_ts, qty, data_type in rows:
            ts = from_apple_secs(start_ts).astimezone(self._local_tz)
            hour = floor_dt(ts, WINDOW_MINUTES)
            if data_type == _STEPS_TYPE:
                hourly_steps[hour] = hourly_steps.get(hour, 0) + qty
            elif data_type == _HR_TYPE:
                hourly_hr.setdefault(hour, []).append(qty * 60)  # beats/sec → bpm
            else:
                hourly_effort.setdefault(hour, []).append(qty)

        chunks = []
        for hour in sorted(set(hourly_steps) | set(hourly_hr) | set(hourly_effort)):
            parts = []
            if hour in hourly_steps:
                parts.append(f"{int(hourly_steps[hour])} steps")
            if hour in hourly_hr:
                parts.append(f"avg HR {round(sum(hourly_hr[hour]) / len(hourly_hr[hour]))}bpm")
            if hour in hourly_effort:
                parts.append(f"avg effort {sum(hourly_effort[hour]) / len(hourly_effort[hour]):.1f} METs")
            chunks.append(Chunk(
                window_start=hour.isoformat(),
                text=f"[{hour.strftime('%Y-%m-%d %H:%M')}] Activity: {', '.join(parts)}.",
                apps=[], total_secs=WINDOW_MINUTES * 60, source="iphone_health",
            ))
        return chunks

    # ── Sleep stages ──────────────────────────────────────────────────────────

    def _sleep_chunks(self, conn, apple_start: float, apple_end: float) -> list[Chunk]:
        rows = conn.execute(
            "SELECT s.start_date, s.end_date, cs.value "
            "FROM samples s JOIN category_samples cs ON cs.ROWID = s.ROWID "
            "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ?",
            (_SLEEP_TYPE, apple_start, apple_end),
        ).fetchall()

        if not rows:
            return []

        # Find the earliest sleep start as the anchor for the chunk
        earliest_start_ts = min(r[0] for r in rows)
        window_start = from_apple_secs(earliest_start_ts).astimezone(self._local_tz)

        stage_secs: dict[int, float] = {}
        for start_ts, end_ts, value in rows:
            dur = (end_ts - start_ts) if end_ts else 0.0
            stage_secs[value] = stage_secs.get(value, 0) + dur

        core_s  = stage_secs.get(_SLEEP_CORE, 0)
        deep_s  = stage_secs.get(_SLEEP_DEEP, 0)
        rem_s   = stage_secs.get(_SLEEP_REM, 0)
        awake_s = stage_secs.get(_SLEEP_AWAKE, 0)
        total_s = core_s + deep_s + rem_s  # exclude awake from "sleep"

        if total_s == 0:
            # Pre-Watch data: only InBed/AsleepUnspecified, fall back to total duration
            total_s = sum(stage_secs.values())
            text = (
                f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
                f"Sleep: {fmt_duration(total_s)} total."
            )
        else:
            parts = []
            if core_s:  parts.append(f"Core {fmt_duration(core_s)}")
            if deep_s:  parts.append(f"Deep {fmt_duration(deep_s)}")
            if rem_s:   parts.append(f"REM {fmt_duration(rem_s)}")
            if awake_s: parts.append(f"awake {fmt_duration(awake_s)}")
            text = (
                f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
                f"Sleep: {fmt_duration(total_s)} total — {', '.join(parts)}."
            )

        # Avg respiratory rate over the whole logical day, not just the sleep
        # samples above — Apple only records it during sleep, so they coincide.
        resp_rate = _agg_quantity(conn, "AVG", _RESP_RATE_TYPE, apple_start, apple_end)
        if resp_rate:
            text += f" Avg respiratory rate: {resp_rate * 60:.1f} breaths/min."  # breaths/sec → breaths/min

        return [Chunk(
            window_start=window_start.isoformat(),
            text=text,
            apps=[], total_secs=int(total_s), source="iphone_health",
            metadata={"kind": "sleep"},
        )]

    # ── Daily vitals ──────────────────────────────────────────────────────────

    def _vitals_chunks(self, conn, apple_start: float, apple_end: float) -> list[Chunk]:
        resting_hr = _first_or_last_quantity(conn, _RESTING_HR_TYPE, apple_start, apple_end)
        hrv        = _agg_quantity(conn, "AVG", _HRV_TYPE, apple_start, apple_end)
        walking_hr = _agg_quantity(conn, "AVG", _WALKING_HR_TYPE, apple_start, apple_end)
        vo2_max    = _first_or_last_quantity(conn, _VO2_MAX_TYPE, apple_start, apple_end, latest=True)
        exercise   = _agg_quantity(conn, "SUM", _EXERCISE_MIN_TYPE, apple_start, apple_end)
        daylight   = _agg_quantity(conn, "SUM", _DAYLIGHT_TYPE, apple_start, apple_end)

        parts = []
        ts = None

        if resting_hr:
            ts = from_apple_secs(resting_hr[0]).astimezone(self._local_tz)
            parts.append(f"resting HR {round(resting_hr[1])}bpm")
        if hrv:
            parts.append(f"HRV {round(hrv)}ms")
        if walking_hr:
            parts.append(f"walking HR avg {round(walking_hr)}bpm")
        if vo2_max:
            parts.append(f"VO2 max {vo2_max[1]:.1f} ml/kg/min")
        if exercise:
            parts.append(f"{round(exercise)} exercise min")
        if daylight:
            parts.append(f"{round(daylight)} min daylight")

        if not parts:
            return []

        if ts is None:
            ts = from_apple_secs(apple_start).astimezone(self._local_tz)

        return [Chunk(
            window_start=ts.isoformat(),
            text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] Daily vitals: {', '.join(parts)}.",
            apps=[], total_secs=0, source="iphone_health",
            metadata={"kind": "vitals"},
        )]

    # ── Workouts ──────────────────────────────────────────────────────────────

    def _workout_chunks(self, conn, apple_start: float, apple_end: float) -> list[Chunk]:
        workouts = conn.execute(
            "SELECT wa.ROWID, wa.activity_type, wa.start_date, wa.end_date, wa.duration "
            "FROM workout_activities wa "
            "WHERE wa.is_primary_activity = 1 "
            "  AND wa.start_date >= ? AND wa.start_date < ?",
            (apple_start, apple_end),
        ).fetchall()

        if not workouts:
            return []

        # Pre-fetch all statistics for these workouts in one query
        rowids = [w[0] for w in workouts]
        placeholders = ",".join("?" * len(rowids))
        stats_rows = conn.execute(
            f"SELECT workout_activity_id, data_type, quantity "
            f"FROM workout_statistics WHERE workout_activity_id IN ({placeholders})",
            rowids,
        ).fetchall()

        stats: dict[int, dict[int, float]] = {}
        for wa_id, data_type, qty in stats_rows:
            stats.setdefault(wa_id, {})[data_type] = qty

        chunks = []
        for rowid, activity_type, start_ts, end_ts, duration_secs in workouts:
            ts = from_apple_secs(start_ts).astimezone(self._local_tz)
            name = _WORKOUT_NAMES.get(activity_type, f"Workout (type {activity_type})")
            dur = fmt_duration(duration_secs or ((end_ts or start_ts) - start_ts))

            w_stats = stats.get(rowid, {})
            parts = [f"{dur}"]
            if _HR_TYPE in w_stats:
                parts.append(f"avg HR {round(w_stats[_HR_TYPE] * 60)}bpm")
            if _ACTIVE_CAL_TYPE in w_stats:
                total_kcal = w_stats[_ACTIVE_CAL_TYPE] + w_stats.get(_BASAL_CAL_TYPE, 0)
                parts.append(f"{round(total_kcal)} kcal")
            if _DISTANCE_TYPE in w_stats:
                parts.append(f"{w_stats[_DISTANCE_TYPE] / 1000:.2f}km")

            text = f"[{ts.strftime('%Y-%m-%d %H:%M')}] {name}: {', '.join(parts)}."
            chunks.append(Chunk(
                window_start=ts.isoformat(),
                text=text,
                apps=[], total_secs=int(duration_secs or 0), source="iphone_health",
                metadata={"kind": "workout"},
            ))

        return chunks
