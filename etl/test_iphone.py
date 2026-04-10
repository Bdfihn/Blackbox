import os
import sqlite3
import shutil
import tempfile
import zoneinfo
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from iphone import check_backup, parse_knowledge_db, parse_health

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def make_apple_ts(dt: datetime) -> float:
    """Convert a tz-aware datetime to an Apple CoreData float timestamp."""
    return (dt.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds()


def make_knowledge_db(rows: list[tuple]) -> str:
    """Create a temp SQLite knowledgeC.db with ZOBJECT rows.

    Each row is (bundle_id, stream_name, start_apple_ts, end_apple_ts).
    Returns path to the temp file (caller must delete).
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE ZOBJECT (
            ZBUNDLEID    TEXT,
            ZSTREAMNAME  TEXT,
            ZSTARTDATE   REAL,
            ZENDDATE     REAL
        )
    """)
    conn.executemany("INSERT INTO ZOBJECT VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def make_health_db(step_rows, hr_rows, sleep_rows) -> str:
    """Create a temp healthdb_secure.sqlite.

    step_rows / hr_rows: list of (start_apple_ts, quantity)
    sleep_rows: list of (start_apple_ts, end_apple_ts, category_value)
    Returns path (caller must delete).
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE samples (
            ROWID      INTEGER PRIMARY KEY AUTOINCREMENT,
            data_type  INTEGER,
            start_date REAL,
            end_date   REAL
        );
        CREATE TABLE quantity_samples (
            ROWID    INTEGER PRIMARY KEY,
            quantity REAL
        );
        CREATE TABLE category_samples (
            ROWID INTEGER PRIMARY KEY,
            value INTEGER
        );
    """)
    rowid = 1
    for start_ts, qty in step_rows:
        conn.execute("INSERT INTO samples VALUES (?, 7, ?, ?)", (rowid, start_ts, start_ts + 60))
        conn.execute("INSERT INTO quantity_samples VALUES (?, ?)", (rowid, qty))
        rowid += 1
    for start_ts, qty in hr_rows:
        conn.execute("INSERT INTO samples VALUES (?, 5, ?, ?)", (rowid, start_ts, start_ts + 60))
        conn.execute("INSERT INTO quantity_samples VALUES (?, ?)", (rowid, qty))
        rowid += 1
    for start_ts, end_ts, val in sleep_rows:
        conn.execute("INSERT INTO samples VALUES (?, 63, ?, ?)", (rowid, start_ts, end_ts))
        conn.execute("INSERT INTO category_samples VALUES (?, ?)", (rowid, val))
        rowid += 1
    conn.commit()
    conn.close()
    return path


def mock_backup(db_path: str) -> MagicMock:
    """Return a mock iOSbackup object whose getFileDecryptedCopy returns db_path."""
    m = MagicMock()
    m.getFileDecryptedCopy.return_value = {"fileDecryptedPath": db_path}
    return m


# ── check_backup tests ────────────────────────────────────────────────────────

def test_check_backup_returns_none_when_no_env_paths(monkeypatch):
    monkeypatch.delenv("IPHONE_BACKUP_PATH", raising=False)
    monkeypatch.delenv("IPHONE_BACKUP_PATH2", raising=False)
    assert check_backup() is None


def test_check_backup_returns_none_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("IPHONE_BACKUP_PATH", str(tmp_path / "nonexistent"))
    monkeypatch.delenv("IPHONE_BACKUP_PATH2", raising=False)
    assert check_backup() is None


def test_check_backup_returns_none_when_no_manifest(monkeypatch, tmp_path):
    # Subdir exists but has no Manifest.db
    (tmp_path / "ABC123").mkdir()
    monkeypatch.setenv("IPHONE_BACKUP_PATH", str(tmp_path))
    monkeypatch.delenv("IPHONE_BACKUP_PATH2", raising=False)
    assert check_backup() is None


def test_check_backup_finds_valid_backup(monkeypatch, tmp_path):
    udid_dir = tmp_path / "AABBCCDD1122"
    udid_dir.mkdir()
    (udid_dir / "Manifest.db").write_bytes(b"")
    monkeypatch.setenv("IPHONE_BACKUP_PATH", str(tmp_path))
    monkeypatch.delenv("IPHONE_BACKUP_PATH2", raising=False)

    result = check_backup()
    assert result is not None
    path, udid = result
    assert path == str(tmp_path)
    assert udid == "AABBCCDD1122"


def test_check_backup_falls_back_to_second_path(monkeypatch, tmp_path):
    path1 = tmp_path / "empty_dir"
    path1.mkdir()
    path2 = tmp_path / "backups"
    path2.mkdir()
    udid_dir = path2 / "DDEEFF334455"
    udid_dir.mkdir()
    (udid_dir / "Manifest.db").write_bytes(b"")

    monkeypatch.setenv("IPHONE_BACKUP_PATH", str(path1))
    monkeypatch.setenv("IPHONE_BACKUP_PATH2", str(path2))

    result = check_backup()
    assert result is not None
    assert result[1] == "DDEEFF334455"


# ── parse_knowledge_db tests ──────────────────────────────────────────────────

TARGET_DATE = datetime(2026, 4, 9, tzinfo=LOCAL_TZ)  # 2026-04-09 in ET


def test_parse_knowledge_db_returns_foreground_events():
    """Rows with /app/inFocus and com.* bundle IDs on target_date are returned."""
    # 10:00 AM ET on 2026-04-09 = 14:00 UTC
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))
    end   = make_apple_ts(datetime(2026, 4, 9, 14, 5, 0, tzinfo=timezone.utc))

    db_path = make_knowledge_db([
        ("com.apple.MobileSafari", "/app/inFocus", start, end),
    ])
    try:
        events = parse_knowledge_db(mock_backup(db_path), TARGET_DATE)
        assert len(events) == 1
        assert events[0]["app_bundle_id"] == "com.apple.MobileSafari"
        assert abs(events[0]["duration_secs"] - 300.0) < 1
        assert events[0]["timestamp"].tzinfo is not None
        expected_ts = datetime(2026, 4, 9, 10, 0, 0, tzinfo=LOCAL_TZ)
        assert events[0]["timestamp"] == expected_ts
    finally:
        os.unlink(db_path)


def test_parse_knowledge_db_excludes_background_stream():
    """Rows with a stream other than /app/inFocus are excluded."""
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))
    end   = make_apple_ts(datetime(2026, 4, 9, 14, 5, 0, tzinfo=timezone.utc))

    db_path = make_knowledge_db([
        ("com.apple.MobileSafari", "/app/inBackground", start, end),
    ])
    try:
        events = parse_knowledge_db(mock_backup(db_path), TARGET_DATE)
        assert events == []
    finally:
        os.unlink(db_path)


def test_parse_knowledge_db_excludes_non_com_bundles():
    """Bundle IDs not starting with 'com.' are excluded."""
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))
    end   = make_apple_ts(datetime(2026, 4, 9, 14, 5, 0, tzinfo=timezone.utc))

    db_path = make_knowledge_db([
        ("apple.MobileSafari", "/app/inFocus", start, end),
    ])
    try:
        events = parse_knowledge_db(mock_backup(db_path), TARGET_DATE)
        assert events == []
    finally:
        os.unlink(db_path)


def test_parse_knowledge_db_excludes_other_dates():
    """Events outside the target date window are excluded."""
    # The next day
    start = make_apple_ts(datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc))
    end   = make_apple_ts(datetime(2026, 4, 10, 14, 5, 0, tzinfo=timezone.utc))

    db_path = make_knowledge_db([
        ("com.apple.MobileSafari", "/app/inFocus", start, end),
    ])
    try:
        events = parse_knowledge_db(mock_backup(db_path), TARGET_DATE)
        assert events == []
    finally:
        os.unlink(db_path)


def test_parse_knowledge_db_timestamp_is_local_tz():
    """Returned timestamps are in LOCAL_TZ, not UTC."""
    # 14:00 UTC = 10:00 ET
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))
    end   = make_apple_ts(datetime(2026, 4, 9, 14, 5, 0, tzinfo=timezone.utc))

    db_path = make_knowledge_db([
        ("com.instagram.Instagram", "/app/inFocus", start, end),
    ])
    try:
        events = parse_knowledge_db(mock_backup(db_path), TARGET_DATE)
        ts = events[0]["timestamp"]
        expected = datetime(2026, 4, 9, 10, 0, 0, tzinfo=LOCAL_TZ)
        assert ts == expected  # 14:00 UTC → 10:00 EDT (UTC-4)
        assert ts.tzinfo is not None
    finally:
        os.unlink(db_path)


# ── parse_health tests ────────────────────────────────────────────────────────

def test_parse_health_returns_steps():
    """Steps rows on target_date are returned with type='steps'."""
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))  # 10 AM ET

    db_path = make_health_db(
        step_rows=[(start, 847.0)],
        hr_rows=[],
        sleep_rows=[],
    )
    try:
        records = parse_health(mock_backup(db_path), TARGET_DATE)
        steps = [r for r in records if r["type"] == "steps"]
        assert len(steps) == 1
        assert steps[0]["value"] == 847.0
        assert steps[0]["unit"] == "count"
    finally:
        os.unlink(db_path)


def test_parse_health_returns_heart_rate():
    """HR rows on target_date are returned with type='heart_rate'."""
    start = make_apple_ts(datetime(2026, 4, 9, 15, 0, 0, tzinfo=timezone.utc))  # 11 AM ET

    db_path = make_health_db(
        step_rows=[],
        hr_rows=[(start, 72.0)],
        sleep_rows=[],
    )
    try:
        records = parse_health(mock_backup(db_path), TARGET_DATE)
        hr = [r for r in records if r["type"] == "heart_rate"]
        assert len(hr) == 1
        assert hr[0]["value"] == 72.0
        assert hr[0]["unit"] == "count/min"
    finally:
        os.unlink(db_path)


def test_parse_health_returns_sleep_with_duration():
    """Sleep rows return duration in seconds (end_ts - start_ts)."""
    start = make_apple_ts(datetime(2026, 4, 9, 4, 0, 0, tzinfo=timezone.utc))   # midnight ET
    end   = make_apple_ts(datetime(2026, 4, 9, 11, 0, 0, tzinfo=timezone.utc))  # 7 AM ET

    db_path = make_health_db(
        step_rows=[],
        hr_rows=[],
        sleep_rows=[(start, end, 1)],
    )
    try:
        records = parse_health(mock_backup(db_path), TARGET_DATE)
        sleep = [r for r in records if r["type"] == "sleep"]
        assert len(sleep) == 1
        assert abs(sleep[0]["value"] - 7 * 3600) < 1
        assert sleep[0]["unit"] == "sec"
    finally:
        os.unlink(db_path)


def test_parse_health_excludes_other_dates():
    """Records outside the target date window are excluded."""
    start = make_apple_ts(datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc))  # next day

    db_path = make_health_db(
        step_rows=[(start, 500.0)],
        hr_rows=[],
        sleep_rows=[],
    )
    try:
        records = parse_health(mock_backup(db_path), TARGET_DATE)
        assert records == []
    finally:
        os.unlink(db_path)


def test_parse_health_timestamp_is_local_tz():
    """Returned timestamps are LOCAL_TZ-aware with correct hour."""
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))  # 10 AM ET

    db_path = make_health_db(
        step_rows=[(start, 100.0)],
        hr_rows=[],
        sleep_rows=[],
    )
    try:
        records = parse_health(mock_backup(db_path), TARGET_DATE)
        ts = records[0]["timestamp"]
        expected = datetime(2026, 4, 9, 10, 0, 0, tzinfo=LOCAL_TZ)
        assert ts == expected  # 14:00 UTC → 10:00 EDT (UTC-4)
        assert ts.tzinfo is not None
    finally:
        os.unlink(db_path)
