from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Protocol

_DAY_START_HOUR = 4  # Day starts/ends at 4 AM

_BUILD_OUTPUT_START_RE = re.compile(r"^(#\d+[\s\[]|PS [A-Z]:\\|> |\$ )")
_BUILD_OUTPUT_INLINE_RE = re.compile(r"\n(?:#\d+[\s\[]|PS [A-Z]:\\)")

_MAX_USER_CHARS = 400
_MAX_ASSISTANT_CHARS = 600


def _trim_user_text(text: str) -> str:
    """Strip pasted terminal/build output and truncate to a reasonable length."""
    if _BUILD_OUTPUT_START_RE.match(text):
        return ""
    m = _BUILD_OUTPUT_INLINE_RE.search(text)
    if m:
        text = text[: m.start()].strip()
    return text[:_MAX_USER_CHARS]


def day_bounds(date: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) for a logical day: date @ 04:00 → next day @ 04:00."""
    start = date.replace(hour=_DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def floor_dt(ts: datetime, minutes: int) -> datetime:
    """Floor a datetime down to a multiple of `minutes` past the hour.

    `minutes` must divide 60 — the floor is within the current hour only.
    """
    return ts.replace(minute=(ts.minute // minutes) * minutes, second=0, microsecond=0)


def fmt_duration(secs: float) -> str:
    """Format seconds as a compact duration, e.g. "1h 30m", "2h", "45m"."""
    h, m = divmod(int(secs) // 60, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


@dataclass
class Chunk:
    window_start: str  # ISO datetime string (LOCAL_TZ-aware)
    text: str
    source: str
    apps: list[str] = field(default_factory=list)
    total_secs: float = 0.0
    metadata: dict = field(default_factory=dict)


class DataSource(Protocol):
    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        ...
