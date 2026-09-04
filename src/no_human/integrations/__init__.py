"""Integrations registry — a status layer over the config.

The issue trackers are first-class config sections (``integrations.jira`` and
friends); the rest are read-only STATUS VIEWS over existing config: github /
gitlab / jenkins / circleci over ``ci.*`` and slack over ``notifications.*``.
There is exactly one source of truth per setting — no duplicate keys.

CircleCI used to be a first-class ``integrations.circleci`` section holding
``org_slug`` + ``project``. Nothing in the CI layer ever read it
(``ci_from_config`` builds ``CircleCICI`` from ``ci.project``), so saving that
form produced a card reading "Configured", a Test-connection reading
"Connected", and NO ``ci:`` block at all — every PR went out ungated while the
panel said CircleCI was the active CI backend. It is now a ``ci.*`` view like
its three siblings, which is what makes that sentence true.

``list_integrations`` is pure and synchronous (never a secret in a detail
string, ``healthy`` always None until checked). ``test_integration`` runs a
live health check for one integration and returns the same shape with
``healthy`` set. Tokens are read from the process env (loaded from
``~/.no_human/.env`` at the CLI/API boundary) — never from config, never
echoed back.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator

# Read the platform through a constant, never an inline `os.name` test, so the
# Windows branch below is reachable from a test on any host.
_IS_WINDOWS = os.name == "nt"

KIND_BY_NAME = {
    "jira": "issue_tracker",
    "linear": "issue_tracker",
    "monday": "issue_tracker",
    "github": "vcs",
    "gitlab": "vcs",
    "jenkins": "ci",
    "circleci": "ci",
    "slack": "notifications",
    "teams": "notifications",
}

# The order the UI lists them (issue tracker → VCS → CI → notifications).
_ORDER = ["jira", "linear", "monday", "github", "gitlab", "jenkins", "circleci",
          "slack", "teams"]


@dataclass
class IntegrationStatus:
    # One of the names in `_ORDER` below — that list is the source of truth.
    name: str
    kind: str            # "issue_tracker" | "vcs" | "ci" | "notifications"
    configured: bool
    healthy: bool | None  # None = never checked
    detail: str          # last check message, NEVER a secret
    # 'configured' (stored token/settings present) | 'ambient' (no stored
    # config, but the CLI the operator already uses — gh/git — is itself
    # authenticated, e.g. 36 PRs shipped via ambient `gh` auth with no
    # integration ever configured) | 'unconfigured'. Only github/gitlab are
    # ever 'ambient' — see `_AMBIENT_PROBES` below.
    status: str = "unconfigured"
    # Whether the integration's own on/off switch in
    # ``integrations.<name>.*`` is on. None = this integration HAS no such
    # switch (github/gitlab/jenkins are views over `ci.*`, whose on/off is
    # `ci.enabled`), so the UI must not render one.
    #
    # This is deliberately SEPARATE from `configured`. A Jira/Linear install
    # can have every setting filled in and still poll nothing, because
    # `nh serve` starts the poller only when `integrations.<name>.enabled` is
    # true (cli/commands.py) — and before this field existed the panel said
    # "Configured" for exactly that case, which is the "I don't have Linear"
    # report. Folding it into `configured` instead would have changed what
    # `configured` means for every existing caller and test.
    enabled: bool | None = None
    # UTC ISO-8601 timestamp of the last health probe (integrations/health.py),
    # or None if never probed. Only the health overlay sets this.
    checked_at: str | None = None


@dataclass
class FieldSpec:
    """One configurable field of an integration, for the settings UI's forms.

    Exactly one of ``env_var`` / ``config_path`` is set: secrets (API tokens)
    live in ``~/.no_human/.env``; everything else is a dotted path into the
    user's ``config.yaml``. Names/paths here are the ones the corresponding
    integration module ALREADY reads (see the modules cited per field below) —
    nothing here is invented.
    """
    name: str
    label: str
    secret: bool
    env_var: str | None = None
    config_path: str | None = None
    # Where-to-find-it help; the source of truth is integrations/help.py, and
    # the emitting functions below fill these from `help_for` so the wizard and
    # Settings render the same catalogue. Defaults keep every existing
    # FieldSpec() call site unchanged.
    help: str = ""
    help_url: str = ""


# github/gitlab/jenkins/circleci are STATUS VIEWS over the single shared `ci.*`
# section (see module docstring — one CI backend active at a time). Saving a
# field for one of them is how the UI selects it as that backend, so a
# successful save also pins `ci.backend` (+ `ci.enabled`) alongside whatever
# field was given.
#
# This map and FIELD_SPECS must not drift: an entry that writes into `ci.*`
# without a line here saves its settings and selects nothing, which is exactly
# how CircleCI shipped a form promising a gate it never created.
# `test_every_ci_kind_form_is_covered_by_the_autopin` derives one from the
# other rather than listing either, so a fifth backend cannot repeat it.
_CI_BACKEND_BY_NAME = {"github": "github_actions", "gitlab": "gitlab",
                       "jenkins": "jenkins", "circleci": "circleci"}

FIELD_SPECS: dict[str, list[FieldSpec]] = {
    # integrations/jira.py + intake/jira.py read integrations.jira.* / JIRA_API_TOKEN.
    "jira": [
        FieldSpec("site", "Site URL", False, config_path="integrations.jira.site"),
        FieldSpec("project_key", "Project key", False, config_path="integrations.jira.project_key"),
        FieldSpec("email", "Email", False, config_path="integrations.jira.email"),
        FieldSpec("jql", "JQL filter", False, config_path="integrations.jira.jql"),
        FieldSpec("api_token", "API token", True, env_var="JIRA_API_TOKEN"),
    ],
    # intake/linear.py reads integrations.linear.* / LINEAR_API_KEY. The key
    # header is the RAW key (`Authorization: <key>`), not `Bearer <key>` —
    # see intake/linear.py's docstring.
    "linear": [
        FieldSpec("team_key", "Team key", False, config_path="integrations.linear.team_key"),
        FieldSpec("label", "Label filter", False, config_path="integrations.linear.label"),
        FieldSpec("api_key", "API key", True, env_var="LINEAR_API_KEY"),
    ],
    # intake/monday.py reads integrations.monday.* / MONDAY_API_TOKEN. The
    # header is the RAW token (`Authorization: <token>`), not `Bearer <token>`
    # — same as Linear. `status_column` is the column's ID ("bug_status"), not
    # its title, because monday's mutations address columns by id.
    "monday": [
        FieldSpec("board_id", "Board id", False, config_path="integrations.monday.board_id"),
        FieldSpec("status_column", "Status column id", False,
                  config_path="integrations.monday.status_column"),
        FieldSpec("api_token", "API token", True, env_var="MONDAY_API_TOKEN"),
    ],
    # ci/circleci.py reads CIRCLECI_TOKEN; `ci.project` is the ONE key the CI
    # layer builds CircleCICI from — it is the API v2 project slug
    # "<vcs>/<org>/<repo>" (ci/__init__.py's circleci branch), which is why
    # this is one field and not the org_slug + project pair it used to be.
    # Named `project_slug` rather than `project` so the panel can give it its
    # own help text without that hint appearing on github/gitlab/jenkins.
    "circleci": [
        FieldSpec("project_slug", "Project slug (vcs/org/repo)", False,
                  config_path="ci.project"),
        FieldSpec("api_token", "API token", True, env_var="CIRCLECI_TOKEN"),
    ],
    # ci/github_actions.py + _ci_view read ci.project as the id_field for this backend.
    "github": [
        FieldSpec("project", "Project (owner/repo)", False, config_path="ci.project"),
    ],
    # ci/gitlab.py + _ci_view read the SAME ci.project key — one source of
    # truth, shared with github (only one backend is active at a time).
    "gitlab": [
        FieldSpec("project", "Project (namespace/repo)", False, config_path="ci.project"),
    ],
    # ci/jenkins.py reads ci.job (id_field) + JENKINS_USER / JENKINS_API_TOKEN.
    "jenkins": [
        FieldSpec("job", "Job path", False, config_path="ci.job"),
        FieldSpec("user", "Jenkins user", True, env_var="JENKINS_USER"),
        FieldSpec("api_token", "API token", True, env_var="JENKINS_API_TOKEN"),
    ],
    # cli/commands.py + api/app.py read notifications.slack_webhook_url. It is
    # a secret (never echoed back) even though it lives in config.yaml, not
    # .env — that is the location the existing code already reads it from.
    "slack": [
        FieldSpec("webhook_url", "Webhook URL", True, config_path="notifications.slack_webhook_url"),
    ],
    # notify/teams.py reads notifications.teams_webhook_url. Secret for the
    # same reason Slack's is: the Power Automate URL carries its own SAS
    # credential (`sp`/`sv`/`sig`) in the query string.
    "teams": [
        FieldSpec("webhook_url", "Webhook URL", True, config_path="notifications.teams_webhook_url"),
    ],
}


class RepoNotRegistered(ValueError):
    """Raised when `default_repo` names a path that is not a registered repo
    profile. A subclass of ValueError so existing `except ValueError` sites
    still catch it, but distinct so the API can map it to 400 (a bad value)
    rather than 422 (a malformed/credential field)."""


def _sect(config: dict, key: str) -> dict:
    """A config sub-section, tolerating a null (the deep-merge shadowing trap)."""
    return (config or {}).get(key) or {}


# --------------------------------------------------------------------------- #
# Pure status derivation (one function per integration)                        #
# --------------------------------------------------------------------------- #

def _status_str(configured: bool) -> str:
    return "configured" if configured else "unconfigured"


def _jira_status(config: dict) -> IntegrationStatus:
    j = _sect(config, "integrations").get("jira") or {}
    configured = bool(j.get("site") and j.get("project_key") and j.get("email"))
    detail = f"{j['site']} · {j['project_key']}" if configured else "not configured"
    return IntegrationStatus("jira", "issue_tracker", configured, None, detail,
                              status=_status_str(configured))


def _linear_status(config: dict) -> IntegrationStatus:
    """Linear needs a team key AND state types that are really state types.

    Reporting "configured" on the presence of a team key alone was a lie of
    the same family ``_monday_status`` documents: ``state_types`` holding
    anything outside Linear's seven ``WorkflowState.type`` strings matches no
    issue at all, so the card showed green next to an integration that could
    only ever poll nothing. The value is not checked against the API here —
    this function is pure and is called on every settings render — but the type
    strings are a fixed documented list, so THAT half costs nothing.
    """
    from ..intake.linear import state_type_problems

    lin = _sect(config, "integrations").get("linear") or {}
    team = lin.get("team_key")
    raw_types = lin.get("state_types")
    # An UNSET value is the shipped default and is fine; a SET one must be
    # real. `raw_types` is passed through untouched (never coerced with
    # `list()`, which would silently turn the string "backlog" into six
    # single-character "types" and report six problems for one mistake).
    problems = state_type_problems(raw_types) if raw_types else []
    configured = bool(team) and not problems
    if configured:
        label = lin.get("label")
        detail = f"team {team}" + (f" · label {label}" if label else "")
    elif problems:
        detail = ("integrations.linear.state_types is not usable: "
                  + "; ".join(problems)
                  + " — every poll would return nothing")
    else:
        detail = "not configured"
    return IntegrationStatus("linear", "issue_tracker", configured, None, detail,
                              status=_status_str(configured))


def _monday_status(config: dict) -> IntegrationStatus:
    """monday needs BOTH a board and a status column to be usable.

    Reporting "configured" on a board id alone would be a lie: without
    `status_column` the adapter cannot filter intake at all and raises, so the
    integration card would show green next to an integration that cannot run.
    """
    mon = _sect(config, "integrations").get("monday") or {}
    board = mon.get("board_id")
    column = mon.get("status_column")
    configured = bool(board and column)
    if configured:
        labels = mon.get("todo_labels") or []
        detail = f"board {board} · {column}"
        if labels:
            detail += " · " + ", ".join(str(x) for x in labels)
    elif board:
        detail = "board set, but integrations.monday.status_column is unset"
    else:
        detail = "not configured"
    return IntegrationStatus("monday", "issue_tracker", configured, None, detail,
                              status=_status_str(configured))


def _teams_status(config: dict) -> IntegrationStatus:
    # The webhook is a secret — report only that one is set, never the URL.
    # A RETIRED Office 365 connector URL is reported as configured-but-broken
    # rather than as a working channel: Microsoft disabled those endpoints in
    # May 2026, so it can never deliver and saying "configured" would hide
    # that until an alert failed to arrive.
    from ..notify.teams import is_retired_connector_url

    url = _sect(config, "notifications").get("teams_webhook_url")
    configured = bool(url)
    if configured and is_retired_connector_url(url):
        return IntegrationStatus(
            "teams", "notifications", True, False,
            "retired Office 365 connector URL — replace with a Power Automate "
            "Workflows webhook", status="configured")
    detail = "webhook configured" if configured else "not configured"
    return IntegrationStatus("teams", "notifications", configured, None, detail,
                              status=_status_str(configured))


#: What a pre-move install has on disk: settings under `integrations.circleci`
#: and no `ci:` block. That config NEVER produced a CI gate, so promoting it to
#: `ci.backend: circleci` + `ci.enabled: true` on upgrade would switch on a gate
#: the operator never had, silently, and start blocking their PRs on a pipeline
#: that has never run for them. It is reported unconfigured — which is the
#: truth — and the detail says the one thing that fixes it.
#: The second clause is not padding. Saving ANY CI form pins `ci.backend`
#: (see `_CI_BACKEND_BY_NAME`), so an operator who has a live `ci:` block on
#: another backend AND a stale `integrations.circleci` block would, by
#: following this nudge, move their CI gate onto CircleCI. Telling them that
#: up front is the difference between an instruction and a trap.
_CIRCLECI_LEGACY_DETAIL = (
    "settings found under integrations.circleci, which no CI backend reads — "
    "re-save the CircleCI form to activate the gate (this makes CircleCI your "
    "ci.backend)"
)


def _circleci_status(config: dict) -> IntegrationStatus:
    """CircleCI as a view over ``ci.*``, plus a named nudge for the legacy block."""
    status = _ci_view(config, "circleci", "circleci", "ci", "project", "CircleCI")
    if status.configured:
        return status
    legacy = _sect(config, "integrations").get("circleci") or {}
    # `enabled` counts as evidence of the legacy block on its own. An operator
    # who switched CircleCI ON but never filled the slug in is the one MOST
    # likely to believe a gate is running, and matching only on the two data
    # fields gave exactly them a bare "not configured" with no way to learn
    # why. Any key present means the block is theirs and the nudge applies.
    if any(legacy.get(k) for k in ("enabled", "org_slug", "project")):
        return replace(status, detail=_CIRCLECI_LEGACY_DETAIL)
    return status


def _ci_view(config: dict, name: str, backend: str, kind: str,
             id_field: str, label: str) -> IntegrationStatus:
    """A status view over ``ci.*`` for a backend the CI layer already owns."""
    ci = _sect(config, "ci")
    configured = bool(ci.get("enabled") and ci.get("backend") == backend and ci.get(id_field))
    detail = f"{label} · {ci[id_field]}" if configured else "not configured"
    return IntegrationStatus(name, kind, configured, None, detail,
                              status=_status_str(configured))


def _github_status(config: dict) -> IntegrationStatus:
    return _ci_view(config, "github", "github_actions", "vcs", "project", "GitHub Actions")


def _gitlab_status(config: dict) -> IntegrationStatus:
    return _ci_view(config, "gitlab", "gitlab", "vcs", "project", "GitLab CI")


def _jenkins_status(config: dict) -> IntegrationStatus:
    return _ci_view(config, "jenkins", "jenkins", "ci", "job", "Jenkins")


def _slack_status(config: dict) -> IntegrationStatus:
    # The webhook is a secret — report only that one is set, never the URL.
    configured = bool(_sect(config, "notifications").get("slack_webhook_url"))
    detail = "webhook configured" if configured else "not configured"
    return IntegrationStatus("slack", "notifications", configured, None, detail,
                              status=_status_str(configured))


_STATUS = {
    "jira": _jira_status, "linear": _linear_status, "monday": _monday_status,
    "github": _github_status,
    "gitlab": _gitlab_status, "jenkins": _jenkins_status,
    "circleci": _circleci_status, "slack": _slack_status, "teams": _teams_status,
}


def _status_for(name: str, config: dict) -> IntegrationStatus:
    """One integration's status, with the ``enabled`` on/off switch filled in
    from its own config block (see :func:`enable_field` — None when it has
    none).

    A config block with no status function yet (a NEW integration, added to
    DEFAULT_CONFIG before the registry catches up) reports unconfigured rather
    than raising: `setup_specs` discovers blocks, so it would otherwise crash
    the whole wizard on the day someone adds one."""
    derive = _STATUS.get(name)
    if derive is None:
        return IntegrationStatus(name, KIND_BY_NAME.get(name, ""), False, None,
                                 "not configured",
                                 enabled=enable_state(config, name))
    return replace(derive(config), enabled=enable_state(config, name))


def list_integrations(config: dict) -> list[IntegrationStatus]:
    """Every integration's configured/kind status. Pure; ``healthy`` is None."""
    return [_status_for(name, config) for name in _ORDER]


# --------------------------------------------------------------------------- #
# Ambient CLI-auth detection (SCRUM-81).                                       #
#                                                                               #
# Some providers work with no integration ever configured here because the    #
# operator's own CLI is already authenticated (e.g. `gh`) — this install has   #
# shipped merged GitHub PRs entirely via ambient `gh`/git auth while the panel #
# still said "Unconfigured". These probes are read-only: they never write a    #
# credential anywhere, and never surface a token/secret value.                 #
# --------------------------------------------------------------------------- #

def _run_probe(
    cmd: list[str], *, timeout: float = 2.0, input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, input=input_text, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_credential_present(host: str) -> bool:
    """Ask git's OWN credential subsystem whether it can produce a credential
    for ``host``, without prompting and without a network round-trip:
    `git credential fill` consults whatever helper is configured (netrc,
    credential store, osxkeychain, manager, `gh auth git-credential`, ...) and
    returns immediately if none has anything — GIT_TERMINAL_PROMPT=0 + a no-op
    GIT_ASKPASS guarantee it never blocks waiting on input. Only WHETHER a
    non-empty `password=` line came back is inspected — never its value — so
    this can never leak a secret. A username alone (e.g. a bare
    `credential.<host>.username` config entry with no stored password) is a
    preference, not proof of an authenticated session, and must not read as
    ambient. `fill` only reads; storing is `approve`/`reject`, never issued
    here.

    The credential's lifetime is bounded to this frame on purpose: `stdout`
    carries `password=<TOKEN>`, and a function's locals stay reachable from a
    traceback for as long as the frame does, so the reply is cleared before
    returning either way. Nothing in this package renders frame locals and
    nothing after the read can raise, so that was never exploitable — it is
    done anyway, because the alternative is a containment claim that promises
    more than its mechanism delivers."""
    # GIT_ASKPASS must name a program that exists and exits 0 silently.
    # `/usr/bin/true` is not one on Windows, and Windows ships no equivalent
    # single binary, so do not invent a path there: GIT_TERMINAL_PROMPT=0
    # already refuses the terminal prompt, and GCM_INTERACTIVE=never is the
    # matching knob for Git Credential Manager, which is the thing that would
    # otherwise open a GUI dialog. (`_run_probe` also bounds this at 2s, so a
    # prompt could stall the probe but never wedge the process.)
    # UNTESTED ON WINDOWS.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if _IS_WINDOWS:
        env["GCM_INTERACTIVE"] = "never"
    else:
        env["GIT_ASKPASS"] = "/usr/bin/true"
    proc = _run_probe(
        ["git", "credential", "fill"],
        input_text=f"protocol=https\nhost={host}\n\n", env=env,
    )
    if proc is None or proc.returncode != 0:
        return False
    try:
        return any(
            line.startswith("password=") and line != "password="
            for line in proc.stdout.splitlines()
        )
    finally:
        proc.stdout = ""
        del proc


#: Token variables `gh` itself prefers over any stored credential. Presence of
#: one is checked, never its value.
_GH_TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


def _gh_hosts_path() -> Path:
    """`gh`'s `hosts.yml`, resolved the way gh resolves it: `GH_CONFIG_DIR`,
    else `XDG_CONFIG_HOME/gh`, else `~/.config/gh`."""
    base = os.environ.get("GH_CONFIG_DIR") or ""
    if not base:
        xdg = os.environ.get("XDG_CONFIG_HOME") or ""
        base = str(Path(xdg) / "gh") if xdg else str(Path.home() / ".config" / "gh")
    return Path(base) / "hosts.yml"


def _gh_hosts_block_lines(text: str, host: str) -> Iterator[str]:
    """Yield only the lines of a `hosts.yml` that sit under the top-level
    ``host:`` key. `hosts.yml` is a map of HOST -> settings, so a token in it
    belongs to whichever host block encloses it; a scan that ignores the
    enclosing block reports a credential for an enterprise host as if it were
    a github.com one. (`gh auth status` had exactly that any-host semantics —
    "exits 0 iff at least one host is logged in" — so scoping this is a
    tightening rather than a regression, but it has to agree with
    `_git_credential_present`, which asks about one host by name.)

    A top-level key is a line starting in column 0; everything indented after
    it belongs to that block, which is all the structure this needs and is why
    it does not pull in a YAML parser (one would bind the whole parsed tree,
    tokens included, to a local)."""
    in_block = False
    for line in text.splitlines():
        if line[:1] not in (" ", "\t", ""):
            in_block = line.split(":", 1)[0].strip().strip("\"'") == host
        elif in_block:
            yield line


def _is_gh_oauth_token_line(line: str) -> bool:
    """True iff *line* is a `hosts.yml` `oauth_token:` entry with something
    after the colon. Takes a line and returns a bool: the value is touched only
    inside this frame, is never returned, stored, logged or compared against
    anything, and nothing here can raise (so it can never reach a traceback
    either). Empty and `""`/`''` placeholders are not a credential."""
    stripped = line.strip()
    if not stripped.startswith("oauth_token:"):
        return False
    return bool(stripped[len("oauth_token:"):].strip().strip("\"'"))


def _probe_github_ambient() -> bool:
    """Is a GitHub credential PRESENT on this machine? Presence only — never
    validity — and with no network round-trip.

    WHY THIS IS NOT `gh auth status`, which it was until 2026-08-02: that
    command is not a local check. It validates the stored token against the
    GitHub API — measured on a dev machine at 1700 ms and 2036 ms, against
    85-93 ms for the local credential read below and ~10 ms for a file read.
    So the old probe TRANSMITTED THE OPERATOR'S GITHUB TOKEN, undisclosed, and
    it did so precisely when GitHub was UNCONFIGURED, since that is the only
    time an ambient probe runs — inverting the one guarantee the reader who
    configured nothing is entitled to. Deciding whether an integration is worth
    OFFERING needs presence, not validity; an expired token fails later,
    visibly, at the point of use, which is a better place to learn it.

    Three local sources, each covering a case the others cannot see, checked
    cheapest-first and short-circuiting:

    1. `GH_TOKEN` / `GITHUB_TOKEN` in the environment — what gh itself prefers
       over stored credentials, and invisible to (2).
    2. git's credential subsystem for github.com, via the same
       `_git_credential_present` helper `_probe_gitlab_ambient` uses. This is
       the case that matters most in practice: `gh auth login` stores the token
       in the OS keyring (not in a file) and registers
       `gh auth git-credential` as github.com's helper, so this is the only
       local way to see a keyring-stored login.
    3. a non-empty `oauth_token:` line **inside the `github.com:` block** of
       gh's `hosts.yml` — a gh login on a machine with no keyring, or where
       `gh auth setup-git` never ran so (2) has no helper to ask. Host-scoped,
       because a token under `ghe.corp.example:` is a credential for that host
       and reporting github.com as ambient on the strength of it would be the
       false "yes" this docstring calls a lie. The cost is that a GHE-only
       login no longer reads as ambient here; that is the fail-closed side.

    Only WHETHER a non-empty token exists is ever inspected. No value is
    returned, logged, stored, or put in an exception message, and none survives
    the frame that inspects it — see the lifetime note in
    `_git_credential_present`. `gh auth token`, which prints the token in the
    clear, is deliberately not used. What this does NOT claim is that no value
    is ever bound at all: `read_text` necessarily holds the file, and a
    credential helper's reply necessarily exists for the length of the check.

    Fails CLOSED — if it cannot tell, it reports "not present" and the panel
    says "Unconfigured". A false "no" costs a suggestion; a false "yes" is a
    lie, and the only ways to shrink that gap further would be to prompt the
    operator for keychain access or to validate on the wire, which is the
    defect this replaces."""
    if any(os.environ.get(v, "").strip() for v in _GH_TOKEN_ENV_VARS):
        return True
    if _git_credential_present("github.com"):
        return True
    try:
        # errors="replace" so an undecodable byte fails closed to False rather
        # than raising a ValueError past the OSError guard.
        return any(
            _is_gh_oauth_token_line(line)
            for line in _gh_hosts_block_lines(
                _gh_hosts_path().read_text(errors="replace"), "github.com")
        )
    except OSError:
        return False


def _probe_gitlab_ambient() -> bool:
    """Is a GitLab credential present? Same local, presence-only mechanism as
    the GitHub probe — see `_git_credential_present`."""
    return _git_credential_present("gitlab.com")


# Only github/gitlab have an ambient path — jira/circleci/jenkins/slack have no
# equivalent "already authenticated CLI" concept.
_AMBIENT_PROBES: dict[str, Callable[[], bool]] = {
    "github": _probe_github_ambient,
    "gitlab": _probe_gitlab_ambient,
}

_AMBIENT_TTL_SECONDS = 60.0

# Process-lifetime cache, keyed by provider name → (checked_at, result). This
# app has no multi-user/session concept (single-operator local tool — see
# `_require_local_origin`), so the running server process IS the "session";
# the cache never stores a credential, only a bool + timestamp, and evaporates
# on restart. Tests inject their own `cache=` dict to isolate state.
_AMBIENT_CACHE: dict[str, tuple[float, bool]] = {}


def ambient_available(
    name: str, *, cache: dict[str, tuple[float, bool]] | None = None, now: float | None = None,
) -> bool:
    """Is ``name`` reachable via ambient CLI auth right now? Cached for
    ``_AMBIENT_TTL_SECONDS`` so a burst of requests within the window doesn't
    repeatedly shell out to `gh`/`git`."""
    probe = _AMBIENT_PROBES.get(name)
    if probe is None:
        return False
    if cache is None:
        cache = _AMBIENT_CACHE
    ts = time.monotonic() if now is None else now
    cached = cache.get(name)
    if cached is not None and (ts - cached[0]) < _AMBIENT_TTL_SECONDS:
        return cached[1]
    result = probe()
    cache[name] = (ts, result)
    return result


_AMBIENT_DETAIL = "available via ambient CLI auth"


def list_integrations_with_ambient(
    config: dict, *, cache: dict[str, tuple[float, bool]] | None = None, now: float | None = None,
) -> list[IntegrationStatus]:
    """``list_integrations`` plus the ambient-auth overlay: an unconfigured
    github/gitlab whose CLI is already authenticated is reported as
    ``status="ambient"`` instead of ``"unconfigured"`` (``configured`` stays
    False — no stored settings exist)."""
    out = []
    for s in list_integrations(config):
        if not s.configured and ambient_available(s.name, cache=cache, now=now):
            s = replace(s, status="ambient", detail=_AMBIENT_DETAIL)
        out.append(s)
    return out


def list_integrations_with_health(config: dict) -> list[IntegrationStatus]:
    """`list_integrations_with_ambient` + the health overlay (integrations/health.py)."""
    from .health import overlay
    return overlay(list_integrations_with_ambient(config))


# --------------------------------------------------------------------------- #
# Write path (settings UI): FIELD_SPECS-validated save + field/set reporting.  #
#                                                                               #
# Paths are always resolved from the config module's ENV_PATH/CONFIG_PATH      #
# ATTRIBUTES (looked up fresh on every call, never captured as a default       #
# parameter) so that tests can monkeypatch them onto tmp_path and this code    #
# picks it up — the same discipline api/app.py's _persist_onboarding uses.     #
# --------------------------------------------------------------------------- #

def _get_dotted(config: dict, dotted: str) -> Any:
    node: Any = config or {}
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set_dotted(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


# Single implementation lives in config, which owns ENV_PATH and every other
# .env read; aliased here so existing call sites are untouched.
from ..config import atomic_write_0600 as _atomic_write_0600  # noqa: E402
from ..config import upsert_env_var as _upsert_env_var  # noqa: E402


def _write_config_values(config_path: Path, updates: dict[str, Any]) -> None:
    """Read-modify-write config.yaml: read the RAW user file (never the
    defaults-merged view — the deep-merge shadowing trap), set the dotted
    path(s), and write back preserving every other key untouched."""
    import yaml

    from .. import config as _config_mod

    try:
        on_disk = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    except (yaml.YAMLError, OSError):
        on_disk = {}
    on_disk = on_disk or {}
    for dotted, value in updates.items():
        _set_dotted(on_disk, dotted, value)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _config_mod._atomic_write_text(config_path, yaml.safe_dump(on_disk, sort_keys=False))


def _field_is_set(spec: FieldSpec, config: dict) -> bool:
    if spec.env_var:
        from .. import config as _config_mod
        status = _config_mod.credential_status([spec.env_var], _config_mod.ENV_PATH)
        return bool(status.get(spec.env_var, False))
    return bool(_get_dotted(config, spec.config_path))


def integration_fields(name: str, config: dict) -> list[dict[str, Any]]:
    """The field descriptors for one integration's settings form — never a
    secret VALUE, only whether each field currently ``set``. ``help``/``help_url``
    come from the shared catalogue (integrations/help.py) so Settings and the
    wizard say the same thing about each field."""
    from .help import help_for
    out = []
    for s in FIELD_SPECS.get(name, []):
        text, url = help_for(name, s.name)
        out.append({"name": s.name, "label": s.label, "secret": s.secret,
                    "set": _field_is_set(s, config), "help": text, "help_url": url})
    return out


def save_integration_config(name: str, fields: dict[str, str]) -> IntegrationStatus:
    """Validate ``fields`` against ``FIELD_SPECS[name]`` and persist them:
    secrets to ``~/.no_human/.env``, everything else to ``config.yaml``.
    Returns the refreshed :class:`IntegrationStatus`. Raises ``ValueError`` for
    an unknown integration or an unknown field name; never logs or returns a
    secret value."""
    specs = FIELD_SPECS.get(name)
    if specs is None:
        raise ValueError(f"unknown integration: {name!r}")

    by_field = {s.name: s for s in specs}
    unknown = sorted(set(fields) - set(by_field))
    if unknown:
        raise ValueError(f"unknown field(s) for integration {name!r}: {', '.join(unknown)}")

    # A value that is not exactly ONE .env line could inject arbitrary extra
    # entries (e.g. a forged CLAUDE_CODE_OAUTH_TOKEN= or ANTHROPIC_API_KEY=
    # line) — refuse before any write is dispatched. Never echo the offending
    # value back.
    #
    # This checked only \n and \r while the writer's own guard rejects every
    # separator `splitlines()` honours. The eight it missed therefore reached
    # the write loop below, which writes ONE KEY AT A TIME: the first key
    # landed on disk before a later one was refused, leaving .env half-updated
    # and the caller with a 500. Sharing the writer's guard is what makes the
    # loop effectively all-or-nothing.
    from ..config import AuthError, assert_single_env_line

    bad = []
    for f, v in sorted(fields.items()):
        if not isinstance(v, str):
            continue
        try:
            assert_single_env_line(v)
        except AuthError:
            bad.append(f)
    if bad:
        raise ValueError(
            f"field value(s) for integration {name!r} must be a single line: "
            f"{', '.join(bad)}"
        )

    from .. import config as _config_mod

    env_updates: dict[str, str] = {}
    config_updates: dict[str, Any] = {}
    for field_name, value in fields.items():
        spec = by_field[field_name]
        if spec.env_var:
            env_updates[spec.env_var] = value
        else:
            config_updates[spec.config_path] = value

    if fields and name in _CI_BACKEND_BY_NAME:
        config_updates.setdefault("ci.backend", _CI_BACKEND_BY_NAME[name])
        config_updates.setdefault("ci.enabled", True)

    for key, value in env_updates.items():
        _upsert_env_var(_config_mod.ENV_PATH, key, value)
    if config_updates:
        _write_config_values(_config_mod.CONFIG_PATH, config_updates)

    refreshed = _config_mod.load_config(_config_mod.CONFIG_PATH)
    status = _status_for(name, refreshed.data)
    # Saving (or clearing) a field must not make an ambiently-authenticated
    # github/gitlab look worse than the list endpoint already reports it —
    # overlay the same ambient check here (see list_integrations_with_ambient).
    if not status.configured and ambient_available(name):
        status = replace(status, status="ambient", detail=_AMBIENT_DETAIL)
    return status


# --------------------------------------------------------------------------- #
# Setup surface (onboarding "Connect your tools")                              #
#                                                                               #
# The wizard's integrations step is generated from THIS, not from a list of    #
# names typed into the UI: `setup_specs` walks whatever blocks exist under     #
# `DEFAULT_CONFIG["integrations"]`, so a sixth integration block appears in    #
# onboarding with no JSX change at all.                                        #
#                                                                               #
# HARD RULE, and the reason this is a separate surface from                    #
# `save_integration_config` above: NOTHING here ever writes a credential into  #
# config.yaml. Onboarding collects the non-secret settings (team key, project  #
# key, filters, on/off) and NAMES the ~/.no_human/.env variable the secret     #
# belongs in — it never accepts the secret itself. `assert_config_safe_field`  #
# is the enforcement point and every write goes through it.                    #
# --------------------------------------------------------------------------- #

#: Which ``~/.no_human/.env`` variable(s) hold each integration's credential,
#: and the module that reads them. Values are NEVER read here — this maps a
#: name to a name, so the wizard can tell the operator what to export.
SETUP_SECRET_ENV: dict[str, tuple[str, ...]] = {
    "jira": ("JIRA_API_TOKEN",),                       # intake/jira.py
    "linear": ("LINEAR_API_KEY",),                     # intake/linear.py
    "monday": ("MONDAY_API_TOKEN",),                   # intake/monday.py
    # No circleci entry: like github/gitlab/jenkins it has no
    # `integrations.circleci` block, so `setup_specs` never reaches it and an
    # entry here would be unreachable. CIRCLECI_TOKEN is collected by the
    # Settings form (FIELD_SPECS above), which is where the other CI backends'
    # credentials are collected too.
    # integrations/slack/worker.py — Socket Mode needs both.
    "slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    "teams": (),                                       # see SETUP_SECRET_NOTE
}

#: Plain-language note about an integration's credential, shown next to (or
#: instead of) the env-var names. Teams is the awkward case and says so: its
#: Power Automate URL is a secret that the code it feeds
#: (notify.build_notifier) reads from ``notifications.teams_webhook_url`` in
#: config.yaml, so onboarding refuses to collect it and points at the one
#: place that already handles it.
SETUP_SECRET_NOTE: dict[str, str] = {
    "teams": (
        "The Power Automate webhook URL carries its own credential in the "
        "query string. Onboarding will not take it — paste it in "
        "Settings → Integrations → Microsoft Teams."
    ),
}

#: Every ``config.yaml`` path that FIELD_SPECS marks as a secret, and every
#: field NAME it marks as a secret. Derived, not typed out: these are the two
#: independent registries that already know what a credential is, so the guard
#: below tracks them instead of restating them.
_SECRET_CONFIG_PATHS = frozenset(
    s.config_path for specs in FIELD_SPECS.values() for s in specs
    if s.secret and s.config_path
)
_SECRET_FIELD_NAMES = frozenset(
    s.name for specs in FIELD_SPECS.values() for s in specs if s.secret
)
_SECRET_ENV_FIELD_NAMES = frozenset(
    s.name for specs in FIELD_SPECS.values() for s in specs if s.env_var
)
#: The mirror image: field names the registry explicitly declares NON-secret.
#: Used only to resolve the `*_key` ambiguity below — never to override a
#: secret declaration, which is checked first.
_NONSECRET_FIELD_NAMES = frozenset(
    s.name for specs in FIELD_SPECS.values() for s in specs if not s.secret
)

#: Last-resort name heuristic, for a credential that no registry knows about
#: yet — a NEW integration block whose author forgot to declare it. These are
#: words that are only ever credentials.
_CREDENTIAL_NAME_RE = re.compile(
    r"(?:^|_)(?:token|secret|password|passwd|pwd|credential|credentials|"
    r"apikey|webhook|cookie|signature|sig|bearer|oauth|auth|pat)(?:_|$)",
    re.IGNORECASE,
)

#: `*_key` cannot be decided by the word alone: Jira's `project_key` and
#: Linear's `team_key` are plain settings, while PagerDuty's `routing_key` and
#: anyone's `api_key` are credentials. So it is resolved by DECLARATION, not by
#: spelling, and it fails CLOSED — see :func:`is_credential_name`.
_AMBIGUOUS_KEY_RE = re.compile(r"(?:^|_)key$", re.IGNORECASE)


def is_credential_name(field: str) -> bool:
    """Would a field called *field* hold a credential?

    Four oracles, checked in order; any "yes" wins:

    1. FIELD_SPECS declares a field of that name secret.
    2. FIELD_SPECS routes a field of that name to ``.env`` (which makes it a
       secret by construction).
    3. The name contains a word that is only ever a credential.
    4. The name ends in ``key`` AND no FIELD_SPEC declares it non-secret.

    (4) is the FAIL-CLOSED rule, and it is what makes this useful against the
    field nobody has thought of yet: an unrecognised ``*_key`` on a NEW
    integration is assumed to be a credential until someone deliberately
    declares otherwise. The cost of a false positive is one setting missing
    from the wizard; the cost of a false negative is a token in a
    world-readable file."""
    if field in _SECRET_FIELD_NAMES or field in _SECRET_ENV_FIELD_NAMES:
        return True
    if _CREDENTIAL_NAME_RE.search(field):
        return True
    return bool(_AMBIGUOUS_KEY_RE.search(field)) and field not in _NONSECRET_FIELD_NAMES


def assert_config_safe_field(name: str, field: str) -> None:
    """Raise ``ValueError`` unless ``integrations.<name>.<field>`` is a field
    onboarding may write into config.yaml.

    Three conditions, all required:

    1. ``name`` is a real integration block.
    2. ``field`` is not a credential — not a path FIELD_SPECS marks secret,
       not a name it marks secret or routes to .env, and not credential-shaped
       (:func:`is_credential_name`).
    3. ``field`` is a real key of ``DEFAULT_CONFIG["integrations"][name]``,
       which stops an arbitrary dotted path being injected through the API.

    (2) is checked BEFORE (3) deliberately. Ordered the other way, the
    credential rule is only ever reached for a field that already exists in
    the block, so for today's five integrations the "unknown setting" check
    would be the one actually doing the work and the credential rule would sit
    there untested — a guard whose coverage nothing observes. This way every
    credential-shaped field is refused BY THE CREDENTIAL RULE, and says so.
    """
    from ..config import DEFAULT_CONFIG

    block = (DEFAULT_CONFIG.get("integrations") or {}).get(name)
    if block is None:
        raise ValueError(f"unknown integration: {name!r}")
    if f"integrations.{name}.{field}" in _SECRET_CONFIG_PATHS or is_credential_name(field):
        raise ValueError(
            f"refusing to write {name}.{field} to config.yaml: it is a "
            f"credential. Secrets belong in ~/.no_human/.env"
        )
    if field not in block:
        raise ValueError(
            f"unknown setting for integration {name!r}: {field!r}")


def enable_field(name: str) -> str | None:
    """The name of *name*'s on/off key in its own config block, or None when
    it has none (github/gitlab/jenkins are views over ``ci.*``).

    Discovered from the defaults rather than listed: ``enabled`` when the
    block has one, else its single boolean key — which is how
    ``integrations.slack`` (whose switch is ``intake``, the Socket-Mode
    worker) is handled without naming it here."""
    from ..config import DEFAULT_CONFIG

    block = (DEFAULT_CONFIG.get("integrations") or {}).get(name)
    if not isinstance(block, dict):
        return None
    if isinstance(block.get("enabled"), bool):
        return "enabled"
    bools = [k for k, v in block.items() if isinstance(v, bool)]
    return bools[0] if len(bools) == 1 else None


def enable_default(name: str) -> bool:
    """The shipped default of *name*'s on/off key."""
    from ..config import DEFAULT_CONFIG

    field = enable_field(name)
    if field is None:
        return False
    block = (DEFAULT_CONFIG.get("integrations") or {}).get(name) or {}
    return bool(block.get(field, False))


def enable_state(config: dict, name: str) -> bool | None:
    """Is *name* switched on in *config*? None when it has no switch."""
    field = enable_field(name)
    if field is None:
        return None
    block = _sect(config, "integrations").get(name) or {}
    return bool(block.get(field, enable_default(name)))


def _humanize(field: str) -> str:
    """`project_key` → `Project key`. Generated, so a new setting gets a
    readable label without anyone maintaining a table of them."""
    words = field.replace("-", "_").split("_")
    return " ".join([words[0].capitalize(), *words[1:]]) if words else field


def _setup_label(name: str, field: str) -> str:
    """The label for one setting: FIELD_SPECS' hand-written one when that
    registry already describes this field (so the wizard and Settings say the
    same thing — "Site URL", "JQL filter", not "Site" and "Jql"), else a
    generated one, so a field no registry knows about still reads."""
    # `default_repo` is a plain config string, so it would otherwise humanize
    # to "Default repo" — a real user could not tell that meant "where the
    # coder runs a pulled-in ticket". Name it for what it does.
    if field == "default_repo":
        return "Run tasks in repo"
    for spec in FIELD_SPECS.get(name, []):
        if spec.name == field and not spec.secret:
            return spec.label
    return _humanize(field)


def _setup_fields(name: str, defaults: dict, current: dict,
                  repos: list[str] | None = None) -> list[dict[str, Any]]:
    """The non-secret, renderable settings of one integration block.

    Kinds are derived from the DEFAULT's type: bool → a checkbox, str → a text
    box, list-of-str → a comma list. Anything else (None, nested dict, mixed
    list) is skipped rather than guessed at — a field the wizard cannot render
    honestly must not be rendered at all.

    ``default_repo`` is the one exception: it is a ``repo_select`` — a dropdown
    over ``repos`` (the operator's registered repo profiles) rather than free
    text, so a pulled-in ticket can only ever name a repo no_human actually
    knows. ``repos`` is threaded in from the API (the store owns profiles);
    ``None`` means "caller had no list", which renders an empty select."""
    from .help import help_for

    out: list[dict[str, Any]] = []
    for key, default in defaults.items():
        try:
            assert_config_safe_field(name, key)
        except ValueError:
            continue  # a credential, or not writable — never offered
        value = current.get(key, default)
        options = None
        if key == "default_repo":
            kind, value, options = "repo_select", str(value or ""), list(repos or [])
        elif isinstance(default, bool):
            kind, value = "bool", bool(value)
        elif isinstance(default, str):
            kind, value = "text", str(value or "")
        elif isinstance(default, list) and all(isinstance(x, str) for x in default):
            kind = "list"
            value = [str(x) for x in (value if isinstance(value, list) else default)]
        else:
            continue
        text, url = help_for(name, key)
        field = {"name": key, "label": _setup_label(name, key),
                 "kind": kind, "value": value, "help": text, "help_url": url}
        if options is not None:
            field["options"] = options
        out.append(field)
    return out


def setup_specs(config: dict, repos: list[str] | None = None) -> list[dict[str, Any]]:
    """Everything the onboarding step needs to render itself, discovered from
    ``DEFAULT_CONFIG["integrations"]``.

    Per integration: its current values (non-secret only), which key is its
    on/off switch, whether it is on, whether that switch *ships* on (a mute
    switch, e.g. teams, vs. an opt-in the user must flip themselves), the
    .env variable names its credential needs and whether each is already
    set. Never a secret VALUE — the credential fields report ``set: bool``
    exactly like :func:`integration_fields` does."""
    from .. import config as _config_mod
    from ..config import DEFAULT_CONFIG

    out: list[dict[str, Any]] = []
    for name, defaults in (DEFAULT_CONFIG.get("integrations") or {}).items():
        if not isinstance(defaults, dict):
            continue
        current = _sect(config, "integrations").get(name) or {}
        env_vars = SETUP_SECRET_ENV.get(name, ())
        set_map = _config_mod.credential_status(list(env_vars), _config_mod.ENV_PATH) \
            if env_vars else {}
        status = _status_for(name, config)
        out.append({
            "name": name,
            "kind": KIND_BY_NAME.get(name, ""),
            "enable_field": enable_field(name),
            "enabled": enable_state(config, name),
            "enable_default": enable_default(name),
            "configured": status.configured,
            # A green "Ready" mark requires a live connection test to have
            # PASSED — not merely that a key was typed. `verified` is the
            # persisted record of that pass (integrations.<name>.last_verified_at,
            # written by `mark_verified`); the wizard also flips it true on an
            # in-session passing test. See web/src/integrationSetup.js readiness.
            "verified": bool(current.get("last_verified_at")),
            "detail": status.detail,
            "fields": _setup_fields(name, defaults, current, repos),
            "secrets": [{"env_var": v, "set": bool(set_map.get(v, False))} for v in env_vars],
            "secret_note": SETUP_SECRET_NOTE.get(name, ""),
        })
    return out


def _coerce_setup_value(name: str, field: str, default: Any, value: Any) -> Any:
    """Coerce one submitted value to the type its default declares, refusing
    anything that cannot be one. Strings are single-line: a newline in a
    config value is how a YAML write turns into two settings."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ValueError(f"{name}.{field} must be true or false")
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{name}.{field} must be text")
        if len(value.splitlines()) > 1 or value != value.strip("\r\n"):
            raise ValueError(f"{name}.{field} must be a single line")
        return value
    if isinstance(default, list):
        if isinstance(value, str):
            value = [p.strip() for p in value.split(",") if p.strip()]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{name}.{field} must be a list of strings")
        return value
    raise ValueError(f"{name}.{field} is not settable here")


def _assert_setup_values_usable(name: str, values: dict[str, Any]) -> None:
    """Refuse a value that is the right TYPE but can never work.

    :func:`_coerce_setup_value` answers "is this a list of strings?"; this
    answers "are those strings ones the integration can actually use?". Only
    checks that need NO network live here — the wizard runs before anything is
    reachable, and an install must never depend on a vendor being up to save a
    setting. Today that is Linear's ``state_types``, whose seven legal values
    are a fixed documented list: anything else matches no issue at all, and
    Linear reports that as an empty page rather than an error, so this is the
    last place it can be caught cheaply.
    """
    if name == "linear" and "state_types" in values:
        from ..intake.linear import STATE_TYPES, state_type_problems

        problems = state_type_problems(values["state_types"])
        if problems:
            raise ValueError(
                "linear.state_types: " + "; ".join(problems)
                + ". The seven values are " + ", ".join(repr(t) for t in STATE_TYPES)
            )


def apply_setup(name: str, values: dict[str, Any],
                repos: list[str] | None = None) -> dict[str, Any]:
    """Persist one integration's NON-SECRET settings to config.yaml and return
    its refreshed spec entry.

    Every field is validated through :func:`assert_config_safe_field` and
    coerced BEFORE anything is written, so a rejected field leaves config.yaml
    byte-for-byte untouched rather than half-written.

    ``default_repo`` is additionally checked for membership in ``repos`` (the
    operator's registered repo profiles): a non-empty value that is not one of
    them raises :class:`RepoNotRegistered` (→ 400), so a pulled-in ticket can
    never be routed to a repo no_human does not know. An empty value (clearing
    the field) is always allowed. ``repos=None`` means the caller did not
    supply the list, so membership is not enforced (used by unit tests / any
    non-API caller)."""
    from .. import config as _config_mod
    from ..config import DEFAULT_CONFIG

    defaults = (DEFAULT_CONFIG.get("integrations") or {}).get(name)
    if not isinstance(defaults, dict):
        raise ValueError(f"unknown integration: {name!r}")

    updates: dict[str, Any] = {}
    coerced: dict[str, Any] = {}
    for field, raw in values.items():
        assert_config_safe_field(name, field)
        coerced[field] = _coerce_setup_value(name, field, defaults[field], raw)
        updates[f"integrations.{name}.{field}"] = coerced[field]
    _assert_setup_values_usable(name, coerced)

    chosen = coerced.get("default_repo")
    if repos is not None and chosen and chosen not in repos:
        raise RepoNotRegistered(
            f"{chosen!r} is not a registered repo. Add it first (Onboarding → "
            f"Repositories), then choose it here.")

    if updates:
        _write_config_values(_config_mod.CONFIG_PATH, updates)
    refreshed = _config_mod.load_config(_config_mod.CONFIG_PATH)
    for spec in setup_specs(refreshed.data, repos):
        if spec["name"] == name:
            return spec
    raise ValueError(f"unknown integration: {name!r}")  # pragma: no cover


def mark_verified(name: str) -> str | None:
    """Record that *name* passed a live connection test, by writing
    ``integrations.<name>.last_verified_at`` (UTC ISO-8601) to config.yaml, and
    return that timestamp.

    Returns ``None`` — writing nothing — for a name that has no
    ``integrations.<name>`` config block (github/gitlab/jenkins/circleci are
    ``ci.*`` views: there is nowhere to persist the flag and nothing that reads
    one, so a write would only litter config.yaml with an inert block).

    Returns ``None`` — writing nothing — ALSO for a VIEW-ONLY integration
    (:data:`VIEW_ONLY_CHECKS`: slack/teams). Their `_check_*` only confirm a
    webhook string is present; they do NOT deliver anything, so a healthy result
    is "saved", not "verified". Letting it earn a green "Ready" is exactly the
    "green means a key was typed, not that it works" lie C2 exists to remove.

    This is the ONLY thing a passing test persists; it never touches a
    credential."""
    from datetime import datetime, timezone

    from .. import config as _config_mod
    from ..config import DEFAULT_CONFIG

    if name in VIEW_ONLY_CHECKS:
        return None
    block = (DEFAULT_CONFIG.get("integrations") or {}).get(name)
    if not isinstance(block, dict):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_config_values(_config_mod.CONFIG_PATH,
                         {f"integrations.{name}.last_verified_at": ts})
    return ts


# --------------------------------------------------------------------------- #
# Live health checks                                                           #
# --------------------------------------------------------------------------- #

async def _http_get(url, headers=None, auth=None, timeout=10.0):
    """Thin async GET seam (monkeypatched in tests; real impl uses httpx)."""
    import httpx
    async with httpx.AsyncClient() as client:
        return await client.get(url, headers=headers, auth=auth, timeout=timeout)


async def _check_jira(config: dict) -> IntegrationStatus:
    base = _jira_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    token = os.environ.get("JIRA_API_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="JIRA_API_TOKEN not set in ~/.no_human/.env")
    j = _sect(config, "integrations").get("jira") or {}
    url = f"{j['site'].rstrip('/')}/rest/api/3/myself"
    try:
        r = await _http_get(url, auth=(j["email"], token), timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — a health check never raises
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    if r.status_code == 200:
        who = ""
        try:
            who = (r.json() or {}).get("displayName", "")
        except Exception:  # noqa: BLE001
            pass
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _http_post(url, headers=None, json=None, timeout=10.0):
    """Thin async POST seam (monkeypatched in tests; real impl uses httpx).

    Linear's API is GraphQL-only, so its health check cannot reuse
    ``_http_get``.
    """
    import httpx
    async with httpx.AsyncClient() as client:
        return await client.post(url, headers=headers, json=json, timeout=timeout)


async def _check_linear(config: dict) -> IntegrationStatus:
    """Live check: the key authenticates AND ``team_key`` names a real team.

    The team check lives HERE, and in the adapter's own poll-time guard, and
    nowhere else — this is a user-initiated action that is already making a
    request, so it is the one place an online validation cannot hang a startup
    or block an offline install. It is a SECOND request rather than extra
    fields on the auth probe, deliberately: the auth probe's query is verified
    and answers the question the operator pressed the button for, and a widened
    query that Linear rejected would take the working half down with it. As a
    separate request it can only ever ADD a verdict — any failure, or a body
    without a `teams` connection, leaves the answer at "authenticated".
    """
    base = _linear_status(config)
    if not base.configured:
        # base.detail, not a fixed string: when state_types is the problem it
        # names the setting, and flattening that to "not configured" would
        # throw away the only thing that says what to fix.
        return replace(base, healthy=False, detail=base.detail)
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        return replace(base, healthy=False,
                       detail="LINEAR_API_KEY not set in ~/.no_human/.env")
    from ..intake.linear import API_URL
    try:
        r = await _http_post(
            API_URL,
            # RAW key, not Bearer — Linear's documented personal-key header.
            headers={"Authorization": key, "Content-Type": "application/json"},
            json={"query": "{ viewer { id name } }"}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — a health check never raises
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    # Linear returns field errors at 200, auth failure at 401 and rate limiting
    # at 400 — every one of them carries an `errors` array, so a 200 alone does
    # not mean success.
    body: Any = {}
    try:
        body = r.json() or {}
    except Exception:  # noqa: BLE001
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        code = ((first.get("extensions") or {}).get("code") or "") if isinstance(first, dict) else ""
        if code == "RATELIMITED":
            return replace(base, healthy=False,
                           detail="rate limited (Linear reports this as HTTP 400)")
        return replace(base, healthy=False, detail=f"API error: {code or 'unknown'}")
    if r.status_code == 200:
        who = ""
        if isinstance(body, dict):
            who = ((body.get("data") or {}).get("viewer") or {}).get("name", "")
        lin = _sect(config, "integrations").get("linear") or {}
        bad_team = await _linear_team_key_problem(key, str(lin.get("team_key") or ""))
        if bad_team:
            return replace(base, healthy=False, detail=bad_team)
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _linear_team_key_problem(key: str, team_key: str) -> str:
    """"``integrations.linear.team_key`` names no team" — or ``""``.

    Fails OPEN on everything else: a transport failure, an errors array, a
    non-200, a body with no ``teams`` connection, or a workspace that lists no
    teams at all (an API key that cannot see them) all return ``""``. Only a
    definitive answer — the workspace listed its teams and this key is not
    among them — becomes a verdict, because a health check that invented a
    config error would send an operator to fix a setting that was right.
    """
    from ..intake.linear import API_URL

    if not team_key:
        return ""
    try:
        r = await _http_post(
            API_URL,
            headers={"Authorization": key, "Content-Type": "application/json"},
            json={"query": "{ teams(first: 250) { nodes { key } } }"}, timeout=10.0)
        body = r.json() or {}
    except Exception:  # noqa: BLE001 — a health check never raises
        return ""
    if not isinstance(body, dict) or body.get("errors") or r.status_code != 200:
        return ""
    conn = ((body.get("data") or {}).get("teams") or {})
    if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
        return ""
    keys = [str(n.get("key")) for n in conn["nodes"]
            if isinstance(n, dict) and n.get("key")]
    if not keys or team_key in keys:
        return ""
    return (f"integrations.linear.team_key is {team_key!r}, which is not a team in "
            f"this workspace — every poll would return nothing. Teams this key can "
            f"see: {', '.join(repr(k) for k in sorted(keys))}")


async def _check_monday(config: dict) -> IntegrationStatus:
    base = _monday_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail=base.detail)
    token = os.environ.get("MONDAY_API_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="MONDAY_API_TOKEN not set in ~/.no_human/.env")
    from ..intake.monday import API_URL, API_VERSION
    try:
        r = await _http_post(
            API_URL,
            # RAW token, not Bearer. API-Version pinned so the account's
            # default moving cannot change what this check exercises.
            headers={"Authorization": token, "Content-Type": "application/json",
                     "API-Version": API_VERSION},
            json={"query": "{ me { id name } }"}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — a health check never raises
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    # Throttling FIRST: monday answers 429 with an HTML body, so parsing before
    # classifying would report a retryable throttle as a broken API.
    if r.status_code == 429:
        return replace(base, healthy=False,
                       detail="rate limited (monday reports this as HTTP 429)")
    body: Any = {}
    try:
        body = r.json() or {}
    except Exception:  # noqa: BLE001
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        # A monday validation error carries no `extensions` at all, so the code
        # may legitimately be absent — fall back to the message, never crash.
        code = ""
        if isinstance(first, dict):
            ext = first.get("extensions") or {}
            code = (ext.get("code") or "") if isinstance(ext, dict) else ""
            code = code or str(first.get("message") or "")
        return replace(base, healthy=False, detail=f"API error: {code or 'unknown'}")
    if r.status_code == 200:
        who = ""
        if isinstance(body, dict):
            who = ((body.get("data") or {}).get("me") or {}).get("name", "")
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _check_teams(config: dict) -> IntegrationStatus:
    """Teams is a status view, not a ping.

    There is deliberately no live probe: the only way to exercise a Workflows
    webhook is to POST a message, and Microsoft's Graph/Teams terms state it is
    a violation "to use Microsoft Teams as a log file — only send messages that
    people will read". A health check must not put noise in a human's channel.
    What IS checked is the one failure we can see without sending: a retired
    Office 365 connector URL, which can never deliver.
    """
    base = _teams_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    if base.healthy is False:      # retired connector URL, detail already set
        return base
    return replace(base, healthy=True,
                   detail=f"{base.detail} — verified by the webhook at run time")


async def _check_circleci(config: dict) -> IntegrationStatus:
    base = _circleci_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    token = os.environ.get("CIRCLECI_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="CIRCLECI_TOKEN not set in ~/.no_human/.env")
    try:
        r = await _http_get("https://circleci.com/api/v2/me",
                            headers={"Circle-Token": token}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return replace(base, healthy=False, detail=f"connection failed: {type(exc).__name__}")
    if r.status_code == 200:
        who = ""
        try:
            who = (r.json() or {}).get("login", "")
        except Exception:  # noqa: BLE001
            pass
        return replace(base, healthy=True,
                       detail=f"authenticated as {who}" if who else "authenticated")
    return replace(base, healthy=False, detail=f"HTTP {r.status_code}")


async def _unconfigured_or_ambient(base: IntegrationStatus, name: str) -> IntegrationStatus:
    """The unconfigured branch shared by the real VCS probes: an unconfigured
    github/gitlab that is nonetheless ambiently authenticated reports
    ``status="ambient"``/``healthy=None`` (agreeing with
    `list_integrations_with_ambient` and the /test endpoint contract), never a
    flat 'not configured'. ``ambient_available`` can shell out, so it is
    offloaded off the event loop."""
    if await asyncio.to_thread(ambient_available, name):
        return replace(base, healthy=None, detail=_AMBIENT_DETAIL, status="ambient")
    return replace(base, healthy=False, detail="not configured")


async def _check_github(config: dict) -> IntegrationStatus:
    """Real GitHub probe: the token authenticates AND the configured repo is
    reachable. FAIL-CLOSED — a transport error is 'not verified', never green."""
    base = _github_status(config)
    if not base.configured:
        return await _unconfigured_or_ambient(base, "github")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="GITHUB_TOKEN not set in ~/.no_human/.env")
    ci = _sect(config, "ci")
    project = str(ci.get("project") or "")
    hostname = str(ci.get("hostname") or "")
    api = "https://api.github.com" if not hostname else f"https://{hostname}/api/v3"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    try:
        u = await _http_get(f"{api}/user", headers=headers, timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — a health check never raises
        return replace(base, healthy=False, detail=f"not verified: {type(exc).__name__}")
    if u.status_code != 200:
        return replace(base, healthy=False, detail=f"HTTP {u.status_code}")
    login = ""
    try:
        login = (u.json() or {}).get("login", "")
    except Exception:  # noqa: BLE001
        pass
    try:
        r = await _http_get(f"{api}/repos/{project}", headers=headers, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return replace(base, healthy=False, detail=f"not verified: {type(exc).__name__}")
    if r.status_code != 200:
        return replace(base, healthy=False,
                       detail=f"repo {project} not found (HTTP {r.status_code})")
    who = f"connected as {login}" if login else "connected"
    return replace(base, healthy=True, detail=f"{who} · repo {project} found")


async def _check_gitlab(config: dict) -> IntegrationStatus:
    """Real GitLab probe: GITLAB_TOKEN authenticates against the project's host.
    FAIL-CLOSED on any transport error."""
    base = _gitlab_status(config)
    if not base.configured:
        return await _unconfigured_or_ambient(base, "gitlab")
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        return replace(base, healthy=False,
                       detail="GITLAB_TOKEN not set in ~/.no_human/.env")
    hostname = str(_sect(config, "ci").get("hostname") or "") or "gitlab.com"
    try:
        r = await _http_get(f"https://{hostname}/api/v4/user",
                            headers={"PRIVATE-TOKEN": token}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return replace(base, healthy=False, detail=f"not verified: {type(exc).__name__}")
    if r.status_code != 200:
        return replace(base, healthy=False, detail=f"HTTP {r.status_code}")
    who = ""
    try:
        who = (r.json() or {}).get("username", "")
    except Exception:  # noqa: BLE001
        pass
    return replace(base, healthy=True,
                   detail=f"connected as {who}" if who else "connected")


async def _check_jenkins(config: dict) -> IntegrationStatus:
    """Real Jenkins probe: basic auth against the controller's ``/api/json``.
    FAIL-CLOSED on any transport error. Jenkins has no ambient-CLI concept."""
    base = _jenkins_status(config)
    if not base.configured:
        return replace(base, healthy=False, detail="not configured")
    user = os.environ.get("JENKINS_USER")
    token = os.environ.get("JENKINS_API_TOKEN")
    if not (user and token):
        return replace(base, healthy=False,
                       detail="JENKINS_USER / JENKINS_API_TOKEN not set in ~/.no_human/.env")
    base_url = str(_sect(config, "ci").get("base_url") or "https://build.example.com").rstrip("/")
    try:
        r = await _http_get(f"{base_url}/api/json", auth=(user, token), timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return replace(base, healthy=False, detail=f"not verified: {type(exc).__name__}")
    if r.status_code != 200:
        return replace(base, healthy=False, detail=f"HTTP {r.status_code}")
    return replace(base, healthy=True, detail=f"connected to {base_url}")


async def _check_view(status_fn, name: str, config: dict) -> IntegrationStatus:
    """github/gitlab/jenkins/slack are status views — 'healthy' mirrors
    'configured'; the live connection is exercised by the CI backend / webhook
    at run time, so a separate ping here would be a second, weaker truth. An
    unconfigured github/gitlab that is nonetheless ambiently authenticated
    (SCRUM-81) reports ``status="ambient"``/``healthy=None`` instead of a flat
    'not configured' — this endpoint must agree with `list_integrations_with_
    ambient`, not contradict it."""
    base = status_fn(config)
    if not base.configured:
        # ambient_available() can shell out (subprocess.run) — never block
        # this coroutine's event loop; offload it exactly like every other
        # blocking call in this codebase.
        if await asyncio.to_thread(ambient_available, name):
            return replace(base, healthy=None, detail=_AMBIENT_DETAIL, status="ambient")
        return replace(base, healthy=False, detail="not configured")
    return replace(base, healthy=True,
                   detail=f"{base.detail} — verified by the backend at run time")


_CHECKERS = {
    "jira": _check_jira,
    "linear": _check_linear,
    "monday": _check_monday,
    "teams": _check_teams,
    "circleci": _check_circleci,
    "github": _check_github,
    "gitlab": _check_gitlab,
    "jenkins": _check_jenkins,
    # Slack stays a view: a webhook cannot be probed without POSTing a message,
    # so the live verification happens at the notifier at run time. github /
    # gitlab / jenkins now have real fail-closed probes above.
    "slack": lambda c: _check_view(_slack_status, "slack", c),
}

#: Integrations whose ``_check_*`` only confirm a webhook string is PRESENT —
#: they cannot deliver anything without POSTing a message, which their terms
#: forbid a health check from doing. A healthy result from one of these is
#: "saved", never "verified", so `mark_verified` refuses them: they must not
#: earn a green "Ready" on config-presence alone (see `mark_verified`).
VIEW_ONLY_CHECKS = frozenset({"slack", "teams"})


async def test_integration(name: str, config: dict) -> IntegrationStatus:
    """Run a live health check for one integration; return its status with
    ``healthy`` set. Never raises on a network error (captured into ``detail``)."""
    checker = _CHECKERS.get(name)
    if checker is None:
        raise ValueError(f"unknown integration: {name!r}")
    return await checker(config)


__all__ = [
    "IntegrationStatus", "KIND_BY_NAME", "list_integrations", "test_integration",
    "FieldSpec", "FIELD_SPECS", "integration_fields", "save_integration_config",
    "ambient_available", "list_integrations_with_ambient", "list_integrations_with_health",
    "SETUP_SECRET_ENV", "SETUP_SECRET_NOTE", "assert_config_safe_field",
    "is_credential_name", "enable_field", "enable_default", "enable_state",
    "setup_specs", "apply_setup", "mark_verified", "RepoNotRegistered",
]
