import json
import logging
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

from .base import Chunk

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    "You are summarizing a Claude Code AI coding session for a personal diary. "
    "Write exactly 2-3 sentences in first person describing what was worked on. "
    "Be specific: name files, features, bugs, and decisions. "
    "No filler phrases, no markdown, no advice. "
    "Start directly with the work, e.g. \"Debugged the timezone handling...\""
)

_MAX_CONTENT_CHARS = 4000


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

        chunks = []
        for jsonl_file in sorted(self._root.rglob("*.jsonl")):
            try:
                chunk = self._process_session(jsonl_file, start, end)
                if chunk:
                    chunks.append(chunk)
            except Exception as e:
                log.error(f"  claude_code error in {jsonl_file.name}: {e}")

        log.info(f"  claude_code: {len(chunks)} sessions")
        return chunks

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

        timestamps = []
        for r in records:
            raw = r.get("timestamp")
            if raw:
                try:
                    timestamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
                except ValueError:
                    pass

        if not timestamps:
            return None

        session_start = min(timestamps)
        session_end = max(timestamps)

        local_start = session_start.astimezone(self._local_tz)
        local_end = session_end.astimezone(self._local_tz)

        if not any(start <= ts.astimezone(self._local_tz) < end for ts in timestamps):
            return None

        user_texts, assistant_texts = [], []
        for r in records:
            rtype = r.get("type")
            if rtype == "user":
                if r.get("isMeta"):
                    continue
                msg = r.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip() and not content.strip().startswith("<"):
                    user_texts.append(content.strip())
            elif rtype == "assistant":
                msg = r.get("message", {})
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            assistant_texts.append(text)

        if not user_texts and not assistant_texts:
            return None

        combined = []
        for u in user_texts:
            combined.append(f"User: {u}")
        for a in assistant_texts:
            combined.append(f"Assistant: {a}")

        content_for_llm = "\n\n".join(combined)
        if len(content_for_llm) > _MAX_CONTENT_CHARS:
            content_for_llm = content_for_llm[:_MAX_CONTENT_CHARS] + "\n[truncated]"

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
