from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

DAY_START_HOUR = 4  # Day starts/ends at 4 AM


def day_bounds(date: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) for a logical day: date @ 04:00 → next day @ 04:00."""
    start = date.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


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
