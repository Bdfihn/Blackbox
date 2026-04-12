import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple_ts(apple_secs: float) -> datetime:
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


@contextmanager
def open_backup_db(backup, relative_path: str):
    """Decrypt and open a SQLite database from an iPhone backup.

    Yields a sqlite3.Connection, or None if the file is not present in the backup.
    Cleans up the temp directory on exit.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        result = backup.getFileDecryptedCopy(relativePath=relative_path, targetFolder=tmpdir)
        if not result:
            yield None
            return
        db_path = result.get("decryptedFilePath") or os.path.join(tmpdir, os.path.basename(relative_path))
        conn = sqlite3.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
