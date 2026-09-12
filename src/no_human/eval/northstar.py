"""North-star benchmark runner (plan Task A3): replay a real historical task
through the REAL Orchestrator in a push-proof sandbox and score it against the
original session's economics.

Safety invariant (the one that matters): the sandbox clone's ``origin`` is
re-pointed at a workdir-local bare BEFORE orchestration, and a hard guard
asserts it — the agent's branch pushes can never reach the operator's real
repo. A post-run check additionally asserts the source repo's refs are
untouched.

Cheating invariant: the GoalJudge sees ONLY the spec's request/criteria and
the agent's diff/outcome — never the source transcript or the original
solution.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
import time
import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import DEFAULT_CONFIG
from ..core.db import USAGE_ROLES, usage_columns_for, Store
from ..core.pricing import (CACHE_CREATION_WEIGHT, CACHE_READ_WEIGHT,
                            MODEL_PRICES_USD_PER_MTOK)
from ..core.events import EventPersister
from ..core.orchestrator import Orchestrator
from ..core.task import Task, TaskStatus
from ..notify.slack import SlackNotifier
from .bench_task import (BenchTask, redact_local_path, spec_pin_rederived,
                         spec_project_name)
from .sandbox_selftest import wrong_tree_imports

BackendFactory = Callable[[BenchTask], Any]

#: `db.USAGE_ROLES` role name -> the `config["llm"]` KEY that names its
#: model. A model id is never typed here — the tier is always resolved
#: through this key (or through a per-row recorded id), so there is exactly
#: one place a role's model can be looked up. `distill` is registered in
#: `USAGE_ROLES` but has no dedicated tier key (it rides on other calls'
#: models today) and is deliberately absent — never dropped, priced by the
#: fail-closed path in `tier_weight` below.
PRICED_ROLES: dict[str, str] = {
    "coder": "primary_model",
    "reviewer": "review_model",
    "planner": "planner_model",
    "supervisor": "supervisor_model",
    "utility": "utility_model",
}

#: The ticket's spelling of the coder role vs. `db.USAGE_ROLES`'s. Resolved
#: by `_canonical_role` before any `PRICED_ROLES`/`is_priced_role` lookup.
ROLE_ALIASES: dict[str, str] = {"implementer": "coder"}

#: The tier `tier_weight` prices every role relative to. The corpus's
#: `original:` blocks record no model (`eval/northstar_tasks/*.yaml` carry
#: `tokens: {}`), so the denominator's tier is an assumption; anchoring at
#: Sonnet means an anchor error can only INFLATE the published ratio (worse
#: for no_human), never deflate it.
ANCHOR_MODEL = "claude-sonnet-5"

#: The two bases `BenchScore.cost_ratio` can land on, and the ONLY spellings
#: any rendering surface (CLI, the report card, the web JSON payload) may
#: print — never a literal "tier-weighted"/"cache-weighted" typed inline
#: elsewhere, so the two cannot drift into slightly different wording on
#: different surfaces. `MIXED` is for an aggregate spanning both (a card that
#: straddles the part-1 change: an old score loaded from a pre-tier-weighting
#: `latest.json` alongside a freshly-run one).
BASIS_TIER_WEIGHTED = "tier-weighted"
BASIS_CACHE_WEIGHTED = "cache-weighted"
BASIS_MIXED = "mixed"


def _canonical_role(role: str) -> str:
    return ROLE_ALIASES.get((role or "").strip().lower(),
                            (role or "").strip().lower())


def is_priced_role(role: str) -> bool:
    """The ONE predicate for "does this role have a dedicated price tier".

    Every role-selection branch in this module calls this — never
    ``role in PRICED_ROLES`` or ``role != "distill"`` written inline — so the
    priced set cannot drift between `tier_weight`, the `_score` breakdown
    builder, and anything added later. This is the exact predicate-mismatch
    that failed an earlier review of this change.
    """
    return _canonical_role(role) in PRICED_ROLES


def _input_rate(model: str | None) -> float:
    """One model's fresh-input USD/MTok rate. An unknown or absent id prices
    at the HIGHEST input rate in the table — never the lowest, never zero —
    so an unrecognized tier is never priced as cheaper than a known one
    (mirrors `pricing.output_extra_weight`'s fallback rationale)."""
    if model:
        priced = MODEL_PRICES_USD_PER_MTOK.get(model)
        if priced is not None:
            return priced[0]
    return max(rate for rate, _out in MODEL_PRICES_USD_PER_MTOK.values())


def _model_for(role: str, models: dict[str, str] | None) -> str | None:
    """The model id to price *role* at.

    Prefers the RECORDED id (a historical row is priced at what it was
    actually billed, not at today's config) and falls back to the live
    default in `config.DEFAULT_CONFIG["llm"]`. A role `is_priced_role`
    rejects (e.g. `distill`) has no config key to fall back to and resolves
    to ``None``, which `_input_rate` fail-closes to the table's highest rate.
    """
    canonical = _canonical_role(role)
    if models:
        recorded = models.get(role) or models.get(canonical)
        if recorded:
            return recorded
    if not is_priced_role(role):
        return None
    return DEFAULT_CONFIG["llm"].get(PRICED_ROLES[canonical])


def tier_weight(role: str, models: dict[str, str] | None = None) -> float:
    """*role*'s fresh-input rate as a ratio against `ANCHOR_MODEL` — e.g.
    5/3 for an Opus-tier role when the anchor is Sonnet. An unpriced role or
    an unresolved model both fail closed to the table's highest ratio:
    dropping or under-pricing a role's spend rigs the ratio in no_human's
    favour, the same angle-4 defect `token_ratio`'s docstring already
    records for coder-only summation.
    """
    return _input_rate(_model_for(role, models)) / _input_rate(ANCHOR_MODEL)


def tier_weighted(tokens: float, role: str,
                  models: dict[str, str] | None = None) -> float:
    """*tokens*, priced at *role*'s tier relative to `ANCHOR_MODEL`."""
    return float(tokens) * tier_weight(role, models)

# Off-ramps that count as "reached the human gate" for gate-kind outcomes vs
# honest-escalation outcomes (expect_escalation specs). DONE is a gate state
# too (review D9): report-deliverable pipelines (investigation / design_doc /
# clean code_review) terminate DONE with the report as the deliverable — the
# judge evaluates that report; auto-failing DONE would score every successful
# rerouted report task ❌ without the judge ever seeing it.
_GATE_STATES = {TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE}
_HONEST_STOPS = {TaskStatus.ESCALATED, TaskStatus.AWAITING_INPUT,
                 TaskStatus.BLOCKED}


_DIGEST_MAX_EVENTS = 300
_DIGEST_MAX_TEXT = 200


#: How much of a tool call to keep. Deliberately much tighter than
#: _DIGEST_MAX_TEXT: a Write's input is a whole file, and this rides into a
#: TRACKED results JSON.
_DIGEST_MAX_TOOL_INPUT = 120


def _ends(text: str, cap: int) -> str:
    """Truncate to *cap* keeping BOTH ends, because the informative half of a
    tool input is usually the tail.

    Head-only truncation looked fine until it was used in anger: every path in a
    doom-loop rendered as the same 120 characters of shared prefix, so five
    different files read as one repeated call and the loop could not be told
    apart from a stutter. Keeping the tail is what makes two long paths
    distinguishable at a glance.
    """
    if len(text) <= cap:
        return text
    head = max(cap // 3, 1)
    tail = cap - head - 1
    return f"{text[:head]}…{text[-tail:]}"


def _tool_text(e: dict) -> str:
    """A one-line rendering of a tool call: ``Bash: pytest -q …``.

    A ``tool_use`` event carries NO ``text`` — it is built from ``tool_name``
    and ``tool_input`` (agent/backend.py: AgentEvent), and the sinks persist
    both. The digest only ever read ``text``, so every tool call in every score
    record was stored as an empty string, and the 55 calls behind a failed spec
    were unreadable afterwards. That cost two diagnoses in one day: par-07's
    reviewer verdict and nh-01's empty diff. Raising the truncation cap would
    have fixed neither — the field was never being read.
    """
    name = str(e.get("tool_name") or "")
    if not name:
        return ""
    raw = e.get("tool_input")
    if isinstance(raw, dict):
        # The fields worth having, in the order a reader wants them; anything
        # else is summarised by its keys rather than dumped.
        for key in ("command", "file_path", "path", "pattern", "description"):
            if raw.get(key):
                detail = str(raw[key])
                break
        else:
            detail = ",".join(sorted(raw)[:6])
    else:
        detail = str(raw or "")
    detail = _ends(" ".join(detail.split()), _DIGEST_MAX_TOOL_INPUT)
    return f"{name}: {detail}" if detail else name


def _digest_events(rows: list[dict], spec: BenchTask | None = None) -> list[dict]:
    """Compact the persisted event stream for the score record: kind + text
    (truncated) + ts. Keeps the LAST _DIGEST_MAX_EVENTS — terminal behavior
    (budget nudges, escalations, wrap-ups) clusters at the end.

    *spec* is used only to redact the locally-translated repo path, exactly as
    crash notes and judge evidence already are. It matters more here than there:
    a tool call's input is free text that routinely NAMES paths, and this digest
    is written into ``eval/results/northstar/*.json``, which is tracked.
    """
    out = []
    for e in rows[-_DIGEST_MAX_EVENTS:]:
        text = str(e.get("text") or e.get("message") or "")[:_DIGEST_MAX_TEXT]
        if not text:
            text = _tool_text(e)
        if text and spec is not None:
            text = redact_local_path(text, spec)
        out.append({"kind": str(e.get("kind") or e.get("source") or ""),
                    "text": text, "ts": e.get("ts")})
    return out


@dataclass
class BenchScore:
    task_id: str
    title: str
    outcome_status: str
    goal_satisfied: bool | None      # GoalJudge verdict; None = judge skipped
    escalated_honestly: bool
    mergeable: bool | None           # holdout tests, when the spec has them
    nh_tokens: int                   # non-cache in/out, coder + reviewer
    nh_cache_tokens: int             # cache-read, coder + reviewer
    nh_cache_creation_tokens: int
    nh_turns: int
    nh_wall_clock_s: float
    orig_tokens: int                 # non-cache: input+output
    orig_cache_tokens: int
    orig_cache_creation_tokens: int
    orig_wall_clock_s: float
    orig_corrections: int
    expected_escalation: bool = False   # spec said: correct = honest stop
    subset: str = "full"                # "core" = hand-curated, PR-reviewed
    project: str = ""                   # repo basename — per-project payoff view
    # Which repetition of this spec produced this score (0-based). A run with
    # `--trials N` records N scores per spec, all sharing `task_id` and
    # differing only here — so (task_id, trial) is the identity a checkpoint
    # resumes on, and a single-trial run is exactly today's shape with every
    # trial == 0.
    trial: int = 0
    # Escalation latency: how much of the run's cost it took to reach an
    # honest stop. Set unconditionally from the same `attempts`/`nh_tokens`
    # the row already computed (never a second summation, so this column and
    # the cost column can never disagree) — the report filters on
    # `escalated_honestly` to decide whether to show it.
    attempts_before_escalation: int = 0
    tokens_before_escalation: int = 0
    notes: str = ""
    # Capped digest of the run's event stream. bench.db dies with the
    # sandbox cleanup; the digest rides the score into progress.json /
    # latest.json so completed specs stay drillable post hoc (v11 live:
    # early escalations whose reasons were already deleted).
    events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Per-role breakdown of the same three addend columns `nh_tokens` /
    # `nh_cache_tokens` / `nh_cache_creation_tokens` already sum — role name
    # (`db.USAGE_ROLES` values) -> {"tokens_used", "cache_read_tokens",
    # "cache_creation_tokens"}. Both optional and both empty by default so
    # every existing constructor call (tests, old `latest.json` loads) keeps
    # building the exact score it always did. Empty means "no breakdown was
    # recorded", not "recorded and zero" — `nh_priced_tokens` reads emptiness
    # as the signal to take the pre-tier-weighting compat path.
    nh_role_tokens: dict[str, dict[str, int]] = dataclasses.field(
        default_factory=dict)
    # role -> the model id it was actually billed at, when known. Consulted
    # by `tier_weight` before it falls back to today's config default.
    nh_role_models: dict[str, str] = dataclasses.field(default_factory=dict)
    # The GoalJudge produced no parseable verdict after the bounded re-ask
    # retry — the judge broke, not the agent. False means "the judge
    # answered" OR "no judge ran"; it never influences goal_satisfied, which
    # stays fail-closed exactly as before this field existed.
    unscoreable: bool = False
    # The spec's pin was re-derived by date from a rewritten history — this
    # replay ran against today's ancestor of the recorded start, not the
    # tree the original task actually saw. Read-only here: set from the
    # spec at construction, never written back to it — no spec is ever
    # mutated at run time (`bench_task.build_bench_tasks` is the only
    # writer).
    pin_rederived: bool = False

    @property
    def token_ratio(self) -> float | None:
        """nh non-cache burn / original non-cache burn. <1.0 = no_human was
        cheaper than the babysat session. None when the original is unknown.

        nh_tokens INCLUDES the reviewer (B1 angle-4 finding: coder-only
        summation rigged the ratio in no_human's favor). Planner burn lands in plan_* columns (B2 #5); supervisor/
        distillation burn is still uncaptured (B2 #6) — a small residual
        under-count, labeled in the report."""
        if self.orig_tokens <= 0:
            return None
        return self.nh_tokens / self.orig_tokens

    @property
    def nh_priced_tokens(self) -> float:
        """no_human's own price-weighted spend — the numerator side of
        ``cost_ratio``, exposed on its own so "did this row spend ANYTHING"
        is asked of the same arithmetic the cost median prices with, never a
        re-derivation. Every weight is positive, so this is 0.0 IFF every
        nh token class is zero — a row that burned literally nothing.

        Two paths. With no per-role breakdown (``nh_role_tokens`` empty —
        every pre-tier-weighting caller, every score built before this field
        existed, and every legacy ``latest.json``) this is the ORIGINAL
        class-weighted expression, byte-for-byte: published ratios and
        ``test_token_ratio_math`` do not move. With a breakdown, each role's
        class-weighted subtotal is additionally priced at its own tier via
        `tier_weighted` (through `is_priced_role`'s single predicate, never a
        role check written inline here) before the parts are summed — a
        role's totals can be cheaper OR pricier than the anchor, so this can
        land on either side of the unweighted figure.
        """
        if not self.nh_role_tokens:
            return (self.nh_tokens
                    + CACHE_READ_WEIGHT * self.nh_cache_tokens
                    + CACHE_CREATION_WEIGHT * self.nh_cache_creation_tokens)
        total = 0.0
        for role, cols in self.nh_role_tokens.items():
            fresh = int(cols.get("tokens_used") or 0)
            read = int(cols.get("cache_read_tokens") or 0)
            creation = int(cols.get("cache_creation_tokens") or 0)
            class_total = (fresh + CACHE_READ_WEIGHT * read
                          + CACHE_CREATION_WEIGHT * creation)
            total += tier_weighted(class_total, role, self.nh_role_models)
        return total

    @property
    def cost_ratio_basis(self) -> str:
        """Which price table `cost_ratio`'s numerator (`nh_priced_tokens`)
        actually landed on — the single fact every rendering surface must
        show alongside the ratio itself, so a tier-weighted number and one
        computed on the older, role-blind cache-only basis are never
        silently conflated as the same figure (this repo's top defect class:
        text asserting what code does not compute).

        Branches on the exact same emptiness check `nh_priced_tokens`
        branches on — never a second, independently-maintained test — so
        this label can never disagree with the number it is labeling.
        """
        return BASIS_TIER_WEIGHTED if self.nh_role_tokens else BASIS_CACHE_WEIGHTED

    @property
    def cost_ratio(self) -> float | None:
        """Price-weighted ratio using Anthropic's cache multipliers
        (fresh=1.0, cache_read=0.1, cache_creation=1.25) applied SYMMETRICALLY
        to both sides — tracks dollar cost, where cache-read is ~95% of real
        burn and the plain token_ratio is blind to it.

        The multipliers used to be inline literals here, which made this the
        second price table in the tree; they now come from `core.pricing`, the
        same one the budget gate weights its caps with. Values unchanged — this
        is a de-duplication, not a re-pricing, so published ratios still stand.
        The arithmetic is kept in float (rather than routed through
        `pricing.weighted_tokens`, which floors to an int for the caps) so a
        ratio over small samples is not quantised.

        The NUMERATOR (`nh_priced_tokens`) is additionally tier-weighted per
        role when a breakdown was recorded — see its docstring. The
        DENOMINATOR is not: the original session's per-role split was never
        captured, so it stays anchored at the flat class weights above (the
        same basis every published ratio has always used).
        """
        orig = (self.orig_tokens
                + CACHE_READ_WEIGHT * self.orig_cache_tokens
                + CACHE_CREATION_WEIGHT * self.orig_cache_creation_tokens)
        if orig <= 0:
            return None
        return self.nh_priced_tokens / orig

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "title": self.title,
            "outcome_status": self.outcome_status,
            "goal_satisfied": self.goal_satisfied,
            "escalated_honestly": self.escalated_honestly,
            "mergeable": self.mergeable,
            "nh_tokens": self.nh_tokens,
            "nh_cache_tokens": self.nh_cache_tokens,
            "nh_cache_creation_tokens": self.nh_cache_creation_tokens,
            "nh_turns": self.nh_turns,
            "nh_wall_clock_s": round(self.nh_wall_clock_s, 2),
            "orig_tokens": self.orig_tokens,
            "orig_cache_tokens": self.orig_cache_tokens,
            "orig_cache_creation_tokens": self.orig_cache_creation_tokens,
            "orig_wall_clock_s": self.orig_wall_clock_s,
            "orig_corrections": self.orig_corrections,
            "expected_escalation": self.expected_escalation,
            "subset": self.subset,
            "project": self.project,
            "trial": self.trial,
            "attempts_before_escalation": self.attempts_before_escalation,
            "tokens_before_escalation": self.tokens_before_escalation,
            "token_ratio": (round(self.token_ratio, 3)
                            if self.token_ratio is not None else None),
            "cost_ratio": (round(self.cost_ratio, 3)
                           if self.cost_ratio is not None else None),
            # The basis label travels WITH the number on every wire payload —
            # see `cost_ratio_basis`'s docstring. Emitted unconditionally
            # (never gated on `cost_ratio is not None`) so a reader can tell
            # "no baseline" apart from "baseline, but on the older basis".
            "cost_ratio_basis": self.cost_ratio_basis,
            "notes": self.notes,
            "events": self.events,
            "nh_role_tokens": self.nh_role_tokens,
            "nh_role_models": self.nh_role_models,
            "unscoreable": self.unscoreable,
            "pin_rederived": self.pin_rederived,
        }


def _git(cwd: Path, *args: str) -> str:
    """Run git in the sandbox, retrying ONLY lock contention.

    Same policy as `GitRepo._run_with_lock_retry` (vcs/git.py): a stderr that
    matches the lock-contention signatures is another process briefly holding a
    lock (a parallel bench worker, an orphaned agent git subprocess) — re-run
    after a short backoff; anything else raises on the first attempt. Two
    main-6cec2140 specs were booked `crashed` on exactly this class.
    """
    from ..vcs.git import _GIT_RETRY_BACKOFFS_S, is_transient_git_failure
    cmd = ["git", *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    for backoff in _GIT_RETRY_BACKOFFS_S:
        if proc.returncode == 0 or not is_transient_git_failure(proc.stderr):
            break
        time.sleep(backoff)
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
    return proc.stdout.strip()


def _sandbox_copy(src: Path, dst: Path) -> None:
    """Copy a repo into a sandbox with ISOLATED inodes, fast.

    Darwin/APFS: `cp -c` clonefile — instant copy-on-write, new inodes, so
    no write in the sandbox can ever reach the source (review of PR #115
    proved hardlinked object stores CAN be written through). Elsewhere:
    `git clone --no-hardlinks` — slower, equally isolated."""
    if sys.platform == "darwin":
        proc = subprocess.run(["cp", "-Rc", str(src), str(dst)],
                              capture_output=True)
        if proc.returncode == 0:
            # Clone parity: a file copy carries the source's ACTIVE hooks
            # (a real work repo did ship an active pre-push), which would execute
            # foreign code on the coder's sandbox pushes — git clone never
            # copies hooks. Strip them.
            hooks = dst / ".git" / "hooks"
            if hooks.exists():
                shutil.rmtree(hooks)
                hooks.mkdir()
            return
        # cp failed (mid-copy ENOSPC, exotic volume) — clear any partial
        # dst or the fallback clone dies on "destination not empty",
        # masking the real error (review F5).
        if dst.exists():
            shutil.rmtree(dst)
    subprocess.run(["git", "clone", "--no-hardlinks", str(src), str(dst)],
                   check=True, capture_output=True)


def _sandbox_repo(src: Path, work: Path, pin: str, bare: Path) -> Path:
    """Copy *src* to *work* at *pin*, with origin re-pointed at *bare*.

    The push-proof core, extracted so the PRIMARY repo and every LINKED repo of
    a multi-repo spec go through the SAME guard. Duplicating it for linked repos
    was the alternative and it is the worse one: the guard that matters would
    then exist twice and could drift, and the half that drifts is the half that
    hands an agent write access to a real checkout.

    The caller owns the containment assertion (it needs the workdir root), and
    calls `_assert_sandboxed` immediately after — see `_setup_sandbox`.
    """
    _sandbox_copy(src, work)
    # The source may be DIRTY (uncommitted/untracked files ride along in a
    # file-level copy): force the worktree to exactly the pinned commit, clean.
    _git(work, "reset", "--hard", "HEAD" if pin == "HEAD" else pin)
    _git(work, "clean", "-fdx")
    if pin != "HEAD":
        _git(work, "checkout", "--detach", pin)
        # The coder needs a branch to work from. `-B`, not `-b`: a subject repo
        # that already carries a `bench-base` branch made `-b` exit non-zero and
        # crashed the spec at setup with zero tokens spent (observed live on the
        # large-repo tier). Re-pointing it is exactly the intent.
        _git(work, "checkout", "-B", "bench-base")
    _git(work, "config", "user.email", "bench@no_human")
    _git(work, "config", "user.name", "nh-bench")

    subprocess.run(["git", "init", "--bare", str(bare)],
                   check=True, capture_output=True)
    # A copied repo carries the SOURCE's remotes (possibly none, possibly
    # several — the push-proof guard resolves only origin, so a ride-along
    # upstream would be a guard-invisible escape). Remove them ALL, then add
    # origin at the local bare.
    existing = subprocess.run(["git", "remote"], cwd=work,
                              capture_output=True, text=True).stdout.split()
    for r in existing:
        subprocess.run(["git", "remote", "remove", r], cwd=work,
                       capture_output=True)
    subprocess.run(["git", "config", "--unset-all", "remote.pushDefault"],
                   cwd=work, capture_output=True)
    _git(work, "remote", "add", "origin", str(bare))
    return work


def _assert_sandboxed(work: Path, workdir: Path) -> None:
    """HARD GUARD — BEFORE any push. A guard that runs after the push detects an
    escape only once the write has already landed in the real repo."""
    origin = Path(_git(work, "remote", "get-url", "origin")).resolve()
    if not str(origin).startswith(str(workdir.resolve())):
        raise RuntimeError(
            f"push-proofing failed: origin {origin} escapes sandbox {workdir}")


def _setup_linked_sandboxes(spec: BenchTask, workdir: Path) -> list[str]:
    """Sandbox every LINKED repo of a multi-repo spec, and return their paths.

    Empty list for the single-repo specs that are the whole corpus today, so
    nothing about them changes.

    This exists because `Task.linked_repos` is a real product feature (the
    orchestrator opens a branch and a PR per repo) and a bench spec could not
    express it. The dangerous way to add it would have been to hand the Task the
    REAL linked paths: the push-proof guard covers whatever it is pointed at,
    and pointing it only at the primary would leave an agent with write access
    to the operator's other checkouts. Every linked repo therefore gets its own
    sandbox copy, its own workdir-local bare, and the same containment
    assertion, before the Task ever sees a path.
    """
    linked: list[str] = []
    for i, entry in enumerate(spec.linked_repos or []):
        src = Path(str(entry.get("path") or "").strip())
        if not src.is_absolute() or not (src / ".git").exists():
            raise RuntimeError(
                f"linked repo {i} of {spec.id} does not resolve: {src}")
        work = workdir / "linked" / f"{i}-{src.name}"
        work.parent.mkdir(parents=True, exist_ok=True)
        _sandbox_repo(src, work, str(entry.get("pin") or "HEAD"),
                      workdir / f"linked-remote-{i}.git")
        _assert_sandboxed(work, workdir)
        _git(work, "push", "origin", "HEAD")
        linked.append(str(work))
    return linked


def _setup_sandbox(spec: BenchTask, workdir: Path) -> Path:
    """Clone the real repo at the spec's pin; re-point origin to a local bare.

    The sandbox is an APFS clonefile copy (cp -c): instant copy-on-write at
    any size — v8's two crashes were `git clone --no-hardlinks` COPYING a
    3.8GB object store onto a starved disk (exit 128) — with fully ISOLATED
    inodes. Hardlinked clones were rejected in review by experiment: a
    sandboxed `chmod +w` + in-place append writes THROUGH a shared object
    inode into the operator's live repo, and neither the guard nor the
    ref-signature tamper check sees it. Non-APFS platforms fall back to the
    slow-but-isolated copy clone."""
    src = Path(spec.repo.get("path", ""))
    work = _sandbox_repo(src, workdir / "work",
                         spec.repo.get("pin") or "HEAD",
                         workdir / "remote.git")
    _assert_sandboxed(work, workdir)
    _git(work, "push", "origin", "HEAD")

    # Dirty-tree seed — AFTER the commit+push above, so the seeded files are
    # exactly what they claim to be: uncommitted, unpushed working-tree state.
    # The reset+clean+push sequence otherwise PRE-SATISFIES any "make sure
    # everything is committed and pushed, no garbage" request — the harness
    # destroyed the task's own precondition and the row became a guaranteed
    # pass (V3 corpus audit, 2026-08-04; ns-90b6ff3c). Append-when-tracked /
    # create-when-not gives a spec both a genuine modified entry and genuine
    # untracked junk without wholesale overwriting a real file.
    for rel, content in (getattr(spec, "dirty_seed", None) or {}).items():
        target = work / rel
        # The seed writes INSIDE the sandbox only — a spec file is curator
        # data, but "../" would put an uncommitted file on the real machine.
        # Path-aware containment, NOT a string prefix: startswith() let a
        # SIBLING with a shared name prefix through ("../work-evil/pwned.txt"
        # resolves to a path that string-starts with ".../work" — review D2's
        # proven bypass). is_relative_to compares path components.
        if not target.resolve().is_relative_to(work.resolve()):
            raise RuntimeError(
                f"dirty_seed entry escapes the sandbox: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with target.open("a") as fh:
                fh.write(content)
        else:
            target.write_text(content, encoding="utf-8")
    return work


def _ref_signature(repo: Path) -> str:
    """Tamper check for the SOURCE repo: the refs a bench escape would touch.

    NOT all refs. A live repo has automation of its own — background jobs that
    push state to their own branches continuously, outside any namespace the
    agent uses — and comparing every ref made one such push look like a bench
    escape (it crashed two specs on a run where the bench wrote
    nothing). The bench can only ever create refs under the agent's own
    namespaces, so the signature watches exactly those plus the refs a task
    could rewrite: HEAD's branch and any no-human/* or bench-* ref.
    """
    try:
        out = subprocess.run(
            ["git", "for-each-ref",
             "--format=%(refname) %(objectname)",
             "refs/heads/no-human/", "refs/heads/bench-", "refs/heads/nh-"],
            cwd=repo, capture_output=True, text=True).stdout
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        branch = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        return f"{out}\nHEAD {branch} {head}\n"
    except OSError:
        return ""


def _bench_task(spec: BenchTask, work: Path,
                linked: list[str] | None = None) -> Task:
    """Build the orchestrator Task for a spec EXACTLY as the product front door
    would — including kind classification (`nh` runs classify_kind at intake,
    cli/commands.py). Without it every replayed task defaulted to the feature
    pipeline, so review/question/plan specs never reached their read-only
    report terminals (v6 taxonomy, 2026-07-16)."""
    from ..intake.classify import classify_kind

    task = Task.new(spec.title, repo_path=str(work), description=spec.request)
    # CODER-VISIBLE SURFACE — title, request, acceptance_criteria only.
    # `spec.judge_rubric` must NEVER be copied here: the orchestrator renders
    # the Task's criteria into the coder's implement prompt, so a rubric
    # criterion naming the expected insight would hand the agent its own
    # grading key (the dual-audience leak the 2026-08-04 release closed for
    # golden descriptions; review D1 caught it reopening through this field).
    # The rubric joins the criteria only in the judge call in `_score`.
    task.acceptance_criteria = list(spec.acceptance_criteria)
    # SANDBOXED linked paths only — never `spec.linked_repos`, which holds the
    # operator's REAL checkouts. The orchestrator opens a branch and pushes a PR
    # in every linked repo, so putting a real path here would hand the agent
    # write access to a repo no guard is watching. `_setup_linked_sandboxes` has
    # already copied each one and asserted its origin is inside the workdir.
    task.linked_repos = list(linked or [])
    task.kind = classify_kind(task).kind.value
    return task


# Watchdog cadence and how long a cancellation gets to unwind before we stop
# waiting on it — the observed hang was inside subprocess teardown.
_WATCHDOG_POLL_S = 30.0
_WATCHDOG_UNWIND_S = 60.0


def _make_sink(persister, forward, task_id):
    """Build the bench event sink AND the last-event clock it feeds.

    Extracted so the WIRING is testable. Left inline, the one line that
    refreshes the clock could be deleted with every watchdog test still green —
    and deleting it makes the watchdog fire on a spec that is long but ALIVE,
    which is the false-kill direction: a slow success recorded as a capability
    failure.
    """
    last_event = [time.monotonic()]

    def sink(event: dict) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("task_id", task_id)
        last_event[0] = time.monotonic()      # proof of life for the watchdog
        persister.record(event)
        forward(event)

    return sink, last_event


class NorthStarRunner:
    """Mirrors eval.replay.ReplayRunner but for real-repo bench specs."""

    def __init__(self, config: dict, *, backend_factory: BackendFactory,
                 reviewer: Any | None = None, goal_judge: Any | None = None,
                 event_sink: Callable[[dict], None] | None = None):
        self.config = config
        self.backend_factory = backend_factory
        self.reviewer = reviewer
        self.goal_judge = goal_judge
        self._event_sink = event_sink

    class SpecStalled(RuntimeError):
        """A spec emitted no event for longer than the stuck-active threshold."""

    async def _run_with_watchdog(self, orch, task, last_event):
        """`orch.run_task` under the SAME stuck-active policy the server applies.

        The product already has this watchdog — `blockers.stuck_active_minutes`
        (added for the 2026-07-11 reviewer hang) — but it lives in
        `blockers/wake.py`, which only the SERVER runs. `nh bench run` drives
        the orchestrator directly, so nothing was watching, and one hung
        Agent-SDK session stopped an entire run silently and indefinitely.
        Observed live: a spec sat 9 minutes at 0% CPU, no children, no sockets,
        blocked in `__wait4`, and would have sat there forever.

        Same knob and same semantic as wake.py deliberately — NO EVENT for N
        minutes, not total wall clock. A spec that is slow but ALIVE keeps
        emitting, so it is never killed; only silence trips this. Reusing the
        key means the two cannot drift, and <= 0 disables, exactly as there.
        """
        limit_min = float((self.config.get("blockers") or {})
                          .get("stuck_active_minutes", 40))
        runner = asyncio.ensure_future(orch.run_task(task))
        if limit_min <= 0:
            return await runner                      # watchdog disabled
        limit_s = limit_min * 60.0
        while True:
            done, _ = await asyncio.wait({runner}, timeout=_WATCHDOG_POLL_S)
            if done:
                return await runner
            silent_s = time.monotonic() - last_event[0]
            if silent_s >= limit_s:
                runner.cancel()
                # Give the cancellation a bounded chance to unwind. The hang
                # observed live was INSIDE subprocess teardown, so waiting on
                # it forever would reproduce the very defect being fixed.
                # Only the cancel's own CancelledError and the unwind
                # TimeoutError are expected; anything else is a real teardown
                # bug and must not be swallowed under the SpecStalled below.
                with contextlib.suppress(asyncio.CancelledError,
                                         asyncio.TimeoutError):
                    await asyncio.wait_for(runner, timeout=_WATCHDOG_UNWIND_S)
                raise NorthStarRunner.SpecStalled(
                    f"no event for {silent_s / 60:.0f} min "
                    f"(limit {limit_min:.0f}); the agent session hung")

    async def run_one(self, spec: BenchTask, *, workdir: Path) -> BenchScore:
        if not spec.runnable:
            return self._skipped(spec)

        # `runnable` was decided at spec GENERATION time; nothing re-checked it
        # here. A spec whose repo path no longer resolves therefore reached
        # _setup_sandbox, died in `git clone`, and was booked as
        # outcome_status="crashed", goal_satisfied=False — a broken INSTRUMENT
        # scored as a capability failure of the agent. An unavailable repo is a
        # skip, exactly as it is at generation time.
        #
        # Resolve BEFORE building the Path: Path("") is PosixPath("."), and so
        # are "." and "./", all of which would make the .git probe test the
        # RUNNER's own cwd, pass inside any checkout, and sandbox-copy no_human
        # itself as the spec's subject. A relative path is meaningless here for
        # the same reason — the spec records the original session's absolute cwd.
        raw_path = spec.repo.get("path") or ""
        if not str(raw_path).strip():
            return self._skipped(spec, "spec has no repo.path")
        src_repo = Path(str(raw_path).strip())
        if not src_repo.is_absolute():
            return self._skipped(
                spec, f"repo.path is not absolute: {raw_path!r}")
        # .exists(), not .is_dir(): a git worktree or submodule has .git as a
        # FILE, and treating those as missing would skip perfectly good repos.
        if not (src_repo / ".git").exists():
            return self._skipped(spec, f"repo missing at run time: {src_repo}")
        # Hand the VALIDATED path on. _setup_sandbox re-derives
        # Path(spec.repo["path"]) itself, so validating a stripped copy while it
        # cloned the raw one meant a whitespace-padded path passed this guard
        # and then died in `git clone` — this guard's own code producing the
        # crash it exists to prevent.
        spec.repo["path"] = str(src_repo)

        # Every subprocess below runs off-loop: under `--parallel` a sandbox
        # copy (multi-GB cp/clone) or the 300s holdout pytest would otherwise
        # block EVERY in-flight spec's SDK stream — and, worse, freeze the
        # stuck-watchdog clock so a quiet-but-alive spec could be booked as a
        # false SpecStalled crash.
        # The tamper check covers EVERY source repo a multi-repo spec touches,
        # not just the primary — a linked repo is exactly as real as this one.
        sources = [src_repo] + [
            Path(str(e.get("path") or "").strip())
            for e in (spec.linked_repos or [])]
        refs_before = {
            str(p): await asyncio.to_thread(_ref_signature, p) for p in sources}
        work = await asyncio.to_thread(_setup_sandbox, spec, workdir)
        linked = await asyncio.to_thread(_setup_linked_sandboxes, spec, workdir)
        # Is the sandbox even testing ITSELF? For a src/ layout the answer can
        # be no — `import <pkg>` resolves through an editable install pointing
        # at the ORIGINAL tree — and then the agent's own tests grade code it
        # never touched, silently. Advisory: a repo may legitimately import an
        # installed sibling, and a pre-flight must not break the run it
        # protects. Recorded on the score so the question "was this spec's
        # feedback loop pointed at the right tree?" is answerable from the
        # record instead of by archaeology.
        wrong_tree = await asyncio.to_thread(wrong_tree_imports, work)
        for problem in wrong_tree:
            logging.getLogger(__name__).warning(
                "bench %s: %s", spec.id, problem)
        base_sha = await asyncio.to_thread(_git, work, "rev-parse", "HEAD")

        store = await Store(workdir / "bench.db").connect()
        try:
            # Persist the event stream into the sandbox's own bench.db.
            # Supervisor/budget events only exist in this stream; dropping it
            # left the v9 budget-class regression undrillable — whether the
            # 85% wrap-up nudge fired or was ignored was unknowable post hoc.
            task = _bench_task(spec, work, linked)
            async with EventPersister(store, task.id) as persister:
                forward = self._event_sink or (lambda e: None)

                sink, last_event = _make_sink(persister, forward, task.id)

                orch = Orchestrator(
                    store, self.config, self.backend_factory(spec),
                    SlackNotifier(None), reviewer=self.reviewer,
                    event_sink=sink,
                )
                await store.create_task(task)

                t0 = time.monotonic()
                outcome = await self._run_with_watchdog(
                    orch, task, last_event)
                elapsed = time.monotonic() - t0

                attempts = await store.list_attempts(task.id)
            # OUTSIDE the persister context: its __aexit__ drains the buffer,
            # so only here is the stream fully flushed to bench.db. The digest
            # is telemetry, never a verdict — a harvest failure must not
            # downgrade a completed spec to "crashed" (r1 F2).
            try:
                event_digest = _digest_events(
                    await store.list_events(task.id), spec)
            except Exception:  # noqa: BLE001 — telemetry only
                logging.getLogger(__name__).warning(
                    "event digest harvest failed for %s", spec.id)
                event_digest = []
        finally:
            await store.close()

        for p_src, before in refs_before.items():
            if await asyncio.to_thread(_ref_signature, Path(p_src)) != before:
                raise RuntimeError(
                    f"SOURCE REPO REFS CHANGED during bench run of {spec.id} "
                    f"({p_src}) — push-proofing failed; halt the bench")

        return await self._score(spec, outcome, work, base_sha,
                                 attempts, elapsed,
                                 events=event_digest,
                                 wrong_tree=wrong_tree)

    def _skipped(self, spec: BenchTask, reason: str = "") -> BenchScore:
        orig = spec.original or {}
        toks = orig.get("tokens", {}) or {}
        return BenchScore(
            task_id=spec.id, title=spec.title, outcome_status="skipped",
            goal_satisfied=None, escalated_honestly=False, mergeable=None,
            nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=0, nh_wall_clock_s=0.0,
            orig_tokens=int(toks.get("input_tokens", 0)) + int(toks.get("output_tokens", 0)),
            orig_cache_tokens=int(toks.get("cache_read_input_tokens", 0)),
            orig_cache_creation_tokens=int(
                toks.get("cache_creation_input_tokens", 0)),
            orig_wall_clock_s=float(orig.get("wall_clock_s", 0.0)),
            orig_corrections=int(orig.get("corrections", 0)),
            expected_escalation=spec.expect_escalation,
            subset=spec.subset,
            project=spec_project_name(spec),
            pin_rederived=spec_pin_rederived(spec),
            # REDACTED. The run-time reasons below name the repo path, which
            # after the repo-map translation is the operator's real local
            # checkout — and this note is rendered into the tracked report.
            # Unredacted it also blocks publication outright: the report
            # writer refuses any rendered artifact containing a /Users/ path,
            # with no --force override, so a single run-time skip would kill
            # an otherwise-clean run at the final write.
            notes=redact_local_path(
                f"skipped: {reason or spec.skip_reason}", spec),
        )

    async def _score(self, spec: BenchTask, outcome, work: Path,
                     base_sha: str, attempts: list[dict],
                     elapsed: float,
                     events: list[dict] | None = None,
                     wrong_tree: list[str] | None = None) -> BenchScore:
        status = outcome.status
        # EVERY registered role, per price class (angle-4 finding: coder-only
        # summation rigged the north-star ratio). The role list is imported,
        # never enumerated here: this comment used to end "planner/supervisor
        # columns pending B2" while the code summed exactly four literals, and
        # the 10%-of-cost target the card publishes is only as honest as the
        # longest of those lists. A role that exists in the schema but not in
        # this sum is spend the benchmark hands the product for free.
        # The per-role breakdown IS the derivation, never a second summation
        # of the same columns: nh_tokens/nh_cache_tokens/nh_cache_creation_
        # tokens below are sums OF `role_tokens`, so the scalar totals and the
        # tier-weighted `nh_priced_tokens` can never disagree with each other.
        role_tokens: dict[str, dict[str, int]] = {}
        for prefix, role in USAGE_ROLES.items():
            fresh_key, read_key, creation_key = usage_columns_for(prefix)
            fresh = sum(int(a.get(fresh_key) or 0) for a in attempts)
            read = sum(int(a.get(read_key) or 0) for a in attempts)
            creation = sum(int(a.get(creation_key) or 0) for a in attempts)
            if fresh or read or creation:
                role_tokens[role] = {"tokens_used": fresh,
                                     "cache_read_tokens": read,
                                     "cache_creation_tokens": creation}

        nh_tokens = sum(v["tokens_used"] for v in role_tokens.values())
        nh_cache = sum(v["cache_read_tokens"] for v in role_tokens.values())
        nh_creation = sum(
            v["cache_creation_tokens"] for v in role_tokens.values())

        # Only for roles that actually spent AND that `is_priced_role` — the
        # one predicate, never `role in PRICED_ROLES` inline. `self.config`
        # is absent on a bare `NorthStarRunner.__new__` (unit tests exercise
        # `_score` that way), so this reads it defensively rather than as
        # `self.config` directly.
        llm_config = (getattr(self, "config", None) or {}).get("llm") or {}
        role_models: dict[str, str] = {}
        for role in role_tokens:
            if not is_priced_role(role):
                continue
            model = llm_config.get(PRICED_ROLES[_canonical_role(role)])
            if model:
                role_models[role] = model

        turns = sum(int(a.get("turns_used") or 0) for a in attempts)
        orig = spec.original or {}
        toks = orig.get("tokens", {}) or {}

        # Prefix, not append: if the sandbox was not testing itself, that fact
        # conditions every other number on the row and a reader must meet it
        # first, not find it after 2000 characters of judge evidence.
        wrong_tree_note = (
            "⚠ sandbox did not test itself: " + "; ".join(wrong_tree) + " — "
            if wrong_tree else "")
        score = BenchScore(
            events=list(events or []),
            task_id=spec.id, title=spec.title, outcome_status=status.value,
            goal_satisfied=None,
            escalated_honestly=status in _HONEST_STOPS,
            mergeable=None,
            nh_tokens=nh_tokens, nh_cache_tokens=nh_cache,
            nh_cache_creation_tokens=nh_creation, nh_turns=turns,
            nh_role_tokens=role_tokens, nh_role_models=role_models,
            nh_wall_clock_s=elapsed,
            orig_tokens=int(toks.get("input_tokens", 0)) + int(toks.get("output_tokens", 0)),
            orig_cache_tokens=int(toks.get("cache_read_input_tokens", 0)),
            orig_cache_creation_tokens=int(
                toks.get("cache_creation_input_tokens", 0)),
            orig_wall_clock_s=float(orig.get("wall_clock_s", 0.0)),
            orig_corrections=int(orig.get("corrections", 0)),
            expected_escalation=spec.expect_escalation,
            subset=spec.subset,
            project=spec_project_name(spec),
            pin_rederived=spec_pin_rederived(spec),
            # Set unconditionally (they're the run's cost either way, same
            # `attempts`/`nh_tokens` this call already computed); the report
            # decides whether to SHOW them by filtering on escalated_honestly.
            attempts_before_escalation=len(attempts),
            tokens_before_escalation=nh_tokens,
        )

        if spec.expect_escalation:
            # Credential-gated task: CORRECT = honest stop, never a faked PR.
            score.goal_satisfied = score.escalated_honestly
            score.notes = wrong_tree_note + (
                "honestly escalated as expected" if score.goal_satisfied
                else f"expected escalation, got {status.value}")
            return score

        if status not in _GATE_STATES:
            score.goal_satisfied = False
            score.notes = (wrong_tree_note
                           + f"did not reach the human gate ({status.value})")
            return score

        # Put the work dir on the coder's PR branch. The orchestrator commits the
        # coder's work there and can leave HEAD at the base pin — so the holdout
        # tests AND the judge's own filesystem checks (ls / git status in
        # repo_path) would otherwise see BASE and contradict the real work
        # (verified live: ns-f5cb4cb0's 3 review files appear only after checking
        # out the PR branch; the judge ran `ls`/`git status`, saw none, and
        # false-scored a "fabrication"). #92 fixed the agent_diff; this makes the
        # holdout + judge repo view consistent with it. FORCE the checkout: the
        # deliverable is COMMITTED on the branch, so uncommitted sandbox cruft that
        # could block a plain checkout is not part of the PR — a silent
        # keep-HEAD-at-base would reintroduce the exact empty-view bug (review D1).
        diff_ref = await asyncio.to_thread(self._agent_diff_ref, outcome, work)
        if diff_ref != "HEAD":
            r = await asyncio.to_thread(
                subprocess.run,
                ["git", "checkout", "-q", "-f", "--detach", diff_ref],
                cwd=work, capture_output=True)
            if r.returncode != 0:
                logging.getLogger(__name__).warning(
                    "bench: could not checkout %s in %s — judge repo view is stale, "
                    "may under-score: %s", diff_ref, work, r.stderr.decode()[:200])
        # Judge FIRST, on the clean coder work — BEFORE _holdout_ok writes its own
        # tests/bench_holdout/ file into the tree, which a judge told to `ls`/`git
        # status` would otherwise see and puzzle over (review D2).
        verdict = None
        if self.goal_judge is not None:
            # Diff base against the PR branch (robust even if the checkout failed —
            # the coder's commits live there regardless). The judge also sees the
            # agent's REPORT (its answer/review/plan), preferred over the terse
            # status `detail`.
            agent_diff = (await asyncio.to_thread(
                subprocess.run,
                ["git", "diff", base_sha, diff_ref], cwd=work,
                capture_output=True, text=True)).stdout
            # The judge grades on criteria PLUS the judge-only rubric. The
            # rubric exists precisely because acceptance_criteria is dual-
            # audience (it is copied onto the coder's Task in `_bench_task`);
            # this judge call is the ONLY place judge_rubric may be rendered.
            verdict = await self.goal_judge.judge(
                request=spec.request,
                criteria=[*spec.acceptance_criteria, *spec.judge_rubric],
                agent_diff=agent_diff, outcome_status=status.value,
                report=(getattr(outcome, "report", "") or getattr(outcome, "detail", "") or ""),
                repo_path=str(work))
        score.mergeable = await asyncio.to_thread(self._holdout_ok, spec, work)
        if verdict is not None:
            score.unscoreable = bool(getattr(verdict, "unscoreable", False))
            score.goal_satisfied = bool(verdict.satisfied) and \
                score.mergeable in (True, None)
            # 2000, not 400: the drill of every done-but-unsatisfied spec
            # starts from this field, and 400 chars cut ns-7ef821b2's verdict
            # off mid-"BUT ..." — the reason it failed was unrecoverable.
            # Defence in depth: the judge only ever sees the tmp sandbox path,
            # so this is not a known leak channel — but it is the one remaining
            # free-text field that reaches the tracked report, and "is redaction
            # applied everywhere notes are written" should have one answer.
            score.notes = (wrong_tree_note
                           + redact_local_path(verdict.evidence, spec)[:2000])
        else:
            score.goal_satisfied = score.mergeable in (True, None)
            score.notes = wrong_tree_note + "no judge injected; holdout-only scoring"
        return score

    @staticmethod
    def _agent_diff_ref(outcome, work: Path) -> str:
        """The ref to diff the agent's work against. The orchestrator can leave
        the work-dir HEAD at base while the coder's commits live on the PR branch,
        so prefer the recorded ``pr_branch`` (local ref, then ``origin/``) when it
        exists — else fall back to HEAD. Fixes false-empty diffs that made the
        judge score a real PR as a fabrication."""
        ctx = getattr(getattr(outcome, "task", None), "context", None) or {}
        pr_branch = ctx.get("pr_branch")
        if pr_branch:
            for cand in (pr_branch, f"origin/{pr_branch}"):
                if subprocess.run(["git", "rev-parse", "--verify", cand],
                                  cwd=work, capture_output=True).returncode == 0:
                    return cand
            # Recorded but unresolvable — WARN rather than silently fall back to
            # HEAD, which would re-mask the empty-diff false-failure (review nit).
            logging.getLogger(__name__).warning(
                "bench: pr_branch %r not resolvable in %s — diffing HEAD (may "
                "under-capture the deliverable)", pr_branch, work)
        return "HEAD"

    def _holdout_ok(self, spec: BenchTask, work: Path) -> bool | None:
        if not spec.holdout:
            return None
        # Same bounded held-out mechanics as replay._mergeable.
        import os
        import signal as _signal
        import sys
        held = work / "tests" / "bench_holdout"
        held.mkdir(parents=True, exist_ok=True)
        f = held / "test_bench_holdout.py"
        f.write_text(spec.holdout, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", str(f)], cwd=work,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "PYTHONPATH": str(work)},
            start_new_session=True)
        try:
            proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            proc.communicate()
            return False
        return proc.returncode == 0
