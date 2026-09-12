"""North-star benchmark task specs (plan i-need-yo-to-wobbly-falcon, Task A2).

A ``BenchTask`` freezes ONE real historical conversation into a replayable task
spec. The no-cheating rule is structural: the builder copies ONLY the initial
user request into the spec — the rest of the source session is consumed solely
for the ``original`` economics block (tokens/duration/corrections), never for
content. Optional hand-curated fields (acceptance criteria, held-out tests) are
leak-linted against the source session's assistant text so a curator cannot
smuggle the original solution into the spec.

Specs live as YAML under ``eval/northstar_tasks/`` (git-tracked, PR-reviewed
like golden tasks). The runner (Task A3) replays them through the real
Orchestrator in a push-proof sandbox.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from ..history.extractor import Transcript

log = logging.getLogger(__name__)

NORTHSTAR_DIR = Path(__file__).resolve().parents[3] / "eval" / "northstar_tasks"
# The raw built corpus contains VERBATIM operator conversations (titles,
# requests, real repo paths — personal and enterprise). It is never committed:
# `generated/` is gitignored. Only hand-curated specs (the core subset, moved
# up into eval/northstar_tasks/ and PR-reviewed like golden tasks) are tracked.
GENERATED_DIR = NORTHSTAR_DIR / "generated"
# Local, GITIGNORED translation from the vendor-neutral repo paths carried in
# tracked specs to the real checkouts on THIS machine. Tracked specs must stay
# vendor-neutral (commit 496233e scrubbed them), but the scrub rewrote
# `repo.path` to values that resolve nowhere, so every scrubbed spec died at
# clone time and scored as a capability failure. This map restores instrument
# validity WITHOUT putting the real names back into git.
REPO_MAP_PATH = NORTHSTAR_DIR.parent / "repo_map.yaml"

_WORD = re.compile(r"[a-z0-9_]+")


def load_repo_map(path: Path | None = None) -> dict[str, str]:
    """Load the local spec-path → real-path map. Absent file = ``{}`` (no-op).

    Raises ``ValueError`` for STRUCTURAL problems only (malformed YAML, a
    non-absolute path on either side). Whether a mapped target actually EXISTS
    is deliberately not fatal here, so that one stale entry cannot kill a whole
    run. ``check_repo_map`` reports those instead, and the CLI prints them
    before a run starts.

    Honest note on the safety net, kept current deliberately: run-time
    skip-on-missing-repo IS now in place, so a target that does not exist is
    SKIPPED rather than booked as a crashed spec. Skipping alone would be a
    FAVOURABLE bias — a skipped spec leaves the success denominator entirely,
    so a corpus that half fails to resolve scores HIGHER. The counterweight is
    the unmeasured-corpus rule (``MAX_UNMEASURED_FRACTION`` via
    ``unmeasured_specs``), which landed first, deliberately, and refuses such a
    run in BOTH the publish refusal and the regression gate. Both nets are in
    place; neither is sufficient alone.

    ``check_repo_map`` is therefore ADVISORY only: the CLI prints what will not
    resolve and then runs anyway. It buys the operator a chance to abort in the
    first seconds instead of discovering it after a night of quota; it does not
    stop the run. Do not read it as a guard.
    """
    p = Path(path) if path is not None else REPO_MAP_PATH
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{p}: malformed YAML — {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a mapping of spec-path → real-path")
    mapping: dict[str, str] = {}
    for src, dst in raw.items():
        src, dst = str(src).strip(), str(dst).strip()
        if not Path(src).is_absolute() or not Path(dst).is_absolute():
            raise ValueError(
                f"{p}: both sides must be absolute paths, got {src!r} → {dst!r}")
        # normpath, not just rstrip: Path() collapses `//` and `/./`
        # before an argv is built, so an unnormalised target would make
        # redact_local_path's exact-substring replace a silent no-op.
        mapping[os.path.normpath(src)] = os.path.normpath(dst)
    return mapping


def check_repo_map(tasks: list["BenchTask"]) -> list[str]:
    """Pre-flight the corpus: which spec repos will not resolve on this machine.

    Returns one human-readable line per problem; empty when everything it CAN
    check resolves (a spec with no pin, or `pin: HEAD`, is not discriminable —
    see below — so it is not checked at all).

    Non-fatal for a mechanical reason, not because the result is fine to read:
    an unresolvable repo is SKIPPED, and a skipped spec leaves the success
    denominator, so a run started with these warnings OVER-reports — it scores
    only the specs healthy enough to resolve. That is the dangerous direction:
    the number looks BETTER the more broken the corpus is. The right response is
    to fix the map and start again; the check exists so that decision costs
    seconds instead of a night of quota.

    Two failure modes this exists to make loud:

    - **No map at all.** ``eval/repo_map.yaml`` is gitignored, so it exists only
      in the checkout where it was written. Bench runs are routinely launched
      from a ``git worktree``, where it is simply absent — every scrubbed spec
      then fails to resolve and the run reads as a model regression caused by
      an operational accident, with no output saying so.
    - **A target that exists but is the WRONG repo.** A typo'd path that happens
      to be a real checkout yields confident, meaningless scores. The pin is the
      cheap discriminator: a repo that does not contain the spec's pinned commit
      is not the repo the spec was recorded against.
    """
    problems: list[str] = []
    for t in tasks:
        # Strip and absolute-check EXACTLY as the run-time guard does. They
        # disagreed in both directions: a padded path was reported here and
        # then ran fine, and a dot-relative path was reported CLEAN here and
        # then skipped at run time. A silent pre-flight followed by a skip is
        # precisely the surprise this function exists to prevent.
        raw = str(t.repo.get("path") or "").strip()
        # A non-runnable spec routes to _skipped and is never cloned, so it can
        # never crash on a bad path; counting it only inflates the headline.
        if not raw or not t.runnable:
            continue
        p = Path(raw)
        if not p.is_absolute():
            problems.append(f"{t.id}: repo path is not absolute — {raw}")
        elif not p.is_dir():
            problems.append(f"{t.id}: repo does not exist here — {raw}")
        elif not (p / ".git").exists():
            problems.append(f"{t.id}: not a git repo — {raw}")
        else:
            pin = str(t.repo.get("pin") or "")
            # `pin: HEAD` (build_bench_tasks' fallback) and an empty pin are NOT
            # discriminating — `HEAD^{commit}` resolves in any non-empty repo,
            # so a wrong-but-real checkout passes. Those specs are unchecked
            # rather than falsely reassuring.
            if not pin or pin == "HEAD":
                continue
            try:
                rc = subprocess.run(
                    ["git", "-C", str(p), "cat-file", "-e", f"{pin}^{{commit}}"],
                    capture_output=True, timeout=5).returncode
            except subprocess.TimeoutExpired:
                # Distinct from the errors below: a timeout means the
                # wrong-repo discriminator silently did not run, which must not
                # look like a pass on the cold mount the 5s cap was set for.
                problems.append(
                    f"{t.id}: pin probe timed out — repo NOT verified")
                continue
            except (OSError, subprocess.SubprocessError):
                # A pre-flight must never be what breaks the run it protects:
                # a missing git binary is not a verdict about the spec.
                continue
            if rc != 0:
                problems.append(
                    f"{t.id}: {raw} does not contain pin {pin[:12]} — "
                    f"wrong repo, or the commit was garbage-collected")
    return problems


def redact_local_path(text: str, spec: "BenchTask") -> str:
    """Replace the locally-translated repo path with the spec's own path.

    `project` was not the only route from a translated path into a TRACKED
    artifact. A crash note is built from ``str(exc)``, and a failed
    ``_sandbox_copy`` raises ``CalledProcessError`` whose string embeds the full
    argv — ``git clone --no-hardlinks <REAL LOCAL PATH> …``. That note is
    rendered into docs/NORTH_STAR_BENCH.md. Truncation is not a guard: whether a
    real org or repo name survives into git depends only on how long the
    operator's home directory happens to be.

    Fires only when a spec IS mapped: with no map, ``local == original`` and
    this returns unchanged. Applied to crash notes and, as defence in depth, to
    the judge-evidence note on every successfully-scored spec — so the answer
    to "is redaction applied wherever notes are written" is simply yes.
    """
    local = str(spec.repo.get("path") or "")
    original = spec.spec_repo_path or ""
    if not text or not local or local == original:
        return text
    replacement = original or "<repo>"
    # Both the literal map value and its normalised form: an exception's argv
    # carries whatever `Path(...)` produced, which is not necessarily the string
    # the map was written with. The STRIPPED forms are included because the
    # run-time guard validates `raw.strip()` and emits that into the note — so
    # a padded path produced text this function could not match, and an
    # unredacted local path hard-blocks publication with no override.
    stripped = local.strip()
    forms = {local, str(Path(local)), os.path.normpath(local),
             stripped, str(Path(stripped)), os.path.normpath(stripped)}
    for form in forms:
        # Skip forms that are DEGENERATE — nothing but separators, dots and
        # whitespace. Those are the ones that match EVERYTHING: a
        # whitespace-only `local` yields "" and ".", and `text.replace("", x)`
        # splices x between every character (a 30-char note became 765), while
        # "/" rewrites every separator in the text.
        #
        # Keyed on degeneracy, NOT on `os.path.isabs`. Absoluteness looks like
        # the same rule and is not: it also skips a relative-but-REAL path,
        # leaving a local path in a note bound for the tracked report. That is
        # disclosure, not corruption, and it hard-blocks publication.
        #
        # `split()` removes whitespace ANYWHERE, using str's own full
        # definition, so no alternation and no character class can escape.
        # FOUR earlier versions of this guard named a charset narrower than
        # this comment claimed — " .\t\n\r" omitted \v and \f; then
        # `string.whitespace` omitted \xa0 and friends; then
        # `strip(A).strip()` still let " / "'s unicode mirror "\xa0/\xa0"
        # through, because chained strips only reach the ENDS. Enumerating
        # characters kept failing, so this stops enumerating.
        if not "".join(form.split()).strip(os.sep + "."):
            continue
        text = text.replace(form, replacement)
    return text


def spec_project_name(spec: "BenchTask") -> str:
    """The project label for REPORTING — always the spec's own path, never the
    locally-translated one.

    ``project`` is rendered into ``docs/NORTH_STAR_BENCH.md``, which is TRACKED.
    Deriving it from ``repo["path"]`` after translation would write the real
    local checkout's name into git and silently undo the vendor-neutral scrub —
    the exact outcome the repo map exists to avoid.

    SCOPE, so this is not read as a stronger promise than it is: it protects
    TRACKED specs, whose own paths are the scrubbed ones. Specs under
    ``generated/`` (loaded by ``--full``) carry the real cwd by construction —
    they were built from live conversations and were never scrubbed — and the
    per-project table renders every score, not just the core subset. A
    ``--full`` publish therefore still writes real basenames. That is
    pre-existing and out of scope here; it is called out so the next reader
    does not mistake this helper for a corpus-wide guarantee.
    """
    if not spec.spec_repo_path:
        # Unreachable via load_bench_tasks, which always records it — but
        # falling back to the TRANSLATED path would emit the real basename,
        # the exact leak this exists to stop. Match redact_local_path's
        # placeholder instead of guessing.
        return "?" if spec.repo.get("path") else ""
    return Path(spec.spec_repo_path).name


def is_resolvable(spec: "BenchTask") -> bool:
    """Whether *spec*'s repo will actually run HERE: marked ``runnable``, its
    ``repo.path`` (already translated through the repo map by
    ``load_bench_tasks``) is absolute, and it is a real git checkout.

    Deliberately the same structural test as the first three checks in
    ``check_repo_map`` (absolute, exists, has ``.git``) MINUS the pin probe:
    this answers "can we even attempt this spec", not "is it verified against
    the exact recorded commit" — the pin check needs a subprocess per spec and
    exists to catch a wrong-but-real checkout, not to gate selection.
    """
    if not spec.runnable:
        return False
    raw = str(spec.repo.get("path") or "").strip()
    if not raw:
        return False
    p = Path(raw)
    return p.is_absolute() and p.is_dir() and (p / ".git").exists()


def quick_cell(spec: "BenchTask") -> tuple[str, bool, bool, str]:
    """The coverage cell a spec belongs to for the `--quick` iteration tier:
    project × runnable × expect_escalation × original-size bucket.

    The size bucket uses the ORIGINAL session's wall clock (S <10min,
    M <1h, L ≥1h) as a complexity proxy — a spec-file constant, so cells never
    shift between runs and cannot be gamed by our own replay times.
    """
    wall = float((spec.original or {}).get("wall_clock_s", 0) or 0)
    bucket = "S" if wall < 600 else ("M" if wall < 3600 else "L")
    return (spec_project_name(spec), spec.runnable, spec.expect_escalation, bucket)


def select_quick_subset(specs: list["BenchTask"]) -> list["BenchTask"]:
    """One RESOLVABLE representative per coverage cell — the stratified
    `--quick` tier.

    Deterministic and input-order independent (fastest original wall clock in
    the cell, spec id as tie-break), so quick runs are comparable to EACH
    OTHER across time. The pick is deliberately fixed rather than rotated:
    rotation would make consecutive quick runs measure different specs and
    read as regressions. Guard against overfitting to the fixed picks is the
    full-core gate, which every publish still has to pass — a quick card is
    refused as baseline by the existing corpus-coverage machinery.

    Within a cell, only ``is_resolvable`` members are candidates: picking an
    unresolvable spec would pin the tier's one representative to something
    that is guaranteed to skip, silently shrinking `ran` while `total` still
    counts it. A cell whose EVERY member is unresolvable is honestly
    unmeasurable on this machine — it is dropped from the tier (never
    silently: a warning names the cell, the reason, and how many specs were
    dropped) rather than falling back to an unresolvable pick.
    """
    by_cell: dict[tuple, list["BenchTask"]] = {}
    for spec in specs:
        by_cell.setdefault(quick_cell(spec), []).append(spec)

    def sort_key(s: "BenchTask") -> tuple[float, str]:
        return (float((s.original or {}).get("wall_clock_s", 0) or 0), s.id)

    selected: list["BenchTask"] = []
    for cell, members in by_cell.items():
        resolvable = [s for s in members if is_resolvable(s)]
        if not resolvable:
            log.warning(
                "quick tier: all specs in cell %s are unresolvable — "
                "dropping %d spec(s): %s",
                cell, len(members), ", ".join(s.id for s in members))
            continue
        selected.append(min(resolvable, key=sort_key))
    return sorted(selected, key=lambda s: s.id)


def remap_repo_path(repo_path: str, mapping: dict[str, str]) -> str:
    """Translate *repo_path* through *mapping*, longest prefix wins.

    Matching is path-BOUNDARY aware: ``/x/foo`` maps ``/x/foo`` and
    ``/x/foo/sub`` but never ``/x/foobar``. Longest-prefix ordering makes
    the result independent of dict order when one prefix nests inside another
    (``/a/b`` vs ``/a/b/c``). Translation is ONE hop: a target that is itself
    a key is not re-translated, so a map cannot chain or loop.
    """
    if not repo_path or not mapping:
        return repo_path
    candidate = repo_path.rstrip("/")
    for src in sorted(mapping, key=len, reverse=True):
        if candidate == src:
            return mapping[src]
        if candidate.startswith(src + "/"):
            return mapping[src] + candidate[len(src):]
    return repo_path


@dataclass
class BenchTask:
    id: str
    title: str
    request: str                       # verbatim initial user message ONLY
    source: dict[str, Any] = field(default_factory=dict)
    repo: dict[str, Any] = field(default_factory=dict)   # {path, pin, branch}
    #: ADDITIONAL repos this spec spans, same {path, pin, branch} shape.
    #: The product already supports multi-repo tasks (`Task.linked_repos`,
    #: `core/multi_repo.py`: a branch and a PR per repo, and the task only
    #: reaches the human gate once every repo has one) — the bench could
    #: not express it, so that capability was unmeasured. Empty for every
    #: single-repo spec, which is the whole corpus today.
    linked_repos: list[dict[str, Any]] = field(default_factory=list)
    original: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: list[str] = field(default_factory=list)
    #: JUDGE-ONLY grading rubric. `acceptance_criteria` is DUAL-AUDIENCE: the
    #: runner copies it onto the coder's Task (`_bench_task`) and the judge
    #: receives it too — so a criterion that names the expected insight hands
    #: the agent under test its own grading key (the exact leak the golden-set
    #: `adjudication` field fixed on 2026-08-04; ns-600d7a02's criteria briefly
    #: reopened it through this second channel). `judge_rubric` is rendered
    #: ONLY into the judge's evaluation input; it must NEVER be placed on the
    #: Task or any coder/reviewer-visible surface. tests/test_northstar.py
    #: pins that wiring.
    judge_rubric: list[str] = field(default_factory=list)
    holdout: str = ""
    # "core" (scored, hand-curated) | "full" (generated) | "canary" (kept in
    # the corpus but excluded from the scored core denominator — e.g. a pure
    # chat-knowledge row that measures the base model, not the product).
    # `startup.py` emits its own "startup" subset for the same reason: a
    # different measurement must not share the core denominator.
    subset: str = "full"
    runnable: bool = True
    skip_reason: str = ""
    #: Files the sandbox writes AFTER the initial commit+push and leaves
    #: UNCOMMITTED (path → content; appended when the path is tracked, created
    #: otherwise). The sandbox reset+clean+push otherwise pre-satisfies any
    #: "make sure everything is committed/pushed, no garbage" request — the
    #: harness itself destroyed that task's precondition, making the row a
    #: guaranteed pass (V3 corpus audit, 2026-08-04). A seed restores a real
    #: dirty tree so the demand is real again. Empty = untouched sandbox.
    dirty_seed: dict[str, str] = field(default_factory=dict)
    expect_escalation: bool = False
    #: WHY this spec is expected to stop and escalate rather than deliver.
    #:
    #: Without it a miss cannot be adjudicated from the data: "the agent did not
    #: escalate here" and "this spec should never have been gated" are the same
    #: row, and every honest-escalation figure needs a caveat nobody can settle.
    #: `skip_reason` beside it is the same idea for `runnable: false`; this is
    #: that idiom applied to the gate.
    #:
    #: Specs that predate this field carry an `unrecorded: …` sentinel in the
    #: YAML itself and NOT a reconstructed reason. Their request text does hint at one —
    #: external systems a replay cannot reach, or local files that no longer
    #: exist — but that is inference, and an inference written into a corpus is
    #: indistinguishable from a record the moment the next reader arrives.
    escalation_reason: str = ""
    path: Path | None = None
    # The repo path exactly as the SPEC FILE carries it, before any local
    # translation. Reporting derives the project name from this, never from the
    # translated path: the translated path names a real local checkout, and the
    # project name is rendered into docs/NORTH_STAR_BENCH.md, which is TRACKED.
    # Deliberately a field rather than a key inside ``repo`` — ``to_dict``
    # serialises ``repo`` wholesale, so a key there could be written back into a
    # spec file, which is the leak this whole mechanism exists to avoid.
    spec_repo_path: str = ""

    @staticmethod
    def from_dict(data: dict[str, Any], *, path: Path | None = None) -> "BenchTask":
        return BenchTask(
            id=data["id"],
            title=data["title"],
            request=data.get("request", ""),
            source=dict(data.get("source", {}) or {}),
            repo=dict(data.get("repo", {}) or {}),
            linked_repos=[dict(r) for r in (data.get("linked_repos") or [])],
            original=dict(data.get("original", {}) or {}),
            acceptance_criteria=list(data.get("acceptance_criteria", []) or []),
            judge_rubric=list(data.get("judge_rubric", []) or []),
            holdout=data.get("holdout", "") or "",
            subset=data.get("subset", "full"),
            runnable=bool(data.get("runnable", True)),
            skip_reason=data.get("skip_reason", "") or "",
            dirty_seed=dict(data.get("dirty_seed", {}) or {}),
            expect_escalation=bool(data.get("expect_escalation", False)),
            escalation_reason=data.get("escalation_reason", "") or "",
            path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "request": self.request,
            "source": self.source, "repo": self.repo,
            "linked_repos": self.linked_repos, "original": self.original,
            "acceptance_criteria": self.acceptance_criteria,
            "judge_rubric": self.judge_rubric,
            "holdout": self.holdout, "subset": self.subset,
            "runnable": self.runnable, "skip_reason": self.skip_reason, "escalation_reason": self.escalation_reason,
            "dirty_seed": self.dirty_seed,
            "expect_escalation": self.expect_escalation,
        }


def load_bench_tasks(directory: Path = NORTHSTAR_DIR, *,
                     subset: str | None = None) -> list[BenchTask]:
    directory = Path(directory)
    tasks: list[BenchTask] = []
    if not directory.exists():
        return tasks
    # Applied at LOAD time so every consumer — the runner's run-time repo
    # check, the sandbox copy, the scorer — sees the real path. Nothing
    # re-serialises a LOADED spec (build_bench_tasks constructs fresh specs
    # from transcripts), so a remapped path can never be written back into a
    # tracked spec file and undo the vendor-neutral scrub.
    mapping = load_repo_map()
    for p in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if data:
            task = BenchTask.from_dict(data, path=p)
            # Recorded whether or not a map exists, so reporting has one rule.
            task.spec_repo_path = str(task.repo.get("path") or "")
            if mapping and task.repo.get("path"):
                task.repo["path"] = remap_repo_path(
                    str(task.repo["path"]), mapping)
            tasks.append(task)
    if subset:
        tasks = [t for t in tasks if t.subset == subset]
    return sorted(tasks, key=lambda t: t.id)


# ----------------------------- leak lint ---------------------------------- #

def _ngrams(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = _WORD.findall((text or "").lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def find_leaks(spec: BenchTask, assistant_text: str, *, n: int = 6) -> list[str]:
    """Curated fields must not share an n-gram with the source session's
    assistant output — that would smuggle the original solution into the
    benchmark. Returns the offending fields (empty = clean). The request
    itself is exempt: it is user-authored by definition."""
    reference = _ngrams(assistant_text, n)
    if not reference:
        return []
    leaks: list[str] = []
    for label, text in (
        ("acceptance_criteria", "\n".join(spec.acceptance_criteria)),
        ("holdout", spec.holdout),
    ):
        if _ngrams(text, n) & reference:
            leaks.append(label)
    return leaks


# ------------------------------ builder ----------------------------------- #

def _first_request(t: Transcript) -> str:
    for m in t.messages:
        if m.role == "user" and m.content.strip():
            return m.content
    return ""


def _pin_for(repo_path: Path, branch: str, started: str) -> str:
    """Best-effort commit pin: the tip of ``branch`` as of the session start.
    Empty string when unresolvable (caller decides HEAD-fallback vs skip)."""
    if not started:
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-list", "-1", f"--before={started}",
             branch or "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# A history rewrite (rebase, filter-repo, force-push) can make a spec's
# recorded pin unreachable in the very repo it was cut from. Re-derivation
# repairs that at BUILD time only — never at run time (`check_repo_map`'s
# probe and skip path are unchanged) — and refuses by name rather than ever
# guessing a replacement commit.
# The date was parseable but still refused: `_rederive_pin` found nothing
# at-or-before it on the recorded branch (or its HEAD fallback).
PIN_NOT_REDERIVABLE = ("pin unreachable and no commit at-or-before the "
                       "recorded start: not re-derivable")
# The recorded start itself is missing or not a parsable ISO timestamp —
# refused before ever calling git (`--before=<garbage>` silently reads as
# "now", see `_ISO_PREFIX` below).
PIN_START_UNPARSABLE = ("pin unreachable and the recorded start is missing "
                        "or not a parsable timestamp: not re-derivable")
# One set for every consumer that needs "was this refused for a pin
# reason" without enumerating both names itself.
PIN_REFUSAL_REASONS = frozenset({PIN_NOT_REDERIVABLE, PIN_START_UNPARSABLE})


def spec_pin_not_rederivable(spec: "BenchTask") -> bool:
    """Was this spec refused re-derivation, under either named reason?"""
    return spec.skip_reason in PIN_REFUSAL_REASONS


# `git --before=<garbage>` silently reads an unparsable date as "now" and
# hands back today's tip disguised as a "derived" pin. This is the guard: a
# `started` that does not even look like an ISO timestamp is refused, never
# handed to git.
_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _pin_reachable(repo_path: Path, pin: str) -> bool | None:
    """Tri-state probe: does *pin* still resolve to a commit in *repo_path*?

    Mirrors ``check_repo_map``'s own ``git cat-file -e <pin>^{commit}`` probe
    and its 5s timeout exactly, so "reachable" means the same thing at build
    time as it does at run time. ``None`` — the probe itself did not run to
    completion (no ``git``, a cold mount timing out) — is FAIL-CLOSED: an
    unverified probe must never be read as "unreachable" and trigger a
    rewrite of a pin that was never actually broken.
    """
    try:
        rc = subprocess.run(
            ["git", "-C", str(repo_path), "cat-file", "-e", f"{pin}^{{commit}}"],
            capture_output=True, timeout=5,
        ).returncode
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return rc == 0


class _ProbeUnverifiable(Exception):
    """A git probe did not run to completion (timeout, missing ``git``, a
    cold mount). Distinct from a probe that ran and returned a negative
    answer — a transient failure must never be read as a confirmed "no"."""


def _branch_exists(repo_path: Path, branch: str) -> bool:
    """Did ``git`` actually confirm ``branch`` is missing? Raises
    ``_ProbeUnverifiable`` when the check itself could not complete, so a
    transient failure is never conflated with "branch confirmed absent" —
    the same fail-closed shape as ``_pin_reachable``."""
    try:
        rc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=repo_path, capture_output=True, timeout=5,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise _ProbeUnverifiable(str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise _ProbeUnverifiable(str(exc)) from exc
    return rc == 0


def _rederive_pin(repo_path: Path, branch: str, started: str) -> str:
    """Re-derive a dead pin by date: the tip of the spec's own recorded
    ``branch`` as of ``started``, reusing the untouched ``_pin_for`` (same
    ``branch or "HEAD"`` rule it has always used — no new default-branch
    discovery, that is a human-gated decision).

    One added fallback: if ``branch`` no longer exists in the rewritten repo
    — the common case, since a rewrite that drops a pin often drops the
    branch that produced it too — fall back to ``HEAD`` rather than refuse.
    Empty string when no commit exists at or before ``started`` on either
    ref: the caller refuses by name instead of guessing.

    Raises ``_ProbeUnverifiable`` when the branch-existence check itself
    could not complete (timeout / cold mount / missing ``git``): the caller
    must leave the spec untouched rather than silently re-deriving against
    HEAD on the unproven assumption that the recorded branch is gone.
    """
    use_branch = branch or "HEAD"
    if use_branch != "HEAD" and not _branch_exists(repo_path, use_branch):
        use_branch = "HEAD"
    return _pin_for(repo_path, use_branch, started)


def spec_pin_rederived(spec: "BenchTask") -> bool:
    """Was this spec's pin re-derived by date from a rewritten history,
    rather than being the commit the source session actually ran against?
    One predicate for every consumer (`northstar.py`'s scoring path,
    reports, comparisons)."""
    return bool(spec.repo.get("pin_rederived"))


class _PinRepairKind(Enum):
    """Four outcomes for one spec's recorded pin. Never a fifth: this is a
    closed decision, not an extensible one — a new outcome here means a new
    named refusal reason too, not a silent guess."""
    UNTOUCHED = "untouched"   # pin decision left exactly as recorded
    REACHABLE = "reachable"   # recorded pin still resolves — no change
    REDERIVED = "rederived"   # dead pin replaced by a date-derived one
    REFUSED = "refused"       # dead pin, no replacement — named refusal


@dataclass(frozen=True)
class _PinRepair:
    kind: _PinRepairKind
    pin: str = ""            # REDERIVED only: the new pin
    pin_original: str = ""   # REDERIVED/REFUSED only: the dead pin to keep
    reason: str = ""         # REFUSED only: PIN_NOT_REDERIVABLE or PIN_START_UNPARSABLE


def _repair_pin(probe_path: Path | None, *, recorded_pin: str,
                recorded_branch: str, pin_original: str,
                started: str) -> _PinRepair:
    """Decide what, if anything, one spec's recorded pin needs on rebuild.
    The single repair site shared by the transcript loop and the sweep
    (`_sweep_unreached_specs`), so both apply identical fail-closed,
    refuse-by-name rules.

    UNTOUCHED — pin empty/``"HEAD"``, no probe-able git repo at
    *probe_path*, the reachability probe itself did not run to completion
    (``_pin_reachable`` returns ``None``), or the branch-existence check
    could not complete (``_ProbeUnverifiable``). The caller must leave the
    pin DECISION alone — carry the recorded ``pin``/``pin_original``/
    ``pin_rederived`` forward exactly as read; nothing else about this
    outcome says the rest of the spec can't still refresh.
    REACHABLE — the recorded pin still resolves. Caller carries the
    existing repo fields forward unchanged; this, not a skip-write, is what
    makes a resolvable-pin rebuild byte-identical (``_pin_for`` and this
    function are both deterministic on unchanged inputs).
    REDERIVED — the recorded pin is confirmed dead and a replacement exists
    at-or-before *started* on *recorded_branch* (or its HEAD fallback, see
    ``_rederive_pin``).
    REFUSED — the recorded pin is confirmed dead and either *started* is
    missing/unparsable (``PIN_START_UNPARSABLE``) or no replacement exists
    on the branch (``PIN_NOT_REDERIVABLE``). The dead pin is preserved as
    ``pin_original``, never silently replaced by a freshly-computed live
    one under a spec claiming to record the original tree.
    """
    if not recorded_pin or recorded_pin == "HEAD":
        return _PinRepair(_PinRepairKind.UNTOUCHED)
    if (probe_path is None or not probe_path.is_dir()
            or not (probe_path / ".git").exists()):
        return _PinRepair(_PinRepairKind.UNTOUCHED)

    reachable = _pin_reachable(probe_path, recorded_pin)
    if reachable is None:
        return _PinRepair(_PinRepairKind.UNTOUCHED)
    if reachable:
        return _PinRepair(_PinRepairKind.REACHABLE)

    resolved_original = pin_original or recorded_pin
    if not started or not _ISO_PREFIX.match(started):
        return _PinRepair(_PinRepairKind.REFUSED, reason=PIN_START_UNPARSABLE,
                          pin_original=resolved_original)

    try:
        new_pin = _rederive_pin(probe_path, recorded_branch, started)
    except _ProbeUnverifiable:
        return _PinRepair(_PinRepairKind.UNTOUCHED)

    if new_pin:
        return _PinRepair(_PinRepairKind.REDERIVED, pin=new_pin,
                          pin_original=resolved_original)
    return _PinRepair(_PinRepairKind.REFUSED, reason=PIN_NOT_REDERIVABLE,
                      pin_original=resolved_original)


def build_bench_tasks(
    transcripts: list[Transcript], *, out_dir: Path = GENERATED_DIR,
) -> list[Path]:
    """Turn transcripts into spec YAMLs. Structural no-leak guarantee: only
    ``_first_request`` (the initial user message) is copied; assistant text
    never reaches the spec. Dedupes resumed/continued sessions by the hash of
    that first request. Non-runnable conversations are still written (with
    ``runnable: false`` + reason) so coverage accounting stays honest.

    Every spec named by a transcript is rebuilt from that transcript on
    every call — a changed source (edited/re-extracted transcript) refreshes
    ``title``/``original`` (tokens, wall-clock, corrections) rather than
    freezing them at first-write forever. This can only overwrite a spec's
    GENERATED fields: hand-curated fields (acceptance criteria, holdout,
    judge rubric) live in the same YAML but this builder only ever emits the
    generated ones (see ``BenchTask.to_dict``) into ``out_dir``
    (``eval/northstar_tasks/generated``, gitignored) — the tracked, curated
    copies under ``eval/northstar_tasks/`` are a separate, human-curated
    step, never overwritten by this function.

    Also the ONLY writer that may repair a pin a history rewrite made
    unreachable (`_repair_pin`, shared with `_sweep_unreached_specs` below):
    an existing spec whose recorded pin no longer resolves gets a
    date-derived replacement with ``pin_rederived: true`` and the dead pin
    preserved as ``pin_original``, or — if no replacement exists, or
    ``started`` is missing/unparsable — a named refusal
    (``PIN_NOT_REDERIVABLE`` / ``PIN_START_UNPARSABLE``) rather than a
    guess. A spec whose pin still resolves has its repo fields carried
    forward unchanged; combined with deterministic rebuilding of everything
    else, a clean rebuild is byte-identical without needing a skip-write.
    Nothing here runs at replay time; the runner's skip behaviour for a
    still-unreachable pin is unchanged.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    seen_requests: set[str] = set()
    # Probe-path translation only (never written into a spec): a TRACKED
    # spec's `repo.path` is vendor-neutral and resolves nowhere on its own;
    # the local, gitignored map restores the real checkout for the
    # reachability probe exactly as `load_bench_tasks` does for a run.
    mapping = load_repo_map()

    for t in transcripts:
        request = _first_request(t)
        if not request.strip():
            continue
        req_hash = hashlib.sha256(request.strip().encode()).hexdigest()
        if req_hash in seen_requests:
            continue  # resumed/continued session — same task
        seen_requests.add(req_hash)

        tid = f"ns-{hashlib.sha256(t.cascade_id.encode()).hexdigest()[:8]}"
        p = out_dir / f"{tid}.yaml"

        existing_data: dict[str, Any] = {}
        if p.exists():
            try:
                loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                log.warning("%s: malformed existing spec YAML — treating as absent", tid)
                loaded = None
            if isinstance(loaded, dict):
                existing_data = loaded
            elif loaded:
                log.warning("%s: existing spec YAML is not a mapping — treating as absent", tid)

        # Claude Code carries cwd; Windsurf carry workspace URIs — the  # term-ok: real IDE names
        # original 89-conversation corpus is ALL workspaces-shaped.
        cwd = getattr(t, "cwd", "") or ""
        if not cwd:
            from ..history.analyzer import _project_from_workspaces
            cwd = _project_from_workspaces(getattr(t, "workspaces", []) or [])
        branch = getattr(t, "git_branch", "") or ""
        # The corpus is the record: a `started` already recorded on the spec
        # beats one re-extracted from a transcript that may have moved on.
        existing_provenance = dict(existing_data.get("source") or {})
        started = str(existing_provenance.get("started")
                      or getattr(t, "started", "") or "")

        existing_repo = dict(existing_data.get("repo") or {})
        recorded_pin = str(existing_repo.get("pin") or "")

        runnable, skip_reason, pin = True, "", ""
        repo_override: dict[str, Any] | None = None

        repo_path = Path(cwd) if cwd else None
        if repo_path is None or not repo_path.is_dir():
            runnable, skip_reason = False, f"repo missing: {cwd or '(unknown cwd)'}"
        elif not (repo_path / ".git").exists():
            runnable, skip_reason = False, f"not a git repo: {cwd}"
        elif recorded_pin:
            # An existing spec's recorded pin is the record of what the
            # session actually ran against — repair it (via the shared
            # `_repair_pin`) rather than blindly recomputing a fresh one.
            # This carries a resolvable pin's repo fields forward unchanged
            # (REACHABLE) and converges a rewritten-history pin
            # (REDERIVED/REFUSED); it never runs for a brand-new spec.
            translated = (remap_repo_path(str(repo_path), mapping)
                         if mapping else str(repo_path))
            probe_path = Path(translated)
            repair = _repair_pin(
                probe_path, recorded_pin=recorded_pin,
                recorded_branch=str(existing_repo.get("branch") or ""),
                pin_original=str(existing_repo.get("pin_original") or ""),
                started=started)

            if repair.kind is _PinRepairKind.REDERIVED:
                pin = repair.pin
                repo_override = {
                    "path": existing_repo.get("path", cwd),
                    "pin": pin,
                    "pin_original": repair.pin_original,
                    "pin_rederived": True,
                    "branch": existing_repo.get("branch", branch),
                }
                runnable, skip_reason = True, ""
            elif repair.kind is _PinRepairKind.REFUSED:
                pin = recorded_pin
                repo_override = dict(existing_repo)
                repo_override["pin_original"] = repair.pin_original
                runnable, skip_reason = False, repair.reason
            else:
                # UNTOUCHED or REACHABLE — the pin DECISION is left exactly
                # as recorded. Only a confirmed-reachable pin (never an
                # unverified probe, which proves nothing either way) may
                # clear a stale pin-refusal stamp, and only a stamp that was
                # actually a pin refusal — this branch runs only once the
                # repo-validity checks above already passed, so it must
                # never clear an unrelated `repo missing:`/`not a git
                # repo:` reason recorded earlier.
                pin = recorded_pin
                repo_override = dict(existing_repo)
                prev_runnable = bool(existing_data.get("runnable", True))
                prev_reason = str(existing_data.get("skip_reason") or "")
                if (repair.kind is _PinRepairKind.REACHABLE and not prev_runnable
                        and prev_reason in PIN_REFUSAL_REASONS):
                    runnable, skip_reason = True, ""
                else:
                    runnable, skip_reason = prev_runnable, prev_reason
        else:
            pin = _pin_for(repo_path, branch, started)
            if not pin:
                pin = "HEAD"  # advisory fallback; curator may flip runnable

        repo = repo_override if repo_override is not None else {
            "path": cwd, "pin": pin, "branch": branch}

        ended = getattr(t, "ended", "") or ""
        wall = 0.0
        if started and ended:
            from datetime import datetime
            try:
                _iso = "%Y-%m-%dT%H:%M:%S.%f%z"
                s = datetime.strptime(started.replace("Z", "+0000"), _iso)
                e = datetime.strptime(ended.replace("Z", "+0000"), _iso)
                wall = max(0.0, (e - s).total_seconds())
            except ValueError:
                wall = 0.0

        # Title from the REQUEST (user-authored), never from the transcript
        # title — auto-generated titles summarize the whole conversation and
        # can encode the eventual solution approach (review finding: an
        # unaudited leak channel straight into the coder's Task).
        req_title = " ".join(request.strip().split())[:100]
        spec = BenchTask(
            id=tid,
            title=req_title,
            request=request,
            source={
                "kind": ("windsurf" if getattr(t, "source", "") == "windsurf"  # term-ok: internal source tag names the real IDE
                         else "claude_code"),
                "session": t.cascade_id,
                "label": getattr(t, "source", "") or "",
                "started": started,
            },
            repo=repo,
            original={
                "tokens": dict(getattr(t, "usage", {}) or {}),
                "wall_clock_s": round(wall, 1),
                "user_messages": sum(1 for m in t.messages if m.role == "user"),
                "corrections": int(getattr(t, "corrections", 0) or 0),
            },
            runnable=runnable,
            skip_reason=skip_reason,
        )
        p.write_text(yaml.safe_dump(spec.to_dict(), sort_keys=False,
                                    allow_unicode=True, width=100), encoding="utf-8")
        written.append(p)

    _sweep_unreached_specs(out_dir, written, mapping)
    return written


def _sweep_unreached_specs(out_dir: Path, written: list[Path],
                           mapping: dict[str, str]) -> None:
    """Repair pins on spec files the transcript loop above never visited —
    a spec whose source session has aged out of the transcript export (or
    was deleted/rotated) still sits on disk with a dead pin forever unless
    something revisits it. Build-time only, using the same `_repair_pin` as
    the transcript loop; never runs at replay time.

    Skips every path already in ``written`` (the transcript loop just
    repaired it — the loop's own repair must never be double-applied here)
    and every file that is not valid spec YAML (logged, left untouched).
    Writes only on REDERIVED or REFUSED, and only if the serialized bytes
    actually changed, so re-sweeping an already-repaired file is a true
    no-op — a dead spec with no ``source.started`` at all is refused as
    ``PIN_START_UNPARSABLE`` every time, never guessed.
    """
    visited = set(written)
    specs = sorted(out_dir.glob("*.yaml")) + sorted(out_dir.glob("*.yml"))
    for p in specs:
        if p in visited:
            continue
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            log.warning("%s: malformed spec YAML — sweep leaving it alone", p.name)
            continue
        if not isinstance(loaded, dict):
            if loaded:
                log.warning("%s: spec YAML is not a mapping — sweep leaving it alone", p.name)
            continue

        existing_repo = dict(loaded.get("repo") or {})
        recorded_pin = str(existing_repo.get("pin") or "")
        if not recorded_pin or recorded_pin == "HEAD":
            continue

        repo_path_str = str(existing_repo.get("path") or "")
        if not repo_path_str:
            continue
        translated = (remap_repo_path(repo_path_str, mapping)
                     if mapping else repo_path_str)
        probe_path = Path(translated)

        started = str((loaded.get("source") or {}).get("started") or "")
        repair = _repair_pin(
            probe_path, recorded_pin=recorded_pin,
            recorded_branch=str(existing_repo.get("branch") or ""),
            pin_original=str(existing_repo.get("pin_original") or ""),
            started=started)

        if repair.kind is _PinRepairKind.REDERIVED:
            new_data = dict(loaded)
            new_data["repo"] = {
                "path": existing_repo.get("path", repo_path_str),
                "pin": repair.pin,
                "pin_original": repair.pin_original,
                "pin_rederived": True,
                "branch": existing_repo.get("branch", ""),
            }
            new_data["runnable"] = True
            new_data["skip_reason"] = ""
        elif repair.kind is _PinRepairKind.REFUSED:
            new_data = dict(loaded)
            new_repo = dict(existing_repo)
            new_repo["pin_original"] = repair.pin_original
            new_data["repo"] = new_repo
            new_data["runnable"] = False
            new_data["skip_reason"] = repair.reason
        else:
            continue  # UNTOUCHED or REACHABLE — nothing to write

        new_text = yaml.safe_dump(new_data, sort_keys=False,
                                  allow_unicode=True, width=100)
        if new_text != p.read_text(encoding="utf-8"):
            p.write_text(new_text, encoding="utf-8")
