import json
import sqlite3
import zoneinfo
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from sources.agy import (
    AgySource,
    _clean_user_text,
    _extract_messages,
    _project_name_from_records,
    _project_name_from_uri,
    _transcript_path,
)

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

_IN_WINDOW = datetime(2026, 7, 10, 14, 0, 0, tzinfo=timezone.utc)
_START = datetime(2026, 7, 10, 4, 0, 0, tzinfo=LOCAL_TZ)
_END = datetime(2026, 7, 11, 4, 0, 0, tzinfo=LOCAL_TZ)


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _user_record(text: str, ts: datetime = _IN_WINDOW) -> dict:
    content = f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime\n</ADDITIONAL_METADATA>"
    return {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "created_at": _ts(ts),
        "content": content,
    }


def _assistant_record(text: str, ts: datetime = _IN_WINDOW, tool_calls: list = None) -> dict:
    return {
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "created_at": _ts(ts),
        "content": text,
        "tool_calls": tool_calls or [],
    }


# ── _clean_user_text & _extract_messages ─────────────────────────────────────

def test_extract_basic_conversation():
    records = [
        _user_record("Fix the database connection"),
        _assistant_record("I updated db.py with the new connection pool."),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == [
        "User: Fix the database connection",
        "Assistant: I updated db.py with the new connection pool.",
    ]


def test_extract_plain_user_input_without_tags():
    record = {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "created_at": _ts(_IN_WINDOW),
        "content": "just continue the work",
    }
    lines = _extract_messages([record], _START, _END, LOCAL_TZ)
    assert lines == ["User: just continue the work"]


def test_extract_skips_slash_commands():
    records = [
        _user_record("/exit"),
        _user_record("/model"),
        _user_record("actual request"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: actual request"]


def test_extract_skips_interrupted_requests():
    records = [
        _user_record("[Request interrupted by user]"),
        _user_record("carry on"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: carry on"]


def test_extract_skips_system_messages():
    records = [
        {
            "step_index": 2,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "created_at": _ts(_IN_WINDOW),
            "content": "Notice: process stopped",
        },
        _user_record("hello"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: hello"]


def test_extract_skips_empty_assistant_responses():
    records = [
        _assistant_record("", tool_calls=[{"name": "read_file"}]),
        _assistant_record(None, tool_calls=[{"name": "run_command"}]),
        _assistant_record("Here is the answer"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["Assistant: Here is the answer"]


def test_extract_truncates_long_assistant_text():
    long_msg = "A" * 1000
    records = [_assistant_record(long_msg)]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert len(lines) == 1
    assert len(lines[0]) <= len("Assistant: ") + 600


def test_extract_strips_docker_build_output_from_user_text():
    records = [
        _user_record("#11 [6/6] COPY . .\n#12 DONE 0.1s"),
        _user_record("the test failed\n#1 load build context\n#2 DONE 0.0s"),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: the test failed"]


def test_extract_skips_out_of_window_records():
    out_of_window = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    records = [
        _user_record("old request", ts=out_of_window),
        _user_record("new request", ts=_IN_WINDOW),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: new request"]


def test_extract_interleaves_chronologically():
    t1 = datetime(2026, 7, 10, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 10, 14, 5, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 10, 14, 10, 0, tzinfo=timezone.utc)
    records = [
        _user_record("first", ts=t1),
        _assistant_record("first reply", ts=t2),
        _user_record("second", ts=t3),
    ]
    lines = _extract_messages(records, _START, _END, LOCAL_TZ)
    assert lines == ["User: first", "Assistant: first reply", "User: second"]


# ── Project name derivation ──────────────────────────────────────────────────

def test_project_name_from_uri():
    assert _project_name_from_uri("file:///C:/Users/Bdfihn/Code/Blackbox") == "Blackbox"
    assert _project_name_from_uri("file:///home/user/project/") == "project"
    assert _project_name_from_uri("C:\\Users\\Bdfihn\\Code\\App") == "App"
    assert _project_name_from_uri("") == "Workspace"


def test_project_name_from_records():
    records = [
        _assistant_record("ok", tool_calls=[
            {"name": "read_file", "args": {"Path": "C:\\Users\\Bdfihn\\Code\\Blackbox\\README.md"}}
        ])
    ]
    assert _project_name_from_records(records) == "Blackbox"


# ── AgySource.get_chunks ─────────────────────────────────────────────────────

def _make_agy_env(tmp_path):
    brain = tmp_path / "brain"
    brain.mkdir(parents=True, exist_ok=True)
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = {"message": {"content": "Worked on the parser."}}
    src = AgySource(
        agy_root=str(tmp_path),
        local_tz=LOCAL_TZ,
        ollama_client=mock_ollama,
        llm_model="test-model",
        num_ctx=32768,
    )
    return src, mock_ollama


def _create_session(tmp_path, cid: str, records: list[dict], filename: str = "transcript.jsonl"):
    log_dir = tmp_path / "brain" / cid / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / filename
    target.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return target


def test_get_chunks_returns_chunk_for_valid_session(tmp_path):
    cid = "sess-123"
    t1 = datetime(2026, 7, 10, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 10, 14, 30, 0, tzinfo=timezone.utc)
    _create_session(tmp_path, cid, [
        _user_record("implement agy source", ts=t1),
        _assistant_record("done implementing", ts=t2),
    ])

    src, mock_ollama = _make_agy_env(tmp_path)
    chunks = src.get_chunks(_START, _END)

    assert len(chunks) == 1
    c = chunks[0]
    assert c.source == "agy"
    assert c.apps == ["Antigravity"]
    assert c.total_secs == 1800.0
    assert c.metadata["session_uuid"] == cid
    assert "Antigravity session (30m) in Workspace: Worked on the parser." in c.text
    assert mock_ollama.chat.called


def test_get_chunks_derives_project_from_db(tmp_path):
    cid = "sess-db"
    _create_session(tmp_path, cid, [
        _user_record("build it", ts=_IN_WINDOW),
        _assistant_record("built", ts=_IN_WINDOW),
    ])

    db_path = tmp_path / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id text PRIMARY KEY, workspace_uris text, nesting_depth integer, parent_conversation_id text)"
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
        (cid, '["file:///C:/Users/Bdfihn/Code/Blackbox"]', 0, "")
    )
    con.commit()
    con.close()

    src, _ = _make_agy_env(tmp_path)
    chunks = src.get_chunks(_START, _END)
    assert len(chunks) == 1
    assert "in Blackbox:" in chunks[0].text


def test_get_chunks_derives_project_from_history_fallback(tmp_path):
    cid = "sess-history"
    _create_session(tmp_path, cid, [
        _user_record("build it", ts=_IN_WINDOW),
        _assistant_record("built", ts=_IN_WINDOW),
    ])

    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps({"conversationId": cid, "workspace": "C:\\Users\\Bdfihn\\Code\\MyProject"}),
        encoding="utf-8",
    )

    src, _ = _make_agy_env(tmp_path)
    chunks = src.get_chunks(_START, _END)
    assert len(chunks) == 1
    assert "in MyProject:" in chunks[0].text


def test_get_chunks_skips_subagents_from_db(tmp_path):
    cid = "sess-subagent"
    _create_session(tmp_path, cid, [
        _user_record("subagent work", ts=_IN_WINDOW),
        _assistant_record("done", ts=_IN_WINDOW),
    ])

    db_path = tmp_path / "conversation_summaries.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id text PRIMARY KEY, workspace_uris text, nesting_depth integer, parent_conversation_id text)"
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
        (cid, '["file:///C:/Users/Bdfihn/Code/Blackbox"]', 1, "parent-uuid")
    )
    con.commit()
    con.close()

    src, _ = _make_agy_env(tmp_path)
    chunks = src.get_chunks(_START, _END)
    assert chunks == []


def test_get_chunks_prefers_transcript_full_if_available(tmp_path):
    cid = "sess-full"
    _create_session(tmp_path, cid, [
        _user_record("short text", ts=_IN_WINDOW),
        _assistant_record("short reply", ts=_IN_WINDOW),
    ], filename="transcript.jsonl")

    _create_session(tmp_path, cid, [
        _user_record("full text with all details", ts=_IN_WINDOW),
        _assistant_record("full reply with all details", ts=_IN_WINDOW),
    ], filename="transcript_full.jsonl")

    src, mock_ollama = _make_agy_env(tmp_path)
    src.get_chunks(_START, _END)

    content_sent = mock_ollama.chat.call_args[1]["messages"][1]["content"]
    assert "full text with all details" in content_sent


def test_summarize_sets_num_ctx_to_avoid_truncation(tmp_path):
    cid = "sess-ctx"
    _create_session(tmp_path, cid, [
        _user_record("build the thing", ts=_IN_WINDOW),
        _assistant_record("done", ts=_IN_WINDOW),
    ])
    src, mock_ollama = _make_agy_env(tmp_path)
    src.get_chunks(_START, _END)
    assert mock_ollama.chat.call_args[1]["options"] == {"num_ctx": 32768}


def test_get_chunks_returns_empty_when_no_brain_dir(tmp_path):
    src = AgySource(
        agy_root=str(tmp_path / "nonexistent"),
        local_tz=LOCAL_TZ,
        ollama_client=MagicMock(),
        llm_model="test-model",
        num_ctx=32768,
    )
    assert src.get_chunks(_START, _END) == []
