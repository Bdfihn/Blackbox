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
        source="iphone_health",
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


def test_embed_short_text_single_pass(monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.embeddings.return_value = {"embedding": [0.1, 0.2]}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    vec = etl.embed("Short chunk text.")
    assert vec == [0.1, 0.2]
    assert mock_ollama.embeddings.call_count == 1
    assert mock_ollama.embeddings.call_args[1]["prompt"] == "Short chunk text."


def test_embed_chunks_and_pools_large_text_without_truncating(monkeypatch):
    mock_ollama = MagicMock()
    # Return valid vectors for any number of sub-chunks
    mock_ollama.embeddings.side_effect = lambda **kwargs: {"embedding": [0.6, 0.8]}
    monkeypatch.setattr(etl, "ollama_client", mock_ollama)

    # 3500 chars exceeds 2500 chunk limit, splitting into sub-chunks
    part1 = "First part of the long text. " * 60
    part2 = "Second part of the long text. " * 60
    huge_text = f"{part1}\n{part2}"

    vec = etl.embed(huge_text)
    assert mock_ollama.embeddings.call_count >= 2
    # Verify all parts of text were included across the sub-chunks (no truncation)
    all_prompts = "".join(call[1]["prompt"] for call in mock_ollama.embeddings.call_args_list)
    assert "First part" in all_prompts
    assert "Second part" in all_prompts
    # Verify vector is normalized to unit length
    assert len(vec) == 2
    mag = sum(x * x for x in vec)
    assert abs(mag - 1.0) < 1e-5
