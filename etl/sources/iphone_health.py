import logging
import zoneinfo
from datetime import datetime

from .base import Chunk, floor_dt
from .iphone_backup import apple_ts, open_backup_db, to_apple_secs

log = logging.getLogger(__name__)

BUCKET_MINUTES = 60

# data_type constants (confirmed against healthdb_secure.sqlite, iOS 17-18)
_STEPS_TYPE       = 7    # HKQuantityTypeIdentifierStepCount
_HR_TYPE          = 5    # HKQuantityTypeIdentifierHeartRate (beats/sec)
_DISTANCE_TYPE    = 8    # HKQuantityTypeIdentifierDistanceWalkingRunning (meters)
_BASAL_CAL_TYPE   = 9    # HKQuantityTypeIdentifierBasalEnergyBurned (kcal)
_ACTIVE_CAL_TYPE  = 10   # HKQuantityTypeIdentifierActiveEnergyBurned (kcal)
_SLEEP_TYPE       = 63   # HKCategoryTypeIdentifierSleepAnalysis
_HRV_TYPE         = 172  # HKQuantityTypeIdentifierHeartRateVariabilitySDNN (ms)
_RESTING_HR_TYPE  = 118  # HKQuantityTypeIdentifierRestingHeartRate (bpm)
_RESP_RATE_TYPE   = 259  # HKQuantityTypeIdentifierRespiratoryRate (breaths/min, sleep)
_VO2_MAX_TYPE     = 124  # HKQuantityTypeIdentifierVO2Max (ml/kg/min)

# HKCategoryValueSleepAnalysis values
_SLEEP_AWAKE = 1
_SLEEP_CORE  = 3
_SLEEP_DEEP  = 4
_SLEEP_REM   = 5

# HKWorkoutActivityType raw values (confirmed against Apple HealthKit framework)
_WORKOUT_NAMES = {
    9:  "Cross Training",   13: "Cycling",          22: "Hiking",
    24: "Functional Strength", 28: "Hiking",         33: "Mind & Body",
    37: "Recovery",         41: "Running",           43: "Skating",
    47: "Stair Climbing",   49: "Swimming",          52: "Track & Field",
    53: "Strength Training", 55: "Walking",          56: "Water Fitness",
    60: "Yoga",             63: "Core Training",     66: "Flexibility",
    67: "HIIT",             77: "Mixed Cardio",      86: "Cooldown",
}


def _fmt_duration(secs: float) -> str:
    h = int(secs) // 3600
    m = (int(secs) % 3600) // 60
    if h:
        return f"{h}h {m}min"
    return f"{m}min"


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
            chunks.extend(self._vitals_chunk(conn, apple_start, apple_end))
            chunks.extend(self._workout_chunks(conn, apple_start, apple_end))

        log.info(f"  healthdb: {len(chunks)} chunks")
        return chunks

    # ── Hourly steps + heart rate ─────────────────────────────────────────────

    def _activity_chunks(self, conn, apple_start: float, apple_end: float) -> list[Chunk]:
        rows = conn.execute(
            "SELECT s.start_date, qs.quantity, s.data_type "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type IN (?, ?) AND s.start_date >= ? AND s.start_date < ?",
            (_STEPS_TYPE, _HR_TYPE, apple_start, apple_end),
        ).fetchall()

        hourly_steps: dict[datetime, float] = {}
        hourly_hr:    dict[datetime, list[float]] = {}

        for start_ts, qty, data_type in rows:
            ts = apple_ts(start_ts).astimezone(self._local_tz)
            hour = floor_dt(ts, 60)
            if data_type == _STEPS_TYPE:
                hourly_steps[hour] = hourly_steps.get(hour, 0) + qty
            else:
                hourly_hr.setdefault(hour, []).append(qty * 60)  # beats/sec → bpm

        chunks = []
        for hour in sorted(set(hourly_steps) | set(hourly_hr)):
            parts = []
            if hour in hourly_steps:
                parts.append(f"{int(hourly_steps[hour])} steps")
            if hour in hourly_hr:
                parts.append(f"avg HR {round(sum(hourly_hr[hour]) / len(hourly_hr[hour]))}bpm")
            chunks.append(Chunk(
                window_start=hour.isoformat(),
                text=f"[{hour.strftime('%Y-%m-%d %H:%M')}] Activity: {', '.join(parts)}.",
                apps=[], total_secs=BUCKET_MINUTES * 60, source="iphone_health",
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
        window_start = apple_ts(earliest_start_ts).astimezone(self._local_tz)

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
                f"Sleep: {_fmt_duration(total_s)} total."
            )
        else:
            parts = []
            if core_s:  parts.append(f"Core {_fmt_duration(core_s)}")
            if deep_s:  parts.append(f"Deep {_fmt_duration(deep_s)}")
            if rem_s:   parts.append(f"REM {_fmt_duration(rem_s)}")
            if awake_s: parts.append(f"awake {_fmt_duration(awake_s)}")
            text = (
                f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
                f"Sleep: {_fmt_duration(total_s)} total — {', '.join(parts)}."
            )

        # Append avg respiratory rate if available for the same window
        resp_rows = conn.execute(
            "SELECT AVG(qs.quantity) "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ?",
            (_RESP_RATE_TYPE, apple_start, apple_end),
        ).fetchone()
        if resp_rows and resp_rows[0]:
            text += f" Avg respiratory rate: {resp_rows[0]:.1f} breaths/min."

        return [Chunk(
            window_start=window_start.isoformat(),
            text=text,
            apps=[], total_secs=int(total_s), source="iphone_health",
        )]

    # ── Daily vitals: resting HR + HRV ───────────────────────────────────────

    def _vitals_chunk(self, conn, apple_start: float, apple_end: float) -> list[Chunk]:
        resting_hr_row = conn.execute(
            "SELECT s.start_date, qs.quantity "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ? "
            "ORDER BY s.start_date LIMIT 1",
            (_RESTING_HR_TYPE, apple_start, apple_end),
        ).fetchone()

        hrv_row = conn.execute(
            "SELECT AVG(qs.quantity) "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ?",
            (_HRV_TYPE, apple_start, apple_end),
        ).fetchone()

        vo2_row = conn.execute(
            "SELECT qs.quantity "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ? "
            "ORDER BY s.start_date DESC LIMIT 1",
            (_VO2_MAX_TYPE, apple_start, apple_end),
        ).fetchone()

        parts = []
        ts = None

        if resting_hr_row:
            ts = apple_ts(resting_hr_row[0]).astimezone(self._local_tz)
            parts.append(f"resting HR {round(resting_hr_row[1])}bpm")
        if hrv_row and hrv_row[0]:
            parts.append(f"HRV {round(hrv_row[0])}ms")
        if vo2_row:
            parts.append(f"VO2 max {vo2_row[0]:.1f} ml/kg/min")

        if not parts:
            return []

        if ts is None:
            ts = apple_ts(apple_start).astimezone(self._local_tz)

        return [Chunk(
            window_start=ts.isoformat(),
            text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] Daily vitals: {', '.join(parts)}.",
            apps=[], total_secs=0, source="iphone_health",
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
            ts = apple_ts(start_ts).astimezone(self._local_tz)
            name = _WORKOUT_NAMES.get(activity_type, f"Workout (type {activity_type})")
            dur = _fmt_duration(duration_secs or ((end_ts or start_ts) - start_ts))

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
            ))

        return chunks
