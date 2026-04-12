"""
etl.py — Nightly ETL for Blackbox.
Pulls all data sources, embeds chunks via nomic-embed-text,
upserts to Qdrant, writes a diary .md file.
"""

import os
import hashlib
import logging
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import ollama
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

from sources import (
    ActivityWatchSource,
    IPhoneHealthSource,
    IPhoneSocialSource,
    IPhonePhotosSource,
    check_backup,
    day_bounds,
    Chunk,
)

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
COLLECTION     = "blackbox"
EMBED_MODEL    = "nomic-embed-text"
SUMMARY_MODEL  = "gemma4:e4b"

DIARY_DIR.mkdir(parents=True, exist_ok=True)

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



def embed(text: str) -> list[float]:
    """Embed a string using nomic-embed-text via Ollama."""
    response = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return response["embedding"]


def upsert_chunks(chunks: list[Chunk], date_str: str):
    """Embed and upsert chunks into Qdrant."""
    if not chunks:
        log.info("No chunks to upsert.")
        return

    points = []
    for chunk in chunks:
        chunk_id = hashlib.md5(chunk.text.encode()).hexdigest()
        vector = embed(chunk.text)
        points.append(PointStruct(
            id=chunk_id,
            vector=vector,
            payload={
                "text":         chunk.text,
                "window_start": chunk.window_start,
                "apps":         chunk.apps,
                "total_secs":   chunk.total_secs,
                "source":       chunk.source,
                "date":         date_str,
                "metadata":     chunk.metadata,
            }
        ))

    qdrant.upsert(collection_name=COLLECTION, points=points)
    log.info(f"Upserted {len(points)} chunks into Qdrant.")


# ── Timeline preprocessing ────────────────────────────────────────────────────
def preprocess_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """
    Reduce noise before diary generation:
    1. Drop chunks under 30 seconds.
    2. Merge consecutive chunks that share the same dominant app into one block.
    """
    filtered = [c for c in chunks if c.total_secs >= 30]

    if not filtered:
        return filtered

    merged: list[Chunk] = []
    current = filtered[0]

    for nxt in filtered[1:]:
        current_dominant = current.apps[0] if current.apps else None
        nxt_dominant = nxt.apps[0] if nxt.apps else None

        if current_dominant and current_dominant == nxt_dominant:
            seen = set(current.apps)
            extra = [a for a in nxt.apps if a not in seen]
            current = Chunk(
                window_start=current.window_start,
                text=current.text + "\n" + nxt.text,
                apps=current.apps + extra,
                total_secs=current.total_secs + nxt.total_secs,
                source=current.source,
                metadata=current.metadata,
            )
        else:
            merged.append(current)
            current = nxt

    merged.append(current)
    return merged


# ── Diary generation ──────────────────────────────────────────────────────────
def generate_diary_entry(date: str, chunks: list[Chunk]) -> str:
    """Use Gemma to write a human-readable diary entry from the day's chunks."""
    if not chunks:
        return f"# {date}\n\nNo activity recorded.\n"

    timeline = "\n".join(c.text for c in chunks)

    prompt = f"""**Role**: You are a factual data summarizer.

**Constraints**
- Output: Plain text only.
- Prohibited: Markdown formatting (no #, *, or -), emojis, and conversational filler.
- Tone: Clinical, first-person, and objective.
- Chronology: Process the timeline from start to finish.
- Time: Use 12-hour AM/PM format (convert from 24-hour logs).
- You must identify the first and last recorded events to state: "I woke up at [Time]..." and "I went to bed at [Time]..."
- Do not include preamble or postscript.

**Logic**
- Group individual log entries into broader activities
- Summarize the nature of the work rather than quoting the logs.


**TIMELINE**
{timeline}
**TIMELINE END**

**Role**: You are a factual data summarizer.

**Constraints**
- Output: Plain text only.
- Prohibited: Markdown formatting (no #, *, or -), emojis, and conversational filler.
- Tone: Clinical, first-person, and objective.
- Chronology: Process the timeline from start to finish.
- You must identify the first and last recorded events to state: "I woke up at [Time]..." and "I went to bed at [Time]..."
- Do not include preamble or postscript.

**Logic**
- Group individual log entries into broader activities
- Summarize the nature of the work rather than quoting the logs.
"""

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
            sources.append(IPhoneSocialSource(backup, LOCAL_TZ))
            sources.append(IPhonePhotosSource(backup, LOCAL_TZ, ollama_client))
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
    diary_chunks = preprocess_chunks(all_chunks)
    log.info(f"Preprocessed {len(all_chunks)} chunks → {len(diary_chunks)} for diary.")
    diary_content = generate_diary_entry(date_str, diary_chunks)
    write_diary(date_str, diary_content)

    log.info(f"ETL complete for {date_str}. {len(all_chunks)} chunks processed.")


if __name__ == "__main__":
    run_etl()
