import logging
import subprocess
import zoneinfo
from datetime import datetime
from pathlib import Path

from .base import Chunk

log = logging.getLogger(__name__)


class GitSource:
    def __init__(self, repos_root: str, local_tz: zoneinfo.ZoneInfo):
        self._repos_root = Path(repos_root)
        self._local_tz = local_tz

    def get_chunks(self, start: datetime, end: datetime) -> list[Chunk]:
        repos = self._find_repos()
        chunks = []
        for repo in repos:
            try:
                commits = self._fetch_commits(repo, start, end)
                chunks.extend(commits)
            except Exception as e:
                log.error(f"  git error in {repo.name}: {e}")
        log.info(f"  git: {len(chunks)} commits across {len(repos)} repos")
        return chunks

    def _find_repos(self) -> list[Path]:
        if not self._repos_root.is_dir():
            return []
        repos = []
        for candidate in self._repos_root.iterdir():
            if candidate.is_dir() and (candidate / ".git").exists():
                repos.append(candidate)
        return repos

    def _fetch_commits(self, repo: Path, start: datetime, end: datetime) -> list[Chunk]:
        result = subprocess.run(
            [
                "git", "log",
                f"--after={start.isoformat()}",
                f"--before={end.isoformat()}",
                "--all",
                "--format=%H\x1f%ai\x1f%an\x1f%s",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

        chunks = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\x1f", 3)
            if len(parts) != 4:
                continue
            sha, timestamp_str, author, subject = parts
            try:
                ts = datetime.fromisoformat(timestamp_str).astimezone(self._local_tz)
            except ValueError:
                continue

            text = (
                f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
                f"Git commit in {repo.name} by {author}: \"{subject}\" ({sha[:7]})"
            )
            chunks.append(Chunk(
                window_start=ts.isoformat(),
                text=text,
                source="git",
                apps=[repo.name],
                total_secs=0,
            ))
        return chunks
