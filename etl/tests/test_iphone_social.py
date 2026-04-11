import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
import zoneinfo

from sources.iphone_social import IPhoneSocialSource, parse_interactions, _floor_15, _readable_app
from sources.iphone_health import APPLE_EPOCH

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

# 2024-01-15 14:23:00 UTC → 09:23 ET → floors to 09:15 ET
_TS_UTC = datetime(2024, 1, 15, 14, 23, 0, tzinfo=timezone.utc)
_TS_APPLE = (_TS_UTC - APPLE_EPOCH).total_seconds()

# 2024-01-15 14:27:00 UTC → 09:27 ET → floors to 09:15 ET (same bucket)
_TS2_UTC = datetime(2024, 1, 15, 14, 27, 0, tzinfo=timezone.utc)
_TS2_APPLE = (_TS2_UTC - APPLE_EPOCH).total_seconds()


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ZCONTACTS (Z_PK INTEGER PRIMARY KEY, ZDISPLAYNAME TEXT)")
    conn.execute("""
        CREATE TABLE ZINTERACTIONS (
            Z_PK INTEGER PRIMARY KEY,
            ZSTARTDATE REAL,
            ZBUNDLEID TEXT,
            ZDIRECTION INTEGER,
            ZSENDER INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE Z_2INTERACTIONRECIPIENT (
            Z_2INTERACTIONS INTEGER,
            Z_3RECIPIENTS INTEGER
        )
    """)
    conn.execute("INSERT INTO ZCONTACTS VALUES (1, 'John Smith')")
    conn.execute("INSERT INTO ZCONTACTS VALUES (2, 'Jane Doe')")
    conn.execute("INSERT INTO ZINTERACTIONS VALUES (1, ?, 'com.apple.MobileSMS', 0, 1)", (_TS_APPLE,))
    conn.execute("INSERT INTO ZINTERACTIONS VALUES (2, ?, 'com.apple.MobileSMS', 1, 2)", (_TS2_APPLE,))
    conn.execute("INSERT INTO Z_2INTERACTIONRECIPIENT VALUES (1, 2)")
    conn.execute("INSERT INTO Z_2INTERACTIONRECIPIENT VALUES (2, 1)")
    conn.commit()
    return conn


@contextmanager
def _mock_db(conn):
    yield conn


def test_readable_app_known_bundle():
    assert _readable_app("com.apple.MobileSMS") == "Messages"
    assert _readable_app("com.apple.mobilephone") == "Phone"


def test_readable_app_unknown_bundle():
    assert _readable_app("com.example.CustomApp") == "com.example.CustomApp"


def test_floor_15():
    tz = zoneinfo.ZoneInfo("America/New_York")
    ts = datetime(2024, 1, 15, 14, 23, 0, tzinfo=tz)
    assert _floor_15(ts).minute == 15
    assert _floor_15(ts).second == 0

    assert _floor_15(datetime(2024, 1, 15, 14, 0, 0, tzinfo=tz)).minute == 0
    assert _floor_15(datetime(2024, 1, 15, 14, 29, 0, tzinfo=tz)).minute == 15
    assert _floor_15(datetime(2024, 1, 15, 14, 45, 0, tzinfo=tz)).minute == 45


def test_parse_interactions_returns_records_in_window():
    conn = _make_db()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_social.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_interactions(None, start, end, LOCAL_TZ)

    assert len(records) == 2
    assert records[0]["bundle_id"] == "com.apple.MobileSMS"
    assert records[0]["sender_name"] == "John Smith"
    assert records[0]["recipient_name"] == "Jane Doe"


def test_parse_interactions_excludes_out_of_window():
    conn = _make_db()
    start = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 17, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_social.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_interactions(None, start, end, LOCAL_TZ)

    assert records == []


def test_get_chunks_groups_into_15min_buckets():
    conn = _make_db()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)
    source = IPhoneSocialSource(None, LOCAL_TZ)

    with patch("sources.iphone_social.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        chunks = source.get_chunks(start, end)

    # Both events fall into the same 15-min bucket
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.source == "iphone_social"
    assert chunk.total_secs == 900
    assert "Messages" in chunk.text
    assert chunk.metadata["event_count"] == 2
    assert set(chunk.metadata["contacts"]) == {"John Smith", "Jane Doe"}


def test_parse_interactions_handles_missing_junction_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ZCONTACTS (Z_PK INTEGER PRIMARY KEY, ZDISPLAYNAME TEXT)")
    conn.execute("""
        CREATE TABLE ZINTERACTIONS (
            Z_PK INTEGER PRIMARY KEY,
            ZSTARTDATE REAL,
            ZBUNDLEID TEXT,
            ZDIRECTION INTEGER,
            ZSENDER INTEGER
        )
    """)
    # Z_2INTERACTIONRECIPIENT deliberately omitted
    conn.execute("INSERT INTO ZCONTACTS VALUES (1, 'John Smith')")
    conn.execute("INSERT INTO ZINTERACTIONS VALUES (1, ?, 'com.apple.MobileSMS', 0, 1)", (_TS_APPLE,))
    conn.commit()

    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_social.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_interactions(None, start, end, LOCAL_TZ)

    assert len(records) == 1
    assert records[0]["sender_name"] == "John Smith"
    assert records[0]["recipient_name"] is None
