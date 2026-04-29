import logging
import sqlite3
import zoneinfo
from collections import Counter
from datetime import datetime

from .base import Chunk, floor_dt
from .iphone_backup import apple_ts, open_backup_db, to_apple_secs

log = logging.getLogger(__name__)

BUCKET_MINUTES = 15

_BUNDLE_NAMES = {
    "com.apple.MobileSMS": "Messages",
    "com.apple.mobilephone": "Phone",
    "com.apple.mobilemail": "Mail",
    "com.apple.facetime": "FaceTime",
}


def _readable_app(bundle_id: str) -> str:
    return _BUNDLE_NAMES.get(bundle_id, bundle_id)



def _junction_cols(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Discover FK column names in Z_2INTERACTIONRECIPIENT via PRAGMA.

    Core Data junction table column names vary across iOS versions
    (e.g. Z_2INTERACTIONS vs Z_3INTERACTIONS). Discovered at runtime.
    Returns (interactions_fk_col, recipients_fk_col).
    """
    rows = conn.execute("PRAGMA table_info(Z_2INTERACTIONRECIPIENT)").fetchall()
    interactions_col = None
    recipients_col = None
    for row in rows:
        col = row[1].upper()
        if "INTERACTIONS" in col:
            interactions_col = row[1]
        elif "RECIPIENTS" in col:
            recipients_col = row[1]
    return interactions_col, recipients_col


def parse_interactions(
    backup,
    start_local: datetime,
    end_local: datetime,
    local_tz: zoneinfo.ZoneInfo,
) -> list[dict]:
    apple_start = to_apple_secs(start_local)
    apple_end = to_apple_secs(end_local)

    with open_backup_db(backup, "Library/CoreDuet/People/interactionC.db") as conn:
        if conn is None:
            log.warning("interactionC.db not found in backup")
            return []

        interactions_col, recipients_col = _junction_cols(conn)

        if interactions_col and recipients_col:
            rows = conn.execute(f"""
                SELECT
                    i.ZSTARTDATE,
                    i.ZBUNDLEID,
                    i.ZDIRECTION,
                    c1.ZDISPLAYNAME AS sender_name,
                    c2.ZDISPLAYNAME AS recipient_name
                FROM ZINTERACTIONS i
                LEFT JOIN ZCONTACTS c1 ON c1.Z_PK = i.ZSENDER
                LEFT JOIN Z_2INTERACTIONRECIPIENT jr
                    ON jr.{interactions_col} = i.Z_PK
                LEFT JOIN ZCONTACTS c2 ON c2.Z_PK = jr.{recipients_col}
                WHERE i.ZSTARTDATE >= ? AND i.ZSTARTDATE < ?
            """, (apple_start, apple_end)).fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    i.ZSTARTDATE,
                    i.ZBUNDLEID,
                    i.ZDIRECTION,
                    c1.ZDISPLAYNAME AS sender_name,
                    NULL AS recipient_name
                FROM ZINTERACTIONS i
                LEFT JOIN ZCONTACTS c1 ON c1.Z_PK = i.ZSENDER
                WHERE i.ZSTARTDATE >= ? AND i.ZSTARTDATE < ?
            """, (apple_start, apple_end)).fetchall()

        return [
            {
                "timestamp": apple_ts(start_date).astimezone(local_tz),
                "bundle_id": bundle_id or "",
                "direction": direction,
                "sender_name": sender_name,
                "recipient_name": recipient_name,
            }
            for start_date, bundle_id, direction, sender_name, recipient_name in rows
        ]


class IPhoneSocialSource:
    def __init__(self, backup, local_tz: zoneinfo.ZoneInfo):
        self._backup = backup
        self._local_tz = local_tz

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        records = parse_interactions(self._backup, start, end, self._local_tz)
        log.info(f"  interactionC: {len(records)} records")
        return self._chunk_interactions(records)

    def _chunk_interactions(self, records: list[dict]) -> list[Chunk]:
        if not records:
            return []

        buckets: dict[datetime, list[dict]] = {}
        for r in records:
            key = floor_dt(r["timestamp"], BUCKET_MINUTES)
            buckets.setdefault(key, []).append(r)

        chunks = []
        for bucket_time in sorted(buckets):
            items = buckets[bucket_time]

            app_counts: Counter[str] = Counter()
            names: set[str] = set()
            bundle_ids: set[str] = set()

            for item in items:
                app = _readable_app(item["bundle_id"])
                app_counts[app] += 1
                bundle_ids.add(item["bundle_id"])
                for name in (item["sender_name"], item["recipient_name"]):
                    if name and name.strip():
                        names.add(name.strip())

            app_summary = ", ".join(
                f"{count} {app}"
                for app, count in app_counts.most_common()
            )
            contact_part = f" Contacts: {', '.join(sorted(names))}." if names else ""
            text = (
                f"[{bucket_time.strftime('%Y-%m-%d %H:%M')}] "
                f"Social activity: {app_summary}.{contact_part}"
            )

            chunks.append(Chunk(
                window_start=bucket_time.isoformat(),
                text=text,
                apps=sorted(app_counts.keys()),
                total_secs=900,
                source="iphone_social",
                metadata={
                    "event_count": len(items),
                    "contacts": sorted(names),
                    "bundle_ids": sorted(bundle_ids),
                },
            ))

        return chunks
