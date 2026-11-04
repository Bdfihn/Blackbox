import json
import logging
import re
import sqlite3
import zoneinfo
from datetime import datetime
from pathlib import Path

from .base import (
    Chunk,
    fmt_duration,
    _trim_user_text,
    _MAX_ASSISTANT_CHARS,
)

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    "You are summarizing a coding session for a personal diary. "
    "The input is a transcript of a conversation between a developer and an AI assistant. "
    "Write exactly 2-3 sentences in first person describing what was worked on. "
    "Focus on what was built, fixed, or decided — not the back-and-forth dialogue. "
    "Name specific files, features, bugs, and decisions. "
    "No filler phrases, no markdown, no bullet points, no advice. "
    "Start directly with the work, e.g. \"Debugged the rate-limiting bug in api.py...\" "
    "or \"Built the auth flow and wired it to the database.\""
)

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


def _clean_user_text(content: str) -> str:
    """Extract and trim the human request from USER_INPUT content."""
    if not content:
        return ""
    m = _USER_REQUEST_RE.search(content)
    text = m.group(1).strip() if m else content.strip()
    if text.startswith("/") or text.startswith("[Request interrupted"):
        return ""
    return _trim_user_text(text)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _project_name_from_uri(uri: str) -> str:
    """Derive project name from a workspace URI or path (e.g. file:///C:/Users/.../Blackbox -> Blackbox)."""
    if not uri:
        return "Workspace"
    clean = uri.replace("\\", "/").removeprefix("file:///").rstrip("/")
    return clean.rsplit("/", 1)[-1] or "Workspace"


def _project_name_from_records(records: list[dict]) -> str | None:
    """Derive project name by inspecting file paths in tool call arguments."""
    for r in records:
        for tc in r.get("tool_calls", []):
            args = tc.get("args", {})
            for val in args.values():
                if isinstance(val, str):
                    m = re.search(r"[A-Za-z]:[/\\](?:Users|home)[/\\][^/\\]+[/\\](?:Code|Projects|repos)[/\\]([^/\\]+)", val)
                    if m:
                        return m.group(1)
    return None


def _transcript_path(conv_dir: Path) -> Path | None:
    logs = conv_dir / ".system_generated" / "logs"
    full = logs / "transcript_full.jsonl"
    if full.is_file():
        return full
    compact = logs / "transcript.jsonl"
    if compact.is_file():
        return compact
    return None


def _load_metadata(agy_root: Path) -> dict[str, dict]:
    """Load metadata mapping cid -> {project, is_subagent} from SQLite db and/or history.jsonl."""
    meta: dict[str, dict] = {}

    db_path = agy_root / "conversation_summaries.db"
    if db_path.is_file():
        try:
            # Using immutable=1 allows opening SQLite databases stored on read-only mounts in WAL mode
            uri = f"file:{db_path.resolve().as_posix()}?immutable=1"
            con = sqlite3.connect(uri, uri=True)
            cur = con.cursor()
            for cid, uris_raw, depth, parent in cur.execute(
                "SELECT conversation_id, workspace_uris, nesting_depth, parent_conversation_id FROM conversation_summaries"
            ):
                is_sub = (depth and depth > 0) or bool(parent)
                proj = "Workspace"
                try:
                    uris = json.loads(uris_raw)
                    if isinstance(uris, list) and uris:
                        proj = _project_name_from_uri(uris[0])
                except Exception:
                    pass
                meta[cid] = {"project": proj, "is_subagent": is_sub}
            con.close()
        except Exception as e:
            log.warning(f"  agy: failed reading conversation_summaries.db: {e}")

    history_path = agy_root / "history.jsonl"
    if history_path.is_file():
        try:
            with history_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid = r.get("conversationId")
                    ws = r.get("workspace")
                    if cid and ws:
                        proj = _project_name_from_uri(ws)
                        meta.setdefault(cid, {})
                        if not meta[cid].get("project") or meta[cid]["project"] == "Workspace":
                            meta[cid]["project"] = proj
                        meta[cid].setdefault("is_subagent", False)
        except Exception as e:
            log.warning(f"  agy: failed reading history.jsonl: {e}")

    return meta


def _extract_messages(
    records: list[dict],
    start: datetime,
    end: datetime,
    local_tz: zoneinfo.ZoneInfo,
) -> list[str]:
    """Return interleaved User/Assistant lines in chronological order within [start, end)."""
    lines = []
    for r in records:
        raw = r.get("created_at")
        if not raw:
            continue
        ts = _parse_ts(raw)
        if not ts or not (start <= ts.astimezone(local_tz) < end):
            continue

        rtype = r.get("type")
        source = r.get("source")

        if rtype == "USER_INPUT" and source == "USER_EXPLICIT":
            content = r.get("content", "")
            if isinstance(content, str):
                text = _clean_user_text(content)
                if text:
                    lines.append(f"User: {text}")
        elif rtype == "PLANNER_RESPONSE" and source == "MODEL":
            content = r.get("content", "")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    lines.append(f"Assistant: {text[:_MAX_ASSISTANT_CHARS]}")
    return lines


class AgySource:
    def __init__(self, agy_root: str, local_tz: zoneinfo.ZoneInfo, ollama_client, llm_model: str, num_ctx: int):
        self._root = Path(agy_root)
        self._local_tz = local_tz
        self._ollama = ollama_client
        self._llm_model = llm_model
        self._num_ctx = num_ctx

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        brain_dir = self._root / "brain"
        if not brain_dir.is_dir():
            log.warning(f"Antigravity brain root not found: {brain_dir}")
            return []

        meta = _load_metadata(self._root)

        chunks = []
        skipped_subagent = 0

        for conv_dir in sorted(brain_dir.iterdir()):
            if not conv_dir.is_dir():
                continue
            cid = conv_dir.name
            conv_meta = meta.get(cid, {})
            if conv_meta.get("is_subagent"):
                skipped_subagent += 1
                continue

            transcript_file = _transcript_path(conv_dir)
            if not transcript_file:
                continue

            try:
                window_ts = self._window_timestamps(transcript_file, start, end)
                if not window_ts:
                    continue
                session_start, session_end = min(window_ts), max(window_ts)
                project = conv_meta.get("project", "Workspace")
                chunk = self._process_session(transcript_file, cid, project, start, end, session_start, session_end)
                if chunk:
                    chunks.append(chunk)
            except Exception as exc:
                log.error(f"  agy error processing session {cid}: {exc}")

        log.info(
            f"  agy: {len(chunks)} sessions"
            f" (skipped {skipped_subagent} subagents)"
        )
        return chunks

    def _window_timestamps(self, path: Path, start: datetime, end: datetime) -> list[datetime]:
        """Return all timestamps in the file that fall within [start, end)."""
        result = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw = r.get("created_at")
                if not raw:
                    continue
                ts = _parse_ts(raw)
                if ts and start <= ts.astimezone(self._local_tz) < end:
                    result.append(ts)
        return result

    def _process_session(
        self,
        path: Path,
        cid: str,
        project_name: str,
        start: datetime,
        end: datetime,
        session_start: datetime,
        session_end: datetime,
    ) -> Chunk | None:
        """Summarize one Antigravity conversation session into a Chunk."""
        records = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not records:
            return None

        if project_name == "Workspace":
            found_proj = _project_name_from_records(records)
            if found_proj:
                project_name = found_proj

        local_start = session_start.astimezone(self._local_tz)
        lines = _extract_messages(records, start, end, self._local_tz)
        if not lines:
            return None

        content_for_llm = "\n\n".join(lines)
        duration_secs = (session_end - session_start).total_seconds()
        duration_str = fmt_duration(duration_secs)

        summary = self._summarize(content_for_llm)

        text = (
            f"[{local_start.strftime('%Y-%m-%d %H:%M')}] "
            f"Antigravity session ({duration_str}) in {project_name}: {summary}"
        )

        return Chunk(
            window_start=local_start.isoformat(),
            text=text,
            source="agy",
            apps=["Antigravity"],
            total_secs=duration_secs,
            metadata={"session_uuid": cid},
        )

    def _summarize(self, content: str) -> str:
        response = self._ollama.chat(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": content},
            ],
            options={"num_ctx": self._num_ctx},
        )
        return response["message"]["content"].strip()
