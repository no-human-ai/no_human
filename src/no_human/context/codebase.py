"""Codebase context via agentic grep/glob + git log (no vector index — PLAN 16#2)."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

from ..core.task import Task
from .base import ContextChunk, keywords

#: rg/grep emit `<path>:<line>:<text>`. The path is matched non-greedily so the
#: FIRST `:<digits>:` field wins: a Windows drive-letter colon is followed by a
#: backslash, not digits, so `C:\repo\lib\math.js` stays whole. `(.*)$` keeps the
#: rest verbatim, so colons inside the matched source line are preserved.
_MATCH_LINE_RE = re.compile(r"^(.+?):(\d+):(.*)$")


class CodebaseSource:
    name = "codebase"

    def __init__(self, max_files: int = 8, max_commits: int = 10):
        self.max_files = max_files
        self.max_commits = max_commits

    async def gather(self, task: Task) -> list[ContextChunk]:
        if not task.repo_path or not Path(task.repo_path).exists():
            return []
        return await asyncio.to_thread(self._gather_sync, task)

    def _gather_sync(self, task: Task) -> list[ContextChunk]:
        repo = Path(task.repo_path)
        chunks: list[ContextChunk] = []
        terms = keywords(task)

        file_hits = self._search(repo, terms)
        for path, lines in list(file_hits.items())[: self.max_files]:
            chunks.append(ContextChunk(
                source="codebase",
                title=str(path.relative_to(repo)),
                content="\n".join(lines[:20]),
                ref=str(path),
                score=float(len(lines)),
            ))

        log = self._git_log(repo)
        if log:
            chunks.append(ContextChunk(
                source="codebase", title="recent commits",
                content=log, ref=str(repo), score=0.0,
            ))
        return chunks

    # Vendored / generated trees that pollute relevance — never search these.
    _EXCLUDE_DIRS = (
        ".git", "node_modules", "dist", "build", "target", ".venv*", "venv*",
        "vendor", "__pycache__", ".idea", ".gradle", "coverage", "out",
        ".next", ".nuxt", ".cache", "public", "static",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
    )
    _EXCLUDE_GLOBS = (
        "*.min.js", "*.map", "*.lock", "*.snap", "*.bundle.js",
        "*.svg", "*.png", "*.jpg", "*.ico", "*.woff*", "*.ttf",
        "*.pyc", "*.class", "*.jar",
    )
    _SEARCH_TIMEOUT = 20  # must be < ContextGatherer.per_source_timeout (30s)

    @staticmethod
    def _parse_match_line(line: str) -> tuple[str, str, str] | None:
        """(path, line_no, text) from one rg/grep output line, or None if it isn't one."""
        m = _MATCH_LINE_RE.match(line)
        return (m.group(1), m.group(2), m.group(3)) if m else None

    # Content bounds — a property of the gatherer's contract, NOT of ripgrep's
    # flag set. Enforced in _search's shared parse loop so every backend
    # (rg, grep, any future third one) is bound by them. grep has no
    # --max-filesize equivalent, so a flag-for-flag port is not available.
    _MAX_FILESIZE_BYTES = 256 * 1024   # files larger than this contribute nothing
    _MAX_MATCHES_PER_FILE = 5          # at most N matched lines per file
    _MAX_LINE_CHARS = 200              # each surviving line truncated to N chars

    def _search(self, repo: Path, terms: list[str]) -> dict[Path, list[str]]:
        """Return {file: matched lines} ranked by hit count, using rg or grep."""
        if not terms:
            return {}
        pattern = "|".join(t.replace("+", r"\+") for t in terms)
        hits: dict[Path, list[str]] = {}
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-i",
                   "--max-columns", str(self._MAX_LINE_CHARS),
                   "--max-count", str(self._MAX_MATCHES_PER_FILE),
                   "--max-filesize", str(self._MAX_FILESIZE_BYTES),
                   "-e", pattern]
            for d in self._EXCLUDE_DIRS:
                cmd += ["--glob", f"!**/{d}/**"]
            for g in self._EXCLUDE_GLOBS:
                cmd += ["--glob", f"!{g}"]
            cmd.append(str(repo))
        else:
            cmd = ["grep", "-rniE", pattern]
            for d in self._EXCLUDE_DIRS:
                cmd.append(f"--exclude-dir={d}")
            for g in self._EXCLUDE_GLOBS:
                cmd.append(f"--exclude={g}")
            cmd.append(str(repo))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=self._SEARCH_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            # Return partial results from whatever stdout was captured.
            proc_stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            proc = type("R", (), {"stdout": proc_stdout, "returncode": -1})()
        oversize: dict[Path, bool] = {}   # stat() cache — one stat per file, not per line
        for line in proc.stdout.splitlines():
            parsed = self._parse_match_line(line)
            if parsed is None:
                continue
            raw_path, line_no, text = parsed
            fp = Path(raw_path)
            existing = hits.setdefault(fp, [])
            if len(existing) >= self._MAX_MATCHES_PER_FILE:
                continue                      # per-file match cap (both branches)
            if fp not in oversize:
                try:
                    oversize[fp] = fp.stat().st_size > self._MAX_FILESIZE_BYTES
                except OSError:
                    oversize[fp] = False      # unknown size is NOT a reason to drop a
                                              # real positive; only a measured
                                              # over-cap size excludes.
            if oversize[fp]:
                continue                      # size cap (both branches)
            existing.append(f"{line_no}: {text.strip()[:self._MAX_LINE_CHARS]}")
        hits = {p: ls for p, ls in hits.items() if ls}   # drop keys left empty by the caps
        # rank files by number of matches (most relevant first)
        return dict(sorted(hits.items(), key=lambda kv: len(kv[1]), reverse=True))

    def _git_log(self, repo: Path) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", "-n", str(self.max_commits)],
            capture_output=True, text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
