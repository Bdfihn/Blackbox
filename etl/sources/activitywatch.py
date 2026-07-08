import logging
import zoneinfo
from collections import Counter
from datetime import datetime, timedelta

import requests

from .base import Chunk, floor_dt

log = logging.getLogger(__name__)

WINDOW_MINUTES = 5

# aw-server rejects requests whose Host header isn't localhost (DNS-rebinding
# protection); from inside the container we reach it via host.docker.internal.
_AW_HEADERS = {"Host": "localhost:5600"}


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap_secs(start: datetime, end: datetime, intervals: list[tuple[datetime, datetime]]) -> float:
    total = 0.0
    for s, e in intervals:
        if s >= end:
            break
        lo, hi = max(start, s), min(end, e)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def _clip_events(events: list[dict], intervals: list[tuple[datetime, datetime]]) -> list[dict]:
    """Clip each event's duration to its overlap with the active intervals.

    Events with no active overlap are dropped entirely.
    """
    clipped = []
    for event in events:
        ts = _parse_ts(event["timestamp"])
        duration = event.get("duration", 0)
        overlap = _overlap_secs(ts, ts + timedelta(seconds=duration), intervals)
        if overlap > 0:
            clipped.append({**event, "duration": overlap})
    return clipped


class ActivityWatchSource:
    def __init__(self, aw_base: str, local_tz: zoneinfo.ZoneInfo, window_minutes: int = WINDOW_MINUTES):
        self._aw_base = aw_base
        self._local_tz = local_tz
        self._window_minutes = window_minutes

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        try:
            bucket_ids = self._fetch_buckets()
            log.info(f"Found ActivityWatch buckets: {bucket_ids}")
        except Exception as e:
            log.error(f"Could not reach ActivityWatch at {self._aw_base}: {e}")
            return []

        window_buckets = [b for b in bucket_ids if "window" in b.lower()]
        afk_buckets = [b for b in bucket_ids if "afk" in b.lower()]
        active = self._active_intervals(afk_buckets, start, end)

        chunks = []
        for bucket_id in window_buckets:
            try:
                events = self._fetch_events(bucket_id, start, end)
                log.info(f"  {bucket_id}: {len(events)} events")
                if active is not None:
                    events = _clip_events(events, active)
                chunks.extend(self._chunk_events(events))
            except Exception as e:
                log.error(f"  Error fetching {bucket_id}: {e}")
        return chunks

    def _fetch_buckets(self) -> list[str]:
        r = requests.get(
            f"{self._aw_base}/buckets/",
            timeout=10,
            allow_redirects=True,
            headers=_AW_HEADERS,
        )
        r.raise_for_status()
        return list(r.json())

    def _active_intervals(self, afk_buckets: list[str], start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
        """Merged not-afk intervals from the AFK watcher, or None if unavailable.

        None means window time cannot be clipped and is counted in full.
        """
        if not afk_buckets:
            log.warning("No AFK bucket found — counting all window time as active.")
            return None

        intervals = []
        try:
            for bucket_id in afk_buckets:
                for event in self._fetch_events(bucket_id, start, end):
                    if event.get("data", {}).get("status") != "not-afk":
                        continue
                    ts = _parse_ts(event["timestamp"])
                    intervals.append((ts, ts + timedelta(seconds=event.get("duration", 0))))
        except Exception as e:
            log.error(f"  Error fetching AFK events, counting all window time as active: {e}")
            return None
        return _merge_intervals(intervals)

    def _fetch_events(self, bucket_id: str, start: datetime, end: datetime) -> list[dict]:
        r = requests.get(
            f"{self._aw_base}/buckets/{bucket_id}/events",
            params={"start": start.isoformat(), "end": end.isoformat(), "limit": 10000},
            timeout=30,
            headers=_AW_HEADERS,
        )
        r.raise_for_status()
        return r.json()

    def _chunk_events(self, events: list[dict]) -> list[Chunk]:
        if not events:
            return []

        windows: dict[datetime, list] = {}

        for event in events:
            ts = _parse_ts(event["timestamp"]).astimezone(self._local_tz)
            duration = event.get("duration", 0)
            data = event.get("data", {})
            app = data.get("app", "unknown")
            title = data.get("title", "")

            floored = floor_dt(ts, self._window_minutes)
            windows.setdefault(floored, []).append({"app": app, "title": title, "duration_secs": duration})

        chunks = []
        for window_start, items in sorted(windows.items()):
            total = sum(i["duration_secs"] for i in items)
            app_totals: Counter[str] = Counter()
            for i in items:
                app_totals[i["app"]] += i["duration_secs"]
            top_apps = app_totals.most_common(5)

            descriptions = [
                f"{i['app']}: '{i['title']}' ({round(i['duration_secs'] / 60, 1)}m)"
                for i in items
                if i["duration_secs"] > 10
            ]
            text = (
                f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
                f"PC activity for {self._window_minutes} minutes. "
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
