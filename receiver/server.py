"""
server.py — Receives the nightly iPhone health export.
POST /health stores the JSON body as <date>.json in the shared
health_export folder, where the ETL's iphone_export source reads it.
"""

import logging
import os
import re
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

EXPORT_DIR = Path(os.getenv("HEALTH_EXPORT_DIR", "/app/health_export"))
TOKEN = os.getenv("HEALTH_EXPORT_TOKEN", "")
LOCAL_TZ = zoneinfo.ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAY_START_HOUR = 4  # logical days run 04:00 → 04:00, matching the ETL


def _logical_date(now: datetime) -> str:
    """The logical day a post at `now` belongs to: before 04:00 is still 'yesterday'."""
    if now.hour < _DAY_START_HOUR:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


def _find_date() -> str:
    """Date for the stored filename, in order of preference:

    1. A YYYY-MM-DD value in any request header (names are the phone's
       choice — scan values instead of guessing the name).
    2. A YYYY-MM-DD substring in the body's "date"/"Date" field. Shortcuts
       serializes Current Date in locale format, so this often fails.
    3. The server's own logical date — the automation fires between the
       evening and the 04:00 boundary of the day it covers.
    """
    for _, value in request.headers.items():
        if _DATE_RE.fullmatch(value.strip()):
            return value.strip()
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() == "date" and isinstance(value, str):
                match = _DATE_RE.search(value)
                if match:
                    return match.group()
    return _logical_date(datetime.now(LOCAL_TZ))


def _body_bytes() -> bytes:
    """The uploaded file if the post is multipart, else the raw body."""
    if request.files:
        return next(iter(request.files.values())).read()
    return request.get_data()


@app.route("/health", methods=["POST"])
def receive():
    if TOKEN and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return jsonify({"error": "unauthorized"}), 401

    date = _find_date()
    body = _body_bytes()
    if not body:
        return jsonify({"error": "empty body"}), 400

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EXPORT_DIR / f"{date}.json.tmp"
    tmp.write_bytes(body)
    tmp.replace(EXPORT_DIR / f"{date}.json")
    log.info(f"Stored health export for {date} ({len(body)} bytes, "
             f"content-type={request.content_type})")
    return jsonify({"stored": date})


if __name__ == "__main__":
    if not TOKEN:
        log.warning("HEALTH_EXPORT_TOKEN not set — accepting unauthenticated posts")
    app.run(host="0.0.0.0", port=8081, debug=False)
