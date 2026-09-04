"""ProjectProfile — per-repo, human-confirmed build/test/CI recipe.

The mechanism that keeps no_human general: how to install / unit-test / lint a
repo, which CI to trigger and how to read it, and which steps are human-gated —
all *config derived from the repo's own declarations and proven by running*,
never hardcoded per repo (no ``if repo == "metrics-core"`` anywhere).

The YAML at ``<repo>/.no_human/project.yml`` is the human-confirmable source of
truth; ``Store`` mirrors it (with the confirmation flag) for the daemon. A
profile is only trusted once ``confirmed`` is true — `nh onboard` proposes it,
a human confirms via the same gate as `nh learnings`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROFILE_RELPATH = Path(".no_human") / "project.yml"


@dataclass
class ProjectProfile:
    repo_path: str
    ecosystem: str = ""                 # e.g. "python-pytest", "node", "maven"
    install_cmd: str = ""               # e.g. "uv sync"
    test_cmd: str = ""                  # unit tests, e.g. "uv run pytest -q"
    integration_test_cmd: str = ""       # integration tests, e.g. "uv run pytest tests/integration -q"
    lint_cmd: str = ""                  # e.g. "uv run ruff check"
    # Change-scoped test commands (2026-07-11): ordered glob rules so a
    # change touching only one area runs the tests for THAT area, not the
    # repo-wide default. Each: {"glob": "web/**", "command": "node --test
    # src/", "cwd": "web"}. Matched against the attempt's edited files
    # (fnmatch, * spans dirs); used only when EVERY edited file matches one
    # rule, else the default test_cmd runs. Empty = today's behaviour exactly.
    test_commands: list[dict[str, Any]] = field(default_factory=list)
    ci: dict[str, Any] = field(default_factory=dict)        # mirrors config "ci" block
    human_gated_steps: list[str] = field(default_factory=list)
    # VCS topology (derived from the repo's `origin` remote), so no_human knows
    # which host to open the PR against without anything hardcoded.
    vcs_host: str = ""                  # e.g. "github.com", "code.example.com", "gitlab.acme.net"
    vcs_remote: str = ""                # origin URL, credentials stripped
    # The ~/.no_human/.env keys this repo needs to be driven end-to-end (derived
    # from the CI backend + VCS host). On a missing one, the engine escalates a
    # MISSING_ACCESS blocker naming the exact key — never the value.
    required_credentials: list[str] = field(default_factory=list)
    # Provenance: which repo declarations each command was derived from, and
    # whether `nh onboard` proved it by running it. Trust requires proof.
    derived_from: list[str] = field(default_factory=list)
    proven: dict[str, bool] = field(default_factory=dict)   # cmd-key -> ran-clean
    confirmed: bool = False
    notes: str = ""
    wiki_commit: str = ""              # git SHA when wiki was last generated
    default_branch: str = ""           # C3: explicit default branch (e.g. "main", "master")
    # SCRUM-26: repo-level calibration for task.config's per-task overrides
    # (attempt_tokens / lifetime_tokens — see blockers/actions.py). 0 = unset;
    # copied into a new task's config only when the task has no explicit
    # override, so an operator hand-set task.config always still wins.
    default_attempt_tokens: int = 0
    default_lifetime_tokens: int = 0
    # R1 (funnel forensics, 2026-08-10): which UNIT the two values above are
    # in. Empty means unknown, which means written before the 2026-07-31
    # cutover, which means RAW — the same fail-closed reading `task.config`
    # takes, and the reading every existing profile on every install needs to
    # keep getting. `nh repo config` stamps `"weighted"` on every write from
    # now on, so a value typed today is never re-converted and the ambiguity
    # this field exists for cannot be created again.
    default_budget_unit: str = ""
    # UI evidence (no-human-67): opt-in browser-walk verification. Off by
    # default (`enabled: False`) so an unconfigured repo's attempts are
    # byte-identical to before this field existed. Read by
    # `core/prompt_blocks.py`'s `ui_evidence_block` (the coder prompt block)
    # and, at attempt time, by `Orchestrator._maybe_capture_ui_evidence`,
    # which wraps `testing/ui_evidence.py`'s manifest runner (`run`) in
    # `ui_evidence.dev_server` (D2, 2026-09-02). Keys:
    #   enabled: bool           - opt-in switch.
    #   start_cmd: str           - argv (shlex-split), run in the attempt's
    #                              worktree by `dev_server` when nothing
    #                              already answers at the *manifest's*
    #                              `base_url` (the file the coder writes at
    #                              `.no_human/ui_evidence.json`, not this
    #                              profile field — the two may differ); the
    #                              process is killed when the walk ends,
    #                              whether it exits normally or raises. A
    #                              server already answering there is left
    #                              running untouched and is never killed.
    #   base_url: str            - http(s)://127.0.0.1|localhost:<port>, the
    #                              only hosts `dev_server` will start a
    #                              subprocess and poll for (never a remote
    #                              host — this runs inside the attempt's own
    #                              worktree, on the attempt's own machine).
    #   ready_path: str          - path polled for readiness (default '/').
    #   ready_timeout_s: int     - seconds to wait for `base_url+ready_path`
    #                              before giving up (default 60); clamped to
    #                              [1, 300] regardless of the configured
    #                              value.
    #   ui_paths: list[str]      - globs (fnmatch, matched like
    #                              `test_commands`) deciding whether an
    #                              attempt's declared plan files are "UI
    #                              work" — only then does the prompt block
    #                              appear.
    #   build_cmd: str           - optional; run in the worktree by `dev_server`
    #                              immediately BEFORE `start_cmd`, and only when a
    #                              server is about to be booted. `&&`-separated
    #                              segments, each shlex-split and run shell=False.
    #                              A failure/timeout is a DISCLOSED walk skip.
    #   build_timeout_s: int     - whole-chain budget (default 300), clamped [1,3600].
    ui_evidence: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "start_cmd": "",
            "base_url": "",
            "ready_path": "/",
            "ready_timeout_s": 60,
            "ui_paths": ["web/**", "src/**/*.jsx", "src/**/*.tsx", "**/*.html", "**/*.css"],
        }
    )

    # --- serialization ---------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "ecosystem": self.ecosystem,
            "install_cmd": self.install_cmd,
            "test_cmd": self.test_cmd,
            "integration_test_cmd": self.integration_test_cmd,
            "lint_cmd": self.lint_cmd,
            "test_commands": self.test_commands,
            "ci": self.ci,
            "human_gated_steps": self.human_gated_steps,
            "vcs_host": self.vcs_host,
            "vcs_remote": self.vcs_remote,
            "required_credentials": self.required_credentials,
            "derived_from": self.derived_from,
            "proven": self.proven,
            "confirmed": self.confirmed,
            "notes": self.notes,
            "wiki_commit": self.wiki_commit,
            "default_branch": self.default_branch,
            "default_attempt_tokens": self.default_attempt_tokens,
            "default_lifetime_tokens": self.default_lifetime_tokens,
            "default_budget_unit": self.default_budget_unit,
            "ui_evidence": self.ui_evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectProfile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    # --- YAML in the repo ------------------------------------------------- #

    def yaml_path(self) -> Path:
        return Path(self.repo_path).expanduser() / PROFILE_RELPATH

    def save(self) -> Path:
        """Write the profile to ``<repo>/.no_human/project.yml``."""
        path = self.yaml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # repo_path is implied by location; keep it out of the on-disk file.
        body = {k: v for k, v in self.to_dict().items() if k != "repo_path"}
        path.write_text(yaml.safe_dump(body, sort_keys=False))
        return path

    @classmethod
    def load(cls, repo_path: str | Path) -> "ProjectProfile | None":
        path = Path(repo_path).expanduser() / PROFILE_RELPATH
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text()) or {}
        data["repo_path"] = str(Path(repo_path).expanduser())
        return cls.from_dict(data)

    # --- readiness -------------------------------------------------------- #

    @property
    def is_usable(self) -> bool:
        """A profile may drive a task only if a human confirmed it and its test
        command was proven to run."""
        return bool(self.confirmed and self.test_cmd and self.proven.get("test_cmd"))

    def usable_under_policy(self, *, auto_confirm_proven: bool) -> bool:
        """Whether this profile may drive a task under the active policy: a
        human confirmed it (``is_usable``), OR ``auto_confirm_proven`` is opted
        in and its test command was PROVEN to run clean. Single source of truth
        for both the orchestrator (which gates test-command resolution on it)
        and the CLI (which warns when it does NOT hold) — they must not drift."""
        if self.is_usable:
            return True
        return bool(auto_confirm_proven and self.test_cmd and self.proven.get("test_cmd"))


# Fields the product itself writes to only ONE side of the yml/DB pair during
# normal operation, so a mismatch on them is not evidence a human edited
# project.yml — it is the product's own write pattern. Comparing them makes
# `profile_divergence` fire permanently on repos nobody hand-edited:
#   - `wiki_commit`: `docs_gen`'s wiki refresh (scheduler.py's periodic job
#     and `nh docs generate`) calls `profile.save()` on the yml only — it
#     never calls `store.upsert_profile`, so the DB row keeps the pre-refresh
#     SHA forever.
#   - `default_attempt_tokens` / `default_lifetime_tokens` / `default_budget_unit`:
#     `nh repo config` (SCRUM-26 per-repo budget overrides) calls
#     `store.upsert_profile` only — it never calls `profile.save()`, so the
#     yml keeps the pre-write values forever.
# `repo_path` is excluded for the same reason (on-disk file never has it),
# just on both sides. Only fields a human sets via `nh onboard --confirm` /
# the API's confirm step (which write both sides together) are compared.
_MACHINE_MANAGED_FIELDS = frozenset({
    "repo_path",
    "wiki_commit",
    "default_attempt_tokens",
    "default_lifetime_tokens",
    "default_budget_unit",
})


def profile_divergence(
    db_profile: "ProjectProfile | None", yml_profile: "ProjectProfile | None"
) -> list[str]:
    """Field names where a repo's `.no_human/project.yml` differs from the
    CONFIRMED DB profile. Empty when either side is absent or they agree.

    Normalization is the dataclass itself: both sides are already
    ``ProjectProfile`` instances, so YAML vs JSON parsing differences
    ('yes' -> True, ints, missing keys -> field defaults) are resolved by
    ``from_dict`` before anything is compared. Fields the product itself
    writes to only one side (:data:`_MACHINE_MANAGED_FIELDS`) are excluded —
    they diverge by design, not because a human edited the file.
    """
    if db_profile is None or yml_profile is None:
        return []
    a = db_profile.to_dict()
    b = yml_profile.to_dict()
    return sorted(
        k for k in a
        if k not in _MACHINE_MANAGED_FIELDS and a[k] != b.get(k)
    )


def apply_default_task_config(
    profile: "ProjectProfile | None", task_config: dict[str, Any]
) -> dict[str, Any]:
    """Copy a repo profile's default token budgets into a new task's config
    (SCRUM-26), for exactly the two keys the orchestrator already reads as
    per-task overrides (``attempt_tokens`` / ``lifetime_tokens`` — see
    blockers/actions.py's ALLOWED_TASK_CONFIG_KEYS). An explicit key already
    present on ``task_config`` always wins; no profile / no defaults set on
    the profile leaves ``task_config`` byte-for-byte unchanged."""
    if profile is None:
        return task_config
    from .core.pricing import BUDGET_UNIT_KEY, TOKEN_CAP_KEYS, WEIGHTED_UNIT

    merged = dict(task_config)
    # R1 — MIXED UNITS: copy nothing, in EITHER direction. `BUDGET_UNIT_KEY`
    # marks the WHOLE dict, so one unit has to describe both sides of this
    # merge, and there are two ways for that to be false:
    #
    #   - a WEIGHTED profile writing beside a cap the caller brought with no
    #     marker, i.e. a raw one. Copying the profile's weighted 4,000,000
    #     under no marker reads it back as raw and converts it to 794,000 —
    #     a 5x CUT of a number the operator typed correctly.
    #   - a RAW profile writing into a dict that already DECLARES weighted.
    #     Copying its 20,200,000 under that marker takes it at face value —
    #     5.04x fail-open, and unlike the first direction nothing warns,
    #     because a marked value never reaches the raise-floor.
    #
    # The first cure covered only the first direction while this comment
    # claimed both; the guard is now the symmetric statement it always
    # described. Copying nothing leaves the ungranted default in force for the
    # key the caller did not set, and the ungranted default is never worse
    # than a value read in the wrong unit.
    #
    # The second clause is not `TOKEN_CAP_KEYS & set(merged)` alone: a bare
    # marker with no cap yet still declares the dict's unit, and copying a
    # raw default under it is exactly the 5.04x above.
    profile_weighted = profile.default_budget_unit == WEIGHTED_UNIT
    caller_weighted = merged.get(BUDGET_UNIT_KEY) == WEIGHTED_UNIT
    caller_declares_a_unit = bool(TOKEN_CAP_KEYS & set(merged)) or BUDGET_UNIT_KEY in merged
    if profile_weighted != caller_weighted and caller_declares_a_unit:
        return merged
    if profile.default_attempt_tokens and "attempt_tokens" not in merged:
        merged["attempt_tokens"] = profile.default_attempt_tokens
    if profile.default_lifetime_tokens and "lifetime_tokens" not in merged:
        merged["lifetime_tokens"] = profile.default_lifetime_tokens
    # Stamp the unit so the orchestrator reads these at face value instead of
    # treating them as pre-cutover raw and converting them (R1, the 40% cut
    # that killed the August funnel). An UNSTAMPED profile is every profile
    # that exists today, including the 12,000,000 one, and it must keep being
    # converted or this change 5x's every install at once.
    if profile_weighted and TOKEN_CAP_KEYS & set(merged):
        merged[BUDGET_UNIT_KEY] = WEIGHTED_UNIT
    return merged
