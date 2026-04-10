import logging
import zoneinfo
from datetime import datetime

from .base import Chunk
from iphone import parse_knowledge_db, parse_health

log = logging.getLogger(__name__)

CHUNK_MINUTES = 5


class IPhoneAppsSource:
    def __init__(self, backup, local_tz: zoneinfo.ZoneInfo, chunk_minutes: int = CHUNK_MINUTES):
        self._backup = backup
        self._local_tz = local_tz
        self._chunk_minutes = chunk_minutes

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        events = parse_knowledge_db(self._backup, start, end)
        log.info(f"  knowledgeC: {len(events)} foreground events")
        return self._chunk_apps(events)

    def _chunk_apps(self, events: list[dict]) -> list[Chunk]:
        if not events:
            return []

        chunk_minutes = self._chunk_minutes
        buckets: dict[datetime, dict[str, float]] = {}
        for event in events:
            ts = event["timestamp"]
            floored = ts.replace(
                minute=(ts.minute // chunk_minutes) * chunk_minutes,
                second=0,
                microsecond=0,
            )
            app_name = event["app_bundle_id"].split(".")[-1]
            buckets.setdefault(floored, {})
            buckets[floored][app_name] = buckets[floored].get(app_name, 0) + event["duration_secs"]

        chunks = []
        for window_start, app_totals in sorted(buckets.items()):
            top_apps = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:5]
            text = (
                f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
                f"iPhone activity for {chunk_minutes} minutes. "
                f"Top apps: {', '.join(f'{a}({round(s/60,1)}m)' for a, s in top_apps)}."
            )
            chunks.append(Chunk(
                window_start=window_start.isoformat(),
                text=text,
                apps=[a for a, _ in top_apps],
                total_secs=sum(app_totals.values()),
                source="iphone",
            ))
        return chunks


class IPhoneHealthSource:
    def __init__(self, backup):
        self._backup = backup

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        records = parse_health(self._backup, start, end)
        log.info(f"  healthdb: {len(records)} records")
        return self._chunk_health(records)

    def _chunk_health(self, records: list[dict]) -> list[Chunk]:
        if not records:
            return []

        chunks = []
        hourly_steps: dict[datetime, float] = {}
        hourly_hr: dict[datetime, list[float]] = {}

        for r in records:
            if r["type"] in ("steps", "heart_rate"):
                hour_key = r["timestamp"].replace(minute=0, second=0, microsecond=0)
                if r["type"] == "steps":
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
                total_secs=3600,
                source="iphone_health",
            ))

        for r in records:
            if r["type"] == "sleep":
                ts = r["timestamp"]
                chunks.append(Chunk(
                    window_start=ts.isoformat(),
                    text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] Sleep session: {round(r['value'] / 3600, 1)} hours.",
                    apps=[],
                    total_secs=int(r["value"]),
                    source="iphone_health",
                ))

        return chunks
