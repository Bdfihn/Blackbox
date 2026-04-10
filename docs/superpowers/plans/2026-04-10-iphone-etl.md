# iPhone ETL Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add iPhone backup data (app usage, health, sleep) as a second ETL source running in the same nightly pipeline as ActivityWatch, with all chunks merged into a single timestamp-sorted timeline for Qdrant storage and diary generation.

**Architecture:** A new `etl/iphone.py` module provides three functions to discover, decrypt, and query iOS backup databases using the `iOSbackup` library. `etl/etl.py` calls them after ActivityWatch ingestion, converts results into the existing chunk format (with `source: "iphone"` / `"iphone_health"`), sorts the merged `all_chunks` list by `window_start`, then feeds the combined timeline into the unchanged `upsert_chunks` + `generate_diary_entry` pipeline. All iPhone parsing is wrapped in try/except so ActivityWatch-only ingestion continues on failure.

**Tech Stack:** Python 3.12, iOSbackup (pip install iOSbackup), sqlite3, zoneinfo, Docker read-only volume mounts

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `etl/iphone.py` | **Create** | Backup discovery (`check_backup`), app usage parsing (`parse_knowledge_db`), health parsing (`parse_health`) |
| `etl/test_iphone.py` | **Create** | Unit tests for all three functions using mocked iOSbackup + real temp SQLite files |
| `etl/etl.py` | **Modify** | Import iphone.py functions, add `chunk_iphone_apps`, `chunk_iphone_health` helpers, integrate into `run_etl`, sort `all_chunks`, fix `upsert_chunks` payload, update diary prompt |
| `etl/requirements.txt` | **Modify** | Add `iOSbackup` |
| `docker-compose.yml` | **Modify** | Add `IPHONE_BACKUP_PASSWORD`, `IPHONE_BACKUP_PATH`, `IPHONE_BACKUP_PATH2` env vars; add two read-only volume mounts for backup folders |

---

### Task 1: Write failing tests for `check_backup` and create `etl/iphone.py` stub

**Files:**
- Create: `etl/test_iphone.py`
- Create: `etl/iphone.py` (empty stub)

- [ ] **Step 1: Create `etl/test_iphone.py` with test infrastructure and `check_backup` tests**

```python
# etl/test_iphone.py
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
```

- [ ] **Step 2: Create empty `etl/iphone.py` stub so the import doesn't crash**

```python
# etl/iphone.py
def check_backup(): ...
def parse_knowledge_db(backup, target_date): ...
def parse_health(backup, target_date): ...
```

- [ ] **Step 3: Run tests — expect `check_backup` tests to fail with NotImplementedError or wrong return values**

```
cd etl && python -m pytest test_iphone.py::test_check_backup_returns_none_when_no_env_paths \
  test_iphone.py::test_check_backup_returns_none_when_dir_missing \
  test_iphone.py::test_check_backup_returns_none_when_no_manifest \
  test_iphone.py::test_check_backup_finds_valid_backup \
  test_iphone.py::test_check_backup_falls_back_to_second_path -v
```

Expected: all FAIL or ERROR (stub returns `None` via `...` which actually passes `returns_none` cases — that's fine, the `finds_valid_backup` test will fail).

---

### Task 2: Implement `check_backup` and run tests

**Files:**
- Modify: `etl/iphone.py`

- [ ] **Step 1: Replace stub with full `iphone.py` including `check_backup` implementation**

```python
# etl/iphone.py
"""
iphone.py — iPhone backup parsing for the Blackbox ETL pipeline.

Requires:  pip install iOSbackup
Env vars:
  IPHONE_BACKUP_PASSWORD   — backup decryption password
  IPHONE_BACKUP_PATH       — first search path (mounted in Docker at /app/iphone_backup)
  IPHONE_BACKUP_PATH2      — second search path (mounted at /app/iphone_backup2)
  TIMEZONE                 — local timezone (default: America/New_York)
"""

import os
import shutil
import sqlite3
import tempfile
import zoneinfo
from datetime import datetime, timedelta, timezone

LOCAL_TZ    = zoneinfo.ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# healthdb_secure.sqlite data_type constants (observed iOS 16-17; verify via Manifest if needed)
_STEPS_TYPE = 7   # HKQuantityTypeIdentifierStepCount
_HR_TYPE    = 5   # HKQuantityTypeIdentifierHeartRate
_SLEEP_TYPE = 63  # HKCategoryTypeIdentifierSleepAnalysis


def _apple_ts(apple_secs: float) -> datetime:
    """Convert Apple CoreData timestamp (seconds since 2001-01-01 UTC) to UTC datetime."""
    return APPLE_EPOCH + timedelta(seconds=apple_secs)


def check_backup() -> tuple[str, str] | None:
    """Scan configured backup paths for a valid iOS backup.

    Checks IPHONE_BACKUP_PATH then IPHONE_BACKUP_PATH2. A valid backup is a
    subdirectory containing Manifest.db.

    Returns (backuproot, udid) for the first valid backup found, or None.
    """
    for env_key in ("IPHONE_BACKUP_PATH", "IPHONE_BACKUP_PATH2"):
        path = os.getenv(env_key, "")
        if not path or not os.path.isdir(path):
            continue
        try:
            for entry in os.scandir(path):
                if entry.is_dir() and os.path.exists(os.path.join(entry.path, "Manifest.db")):
                    return path, entry.name
        except OSError:
            continue
    return None


def parse_knowledge_db(backup, target_date: datetime) -> list[dict]:
    """Extract foreground app usage from knowledgeC.db for target_date.

    Args:
        backup: An open iOSbackup instance.
        target_date: A LOCAL_TZ-aware datetime; extracts midnight–midnight that day.

    Returns:
        List of {timestamp (LOCAL_TZ datetime), app_bundle_id, duration_secs}.
    """
    start_local = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local   = start_local + timedelta(days=1)

    tmpdir = tempfile.mkdtemp()
    try:
        result  = backup.getFileDecryptedCopy(
            fileRelativePath="Library/CoreDuet/Knowledge/knowledgeC.db",
            domain="HomeDomain",
            targetFolder=tmpdir,
        )
        db_path = result.get("fileDecryptedPath") or os.path.join(tmpdir, "knowledgeC.db")
        conn    = sqlite3.connect(db_path)

        rows = conn.execute("""
            SELECT ZBUNDLEID, ZSTARTDATE, ZENDDATE
            FROM   ZOBJECT
            WHERE  ZSTREAMNAME = '/app/inFocus'
              AND  ZBUNDLEID   LIKE 'com.%'
              AND  ZSTARTDATE  IS NOT NULL
        """).fetchall()
        conn.close()

        events = []
        for bundle_id, start_ts, end_ts in rows:
            ts = _apple_ts(start_ts).astimezone(LOCAL_TZ)
            if not (start_local <= ts < end_local):
                continue
            duration = (end_ts - start_ts) if end_ts is not None else 0.0
            events.append({
                "timestamp":     ts,
                "app_bundle_id": bundle_id,
                "duration_secs": duration,
            })
        return events
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def parse_health(backup, target_date: datetime) -> list[dict]:
    """Extract steps, heart rate, and sleep from healthdb_secure.sqlite for target_date.

    Args:
        backup: An open iOSbackup instance.
        target_date: A LOCAL_TZ-aware datetime; extracts midnight–midnight that day.

    Returns:
        List of {timestamp (LOCAL_TZ datetime), type ('steps'|'heart_rate'|'sleep'),
                 value (float), unit (str)}.
        For 'sleep', value is duration in seconds (end_date − start_date).
    """
    start_local = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local   = start_local + timedelta(days=1)

    tmpdir = tempfile.mkdtemp()
    try:
        result  = backup.getFileDecryptedCopy(
            fileRelativePath="Library/Application Support/com.apple.healthstore/healthdb_secure.sqlite",
            domain="HealthDomain",
            targetFolder=tmpdir,
        )
        db_path = result.get("fileDecryptedPath") or os.path.join(tmpdir, "healthdb_secure.sqlite")
        conn    = sqlite3.connect(db_path)

        records = []

        # Steps
        for start_ts, qty in conn.execute(
            "SELECT s.start_date, qs.quantity "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type = ?",
            (_STEPS_TYPE,),
        ).fetchall():
            ts = _apple_ts(start_ts).astimezone(LOCAL_TZ)
            if start_local <= ts < end_local:
                records.append({"timestamp": ts, "type": "steps", "value": qty, "unit": "count"})

        # Heart rate
        for start_ts, qty in conn.execute(
            "SELECT s.start_date, qs.quantity "
            "FROM samples s JOIN quantity_samples qs ON qs.ROWID = s.ROWID "
            "WHERE s.data_type = ?",
            (_HR_TYPE,),
        ).fetchall():
            ts = _apple_ts(start_ts).astimezone(LOCAL_TZ)
            if start_local <= ts < end_local:
                records.append({"timestamp": ts, "type": "heart_rate", "value": qty, "unit": "count/min"})

        # Sleep
        for start_ts, end_ts, _val in conn.execute(
            "SELECT s.start_date, s.end_date, cs.value "
            "FROM samples s JOIN category_samples cs ON cs.ROWID = s.ROWID "
            "WHERE s.data_type = ?",
            (_SLEEP_TYPE,),
        ).fetchall():
            ts = _apple_ts(start_ts).astimezone(LOCAL_TZ)
            if start_local <= ts < end_local:
                duration = (end_ts - start_ts) if end_ts is not None else 0.0
                records.append({"timestamp": ts, "type": "sleep", "value": duration, "unit": "sec"})

        conn.close()
        return records
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

- [ ] **Step 2: Run `check_backup` tests**

```
cd etl && python -m pytest test_iphone.py -k "check_backup" -v
```

Expected: all 5 PASS.

- [ ] **Step 3: Commit**

```bash
git add etl/iphone.py etl/test_iphone.py
git commit -m "feat: add iphone.py with check_backup + parse stubs"
```

---

### Task 3: Write failing tests for `parse_knowledge_db`, then verify they fail

**Files:**
- Modify: `etl/test_iphone.py`

- [ ] **Step 1: Add `parse_knowledge_db` tests to `etl/test_iphone.py`**

Append this block to the file:

```python
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
        assert ts.hour == 10  # 14:00 UTC → 10:00 ET
        assert ts.tzinfo is not None
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run new tests — expect them to FAIL (stub returns `[]`)**

```
cd etl && python -m pytest test_iphone.py -k "parse_knowledge" -v
```

Expected: `test_parse_knowledge_db_returns_foreground_events` FAIL, others may PASS (empty list is correct for them — that's expected, they're validating exclusion logic we haven't implemented yet).

---

### Task 4: Run all tests and confirm `parse_knowledge_db` passes

The implementation is already in Task 2's `iphone.py`. Run the full test suite:

- [ ] **Step 1: Run all `parse_knowledge_db` tests**

```
cd etl && python -m pytest test_iphone.py -k "parse_knowledge" -v
```

Expected: all 5 PASS.

- [ ] **Step 2: Commit**

```bash
git add etl/test_iphone.py etl/iphone.py
git commit -m "feat: implement parse_knowledge_db with date filtering and tests"
```

---

### Task 5: Write failing tests for `parse_health`

**Files:**
- Modify: `etl/test_iphone.py`

- [ ] **Step 1: Append `parse_health` tests to `etl/test_iphone.py`**

```python
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
        assert sleep[0]["value"] == pytest.approx(7 * 3600, abs=1)
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
    """Returned timestamps are LOCAL_TZ-aware."""
    start = make_apple_ts(datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc))  # 10 AM ET

    db_path = make_health_db(
        step_rows=[(start, 100.0)],
        hr_rows=[],
        sleep_rows=[],
    )
    try:
        records = parse_health(mock_backup(db_path), TARGET_DATE)
        assert records[0]["timestamp"].hour == 10
        assert records[0]["timestamp"].tzinfo is not None
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run new tests — expect FAIL (stub returns `[]`)**

```
cd etl && python -m pytest test_iphone.py -k "parse_health" -v
```

Expected: `test_parse_health_returns_steps`, `test_parse_health_returns_heart_rate`, `test_parse_health_returns_sleep_with_duration`, `test_parse_health_timestamp_is_local_tz` FAIL. `test_parse_health_excludes_other_dates` may PASS (empty list matches).

---

### Task 6: Run all tests and confirm `parse_health` passes

The implementation is already in Task 2's `iphone.py`.

- [ ] **Step 1: Run all `parse_health` tests**

```
cd etl && python -m pytest test_iphone.py -k "parse_health" -v
```

Expected: all 5 PASS.

- [ ] **Step 2: Run full test suite**

```
cd etl && python -m pytest test_iphone.py -v
```

Expected: all 15 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add etl/test_iphone.py
git commit -m "feat: add parse_health tests — all iphone.py tests passing"
```

---

### Task 7: Update `etl/requirements.txt`

**Files:**
- Modify: `etl/requirements.txt`

- [ ] **Step 1: Add `iOSbackup` to requirements**

Replace the file contents with:

```
requests==2.31.0
qdrant-client==1.9.1
ollama==0.2.1
schedule==1.2.1
python-dotenv==1.0.1
iOSbackup
```

(No version pin — iOSbackup is under active development and the latest version is safest.)

- [ ] **Step 2: Commit**

```bash
git add etl/requirements.txt
git commit -m "chore: add iOSbackup dependency"
```

---

### Task 8: Add `chunk_iphone_apps` and `chunk_iphone_health` to `etl/etl.py`

**Files:**
- Modify: `etl/etl.py`

These two helpers convert iphone.py output into the same chunk shape as `chunk_events` so the rest of the pipeline is unchanged.

- [ ] **Step 1: Add the two chunking helpers after the existing `chunk_events` function (after line 168)**

Insert the following after the closing `return chunks` of `chunk_events`:

```python
def chunk_iphone_apps(events: list[dict], chunk_minutes: int = CHUNK_MINUTES) -> list[dict]:
    """Convert knowledgeC foreground events into 5-min chunks matching ActivityWatch format."""
    if not events:
        return []

    buckets: dict[datetime, dict[str, float]] = {}
    for event in events:
        ts = event["timestamp"]  # already LOCAL_TZ-aware
        floored = ts.replace(
            minute=(ts.minute // chunk_minutes) * chunk_minutes,
            second=0,
            microsecond=0,
        )
        app_name = event["app_bundle_id"].split(".")[-1]  # e.g. "instagram"
        if floored not in buckets:
            buckets[floored] = {}
        buckets[floored][app_name] = buckets[floored].get(app_name, 0) + event["duration_secs"]

    chunks = []
    for window_start, app_totals in sorted(buckets.items()):
        top_apps = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:5]
        total = sum(app_totals.values())
        text = (
            f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
            f"iPhone activity for {chunk_minutes} minutes. "
            f"Top apps: {', '.join(f'{a}({round(s/60,1)}m)' for a, s in top_apps)}."
        )
        chunks.append({
            "window_start": window_start.isoformat(),
            "text":         text,
            "apps":         [a for a, _ in top_apps],
            "total_secs":   total,
            "source":       "iphone",
        })
    return chunks


def chunk_iphone_health(records: list[dict]) -> list[dict]:
    """Convert health records into hourly summary chunks and per-sleep-session chunks."""
    if not records:
        return []

    chunks = []

    # ── Hourly summaries for steps + heart rate ───────────────────────────────
    hourly_steps: dict[datetime, float] = {}
    hourly_hr: dict[datetime, list[float]] = {}

    for r in records:
        if r["type"] in ("steps", "heart_rate"):
            ts = r["timestamp"]
            hour_key = ts.replace(minute=0, second=0, microsecond=0)
            if r["type"] == "steps":
                hourly_steps[hour_key] = hourly_steps.get(hour_key, 0) + r["value"]
            else:
                hourly_hr.setdefault(hour_key, []).append(r["value"])

    for hour in sorted(set(hourly_steps) | set(hourly_hr)):
        parts = []
        if hour in hourly_steps:
            parts.append(f"{int(hourly_steps[hour])} steps")
        if hour in hourly_hr:
            avg_hr = sum(hourly_hr[hour]) / len(hourly_hr[hour])
            parts.append(f"avg HR {round(avg_hr)}bpm")
        text = (
            f"[{hour.strftime('%Y-%m-%d %H:%M')}] "
            f"Health summary: {', '.join(parts)}."
        )
        chunks.append({
            "window_start": hour.isoformat(),
            "text":         text,
            "apps":         [],
            "total_secs":   3600,
            "source":       "iphone_health",
        })

    # ── Sleep sessions ────────────────────────────────────────────────────────
    for r in records:
        if r["type"] == "sleep":
            ts = r["timestamp"]
            duration_hours = r["value"] / 3600
            text = (
                f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
                f"Sleep session: {round(duration_hours, 1)} hours."
            )
            chunks.append({
                "window_start": ts.isoformat(),
                "text":         text,
                "apps":         [],
                "total_secs":   int(r["value"]),
                "source":       "iphone_health",
            })

    return chunks
```

- [ ] **Step 2: Fix `upsert_chunks` payload to use `.get()` for optional fields (line ~189)**

Find this block inside `upsert_chunks`:
```python
        point = PointStruct(
            id=chunk_id,
            vector=vector,
            payload={
                "text": chunk["text"],
                "window_start": chunk["window_start"],
                "apps": chunk["apps"],
                "total_secs": chunk["total_secs"],
                "source": chunk["source"],
                "date": chunk["window_start"][:10],
            }
        )
```

Replace with:
```python
        point = PointStruct(
            id=chunk_id,
            vector=vector,
            payload={
                "text":         chunk["text"],
                "window_start": chunk["window_start"],
                "apps":         chunk.get("apps", []),
                "total_secs":   chunk.get("total_secs", 0),
                "source":       chunk["source"],
                "date":         chunk["window_start"][:10],
            }
        )
```

- [ ] **Step 3: Commit**

```bash
git add etl/etl.py
git commit -m "feat: add chunk_iphone_apps, chunk_iphone_health helpers; fix upsert_chunks payload"
```

---

### Task 9: Integrate iPhone ingestion into `run_etl` and sort the timeline

**Files:**
- Modify: `etl/etl.py`

- [ ] **Step 1: Add `iphone` imports at the top of `etl/etl.py` (after the existing imports)**

After `from qdrant_client.models import ...` add:

```python
from iphone import check_backup, parse_knowledge_db, parse_health
from iOSbackup import iOSbackup as _IOSBackup
```

- [ ] **Step 2: Replace the `run_etl` function body after the ActivityWatch section**

The current `run_etl` ends with:
```python
    # Upsert to Qdrant
    upsert_chunks(all_chunks)

    # Generate and write diary
    log.info("Generating diary entry...")
    diary_content = generate_diary_entry(date_str, all_chunks)
    write_diary(date_str, diary_content)

    log.info(f"ETL complete for {date_str}. {len(all_chunks)} chunks processed.")
```

Replace those final lines with:

```python
    # ── iPhone backup ingestion ───────────────────────────────────────────────
    try:
        backup_info = check_backup()
        if backup_info:
            backuproot, udid = backup_info
            password = os.getenv("IPHONE_BACKUP_PASSWORD", "")
            backup = _IOSBackup(
                udid=udid,
                cleartextpassword=password,
                backuproot=backuproot,
            )
            log.info(f"iPhone backup found: {udid} at {backuproot}")

            app_events = parse_knowledge_db(backup, target_date)
            log.info(f"  knowledgeC: {len(app_events)} foreground events")
            all_chunks.extend(chunk_iphone_apps(app_events))

            health_records = parse_health(backup, target_date)
            log.info(f"  healthdb: {len(health_records)} records")
            all_chunks.extend(chunk_iphone_health(health_records))
        else:
            log.info("No iPhone backup found — skipping iPhone data.")
    except Exception as e:
        log.warning(f"iPhone ingestion failed, continuing without it: {e}")

    # ── Sort all sources by timestamp before upsert + diary ──────────────────
    all_chunks.sort(key=lambda c: c["window_start"])

    # Upsert to Qdrant
    upsert_chunks(all_chunks)

    # Generate and write diary
    log.info("Generating diary entry...")
    diary_content = generate_diary_entry(date_str, all_chunks)
    write_diary(date_str, diary_content)

    log.info(f"ETL complete for {date_str}. {len(all_chunks)} chunks processed.")
```

- [ ] **Step 3: Update `generate_diary_entry` prompt to reflect multi-source input (line ~223)**

Find the prompt string in `generate_diary_entry`:
```python
    prompt = f"""You are writing a personal productivity diary entry for {date}.
Below is a timeline of computer activity logged automatically throughout the day.
Write a concise, honest diary entry (3-5 paragraphs) that:
- Summarises what the person worked on and when
- Notes any apparent focus sessions or distracted periods
- Identifies the most productive and least productive parts of the day
- Uses plain, first-person language as if the person is reflecting on their own day

Activity timeline:
{timeline}

Write only the diary entry, no preamble."""
```

Replace with:
```python
    prompt = f"""You are writing a personal life and productivity diary entry for {date}.
Below is a chronological timeline automatically logged from multiple sources: PC activity (ActivityWatch), iPhone app usage, and iPhone health data (steps, heart rate, sleep).
Write a concise, honest diary entry (3-5 paragraphs) that:
- Summarises what the person worked on, used their phone for, and how they felt physically (based on health data)
- Notes any apparent focus sessions, distractions, or patterns across devices
- Identifies the most and least productive parts of the day
- Includes any notable health signals (sleep quality, step count, elevated heart rate)
- Uses plain, first-person language as if the person is reflecting on their own day

Timeline (all sources, chronological):
{timeline}

Write only the diary entry, no preamble."""
```

- [ ] **Step 4: Commit**

```bash
git add etl/etl.py
git commit -m "feat: integrate iPhone data into run_etl; sort unified timeline; update diary prompt"
```

---

### Task 10: Update `docker-compose.yml`

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Replace the full `docker-compose.yml` with the updated version**

```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    container_name: blackbox-qdrant
    restart: unless-stopped
    ports:
      - "6333:6333"
    volumes:
      - qdrant_data:/qdrant/storage

  etl:
    build:
      context: ./etl
    container_name: blackbox-etl
    restart: unless-stopped
    depends_on:
      - qdrant
    volumes:
      - ./diary:/app/diary
      - ./data:/app/data
      - "C:/Users/Bdfihn/AppData/Roaming/Apple Computer/MobileSync/Backup:/app/iphone_backup:ro"
      - "C:/Users/Bdfihn/Apple/MobileSync/Backup:/app/iphone_backup2:ro"
    environment:
      - QDRANT_HOST=blackbox-qdrant
      - QDRANT_PORT=6333
      - OLLAMA_HOST=host.docker.internal
      - OLLAMA_PORT=11434
      - ACTIVITYWATCH_HOST=host.docker.internal
      - ACTIVITYWATCH_PORT=5600
      - IPHONE_BACKUP_PASSWORD=changeme
      - IPHONE_BACKUP_PATH=/app/iphone_backup
      - IPHONE_BACKUP_PATH2=/app/iphone_backup2
      - TIMEZONE=America/New_York
      - TZ=America/New_York
    extra_hosts:
      - "host.docker.internal:host-gateway"

  query:
    build:
      context: ./query
    container_name: blackbox-query
    restart: unless-stopped
    depends_on:
      - qdrant
    ports:
      - "8080:8080"
    volumes:
      - ./diary:/app/diary
      - ./data:/app/data
    environment:
      - QDRANT_HOST=blackbox-qdrant
      - QDRANT_PORT=6333
      - OLLAMA_HOST=host.docker.internal
      - OLLAMA_PORT=11434
    extra_hosts:
      - "host.docker.internal:host-gateway"

volumes:
  qdrant_data:
```

> **Note:** Set `IPHONE_BACKUP_PASSWORD` to your real backup password before running. The volume mount paths are Windows host paths — Docker Desktop maps them automatically.

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: mount iPhone backup dirs and add iphone env vars to docker-compose"
```

---

### Task 11: Full test run and smoke verification

- [ ] **Step 1: Run the complete test suite**

```
cd etl && python -m pytest test_iphone.py -v
```

Expected output (all 15 pass):
```
test_iphone.py::test_check_backup_returns_none_when_no_env_paths PASSED
test_iphone.py::test_check_backup_returns_none_when_dir_missing PASSED
test_iphone.py::test_check_backup_returns_none_when_no_manifest PASSED
test_iphone.py::test_check_backup_finds_valid_backup PASSED
test_iphone.py::test_check_backup_falls_back_to_second_path PASSED
test_iphone.py::test_parse_knowledge_db_returns_foreground_events PASSED
test_iphone.py::test_parse_knowledge_db_excludes_background_stream PASSED
test_iphone.py::test_parse_knowledge_db_excludes_non_com_bundles PASSED
test_iphone.py::test_parse_knowledge_db_excludes_other_dates PASSED
test_iphone.py::test_parse_knowledge_db_timestamp_is_local_tz PASSED
test_iphone.py::test_parse_health_returns_steps PASSED
test_iphone.py::test_parse_health_returns_heart_rate PASSED
test_iphone.py::test_parse_health_returns_sleep_with_duration PASSED
test_iphone.py::test_parse_health_excludes_other_dates PASSED
test_iphone.py::test_parse_health_timestamp_is_local_tz PASSED

15 passed
```

- [ ] **Step 2: Verify `etl.py` imports cleanly (no import errors)**

```
cd etl && python -c "from etl import run_etl; print('import OK')"
```

Expected: `import OK`

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat: phase 2 — iPhone backup ETL integration complete"
```

---

## Notes for implementer

**iOSbackup file paths may vary by iOS version.**
If `getFileDecryptedCopy` raises a `KeyError` or file-not-found error:
- For `knowledgeC.db`: try domain `"AppDomain-com.apple.coreduet"`, relativePath `"Library/CoreData/CoreDuetDC.db"`
- For `healthdb_secure.sqlite`: try domain `"AppDomain-com.apple.Health"`, relativePath `"Library/Application Support/healthdb.sqlite"` or `"Library/Application Support/com.apple.health/healthdb_secure.sqlite"`
- Run `iOSbackup(udid=..., cleartextpassword=..., backuproot=...).getManifestDB()` to inspect the actual file list in the backup

**healthdb data_type IDs** (observed iOS 16-17; may vary):
- Steps: `7` — if no results, query `SELECT DISTINCT data_type FROM samples` to find the actual ID
- Heart rate: `5`
- Sleep: `63`

**Docker volume mounts on Windows:** Both backup paths are mounted but only the one that actually exists on disk will work. If neither path exists, Docker will create empty directories and `check_backup()` will return `None` cleanly.
