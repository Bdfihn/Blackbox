import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from .base import Chunk, fmt_duration

log = logging.getLogger(__name__)

EPISODE_GAP_SECS = 120
EPISODE_MIN_SECS = 60

_BROWSER_SUFFIXES = {
    "Google Chrome": "Chrome",
    "Mozilla Firefox": "Firefox",
    "Microsoft Edge": "Edge",
}
_SITE_SUFFIXES = ("YouTube",)


def normalize_title(app: str, title: str) -> tuple[str, str]:
    """Strip boilerplate suffixes from a window title.

    Returns (clean_title, label) where label is the most specific source
    we can name deterministically: a site, a browser, or the bare app.
    """
    label = app[:-4] if app.lower().endswith(".exe") else app
    for browser, short in _BROWSER_SUFFIXES.items():
        suffix = f" - {browser}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            label = short
            break
    for site in _SITE_SUFFIXES:
        suffix = f" - {site}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            label = site
            break
    return title.strip(), label

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


@dataclass
class _Episode:
    title: str
    label: str
    start: datetime
    end: datetime
    active_secs: float


class ActivityWatchSource:
    def __init__(self, aw_base: str, local_tz: zoneinfo.ZoneInfo):
        self._aw_base = aw_base
        self._local_tz = local_tz

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
                chunks.extend(self._render_episodes(self._build_episodes(events)))
            except Exception as e:
                log.error(f"  Error fetching {bucket_id}: {e}")
        chunks.sort(key=lambda c: c.window_start)
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

    def _build_episodes(self, events: list[dict]) -> list[_Episode]:
        episodes: list[_Episode] = []
        current: _Episode | None = None
        for event in sorted(events, key=lambda e: e["timestamp"]):
            ts = _parse_ts(event["timestamp"]).astimezone(self._local_tz)
            duration = event.get("duration", 0)
            data = event.get("data", {})
            title, label = normalize_title(data.get("app", "unknown"), data.get("title", ""))
            end = ts + timedelta(seconds=duration)
            if (
                current
                and current.title == title
                and current.label == label
                and (ts - current.end).total_seconds() <= EPISODE_GAP_SECS
            ):
                current.end = max(current.end, end)
                current.active_secs += duration
            else:
                if current:
                    episodes.append(current)
                current = _Episode(title, label, ts, end, duration)
        if current:
            episodes.append(current)
        return episodes

    def _render_episodes(self, episodes: list[_Episode]) -> list[Chunk]:
        chunks: list[Chunk] = []
        brief: list[_Episode] = []

        def subject(ep: _Episode) -> str:
            if ep.title and ep.title != ep.label:
                return f'{ep.label}: "{ep.title}"'
            return ep.title or ep.label

        def flush_brief():
            if not brief:
                return
            first = brief[0]
            seen: set[str] = set()
            items: list[str] = []
            for ep in brief:
                item = f'"{ep.title}" ({ep.label})' if ep.title and ep.title != ep.label else ep.label
                if item not in seen:
                    seen.add(item)
                    items.append(item)
            chunks.append(Chunk(
                window_start=first.start.isoformat(),
                text=f"[{first.start.strftime('%Y-%m-%d %H:%M')}] Briefly: {', '.join(items)}.",
                apps=sorted({ep.label for ep in brief}),
                total_secs=sum(ep.active_secs for ep in brief),
                source="activitywatch",
            ))
            brief.clear()

        for ep in episodes:
            if ep.active_secs < EPISODE_MIN_SECS:
                brief.append(ep)
                continue
            flush_brief()
            span = f"{ep.start.strftime('%Y-%m-%d %H:%M')}–{ep.end.strftime('%H:%M')}"
            chunks.append(Chunk(
                window_start=ep.start.isoformat(),
                text=f"[{span}] {subject(ep)} ({fmt_duration(ep.active_secs)})",
                apps=[ep.label],
                total_secs=ep.active_secs,
                source="activitywatch",
            ))
        flush_brief()
        return chunks
