import zoneinfo
from datetime import datetime

from sources import ActivityWatchSource
from sources.activitywatch import normalize_title

UTC = zoneinfo.ZoneInfo("UTC")
START = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
END = datetime(2026, 7, 2, 4, 0, tzinfo=UTC)

WINDOW_BUCKET = "aw-watcher-window_pc"
AFK_BUCKET = "aw-watcher-afk_pc"


def _event(ts: str, duration: float, **data) -> dict:
    return {"timestamp": ts, "duration": duration, "data": data}


def _source(monkeypatch, events_by_bucket: dict) -> ActivityWatchSource:
    src = ActivityWatchSource("http://aw", UTC)
    monkeypatch.setattr(src, "_fetch_buckets", lambda: list(events_by_bucket))
    monkeypatch.setattr(
        src, "_fetch_events",
        lambda bucket_id, start, end: events_by_bucket[bucket_id],
    )
    return src


def test_window_event_during_afk_is_dropped(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [_event("2026-07-01T10:00:00Z", 300, app="chrome", title="YouTube")],
        AFK_BUCKET: [_event("2026-07-01T09:00:00Z", 7200, status="afk")],
    })

    assert src.get_chunks(START, END) == []


def test_window_event_clipped_to_not_afk_overlap(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [_event("2026-07-01T10:00:00Z", 300, app="chrome", title="Docs")],
        AFK_BUCKET: [
            _event("2026-07-01T10:00:00Z", 120, status="not-afk"),
            _event("2026-07-01T10:02:00Z", 180, status="afk"),
        ],
    })

    chunks = src.get_chunks(START, END)

    assert len(chunks) == 1
    assert chunks[0].total_secs == 120


def test_window_event_spanning_multiple_active_intervals(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [_event("2026-07-01T10:00:00Z", 600, app="Code", title="etl.py")],
        AFK_BUCKET: [
            _event("2026-07-01T10:00:00Z", 120, status="not-afk"),
            _event("2026-07-01T10:02:00Z", 180, status="afk"),
            _event("2026-07-01T10:05:00Z", 120, status="not-afk"),
        ],
    })

    chunks = src.get_chunks(START, END)

    assert len(chunks) == 1
    assert chunks[0].total_secs == 240


def test_no_afk_bucket_keeps_full_durations(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [_event("2026-07-01T10:00:00Z", 300, app="chrome", title="Docs")],
    })

    chunks = src.get_chunks(START, END)

    assert len(chunks) == 1
    assert chunks[0].total_secs == 300


def test_afk_bucket_with_no_active_time_drops_everything(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [_event("2026-07-01T10:00:00Z", 300, app="chrome", title="Docs")],
        AFK_BUCKET: [],
    })

    assert src.get_chunks(START, END) == []


def test_normalize_title_strips_youtube_and_browser_suffix():
    title, label = normalize_title(
        "chrome.exe",
        "Stygian Onslaught 6.7 - Dire - Skirk vs Ball in 66s | Genshin Impact - YouTube - Google Chrome",
    )
    assert title == "Stygian Onslaught 6.7 - Dire - Skirk vs Ball in 66s | Genshin Impact"
    assert label == "YouTube"


def test_normalize_title_strips_browser_suffix_only():
    title, label = normalize_title("chrome.exe", "Marketplace - Standing Desk - Google Chrome")
    assert title == "Marketplace - Standing Desk"
    assert label == "Chrome"


def test_normalize_title_plain_app_falls_back_to_app_label():
    title, label = normalize_title("GenshinImpact.exe", "Genshin Impact")
    assert title == "Genshin Impact"
    assert label == "GenshinImpact"


def test_normalize_title_keeps_inner_dashes():
    title, label = normalize_title("chrome.exe", "BLG VS HLE - MSI 2026 - YouTube - Google Chrome")
    assert title == "BLG VS HLE - MSI 2026"
    assert label == "YouTube"


def test_normalize_title_empty_title():
    title, label = normalize_title("Code.exe", "")
    assert title == ""
    assert label == "Code"
