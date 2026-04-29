import base64
import io
import logging
import os
import shutil
import subprocess
import tempfile
import zoneinfo
from datetime import datetime

import ollama
from geopy.geocoders import Nominatim
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()

from .base import Chunk
from .iphone_backup import apple_ts, open_backup_db, to_apple_secs

log = logging.getLogger(__name__)

VISION_PROMPT = (
    "Describe this image in one sentence. Include what is shown, "
    "where it appears to be taken, and what activity it represents."
)
MAX_PX = 800
FRAME_OFFSETS = (0.1, 0.5, 0.9)

_ASSET_TABLE_CANDIDATES = ("ZGENERICASSET", "ZASSET")


def _asset_table(conn) -> str | None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for candidate in _ASSET_TABLE_CANDIDATES:
        if candidate in tables:
            return candidate
    return None


def _resize(img: Image.Image, max_px: int = MAX_PX) -> Image.Image:
    w, h = img.size
    scale = max_px / max(w, h)
    if scale >= 1:
        return img
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _coord_str(lat: float, lon: float) -> str:
    lat_dir = "N" if lat >= 0 else "S"
    lon_dir = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f}°{lat_dir}, {abs(lon):.4f}°{lon_dir}"


_geocache: dict[tuple[float, float], str | None] = {}
_geolocator = Nominatim(user_agent="blackbox-etl")


def _reverse_geocode(lat: float, lon: float) -> str | None:
    key = (round(lat, 4), round(lon, 4))
    if key in _geocache:
        return _geocache[key]
    try:
        location = _geolocator.reverse(key, language="en", timeout=10)
        if location is None:
            _geocache[key] = None
            return None
        addr = location.raw.get("address", {})
        specific = (
            addr.get("amenity")
            or addr.get("building")
            or addr.get("neighbourhood")
            or addr.get("suburb")
            or addr.get("quarter")
            or addr.get("road")
        )
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
        )
        name = ", ".join(filter(None, [specific, city])) or None
    except Exception as e:
        log.warning(f"Reverse geocode failed for {key}: {e}")
        name = None
    _geocache[key] = name
    return name


def _extract_video_frames(video_path: str, duration: float) -> list[Image.Image]:
    """Extract frames at 10%, 50%, 90% of duration via ffmpeg.

    If duration is 0 (null in DB), extracts a single frame at timestamp 0.
    """
    offsets = FRAME_OFFSETS if duration > 0 else (0.0,)
    frames = []
    tmpdir = tempfile.mkdtemp()
    try:
        for offset in offsets:
            ts = duration * offset
            out_path = os.path.join(tmpdir, f"frame_{offset}.png")
            result = subprocess.run(
                ["ffmpeg", "-ss", str(ts), "-i", video_path, "-vframes", "1", "-y", out_path],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and os.path.exists(out_path):
                frames.append(Image.open(out_path).copy())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return frames


def parse_photos(
    backup,
    start_local: datetime,
    end_local: datetime,
    local_tz: zoneinfo.ZoneInfo,
) -> list[dict]:
    apple_start = to_apple_secs(start_local)
    apple_end = to_apple_secs(end_local)

    with open_backup_db(backup, "Media/PhotoData/Photos.sqlite") as conn:
        if conn is None:
            log.warning("Photos.sqlite not found in backup")
            return []

        table = _asset_table(conn)
        if not table:
            log.warning("No known asset table (ZGENERICASSET/ZASSET) in Photos.sqlite")
            return []

        rows = conn.execute(f"""
            SELECT
                ZDATECREATED,
                ZLATITUDE,
                ZLONGITUDE,
                ZFILENAME,
                ZDIRECTORY,
                ZKIND,
                ZDURATION,
                ZWIDTH,
                ZHEIGHT
            FROM {table}
            WHERE ZDATECREATED IS NOT NULL
              AND ZDATECREATED >= ? AND ZDATECREATED < ?
            ORDER BY ZDATECREATED
        """, (apple_start, apple_end)).fetchall()

        records = []
        for created, lat, lon, filename, directory, kind, duration, width, height in rows:
            records.append({
                "timestamp": apple_ts(created).astimezone(local_tz),
                "lat": lat if (lat is not None and lat != -180.0) else None,
                "lon": lon if (lon is not None and lon != -180.0) else None,
                "filename": filename,
                "directory": directory or "",
                "kind": "video" if kind == 1 else "photo",
                "duration": duration or 0.0,
                "width": width,
                "height": height,
            })
        return records


class IPhonePhotosSource:
    def __init__(self, backup, local_tz: zoneinfo.ZoneInfo, ollama_client: ollama.Client, llm_model: str):
        self._backup = backup
        self._local_tz = local_tz
        self._ollama = ollama_client
        self._llm_model = llm_model

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        records = parse_photos(self._backup, start, end, self._local_tz)
        log.info(f"  Photos.sqlite: {len(records)} assets in window")

        chunks = []
        for asset in records:
            kind_label = asset["kind"].capitalize()

            if asset["lat"] is not None and asset["lon"] is not None:
                place_name = _reverse_geocode(asset["lat"], asset["lon"])
                location_text = place_name if place_name else _coord_str(asset["lat"], asset["lon"])
                chunks.append(Chunk(
                    window_start=asset["timestamp"].isoformat(),
                    text=(
                        f"[{asset['timestamp'].strftime('%Y-%m-%d %H:%M')}] "
                        f"{kind_label} taken at "
                        f"{location_text} "
                        f"({asset['filename']})."
                    ),
                    apps=[],
                    total_secs=0,
                    source="iphone_gps",
                    metadata={
                        "lat": asset["lat"],
                        "lon": asset["lon"],
                        "place_name": place_name,
                        "filename": asset["filename"],
                        "kind": asset["kind"],
                    },
                ))

            try:
                vision_chunk = self._vision_chunk(asset, kind_label)
                if vision_chunk:
                    chunks.append(vision_chunk)
            except Exception as e:
                log.warning(f"Vision processing failed for {asset['filename']}: {e}")

        return chunks

    def _extract_file(self, asset: dict, tmpdir: str) -> str | None:
        directory = asset["directory"]
        filename = asset["filename"]

        for relative_path in (
            f"Media/{directory}/{filename}",
            f"{directory}/{filename}",
        ):
            result = self._backup.getFileDecryptedCopy(
                relativePath=relative_path,
                targetFolder=tmpdir,
            )
            if result:
                log.debug(f"Extracted {filename} via path: {relative_path}")
                return result.get("decryptedFilePath") or os.path.join(tmpdir, filename)

        log.warning(
            f"Could not extract {filename} from backup "
            f"(tried Media/{directory}/{filename} and {directory}/{filename})"
        )
        return None

    def _vision_chunk(self, asset: dict, kind_label: str) -> Chunk | None:
        tmpdir = tempfile.mkdtemp()
        try:
            file_path = self._extract_file(asset, tmpdir)
            if not file_path:
                return None

            if asset["kind"] == "video":
                images = _extract_video_frames(file_path, asset["duration"])
                if not images:
                    log.warning(f"No frames extracted from {asset['filename']}")
                    return None
                b64_images = [_to_b64(_resize(img)) for img in images]
            else:
                b64_images = [_to_b64(_resize(Image.open(file_path)))]

            response = self._ollama.generate(
                model=self._llm_model,
                prompt=VISION_PROMPT,
                images=b64_images,
            )
            description = response["response"].strip()

            ts = asset["timestamp"]
            return Chunk(
                window_start=ts.isoformat(),
                text=f"[{ts.strftime('%Y-%m-%d %H:%M')}] {kind_label}: {description}",
                apps=[],
                total_secs=0,
                source="iphone_photos",
                metadata={
                    "filename": asset["filename"],
                    "lat": asset["lat"],
                    "lon": asset["lon"],
                    "kind": asset["kind"],
                    "width": asset["width"],
                    "height": asset["height"],
                },
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
