import json
import logging
import re
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

from .base import Chunk

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


_CLAUDE_CODE_SYSTEM_TAGS = frozenset({
    "command-name",
    "local-command-stdout",
    "task-notification",
    "system-reminder",
})

_SYSTEM_TAG_RE = re.compile(r"^<([a-zA-Z][a-zA-Z0-9_-]*)")


def _is_system_message(text: str) -> bool:
    m = _SYSTEM_TAG_RE.match(text)
    return m is not None and m.group(1) in _CLAUDE_CODE_SYSTEM_TAGS


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_messages(
    records: list[dict],
    start: datetime,
    end: datetime,
    local_tz: zoneinfo.ZoneInfo,
) -> list[str]:
    """Return interleaved User/Assistant lines in chronological order."""
    lines = []
    for r in records:
        raw = r.get("timestamp")
        if raw:
            ts = _parse_ts(raw)
            if not ts or not (start <= ts.astimezone(local_tz) < end):
                continue
        rtype = r.get("type")
        if rtype == "user":
            if r.get("isMeta"):
                continue
            content = r.get("message", {}).get("content", "")
            if isinstance(content, str):
                text = content.strip()
                if text and not _is_system_message(text):
                    lines.append(f"User: {text}")
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text", "").strip()
                    if text and not text.startswith("[Request interrupted"):
                        lines.append(f"User: {text}")
        elif rtype == "assistant":
            for block in r.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        lines.append(f"Assistant: {text}")
    return lines



class ClaudeCodeSource:
    def __init__(self, transcripts_root: str, local_tz: zoneinfo.ZoneInfo, ollama_client, llm_model: str):
        self._root = Path(transcripts_root)
        self._local_tz = local_tz
        self._ollama = ollama_client
        self._llm_model = llm_model

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        if not self._root.is_dir():
            log.warning(f"Claude transcripts root not found: {self._root}")
            return []

        # Collect in-window time ranges for all candidate files.
        skipped_subagent = 0
        file_ranges: list[tuple[Path, datetime, datetime]] = []
        for jsonl_file in sorted(self._root.rglob("*.jsonl")):
            if jsonl_file.parent.name == "subagents":
                skipped_subagent += 1
                continue
            try:
                ts = self._window_timestamps(jsonl_file, start, end)
                if ts:
                    file_ranges.append((jsonl_file, min(ts), max(ts)))
            except Exception as exc:
                log.error(f"  claude_code error scanning {jsonl_file.name}: {exc}")

        total_candidate = len(file_ranges)

        # Within each project directory, skip sessions whose in-window range is
        # strictly contained inside a larger sibling session — those are subagents
        # that were stored at the top level rather than in a subagents/ directory.
        def is_contained(path: Path, s: datetime, e: datetime) -> bool:
            for other, os, oe in file_ranges:
                if other.parent != path.parent or other == path:
                    continue
                if os <= s and oe >= e and (os, oe) != (s, e):
                    return True
            return False

        chunks = []
        skipped_contained = 0
        for jsonl_file, s, e in file_ranges:
            if is_contained(jsonl_file, s, e):
                log.debug(f"  claude_code: skipping contained session {jsonl_file.name}")
                skipped_contained += 1
                continue
            try:
                chunk = self._process_session(jsonl_file, start, end)
                if chunk:
                    chunks.append(chunk)
            except Exception as exc:
                log.error(f"  claude_code error in {jsonl_file.name}: {exc}")

        log.info(
            f"  claude_code: {len(chunks)} sessions"
            f" (found {total_candidate}, skipped {skipped_contained} contained"
            f", {skipped_subagent} subagent files)"
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
                raw = r.get("timestamp")
                if not raw:
                    continue
                ts = _parse_ts(raw)
                if ts and start <= ts.astimezone(self._local_tz) < end:
                    result.append(ts)
        return result

    def _process_session(self, path: Path, start: datetime, end: datetime) -> Chunk | None:
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

        # Only use timestamps within the target window for duration and anchor time.
        # This prevents resumed sessions from showing absurd multi-day durations.
        window_ts = []
        for r in records:
            raw = r.get("timestamp")
            if not raw:
                continue
            ts = _parse_ts(raw)
            if ts and start <= ts.astimezone(self._local_tz) < end:
                window_ts.append(ts)

        if not window_ts:
            return None

        session_start = min(window_ts)
        session_end = max(window_ts)
        local_start = session_start.astimezone(self._local_tz)

        lines = _extract_messages(records, start, end, self._local_tz)

        if not lines:
            return None

        content_for_llm = "\n\n".join(lines)

        duration_secs = (session_end - session_start).total_seconds()
        duration_str = _fmt_duration(duration_secs)

        project_name = path.parent.name.replace("C--Users-Bdfihn-Code-", "").replace("C--Users-Bdfihn-", "")

        summary = self._summarize(content_for_llm)

        text = (
            f"[{local_start.strftime('%Y-%m-%d %H:%M')}] "
            f"Claude Code session ({duration_str}) in {project_name}: {summary}"
        )

        return Chunk(
            window_start=local_start.isoformat(),
            text=text,
            source="claude_code",
            apps=["Claude Code"],
            total_secs=duration_secs,
            metadata={"session_uuid": path.stem},
        )

    def _summarize(self, content: str) -> str:
        response = self._ollama.chat(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        return response["message"]["content"].strip()


def _fmt_duration(secs: float) -> str:
    total = int(secs)
    h, m = divmod(total // 60, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"
