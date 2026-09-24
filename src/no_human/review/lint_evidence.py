"""Deterministic lint evidence for the reviewer (SCRUM-64, phase 1: Python/ruff).

The reviewer currently judges a diff cold. This module detects whether the
repo under review has ruff configured (its OWN config — we never impose our
style), runs ruff on the diff's changed Python files ONLY, and returns
structured findings the reviewer context builder can attach as machine-parsed
evidence.

Scoped to the diff's changed LINES, not merely its changed files. A violation
that already existed on a line the agent never touched is not evidence about
the agent's diff — it is noise that grows with file size and can push an
adversarial reviewer toward a false FAIL. When the caller supplies the two
refs, findings are filtered to lines the diff added or modified in the AFTER
state; a newly added file counts as all-lines-changed. If the changed-line map
cannot be computed (no git, unparseable diff, timeout) we fail OPEN to the
previous whole-file behaviour rather than to silence.

Advisory only: any failure — no config, missing binary, timeout, bad output —
returns an empty result. This must never block or slow the review gate.

Whose ruff runs: the `ruff` binary resolved from no_human's OWN PATH/
environment — deliberately never the target repo's venv. The goal is that
nothing this module runs is chosen by the repo under review; that is enforced
vector by vector, not by assertion — see `changed_line_numbers` for the git
flags that pin it and for what remains unproven. If that ruff's version disagrees
with the target repo's pinned ruff (e.g. an unsupported `--output-format` or
a rejected config option), ruff exits 2 and `collect_lint_evidence` treats it
as untrusted output -> empty result, by design.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Hard cap. The review gate has its own multi-minute budget; lint evidence is
# a small advisory add-on and must never be the reason a review stalls.
LINT_TIMEOUT = 30

_RUFF_TABLE_RE = re.compile(r"^\[tool\.ruff(\.[\w.-]+)?\]", re.MULTILINE)


@dataclass
class LintFinding:
    path: str
    line_number: int
    error_code: str
    message: str


def has_ruff_config(repo_path: Path) -> bool:
    """True if the repo configures ruff via ruff.toml/.ruff.toml, or a
    [tool.ruff] table in pyproject.toml. Absence of config means we attach
    nothing — never our own style preferences."""
    try:
        if (repo_path / "ruff.toml").is_file():
            return True
        if (repo_path / ".ruff.toml").is_file():
            return True
        pyproject = repo_path / "pyproject.toml"
        if pyproject.is_file():
            text = pyproject.read_text(encoding="utf-8", errors="ignore")
            if _RUFF_TABLE_RE.search(text):
                return True
    except OSError:
        return False
    return False


# `@@ -a,b +c,d @@` — we only care about the AFTER side. `d` defaults to 1 when
# omitted; `d == 0` is a pure deletion and contributes no after-state lines.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NEW_PATH_RE = re.compile(r"^\+\+\+ (.*)$")

# The changed-line map is a small git read; it must never outlast the lint step.
DIFF_TIMEOUT = 20


def unquote_git_path(path: str) -> str:
    """git wraps a path in double quotes and C-escapes it when it contains
    non-ASCII, backslashes or control characters (`"b/caf\\303\\251.py"`).
    Plain paths — including ones with spaces — are emitted verbatim.

    Public because `diff_coverage._unquote_path` is a second consumer: any
    patch-header path there is quoted under the same rules."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        try:
            return (
                path[1:-1]
                .encode("latin-1", "backslashreplace")
                .decode("unicode_escape")
                .encode("latin-1")
                .decode("utf-8", "replace")
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            return path[1:-1]
    return path


def parse_changed_lines(diff_text: str) -> dict[str, set[int]]:
    """Parse `git diff --unified=0` output into {repo-relative path: after-state
    line numbers added or modified}.

    A path that appears in the diff but gains no after-state lines (a pure
    deletion) maps to an EMPTY set — deliberately present, so a caller can tell
    "this file changed but no line was added" apart from "we learned nothing".

    `+++ ` is only honoured inside a file's header region (after `diff --git`,
    before its first `@@`). An ADDED line whose own content begins with `++ `
    renders as `+++ ...` at column 0 and would otherwise be misread as a header,
    letting the diff's own content redirect the line map to another path.
    """
    out: dict[str, set[int]] = {}
    current: str | None = None
    in_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_header, current = True, None
            continue
        if in_header and line.startswith("+++ "):
            m = _NEW_PATH_RE.match(line)
            # The quoting wraps the WHOLE header token including git's `b/`
            # prefix (`+++ "b/caf\303\251.py"`), so unquote before stripping it.
            raw = unquote_git_path((m.group(1) if m else "").rstrip("\t"))
            if not raw or raw == "/dev/null":
                current = None
                continue
            current = raw[2:] if raw.startswith("b/") else raw
            out.setdefault(current, set())
            continue
        if not line.startswith("@@"):
            continue
        # With --unified=0 every content line carries a +/- prefix, so a bare
        # `@@` at column 0 is always a real hunk header.
        in_header = False
        m = _HUNK_RE.match(line)
        if current is None or not m:
            continue
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        if count <= 0:  # deletion-only hunk: no after-state lines
            continue
        out[current].update(range(start, start + count))
    return out


def changed_line_numbers(
    repo_path: Path,
    before_ref: str,
    after_ref: str,
    *,
    timeout: int = DIFF_TIMEOUT,
) -> dict[str, set[int]]:
    """{repo-relative path: line numbers the diff added or modified, in the
    AFTER state}. Returns {} on ANY failure — advisory, never raises.

    Every flag below neutralises something the repo under review controls. The
    repo owns `.gitattributes` (a tracked file that can arrive in the very diff
    being reviewed) and `.git/config` (writable by the coder agent), so git's
    own defaults are attacker-influenced here:

    * `--no-ext-diff` — ignore a `diff.<driver>.command` external diff program.
    * `--no-textconv` — ignore a `diff.<driver>.textconv` filter. This is NOT
      covered by `--no-ext-diff`; verified by observation — with only
      `--no-ext-diff`, a textconv of `sh -c 'touch PWNED; cat'` selected via
      `*.py diff=evil` in `.gitattributes` still executed.
    * `--src-prefix=a/ --dst-prefix=b/` — pin the header prefixes this parser
      strips. `diff.dstPrefix=dst/` would otherwise make every key `dst/…`, no
      key would match a ruff finding, and ALL evidence would vanish silently.

    This does not amount to a proof that git executes nothing: it pins the two
    execution vectors that are known and reachable here. (`core.fsmonitor` was
    also tested and does not fire for a commit-to-commit diff.)
    """
    try:
        proc = subprocess.run(
            ["git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv",
             "--no-color", "--unified=0", "-M",
             "--src-prefix=a/", "--dst-prefix=b/", f"{before_ref}..{after_ref}"],
            cwd=repo_path, capture_output=True, text=True,
            errors="replace", timeout=timeout,
        )
        if proc.returncode != 0:
            log.warning("git diff for changed lines exited %d: %s",
                        proc.returncode, (proc.stderr or "")[:300])
            return {}
        return parse_changed_lines(proc.stdout or "")
    except subprocess.TimeoutExpired:
        log.warning("git diff for changed lines timed out after %ds", timeout)
        return {}
    except Exception:  # noqa: BLE001 — advisory evidence, never blocks the review
        log.warning("changed-line map failed", exc_info=True)
        return {}


def _repo_relative(raw: str, repo_path: Path) -> str:
    """ruff's JSON `filename` is ABSOLUTE and symlink-resolved even when it was
    handed a relative path (verified against ruff 0.14.0). Left as-is it both
    defeats the changed-line filter — whose keys are git's repo-relative paths
    — and leaks the operator's home directory into the reviewer prompt.

    Returns the path unchanged if it cannot be placed inside the repo, so an
    unexpected shape degrades to today's rendering rather than vanishing.
    """
    if not raw:
        return raw
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_path / candidate
        root = Path(os.path.realpath(str(repo_path)))
        resolved = Path(os.path.realpath(str(candidate)))
        return resolved.relative_to(root).as_posix()
    except (ValueError, OSError):
        return raw


def _changed_python_files(repo_path: Path, changed_files: list[str]) -> list[str]:
    """Changed `.py` paths that actually exist inside `repo_path`. Never scans
    the repo — only ever considers paths the caller says are in the diff, and
    drops anything that would resolve outside the repo."""
    resolved_root = repo_path.resolve()
    out: list[str] = []
    for rel in changed_files:
        if not rel.endswith(".py"):
            continue
        try:
            resolved = (repo_path / rel).resolve()
            if not resolved.is_relative_to(resolved_root):
                continue
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            out.append(rel)
    return out


def _filter_to_changed_lines(
    findings: list[LintFinding], changed_lines: dict[str, set[int]],
) -> list[LintFinding]:
    """Drop findings on lines the diff did not touch.

    Three distinct cases, all verified by observation:

    * The map is EMPTY — we learned nothing about which lines changed (no git,
      a bad ref, a timeout). Fail OPEN: every finding is kept, which is the
      whole-file behaviour this scoping replaced.
    * The map has an entry for the path whose set is EMPTY — the file appeared
      in the diff with a header but gained no after-state lines, i.e. lines
      were deleted from a file that still exists. Its remaining violations are
      pre-existing, so they are dropped.
    * The map is non-empty but has NO entry for the path — a pure rename or a
      mode-only change emits no `+++` header at all, so no key is created.
      These are dropped too, by the `.get(path, ())` default.

    Note the asymmetry: "no entry" only fails open when it makes the WHOLE map
    empty, i.e. when the diff contains nothing but such changes. In a mixed
    diff another file supplies the entries, and a rename-only path is dropped.
    """
    if not changed_lines:
        return findings
    return [
        f for f in findings
        if f.line_number in changed_lines.get(f.path, ())
    ]


def collect_lint_evidence(
    repo_path: Path,
    changed_files: list[str],
    *,
    before_ref: str | None = None,
    after_ref: str | None = None,
    timeout: int = LINT_TIMEOUT,
) -> list[LintFinding]:
    """Run ruff on the diff's changed Python files, if the repo configures
    ruff. Returns [] on: no config, no changed .py files, a missing/failing
    ruff binary, a timeout, or unparseable output — this is advisory evidence
    and must never raise into the review gate.

    When both `before_ref` and `after_ref` are given, findings are further
    scoped to the lines that diff touched; without them the whole changed file
    is reported, as before.
    """
    try:
        if not has_ruff_config(repo_path):
            return []
        files = _changed_python_files(repo_path, changed_files)
        if not files:
            return []
        proc = subprocess.run(
            ["ruff", "check", "--output-format=json", "--no-fix", *files],
            cwd=repo_path, capture_output=True, text=True, timeout=timeout,
        )
        # ruff exits 1 when it finds violations — that is a normal result, not
        # a failure. Anything else (2 = usage/config error, missing binary
        # raising below) means we cannot trust the output.
        if proc.returncode not in (0, 1):
            log.warning("ruff exited %d: %s", proc.returncode, (proc.stderr or "")[:500])
            return []
        data = json.loads(proc.stdout or "[]")
        if not isinstance(data, list):
            return []
        findings: list[LintFinding] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            loc = item.get("location") or {}
            findings.append(LintFinding(
                path=_repo_relative(str(item.get("filename") or ""), repo_path),
                line_number=int(loc.get("row") or 0),
                error_code=str(item.get("code") or ""),
                message=str(item.get("message") or ""),
            ))
        if before_ref is not None and after_ref is not None:
            findings = _filter_to_changed_lines(
                findings, changed_line_numbers(repo_path, before_ref, after_ref),
            )
        # Determinism is enforced here, not inherited from ruff's (unordered
        # across files) output order.
        findings.sort(key=lambda f: (f.path, f.line_number, f.error_code))
        return findings
    except subprocess.TimeoutExpired:
        log.warning("ruff lint evidence timed out after %ds", timeout)
        return []
    except Exception:  # noqa: BLE001 — advisory evidence, never blocks the review
        log.warning("ruff lint evidence failed", exc_info=True)
        return []


# Hard caps on the rendered block. A changed file with thousands of
# violations (generated/vendored code, an aggressive `select = ["ALL"]`)
# must never blow the adversarial reviewer's context — this is advisory
# evidence, not the diff itself.
MAX_LINT_FINDINGS = 50
MAX_LINT_BYTES = 8192


def format_lint_evidence(findings: list[LintFinding]) -> str:
    """Render findings as a labeled block for the reviewer prompt.

    Empty string when there is nothing to show — callers must omit the
    section entirely rather than attach an empty, header-only block.

    Capped at MAX_LINT_FINDINGS findings and MAX_LINT_BYTES bytes, whichever
    is hit first, with a trailing '... truncated' line noting how many
    findings were dropped.
    """
    if not findings:
        return ""
    # The scope is stated so the reviewer does not read "no finding for foo.py"
    # as "foo.py is clean" — untouched lines were never in scope.
    header = (
        "Evidence: ruff (deterministic static analysis, scoped to the lines "
        "this diff changed)"
    )
    lines = [header]
    shown = 0
    size = len(header)
    for f in findings:
        if shown >= MAX_LINT_FINDINGS:
            break
        loc = f"{f.path}:{f.line_number}" if f.line_number else f.path
        code = f" {f.error_code}" if f.error_code else ""
        line = f"  {loc}{code} {f.message}".rstrip()
        if size + 1 + len(line) > MAX_LINT_BYTES:
            break
        lines.append(line)
        size += 1 + len(line)
        shown += 1
    remaining = len(findings) - shown
    if remaining > 0:
        lines.append(f"  ... truncated ({remaining} more findings)")
    return "\n".join(lines)
