import json
import zoneinfo
from datetime import datetime, timedelta
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
        "sleep": "2026-07-08T01:14:00-04:00,2026-07-08T03:29:00-04:00,Core",
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert "trusting filename" in caplog.text


def test_sleep_lines_aggregate_into_sleep_chunk(tmp_path):
    lines = "\n".join([
        "2026-07-08T01:14:00-04:00,2026-07-08T03:29:00-04:00,Core",
        "2026-07-08T03:29:00-04:00,2026-07-08T04:10:00-04:00,Deep",
        "2026-07-08T04:10:00-04:00,2026-07-08T04:39:00-04:00,REM",
        "2026-07-08T04:39:00-04:00,2026-07-08T07:35:00-04:00,Awake",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == ("[2026-07-08 01:14] Sleep: 3h 25m total — Core 2h 15m, Deep 41m, "
                      "REM 29m, awake 2h 56m.")
    assert c.metadata["kind"] == "sleep"
    assert c.source == "iphone_export"
    assert c.total_secs == (135 + 41 + 29) * 60


def test_sleep_skips_malformed_lines_and_unknown_stages(tmp_path):
    lines = "\n".join([
        "2026-07-08T01:14:00-04:00,2026-07-08T02:14:00-04:00,Core",
        "not,a,valid,line",
        "garbage",
        "2026-07-08T02:14:00-04:00,2026-07-08T03:14:00-04:00,In Bed",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 1h total — Core 1h."


def test_sleep_with_no_valid_lines_emits_nothing(tmp_path):
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep": "Core\nDeep\nAwake"})
    assert _get_chunks(export_dir) == []


def test_sleep_accepts_space_separated_lines(tmp_path):
    lines = "\n".join([
        "2026-07-08T01:14:00-04:00 2026-07-08T02:14:00-04:00 Core",
        "2026-07-08T02:14:00-04:00 2026-07-08T03:14:00-04:00 In Bed",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-08", "sleep": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 1h total — Core 1h."


def test_sleep_accepts_shortcuts_locale_datetime_lines(tmp_path):
    lines = "\n".join([
        "Jul 9, 2026 at 2:41 AMJul 9, 2026 at 2:53 AMCore",
        "Jul 9, 2026 at 2:53 AMJul 9, 2026 at 3:02 AMDeep",
    ])
    export_dir = _write_export(tmp_path, {"date": "2026-07-09", "sleep": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-09 02:41] Sleep: 21m total — Core 12m, Deep 9m."


def test_sleep_locale_lines_with_narrow_no_break_space(tmp_path):
    lines = "Jul 9, 2026 at 2:41 AMJul 9, 2026 at 3:41 AMCore"
    export_dir = _write_export(tmp_path, {"date": "2026-07-09", "sleep": lines})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-09 02:41] Sleep: 1h total — Core 1h."


def test_sleep_accepts_json_array_of_lines(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep": [
            "2026-07-08T01:14:00-04:00,2026-07-08T02:14:00-04:00,Core",
            "2026-07-08T02:14:00-04:00,2026-07-08T02:44:00-04:00,Deep",
        ],
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08 01:14] Sleep: 1h 30m total — Core 1h, Deep 30m."


def test_step_lines_render_stretches_and_daily_total(tmp_path):
    export_dir = _write_export(tmp_path, {"step": "\n".join([
        "2026-07-08T09:10:00-04:00,2026-07-08T09:20:00-04:00,120",
        "2026-07-08T20:10:00-04:00,2026-07-08T20:40:00-04:00,3235",
        "2026-07-08T21:05:00-04:00,2026-07-08T21:30:00-04:00,1908",
    ])})
    chunks = _get_chunks(export_dir)
    texts = [c.text for c in chunks]
    assert "[2026-07-08 20:00–22:00] Sustained movement: ~5,100 steps." in texts
    assert "[2026-07-08] Steps: 5,263 total." in texts
    assert not any("Activity:" in t for t in texts)
    assert all(c.metadata == {"kind": "steps"} for c in chunks)


def test_quiet_hours_only_feed_the_total(tmp_path):
    export_dir = _write_export(tmp_path, {"step": "\n".join([
        "2026-07-08T09:10:00-04:00,2026-07-08T09:20:00-04:00,120",
        "2026-07-08T14:10:00-04:00,2026-07-08T14:20:00-04:00,85",
    ])})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08] Steps: 205 total."


def test_stretch_threshold_is_per_hour_boundary(tmp_path):
    export_dir = _write_export(tmp_path, {"step": "\n".join([
        "2026-07-08T20:10:00-04:00,2026-07-08T20:40:00-04:00,500",
        "2026-07-08T21:05:00-04:00,2026-07-08T21:30:00-04:00,499",
    ])})
    chunks = _get_chunks(export_dir)
    texts = [c.text for c in chunks]
    assert "[2026-07-08 20:00–21:00] Sustained movement: ~500 steps." in texts


def test_resting_hr_and_hrv_lines_build_vitals_chunk(tmp_path):
    payload = {
        "date": "2026-07-09",
        "resting_hr": "Jul 9, 2026 at 12:03 AMJul 9, 2026 at 6:24 PM68",
        "hrv": "\n".join([
            "Jul 9, 2026 at 1:27 AMJul 9, 2026 at 1:28 AM33.5",
            "Jul 9, 2026 at 5:17 AMJul 9, 2026 at 5:18 AM115.5",
            "Jul 9, 2026 at 9:21 AMJul 9, 2026 at 9:22 AM19.0",
        ]),
    }
    export_dir = _write_export(tmp_path, payload, name="2026-07-09.json")
    source = IPhoneExportSource(str(tmp_path), LOCAL_TZ)
    chunks = source.get_chunks(datetime(2026, 7, 9, 4, 0, tzinfo=LOCAL_TZ),
                               datetime(2026, 7, 10, 4, 0, tzinfo=LOCAL_TZ))
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == "[2026-07-09 00:03] Daily vitals: resting HR 68bpm, HRV 56ms."
    assert c.metadata["kind"] == "vitals"


def test_hrv_alone_builds_vitals_chunk(tmp_path):
    payload = {"date": "2026-07-09", "hrv": "Jul 9, 2026 at 1:27 AMJul 9, 2026 at 1:28 AM40.2"}
    export_dir = _write_export(tmp_path, payload, name="2026-07-09.json")
    source = IPhoneExportSource(str(tmp_path), LOCAL_TZ)
    chunks = source.get_chunks(datetime(2026, 7, 9, 4, 0, tzinfo=LOCAL_TZ),
                               datetime(2026, 7, 10, 4, 0, tzinfo=LOCAL_TZ))
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-09 04:00] Daily vitals: HRV 40ms."


def test_hr_field_is_ignored(tmp_path):
    payload = {"date": "2026-07-09", "hr": "Jul 9, 2026 at 12:03 AMJul 9, 2026 at 12:57 AM872"}
    export_dir = _write_export(tmp_path, payload, name="2026-07-09.json")
    source = IPhoneExportSource(str(tmp_path), LOCAL_TZ)
    chunks = source.get_chunks(datetime(2026, 7, 9, 4, 0, tzinfo=LOCAL_TZ),
                               datetime(2026, 7, 10, 4, 0, tzinfo=LOCAL_TZ))
    assert chunks == []


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


def test_activity_entries_render_via_steps_chunks(tmp_path):
    export_dir = _write_export(tmp_path, {"activity": [
        {"hour": "2026-07-08T14:00:00-04:00", "steps": 147, "avg_hr": 76},
        {"hour": "2026-07-08T15:00:00-04:00", "avg_hr": 70},
    ]})
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 1
    assert chunks[0].text == "[2026-07-08] Steps: 147 total."


def test_trailing_metrics_reads_prior_days(tmp_path):
    for day, steps in (("2026-07-06", 4000), ("2026-07-07", 6000)):
        (tmp_path / f"{day}.json").write_text(json.dumps({
            "step": f"{day}T10:10:00-04:00,{day}T10:40:00-04:00,{steps}",
            "resting_hr": f"{day}T23:00:00-04:00,{day}T23:00:00-04:00,60",
            "hrv": f"{day}T23:00:00-04:00,{day}T23:00:00-04:00,50",
        }), encoding="utf-8")
    src = IPhoneExportSource(str(tmp_path), LOCAL_TZ)
    metrics = src._trailing_metrics(_START)
    assert len(metrics) == 2
    assert {m["steps"] for m in metrics} == {4000, 6000}
    assert all(m["resting_hr"] == 60 and m["hrv_ms"] == 50 for m in metrics)


def test_trailing_metrics_skips_missing_and_bad_files(tmp_path):
    (tmp_path / "2026-07-05.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "2026-07-07.json").write_text(json.dumps({"hrv": "2026-07-07T23:00:00-04:00,2026-07-07T23:00:00-04:00,44"}), encoding="utf-8")
    src = IPhoneExportSource(str(tmp_path), LOCAL_TZ)
    metrics = src._trailing_metrics(_START)
    assert metrics == [{"steps": None, "resting_hr": None, "hrv_ms": 44.0}]


def test_trailing_metrics_ignores_today_and_future(tmp_path):
    (tmp_path / "2026-07-08.json").write_text(json.dumps({"step": "2026-07-08T10:00:00-04:00,2026-07-08T10:30:00-04:00,9999"}), encoding="utf-8")
    src = IPhoneExportSource(str(tmp_path), LOCAL_TZ)
    assert src._trailing_metrics(_START) == []


def _write_trailing_days(tmp_path, days, steps=5000, resting=60, hrv=50):
    for offset in range(1, days + 1):
        day = (_START - timedelta(days=offset)).strftime("%Y-%m-%d")
        (tmp_path / f"{day}.json").write_text(json.dumps({
            "step": f"{day}T10:00:00-04:00,{day}T10:30:00-04:00,{steps}",
            "resting_hr": f"{day}T23:00:00-04:00,{day}T23:00:00-04:00,{resting}",
            "hrv": f"{day}T23:00:00-04:00,{day}T23:00:00-04:00,{hrv}",
        }), encoding="utf-8")


def test_vitals_within_baseline_are_diary_suppressed(tmp_path):
    _write_trailing_days(tmp_path, 7)
    export_dir = _write_export(tmp_path, {
        "resting_hr": "2026-07-08T23:00:00-04:00,2026-07-08T23:00:00-04:00,61",
        "hrv": "2026-07-08T23:00:00-04:00,2026-07-08T23:00:00-04:00,52",
    })
    chunks = _get_chunks(export_dir)
    vitals = [c for c in chunks if c.metadata.get("kind") == "vitals"]
    assert len(vitals) == 1
    assert vitals[0].metadata["diary"] is False


def test_vitals_deviating_over_ten_percent_stay_diary_visible(tmp_path):
    _write_trailing_days(tmp_path, 7)
    export_dir = _write_export(tmp_path, {
        "resting_hr": "2026-07-08T23:00:00-04:00,2026-07-08T23:00:00-04:00,61",
        "hrv": "2026-07-08T23:00:00-04:00,2026-07-08T23:00:00-04:00,38",  # 24% below 50
    })
    chunks = _get_chunks(export_dir)
    vitals = [c for c in chunks if c.metadata.get("kind") == "vitals"]
    assert len(vitals) == 1
    assert "diary" not in vitals[0].metadata


def test_vitals_with_insufficient_history_are_diary_suppressed(tmp_path):
    _write_trailing_days(tmp_path, 3)
    export_dir = _write_export(tmp_path, {
        "hrv": "2026-07-08T23:00:00-04:00,2026-07-08T23:00:00-04:00,20",
    })
    chunks = _get_chunks(export_dir)
    vitals = [c for c in chunks if c.metadata.get("kind") == "vitals"]
    assert vitals[0].metadata["diary"] is False


def test_steps_total_compared_against_baseline(tmp_path):
    _write_trailing_days(tmp_path, 7, steps=5000)
    export_dir = _write_export(tmp_path, {
        "step": "2026-07-08T10:00:00-04:00,2026-07-08T10:30:00-04:00,9000",
    })
    chunks = _get_chunks(export_dir)
    total_lines = [c.text for c in chunks if "Steps:" in c.text]
    assert total_lines == ["[2026-07-08] Steps: 9,000 total — well above recent average (5,000/day)."]


def test_typical_steps_total_gets_no_comparison(tmp_path):
    _write_trailing_days(tmp_path, 7, steps=5000)
    export_dir = _write_export(tmp_path, {
        "step": "2026-07-08T10:00:00-04:00,2026-07-08T10:30:00-04:00,5500",
    })
    chunks = _get_chunks(export_dir)
    total_lines = [c.text for c in chunks if "Steps:" in c.text]
    assert total_lines == ["[2026-07-08] Steps: 5,500 total."]


def test_chunks_sorted_by_window_start(tmp_path):
    export_dir = _write_export(tmp_path, {
        "date": "2026-07-08",
        "sleep": "2026-07-08T05:14:00-04:00,2026-07-08T07:29:00-04:00,Core",
        "step": "2026-07-08T14:00:00-04:00,2026-07-08T14:30:00-04:00,312",
        "hrv": "2026-07-08T09:00:00-04:00,2026-07-08T09:01:00-04:00,44.0",
    })
    chunks = _get_chunks(export_dir)
    assert len(chunks) == 3
    assert [c.window_start for c in chunks] == sorted(c.window_start for c in chunks)
