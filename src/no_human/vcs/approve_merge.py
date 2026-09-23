"""`nh approve` merges the PR: local squash as the operator identity.

Operator directive 2026-08-12 (refile of 74bf7dec): approving a completed
task must MERGE its PR, not just record approval. This is the one sanctioned
place the product performs a real merge — done under the OPERATOR's git
identity (``git.approve_identity``), never the agent's, and only in response
to an explicit human `nh approve` / API call. The agent itself still never
merges anything (constraint #2 is unchanged).

The eight-step procedure, proven by hand before this module existed:

  1. preconditions   — config enabled, a PR exists, `gh` is on PATH, the
                        branch resolves, and the squash subject the task
                        title would produce (after redaction) is Conventional
                        Commits v1.0.0 compliant — refused, never
                        auto-rewritten, pointing at `nh task retitle`.
                        (Review-PASS-for-head-sha is a CALLER precondition —
                        see `cli/commands.py`'s `approve` — because it needs
                        `Orchestrator._rounds_for_head`, which this
                        lower-level vcs module must not import.)
  2. fetch + worktree — fetch the remote, resolve the CURRENT default-branch
                        tip, and create a detached temp worktree there.
  3. squash           — `git merge --squash <branch>` into the worktree. A
                        conflict confined to RELEASE_MANIFEST.txt takes the
                        tip's copy and continues — step 4 re-derives that
                        file wholesale anyway, on EITHER backend (see below)
                        — because RELEASE_MANIFEST.txt moves under every
                        landing, so it conflicts against every PR cut before
                        the previous one regardless of what that PR touches
                        (the O(N) landing-conflicts-every-open-PR pattern).
                        Any other conflict refuses here — a coder round is
                        the only safe resolver for a hand-authored path.
  4. manifest         — the merge-result ledger rule, on whichever backend
                        this repo carries: an export-gated repo resets ONLY
                        RELEASE_MANIFEST.txt to the tip's version (the
                        branch's EXPORT_CLASSIFICATION.txt, with its own
                        count bumps, is left exactly as the squash produced
                        it), then `export_guard.py approve` the branch's
                        changed ship-classified files so the manifest is
                        re-derived from tip + those pins, and stages it. A
                        repo without the guard (this one's own shape) instead
                        runs `check_release_manifest.py --write`, which
                        rebuilds every pin from the squashed tree wholesale
                        — no per-path approve/prune machinery needed — and
                        stages it the same way.
  5. commit           — one commit, `-c user.name=<identity> -c
                        user.email=<identity>`, message = task title + task
                        id + review-evidence line. The identity is
                        `git.approve_identity` if set, else git's own
                        resolved `user.name`/`user.email` for the repo
                        (repo-local overriding global); if neither resolves,
                        the run refuses at the `preconditions` step.
  6. verify + tests   — `export_guard.py verify` (or, on the guard-less
                        backend, `check_release_manifest.py --strict` — the
                        same check the `inventory` CI job runs), then the
                        merge-time test
                        gate, both run IN the worktree. The gate is
                        FOCUSED (the task's change-scoped tests) when the
                        squash result's tree matches the tree the attempt's
                        recorded full suite ran on; it is the FULL suite
                        when that tree diverged (a conflict-round or
                        supervisor commit landed on the branch, or the base
                        moved) or the tested tree is unknown/unresolvable —
                        a conflict round changes the tree, and the attempt's
                        full-suite evidence is only ever attached to the
                        tree it actually ran on.
  7. push             — re-check the tip has not moved, then push the landed
                        commit straight onto the remote's default branch ref
                        (a non-force push, so a raced tip is refused by git
                        itself as well as by the re-check); verify the
                        remote ref actually advanced.
  8. close_pr         — close the PR without a comment (a comment re-wakes
                        the watcher — ticket b1fd13ca); idempotent on an
                        already-closed/merged PR; a failure here is a
                        non-fatal warning — the code is already on the
                        default branch.

WHY ONLY RELEASE_MANIFEST.txt GETS THIS TREATMENT
--------------------------------------------------
The 2026-09-14 incident (task 4135165f, PR #356) also showed conflicts in
docs/security.md and tests/test_readme_claims.py on the SAME landing. Those
two are deliberately NOT given the "take tip's copy, then re-derive" step-3
tolerance RELEASE_MANIFEST.txt gets here:

  * RELEASE_MANIFEST.txt is a pure PROJECTION of the tree — every line is a
    `<sha256> <path>` pair a tool (`export_guard.py` / `check_release_
    manifest.py --write`) recomputes from the tree's actual bytes. A branch's
    copy of it carries no information that is not ALSO recoverable from that
    branch's real file changes, so discarding the branch's copy of the ledger
    and rebuilding it from tip + the branch's other changes loses nothing.
  * docs/security.md and tests/test_readme_claims.py are HAND-AUTHORED. Their
    content on a branch IS the information — a security-policy edit or a new
    README-claims check has no other representation to re-derive it from.
    Auto-resolving a conflict there by taking the tip's copy would silently
    drop the branch's own edit forever, which is exactly the kind of quiet
    data loss constraint #2 and this module's whole "never weaken a check to
    make merging easier" rule forbid. Those two conflicts are correctly left
    for a coder round to resolve by hand, same as any other hand-authored
    path (step 3, above) — the O(N) manifest problem does not generalize to
    them because their conflicts are REAL (two branches' prose disagreeing),
    not an artifact of a generated ledger moving under every landing.

WHAT THIS DOES NOT CLOSE
-------------------------
Two narrowing notes, so this fix is not read as broader than it is:

  * The step-3 tolerance only fires when RELEASE_MANIFEST.txt is the ONLY
    unmerged path (`unmerged == {"RELEASE_MANIFEST.txt"}`, above) — a
    conflict that touches the ledger AND a hand-authored file still refuses
    here, same as before, and still needs a coder round for the
    hand-authored side. Of the three PRs open against this incident's own
    tree when this was written (#319, #313, #302), NONE is manifest-only —
    each also conflicts on other files — so this change does not, by
    itself, make any of the three land without a coder round; it only
    removes RELEASE_MANIFEST.txt from what that round has to resolve, and it
    fully closes the case where the ledger is the SOLE collision (proven by
    `test_two_independent_prs_from_the_same_base_both_land_without_manual_
    conflict_resolution`, tests/test_approve_merge.py).
  * This tolerance lives in `land_task`, the `nh approve` code path, only. A
    human running `git merge --squash` by hand, or GitHub's own "Squash and
    merge" button on a PR, still hits the raw multi-way conflict in
    RELEASE_MANIFEST.txt with no help from this module — neither of those
    paths calls into `_land_in_worktree`. Landing through `nh approve` is
    how this repo's own PRs land (see this module's own opening line), so
    that is the path this fix targets; it was not widened to cover the
    other two.

Every step failure removes the temp worktree and returns a
:class:`LandResult` naming the failing `step` and the captured `stderr` —
the caller leaves the task `awaiting_approval` and surfaces both verbatim.
Nothing here touches the task store; that is the caller's job (mirrors
`manifest_repair.py`'s shape).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..agent.session_mark import current_mark
from ..proc import _VENV_INTERPRETERS, real_python
from .git import GitError, GitRepo, ProtectedBranch
from .pr_watcher import parse_pr_url

STEPS = (
    "preconditions", "fetch", "worktree", "squash", "manifest",
    "commit", "verify", "tests", "push", "close_pr",
)

_STDERR_CAP = 4000
_DEFAULT_TEST_TIMEOUT_S = 1800
_DEFAULT_FULL_TEST_TIMEOUT_S = 5400
_APPROVE_TIMEOUT_S = 120
_VERIFY_TIMEOUT_S = 120
_GH_TIMEOUT_S = 30

@dataclass(frozen=True)
class LandResult:
    """The outcome of one `land_task` run.

    ``step`` is always one of :data:`STEPS` — the step that failed (``ok``
    is False), or the last step that ran (``ok`` is True; normally
    ``"close_pr"``). ``skipped`` marks the today's-behaviour record-only
    path (``approve_merge.enabled`` false, no PR, or no `gh`) — that is
    still ``ok=True``, never a failure.
    """

    ok: bool
    step: str = ""
    landed_sha: str = ""
    pr_url: str = ""
    branch: str = ""
    message: str = ""
    stderr: str = ""
    skipped: bool = False
    #: Non-empty when the land step rewrote a win-count in
    #: EXPORT_CLASSIFICATION.txt by merge arithmetic — the human who approved
    #: must be able to see that a hand-maintained file was touched.
    reconciled: str = ""
    #: Which merge-time test gate ran — "focused" (change-scoped tests) or
    #: "full" (the whole suite, because the squash tree diverged from the
    #: tested attempt's tree, or that tree is unknown). "" when the tests
    #: step never ran (an earlier step failed first).
    gate: str = ""
    #: Human-readable reason behind ``gate`` — also folded into ``message``
    #: on success and into ``stderr`` on a tests-step failure.
    gate_reason: str = ""


def _cap(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _STDERR_CAP:
        return text
    return text[:_STDERR_CAP] + "\n…(truncated)"


def _sh(args: list[str], *, cwd: Path | str, timeout: float | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    # `encoding` AND `errors`, not one or the other. With `text=True` alone
    # the decode uses the LOCALE codec, and `git commit` echoes the subject
    # back through this helper. Concretely: the Hebrew letter lamed is UTF-8
    # `D7 9C`, and `0x9C` is undefined in cp1255, so it raises. (Not every
    # non-Latin script does — `"привет"` survives cp1251 as mojibake; `"Иван"`
    # raises on `0x98`. The failure is per-character, which is why it looks
    # intermittent.)
    #
    # On Windows `Popen._readerthread` reads each pipe in a THREAD, so that
    # UnicodeDecodeError kills the THREAD rather than reaching the caller:
    # `_communicate` joins an already-dead thread without raising, the buffer
    # stays empty, and its tail — `stdout[0] if stdout else None` — yields
    # None. The caller then gets None where it expects a string.
    #
    # `errors="replace"` over strict is deliberate and safe HERE specifically:
    # every decision this file makes on `_sh` output either compares a sha, a
    # tree hash or a returncode (`_git_config_value`, `_tree_of`, `land_task`,
    # `_land_in_worktree`), parses diagnostic text that is already tolerant of
    # a malformed body (`_declared_counts`, `reconcile_merge_count_drift`,
    # `reconcile_commit_count_drift`, `_close_pr`'s `json.loads` under
    # `try/except`), or splits PATHS that are handed straight back to git
    # (`_ship_classified_paths`, `_unmerged_paths`, `_land_regenerate_manifest`'s
    # `git add -- <path>`) — where a replacement character can only make the
    # `git add` FAIL and the land refuse. So a replacement character can only
    # push the decision toward the conservative branch — run the full suite,
    # or refuse to land. It is never the difference between refusing and
    # wrongly accepting.
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env=env, encoding="utf-8", errors="replace",
    )


def _git_config_value(repo_path: Path | str, key: str) -> str:
    """Best-effort `git config --get <key>` for *repo_path* — repo-local
    overriding global overriding system, exactly as `git config --get`
    already resolves it. Returns "" on any failure (key unset, empty value,
    unparseable config, multivar, or a timeout) — never raises."""
    try:
        proc = _sh(["git", "config", "--get", key], cwd=repo_path,
                    timeout=_APPROVE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _resolve_approve_identity(git_cfg: dict, repo_path: Path | str) -> tuple[str, str, str]:
    """Return ``(name, email, error)`` for the identity `nh approve`'s squash
    commit is attributed to. Precedence, highest first: explicit
    ``git.approve_identity.{name,email}``; then the flat
    ``git.merge_identity_name``/``git.merge_identity_email`` aliases; then
    git's own resolved identity for *repo_path* (`git config --get`, which
    already applies repo-local -> global -> system precedence). NEVER falls
    back to the agent identity — this function never reads
    `agent_identity_name`/`agent_identity_email`. If either field is still
    empty after all three sources, returns an explicit error and no
    identity; the caller must refuse rather than guess."""
    ident = git_cfg.get("approve_identity") or {}
    name = (ident.get("name") or "").strip()
    email = (ident.get("email") or "").strip()
    if not name:
        name = (git_cfg.get("merge_identity_name") or "").strip()
    if not email:
        email = (git_cfg.get("merge_identity_email") or "").strip()
    if not name:
        name = _git_config_value(repo_path, "user.name")
    if not email:
        email = _git_config_value(repo_path, "user.email")
    if not name or not email:
        return "", "", (
            "cannot determine the identity to attribute this merge to: git "
            f"user.name/user.email are unset for {repo_path}. Run `git config "
            'user.email "you@example.com"` (and user.name), or set '
            "git.approve_identity.name/.email in your config file. This "
            "refuses rather than guessing, and will never land a human "
            "merge under the agent identity.")
    return name, email, ""


def _step(on_step: Callable[[str], None] | None, name: str) -> None:
    """Invoke *on_step* for a step boundary — never lets a progress callback
    (a WS broadcast, in production) turn a land failure into a callback
    failure. Best-effort only; the land procedure's own return value is the
    single source of truth for what happened."""
    if on_step is None:
        return
    try:
        on_step(name)
    except Exception:  # noqa: BLE001 — progress reporting must never fail a land
        pass


def _load_export_builder(root: Path):
    """Load `scripts/build_public_export.py` by path, same as `export_guard.py`
    does — best-effort: returns None on any error, since this is only used to
    narrow the file list handed to `approve` (never a decision the gate
    itself is trusted to make)."""
    path = root / "scripts" / "build_public_export.py"
    if not path.exists():
        return None
    try:
        # The `sys.modules` key is uniquified per resolved root: two
        # different fixture repos loading this in the same xdist worker
        # would otherwise bind different files to one fixed key.
        root_hash = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:8]
        module_name = f"_nh_approve_merge_build_public_export_{root_hash}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        # `build_public_export.py` uses `@dataclass`, and dataclasses resolves
        # forward-ref types via `sys.modules[cls.__module__]` — the module MUST
        # be registered before `exec_module` runs the class body, or that
        # lookup finds nothing and raises (matches `export_guard.py`'s own
        # `_load_builder`, which registers for the same reason).
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — advisory narrowing only
        return None


CLASSIFICATION_NAME = "EXPORT_CLASSIFICATION.txt"

#: The guard names a drifted rule like ``EXPORT_CLASSIFICATION.txt:248: `ship
#: 299  tests/*.py` actually wins 300 file(s).`` (build_public_export.
#: classification_errors; approve prints it on stderr, rc 2).
COUNT_DRIFT_RE = re.compile(
    r"`(?P<verb>ship|drop)\s+(?P<declared>\d+)\s+(?P<pattern>\S+)` actually wins "
    r"(?P<real>\d+) file")
_RULE_LINE_RE = re.compile(
    r"^(?P<verb>ship|drop)(?P<sp1>\s+)(?P<count>\d+)(?P<sp2>\s+)(?P<pattern>\S+)\s*$")


def _declared_counts(worktree_path: Path, sha: str) -> tuple[dict[tuple[str, str], int] | None, set]:
    """(``{(verb, pattern): declared}`` at *sha*, keys that appear MORE THAN
    ONCE there). None when the file is absent at *sha*. A duplicated
    ``(verb, pattern)`` is legal in the grammar (the shadowed line declares 0),
    so it is reported rather than collapsed last-wins."""
    show = _sh(["git", "show", f"{sha}:{CLASSIFICATION_NAME}"], cwd=worktree_path)
    if show.returncode != 0:
        return None, set()
    out: dict[tuple[str, str], int] = {}
    dups: set = set()
    for line in show.stdout.splitlines():
        m = _RULE_LINE_RE.match(line.strip())
        if not m:
            continue
        key = (m["verb"], m["pattern"])
        if key in out:
            dups.add(key)
        out[key] = int(m["count"])
    return out, dups


def _rewrite_declared_count(lines: list[str], verb: str, pattern: str,
                            declared: int, real: int) -> int:
    """Rewrite ONE classification line's declared count to *real* in place,
    preserving column alignment (counts are right-aligned, so a width change
    is absorbed into the leading pad when there is room). Shared by every
    reconciler in this module — this is the only place a count is ever
    written, mechanical or not.

    Returns the number of lines matched. The caller must treat anything
    other than 1 as a refusal: 0 means the file does not say what the
    refusal claims (nothing to rewrite — guessing is exactly what is
    forbidden), and >1 means the pattern is ambiguous.
    """
    hits = 0
    for i, line in enumerate(lines):
        lm = _RULE_LINE_RE.match(line.strip())
        if lm and (lm["verb"], lm["pattern"]) == (verb, pattern) and int(lm["count"]) == declared:
            sp1 = lm["sp1"]
            delta = len(str(real)) - len(str(declared))
            if 0 < delta < len(sp1):
                sp1 = sp1[delta:]
            elif delta < 0:
                sp1 = sp1 + " " * (-delta)
            lines[i] = line.replace(f"{lm['verb']}{lm['sp1']}{declared}{lm['sp2']}",
                                    f"{lm['verb']}{sp1}{real}{lm['sp2']}", 1)
            hits += 1
    return hits


def _write_classification_lines(worktree_path: Path, lines: list[str]) -> tuple[bool, str]:
    """Persist the rewritten classification lines and stage the file —
    shared by every reconciler in this module so a rewritten count is always
    staged the same way. Returns ``(True, "")`` on success, or ``(False,
    <stderr>)`` if `git add` itself fails."""
    path = worktree_path / CLASSIFICATION_NAME
    path.write_text("".join(lines), encoding="utf-8")
    add = _sh(["git", "add", "--", CLASSIFICATION_NAME], cwd=worktree_path)
    if add.returncode != 0:
        return False, _cap(add.stderr)
    return True, ""


def reconcile_merge_count_drift(worktree_path: Path, base_ref: str, branch_ref: str,
                                refusal_text: str) -> tuple[bool, str]:
    """Repair a win-COUNT that is stale ONLY because two reviewed counts met.

    `EXPORT_CLASSIFICATION.txt` is not a derived artefact: its counts are
    hand-maintained and a textual conflict in it is a ship/drop decision no
    mechanical step may make. This is the one case that is NOT a decision.
    The file merged cleanly, base and branch each bumped a rule's count for
    files each added — both commits passed the count gate — and the merged
    tree carries both sets of files, so the only correct count is

        base_declared + (branch_declared - merge_base_declared)

    The guard's own refusal names the drift; the three declared values are
    read from git; the number is rewritten ONLY under exactly that equality
    (and only when the rule is present, once, on every side); anything else
    is refused with the arithmetic shown. Used by both merge sites — the
    derived-conflict resolver (a PR that went CONFLICTING) and `nh approve`'s
    land step (a squash onto a moved tip) — which hit the identical refusal.
    INCIDENT 2026-08-20, task c309a6a3 / PR #511: 298→299 on both sides, real
    300, `approve` refused, the mechanical round failed at 'regenerate', a
    review-PASSED task escalated to a human who did this arithmetic by hand;
    twice.
    """
    # ``base_ref``/``branch_ref`` are commit-ish (a sha, or a ref name such as
    # ``origin/<branch>`` — the land step passes `resolve_commitish`'s result);
    # git resolves them, and the same ref the merge used is what gets read.
    drifts = list(COUNT_DRIFT_RE.finditer(refusal_text))
    if not drifts:
        return False, "no count drift in the refusal"
    mb = _sh(["git", "merge-base", base_ref, branch_ref], cwd=worktree_path)
    if mb.returncode != 0 or not mb.stdout.strip():
        return False, "no merge base between base and branch"
    base, bd = _declared_counts(worktree_path, base_ref)
    branch, rd = _declared_counts(worktree_path, branch_ref)
    anc, ad = _declared_counts(worktree_path, mb.stdout.strip())
    if base is None or branch is None or anc is None:
        return False, f"{CLASSIFICATION_NAME} missing on one side of the merge"
    path = worktree_path / CLASSIFICATION_NAME
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    notes: list[str] = []
    for m in drifts:
        key = (m["verb"], m["pattern"])
        declared, real = int(m["declared"]), int(m["real"])
        if key in bd or key in rd or key in ad:
            return False, (f"rule `{m['verb']} {m['pattern']}` appears more than once on "
                           "a side of the merge — ambiguous, a hand decision")
        if key not in base or key not in branch or key not in anc:
            return False, (f"rule `{m['verb']} {m['pattern']}` is not present on every "
                           "side of the merge — a hand decision, not merge arithmetic")
        expected = base[key] + (branch[key] - anc[key])
        if real != expected:
            return False, (f"rule `{m['verb']} {m['pattern']}`: real {real} != base {base[key]} "
                           f"+ (branch {branch[key]} - merge-base {anc[key]}) = {expected} — "
                           "not a mechanical merge of two reviewed counts")
        hits = _rewrite_declared_count(lines, m["verb"], m["pattern"], declared, real)
        if hits != 1:
            return False, f"rule `{m['verb']} {m['pattern']}` matched {hits} line(s), expected 1"
        notes.append(f"{m['verb']} {m['pattern']}: {declared} -> {real} "
                     f"(base {base[key]} + branch {branch[key]} - merge-base {anc[key]})")
    ok, err = _write_classification_lines(worktree_path, lines)
    if not ok:
        return False, err
    return True, "; ".join(notes)


def reconcile_commit_count_drift(worktree_path: Path, base_ref: str,
                                 refusal_text: str) -> tuple[bool, str]:
    """Repair a win-COUNT that is stale ONLY because THIS ATTEMPT's own diff
    added or removed files a declared rule wins.

    Sibling of `reconcile_merge_count_drift` for the commit-time case: there
    is no merge here — one attempt, one base, one worktree — so the sound
    condition is not base+branch-ancestor arithmetic but

        real - declared == (files this attempt's diff ADDED under the rule)
                          - (files this attempt's diff REMOVED under the rule)

    computed from ``git diff --no-renames --name-status <base_ref>`` against
    the worktree. Each changed path is resolved through
    `build_public_export.classify`'s LAST-matching-rule winner (`cls.wins`)
    — never a bare `rule.matcher()` check — because a later, more specific
    `drop` rule for one file can override an earlier broad `ship` glob for
    that same file (EXPORT_CLASSIFICATION.txt carries exactly this shape:
    `ship tests/*.py` followed by individual `drop tests/test_*.py`
    carve-outs), and only the winning rule may have its count moved.

    A modified (not added/removed) file can never move a win-COUNT — the
    file still wins (or still doesn't win) whatever rule it did before — so
    the diff's ``M`` rows are ignored.

    Shares `COUNT_DRIFT_RE` (the refusal parser), `_rewrite_declared_count`
    (the writer) and `_write_classification_lines` (the persist/stage step)
    with `reconcile_merge_count_drift` — the same refusal grammar, the same
    in-place rewrite, the same staging, for a different arithmetic. Anything
    the attempt's own diff does not explain still refuses, with the
    arithmetic and the unexplained drift shown, exactly like the merge case.
    """
    drifts = list(COUNT_DRIFT_RE.finditer(refusal_text))
    if not drifts:
        return False, "no count drift in the refusal"

    diff = _sh(["git", "diff", "--no-renames", "--name-status", base_ref],
              cwd=worktree_path)
    if diff.returncode != 0:
        return False, f"could not diff against {base_ref}: {_cap(diff.stderr)}"

    added_paths: list[str] = []
    removed_paths: list[str] = []
    for line in diff.stdout.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("A") and len(parts) >= 2:
            added_paths.append(parts[-1])
        elif status.startswith("D") and len(parts) >= 2:
            removed_paths.append(parts[-1])
        # M (modified) and anything else never moves a win-COUNT: the file
        # already won (or didn't win) whatever rule it wins today.

    builder = _load_export_builder(worktree_path)
    if builder is None:
        return False, f"could not load {CLASSIFICATION_NAME}'s rule engine"
    cls_path = worktree_path / CLASSIFICATION_NAME
    if not cls_path.exists():
        return False, f"{CLASSIFICATION_NAME} missing"
    try:
        rules = builder.parse_classification(cls_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — surfaced as a refusal, not a crash
        return False, f"could not parse {CLASSIFICATION_NAME}: {exc}"

    rules_by_key: dict[tuple[str, str], object] = {}
    dupes: set = set()
    for rule in rules:
        key = (rule.verb, rule.pattern)
        if key in rules_by_key:
            dupes.add(key)
        rules_by_key[key] = rule

    def _winner_lineno(path: str) -> int | None:
        # `classify` is the ONE win-count source in this module (`cls.wins`)
        # — reused per-path here instead of a second glob matcher, so a
        # later carve-out rule always overrides an earlier broad one exactly
        # as the real gate would score the whole tree.
        c = builder.classify(rules, [path])
        for lineno, count in c.wins.items():
            if count:
                return lineno
        return None

    added_winner = {p: _winner_lineno(p) for p in added_paths}
    removed_winner = {p: _winner_lineno(p) for p in removed_paths}

    lines = cls_path.read_text(encoding="utf-8").splitlines(keepends=True)
    notes: list[str] = []
    for m in drifts:
        key = (m["verb"], m["pattern"])
        declared, real = int(m["declared"]), int(m["real"])
        if key in dupes:
            return False, (f"rule `{m['verb']} {m['pattern']}` appears more than "
                           "once — ambiguous, a hand decision")
        rule = rules_by_key.get(key)
        if rule is None:
            return False, (f"rule `{m['verb']} {m['pattern']}` is not present in "
                           f"{CLASSIFICATION_NAME} — a hand decision, not diff arithmetic")
        added_matching = sorted(p for p, ln in added_winner.items() if ln == rule.lineno)
        removed_matching = sorted(p for p, ln in removed_winner.items() if ln == rule.lineno)
        expected = declared + len(added_matching) - len(removed_matching)
        if real != expected:
            unexplained = real - expected
            return False, (
                f"rule `{m['verb']} {m['pattern']}`: real {real} != declared {declared} "
                f"+ (added {len(added_matching)} - removed {len(removed_matching)} in this "
                f"attempt's diff) = {expected} — {abs(unexplained)} file(s) of drift not "
                "explained by this attempt's own changes"
            )
        hits = _rewrite_declared_count(lines, m["verb"], m["pattern"], declared, real)
        if hits != 1:
            return False, f"rule `{m['verb']} {m['pattern']}` matched {hits} line(s), expected 1"
        notes.append(
            f"{m['verb']} {m['pattern']}: {declared} -> {real} (added "
            f"{len(added_matching)} - removed {len(removed_matching)} in this attempt's diff)"
        )
    ok, err = _write_classification_lines(worktree_path, lines)
    if not ok:
        return False, err
    return True, "; ".join(notes)


def _ship_classified_paths(root: Path, paths: list[str]) -> list[str]:
    """*paths* filtered to the ones `export_guard.py` classifies ship.

    A drop-classified changed path (a test file, a doc) handed to `approve`
    would be REFUSED as "not ship-classified" and wrongly abort the land —
    this is what keeps the manifest step scoped to files the guard will
    actually accept."""
    if not paths:
        return []
    builder = _load_export_builder(root)
    if builder is None:
        return []
    try:
        rules = builder.parse_classification(
            (root / builder.CLASSIFICATION_NAME).read_text(encoding="utf-8"))
        tracked_out = _sh(["git", "ls-files", "-z"], cwd=root).stdout
        tracked = [p for p in tracked_out.split("\0") if p]
        cls = builder.classify(rules, tracked)
    except Exception:  # noqa: BLE001 — advisory narrowing only
        return []
    shipped = set(cls.shipped)
    manifest_name = builder.RELEASE_MANIFEST_NAME
    return [p for p in paths if p in shipped and p != manifest_name]


def _map_change_scoped_tests(root: Path, changed_files: list[str]) -> list[str]:
    """`src/no_human/**/<stem>.py` -> `tests/test_<stem>.py`, plus any changed
    path already under `tests/`. Missing mappings are silently dropped — the
    caller logs "no change-scoped tests matched" rather than claim a pass."""
    tests: list[str] = []
    for f in changed_files:
        p = Path(f)
        if p.suffix != ".py":
            continue
        if f.startswith("tests/"):
            if (root / f).exists():
                tests.append(f)
            continue
        if f.startswith("src/"):
            candidate = f"tests/test_{p.stem}.py"
            if (root / candidate).exists():
                tests.append(candidate)
    return sorted(dict.fromkeys(tests))


def _real_python(repo_path: Path | None = None) -> str | None:
    """A real Python interpreter, or ``None`` if none is usable.

    Normally ``sys.executable``. But in a PyInstaller-frozen build — the
    shipped desktop app — ``sys.executable`` IS the frozen ``nh`` binary, not
    a Python. Every ``[sys.executable, "-m", "pytest", ...]`` and
    ``[sys.executable, "<script>.py", ...]`` below then re-enters the click
    CLI, which exits in its own argument parser without running anything
    (measured 2026-09-14 against the shipped bundle: ``nh -m pytest -q`` ->
    ``Error: No such option '-m'``; ``nh scripts/check_release_manifest.py``
    -> ``Error: No such command``). So `nh approve` and the board's Approve
    button failed at step "tests" for EVERY repo, and at step "manifest" for
    this repo's shape, on every land made from the app.

    Third instance of this exact scar. A FOURTH then landed in
    ``vcs/manifest_repair.py`` (issue #402), so the frozen resolution itself
    lives once in ``proc.real_python`` and this is the merge gate's binding
    of it. ``None`` means the caller must fail closed and say so, never shell
    out to the CLI by accident.

    THIS gate needs an interpreter carrying the TARGET's test deps (pytest)
    even OFF a freeze, so it probes the target repo's ``.venv`` itself, ahead
    of delegating to ``proc.real_python``. That delegation cannot cover this
    case: ``proc.real_python`` returns ``sys.executable`` off a freeze by
    design — several callers depend on that (``worktree._builder_python``
    builds a venv WITH the running interpreter and must not switch it), so its
    non-frozen behavior must not change. But ``sys.executable`` is the pytest
    carrier ONLY when nh runs from a dev checkout's own ``.venv``; a ``uv tool
    install`` of nh runs on an interpreter with only nh's RUNTIME deps — no
    pytest — so the gate's ``[py, "-m", "pytest"]`` failed with "No module
    named pytest" and NO PR could land (measured 2026-09-17). Probing the
    repo's ``.venv`` here fixes that without touching the shared resolver.

    *repo_path* must be the MAIN repo checkout (``repo.path``), NOT the
    throwaway ``nh-land-*`` worktree the land runs in: a fresh detached
    worktree has no ``.venv`` of its own. The main checkout's ``.venv`` holds
    the deps. A repo with no ``.venv`` (the test fixtures, an arbitrary target
    project) falls through to ``proc.real_python`` unchanged.
    """
    if repo_path is not None:
        venv = Path(repo_path) / ".venv"
        for sub, name in _VENV_INTERPRETERS:
            candidate = venv / sub / name
            if candidate.is_file():
                return str(candidate)
    return real_python()


def _run_pytest(argv: list[str], *, cwd: Path, timeout: float,
                 env: dict) -> subprocess.CompletedProcess:
    """The one place `nh approve`'s merge-time gate shells out to pytest —
    a seam so tests can pin WHICH set was invoked (focused vs. full)
    without ever running it for real."""
    # A captured Python child writes in the LOCALE encoding, unlike git and
    # gh which emit UTF-8 regardless. `_sh` now decodes as UTF-8, so without
    # this a cp1255/cp932 host would render this gate's failure report as
    # mojibake — the verdict is the returncode and stays correct, but the
    # text a human reads to understand a refused land would not be. Set on a
    # COPY: the caller's dict is its own and must not gain a key it did not
    # ask for.
    env = {**env, "PYTHONIOENCODING": "utf-8"}
    return _sh(argv, cwd=cwd, timeout=timeout, env=env)


def _tree_of(worktree_path: Path, commitish: str) -> str:
    """The tree object *commitish* points at, resolved IN *worktree_path* —
    "" if it does not resolve there (unknown sha, pruned branch, empty
    string). Comparing trees (not commits) is deliberate: a squash landing
    exactly its tested content produces the identical tree even though its
    commit sha differs (new parent, new message)."""
    if not commitish:
        return ""
    proc = _sh(["git", "rev-parse", "--verify", "--quiet",
                f"{commitish}^{{tree}}"], cwd=worktree_path)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _decide_gate(landed_tree: str, tested_commit_sha: str,
                  tested_tree: str) -> tuple[str, str]:
    """Which merge-time test gate to run, and why — conservative by default
    (an unknown or unresolvable tested tree runs the FULL suite, never the
    cheap path): trees compare equal only when the squash result is
    byte-for-byte the tree the attempt's recorded full suite passed on."""
    if not tested_commit_sha:
        return "full", "full (no recorded tested-attempt tree)"
    if not tested_tree:
        return "full", (f"full (tested attempt {tested_commit_sha[:12]} does not "
                         "resolve here)")
    if not landed_tree:
        return "full", "full (could not resolve the squash-result tree)"
    if landed_tree == tested_tree:
        return "focused", f"focused (tree matches tested attempt {tested_commit_sha[:12]})"
    return "full", (f"full (tree diverged from tested attempt "
                     f"{tested_commit_sha[:12]}: conflict rounds or a moved base)")


def _close_pr(pr_url: str, cwd: Path) -> str:
    """Close *pr_url* without a comment. Idempotent, best-effort: any problem
    (gh/glab missing, network, already handled) returns a note but never
    raises — the code is already on the default branch by the time this
    runs, so failing the task here would strand a landed change.

    *cwd* is an explicit, guaranteed-existing directory (the caller's repo
    path). `gh`/`glab` here only ever address `--repo`/`-R <slug>`
    explicitly, so the directory is never actually read as a git repo — but
    subprocess still requires it to exist, and the process-global cwd is
    ambient state that other test modules mutate."""
    parsed = parse_pr_url(pr_url)
    if not parsed:
        return "pr close skipped: could not parse PR URL"
    forge, host, slug, number = parsed

    if forge == "gitlab":
        if not shutil.which("glab"):
            return "mr close skipped: glab not found"
        try:
            state_proc = _sh(["glab", "mr", "view", str(number), "-R", slug],
                              cwd=cwd, timeout=_GH_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"mr close skipped: could not read state ({exc})"
        out = (state_proc.stdout or "").lower()
        if "state:\tmerged" in out or "state:\tclosed" in out:
            return ""
        try:
            close_proc = _sh(["glab", "mr", "close", str(number), "-R", slug],
                              cwd=cwd, timeout=_GH_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"mr close failed (non-fatal): {exc}"
        if close_proc.returncode != 0:
            return f"mr close failed (non-fatal): {close_proc.stderr.strip()[:300]}"
        return ""

    if not shutil.which("gh"):
        return "pr close skipped: gh not found"
    repo_arg = f"{host}/{slug}"
    state = ""
    try:
        state_proc = _sh(
            ["gh", "pr", "view", str(number), "--repo", repo_arg, "--json", "state"],
            cwd=cwd, timeout=_GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"pr close skipped: could not read state ({exc})"
    if state_proc.returncode == 0:
        try:
            state = str(json.loads(state_proc.stdout).get("state") or "").upper()
        except json.JSONDecodeError:
            state = ""
    if state in ("MERGED", "CLOSED"):
        return ""
    try:
        close_proc = _sh(["gh", "pr", "close", str(number), "--repo", repo_arg],
                          cwd=cwd, timeout=_GH_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"pr close failed (non-fatal): {exc}"
    if close_proc.returncode != 0:
        return f"pr close failed (non-fatal): {close_proc.stderr.strip()[:300]}"
    return ""


def land_task(
    *,
    repo_path: Path | str,
    branch: str,
    pr_url: str,
    task_id: str,
    task_title: str,
    review_evidence: str,
    config: dict,
    changed_test_paths: list[str] | None = None,
    tested_commit_sha: str = "",
    remote: str = "origin",
    _before_push: Callable[[], None] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> LandResult:
    """Run the whole land procedure. Never raises — every failure path
    returns a :class:`LandResult` with ``ok=False`` and the failing step.

    ``config`` is a plain nested dict (``Config.data`` in production) —
    reads ``config["git"]`` (``agent_identity_name``/``_email``,
    ``never_push_to``, ``approve_identity``) and ``config["approve_merge"]``
    (``enabled``, ``test_timeout_seconds``, ``full_test_timeout_seconds``).

    ``tested_commit_sha`` is the commit the task's ATTEMPT recorded its
    full-suite pass on (``store.latest_attempt_branch(task_id)["commit_sha"]``
    at the caller). If the squash result's tree matches that commit's tree,
    the merge-time gate stays the cheap change-scoped set; if it diverged
    (a conflict-round or supervisor commit landed on the branch, or the
    base moved since) — or *tested_commit_sha* is empty or does not resolve
    — the gate runs the FULL suite instead, since the full-suite evidence
    attached to the PR is only ever valid for the tree it actually ran on.

    ``_before_push`` is a test-only seam: a callable invoked immediately
    before the push-time tip re-check, so a test can simulate a concurrent
    push landing on the remote's default branch mid-run — otherwise
    unreachable from a synchronous, single-threaded call.

    ``on_step`` is an optional progress callback, invoked with one of
    :data:`STEPS` at each step boundary (so a caller can stream "merge is at
    step X" to a human watching a slow land run) — never raises out of this
    function (see :func:`_step`).
    """
    # The act-level half of the human gate (session_mark.py): this is the one
    # sanctioned place the product performs a real merge, so the mark check
    # lives HERE, at the act, not only in the CLI wrapper and HTTP middleware
    # that call in. A process descended from a coding backend carries the mark
    # (stamped by `claude_backend._options` / `codex_backend._child_env`); if it
    # drives this module in-process it is refused before any state mutates.
    # Operator/server callers are unmarked and pass. Never raises, per this
    # function's contract — a set mark returns a failed LandResult.
    mark = current_mark()
    if mark is not None:
        return LandResult(
            ok=False, step="preconditions", branch=branch, pr_url=pr_url,
            stderr=(
                "refused: this process is a marked agent session "
                f"(kind={mark!r}); merging a PR is operator-only "
                "(see docs/security.md)."
            ),
        )
    _step(on_step, "preconditions")
    approve_cfg = config.get("approve_merge") or {}
    if not approve_cfg.get("enabled", True):
        return LandResult(ok=True, step="preconditions", skipped=True, branch=branch,
                           pr_url=pr_url, message="approve_merge disabled — "
                           "approval recorded only, merge the PR yourself")
    if not (pr_url or "").strip():
        return LandResult(ok=True, step="preconditions", skipped=True, branch=branch,
                           message="no PR to merge")
    if not shutil.which("gh"):
        return LandResult(ok=True, step="preconditions", skipped=True, branch=branch,
                           pr_url=pr_url, message="gh CLI not found — cannot merge "
                           "automatically; merge the PR yourself")

    # Fail CLOSED on a non-Conventional-Commits squash subject. The squash
    # message's first line is built from the task title verbatim (below), and
    # a task title is prose — 4 of the last 8 origin/main subjects (measured
    # 2026-09-19) were a prose task title that violated the operator's hard
    # rule (Conventional Commits v1.0.0, 2026-09-17). Validated on the
    # REDACTED string, which is the exact byte sequence that reaches git
    # history. NEVER auto-rewrites: the title is the human's to correct.
    from ..eval.vendor_terms import redact_for_publish
    from ..core.task import conventional_subject_error
    subject = redact_for_publish(task_title).splitlines()[0] if task_title else ""
    reason = conventional_subject_error(subject)
    if reason is not None:
        return LandResult(
            ok=False, step="preconditions", branch=branch, pr_url=pr_url,
            stderr=(f"refused: squash subject {subject!r} is not "
                    f"Conventional Commits v1.0.0 ({reason}). "
                    f"Nothing was pushed. Fix the task title with "
                    f"`nh task retitle {task_id} \"fix(scope): …\"` "
                    f"and re-run `nh approve`."),
        )

    git_cfg = config.get("git") or {}
    try:
        repo = GitRepo(
            Path(repo_path),
            identity_name=git_cfg.get("agent_identity_name", "no_human"),
            identity_email=git_cfg.get("agent_identity_email", "no-human@acme.com"),
            never_push_to=git_cfg.get("never_push_to")
            or ["main", "master", "release/*"],
        )
    except GitError as exc:
        return LandResult(ok=False, step="preconditions", branch=branch, pr_url=pr_url,
                           stderr=_cap(str(exc)))

    resolved_branch = repo.resolve_commitish(branch)
    if not resolved_branch:
        return LandResult(ok=False, step="preconditions", branch=branch, pr_url=pr_url,
                           stderr=f"branch {branch!r} does not resolve to a commit")

    op_name, op_email, ident_err = _resolve_approve_identity(git_cfg, repo.path)
    if ident_err:
        return LandResult(ok=False, step="preconditions", branch=branch,
                           pr_url=pr_url, stderr=_cap(ident_err))
    test_timeout = approve_cfg.get("test_timeout_seconds", _DEFAULT_TEST_TIMEOUT_S)
    full_test_timeout = approve_cfg.get(
        "full_test_timeout_seconds", _DEFAULT_FULL_TEST_TIMEOUT_S)

    # -- step 2: fetch + resolve the CURRENT default-branch tip ------------ #
    _step(on_step, "fetch")
    repo.fetch(remote)
    resolved_branch = repo.resolve_commitish(branch) or resolved_branch
    default = repo.default_branch()
    if not default:
        return LandResult(ok=False, step="fetch", branch=branch, pr_url=pr_url,
                           stderr="cannot resolve the remote's default branch")
    # Always the REMOTE-TRACKING ref, never `resolve_commitish(default)`: a
    # task's repo_path clone routinely carries a local branch of the same
    # name as the default branch (e.g. left over from clone time) that
    # `git fetch` never fast-forwards — `resolve_commitish` prefers exactly
    # that stale local branch over `origin/<default>`, which silently pins
    # the land to a base that predates this run's own fetch.
    tip_ref = f"{remote}/{default}"
    tip_proc = _sh(["git", "rev-parse", "--verify", "--quiet", tip_ref], cwd=repo.path)
    if tip_proc.returncode != 0 or not tip_proc.stdout.strip():
        return LandResult(ok=False, step="fetch", branch=branch, pr_url=pr_url,
                           stderr=f"cannot resolve {tip_ref!r} to a commit")
    if tip_proc.returncode != 0:
        return LandResult(ok=False, step="fetch", branch=branch, pr_url=pr_url,
                           stderr=_cap(tip_proc.stderr))
    tip_sha = tip_proc.stdout.strip()

    # -- worktree, at the tip, detached -------------------------------------#
    _step(on_step, "worktree")
    tmp_dir = Path(tempfile.mkdtemp(prefix="nh-land-"))
    shutil.rmtree(tmp_dir, ignore_errors=True)  # free the name for `worktree add`
    try:
        repo.add_worktree(tmp_dir, base=tip_sha, detach=True)
    except (GitError, ProtectedBranch) as exc:
        # `git worktree add` can register the worktree (and create its admin
        # dir / the directory itself) before failing partway through — e.g. a
        # `ProtectedBranch` refusal raised by this wrapper AFTER the `git`
        # call already ran, or a git-side failure after the admin files were
        # written. Best-effort cleanup here closes the one failure path that
        # used to strand a worktree (every OTHER step's failure already goes
        # through the `finally` below).
        _cleanup_worktree(repo, tmp_dir)
        return LandResult(ok=False, step="worktree", branch=branch, pr_url=pr_url,
                           stderr=_cap(str(exc)))

    try:
        return _land_in_worktree(
            repo=repo, worktree_path=tmp_dir, remote=remote, branch=branch,
            resolved_branch=resolved_branch, default=default, tip_sha=tip_sha,
            task_id=task_id, task_title=task_title, review_evidence=review_evidence,
            op_name=op_name, op_email=op_email, test_timeout=test_timeout,
            full_test_timeout=full_test_timeout,
            pr_url=pr_url, changed_test_paths=changed_test_paths,
            tested_commit_sha=tested_commit_sha,
            _before_push=_before_push, on_step=on_step,
        )
    finally:
        _cleanup_worktree(repo, tmp_dir)


def _unmerged_paths(worktree_path: Path) -> set[str]:
    """Paths git still reports as unmerged after a failed squash — the
    exact set, so a caller can tell "only the ledger file" from a real
    conflict. Empty on any git failure (which the caller treats as a real
    conflict, never as clean)."""
    proc = _sh(["git", "diff", "--name-only", "--diff-filter=U"], cwd=worktree_path)
    if proc.returncode != 0:
        return set()
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


def _cleanup_worktree(repo: "GitRepo", path: Path) -> None:
    """Best-effort worktree teardown, shared by every failure path (the
    `add_worktree` failure above and the `finally` around every in-worktree
    step) so no `land_task` outcome can strand a temp worktree or its admin
    dir. Swallows the same errors `GitRepo.remove_worktree`'s own `check=
    False` already tolerates plus a missing directory — cleanup must never
    replace the real failure being reported."""
    try:
        repo.remove_worktree(path, force=True)
    except (GitError, OSError):
        pass
    shutil.rmtree(path, ignore_errors=True)


def _land_regenerate_manifest(
    *, worktree_path: Path, py: str, guard: Path, inventory: Path, manifest: Path,
    tip_sha: str, resolved_branch: str, branch: str, pr_url: str,
) -> tuple["LandResult | None", str]:
    """`_land_in_worktree` step 4: regenerate RELEASE_MANIFEST.txt for the
    squashed tree, on whichever of the two backends this repo shape carries
    — `export_guard.py approve` (per-path pins, when `EXPORT_CLASSIFICATION.
    txt` is in play) or `check_release_manifest.py --write` (wholesale
    rebuild, this repo's own guard-less shape). Split out of
    `_land_in_worktree` purely to keep that function under the structural
    line-budget ratchet (`tests/test_structural_budget.py`); behavior is
    unchanged from when this lived inline. Returns `(None, reconciled_note)`
    on success, `(failure_result, "")` on failure — the caller returns the
    failure result as-is.

    *py* is the interpreter `_land_in_worktree` already resolved once (from the
    MAIN repo's `.venv`, not the venv-less worktree — see `_real_python`) and
    None-checked, so this step reuses it rather than resolving a second, wrong
    one against the worktree.
    """
    reconciled_note = ""
    if guard.exists() and manifest.exists():
        co = _sh(["git", "checkout", tip_sha, "--", "RELEASE_MANIFEST.txt"],
                  cwd=worktree_path)
        if co.returncode != 0:
            return LandResult(ok=False, step="manifest", branch=branch, pr_url=pr_url,
                               stderr=_cap(co.stderr)), ""

        diff_proc = _sh(
            ["git", "diff", "--name-only", "--diff-filter=d",
             f"{tip_sha}..{resolved_branch}"],
            cwd=worktree_path,
        )
        changed = [p.strip() for p in diff_proc.stdout.splitlines() if p.strip()]
        shipped_changed = _ship_classified_paths(worktree_path, changed)

        if shipped_changed:
            _sh(["git", "add", "-A", "--", *shipped_changed], cwd=worktree_path)
            try:
                approve_proc = _sh(
                    [py, "scripts/export_guard.py", "approve",
                     *shipped_changed],
                    cwd=worktree_path, timeout=_APPROVE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return LandResult(
                    ok=False, step="manifest", branch=branch, pr_url=pr_url,
                    stderr=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s"), ""
            if approve_proc.returncode == 2 and COUNT_DRIFT_RE.search(
                    approve_proc.stdout + approve_proc.stderr):
                # Same refusal the derived-conflict resolver meets, same
                # arithmetic: a squash onto a tip that also bumped the count.
                ok, note = reconcile_merge_count_drift(
                    worktree_path, tip_sha, resolved_branch,
                    approve_proc.stdout + approve_proc.stderr)
                if not ok:
                    return LandResult(
                        ok=False, step="manifest", branch=branch, pr_url=pr_url,
                        stderr=_cap(f"{CLASSIFICATION_NAME} count drift is not merge "
                                    f"arithmetic ({note}):\n"
                                    + approve_proc.stdout + approve_proc.stderr)), ""
                reconciled_note = note
                # Wherever a repo SHIPS its classification file it is pinned,
                # and the rewrite stales that pin — re-pin it or step-7 verify
                # refuses. (This repo drops the file, so here it is a no-op;
                # the land fixture ships it and covers the path.)
                retry_targets = list(dict.fromkeys(
                    [*shipped_changed,
                     *_ship_classified_paths(worktree_path, [CLASSIFICATION_NAME])]))
                try:
                    approve_proc = _sh(
                        [py, "scripts/export_guard.py", "approve",
                         *retry_targets],
                        cwd=worktree_path, timeout=_APPROVE_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired:
                    return LandResult(
                        ok=False, step="manifest", branch=branch, pr_url=pr_url,
                        stderr=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s "
                               f"(after count reconcile: {note})"), ""
            if approve_proc.returncode != 0:
                why = ("scan-hit refusal" if approve_proc.returncode == 1
                       else "refused before writing pins")
                return LandResult(
                    ok=False, step="manifest", branch=branch, pr_url=pr_url,
                    stderr=_cap(f"export_guard approve {why} "
                                f"({approve_proc.returncode}):\n"
                                + approve_proc.stdout + approve_proc.stderr)), ""

        add_manifest = _sh(["git", "add", "--", "RELEASE_MANIFEST.txt"], cwd=worktree_path)
        if add_manifest.returncode != 0:
            return LandResult(ok=False, step="manifest", branch=branch, pr_url=pr_url,
                               stderr=_cap(add_manifest.stderr)), ""
    elif inventory.exists() and manifest.exists():
        # No export guard on this repo shape (this repo's own shape: public
        # working tree, no EXPORT_CLASSIFICATION.txt) — the ledger has no
        # per-path approval step, so the whole file is regenerated wholesale
        # from the landed tree, exactly as `derived_conflict._inventory_
        # resolve_tail` already does for the mechanical-conflict path this
        # one mirrors.
        try:
            write_proc = _sh(
                [py, "scripts/check_release_manifest.py", "--write"],
                cwd=worktree_path, timeout=_APPROVE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return LandResult(
                ok=False, step="manifest", branch=branch, pr_url=pr_url,
                stderr=f"check_release_manifest.py --write timed out after "
                       f"{_APPROVE_TIMEOUT_S}s"), ""
        if write_proc.returncode != 0:
            return LandResult(
                ok=False, step="manifest", branch=branch, pr_url=pr_url,
                stderr=_cap(f"check_release_manifest.py --write failed "
                            f"({write_proc.returncode}):\n"
                            + write_proc.stdout + write_proc.stderr)), ""
        add_manifest = _sh(["git", "add", "--", "RELEASE_MANIFEST.txt"], cwd=worktree_path)
        if add_manifest.returncode != 0:
            return LandResult(ok=False, step="manifest", branch=branch, pr_url=pr_url,
                               stderr=_cap(add_manifest.stderr)), ""
    return None, reconciled_note


def _land_in_worktree(
    *, repo: GitRepo, worktree_path: Path, remote: str, branch: str,
    resolved_branch: str, default: str, tip_sha: str, task_id: str,
    task_title: str, review_evidence: str, op_name: str, op_email: str,
    test_timeout: float, full_test_timeout: float, pr_url: str,
    changed_test_paths: list[str] | None, tested_commit_sha: str = "",
    _before_push: Callable[[], None] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> LandResult:
    guard = worktree_path / "scripts" / "export_guard.py"
    inventory = worktree_path / "scripts" / "check_release_manifest.py"
    manifest = worktree_path / "RELEASE_MANIFEST.txt"

    # Every interpreter shell-out below goes through this, resolved once from
    # the MAIN repo's `.venv` (the throwaway worktree has none of its own), so
    # a tool-installed nh — whose own interpreter lacks pytest — still runs the
    # gate on the repo's dep-complete interpreter. `sys.executable` is the
    # frozen `nh` binary in the desktop build, not a Python — see
    # `_real_python`. Fail closed with the condition named rather than
    # re-entering the click CLI and reporting its own argument-parser error as
    # a test or manifest failure.
    py = _real_python(repo.path)
    if py is None:
        return LandResult(
            ok=False, step="preconditions", branch=branch, pr_url=pr_url,
            stderr="no Python interpreter available to run the merge-time "
                   "gate: this build's sys.executable is the frozen `nh` "
                   "binary and no python3/python was found on PATH, nor a "
                   ".venv in the worktree")

    # -- step 3: squash --------------------------------------------------- #
    _step(on_step, "squash")
    merge_proc = _sh(["git", "merge", "--squash", resolved_branch], cwd=worktree_path)
    if merge_proc.returncode != 0:
        # RELEASE_MANIFEST.txt moves under every landing, so a branch cut
        # before the previous landing conflicts on it here — and step 4
        # discards the squash's copy of that file anyway (tip's copy wins,
        # the branch's pins are re-derived on top, on EITHER backend — see
        # step 4). Refusing at this step made every PR older than the last
        # landing unlandable by `nh approve` (#582, 2026-08-22). So a
        # conflict confined to the ledger file is resolved exactly as step 4
        # would: take the tip's copy and carry on. Anything else unmerged is
        # a real conflict (a hand-authored path collided) and refuses.
        #
        # This tolerance used to require `scripts/export_guard.py` to be
        # present, because step 4 used to re-derive the manifest only on
        # that backend — a repo without it (this repo's own shape: no
        # export guard, `scripts/check_release_manifest.py` instead) fell
        # through to the `else` below and refused, meaning EVERY
        # manifest-only conflict on the public working repo needed a full,
        # costly coder round or a manual `nh approve` retry (the incident
        # this fix closes: task 4135165f, PR #356, 2026-09-14 — 11 attempts
        # / 484k tokens / ~$69 spent resolving a conflict that was, in the
        # end, one regenerated file). Step 4 (`_land_regenerate_manifest`)
        # now re-derives the manifest on BOTH backends, so this still gates
        # on a backend being present (`guard` or `inventory`) — never
        # tolerate a manifest-only conflict that step 4 has no way to
        # reconcile, or the landed manifest would silently keep the tip's
        # stale copy with nothing to re-derive it and no verify step to
        # catch the drift — but no longer needs to pick between the two —
        # see `derived_conflict.py`'s identical, already-fixed
        # `resolve_derived_conflict` (2026-09-04) for the sibling repair
        # this one completes.
        unmerged = _unmerged_paths(worktree_path)
        if unmerged == {"RELEASE_MANIFEST.txt"} and (guard.exists() or inventory.exists()):
            co = _sh(["git", "checkout", tip_sha, "--", "RELEASE_MANIFEST.txt"],
                      cwd=worktree_path)
            if co.returncode != 0:
                _sh(["git", "merge", "--abort"], cwd=worktree_path)
                return LandResult(ok=False, step="squash", branch=branch, pr_url=pr_url,
                                   stderr=_cap(merge_proc.stdout + "\n" + co.stderr))
        else:
            _sh(["git", "merge", "--abort"], cwd=worktree_path)
            return LandResult(ok=False, step="squash", branch=branch, pr_url=pr_url,
                               stderr=_cap(merge_proc.stdout + "\n" + merge_proc.stderr))

    # -- step 4: manifest merge-result ledger rule ------------------------ #
    _step(on_step, "manifest")
    manifest_err, reconciled_note = _land_regenerate_manifest(
        worktree_path=worktree_path, py=py, guard=guard, inventory=inventory,
        manifest=manifest, tip_sha=tip_sha, resolved_branch=resolved_branch,
        branch=branch, pr_url=pr_url)
    if manifest_err is not None:
        return manifest_err

    # -- step 5: operator-identity commit ---------------------------------- #
    # `GIT_AUTHOR_NAME`/`_EMAIL`/`GIT_COMMITTER_NAME`/`_EMAIL` env vars, when
    # set, OUTRANK `-c user.name=`/`user.email=` on the command line — and
    # `nh approve` can itself run inside a coder/agent process whose sandbox
    # sets exactly those four to the AGENT identity (observed in practice).
    # Scrubbed here so the operator identity always wins regardless of the
    # ambient environment this runs in.
    #
    # `task_title` and `review_evidence` are free text (a task title has
    # carried a flagged vendor term before — the PR #334/#339 incident) and
    # this commit lands permanently on the forge's default branch, so both
    # go through the same outbound scrub `nh bench publish` uses before
    # anything free-text reaches a tracked, published artifact. Imported
    # lazily — `eval` transitively imports `core.orchestrator`, which imports
    # this package, so a module-level import here is circular.
    _step(on_step, "commit")
    from ..eval.vendor_terms import redact_for_publish
    message = (f"{redact_for_publish(task_title)}\n\n{task_id}\n"
               f"{redact_for_publish(review_evidence)}")
    commit_env = dict(os.environ)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        commit_env.pop(var, None)
    commit_proc = _sh(
        ["git", "-c", f"user.name={op_name}", "-c", f"user.email={op_email}",
         "commit", "-m", message],
        cwd=worktree_path, env=commit_env,
    )
    if commit_proc.returncode != 0:
        return LandResult(ok=False, step="commit", branch=branch, pr_url=pr_url,
                           stderr=_cap(commit_proc.stdout + "\n" + commit_proc.stderr))
    landed_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 6a: export_guard verify -------------------------------------- #
    _step(on_step, "verify")
    if guard.exists():
        try:
            verify_proc = _sh([py, "scripts/export_guard.py", "verify"],
                               cwd=worktree_path, timeout=_VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return LandResult(ok=False, step="verify", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=f"export_guard verify timed out after "
                                      f"{_VERIFY_TIMEOUT_S}s")
        if verify_proc.returncode != 0:
            return LandResult(ok=False, step="verify", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=_cap(verify_proc.stdout + "\n" + verify_proc.stderr))
    elif inventory.exists():
        # Matches the `inventory` CI job (`.github/workflows/ci.yml`):
        # `python scripts/check_release_manifest.py --strict`. This is a new
        # check for this repo shape — previously no manifest verification
        # ran during `nh approve` here at all, since step 4's regeneration
        # (above) used to be guard-only too.
        try:
            verify_proc = _sh(
                [py, "scripts/check_release_manifest.py", "--strict"],
                cwd=worktree_path, timeout=_VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return LandResult(ok=False, step="verify", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=f"check_release_manifest.py --strict timed out "
                                      f"after {_VERIFY_TIMEOUT_S}s")
        if verify_proc.returncode != 0:
            return LandResult(ok=False, step="verify", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha,
                               stderr=_cap(verify_proc.stdout + "\n" + verify_proc.stderr))

    # -- step 6b: merge-time test gate --------------------------------------#
    # FOCUSED (change-scoped) when the squash result's tree matches the tree
    # the attempt's recorded full suite ran on; FULL otherwise (conflict
    # rounds / a moved base / an unknown tested tree) — see `_decide_gate`.
    _step(on_step, "tests")
    landed_tree = _tree_of(worktree_path, "HEAD")
    tested_tree = _tree_of(worktree_path, tested_commit_sha)
    gate, gate_reason = _decide_gate(landed_tree, tested_commit_sha, tested_tree)

    if gate == "focused":
        test_paths = changed_test_paths
        if test_paths is None:
            squash_diff = _sh(["git", "diff", "--name-only", f"{tip_sha}..HEAD"],
                               cwd=worktree_path)
            changed_files = [p.strip() for p in squash_diff.stdout.splitlines() if p.strip()]
            test_paths = _map_change_scoped_tests(worktree_path, changed_files)
        if test_paths:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(worktree_path / "src")
            argv = [py, "-m", "pytest", "-q", *test_paths]
            try:
                test_proc = _run_pytest(argv, cwd=worktree_path, timeout=test_timeout, env=env)
            except subprocess.TimeoutExpired:
                return LandResult(ok=False, step="tests", branch=branch, pr_url=pr_url,
                                   landed_sha=landed_sha, gate=gate, gate_reason=gate_reason,
                                   stderr=f"{gate_reason}\nchange-scoped tests timed out "
                                          f"after {test_timeout}s")
            if test_proc.returncode != 0:
                return LandResult(ok=False, step="tests", branch=branch, pr_url=pr_url,
                                   landed_sha=landed_sha, gate=gate, gate_reason=gate_reason,
                                   stderr=_cap(f"{gate_reason}\n"
                                               + test_proc.stdout + "\n" + test_proc.stderr))
    else:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(worktree_path / "src")
        argv = [py, "-m", "pytest", "-q"]
        if importlib.util.find_spec("xdist") is not None:
            argv += ["-n", "4"]
        try:
            test_proc = _run_pytest(argv, cwd=worktree_path, timeout=full_test_timeout, env=env)
        except subprocess.TimeoutExpired:
            return LandResult(ok=False, step="tests", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha, gate=gate, gate_reason=gate_reason,
                               stderr=f"{gate_reason}\nfull suite timed out after "
                                      f"{full_test_timeout}s")
        if test_proc.returncode == 5:
            # No tests collected — a repo with no suite must not be blocked
            # from landing; annotate rather than fail.
            gate_reason = f"{gate_reason} (no tests collected)"
        elif test_proc.returncode != 0:
            return LandResult(ok=False, step="tests", branch=branch, pr_url=pr_url,
                               landed_sha=landed_sha, gate=gate, gate_reason=gate_reason,
                               stderr=_cap(f"{gate_reason}\n"
                                           + test_proc.stdout + "\n" + test_proc.stderr))

    # -- step 7: ff-merge + push, remote-ref verified ---------------------- #
    _step(on_step, "push")
    if _before_push is not None:
        _before_push()
    repo.fetch(remote)
    check_proc = _sh(
        ["git", "-C", str(repo.path), "rev-parse", "--verify", "--quiet",
         f"{remote}/{default}"],
        cwd=repo.path,
    )
    current_tip = check_proc.stdout.strip()
    if current_tip and current_tip != tip_sha:
        return LandResult(
            ok=False, step="push", branch=branch, pr_url=pr_url, landed_sha=landed_sha,
            stderr=f"{default} advanced from {tip_sha[:12]} to {current_tip[:12]} "
                   "during land; retry")

    # Pushed from the MAIN repo, deliberately NOT the worktree: `add_worktree`
    # installs a pre-push hook there (push_hook.py) that refuses any push
    # whose resolved ref matches `never_push_to` — the agent's second
    # enforcement point. This IS the one sanctioned protected-branch write
    # (a human `nh approve`, never the agent), and the main repo carries no
    # such hook, so pushing from there is what makes this write reach the
    # remote at all. Both worktrees share one object database, so the sha
    # created in the worktree is already visible here.
    push_proc = _sh(
        ["git", "push", remote, f"{landed_sha}:refs/heads/{default}"],
        cwd=repo.path,
    )
    if push_proc.returncode != 0:
        return LandResult(ok=False, step="push", branch=branch, pr_url=pr_url,
                           landed_sha=landed_sha,
                           stderr=_cap(push_proc.stdout + "\n" + push_proc.stderr))

    ls_proc = _sh(["git", "ls-remote", remote, f"refs/heads/{default}"], cwd=repo.path)
    remote_sha = (ls_proc.stdout.split() or [""])[0]
    if remote_sha != landed_sha:
        return LandResult(
            ok=False, step="push", branch=branch, pr_url=pr_url, landed_sha=landed_sha,
            stderr=f"remote ref did not advance to {landed_sha} "
                   f"(saw {remote_sha or '(none)'})")

    # -- step 8: close the PR, without a comment ---------------------------#
    _step(on_step, "close_pr")
    close_cwd = repo.path if repo.path.exists() else Path(tempfile.gettempdir())
    close_note = _close_pr(pr_url, close_cwd)
    msg = f"landed {landed_sha[:12]} onto {default}; gate: {gate_reason}"
    if close_note:
        msg += f"; {close_note}"
    return LandResult(ok=True, step="close_pr", branch=branch, pr_url=pr_url,
                      reconciled=reconciled_note, gate=gate, gate_reason=gate_reason,
                       landed_sha=landed_sha, message=msg)
