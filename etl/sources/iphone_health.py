import logging
import zoneinfo
from datetime import datetime

from .base import Chunk, floor_dt
from .iphone_backup import apple_ts, open_backup_db, to_apple_secs

log = logging.getLogger(__name__)

BUCKET_SECS = 3600

# healthdb_secure.sqlite data_type constants (observed iOS 16-17)
_STEPS_TYPE = 7   # HKQuantityTypeIdentifierStepCount
_HR_TYPE    = 5   # HKQuantityTypeIdentifierHeartRate
_SLEEP_TYPE = 63  # HKCategoryTypeIdentifierSleepAnalysis


def parse_health(backup, start_local: datetime, end_local: datetime, local_tz: zoneinfo.ZoneInfo) -> list[dict]:
    """Extract steps, heart rate, and sleep from healthdb_secure.sqlite for the given window.

    Returns:
        List of {timestamp (local_tz datetime), type ('steps'|'heart_rate'|'sleep'),
                 value (float), unit (str)}.
        For 'sleep', value is duration in seconds (end_date − start_date).
    """
    apple_start = to_apple_secs(start_local)
    apple_end = to_apple_secs(end_local)

    with open_backup_db(backup, "Health/healthdb_secure.sqlite") as conn:
        if conn is None:
            raise FileNotFoundError("healthdb_secure.sqlite not found in backup")
        records = []

        for start_ts, qty, data_type in conn.execute(
            "SELECT s.start_date, qs.quantity, s.data_type "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type IN (?, ?) AND s.start_date >= ? AND s.start_date < ?",
            (_STEPS_TYPE, _HR_TYPE, apple_start, apple_end),
        ).fetchall():
            ts = apple_ts(start_ts).astimezone(local_tz)
            rtype = "steps" if data_type == _STEPS_TYPE else "heart_rate"
            unit = "count" if data_type == _STEPS_TYPE else "count/min"
            records.append({"timestamp": ts, "type": rtype, "value": qty, "unit": unit})

        for start_ts, end_ts, _val in conn.execute(
            "SELECT s.start_date, s.end_date, cs.value "
            "FROM samples s JOIN category_samples cs ON cs.ROWID = s.ROWID "
            "WHERE s.data_type = ? AND s.start_date >= ? AND s.start_date < ?",
            (_SLEEP_TYPE, apple_start, apple_end),
        ).fetchall():
            ts = apple_ts(start_ts).astimezone(local_tz)
            duration = (end_ts - start_ts) if end_ts is not None else 0.0
            records.append({"timestamp": ts, "type": "sleep", "value": duration, "unit": "sec"})

        return records


class IPhoneHealthSource:
    def __init__(self, backup, local_tz: zoneinfo.ZoneInfo):
        self._backup = backup
        self._local_tz = local_tz

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        records = parse_health(self._backup, start, end, self._local_tz)
        log.info(f"  healthdb: {len(records)} records")
        return self._chunk_health(records)

    def _chunk_health(self, records: list[dict]) -> list[Chunk]:
        if not records:
            return []

        chunks = []
        hourly_steps: dict[datetime, float] = {}
        hourly_hr: dict[datetime, list[float]] = {}

        for r in records:
            ts = r["timestamp"]
            rtype = r["type"]
            if rtype == "sleep":
                chunks.append(Chunk(
                    window_start=ts.isoformat(),
                    text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] Sleep session: {round(r['value'] / 3600, 1)} hours.",
                    apps=[],
                    total_secs=int(r["value"]),
                    source="iphone_health",
                ))
            else:
                hour_key = floor_dt(ts, 60)
                if rtype == "steps":
                    hourly_steps[hour_key] = hourly_steps.get(hour_key, 0) + r["value"]
                else:
                    hourly_hr.setdefault(hour_key, []).append(r["value"])

        for hour in sorted(set(hourly_steps) | set(hourly_hr)):
            parts = []
            if hour in hourly_steps:
                parts.append(f"{int(hourly_steps[hour])} steps")
            if hour in hourly_hr:
                parts.append(f"avg HR {round(sum(hourly_hr[hour]) / len(hourly_hr[hour]))}bpm")
            chunks.append(Chunk(
                window_start=hour.isoformat(),
                text=f"[{hour.strftime('%Y-%m-%d %H:%M')}] Health summary: {', '.join(parts)}.",
                apps=[],
                total_secs=BUCKET_SECS,
                source="iphone_health",
            ))

        return chunks
