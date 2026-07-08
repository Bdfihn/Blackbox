import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
import zoneinfo

from sources.iphone_health import IPhoneHealthSource
from sources.iphone_backup import APPLE_EPOCH

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

_WINDOW_START = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
_WINDOW_END = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

# 2024-01-15 14:23 UTC → 09:23 ET
_TS = (datetime(2024, 1, 15, 14, 23, 0, tzinfo=timezone.utc) - APPLE_EPOCH).total_seconds()


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE samples (ROWID INTEGER PRIMARY KEY, start_date REAL, end_date REAL, data_type INTEGER)")
    conn.execute("CREATE TABLE quantity_samples (ROWID INTEGER PRIMARY KEY, quantity REAL)")
    conn.execute("CREATE TABLE category_samples (ROWID INTEGER PRIMARY KEY, value INTEGER)")
    conn.execute("""
        CREATE TABLE workout_activities (
            ROWID INTEGER PRIMARY KEY,
            activity_type INTEGER,
            start_date REAL,
            end_date REAL,
            duration REAL,
            is_primary_activity INTEGER
        )
    """)
    conn.execute("CREATE TABLE workout_statistics (workout_activity_id INTEGER, data_type INTEGER, quantity REAL)")
    return conn


def _insert_quantity(conn, rowid, data_type, quantity, start=_TS, end=None):
    conn.execute("INSERT INTO samples VALUES (?, ?, ?, ?)", (rowid, start, end or start, data_type))
    conn.execute("INSERT INTO quantity_samples VALUES (?, ?)", (rowid, quantity))


def _insert_category(conn, rowid, data_type, value, start=_TS, end=None):
    conn.execute("INSERT INTO samples VALUES (?, ?, ?, ?)", (rowid, start, end or start, data_type))
    conn.execute("INSERT INTO category_samples VALUES (?, ?)", (rowid, value))


@contextmanager
def _mock_db(conn):
    yield conn


def _get_chunks(conn):
    source = IPhoneHealthSource(None, LOCAL_TZ)
    with patch("sources.iphone_health.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        return source.get_chunks(_WINDOW_START, _WINDOW_END)


def test_activity_chunk_aggregates_steps_and_heart_rate():
    conn = _make_db()
    _insert_quantity(conn, 1, 7, 300)          # steps
    _insert_quantity(conn, 2, 7, 220, start=_TS + 60)
    _insert_quantity(conn, 3, 5, 1.5, start=_TS + 120)  # HR in beats/sec

    chunks = _get_chunks(conn)
    activity = [c for c in chunks if "Activity:" in c.text]
    assert len(activity) == 1
    assert "520 steps" in activity[0].text
    assert "avg HR 90bpm" in activity[0].text


def test_hrv_is_read_from_type_139_not_audio_exposure():
    conn = _make_db()
    _insert_quantity(conn, 1, 139, 25)  # HRV SDNN in ms
    _insert_quantity(conn, 2, 139, 35, start=_TS + 60)
    _insert_quantity(conn, 3, 172, 60, start=_TS + 120)  # environmental audio dB decoy

    chunks = _get_chunks(conn)
    vitals = [c for c in chunks if "Daily vitals" in c.text]
    assert len(vitals) == 1
    assert "HRV 30ms" in vitals[0].text


def test_sleep_respiratory_rate_reads_type_61_and_converts_to_breaths_per_min():
    conn = _make_db()
    _insert_category(conn, 1, 63, 4, start=_TS, end=_TS + 3600)  # deep sleep
    _insert_quantity(conn, 2, 61, 0.25, start=_TS + 600)         # breaths/sec
    _insert_quantity(conn, 3, 61, 0.35, start=_TS + 1200)

    chunks = _get_chunks(conn)
    sleep = [c for c in chunks if "Sleep:" in c.text]
    assert len(sleep) == 1
    assert "Avg respiratory rate: 18.0 breaths/min" in sleep[0].text


def test_workout_names_match_apple_enum():
    conn = _make_db()
    for rowid, activity_type in enumerate([50, 59, 37, 52, 24], start=1):
        conn.execute(
            "INSERT INTO workout_activities VALUES (?, ?, ?, ?, ?, 1)",
            (rowid, activity_type, _TS + rowid * 3600, _TS + rowid * 3600 + 1800, 1800),
        )

    chunks = _get_chunks(conn)
    texts = "\n".join(c.text for c in chunks)
    assert "Strength Training" in texts
    assert "Core Training" in texts
    assert "Running" in texts
    assert "Walking" in texts
    assert "Hiking" in texts


def test_activity_chunk_includes_physical_effort():
    conn = _make_db()
    _insert_quantity(conn, 1, 7, 500)                    # steps
    _insert_quantity(conn, 2, 286, 2.0, start=_TS + 60)  # physical effort METs
    _insert_quantity(conn, 3, 286, 4.0, start=_TS + 120)

    chunks = _get_chunks(conn)
    activity = [c for c in chunks if "Activity:" in c.text]
    assert len(activity) == 1
    assert "avg effort 3.0 METs" in activity[0].text


def test_vitals_include_daylight_exercise_minutes_and_walking_hr():
    conn = _make_db()
    _insert_quantity(conn, 1, 118, 68)                    # resting HR
    _insert_quantity(conn, 2, 279, 12, start=_TS + 60)    # daylight minutes
    _insert_quantity(conn, 3, 279, 24, start=_TS + 120)
    _insert_quantity(conn, 4, 75, 20, start=_TS + 180)    # exercise minutes
    _insert_quantity(conn, 5, 75, 22, start=_TS + 240)
    _insert_quantity(conn, 6, 137, 100, start=_TS + 300)  # walking HR avg in bpm

    chunks = _get_chunks(conn)
    vitals = [c for c in chunks if "Daily vitals" in c.text]
    assert len(vitals) == 1
    text = vitals[0].text
    assert "resting HR 68bpm" in text
    assert "walking HR avg 100bpm" in text
    assert "42 exercise min" in text
    assert "36 min daylight" in text


def test_sleep_vitals_and_workout_chunks_carry_kind_metadata():
    conn = _make_db()
    _insert_category(conn, 1, 63, 4, start=_TS, end=_TS + 3600)  # deep sleep
    _insert_quantity(conn, 2, 118, 68, start=_TS + 60)           # resting HR
    conn.execute(
        "INSERT INTO workout_activities VALUES (1, 37, ?, ?, 1800, 1)",
        (_TS + 7200, _TS + 9000),
    )

    chunks = _get_chunks(conn)
    sleep = next(c for c in chunks if "Sleep:" in c.text)
    assert sleep.metadata["kind"] == "sleep"
    vitals = next(c for c in chunks if "Daily vitals" in c.text)
    assert vitals.metadata["kind"] == "vitals"
    workout = next(c for c in chunks if "Running" in c.text)
    assert workout.metadata["kind"] == "workout"


def test_no_vitals_chunk_when_no_data():
    conn = _make_db()
    chunks = _get_chunks(conn)
    assert [c for c in chunks if "Daily vitals" in c.text] == []
