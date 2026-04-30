import zoneinfo
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from sources.claude_code import (
    ClaudeCodeSource,
    _extract_messages,
    _fmt_duration,
    _is_system_message,
    _trim_user_text,
)

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

_IN_WINDOW = datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
_START = datetime(2024, 1, 15, 0, 0, 0, tzinfo=LOCAL_TZ)
_END = datetime(2024, 1, 16, 0, 0, 0, tzinfo=LOCAL_TZ)


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _user_str(text: str, ts: datetime = _IN_WINDOW) -> dict:
    return {"type": "user", "timestamp": _ts(ts), "message": {"content": text}}


def _user_list(blocks: list[dict], ts: datetime = _IN_WINDOW) -> dict:
    return {"type": "user", "timestamp": _ts(ts), "message": {"content": blocks}}


def _assistant(texts: list[str], ts: datetime = _IN_WINDOW) -> dict:
    blocks = [{"type": "text", "text": t} for t in texts]
    return {"type": "assistant", "timestamp": _ts(ts), "message": {"content": blocks}}


def _assistant_with_tool(texts: list[str], tool_name: str = "Bash", ts: datetime = _IN_WINDOW) -> dict:
    blocks = [{"type": "text", "text": t} for t in texts]
    blocks.append({"type": "tool_use", "name": tool_name, "id": "x", "input": {"command": "pytest"}})
    return {"type": "assistant", "timestamp": _ts(ts), "message": {"content": blocks}}


# --- _extract_messages ---

def test_extract_basic_conversation():
    records = [
        _user_str("Fix the bug"),
        _assistant(["I found the issue in foo.py"]),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: Fix the bug", "Assistant: I found the issue in foo.py"]


def test_extract_interleaves_chronologically():
    t1 = datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2024, 1, 15, 14, 1, 0, tzinfo=timezone.utc)
    t3 = datetime(2024, 1, 15, 14, 2, 0, tzinfo=timezone.utc)
    records = [
        _user_str("First message", ts=t1),
        _assistant(["First response"], ts=t2),
        _user_str("Second message", ts=t3),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines[0].startswith("User:")
    assert lines[1].startswith("Assistant:")
    assert lines[2].startswith("User:")


def test_extract_skips_tool_use_blocks():
    records = [_assistant_with_tool(["Reading the file"], tool_name="Read")]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["Assistant: Reading the file"]


def test_extract_skips_thinking_blocks():
    record = {
        "type": "assistant",
        "timestamp": _ts(_IN_WINDOW),
        "message": {
            "content": [
                {"type": "thinking", "thinking": "internal reasoning"},
                {"type": "text", "text": "Here is my answer"},
            ]
        },
    }
    lines = _extract_messages([record], _START, _END, LOCAL_TZ)
    assert lines == ["Assistant: Here is my answer"]


def test_extract_user_list_content_recovers_text():
    records = [
        _user_list([
            {"type": "tool_result", "tool_use_id": "x", "content": "big bash output..."},
            {"type": "text", "text": "But this is my actual message"},
        ])
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: But this is my actual message"]


def test_extract_user_list_filters_request_interrupted():
    records = [
        _user_list([{"type": "text", "text": "[Request interrupted by user for tool use]"}])
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == []


def test_extract_skips_meta_records():
    record = {**_user_str("should be skipped"), "isMeta": True}
    lines = _extract_messages([record], _START, _END, LOCAL_TZ)
    assert lines == []


def test_is_system_message_matches_known_tags():
    assert _is_system_message("<command-name>/clear</command-name>")
    assert _is_system_message("<local-command-stdout>See ya!</local-command-stdout>")
    assert _is_system_message("<task-notification>...</task-notification>")
    assert _is_system_message("<system-reminder>injected</system-reminder>")


def test_is_system_message_passes_unknown_tags():
    assert not _is_system_message("<html><body>pasted markup</body></html>")
    assert not _is_system_message("<config>some xml</config>")
    assert not _is_system_message("just text")


def test_extract_skips_claude_code_system_messages():
    records = [
        _user_str("<command-name>/clear</command-name>"),
        _user_str("<local-command-stdout>See ya!</local-command-stdout>"),
        _user_str("actual user message"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: actual user message"]


def test_extract_keeps_user_messages_starting_with_unknown_tag():
    records = [_user_str("<html>pasted markup</html>")]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: <html>pasted markup</html>"]


def test_extract_skips_out_of_window_records():
    out_of_window = datetime(2024, 1, 14, 12, 0, 0, tzinfo=timezone.utc)
    records = [
        _user_str("Old message", ts=out_of_window),
        _user_str("In window message", ts=_IN_WINDOW),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert len(lines) == 1
    assert "in window" in lines[0].lower()


def test_extract_multiple_assistant_text_blocks():
    record = {
        "type": "assistant",
        "timestamp": _ts(_IN_WINDOW),
        "message": {
            "content": [
                {"type": "text", "text": "First observation"},
                {"type": "tool_use", "name": "Bash", "id": "x", "input": {}},
                {"type": "text", "text": "Second observation after running"},
            ]
        },
    }
    lines = _extract_messages([record], _START, _END, LOCAL_TZ)
    assert lines == [
        "Assistant: First observation",
        "Assistant: Second observation after running",
    ]



# --- _fmt_duration ---

def test_fmt_duration_minutes_only():
    assert _fmt_duration(600) == "10m"


def test_fmt_duration_hours_only():
    assert _fmt_duration(7200) == "2h"


def test_fmt_duration_hours_and_minutes():
    assert _fmt_duration(5400) == "1h 30m"


# --- ClaudeCodeSource.get_chunks (integration-style, no LLM) ---

def _make_source(tmp_path, monkeypatch):
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Worked on foo."}}
    src = ClaudeCodeSource(
        transcripts_root=str(tmp_path),
        local_tz=LOCAL_TZ,
        ollama_client=mock_ollama,
        llm_model="test-model",
    )
    return src, mock_ollama


def _write_jsonl(path, records):
    import json
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_get_chunks_returns_chunk_for_valid_session(tmp_path, monkeypatch):
    project = tmp_path / "MyProject"
    project.mkdir()
    _write_jsonl(project / "session.jsonl", [
        _user_str("build the thing"),
        _assistant(["Done, wrote foo.py"]),
    ])
    src, _ = _make_source(tmp_path, monkeypatch)
    chunks = src.get_chunks(_START, _END)
    assert len(chunks) == 1
    assert "MyProject" in chunks[0].text


def test_get_chunks_sends_interleaved_content_to_llm(tmp_path, monkeypatch):
    project = tmp_path / "MyProject"
    project.mkdir()
    _write_jsonl(project / "session.jsonl", [
        _user_str("first request"),
        _assistant(["first response"]),
        _user_str("second request"),
        _assistant(["second response"]),
    ])
    src, mock_ollama = _make_source(tmp_path, monkeypatch)
    src.get_chunks(_START, _END)

    call_args = mock_ollama.chat.call_args
    content_sent = call_args[1]["messages"][1]["content"]
    assert "User: first request" in content_sent
    assert "Assistant: first response" in content_sent
    # Check ordering: first request should appear before second response
    assert content_sent.index("first request") < content_sent.index("second response")


def test_get_chunks_excludes_tool_result_content(tmp_path, monkeypatch):
    project = tmp_path / "MyProject"
    project.mkdir()
    _write_jsonl(project / "session.jsonl", [
        _user_list([
            {"type": "tool_result", "tool_use_id": "x", "content": "HUGE BASH OUTPUT " * 500},
        ]),
        _assistant(["Fixed the test"]),
    ])
    src, mock_ollama = _make_source(tmp_path, monkeypatch)
    src.get_chunks(_START, _END)

    content_sent = mock_ollama.chat.call_args[1]["messages"][1]["content"]
    assert "HUGE BASH OUTPUT" not in content_sent


def test_get_chunks_skips_session_outside_window(tmp_path, monkeypatch):
    project = tmp_path / "MyProject"
    project.mkdir()
    out_of_window = datetime(2024, 1, 13, 12, 0, 0, tzinfo=timezone.utc)
    _write_jsonl(project / "session.jsonl", [
        _user_str("old work", ts=out_of_window),
    ])
    src, _ = _make_source(tmp_path, monkeypatch)
    chunks = src.get_chunks(_START, _END)
    assert chunks == []


def test_get_chunks_includes_session_uuid_in_metadata(tmp_path, monkeypatch):
    project = tmp_path / "MyProject"
    project.mkdir()
    _write_jsonl(project / "abc123.jsonl", [
        _user_str("build the thing"),
        _assistant(["Done"]),
    ])
    src, _ = _make_source(tmp_path, monkeypatch)
    chunks = src.get_chunks(_START, _END)
    assert len(chunks) == 1
    assert chunks[0].metadata.get("session_uuid") == "abc123"


def test_get_chunks_produces_one_chunk_per_session_file(tmp_path, monkeypatch):
    """Each .jsonl file must produce at most one chunk — no duplication."""
    project = tmp_path / "MyProject"
    project.mkdir()
    _write_jsonl(project / "session-a.jsonl", [
        _user_str("task A"),
        _assistant(["done A"]),
    ])
    _write_jsonl(project / "session-b.jsonl", [
        _user_str("task B"),
        _assistant(["done B"]),
    ])
    src, _ = _make_source(tmp_path, monkeypatch)
    chunks = src.get_chunks(_START, _END)
    assert len(chunks) == 2
    uuids = [c.metadata.get("session_uuid") for c in chunks]
    assert sorted(uuids) == ["session-a", "session-b"]


# --- _trim_user_text ---

def test_trim_user_text_passes_normal_message():
    assert _trim_user_text("fix the bug in api.py") == "fix the bug in api.py"


def test_trim_user_text_drops_docker_build_output():
    assert _trim_user_text("#11 [6/6] COPY . .\n#12 exporting to image\n") == ""


def test_trim_user_text_drops_powershell_prompt():
    assert _trim_user_text(r"PS C:\Users\Bdfihn\Code> docker compose build") == ""


def test_trim_user_text_strips_pasted_output_after_human_text():
    msg = "bruh the build failed again\n#1 [internal] load build context\n#2 DONE 0.1s"
    result = _trim_user_text(msg)
    assert result == "bruh the build failed again"
    assert "#1 [internal]" not in result


def test_trim_user_text_truncates_long_human_message():
    long_msg = "x" * 600
    assert len(_trim_user_text(long_msg)) == 400


def test_extract_skips_docker_build_user_messages():
    records = [
        _user_str("#11 [6/6] COPY . .\n#12 exporting layers\n#12 DONE 22.0s"),
        _user_str("can you fix the dependency issue"),
        _assistant(["Sure, let me look at requirements.txt"]),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert len(lines) == 2
    assert all("#11" not in l for l in lines)
    assert any("fix the dependency issue" in l for l in lines)


def test_extract_strips_build_output_appended_to_user_message():
    records = [
        _user_str("the build is still failing\n#5 [internal] load build context\n#5 DONE 0.0s\n#6 pip install failed"),
        _assistant(["Let me check the Dockerfile"]),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    user_lines = [l for l in lines if l.startswith("User:")]
    assert len(user_lines) == 1
    assert user_lines[0] == "User: the build is still failing"


def test_extract_skips_local_command_caveat_messages():
    records = [
        _user_str("<local-command-caveat>Caveat: messages below were generated by running local commands.</local-command-caveat>"),
        _user_str("actually fix it"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: actually fix it"]


def test_extract_truncates_long_assistant_text():
    long_text = "A" * 1000
    records = [_assistant([long_text])]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert len(lines) == 1
    assert len(lines[0]) <= len("Assistant: ") + 600


def test_get_chunks_skips_subagent_directories(tmp_path, monkeypatch):
    project = tmp_path / "MyProject"
    project.mkdir()
    _write_jsonl(project / "parent.jsonl", [
        _user_str("parent task"),
        _assistant(["parent done"]),
    ])
    subagents_dir = project / "parent" / "subagents"
    subagents_dir.mkdir(parents=True)
    _write_jsonl(subagents_dir / "agent-xyz.jsonl", [
        _user_str("subagent task"),
        _assistant(["subagent done"]),
    ])
    src, _ = _make_source(tmp_path, monkeypatch)
    chunks = src.get_chunks(_START, _END)
    assert len(chunks) == 1
    assert chunks[0].metadata.get("session_uuid") == "parent"
