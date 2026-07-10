from datetime import datetime

from sources.base import Chunk, floor_dt, fmt_duration


def test_floor_dt_15min():
    ts = datetime(2024, 1, 15, 9, 47, 33)
    assert floor_dt(ts, 15) == datetime(2024, 1, 15, 9, 45, 0)


def test_floor_dt_60min():
    ts = datetime(2024, 1, 15, 9, 47, 33)
    assert floor_dt(ts, 60) == datetime(2024, 1, 15, 9, 0, 0)


def test_chunk_metadata_defaults_to_empty_dict():
    c = Chunk(window_start="2024-01-15T14:00:00", text="hello", source="test")
    assert c.metadata == {}


def test_chunk_metadata_accepts_arbitrary_keys():
    c = Chunk(
        window_start="2024-01-15T14:00:00",
        text="hello",
        source="test",
        metadata={"lat": 40.7128, "lon": -74.006},
    )
    assert c.metadata["lat"] == 40.7128
    assert c.metadata["lon"] == -74.006


def test_fmt_duration_minutes_only():
    assert fmt_duration(600) == "10m"


def test_fmt_duration_hours_only():
    assert fmt_duration(7200) == "2h"


def test_fmt_duration_hours_and_minutes():
    assert fmt_duration(5400) == "1h 30m"


def test_fmt_duration_zero():
    assert fmt_duration(0) == "0m"
