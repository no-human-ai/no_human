"""Read a TARGET REPO's own citation-drift re-anchoring script, structurally.

A citation is a `file.py:LINE[-LINE]` reference a doc makes into code; any
edit above that line drifts it. A repo that ships the convention this module
consumes — `scripts/reanchor_citations.py` (the mechanical re-anchorer) and
`tests/test_readme_claims.py` (the checker it loads by path, and the source
of truth for what a "citation" even is) — already has a working, read-only
`--check` and a writing `--apply`. This module never re-implements either:
it shells out to the repo's OWN script and translates the result.

Contract with `scripts/reanchor_citations.py` (confirmed against the script
at the time this module was written — never edited by this module):
exit 0 with `VERDICT=OK` on stdout means clean (nothing to do) or fully
applied; exit 1 with `VERDICT=FAIL` means drift and/or an unfixable citation
remains; exit 2 means the checker itself failed to import/parse. Stdout
carries `DRIFT: <doc> \\`old\\` -> \\`new\\` (re-anchoring|would re-anchor)`
per fixable citation, `FAIL: <doc> \\`raw\\` — <reason>` per one the script
will not guess at (occurs zero or more-than-once), and `applied N
re-anchor(s)` when `--apply` actually wrote files.

Deliberately the INVERSE of `structural_budget.py`'s "FAIL-OPEN, ALWAYS"
doctrine: fail-open (`Status.INAPPLICABLE`) ONLY when the convention itself
is absent from the target repo (no script, or no checker for it to load) —
every OTHER failure mode this module can observe (a timeout, an `OSError`
starting the subprocess, a crash inside the script that exits without ever
printing a `VERDICT=` marker) resolves to `Status.UNKNOWN`, which is
BLOCKING. An unreadable file, an erroring subprocess, or a citation the
script would not guess at must never be silently read as "no drift" —
`classify` keys off the `VERDICT=` marker rather than trusting the return
code alone for exactly this reason: an uncaught exception inside the real
script still exits non-zero, but with no marker, and naive `rc != 0`
handling would misclassify that crash as an ordinary, nameable `UNFIXABLE`
instead of the unnameable `UNKNOWN` it actually is.
"""

from __future__ import annotations

import enum
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Relative to the target repo's root — never this pipeline's own.
SCRIPT_RELPATH = "scripts/reanchor_citations.py"
CHECKER_RELPATH = "tests/test_readme_claims.py"

#: Generous on purpose: the script re-parses the checker module and every
#: cited doc on each invocation, and this runs on the pipeline's own clock,
#: never a human's — a slow but honest run must not be mistaken for a hang.
DEFAULT_TIMEOUT_S = 180.0

_VERDICT_RE = re.compile(r"^VERDICT=(OK|FAIL)\s*$", re.MULTILINE)
_DRIFT_RE = re.compile(
    r"^DRIFT: (?P<doc>\S+) `(?P<old>.*?)` -> `(?P<new>.*?)` "
    r"\((?:re-anchoring|would re-anchor)\)\s*$",
    re.MULTILINE,
)
_FAIL_RE = re.compile(
    r"^FAIL: (?P<doc>\S+) `(?P<raw>.*?)` — (?P<reason>.*)$",
    re.MULTILINE,
)
_APPLIED_RE = re.compile(r"^applied (\d+) re-anchor\(s\)\s*$", re.MULTILINE)


class Status(enum.Enum):
    """Every outcome `run_reanchor` can report — exhaustive on purpose so a
    caller's `if`/`elif` chain can never fall through to an implicit "fine"."""

    #: The target repo does not ship this convention at all (no script, or
    #: no checker beside it). Not a finding — there is nothing to check.
    INAPPLICABLE = "inapplicable"
    #: The script ran, produced a `VERDICT=OK`, and rewrote nothing.
    CLEAN = "clean"
    #: The script ran, found drift, and mechanically re-anchored ALL of it —
    #: real bytes are now sitting uncommitted in the target repo's worktree.
    REANCHORED = "reanchored"
    #: The script ran and named at least one citation it will not guess at
    #: (occurs zero or more than once), or whose doc it could not resolve.
    UNFIXABLE = "unfixable"
    #: FAIL CLOSED: the subprocess timed out, could not start, or exited
    #: without ever printing a `VERDICT=` marker (a crash, a kill, output
    #: this parser does not recognize). Blocking, exactly like UNFIXABLE —
    #: "we could not tell" must never read as "nothing is wrong".
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CitationOutcome:
    """The translated result of one `run_reanchor` call.

    `docs` are the target-repo-relative doc paths (`docs/security.md`, ...)
    the run named, fixed or not — a preflight uses these to admit exactly
    those paths into a corrective round's scope, never `docs/` wholesale.
    `failures` are `doc:raw-citation` strings for every citation the script
    itself refused to guess at. `detail` is the script's own stdout/stderr,
    truncated by the caller before it reaches a prompt or an event, never
    reformatted here — the coder should see the script's own words.
    """

    status: Status
    docs: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    detail: str = ""

    @property
    def blocking(self) -> bool:
        """True for the two statuses a preflight must not treat as done:
        something the script named but did not (UNFIXABLE) or could not
        (UNKNOWN) resolve on its own."""
        return self.status in (Status.UNFIXABLE, Status.UNKNOWN)


def convention_present(repo_path: Path) -> bool:
    """True only if *repo_path* ships BOTH halves of the convention this
    module consumes. Either alone is not enough: the script imports the
    checker by path (`_load_checker`, inside the script itself) and cannot
    run without it, and the checker alone has no mechanical re-anchorer.
    Absence of either means "this repo doesn't use this convention" — an
    INAPPLICABLE result, never a finding — so a repo that has never adopted
    it pays nothing for this preflight."""
    return ((repo_path / SCRIPT_RELPATH).is_file()
            and (repo_path / CHECKER_RELPATH).is_file())


def should_run(repo_path: Path) -> bool:
    """Alias kept distinct from `convention_present` so a caller's gate
    reads as an intent ("should this preflight run at all") rather than a
    filesystem fact, the same split `structural_budget.py` draws between
    its own presence checks and the preflight that consumes them."""
    return convention_present(repo_path)


def reanchor_command(repo_path: Path, *, apply: bool) -> list[str]:
    """The argv this module runs, always via `sys.executable` — never a bare
    `python`/`python3` off PATH, so this matches the interpreter the rest of
    the pipeline is already running under, target-repo venv or not."""
    return [
        sys.executable,
        str(repo_path / SCRIPT_RELPATH),
        "--apply" if apply else "--check",
    ]


def _doc_path(doc_key: str) -> str:
    """`security.md` -> `docs/security.md`. The checker's own
    `_CITATION_DOC_PATHS` dict is the actual source of truth for this
    mapping, but that dict lives on the far side of a subprocess boundary
    this module deliberately never crosses (`should_run`'s docstring: this
    module talks to the target repo's script over argv/stdout only, never
    its Python objects) — every doc key the checker currently defines
    (`security.md`, `eval.md`, `KNOWN_ISSUES.md`) resolves under `docs/`,
    so this mirrors that fixed layout rather than importing it."""
    return f"docs/{doc_key}"


def classify(returncode: int, stdout: str, stderr: str) -> CitationOutcome:
    """Pure, exhaustive, FAIL-CLOSED translation of one
    `reanchor_citations.py --apply` invocation into a `CitationOutcome`.

    See the module docstring for why this keys off the `VERDICT=` marker
    rather than `returncode` alone. Every branch below that is not a
    recognized, self-consistent shape returns `Status.UNKNOWN` — this
    function never raises and never guesses.
    """
    verdict_match = _VERDICT_RE.search(stdout)
    fails = [(m.group("doc"), m.group("raw"), m.group("reason"))
             for m in _FAIL_RE.finditer(stdout)]
    drifts = [(m.group("doc"), m.group("old"), m.group("new"))
              for m in _DRIFT_RE.finditer(stdout)]
    applied = _APPLIED_RE.search(stdout) is not None

    if verdict_match is None:
        # No verdict marker: a crash, a kill, or output this parser does
        # not recognize. Never guess which — block.
        detail = (stdout + stderr).strip() or f"no VERDICT marker (rc={returncode})"
        return CitationOutcome(Status.UNKNOWN, detail=detail)

    verdict = verdict_match.group(1)

    if verdict == "OK":
        if returncode != 0:
            # The script claims success but exited non-zero — a
            # self-contradictory shape this parser does not trust either
            # half of. Block rather than believe the happier half.
            return CitationOutcome(
                Status.UNKNOWN,
                detail=f"VERDICT=OK but rc={returncode}: {(stdout + stderr).strip()}")
        if applied:
            docs = tuple(sorted({_doc_path(d) for d, _old, _new in drifts}))
            return CitationOutcome(Status.REANCHORED, docs=docs, detail=stdout.strip())
        return CitationOutcome(Status.CLEAN, detail=stdout.strip())

    # verdict == "FAIL"
    if not fails and not drifts:
        # A FAIL verdict naming nothing recognizable is a shape this parser
        # does not understand — block rather than guess why.
        return CitationOutcome(
            Status.UNKNOWN,
            detail=f"VERDICT=FAIL with no recognizable finding (rc={returncode}): "
                   f"{(stdout + stderr).strip()}")
    doc_paths = tuple(sorted(
        {_doc_path(d) for d, _raw, _reason in fails}
        | {_doc_path(d) for d, _old, _new in drifts}
    ))
    failures = tuple(f"{doc}:{raw}" for doc, raw, _reason in fails)
    return CitationOutcome(
        Status.UNFIXABLE, docs=doc_paths, failures=failures, detail=stdout.strip())


def run_reanchor(repo_path: Path, *, timeout: float = DEFAULT_TIMEOUT_S) -> CitationOutcome:
    """Run the target repo's OWN `scripts/reanchor_citations.py --apply`
    inside *repo_path* and translate the result via `classify`.

    FAIL CLOSED, unconditionally: every branch that is not a clean
    `classify()` call returns `Status.UNKNOWN`. Deliberately no
    `check=True` (a subprocess helper that raises on a non-zero exit would
    just relocate the fail-closed decision into an uncaught exception
    instead of a reported `CitationOutcome`) and no bare `except Exception`
    — only the two failure modes an external-process call can actually
    raise (`TimeoutExpired`, `OSError`) are caught, each becoming its own
    reported, blocking outcome.
    """
    if not convention_present(repo_path):
        return CitationOutcome(Status.INAPPLICABLE, detail="convention not present")
    try:
        proc = subprocess.run(
            reanchor_command(repo_path, apply=True),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CitationOutcome(
            Status.UNKNOWN,
            detail=f"reanchor_citations.py timed out after {timeout}s: {exc}")
    except OSError as exc:
        return CitationOutcome(
            Status.UNKNOWN, detail=f"could not run reanchor_citations.py: {exc}")
    return classify(proc.returncode, proc.stdout, proc.stderr)
