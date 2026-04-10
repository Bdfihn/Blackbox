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


def parse_knowledge_db(backup, start_local: datetime, end_local: datetime) -> list[dict]:
    """Extract foreground app usage from knowledgeC.db for the given time window.

    Args:
        backup: An open iOSbackup instance.
        start_local: Window start (LOCAL_TZ-aware datetime).
        end_local:   Window end   (LOCAL_TZ-aware datetime).

    Returns:
        List of {timestamp (LOCAL_TZ datetime), app_bundle_id, duration_secs}.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        try:
            result = backup.getFileDecryptedCopy(
                relativePath="Library/CoreDuet/Knowledge/knowledgeC.db",
                targetFolder=tmpdir,
            )
        except Exception:
            # knowledgeC.db is not included in standard iTunes/Finder backups
            return []
        if not result:
            return []
        db_path = result.get("decryptedFilePath") or os.path.join(tmpdir, "knowledgeC.db")
        conn    = sqlite3.connect(db_path)
        try:
            rows = conn.execute("""
                SELECT ZBUNDLEID, ZSTARTDATE, ZENDDATE
                FROM   ZOBJECT
                WHERE  ZSTREAMNAME = '/app/inFocus'
                  AND  ZBUNDLEID   LIKE 'com.%'
                  AND  ZSTARTDATE  IS NOT NULL
            """).fetchall()
        finally:
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


def parse_health(backup, start_local: datetime, end_local: datetime) -> list[dict]:
    """Extract steps, heart rate, and sleep from healthdb_secure.sqlite for the given window.

    Args:
        backup: An open iOSbackup instance.
        start_local: Window start (LOCAL_TZ-aware datetime).
        end_local:   Window end   (LOCAL_TZ-aware datetime).

    Returns:
        List of {timestamp (LOCAL_TZ datetime), type ('steps'|'heart_rate'|'sleep'),
                 value (float), unit (str)}.
        For 'sleep', value is duration in seconds (end_date − start_date).
    """
    tmpdir = tempfile.mkdtemp()
    try:
        result  = backup.getFileDecryptedCopy(
            relativePath="Health/healthdb_secure.sqlite",
            targetFolder=tmpdir,
        )
        if not result:
            raise FileNotFoundError("getFileDecryptedCopy returned no result for healthdb_secure.sqlite")
        db_path = result.get("decryptedFilePath") or os.path.join(tmpdir, "healthdb_secure.sqlite")
        conn    = sqlite3.connect(db_path)
        records = []
        try:
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
        finally:
            conn.close()

        return records
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
