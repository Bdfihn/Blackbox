import logging
import os
import zoneinfo
from datetime import datetime

import requests

from .base import Chunk

log = logging.getLogger(__name__)

CHUNK_MINUTES = 5


class ActivityWatchSource:
    def __init__(self, aw_base: str, local_tz: zoneinfo.ZoneInfo, chunk_minutes: int = CHUNK_MINUTES):
        self._aw_base = aw_base
        self._local_tz = local_tz
        self._chunk_minutes = chunk_minutes

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        try:
            bucket_ids = self._fetch_buckets()
            log.info(f"Found ActivityWatch buckets: {bucket_ids}")
        except Exception as e:
            log.error(f"Could not reach ActivityWatch at {self._aw_base}: {e}")
            return []

        chunks = []
        for bucket_id in bucket_ids:
            try:
                events = self._fetch_events(bucket_id, start, end)
                log.info(f"  {bucket_id}: {len(events)} events")
                chunks.extend(self._chunk_events(events))
            except Exception as e:
                log.error(f"  Error fetching {bucket_id}: {e}")
        return chunks

    def _fetch_buckets(self) -> list[str]:
        r = requests.get(
            f"{self._aw_base}/buckets/",
            timeout=10,
            allow_redirects=True,
            headers={"Host": "localhost:5600"},
        )
        r.raise_for_status()
        return [b for b in r.json() if "window" in b.lower()]

    def _fetch_events(self, bucket_id: str, start: datetime, end: datetime) -> list[dict]:
        r = requests.get(
            f"{self._aw_base}/buckets/{bucket_id}/events",
            params={"start": start.isoformat(), "end": end.isoformat(), "limit": 10000},
            timeout=30,
            headers={"Host": "localhost:5600"},
        )
        r.raise_for_status()
        return r.json()

    def _chunk_events(self, events: list[dict]) -> list[Chunk]:
        if not events:
            return []

        chunk_minutes = self._chunk_minutes
        buckets: dict[datetime, list] = {}

        for event in events:
            ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).astimezone(self._local_tz)
            duration = event.get("duration", 0)
            data = event.get("data", {})
            app = data.get("app", "unknown")
            title = data.get("title", "")

            floored = ts.replace(
                minute=(ts.minute // chunk_minutes) * chunk_minutes,
                second=0,
                microsecond=0,
            )
            buckets.setdefault(floored, []).append({"app": app, "title": title, "duration_secs": duration})

        chunks = []
        for window_start, items in sorted(buckets.items()):
            total = sum(i["duration_secs"] for i in items)
            app_totals: dict[str, float] = {}
            for i in items:
                app_totals[i["app"]] = app_totals.get(i["app"], 0) + i["duration_secs"]
            top_apps = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:5]

            descriptions = [
                f"{i['app']}: '{i['title']}' ({round(i['duration_secs'] / 60, 1)}m)"
                for i in items
                if i["duration_secs"] > 10
            ]
            text = (
                f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
                f"PC activity for {chunk_minutes} minutes. "
                f"Top apps: {', '.join(f'{a}({round(s/60,1)}m)' for a, s in top_apps)}. "
                f"Details: {'; '.join(descriptions[:10])}"
            )
            chunks.append(Chunk(
                window_start=window_start.isoformat(),
                text=text,
                apps=[a for a, _ in top_apps],
                total_secs=total,
                source="activitywatch",
            ))
        return chunks
