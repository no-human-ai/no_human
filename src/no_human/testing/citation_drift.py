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
doctrine: fail-open (`Status.INAPPLICABLE`) only in the two cases this
module can prove carry no citation finding at all — the convention itself
is absent from the target repo (no script, or no checker for it to load),
or the checker failed to import for the one, narrowly-matched reason of
missing the dev-only `pytest` dependency under the interpreter this module
chose (`classify`'s `_CHECKER_IMPORT_FAIL_RE` branch; see `_interpreter`'s
docstring for why that specific case is reachable without any citation
ever being examined, and why it cannot be used to hide a real one). Every
OTHER failure mode this module can observe (a timeout, an `OSError`
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
#: The ONE `FAIL:` shape `scripts/reanchor_citations.py` prints that never
#: names a citation at all: `_load_checker()` raised before `plan()` ever
#: ran (confirmed against the script's own `main()`, which prints this exact
#: literal — not `CHECKER_RELPATH`-interpolated, hardcoded the same way the
#: constant's value is — then `VERDICT=FAIL`, then `return 2`, all before
#: touching `plan()`). See `_interpreter`'s docstring: the single reproduced
#: cause is a `sys.executable` fallback that lacks the dev-only `pytest`
#: `tests/test_readme_claims.py` imports at module scope. Narrowed further
#: to that one reason in `classify` below — this regex alone only isolates
#: the SHAPE (checker never loaded), not the cause.
_CHECKER_IMPORT_FAIL_RE = re.compile(
    r"^FAIL: could not load tests/test_readme_claims\.py: (?P<reason>.*)$",
    re.MULTILINE,
)


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


def _interpreter(repo_path: Path) -> str:
    """The interpreter to run `scripts/reanchor_citations.py` under.

    `scripts/reanchor_citations.py` itself is stdlib-only, but it loads
    `tests/test_readme_claims.py` by path (`_load_checker`), and THAT module
    `import pytest`s at module scope — a dev-only dependency
    (`[dependency-groups] dev` in `pyproject.toml`, not `[project]
    dependencies`) that a plain `pip install no-human` run of this pipeline's
    own `sys.executable` is not guaranteed to have. Running the checker under
    an interpreter that lacks it fails every single time
    (`FAIL: could not load tests/test_readme_claims.py: No module named
    'pytest'`, confirmed by direct reproduction) — not a real citation
    finding, an environment mismatch this module must not mistake for one.

    Same precedent `testing/repro_gate.py`'s `_pytest_python` already
    established for the identical problem: prefer the TARGET REPO's own
    venv (`testing/runner.py`'s `_venv_bin`) — it has the repo's own dev
    dependencies, including pytest — and fall back to `sys.executable` only
    when the repo ships no venv this module can find. Imported lazily to
    avoid a module-level import cycle between `testing.citation_drift` and
    `testing.runner`.
    """
    from .runner import _venv_bin, _IS_WINDOWS

    bin_dir = _venv_bin(repo_path)
    if bin_dir is not None:
        return str(bin_dir / ("python.exe" if _IS_WINDOWS else "python"))
    return sys.executable


def reanchor_command(repo_path: Path, *, apply: bool) -> list[str]:
    """The argv this module runs: the target repo's own venv interpreter
    when it has one (see `_interpreter`), `sys.executable` otherwise —
    never a bare `python`/`python3` off PATH, which does not exist at all
    in a uv/venv project."""
    return [
        _interpreter(repo_path),
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
    `reanchor_citations.py` invocation — `--check` or `--apply`, both
    produce the same `VERDICT=`/`DRIFT:`/`FAIL:`/`applied N` vocabulary this
    parses — into a `CitationOutcome`.

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
        if fails:
            # Self-contradictory shape: the script claims `VERDICT=OK` (rc 0
            # already checked above) yet also printed `FAIL:` lines. In the
            # real script's own `main()` (read directly, not re-derived):
            # `VERDICT=OK` prints in exactly two places — (1) `plan()`
            # returned no drifts AND no unfixable at all, before the mode is
            # even considered, or (2) `--apply` was given, `drifts` was
            # non-empty, `_apply_all` resolved every one of them (its
            # `unresolved` list came back empty), AND `plan()`'s `unfixable`
            # list was empty (the trailing `"FAIL" if unfixable else "OK"`).
            # In `--check` mode there is no path (2): `if not args.apply:
            # print("VERDICT=FAIL"); return 1` fires on ANY drift or
            # unfixable finding, so `--check` prints OK only on a fully
            # clean run. Either way, both routes to OK require `unfixable`
            # empty — so a `FAIL:` line has nowhere legitimate to come from:
            # `main()` has exactly two sources of `FAIL:` output, `plan()`'s
            # `unfixable` list (printed unconditionally up front, whenever
            # non-empty) and `_apply_all`'s own `unresolved` list (printed
            # only in the branch that immediately follows with
            # `VERDICT=FAIL`) — and both of those sources being non-empty is
            # exactly what rules out VERDICT=OK above. "OK" and "FAIL:"
            # never legitimately coexist even though "OK" and `DRIFT:` can.
            # Trusting the "OK" half here would silently drop the finding —
            # CLEAN if there were also no drifts/applied marker, or
            # REANCHORED with an empty `failures` tuple if there were —
            # either way reporting non-blocking while a named citation the
            # script refused to guess at sat right there in the same stdout.
            # Block instead, same as the drifts-without-applied case below.
            docs = tuple(sorted(
                {_doc_path(d) for d, _raw, _reason in fails}
                | {_doc_path(d) for d, _old, _new in drifts}
            ))
            failures = tuple(f"{doc}:{raw}" for doc, raw, _reason in fails)
            return CitationOutcome(
                Status.UNKNOWN, docs=docs, failures=failures,
                detail=f"VERDICT=OK with unresolved FAIL lines: "
                       f"{(stdout + stderr).strip()}")
        if applied:
            docs = tuple(sorted({_doc_path(d) for d, _old, _new in drifts}))
            return CitationOutcome(Status.REANCHORED, docs=docs, detail=stdout.strip())
        if drifts:
            # Self-contradictory shape: the script claims `VERDICT=OK` (rc 0
            # already checked above) yet also printed `DRIFT:` lines for
            # citations it never confirmed applying (no `applied N
            # re-anchor(s)` marker). The real script never emits this combo —
            # a resolved drift always earns its `applied` line before
            # `VERDICT=OK` — so a parser that saw it anyway is looking at
            # output this contract does not define. Trusting the "OK" half
            # would report `Status.CLEAN` ("rewrote nothing", per
            # `CitationOutcome`'s own docstring) while unresolved `DRIFT:`
            # lines sat right there in the same stdout; block instead.
            docs = tuple(sorted({_doc_path(d) for d, _old, _new in drifts}))
            return CitationOutcome(
                Status.UNKNOWN, docs=docs,
                detail=f"VERDICT=OK with unresolved DRIFT lines and no "
                       f"'applied' marker: {stdout.strip()}")
        return CitationOutcome(Status.CLEAN, detail=stdout.strip())

    # verdict == "FAIL"
    if not fails and not drifts:
        import_fail = _CHECKER_IMPORT_FAIL_RE.search(stdout)
        if (import_fail is not None
                and "No module named 'pytest'" in import_fail.group("reason")):
            # The checker (CHECKER_RELPATH) never loaded at all — `plan()`
            # never ran, so there is no citation finding to report, real or
            # missed. Narrowed to this ONE reason on purpose: a coder could
            # in principle break the checker's import some OTHER way to
            # reach the generic shape below, but cannot reach THIS exact
            # substring without either genuinely lacking the dev-only
            # `pytest` dependency (an environment fact, not a citation
            # finding — the target repo's own `.venv` prefers this away
            # already, see `_interpreter`) or deleting `pytest` from that
            # dependency itself — which breaks CHECKER_RELPATH's own
            # collection under a REAL pytest too (it is not merely imported,
            # it IS a pytest test module), so TESTING's own scoped run of it
            # fails independently either way. Falling open here costs
            # nothing a tamper could exploit and stops a fact about THIS
            # process's own interpreter from permanently, falsely blocking
            # every attempt as an unfixable citation.
            return CitationOutcome(
                Status.INAPPLICABLE,
                detail=f"{CHECKER_RELPATH} could not be imported under this "
                       f"interpreter (missing pytest, a dev-only dependency) "
                       f"— not a citation finding: {import_fail.group('reason')}")
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


def run_reanchor(
    repo_path: Path, *, apply: bool = True, timeout: float = DEFAULT_TIMEOUT_S,
) -> CitationOutcome:
    """Run the target repo's OWN `scripts/reanchor_citations.py` inside
    *repo_path* and translate the result via `classify`.

    *apply* defaults to True (the mechanical-fix call every existing caller
    already relies on) but a caller that only wants to know whether drift
    remains — WITHOUT writing to the worktree, e.g. a verification re-check
    after a corrective round has already had its chance to fix things —
    should pass `apply=False` to run the script's own read-only `--check`
    instead. Either way a citation that is fixable-in-principle but was not
    (because this call did not `--apply`) still reports as blocking
    (`Status.UNFIXABLE`, since the script's own `--check` verdict is FAIL
    while drift remains) — never mistaken for clean.

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
            reanchor_command(repo_path, apply=apply),
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
