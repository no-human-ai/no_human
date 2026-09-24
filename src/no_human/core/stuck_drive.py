"""`drive_stuck_detector`: the single entry point the orchestrator's
`_agent_sink` feeds every coder-role event through to keep `StuckDetector`
current, plus the small helpers it alone depends on.

Extracted out of `orchestrator.py` (not reimplemented) so
`no_human.eval.event_replay` can feed a RECORDED event stream through the
EXACT same code production runs, instead of a hand-rolled replica that
silently drifts from it — a replica cannot be trusted to reproduce a
hard-abort fire, because the detector's edit-loop and doom-loop tiers are
driven off the raw tool input, `tool_use_id`, `exit_code`, `result_chars`
and edit payloads of each event, not just the tool name.
"""

from __future__ import annotations

import hashlib
import re

from ..agent.backend import AgentEvent
from ..agent.scope_guard import is_agent_owned, is_outside_repo
from .bounds import StuckDetector


def _summarize_tool_sig(tool: str, inp: dict) -> str:
    """Compact tool-call signature for doom-loop detection.

    Produces a short deterministic summary of a tool invocation so
    ``StuckDetector.record_tool_call`` can compare consecutive calls.

    THE SIGNATURE MUST SEPARATE PROGRESS FROM RETRY. On 2026-08-16 two
    healthy attempts were hard-aborted as "doom-loop: identical tool call
    repeated 9x" while progressively window-reading one 2,805-line file:
    the Read signature was the file path ALONE, so reads at offsets
    415/600/1000/... were identical by construction. ~19M weighted tokens
    of correct work died to the false positive. Every branch below now
    carries the parameters that distinguish a NEW action on the same
    target from the SAME action retried:

    * Read/View  - offset+limit ride along; re-reading the same window 9x
      is still a loop, scanning nine windows is not.
    * Edit family - a hash of the change payload; nine DIFFERENT edits to
      one file are work (the separate edit-loop counter still watches
      volume), nine identical ones are a loop.
    * Bash - a hash of the FULL command; long worktree-path prefixes made
      distinct commands collide inside any prefix truncation.
    """
    if tool in ("Read", "View"):
        path = inp.get("file_path") or inp.get("path") or ""
        # `pages` included for the same reason as offset: walking a PDF
        # page-range by page-range is progress, not retry (review follow-up).
        return (f"{path}#o={inp.get('offset', 0)},l={inp.get('limit', 0)}"
                f",p={inp.get('pages', '')}")
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("path") or ""
        payload = "".join(
            str(inp.get(k, "")) for k in
            # replace_all included: a failed unique-match Edit legitimately
            # retried WITH replace_all is a different action, and must not
            # count toward the same-signature streak (review follow-up).
            ("old_string", "new_string", "content", "edits", "new_source",
             "replace_all")
        )
        digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:10]
        return f"{path}#{digest}"
    if tool in ("Grep", "Search"):
        q = inp.get("query") or inp.get("pattern") or ""
        p = inp.get("path") or inp.get("search_path") or ""
        return f"{q[:60]}|{p}"
    if tool in ("Bash", "Terminal"):
        cmd = inp.get("command") or inp.get("cmd") or ""
        digest = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:10]
        return f"{cmd[:80]}#{digest}"
    first = next(iter(inp.values()), "") if inp else ""
    return str(first)[:80]


#: Common test-runner invocations, matched against a Bash tool call's raw
#: command. ONE shared predicate feeds BOTH `ConvergenceTracker.mark_progress`
#: (P2) and `StuckDetector`'s progress-gated hard edit-loop tier
#: (`StuckDetector.note_test_run`, via `_note_test_activity` in
#: `orchestrator.py`) — this used to be two regexes (a stricter one here, a
#: separate broader `_HARNESS_RUN_RE` feeding only the edit tier); that split
#: let a coder whose runner is a harness script read as making no progress to
#: whichever guard still used the narrower set, so it was collapsed into this
#: one. This is the closest cheap proxy the live event stream has for "a test
#: result appeared": `tool_result` events never carry output text
#: (`claude_backend._exit_status`'s docstring — only size, and an exit code
#: on failure, by design), so whether the run PASSED cannot be read from the
#: stream at all. Running one of these commands is itself evidence the
#: attempt is verifying, not just looking around.
#:
#: `node <script>` (task 0ab78498 attempt 1/2: `node /tmp/dcrace/harness.mjs`;
#: f6e626fd attempt 1: `node web/e2e/dead-click-race.mjs`) is a POSITIONAL
#: file argument only — `node\s+(?!-)\S+` requires the token right after
#: `node` to not start with `-`, so `node -e '...'`, `node --require x` and
#: `node --inspect` (utility/flag invocations, not a test/harness run) do
#: NOT match; `node --test` still matches via its own alternative.
#: `npm run <script>` counts ONLY for the test-adjacent script names
#: `test`/`check`/`verify`/`spec` — `npm run build`, `npm run lint`,
#: `npm run deploy` and any other script name are deliberately excluded as
#: non-test executables (this subsumes the old bare `npm\s+(run\s+)?test`;
#: `npm test` without `run` still matches via its own alternative).
#: `npx playwright` is the third harness shape the incidents above used.
#: Not narrowed to project-specific commands otherwise because the
#: convergence signal only needs "some test framework ran", not which one.
_TEST_RUNNER_RE = re.compile(
    r"\b(pytest|py\.test|unittest|npm\s+test|npm\s+run\s+(test|check|verify|spec)|"
    r"yarn\s+test|pnpm\s+test|node\s+--test|node\s+(?!-)\S+|npx\s+playwright|"
    r"go\s+test|cargo\s+test|mvn\s+test|gradle\s+test|rspec|jest|vitest)\b"
)

#: Leading tokens that make a shell segment a read-only SEARCH/INSPECTION,
#: never an execution — a segment starting with one of these never counts as
#: "a test ran" no matter what runner's name appears later in it. Review fix
#: (P2 round 2): a corpus replay found `rg pytest` / `grep -rn "node --test"`
#: — a search FOR the runner's name, not a run of it — as 5.0% of the
#: original (unfiltered) regex's matches.
_READ_ONLY_LEADING_TOKENS = frozenset({
    "grep", "rg", "cat", "ls", "find", "head", "tail", "sed", "awk", "wc",
})
#: `git <subcommand>` is read-only for exactly these subcommands. Every other
#: git subcommand (`checkout`, `stash`, `commit`, ...) is left unclassified —
#: NOT rejected — because this set exists only to exclude known-safe
#: inspection, never to positively assert what git DOES execute.
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({"grep", "log", "show", "diff"})

#: Splits a shell command on its control operators, so a compound command's
#: EXECUTING segment is still judged on its own merits even when an earlier
#: segment is a read-only search — `rg pytest && pytest -q` counts (on its
#: second segment); `rg pytest` alone does not.
_SHELL_SEGMENT_RE = re.compile(r"&&|\|\||[|;]")


def _looks_like_test_run(command: str) -> bool:
    """True when *command* EXECUTES a recognized test runner (P2).

    Matched per shell segment against `_TEST_RUNNER_RE`, skipping any segment
    whose leading token is a read-only search/inspection tool
    (`_READ_ONLY_LEADING_TOKENS`/`_GIT_READ_ONLY_SUBCOMMANDS`) — a grep or a
    `git log` that merely MENTIONS a runner's name by string match is not
    evidence the attempt ran anything.
    """
    if not command:
        return False
    for segment in _SHELL_SEGMENT_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        tokens = segment.split()
        if not tokens:
            continue
        lead = tokens[0]
        if lead in _READ_ONLY_LEADING_TOKENS:
            continue
        if lead == "git" and len(tokens) > 1 and tokens[1] in _GIT_READ_ONLY_SUBCOMMANDS:
            continue
        if _TEST_RUNNER_RE.search(segment):
            return True
    return False


def _test_run_summary(meta: dict | None) -> str:
    """A human-readable, comparable outcome for one test-runner invocation.

    Rendered from `tool_result` meta ALONE — the SDK never delivers output
    text on the wire (`claude_backend._exit_status`'s docstring, by design,
    so a printed credential is never captured): `exit_code` when a FAILED
    result states one, else ``ok``/``failed`` from `is_error`, plus
    `result_chars`. Two test-runner calls with the SAME outcome produce a
    byte-identical string here; `StuckDetector.record_test_outcome` compares
    consecutive strings (via `bounds._status_only`, which strips the chars
    tail) to decide whether an edit-loop is real progress or the SAME
    failure repeated (task f6e626fd).

    Tolerates a missing/garbage `meta` so a degenerate `tool_result` still
    yields a STABLE string rather than raising: `meta=None` reads as `{}`,
    a missing `result_chars` reads as `0`, and a non-int `exit_code` is
    still rendered via the same f-string (str()'d) rather than crashing.
    Undercounts the same way `ConvergenceTracker`'s docstring already
    documents for this seam: an outcome that changes WITHOUT changing its
    length or its error/success status reads as unchanged.
    """
    meta = meta or {}
    is_error = bool(meta.get("is_error", False))
    exit_code = meta.get("exit_code")
    status = (f"exit {exit_code}" if exit_code is not None
              else ("failed" if is_error else "ok"))
    return f"{status}, {meta.get('result_chars', 0)} chars"


def drive_stuck_detector(
    detector: StuckDetector, event: AgentEvent, *, repo_root: str = ""
) -> tuple[list[str], str | None]:
    """The entry point `_agent_sink` feeds every coder-role event through to
    keep `StuckDetector` current — doom-loop/ping-pong (`record_tool_call`/
    `detect_ping_pong`), the edit-loop tiers (`record_edit`, gated by
    `is_agent_owned`/`is_outside_repo` against *repo_root* exactly the way
    `_agent_sink` gates its own `_agent_edited_files` bookkeeping), and the
    test-outcome progress signal (`note_test_run`/`record_test_outcome`) —
    then reads `hard_stuck_reason`, in that order, so the hard check sees
    whatever the record calls above it just wrote for THIS event.

    Extracted (not reimplemented) so `no_human.eval.event_replay` can feed a
    RECORDED event stream through the exact same code production runs,
    instead of a hand-rolled replica that silently drifts from it — see
    that module's docstring for why a replica cannot be trusted to reproduce
    a hard-abort fire. Pure with respect to everything outside *detector*:
    does not touch `ConvergenceTracker` (a separate signal `_agent_sink`
    also feeds from the same event) or `self._agent_edited_files`, and
    never emits or raises — it only reports what fired so the caller
    decides what to do about it.

    Returns ``(advisories, hard)``: *advisories* is the ordered list of
    advisory-tier reason strings that fired for this one event (usually
    empty, occasionally more than one — a doom-loop/ping-pong fire and an
    edit-loop fire are independent and can both land on the same event, so
    this is a list, not a single Optional str); *hard* is the hard-tier
    reason string, or None.
    """
    advisories: list[str] = []
    if event.kind == "tool_use":
        sig = _summarize_tool_sig(event.tool_name or "", event.tool_input or {})
        if detector.record_tool_call(event.tool_name or "", sig):
            advisories.append(
                "doom-loop: identical tool call repeated "
                f"{detector.doom_loop_threshold}×; "
                "will reset context on next attempt"
            )
        elif detector.detect_ping_pong():
            advisories.append(
                "ping-pong: alternating between two actions; "
                "consider a different approach"
            )
    if event.kind == "tool_use" and event.tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        inp = event.tool_input or {}
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
        if path and not (is_agent_owned(path, repo_root) or is_outside_repo(path, repo_root)):
            if detector.record_edit(str(path)):
                advisories.append(
                    f"edit-loop: {path} edited {detector._edit_counts[str(path)]}×; "
                    "consider a different approach"
                )
    if event.kind == "tool_use" and event.tool_name in ("Bash", "Terminal"):
        command = (event.tool_input or {}).get("command") or (
            event.tool_input or {}).get("cmd") or ""
        if _looks_like_test_run(command):
            detector.note_test_run(event.meta.get("tool_use_id"))
    elif event.kind == "tool_result":
        detector.record_test_outcome(
            event.meta.get("tool_use_id"), _test_run_summary(event.meta)
        )
    hard = detector.hard_stuck_reason if event.kind == "tool_use" else None
    return advisories, hard
