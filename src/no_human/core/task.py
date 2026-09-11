"""Task model, status enum, and the legal state-transition map (PLAN.md 4.3)."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


PRIORITY_ORDER = ("high", "medium", "low")   # dispatch order, first = first
DEFAULT_PRIORITY = "medium"


def normalise_priority(value: Any) -> str:
    """Validate and canonicalise a priority token.

    Empty/None maps to the default; anything not in PRIORITY_ORDER raises
    ValueError so writers (CLI/API) can reject junk before it hits the DB.
    """
    if value is None:
        return DEFAULT_PRIORITY
    token = str(value).strip().lower()
    if not token:
        return DEFAULT_PRIORITY
    if token not in PRIORITY_ORDER:
        raise ValueError(
            f"unknown priority {value!r}; one of {', '.join(PRIORITY_ORDER)}"
        )
    return token


def commit_subject(title: str, external_id: str | None, prefix: str = "") -> str:
    """The commit subject / PR title a task's *title* produces.

    The ONE home for "what subject does this title produce" — both
    ``Orchestrator._commit_message`` (the pipeline's own commit) and
    ``nh task retitle`` (correcting a PR title in step with a corrected
    title) build the subject through this function, so the two cannot drift.
    """
    ext = ""
    if external_id and not (title or "").lstrip().startswith(f"{external_id}:"):
        ext = f"{external_id}: "
    return f"{prefix}{ext}{title}"


def priority_rank(value: Any) -> int:
    """Sort key for dispatch ordering; never raises — fails soft to medium's rank.

    Used on the claim path, where a hand-written/legacy DB row with junk in
    the priority column must not break scheduling.
    """
    if value is None:
        return PRIORITY_ORDER.index(DEFAULT_PRIORITY)
    token = str(value).strip().lower()
    if token not in PRIORITY_ORDER:
        return PRIORITY_ORDER.index(DEFAULT_PRIORITY)
    return PRIORITY_ORDER.index(token)


class TaskStatus(str, Enum):
    PENDING = "pending"
    CONTEXT = "context"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    TESTING = "testing"
    AWAITING_APPROVAL = "awaiting_approval"
    # compound parent — parked while sub-tasks execute (frees worker slot)
    COMPOUND_PARENT = "compound_parent"
    # off-ramps
    BLOCKED = "blocked"
    AWAITING_INPUT = "awaiting_input"
    PAUSED_QUOTA = "paused_quota"
    ESCALATED = "escalated"
    DONE = "done"
    FAILED = "failed"


# The happy-path spine, in order. Used to advance the loop linearly.
MAIN_FLOW: tuple[TaskStatus, ...] = (
    TaskStatus.PENDING,
    TaskStatus.CONTEXT,
    TaskStatus.PLANNING,
    TaskStatus.IMPLEMENTING,
    TaskStatus.REVIEWING,
    TaskStatus.TESTING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.DONE,
)

TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED}
)

# Off-ramp states reachable from any active working state (Part 22).
_OFF_RAMPS: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.PAUSED_QUOTA,
        TaskStatus.ESCALATED,
        TaskStatus.FAILED,
    }
)

_ACTIVE: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.CONTEXT,
        TaskStatus.PLANNING,
        TaskStatus.IMPLEMENTING,
        TaskStatus.REVIEWING,
        TaskStatus.TESTING,
    }
)


def _allowed_transitions() -> dict[TaskStatus, frozenset[TaskStatus]]:
    """Build the legal transition map.

    Rules:
    - Each main-flow state may advance to the next main-flow state.
    - Any active working state may drop to any off-ramp.
    - Parked off-ramps (blocked/awaiting_input/paused_quota) may resume to any
      active state (the watcher / a human reply re-enters mid-flow).
    - escalated may resume too (after a human answers); failed is terminal.
    - awaiting_approval -> done (human approves) or implementing (sent back).
    """
    table: dict[TaskStatus, set[TaskStatus]] = {s: set() for s in TaskStatus}

    for i, state in enumerate(MAIN_FLOW[:-1]):
        table[state].add(MAIN_FLOW[i + 1])

    for state in _ACTIVE:
        table[state] |= _OFF_RAMPS

    # PENDING can fail immediately (e.g. no repo_path set).
    table[TaskStatus.PENDING].add(TaskStatus.FAILED)

    # code_review tasks skip planning/implementing and go straight to reviewing.
    table[TaskStatus.CONTEXT].add(TaskStatus.REVIEWING)
    # code_review completes directly after review (no approval gate).
    table[TaskStatus.REVIEWING].add(TaskStatus.DONE)

    # reviewing can loop back to implementing when the gate fails (within bounds)
    table[TaskStatus.REVIEWING].add(TaskStatus.IMPLEMENTING)
    table[TaskStatus.TESTING].add(TaskStatus.IMPLEMENTING)

    # implementing -> testing: a review verdict can be reached while the row
    # still reads IMPLEMENTING — the review runs INSIDE the implement round
    # (_run_attempt), and the handle regresses to the row's status whenever
    # Store.update_task refreshes it (db.py) or a second process's STARTUP
    # orphan sweep (Scheduler._recover_orphans(startup=True), core/
    # scheduler.py) writes IMPLEMENTING with validate=False, having found the
    # row REVIEWING with no evidence of who — or whether anyone — still owned
    # it. Incident 6408aba0 (2026-08-19): review PASSED at 17:35:57, a second
    # process's startup sweep requeued the row one second later, and a fully
    # reviewed change was discarded. (`WakeWatcher._resume`, blockers/
    # wake.py:720, also writes IMPLEMENTING with validate=False at :799, but
    # only for a task it is actually resuming from a genuine park — it was
    # not the writer in this incident.) Skipping REVIEWING is not a skipped
    # review here — REVIEWING is a reporting state for work that already
    # happened.
    table[TaskStatus.IMPLEMENTING].add(TaskStatus.TESTING)

    # NOTE: IMPLEMENTING -> DONE and TESTING -> DONE are deliberately NOT
    # added here. `Store.reconcile_landed_orphan` (core/db.py) completes an
    # orphaned-but-landed row through its OWN narrower gate
    # (`assert_landed_reconciliation` / `LANDED_RECONCILABLE` below), not
    # through this general map — widening this map instead would also make
    # IMPLEMENTING/TESTING -> DONE legal for `Orchestrator._advance_after_
    # review`'s plain `set_status(task, target)` call, defeating the guard
    # `tests/test_post_review_transition_6408aba0.py::
    # test_recovery_never_launders_an_illegal_jump` exists to enforce: a
    # post-review target outside the two post-review states must still be
    # refused. See Scheduler._reconcile_landed_orphan (core/scheduler.py).

    # NOTE: FAILED -> DONE is likewise deliberately NOT added here.
    # `Store.reconcile_landed_terminal` (core/db.py) completes a TERMINAL
    # failed/cancelled row whose recorded work is provably on the base
    # branch through its OWN narrower gate
    # (`assert_terminal_landed_reconciliation` /
    # `TERMINAL_LANDED_RECONCILABLE` below) — widening this map instead
    # would make FAILED -> DONE legal for every plain `set_status(task,
    # DONE)` call, which is exactly the "resurrect a row without reachable
    # evidence" failure mode this feature must not introduce. See
    # `Scheduler._reconcile_landed_terminal` (core/scheduler.py).

    # approval gate
    table[TaskStatus.AWAITING_APPROVAL].add(TaskStatus.IMPLEMENTING)  # sent back
    table[TaskStatus.AWAITING_APPROVAL] |= _OFF_RAMPS

    resumable = {
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.PAUSED_QUOTA,
        TaskStatus.ESCALATED,
    }
    for state in resumable:
        table[state] |= _ACTIVE
        table[state].add(TaskStatus.FAILED)
        table[state].add(TaskStatus.PENDING)  # a parked task can resume to pending

    # Compound parent: implementing → compound_parent (park),
    # compound_parent → done / failed / escalated. Nothing creates these
    # rows any more — the LeadAgent decomposition subsystem that wrote them
    # was deleted 2026-08-12 (operator decision A1). These transitions are
    # retained only so historical rows written before the deletion still
    # validate/round-trip; see
    # tests/test_db.py::test_historical_compound_parent_and_subtask_rows_still_load.
    table[TaskStatus.IMPLEMENTING].add(TaskStatus.COMPOUND_PARENT)
    table[TaskStatus.COMPOUND_PARENT] |= {
        TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ESCALATED,
    }

    return {k: frozenset(v) for k, v in table.items()}


ALLOWED_TRANSITIONS = _allowed_transitions()


class IllegalTransition(ValueError):
    """Raised when a state change is not in the allowed map."""


def can_transition(src: TaskStatus, dst: TaskStatus) -> bool:
    if src == dst:
        return True
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: TaskStatus, dst: TaskStatus) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed")


#: Statuses from which `Store.reconcile_landed_orphan` may complete a row
#: whose recorded work is provably on the base branch. CONTEXT/PLANNING are
#: deliberately absent: no attempt of theirs has committed work, so there is
#: nothing to reconcile — those orphans keep today's requeue exactly.
LANDED_RECONCILABLE: frozenset[TaskStatus] = frozenset({
    TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
    TaskStatus.TESTING, TaskStatus.AWAITING_APPROVAL,
})


def assert_landed_reconciliation(src: TaskStatus) -> None:
    """Raise `IllegalTransition` unless *src* is one of `LANDED_RECONCILABLE`.

    A second, narrower gate than `assert_transition` — REVIEWING -> DONE and
    AWAITING_APPROVAL -> DONE are already legal main-flow/approval edges, but
    legal there does not mean "this attempt's work landed while orphaned";
    this asserts the reconciliation's own precondition, independent of
    whether the destination edge happens to be allowed for other reasons.
    """
    if src not in LANDED_RECONCILABLE:
        raise IllegalTransition(
            f"{src.value} -> done is not a landed reconciliation")


#: Statuses from which `Store.reconcile_landed_terminal` may complete a row
#: whose recorded work is provably on the base branch, despite the row
#: already being TERMINAL. This is a separate, narrower set from
#: `LANDED_RECONCILABLE` above — it does not add `FAILED` to that one, and
#: it does not add `FAILED -> DONE` to `ALLOWED_TRANSITIONS` either; both
#: stay exactly as they are, so a plain `set_status(task, DONE)` call keeps
#: refusing this edge. The only failed/cancelled shape recognised here is
#: `FAILED` itself — there is no separate `CANCELLED` status (see
#: `core/scheduler.py`; "cancelled" is `FAILED` + `context["cancel_reason"]`).
TERMINAL_LANDED_RECONCILABLE: frozenset[TaskStatus] = frozenset({
    TaskStatus.FAILED,
})


def assert_terminal_landed_reconciliation(src: TaskStatus) -> None:
    """Raise `IllegalTransition` unless *src* is `FAILED`.

    The terminal-row twin of `assert_landed_reconciliation`: a task that
    already went failed (with or without a `cancel_reason`), but whose
    recorded work is verifiably reachable from the default branch, is
    reconciled to DONE ONLY through this gate — never by widening
    `ALLOWED_TRANSITIONS` or `LANDED_RECONCILABLE`.
    """
    if src not in TERMINAL_LANDED_RECONCILABLE:
        raise IllegalTransition(
            f"{src.value} -> done is not a terminal landed reconciliation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Extension-less filenames that are still real paths (lowercased basenames).
_EXTENSIONLESS_PATHS = frozenset({
    "jenkinsfile", "makefile", "dockerfile", "rakefile", "gemfile",
    "procfile", "license", "readme", "vagrantfile", "brewfile",
})

#: Suffixes that make a token a file path. Union of the repo's existing
#: lists — agent/lint_hook.py:_CODE_EXTS (source) and
#: agent/guard.py:_WHOLE_READ_OK_EXTS (data/config/doc) — plus the shapes
#: this repo's own plans name (.pyi, .sh, .cfg, .env, .tf, .proto, ...).
_KNOWN_EXTENSIONS = frozenset({
    # source (lint_hook.py:_CODE_EXTS)
    "py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "java", "go", "rs",
    "rb", "php", "c", "cc", "cpp", "h", "hpp", "cs", "kt", "swift", "scala",
    "sh", "bash", "zsh", "ps1", "bat",
    # data/config/doc (guard.py:_WHOLE_READ_OK_EXTS)
    "json", "yaml", "yml", "toml", "lock", "csv", "xml", "sql", "txt", "md",
    "rst", "ini", "cfg", "conf", "env", "properties",
    # web + infra
    "html", "css", "scss", "less", "vue", "svelte", "tf", "proto", "gradle",
    "plist", "service",
})

#: Prose abbreviations that survive decoration-stripping and look
#: extension-ish (e.g. "e.g." -> "e.g" after trailing-punctuation strip).
#: Belt-and-braces on top of the extension allowlist.
_PROSE_ABBREVIATIONS = frozenset({
    "e.g", "eg", "i.e", "ie", "etc", "vs", "cf", "no", "nos", "fig", "eq",
    "al", "approx", "ca", "ver", "vol", "ch", "sec", "pp", "resp",
})

#: Two-segment slash idioms that are prose, not paths.
_PROSE_SLASHES = frozenset({
    "and/or", "n/a", "i/o", "w/o", "yes/no", "read/write", "input/output",
    "tcp/ip", "client/server", "pass/fail",
})


def _path_candidate(item: str) -> str | None:
    """First path-shaped whitespace token in *item*, or ``None``.

    A token is path-shaped when it is a known extension-less filename
    (``Jenkinsfile``, ``Makefile``, ...), has a known code/config/doc
    extension on a plausible basename, starts with a leading dot
    (``.gitignore``), or contains a ``/`` with at least one real (longer
    than two chars) segment — that last check exists so a short
    slash-separated abbreviation like ``N/A`` is not mistaken for a path.
    A path requires a ``/`` or a known extension on a plausible basename;
    a bare prose abbreviation that merely contains a dot (``v2.1``,
    ``e.g.``, ``No.4``, ``i.e.``) is never a path, even though it survives
    decoration-stripping and looks extension-ish. Markdown decoration
    (backticks, ``*``, ``_``, quotes, trailing punctuation) is stripped
    before the check.
    """
    for tok in item.split():
        cleaned = tok.strip("`*_'\"").rstrip(".,:;()")
        if not cleaned:
            continue
        low = cleaned.lower()
        if low.rstrip(".") in _PROSE_ABBREVIATIONS:
            continue
        base = cleaned.rsplit("/", 1)[-1]
        if base.lower() in _EXTENSIONLESS_PATHS:
            return cleaned
        if "." in base:
            stem, _, suffix = base.rpartition(".")
            if (
                stem
                and suffix.isalpha()
                and suffix.lower() in _KNOWN_EXTENSIONS
                and re.match(r"^[A-Za-z0-9_.+\-]+$", stem)
            ):
                return cleaned
        if cleaned.startswith(".") and re.match(r"^[A-Za-z0-9_-]{2,}$", cleaned[1:]):
            return cleaned
        if (
            "/" in cleaned
            and low not in _PROSE_SLASHES
            and any(len(seg) > 2 for seg in cleaned.split("/"))
        ):
            return cleaned
    return None


def _looks_like_path(item: str) -> bool:
    """True when *item* contains at least one path-shaped token.

    Used both to pick the right cell out of a markdown-table row and as a
    universal final filter on ``files_to_change`` — junk like ``'#'``,
    bare row numbers, or blank cells must never survive into that field
    regardless of which branch produced it (empty is safer than junk: see
    the ``commit_paths`` incident class — consumers scope real decisions by
    this list).
    """
    return _path_candidate(item) is not None


@dataclass
class TaskSpec:
    """Structured spec extracted from the planner's output."""
    files_to_change: list[str] = field(default_factory=list)
    approach: str = ""
    test_plan: str = ""
    out_of_scope: list[str] = field(default_factory=list)
    verification: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_to_change": self.files_to_change,
            "approach": self.approach,
            "test_plan": self.test_plan,
            "out_of_scope": self.out_of_scope,
            "verification": self.verification,
        }

    @staticmethod
    def from_plan(plan_text: str) -> "TaskSpec":
        """Parse structured sections from the planner's markdown output."""
        sections: dict[str, str] = {}
        current: str | None = None
        lines: list[str] = []
        for line in plan_text.splitlines():
            m = re.match(r"^##\s+(.+)", line)
            if m:
                if current:
                    sections[current] = "\n".join(lines).strip()
                current = m.group(1).strip().upper()
                lines = []
            else:
                lines.append(line)
        if current:
            sections[current] = "\n".join(lines).strip()

        def _list_items(text: str) -> list[str]:
            items = []
            for l in text.splitlines():
                s = l.strip()
                # Skip markdown horizontal rules ("---", "***", ...) — these
                # separate sections in the planner's output and would otherwise
                # be mis-parsed as an empty bullet item.
                if re.match(r"^[-*_]{3,}$", s):
                    continue
                if s.startswith(("-", "*")):
                    item = s.lstrip("-*").strip()
                    if item:
                        items.append(item)
            return items

        def _table_files(text: str) -> list[str]:
            """Extract file paths from a markdown table's FIRST path-shaped cell.

            The planner sometimes emits FILES TO CHANGE/CREATE as a table —
            `| File | Change |` (path in column 0) or `| # | Path | Change |`
            (index in column 0, path in column 1). Picking `cells[0]`
            unconditionally captured the index column ('#', '1', '2', ...)
            for the latter shape, so instead pick whichever cell in the row
            is path-shaped.
            """
            files = []
            for l in text.splitlines():
                s = l.strip()
                if not s.startswith("|"):
                    continue
                if re.match(r"^\|[\s:-]+\|", s):
                    continue  # separator row, e.g. |------|------|
                cells = [c.strip().strip("`").strip() for c in s.strip("|").split("|")]
                if not cells:
                    continue
                if cells[0].lower() in ("file", "files", "path", "#"):
                    continue  # header row (belt-and-braces on top of the shape filter)
                match = next((c for c in cells if _looks_like_path(c)), None)
                if match:
                    files.append(match.replace("`", ""))
            return files

        files_section = sections.get("FILES TO CHANGE/CREATE", "")
        # Junk ('#', bare row numbers, blank cells) must never enter
        # files_to_change regardless of source format — filter each branch,
        # and only fall through to the table branch when the bullet-list
        # branch yielded nothing usable (not merely nothing at all).
        #
        # BLAST RADIUS (grep-verified, `grep -rn files_to_change src/ tests/`):
        # table-shaped plans used to yield junk ('#', '1', '2', ...) here, so
        # every consumer below was effectively inert or wrong for that shape.
        # This fix changes their behaviour for table-shaped plans specifically
        # (bullet-list plans are unchanged — see the parametrized control
        # test): scope_guard.py:113 iterates files_to_change to scope its
        # checks and now sees real paths instead of digits, so it goes from
        # inert to ACTIVE on table-shaped plans; complexity.py:222's
        # `files_to_change > 4` tier signal now counts real path entries
        # instead of table-row markers, so the tier it reports for a
        # table-shaped repro case can shift. Nothing found scopes a commit or
        # skips validation by this field directly (no `commit_paths`-style
        # write path off of it) — the exposure is these two read-only
        # signals, not commit scoping.
        files_to_change = [f for f in _list_items(files_section) if _looks_like_path(f)]
        if not files_to_change:
            files_to_change = [f for f in _table_files(files_section) if _looks_like_path(f)]

        return TaskSpec(
            files_to_change=files_to_change,
            approach=sections.get("APPROACH", ""),
            test_plan=sections.get("TEST PLAN", ""),
            out_of_scope=_list_items(sections.get("OUT OF SCOPE", "")),
            verification=sections.get("VERIFICATION", ""),
        )


@dataclass
class Task:
    id: str
    source: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    external_id: str | None = None
    description: str | None = None
    requirements: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    repo_path: str | None = None
    kind: str = "feature"           # WS-A task type: feature|bugfix|ci_fix|traceability|test_gap
    linked_repos: list[str] = field(default_factory=list)  # WS-E: additional repo paths
    blocker: dict[str, Any] | None = None
    wake_check_at: str | None = None
    priority: str = "medium"
    context: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    # A follow-up task's link to the task it continues. Sibling link ONLY —
    # unlike `parent_id` (the LeadAgent compound-child relation the orchestrator
    # still knows about), nothing schedules or aggregates on this column; it
    # exists so the drawer/API can say "follows <task>" without the follow-up
    # being mistaken for a sub-task (see this file's COMPOUND_PARENT note above,
    # ~line 170: nothing creates new compound-parent rows any more, but the
    # transition table stays for historical rows — a follow-up must not reuse
    # that relation).
    follows_id: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def new(title: str, *, source: str = "freeform", repo_path: str | None = None,
            description: str | None = None, external_id: str | None = None,
            kind: str = "feature", parent_id: str | None = None,
            follows_id: str | None = None) -> "Task":
        return Task(
            id=uuid.uuid4().hex,
            source=source,
            title=title,
            description=description,
            repo_path=repo_path,
            external_id=external_id,
            kind=kind,
            parent_id=parent_id,
            follows_id=follows_id,
        )

    def to_row(self) -> dict[str, Any]:
        """Serialize to the SQLite column shape (JSON-encode the lists/dicts)."""
        return {
            "id": self.id,
            "external_id": self.external_id,
            "source": self.source,
            "title": self.title,
            "description": self.description,
            "requirements": json.dumps(self.requirements),
            "acceptance_criteria": json.dumps(self.acceptance_criteria),
            "repo_path": self.repo_path,
            "kind": self.kind,
            "linked_repos": json.dumps(self.linked_repos),
            "status": self.status.value,
            "blocker": json.dumps(self.blocker) if self.blocker else None,
            "wake_check_at": self.wake_check_at,
            "priority": self.priority,
            "context": json.dumps(self.context),
            "plan": json.dumps(self.plan),
            "config": json.dumps(self.config),
            "parent_id": self.parent_id,
            "follows_id": self.follows_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_row(row: dict[str, Any]) -> "Task":
        return Task(
            id=row["id"],
            external_id=row["external_id"],
            source=row["source"],
            title=row["title"],
            description=row["description"],
            requirements=json.loads(row["requirements"] or "[]"),
            acceptance_criteria=json.loads(row["acceptance_criteria"] or "[]"),
            repo_path=row["repo_path"],
            kind=row.get("kind") or "feature",
            linked_repos=json.loads(row.get("linked_repos") or "[]"),
            status=TaskStatus(row["status"]),
            blocker=json.loads(row["blocker"]) if row["blocker"] else None,
            wake_check_at=row["wake_check_at"],
            priority=row["priority"] or DEFAULT_PRIORITY,
            context=json.loads(row["context"] or "{}"),
            plan=json.loads(row["plan"] or "{}"),
            config=json.loads(row["config"] or "{}"),
            parent_id=row.get("parent_id"),
            follows_id=row.get("follows_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
