"""
etl.py — Nightly ETL for Phase 1 (ActivityWatch)
Pulls today's ActivityWatch events, chunks them into 5-min windows,
embeds via nomic-embed-text, upserts to Qdrant, writes a diary .md file.
"""

import os
import json
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
CHUNK_MINUTES  = 5

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


# ── ActivityWatch ─────────────────────────────────────────────────────────────
def fetch_aw_buckets() -> list[str]:
    """Return bucket IDs that track windows (aw-watcher-window)."""
    r = requests.get(f"{AW_BASE}/buckets/", timeout=10, allow_redirects=True, headers={"Host": "localhost:5600"})
    r.raise_for_status()
    buckets = r.json()
    return [b for b in buckets if "window" in b.lower()]


def fetch_aw_events(bucket_id: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch raw events from a bucket for a time range."""
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 10000,
    }
    r = requests.get(f"{AW_BASE}/buckets/{bucket_id}/events", params=params, timeout=30, headers={"Host": "localhost:5600"})
    r.raise_for_status()
    return r.json()


def chunk_events(events: list[dict], chunk_minutes: int = CHUNK_MINUTES) -> list[dict]:
    """
    Group events into fixed time buckets of `chunk_minutes`.
    Each chunk becomes one document in Qdrant.
    Returns list of dicts: {window_start, apps: [{app, title, duration_secs}], total_secs}
    """
    if not events:
        return []

    buckets: dict[datetime, list] = {}

    for event in events:
        ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        ts = ts.astimezone(LOCAL_TZ)
        duration = event.get("duration", 0)
        data = event.get("data", {})
        app = data.get("app", "unknown")
        title = data.get("title", "")

        # Round down to nearest chunk_minutes
        floored = ts.replace(
            minute=(ts.minute // chunk_minutes) * chunk_minutes,
            second=0,
            microsecond=0
        )
        if floored not in buckets:
            buckets[floored] = []
        buckets[floored].append({"app": app, "title": title, "duration_secs": duration})

    chunks = []
    for window_start, items in sorted(buckets.items()):
        total = sum(i["duration_secs"] for i in items)
        # Summarise: top apps by time
        app_totals: dict[str, float] = {}
        for i in items:
            app_totals[i["app"]] = app_totals.get(i["app"], 0) + i["duration_secs"]
        top_apps = sorted(app_totals.items(), key=lambda x: x[1], reverse=True)[:5]

        # Build a natural-language description of this chunk
        descriptions = []
        for item in items:
            if item["duration_secs"] > 10:
                mins = round(item["duration_secs"] / 60, 1)
                descriptions.append(f"{item['app']}: '{item['title']}' ({mins}m)")

        text = (
            f"[{window_start.strftime('%Y-%m-%d %H:%M')}] "
            f"PC activity for {chunk_minutes} minutes. "
            f"Top apps: {', '.join(f'{a}({round(s/60,1)}m)' for a,s in top_apps)}. "
            f"Details: {'; '.join(descriptions[:10])}"
        )

        chunks.append({
            "window_start": window_start.isoformat(),
            "text": text,
            "apps": [a for a, _ in top_apps],
            "total_secs": total,
            "source": "activitywatch",
        })

    return chunks


def embed(text: str) -> list[float]:
    """Embed a string using nomic-embed-text via Ollama."""
    response = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return response["embedding"]


def upsert_chunks(chunks: list[dict]):
    """Embed and upsert chunks into Qdrant, skipping already-ingested ones."""
    points = []
    for chunk in chunks:
        chunk_id = hashlib.md5(chunk["text"].encode()).hexdigest()
        if already_ingested(chunk_id):
            continue

        vector = embed(chunk["text"])
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
def generate_diary_entry(date: str, chunks: list[dict]) -> str:
    """Use gemma4:27b to write a human-readable diary entry from the day's chunks."""
    if not chunks:
        return f"# {date}\n\nNo activity recorded.\n"

    # Build a compact timeline for the prompt
    timeline = "\n".join(c["text"] for c in chunks)

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
    since the nightly job runs just after midnight).
    """
    if target_date is None:
        target_date = datetime.now(LOCAL_TZ) - timedelta(days=1)

    date_str = target_date.strftime("%Y-%m-%d")
    log.info(f"Starting ETL for {date_str}")

    ensure_collection()
    ensure_db()

    start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)

    all_chunks = []

    # Pull from all window-tracking buckets
    try:
        buckets = fetch_aw_buckets()
        log.info(f"Found ActivityWatch buckets: {buckets}")
    except Exception as e:
        log.error(f"Could not reach ActivityWatch at {AW_BASE}: {e}")
        buckets = []

    for bucket_id in buckets:
        try:
            events = fetch_aw_events(bucket_id, start, end)
            log.info(f"  {bucket_id}: {len(events)} events")
            chunks = chunk_events(events)
            all_chunks.extend(chunks)
        except Exception as e:
            log.error(f"  Error fetching {bucket_id}: {e}")

    # Upsert to Qdrant
    upsert_chunks(all_chunks)

    # Generate and write diary
    log.info("Generating diary entry...")
    diary_content = generate_diary_entry(date_str, all_chunks)
    write_diary(date_str, diary_content)

    log.info(f"ETL complete for {date_str}. {len(all_chunks)} chunks processed.")


if __name__ == "__main__":
    run_etl()
