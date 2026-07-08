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


def test_generate_diary_entry_includes_sleep_instructions_with_sleep_chunk(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Diary body."}}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    chunks = [
        Chunk(
            window_start="2024-01-15T05:00:00",
            text="[2024-01-15 05:00] Sleep: 6h 30min total — Core 4h, Deep 1h 30min, REM 1h.",
            source="iphone_health",
        ),
        Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test"),
    ]
    etl.generate_diary_entry("2024-01-15", chunks)

    system_content = mock_ollama.chat.call_args[1]["messages"][0]["content"]
    assert "sleep" in system_content.lower()
    assert "stage breakdown" in system_content


def test_generate_diary_entry_omits_sleep_instructions_without_sleep_chunk(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Diary body."}}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Browsed the web.", source="test")]
    etl.generate_diary_entry("2024-01-15", chunks)

    system_content = mock_ollama.chat.call_args[1]["messages"][0]["content"]
    assert "sleep" not in system_content.lower()
    assert "first meaningful activity" in system_content


def test_generate_diary_entry_sets_num_ctx_to_avoid_truncation(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Diary body."}}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    chunks = [Chunk(window_start="2024-01-15T09:00:00", text="Woke up.", source="test")]
    etl.generate_diary_entry("2024-01-15", chunks)

    assert mock_ollama.chat.call_args[1]["options"] == {"num_ctx": 32768}
