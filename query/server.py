"""
server.py — Lightweight Flask server for the query UI.
Serves the web interface at port 8080 and exposes /api/query.
"""

import os
import re
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from rag import answer, QDRANT_HOST, QDRANT_PORT, COLLECTION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app)

DIARY_DIR = Path(os.getenv("DIARY_DIR", "/app/diary"))
qdrant    = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


def _scroll_all(date_filter: Filter, *, with_payload: bool) -> list:
    points = []
    offset = None
    while True:
        results, offset = qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=date_filter,
            limit=1000,
            offset=offset,
            with_payload=with_payload,
            with_vectors=False,
        )
        points.extend(results)
        if offset is None:
            break
    return points


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/query", methods=["POST"])
def query():
    data = request.json
    question = data.get("question", "").strip()
    date_filter = data.get("date")  # optional YYYY-MM-DD

    if not question:
        return jsonify({"error": "question is required"}), 400

    log.info(f"Query: {question!r} (date_filter={date_filter})")
    result = answer(question, date_filter=date_filter)
    return jsonify(result)


@app.route("/api/diary", methods=["GET"])
def list_diary():
    """Return list of available diary entries."""
    entries = sorted(
        [f.stem for f in DIARY_DIR.glob("*.md")],
        reverse=True
    )
    return jsonify({"entries": entries})


@app.route("/api/diary/<date>", methods=["GET"])
def get_diary(date: str):
    """Return the content of a specific diary entry."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify({"error": "Invalid date format"}), 400
    path = DIARY_DIR / f"{date}.md"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify({"date": date, "content": path.read_text(encoding="utf-8")})


@app.route("/api/diary/<date>/timeline", methods=["GET"])
def get_timeline(date: str):
    """Return all activity chunks for a date, sorted by time."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify({"error": "Invalid date format"}), 400

    qdrant_filter = Filter(must=[FieldCondition(key="date", match=MatchValue(value=date))])
    points = _scroll_all(qdrant_filter, with_payload=True)
    chunks = [
        {
            "time": p.payload.get("window_start", "")[:16].replace("T", " "),
            "apps": p.payload.get("apps", []),
            "text": p.payload.get("text", ""),
        }
        for p in points
    ]
    chunks.sort(key=lambda c: c["time"])
    return jsonify({"date": date, "chunks": chunks})


@app.route("/api/diary/<date>", methods=["DELETE"])
def delete_diary(date: str):
    """Delete a diary entry and all associated vector + tracking data."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify({"error": "Invalid date format"}), 400

    # 1. Collect point IDs from Qdrant before deleting
    qdrant_filter = Filter(must=[FieldCondition(key="date", match=MatchValue(value=date))])
    chunk_ids = [str(p.id) for p in _scroll_all(qdrant_filter, with_payload=False)]

    # 2. Delete from Qdrant
    if chunk_ids:
        qdrant.delete(collection_name=COLLECTION, points_selector=chunk_ids)
        log.info(f"Deleted {len(chunk_ids)} Qdrant points for {date}")

    # 3. Delete diary file
    path = DIARY_DIR / f"{date}.md"
    if path.exists():
        path.unlink()
        log.info(f"Deleted diary file: {path}")
    elif not chunk_ids:
        return jsonify({"error": "Not found"}), 404

    return jsonify({"deleted": date, "chunks_removed": len(chunk_ids)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
