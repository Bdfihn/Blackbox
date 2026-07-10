from unittest.mock import MagicMock

import etl
from sources import Chunk


def test_generate_diary_entry_sends_instructions_as_system_message(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Diary body."}}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Woke up.", source="test")]
    etl.generate_diary_entry("2024-01-15", chunks)

    messages = mock_ollama.chat.call_args[1]["messages"]
    assert messages[0]["role"] == "system"
    assert "Plain text only" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Woke up." in messages[1]["content"]
    assert "Plain text only" not in messages[1]["content"]


def test_generate_diary_entry_returns_no_activity_message_for_empty_chunks():
    result = etl.generate_diary_entry("2024-01-15", [])
    assert "No activity recorded" in result


def _diary_system_content(monkeypatch, chunks):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Diary body."}}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)
    etl.generate_diary_entry("2024-01-15", chunks)
    return mock_ollama.chat.call_args[1]["messages"][0]["content"]


def _health_chunk(kind, text):
    return Chunk(
        window_start="2024-01-15T05:00:00",
        text=text,
        source="iphone_export",
        metadata={"kind": kind},
    )


def test_generate_diary_entry_includes_sleep_instructions_with_sleep_chunk(monkeypatch):
    chunks = [
        _health_chunk("sleep", "[2024-01-15 05:00] Sleep: 6h 30min total — Core 4h, Deep 1h 30min, REM 1h."),
        Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test"),
    ]
    system_content = _diary_system_content(monkeypatch, chunks)
    assert "sleep" in system_content.lower()
    assert "stage breakdown" in system_content


def test_generate_diary_entry_omits_sleep_instructions_without_sleep_chunk(monkeypatch):
    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test")]
    system_content = _diary_system_content(monkeypatch, chunks)
    assert "sleep" not in system_content.lower()
    assert "first meaningful activity" in system_content


def test_generate_diary_entry_includes_vitals_and_workout_rules_when_present(monkeypatch):
    chunks = [
        _health_chunk("vitals", "[2024-01-16 01:00] Daily vitals: resting HR 68bpm, HRV 41ms."),
        _health_chunk("workout", "[2024-01-15 18:00] Running: 30min, avg HR 150bpm, 320 kcal."),
        Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test"),
    ]
    system_content = _diary_system_content(monkeypatch, chunks)
    assert "For daily vitals" in system_content
    assert "For workouts" in system_content


def test_generate_diary_entry_omits_vitals_and_workout_rules_when_absent(monkeypatch):
    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test")]
    system_content = _diary_system_content(monkeypatch, chunks)
    assert "For daily vitals" not in system_content
    assert "HRV" not in system_content
    assert "For workouts" not in system_content


def test_select_diary_chunks_filters_suppressed():
    visible = Chunk(window_start="2024-01-15T09:00:00", text="a", source="test")
    suppressed = Chunk(window_start="2024-01-15T10:00:00", text="b", source="test", metadata={"diary": False})
    assert etl.select_diary_chunks([visible, suppressed]) == [visible]


def test_prompt_demands_specifics_over_categories(monkeypatch):
    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test")]
    system_content = _diary_system_content(monkeypatch, chunks)
    assert "Name the specific" in system_content
    assert "event log with exact titles" in system_content


def test_generate_diary_entry_sets_num_ctx_to_avoid_truncation(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Diary body."}}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Woke up.", source="test")]
    etl.generate_diary_entry("2024-01-15", chunks)

    assert mock_ollama.chat.call_args[1]["options"] == {"num_ctx": 32768}
