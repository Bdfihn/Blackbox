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


def test_consecutive_same_title_events_merge_into_one_episode(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [
            _event("2026-07-01T10:00:00Z", 300, app="chrome.exe", title="BLG VS HLE - MSI 2026 - YouTube - Google Chrome"),
            _event("2026-07-01T10:05:00Z", 300, app="chrome.exe", title="BLG VS HLE - MSI 2026 - YouTube - Google Chrome"),
        ],
    })
    chunks = src.get_chunks(START, END)
    assert len(chunks) == 1
    assert chunks[0].total_secs == 600
    assert 'YouTube: "BLG VS HLE - MSI 2026" (10m)' in chunks[0].text
    assert "10:00–10:10" in chunks[0].text


def test_gap_over_tolerance_splits_episodes(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [
            _event("2026-07-01T10:00:00Z", 300, app="chrome.exe", title="BLG VS HLE - MSI 2026 - YouTube - Google Chrome"),
            # ends 10:05; next starts 10:08 → 180s gap > EPISODE_GAP_SECS
            _event("2026-07-01T10:08:00Z", 300, app="chrome.exe", title="BLG VS HLE - MSI 2026 - YouTube - Google Chrome"),
        ],
    })
    chunks = src.get_chunks(START, END)
    assert len(chunks) == 2


def test_gap_within_tolerance_bridges_episode(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [
            _event("2026-07-01T10:00:00Z", 300, app="chrome.exe", title="BLG VS HLE - MSI 2026 - YouTube - Google Chrome"),
            # ends 10:05; next starts 10:06:30 → 90s gap <= 120s
            _event("2026-07-01T10:06:30Z", 300, app="chrome.exe", title="BLG VS HLE - MSI 2026 - YouTube - Google Chrome"),
        ],
    })
    chunks = src.get_chunks(START, END)
    assert len(chunks) == 1
    assert chunks[0].total_secs == 600


def test_short_episodes_fold_into_briefly_line(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [
            _event("2026-07-01T10:00:00Z", 30, app="chrome.exe", title="skirk dire - YouTube - Google Chrome"),
            _event("2026-07-01T10:00:30Z", 20, app="chrome.exe", title="Marketplace - Google Chrome"),
            _event("2026-07-01T10:01:00Z", 300, app="chrome.exe", title="Skirk vs Ball in 66s - YouTube - Google Chrome"),
        ],
    })
    chunks = src.get_chunks(START, END)
    assert len(chunks) == 2
    assert chunks[0].text.startswith("[2026-07-01 10:00] Briefly: ")
    assert '"skirk dire" (YouTube)' in chunks[0].text
    assert '"Marketplace" (Chrome)' in chunks[0].text
    assert 'YouTube: "Skirk vs Ball in 66s" (5m)' in chunks[1].text


def test_title_equal_to_label_renders_label_only(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [_event("2026-07-01T10:00:00Z", 300, app="Genshin Impact.exe", title="Genshin Impact")],
    })
    chunks = src.get_chunks(START, END)
    assert len(chunks) == 1
    assert "Genshin Impact (5m)" in chunks[0].text
    assert '"' not in chunks[0].text


def test_episode_chunks_are_chronological(monkeypatch):
    src = _source(monkeypatch, {
        WINDOW_BUCKET: [
            _event("2026-07-01T11:00:00Z", 300, app="Code.exe", title="etl.py"),
            _event("2026-07-01T10:00:00Z", 300, app="chrome.exe", title="Docs - Google Chrome"),
        ],
    })
    chunks = src.get_chunks(START, END)
    assert [c.window_start for c in chunks] == sorted(c.window_start for c in chunks)
