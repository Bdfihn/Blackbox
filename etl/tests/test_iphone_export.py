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


def test_sleep_samples_lines_aggregate_into_sleep_chunk(tmp_path):
    lines = "\n".join([
        "2026-07-08T01:14:00-04:00,2026-07-08T03:29:00-04:00,Core",
        "2026-07-08T03:29:00-04:00,2026-07-08T04:10:00-04:00,Deep",
        "2026-07-08T04:10:00-04:00,2026-07-08T04:39:00-04:00,REM",
        "2026-07-08T04:39:00-04:00,2026-07-08T07:35:00-04:00,Awake",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep_samples": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == ("[2026-07-08 01:14] Sleep: 3h 25m total — Core 2h 15m, Deep 41m, "
                      "REM 29m, awake 2h 56m.")
    assert c.metadata["kind"] == "sleep"
    assert c.total_secs == (135 + 41 + 29) * 60


def test_sleep_samples_skips_malformed_lines_and_unknown_stages(tmp_path):
    lines = "\n".join([
        "2026-07-08T01:14:00-04:00,2026-07-08T02:14:00-04:00,Core",
        "not,a,valid,line",
        "garbage",
        "2026-07-08T02:14:00-04:00,2026-07-08T03:14:00-04:00,In Bed",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep_samples": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 1h total — Core 1h."


def test_sleep_samples_with_no_valid_lines_emits_nothing(tmp_path):
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep_samples": "Core\nDeep\nAwake"})
    assert _get_chunks(export_dir) == []


def test_sleep_samples_accepts_space_separated_lines(tmp_path):
    lines = "\n".join([
        "2026-07-08T01:14:00-04:00 2026-07-08T02:14:00-04:00 Core",
        "2026-07-08T02:14:00-04:00 2026-07-08T03:14:00-04:00 In Bed",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep_samples": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 1h total — Core 1h."


def test_sleep_samples_accepts_shortcuts_locale_datetime_lines(tmp_path):
    lines = "\n".join([
        "Jul 9, 2026 at 2:41 AMJul 9, 2026 at 2:53 AMCore",
        "Jul 9, 2026 at 2:53 AMJul 9, 2026 at 3:02 AMDeep",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-09", "sleep_samples": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-09 02:41] Sleep: 21m total — Core 12m, Deep 9m."


def test_sleep_samples_locale_lines_with_narrow_no_break_space(tmp_path):
    lines = "Jul 9, 2026 at 2:41 AMJul 9, 2026 at 3:41 AMCore"
    export_dir = _write_export(tmp_path, {"date": "2026-07-09", "sleep_samples": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-09 02:41] Sleep: 1h total — Core 1h."


def test_sleep_samples_accepts_json_array_of_lines(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep_samples": [
            "2026-07-08T01:14:00-04:00,2026-07-08T02:14:00-04:00,Core",
            "2026-07-08T02:14:00-04:00,2026-07-08T02:44:00-04:00,Deep",
        ],
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 1h 30m total — Core 1h, Deep 30m."


def test_vitals_chunk_matches_backup_format(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "vitals": {"time": "2026-07-09T01:00:00-04:00", "resting_hr": 71, "hrv_ms": 41,
                   "walking_hr_avg": 96, "vo2_max": 42.13, "daylight_min": 16, "exercise_min": 42},
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == ("[2026-07-09 01:00] Daily vitals: resting HR 71bpm, HRV 41ms, "
                      "walking HR avg 96bpm, VO2 max 42.1 ml/kg/min, 42 exercise min, "
                      "16 min daylight.")
    assert c.metadata["kind"] == "vitals"
    assert c.total_secs == 0


def test_vitals_without_time_anchors_at_window_start(tmp_path):
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "vitals": {"resting_hr": 71}})
    chunks = _get_chunks(export_dir)
    assert chunks[0].text == "[2026-07-08 04:00] Daily vitals: resting HR 71bpm."


def test_empty_vitals_section_emits_nothing(tmp_path):
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "vitals": {}})
    assert _get_chunks(export_dir) == []


def test_activity_chunks_match_backup_format(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "activity": [
            {"hour": "2026-07-08T14:00:00-04:00", "steps": 147, "avg_hr": 76, "avg_mets": 3.04},
            {"hour": "2026-07-08T15:00:00-04:00", "avg_hr": 70},
        ],
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 2
    assert chunks[0].text == "[2026-07-08 14:00] Activity: 147 steps, avg HR 76bpm, avg effort 3.0 METs."
    assert chunks[0].total_secs == 3600
    assert chunks[0].metadata == {}
    assert chunks[1].text == "[2026-07-08 15:00] Activity: avg HR 70bpm."


def test_workout_chunk_matches_backup_format(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "workouts": [{"start": "2026-07-08T18:02:00-04:00", "type": "Running",
                      "duration_min": 30, "avg_hr": 150, "kcal": 320.4, "distance_km": 4.8}],
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == "[2026-07-08 18:02] Running: 30m, avg HR 150bpm, 320 kcal, 4.80km."
    assert c.metadata["kind"] == "workout"
    assert c.total_secs == 1800


def test_chunks_sorted_by_window_start(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep": {"start": "2026-07-08T01:14:00-04:00", "core_min": 135},
        "activity": [{"hour": "2026-07-08T14:00:00-04:00", "steps": 10}],
        "workouts": [{"start": "2026-07-08T08:00:00-04:00", "type": "Walking", "duration_min": 20}],
    })
    chunks = _get_chunks(export_dir)
    assert [c.window_start for c in chunks] == sorted(c.window_start for c in chunks)
