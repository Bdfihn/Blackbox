"""
etl.py — Nightly ETL for Blackbox.
Pulls all data sources, embeds chunks via nomic-embed-text,
upserts to Qdrant, writes a diary .md file.
"""

import os
import sqlite3
import hashlib
import logging
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import ollama
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

from sources import ActivityWatchSource, IPhoneHealthSource, check_backup, day_bounds, Chunk

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
LOCAL_TZ       = zoneinfo.ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))
AW_BASE        = f"http://{os.getenv('ACTIVITYWATCH_HOST', 'host.docker.internal')}:{os.getenv('ACTIVITYWATCH_PORT', 5600)}/api/0"
QDRANT_HOST    = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT    = int(os.getenv("QDRANT_PORT", 6333))
OLLAMA_HOST    = os.getenv("OLLAMA_HOST", "localhost")
OLLAMA_PORT    = int(os.getenv("OLLAMA_PORT", 11434))
DIARY_DIR      = Path(os.getenv("DIARY_DIR", "/app/diary"))
DB_PATH        = Path(os.getenv("DB_PATH", "/app/data/blackbox.db"))
COLLECTION     = "blackbox"
EMBED_MODEL    = "nomic-embed-text"
SUMMARY_MODEL  = "gemma4:e4b"

DIARY_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Clients ───────────────────────────────────────────────────────────────────
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
ollama_client = ollama.Client(host=f"http://{OLLAMA_HOST}:{OLLAMA_PORT}")


def ensure_collection():
    """Create Qdrant collection if it doesn't exist."""
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )
        log.info(f"Created Qdrant collection '{COLLECTION}'")


def ensure_db():
    """Create SQLite tracking table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingested_chunks (
            chunk_id TEXT PRIMARY KEY,
            ingested_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def already_ingested(chunk_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM ingested_chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
    conn.close()
    return row is not None


def mark_ingested(chunk_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO ingested_chunks VALUES (?, ?)",
        (chunk_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def embed(text: str) -> list[float]:
    """Embed a string using nomic-embed-text via Ollama."""
    response = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return response["embedding"]


def upsert_chunks(chunks: list[Chunk], date_str: str):
    """Embed and upsert chunks into Qdrant, skipping already-ingested ones."""
    points = []
    for chunk in chunks:
        chunk_id = hashlib.md5(chunk.text.encode()).hexdigest()
        if already_ingested(chunk_id):
            continue

        vector = embed(chunk.text)
        point = PointStruct(
            id=chunk_id,
            vector=vector,
            payload={
                "text":         chunk.text,
                "window_start": chunk.window_start,
                "apps":         chunk.apps,
                "total_secs":   chunk.total_secs,
                "source":       chunk.source,
                "date":         date_str,
            }
        )
        points.append((chunk_id, point))

    if not points:
        log.info("No new chunks to upsert.")
        return

    qdrant.upsert(
        collection_name=COLLECTION,
        points=[p for _, p in points]
    )
    for chunk_id, _ in points:
        mark_ingested(chunk_id)

    log.info(f"Upserted {len(points)} new chunks into Qdrant.")


# ── Diary generation ──────────────────────────────────────────────────────────
def generate_diary_entry(date: str, chunks: list[Chunk]) -> str:
    """Use Gemma to write a human-readable diary entry from the day's chunks."""
    if not chunks:
        return f"# {date}\n\nNo activity recorded.\n"

    timeline = "\n".join(c.text for c in chunks)

    prompt = f"""Write a concise diary entry for {date} based on the logs below.

- Use plain, first-person language.
- Describe the day chronologically, grouping related tasks into a clear progression.
- Describe what I did, not exact data points. Summarize activities and tasks, not verbatim logs. Make reasonable inferences based on provided context for what I was doing.
- ALWAYS explicitly note important activites like when I wake up and when and I sleep. You should infer these based on activity. Use phrases like "I woke up at..." and "I went to bed at..."
- Avoid flowery adjectives, no guessing how I felt.
- Write only the diary entry, no preamble.

Example Output:
I woke up around 9:00 AM and started the day with light movement, recording about 150 steps through the late morning. My physical activity remained low and sporadic throughout the afternoon until 4:00 PM, when I logged a more consistent walk of 406 steps. I recorded my highest period of movement between 6:00 PM and 7:00 PM, totaling 1,863 steps.

I began using my PC at 10:50 PM, starting with a session in a browser and an AI assistant. At 11:05 PM, I moved into the terminal to manage various containers, specifically pulling new data models and running several system updates.

Through the rest of the hour, I performed several technical tasks: I executed a clean build for a data processing service, ran scripts to sync recent metrics, and renamed a local project directory. I spent the final hour of the night in a code editor and a browser, refining some scripts and reviewing the updated system interface. I finished working and prepared for sleep shortly after 12:00 AM.

Timeline:
{timeline}"""

    response = ollama_client.chat(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}]
    )
    return f"# {date}\n\n{response['message']['content']}\n"


def write_diary(date: str, content: str):
    path = DIARY_DIR / f"{date}.md"
    path.write_text(content, encoding="utf-8")
    log.info(f"Diary written: {path}")


# ── Main ETL run ──────────────────────────────────────────────────────────────
def run_etl(target_date: datetime | None = None):
    """
    Run the full ETL for a given date (defaults to yesterday,
    since the nightly job runs just after the 04:00 day boundary).
    """
    if target_date is None:
        target_date = datetime.now(LOCAL_TZ) - timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")
    log.info(f"Starting ETL for {date_str}")

    ensure_collection()
    ensure_db()

    start, end = day_bounds(target_date)

    sources = [ActivityWatchSource(AW_BASE, LOCAL_TZ)]

    try:
        from iOSbackup import iOSbackup as _IOSBackup
        backup_info = check_backup()
        if backup_info:
            backuproot, udid = backup_info
            password = os.getenv("IPHONE_BACKUP_PASSWORD", "")
            backup = _IOSBackup(udid=udid, cleartextpassword=password, backuproot=backuproot)
            log.info(f"iPhone backup found: {udid} at {backuproot}")
            sources.append(IPhoneHealthSource(backup, LOCAL_TZ))
        else:
            log.info("No iPhone backup found — skipping iPhone data.")
    except Exception as e:
        log.warning(f"iPhone ingestion failed, continuing without it: {e}")

    all_chunks: list[Chunk] = []
    for source in sources:
        all_chunks.extend(source.get_chunks(start, end))

    all_chunks.sort(key=lambda c: c.window_start)

    upsert_chunks(all_chunks, date_str)

    log.info("Generating diary entry...")
    diary_content = generate_diary_entry(date_str, all_chunks)
    write_diary(date_str, diary_content)

    log.info(f"ETL complete for {date_str}. {len(all_chunks)} chunks processed.")


if __name__ == "__main__":
    run_etl()
