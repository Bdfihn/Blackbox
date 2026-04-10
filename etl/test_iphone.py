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
    path = tempfile.mktemp(suffix=".db")
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
    path = tempfile.mktemp(suffix=".db")
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
