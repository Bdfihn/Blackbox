import io
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import zoneinfo

from PIL import Image

from sources.iphone_photos import IPhonePhotosSource, parse_photos, _resize, _to_b64, _reverse_geocode, _geocache
from sources.face_index import FaceIndex
from sources.iphone_backup import APPLE_EPOCH

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

_TS_UTC = datetime(2024, 1, 15, 14, 23, 0, tzinfo=timezone.utc)
_TS_APPLE = (_TS_UTC - APPLE_EPOCH).total_seconds()


def _make_photos_db(lat=40.7128, lon=-74.006, kind=0, duration=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ZGENERICASSET (
            Z_PK INTEGER PRIMARY KEY,
            ZDATECREATED REAL,
            ZLATITUDE REAL,
            ZLONGITUDE REAL,
            ZFILENAME TEXT,
            ZDIRECTORY TEXT,
            ZKIND INTEGER,
            ZDURATION REAL,
            ZWIDTH INTEGER,
            ZHEIGHT INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO ZGENERICASSET VALUES (1, ?, ?, ?, 'IMG_1234.HEIC', 'DCIM/100APPLE', ?, ?, 4032, 3024)",
        (_TS_APPLE, lat, lon, kind, duration),
    )
    conn.commit()
    return conn


@contextmanager
def _mock_db(conn):
    yield conn


def _make_image(width=2000, height=1500) -> Image.Image:
    return Image.new("RGB", (width, height), color=(128, 64, 32))


# ── _resize ──────────────────────────────────────────────────────────────────

def test_resize_scales_down_landscape():
    img = _make_image(2000, 1500)
    result = _resize(img, max_px=800)
    assert max(result.size) == 800
    assert result.size == (800, 600)


def test_resize_scales_down_portrait():
    img = _make_image(1500, 2000)
    result = _resize(img, max_px=800)
    assert max(result.size) == 800
    assert result.size == (600, 800)


def test_resize_does_not_upscale():
    img = _make_image(400, 300)
    result = _resize(img, max_px=800)
    assert result.size == (400, 300)


# ── _to_b64 ──────────────────────────────────────────────────────────────────

def test_to_b64_produces_valid_png():
    import base64
    img = _make_image(10, 10)
    b64 = _to_b64(img)
    data = base64.b64decode(b64)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes


# ── _reverse_geocode ─────────────────────────────────────────────────────────

def _mock_location(address: dict):
    loc = MagicMock()
    loc.raw = {"address": address}
    return loc


def _call_reverse_geocode(address: dict) -> str | None:
    key = (42.3601, -71.0589)
    _geocache.pop(key, None)
    with patch("sources.iphone_photos._geolocator") as mock_geo:
        mock_geo.reverse.return_value = _mock_location(address)
        result = _reverse_geocode(*key)
    _geocache.pop(key, None)
    return result


def test_reverse_geocode_prefers_amenity():
    result = _call_reverse_geocode({
        "amenity": "Boston Athenaeum",
        "neighbourhood": "Beacon Hill",
        "city": "Boston",
    })
    assert result == "Boston Athenaeum, Boston"


def test_reverse_geocode_falls_back_to_neighbourhood():
    result = _call_reverse_geocode({
        "neighbourhood": "Beacon Hill",
        "city": "Boston",
    })
    assert result == "Beacon Hill, Boston"


def test_reverse_geocode_falls_back_to_road():
    result = _call_reverse_geocode({
        "road": "Tremont Street",
        "city": "Boston",
    })
    assert result == "Tremont Street, Boston"


def test_reverse_geocode_omits_city_when_absent():
    result = _call_reverse_geocode({
        "neighbourhood": "Beacon Hill",
    })
    assert result == "Beacon Hill"


def test_reverse_geocode_returns_none_when_no_specific_or_city():
    result = _call_reverse_geocode({"country": "United States"})
    assert result is None


def test_reverse_geocode_caches_result():
    key = (42.3601, -71.0589)
    _geocache.pop(key, None)
    with patch("sources.iphone_photos._geolocator") as mock_geo:
        mock_geo.reverse.return_value = _mock_location({"neighbourhood": "South End", "city": "Boston"})
        _reverse_geocode(*key)
        _reverse_geocode(*key)
    assert mock_geo.reverse.call_count == 1
    _geocache.pop(key, None)


# ── parse_photos ─────────────────────────────────────────────────────────────

def test_parse_photos_returns_asset_in_window():
    conn = _make_photos_db()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_photos(None, start, end, LOCAL_TZ)

    assert len(records) == 1
    assert records[0]["filename"] == "IMG_1234.HEIC"
    assert records[0]["kind"] == "photo"
    assert records[0]["lat"] == 40.7128
    assert records[0]["lon"] == -74.006


def test_parse_photos_excludes_out_of_window():
    conn = _make_photos_db()
    start = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 17, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_photos(None, start, end, LOCAL_TZ)

    assert records == []


def test_parse_photos_no_gps_sets_none():
    conn = _make_photos_db(lat=-180.0, lon=-180.0)
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_photos(None, start, end, LOCAL_TZ)

    assert records[0]["lat"] is None
    assert records[0]["lon"] is None


def test_parse_photos_null_duration_defaults_to_zero():
    conn = _make_photos_db(kind=1, duration=None)  # video with null duration
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    with patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        records = parse_photos(None, start, end, LOCAL_TZ)

    assert records[0]["duration"] == 0.0
    assert records[0]["kind"] == "video"


# ── IPhonePhotosSource.get_chunks ────────────────────────────────────────────

def test_get_chunks_emits_gps_chunk_with_place_name():
    conn = _make_photos_db()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    mock_ollama = MagicMock()
    mock_backup = MagicMock()
    mock_backup.getFileDecryptedCopy.return_value = None  # extraction fails → no vision chunk

    source = IPhonePhotosSource(mock_backup, LOCAL_TZ, mock_ollama, "test-model")

    with (
        patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)),
        patch("sources.iphone_photos._reverse_geocode", return_value="Downtown Crossing, Boston"),
    ):
        chunks = source.get_chunks(start, end)

    gps_chunks = [c for c in chunks if c.source == "iphone_gps"]
    assert len(gps_chunks) == 1
    assert "Downtown Crossing, Boston" in gps_chunks[0].text
    assert gps_chunks[0].metadata["kind"] == "photo"
    assert gps_chunks[0].metadata["place_name"] == "Downtown Crossing, Boston"


def test_get_chunks_gps_chunk_falls_back_to_coords_when_geocode_fails():
    conn = _make_photos_db()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    mock_ollama = MagicMock()
    mock_backup = MagicMock()
    mock_backup.getFileDecryptedCopy.return_value = None

    source = IPhonePhotosSource(mock_backup, LOCAL_TZ, mock_ollama, "test-model")

    with (
        patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)),
        patch("sources.iphone_photos._reverse_geocode", return_value=None),
    ):
        chunks = source.get_chunks(start, end)

    gps_chunks = [c for c in chunks if c.source == "iphone_gps"]
    assert len(gps_chunks) == 1
    assert "40.7128" in gps_chunks[0].text
    assert "74.0060" in gps_chunks[0].text
    assert gps_chunks[0].metadata["place_name"] is None


def test_get_chunks_skips_gps_chunk_when_no_gps():
    conn = _make_photos_db(lat=-180.0, lon=-180.0)
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    mock_ollama = MagicMock()
    mock_backup = MagicMock()
    mock_backup.getFileDecryptedCopy.return_value = None

    source = IPhonePhotosSource(mock_backup, LOCAL_TZ, mock_ollama, "test-model")

    with patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)):
        chunks = source.get_chunks(start, end)

    assert not any(c.source == "iphone_gps" for c in chunks)


def test_get_chunks_vision_failure_does_not_abort_gps():
    """If vision extraction fails, the GPS chunk is still emitted."""
    conn = _make_photos_db()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    mock_ollama = MagicMock()
    mock_ollama.generate.side_effect = RuntimeError("ollama unavailable")
    mock_backup = MagicMock()
    # Return a path that doesn't exist so Image.open raises
    mock_backup.getFileDecryptedCopy.return_value = {"decryptedFilePath": "/nonexistent/file.heic"}

    source = IPhonePhotosSource(mock_backup, LOCAL_TZ, mock_ollama, "test-model")

    with (
        patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)),
        patch("sources.iphone_photos._reverse_geocode", return_value=None),
    ):
        chunks = source.get_chunks(start, end)

    gps_chunks = [c for c in chunks if c.source == "iphone_gps"]
    photo_chunks = [c for c in chunks if c.source == "iphone_photos"]
    assert len(gps_chunks) == 1
    assert len(photo_chunks) == 0


# ── FaceIndex ─────────────────────────────────────────────────────────────────

def test_face_index_empty_when_dir_missing():
    fi = FaceIndex("/nonexistent/faces_dir")
    assert fi.empty
    assert fi.identify("/any/image.jpg") == []


def test_face_index_empty_when_faces_dir_is_none():
    fi = FaceIndex(None)
    assert fi.empty


def _unit_vec(dim=512):
    import numpy as np
    v = np.zeros(dim, dtype=np.float32)
    v[0] = 1.0
    return v


def _make_face(embedding):
    face = MagicMock()
    face.embedding = embedding
    return face


def test_face_index_loads_encodings(tmp_path, fake_insightface):
    person_dir = tmp_path / "Alice"
    person_dir.mkdir()
    (person_dir / "1.png").write_bytes(b"fake")

    enc = _unit_vec()
    fake_insightface.get.return_value = [_make_face(enc)]

    fi = FaceIndex(str(tmp_path))

    assert not fi.empty
    assert "Alice" in fi._encodings


def test_face_index_identify_returns_matched_people(tmp_path, fake_insightface):
    person_dir = tmp_path / "Bob"
    person_dir.mkdir()
    (person_dir / "1.png").write_bytes(b"fake")

    enc = _unit_vec()
    fake_insightface.get.return_value = [_make_face(enc)]
    fi = FaceIndex(str(tmp_path))

    fake_insightface.get.return_value = [_make_face(enc)]
    result = fi.identify("/photo.png")

    assert result == ["Bob"]


def test_face_index_identify_no_match(tmp_path, fake_insightface):
    import numpy as np

    person_dir = tmp_path / "Carol"
    person_dir.mkdir()
    (person_dir / "1.png").write_bytes(b"fake")

    ref_enc = _unit_vec()
    fake_insightface.get.return_value = [_make_face(ref_enc)]
    fi = FaceIndex(str(tmp_path))

    # Orthogonal non-unit vector — after normalization cosine similarity = 0.0, below threshold
    other_enc = np.zeros(512, dtype=np.float32)
    other_enc[1] = 5.0
    fake_insightface.get.return_value = [_make_face(other_enc)]
    result = fi.identify("/photo.png")

    assert result == []


def test_face_index_skips_multi_face_reference(tmp_path, fake_insightface):
    person_dir = tmp_path / "Dave"
    person_dir.mkdir()
    (person_dir / "1.png").write_bytes(b"fake")

    enc = _unit_vec()
    fake_insightface.get.return_value = [_make_face(enc), _make_face(enc)]

    fi = FaceIndex(str(tmp_path))

    assert fi.empty


def test_face_index_skips_non_image_files(tmp_path, fake_insightface):
    person_dir = tmp_path / "Eve"
    person_dir.mkdir()
    (person_dir / "video.mov").write_bytes(b"")

    fi = FaceIndex(str(tmp_path))

    fake_insightface.get.assert_not_called()
    assert fi.empty


# ── IPhonePhotosSource face recognition integration ───────────────────────────

def _make_vision_chunk_setup():
    """Returns (conn, mock_backup, mock_ollama) for vision chunk tests."""
    conn = _make_photos_db()
    mock_ollama = MagicMock()
    mock_ollama.generate.return_value = {"response": "A person smiling."}
    mock_backup = MagicMock()
    img_buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(img_buf, format="PNG")
    img_buf.seek(0)
    import tempfile, os
    tmp = tempfile.mkdtemp()
    img_path = os.path.join(tmp, "IMG_1234.HEIC")
    with open(img_path, "wb") as f:
        f.write(img_buf.getvalue())
    mock_backup.getFileDecryptedCopy.return_value = {"decryptedFilePath": img_path}
    return conn, mock_backup, mock_ollama


def test_vision_chunk_injects_people_into_text_and_metadata():
    conn, mock_backup, mock_ollama = _make_vision_chunk_setup()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    source = IPhonePhotosSource(mock_backup, LOCAL_TZ, mock_ollama, "test-model")

    with (
        patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)),
        patch("sources.iphone_photos._reverse_geocode", return_value=None),
        patch.object(source._faces, "identify", return_value=["Alice"]),
    ):
        chunks = source.get_chunks(start, end)

    photo_chunks = [c for c in chunks if c.source == "iphone_photos"]
    assert len(photo_chunks) == 1
    assert "[with: Alice]" in photo_chunks[0].text
    assert photo_chunks[0].metadata["people"] == ["Alice"]


def test_vision_chunk_no_people_when_face_index_empty():
    conn, mock_backup, mock_ollama = _make_vision_chunk_setup()
    start = datetime(2024, 1, 15, 4, 0, tzinfo=LOCAL_TZ)
    end = datetime(2024, 1, 16, 4, 0, tzinfo=LOCAL_TZ)

    source = IPhonePhotosSource(mock_backup, LOCAL_TZ, mock_ollama, "test-model")

    with (
        patch("sources.iphone_photos.open_backup_db", side_effect=lambda *a, **kw: _mock_db(conn)),
        patch("sources.iphone_photos._reverse_geocode", return_value=None),
    ):
        chunks = source.get_chunks(start, end)

    photo_chunks = [c for c in chunks if c.source == "iphone_photos"]
    assert len(photo_chunks) == 1
    assert "[with:" not in photo_chunks[0].text
    assert photo_chunks[0].metadata["people"] == []
