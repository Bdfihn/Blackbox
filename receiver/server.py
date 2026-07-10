"""
server.py — Receives the nightly iPhone health export.
POST /health stores the JSON body as <date>.json in the shared
health_export folder, where the ETL's iphone_export source reads it.
"""

import json
import logging
import os
import re
from pathlib import Path

from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

EXPORT_DIR = Path(os.getenv("HEALTH_EXPORT_DIR", "/app/health_export"))
TOKEN = os.getenv("HEALTH_EXPORT_TOKEN", "")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


@app.route("/health", methods=["POST"])
def receive():
    if TOKEN and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    date = data.get("date", "")
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EXPORT_DIR / f"{date}.json.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(EXPORT_DIR / f"{date}.json")
    log.info(f"Stored health export for {date}")
    return jsonify({"stored": date})


if __name__ == "__main__":
    if not TOKEN:
        log.warning("HEALTH_EXPORT_TOKEN not set — accepting unauthenticated posts")
    app.run(host="0.0.0.0", port=8081, debug=False)
