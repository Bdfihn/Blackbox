import json
import zoneinfo
from datetime import datetime
from pathlib import Path

from sources.iphone_export import IPhoneExportSource

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")
_START = datetime(2026, 7, 8, 4, 0, tzinfo=LOCAL_TZ)
_END = datetime(2026, 7, 9, 4, 0, tzinfo=LOCAL_TZ)


def _write_export(tmp_path: Path, payload: dict, name: str = "2026-07-08.json") -> str:
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    return str(tmp_path)


def _get_chunks(export_dir: str):
    return IPhoneExportSource(export_dir, LOCAL_TZ).get_chunks(_START, _END)


def test_missing_file_returns_no_chunks(tmp_path):
    assert _get_chunks(str(tmp_path)) == []


def test_malformed_json_returns_no_chunks(tmp_path):
    (tmp_path / "2026-07-08.json").write_text("{not json", encoding="utf-8")
    assert _get_chunks(str(tmp_path)) == []


def test_date_field_mismatch_trusts_filename(tmp_path, caplog):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-01",
        "sleep": {"start": "2026-07-08T01:14:00-04:00", "core_min": 135},
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert "trusting filename" in caplog.text


def test_sleep_chunk_matches_backup_format(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep": {"start": "2026-07-08T01:14:00-04:00", "core_min": 135, "deep_min": 41,
                  "rem_min": 29, "awake_min": 176, "resp_rate_avg": 17.1},
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == ("[2026-07-08 01:14] Sleep: 3h 25m total — Core 2h 15m, Deep 41m, "
                      "REM 29m, awake 2h 56m. Avg respiratory rate: 17.1 breaths/min.")
    assert c.metadata["kind"] == "sleep"
    assert c.source == "iphone_export"
    assert c.total_secs == (135 + 41 + 29) * 60


def test_sleep_without_optional_fields(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep": {"start": "2026-07-08T01:14:00-04:00", "core_min": 380},
    })
    chunks = _get_chunks(export_dir)
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 6h 20m total — Core 6h 20m."


def test_sleep_with_zero_total_is_skipped(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep": {"start": "2026-07-08T01:14:00-04:00"},
    })
    assert _get_chunks(export_dir) == []
