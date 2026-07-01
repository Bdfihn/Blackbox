"""
spike_health.py — Enumerate what's actually in healthdb_secure.sqlite.

Run with:
  docker compose --profile etl run --rm etl python spike_health.py

Prints all data_types with counts, sleep category value distribution,
workout table schema + sample rows, and available tables.
"""

import os
import sys
import zoneinfo
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from sources.iphone_backup import check_backup, open_backup_db, apple_ts

LOCAL_TZ = zoneinfo.ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))


def main():
    backup_info = check_backup()
    if not backup_info:
        print("ERROR: No iPhone backup found. Check IPHONE_BACKUP_PATH.")
        sys.exit(1)

    backuproot, udid = backup_info
    password = os.getenv("IPHONE_BACKUP_PASSWORD", "")

    from iOSbackup import iOSbackup as _IOSBackup
    backup = _IOSBackup(udid=udid, cleartextpassword=password, backuproot=backuproot)
    print(f"Opened backup: {udid}\n")

    with open_backup_db(backup, "Health/healthdb_secure.sqlite") as conn:
        if conn is None:
            print("ERROR: healthdb_secure.sqlite not found in backup.")
            sys.exit(1)

        # ── Tables ────────────────────────────────────────────────────────────
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        print("=== TABLES ===")
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:40s} {count:>8} rows")

        # ── All data_types with counts ────────────────────────────────────────
        print("\n=== SAMPLES data_type COUNTS ===")
        rows = conn.execute("""
            SELECT s.data_type, COUNT(*) as cnt,
                   MIN(s.start_date) as earliest, MAX(s.start_date) as latest
            FROM samples s
            GROUP BY s.data_type
            ORDER BY cnt DESC
        """).fetchall()
        for data_type, cnt, earliest, latest in rows:
            e = apple_ts(earliest).astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if earliest else "?"
            l = apple_ts(latest).astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if latest else "?"
            print(f"  data_type={data_type:>4}  count={cnt:>7}  range={e} → {l}")

        # ── Sleep category value breakdown ────────────────────────────────────
        print("\n=== SLEEP (data_type=63) category values ===")
        sleep_rows = conn.execute("""
            SELECT cs.value, COUNT(*) as cnt
            FROM samples s
            JOIN category_samples cs ON cs.ROWID = s.ROWID
            WHERE s.data_type = 63
            GROUP BY cs.value
            ORDER BY cs.value
        """).fetchall()
        labels = {0: "InBed/AsleepLegacy", 1: "Awake", 2: "AsleepUnspecified",
                  3: "AsleepCore (light)", 4: "AsleepDeep", 5: "AsleepREM"}
        for val, cnt in sleep_rows:
            print(f"  value={val}  {labels.get(val, '?'):25s}  count={cnt}")

        # ── Workout table schema + samples ────────────────────────────────────
        for table in ("workout_activities", "workouts"):
            if table not in tables:
                continue
            print(f"\n=== {table} schema ===")
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            for col in cols:
                print(f"  {col[1]:30s}  {col[2]}")
            print(f"\n=== {table} sample rows (last 5) ===")
            sample = conn.execute(f"SELECT * FROM {table} ORDER BY ROWID DESC LIMIT 5").fetchall()
            col_names = [c[1] for c in cols]
            for row in sample:
                for name, val in zip(col_names, row):
                    if name in ("start_date", "end_date") and val:
                        try:
                            val = f"{val}  ({apple_ts(val).astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')})"
                        except Exception:
                            pass
                    print(f"    {name}: {val}")
                print()

        # ── Known Watch-specific types: spot-check recent values ─────────────
        WATCH_TYPES = {
            39:  "RestingHeartRate",
            96:  "HRV (SDNN ms)",
            109: "BloodOxygen %",
            110: "RespiratoryRate breaths/min",
            12:  "ActiveEnergyBurned kcal",
            8:   "DistanceWalkingRunning m",
        }
        print("\n=== SPOT-CHECK Watch-specific quantity types ===")
        for dt_id, label in WATCH_TYPES.items():
            row = conn.execute("""
                SELECT COUNT(*), AVG(qs.quantity), MIN(qs.quantity), MAX(qs.quantity)
                FROM samples s
                JOIN quantity_samples qs ON qs.ROWID = s.ROWID
                WHERE s.data_type = ?
            """, (dt_id,)).fetchone()
            cnt, avg, mn, mx = row
            if cnt:
                print(f"  data_type={dt_id:>4}  {label:35s}  count={cnt}  avg={avg:.1f}  min={mn:.1f}  max={mx:.1f}")
            else:
                print(f"  data_type={dt_id:>4}  {label:35s}  NOT FOUND")


if __name__ == "__main__":
    main()
