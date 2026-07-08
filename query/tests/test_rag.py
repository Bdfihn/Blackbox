from unittest.mock import MagicMock

import rag

_CHUNK = {
    "text": "[2026-07-01 09:00] PC activity for 5 minutes. Top apps: chrome(4.2m).",
    "window_start": "2026-07-01T09:00:00-04:00",
    "apps": ["chrome"],
    "source": "activitywatch",
}


def _search_returning(chunks):
    return lambda question, **kwargs: chunks


def test_answer_sends_instructions_as_system_message(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "You browsed."}}
    monkeypatch.setattr(rag, "ollama_client", mock_ollama)
    monkeypatch.setattr(rag, "search", _search_returning([_CHUNK]))

    rag.answer("What did I do?")

    messages = mock_ollama.chat.call_args[1]["messages"]
    assert messages[0]["role"] == "system"
    assert "ONLY the context" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "What did I do?" in messages[1]["content"]
    assert "PC activity" in messages[1]["content"]
    assert "ONLY the context" not in messages[1]["content"]


def test_answer_sets_num_ctx_to_avoid_truncation(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "You browsed."}}
    monkeypatch.setattr(rag, "ollama_client", mock_ollama)
    monkeypatch.setattr(rag, "search", _search_returning([_CHUNK]))

    rag.answer("What did I do?")

    assert mock_ollama.chat.call_args[1]["options"] == {"num_ctx": 32768}


def test_answer_returns_fallback_when_no_chunks(monkeypatch):
    mock_ollama = MagicMock()
    monkeypatch.setattr(rag, "ollama_client", mock_ollama)
    monkeypatch.setattr(rag, "search", _search_returning([]))

    result = rag.answer("What did I do?")

    assert result["sources"] == []
    assert result["retrieved_chunks"] == []
    mock_ollama.chat.assert_not_called()


def test_answer_reports_sources_of_retrieved_chunks(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "You browsed."}}
    monkeypatch.setattr(rag, "ollama_client", mock_ollama)
    monkeypatch.setattr(rag, "search", _search_returning([_CHUNK]))

    result = rag.answer("What did I do?")

    assert result["answer"] == "You browsed."
    assert result["sources"] == ["activitywatch"]
    assert result["retrieved_chunks"] == [_CHUNK]
