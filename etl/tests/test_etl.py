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
