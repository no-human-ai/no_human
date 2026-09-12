"""`nh init` — guided first-run setup.

Walks a new user from zero to a working no_human installation:
  1. Check prerequisites (python, uv, git, claude CLI).
  2. Create ``~/.no_human/`` with correct permissions.
  3. Set up the subscription token (detect or guide).
  4. Generate default ``config.yaml`` with git identity pre-populated.
  5. Optionally onboard a first repo.
  6. Print a summary card with next steps.

Idempotent: running twice never destroys existing config, secrets, or data.
"""

from __future__ import annotations

import hmac
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.panel import Panel

from . import print_path_error
from ..config import (
    API_KEY_VAR,
    CONFIG_PATH,
    DB_PATH,
    DEFAULT_AUTH_PROFILE,
    ENV_PATH,
    METERED_AUTH_VARS,
    NO_HUMAN_HOME,
    SUBSCRIPTION_TOKEN_VAR,
    AuthError,
    assert_oauth_token_usable,
    credential_status,
    load_config,
    set_profile_token,
)

console = Console()

# Required external tools. Each: (binary, version_flag, install_hint, minimum).
# `minimum` is a version tuple or None when any version will do. It must track
# pyproject's `requires-python` — this list is what a user is TOLD, and for a
# long time it told them Python 3.10 was fine (onboarding walkthrough
# 2026-08-09, finding B12: a green tick against a stated 3.12+ requirement).
_MIN_PYTHON = (3, 12)
_PREREQUISITES = [
    ("python3", "--version", "https://python.org or `brew install python@3.12`",
     _MIN_PYTHON),
    ("git", "--version", "https://git-scm.com or `brew install git`", None),
]

# Optional but recommended tools.
_OPTIONAL = [
    ("uv", "--version", "`curl -LsSf https://astral.sh/uv/install.sh | sh`"),
    ("claude", "--version", "`npm install -g @anthropic-ai/claude-code`"),
]


# --------------------------------------------------------------------------- #
# Prerequisite checks                                                         #
# --------------------------------------------------------------------------- #

def _check_tool(binary: str, flag: str) -> str | None:
    """Return the version string or None if the tool is not found."""
    path = shutil.which(binary)
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, flag], capture_output=True, text=True, timeout=10,
        )
        return (out.stdout.strip() or out.stderr.strip())[:120]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _version_too_old(version: str, minimum: tuple[int, ...] | None) -> str | None:
    """Return why *version* fails *minimum* (naming it), or None if it passes.

    ``_check_tool`` already captured the version string and nothing ever read
    it, so `nh init` printed a green tick for a Python it knew was too old.
    An UNPARSEABLE string is deliberately not an error: a version format this
    doesn't recognise is not evidence of an old interpreter, and failing on it
    would block an install that works.
    """
    if not minimum:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not m:
        return None
    found = tuple(int(g) for g in m.groups() if g is not None)
    if found >= minimum:
        return None
    return f"too old — {'.'.join(str(p) for p in minimum)}+ required"


def check_prerequisites() -> tuple[list[str], list[str]]:
    """Check required and optional tools. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    for binary, flag, hint, minimum in _PREREQUISITES:
        ver = _check_tool(binary, flag)
        old = _version_too_old(ver, minimum) if ver else None
        if ver and not old:
            console.print(f"  [green]✓[/] {binary}: {ver}")
        elif ver and binary == "python3" and sys.version_info[:2] >= _MIN_PYTHON:
            # The `python3` on PATH is too old, but no_human is RUNNING on an
            # interpreter that is not — a venv or `uv run`, which is the
            # documented install. Reporting it is the fix (the green tick
            # claimed a requirement was met that this binary does not meet);
            # failing the install over a binary nothing here uses would refuse
            # a machine the product demonstrably works on.
            running = ".".join(str(p) for p in sys.version_info[:3])
            warnings.append(f"{binary} {old} (no_human is running on {running})")
            console.print(
                f"  [yellow]~[/] {binary}: {ver} — {old}; no_human is running "
                f"on {running}, so this is only a problem for `python3 -m "
                f"no_human`. {hint}"
            )
        elif ver:
            errors.append(f"{binary} {old} — install: {hint}")
            console.print(f"  [red]✗[/] {binary}: {ver} — {old}. {hint}")
        else:
            errors.append(f"{binary} not found — install: {hint}")
            console.print(f"  [red]✗[/] {binary}: not found — {hint}")

    for binary, flag, hint in _OPTIONAL:
        ver = _check_tool(binary, flag)
        if ver:
            console.print(f"  [green]✓[/] {binary}: {ver}")
        else:
            warnings.append(f"{binary} not found (optional) — {hint}")
            console.print(f"  [yellow]~[/] {binary}: not found (optional) — {hint}")

    return errors, warnings


# --------------------------------------------------------------------------- #
# Directory + .env setup                                                      #
# --------------------------------------------------------------------------- #

def ensure_home_dir() -> bool:
    """Ensure ``~/.no_human/`` exists and grants no group or other access.

    Does NOT force 0700: owner bits are left as the operator set them (a 0500
    lockdown stays 0500). Raises if the result is unusable, rather than
    widening it. Returns True if the directory was created.
    """
    # Delegates to config.ensure_private_dir so ONE implementation governs
    # every writer. The old `!= 0o700` here re-OPENED a directory the operator
    # had locked down further (0500 -> 0700), contradicting the invariant the
    # shared helper documents; it only ever needs to clear group/other bits.
    from ..config import ensure_private_dir

    created = not NO_HUMAN_HOME.exists()
    ensure_private_dir(NO_HUMAN_HOME)
    # The shared helper deliberately does NOT restore owner bits — clearing
    # group/other is the security goal, and widening a directory the operator
    # locked down is not ours to do. But `nh init`'s whole contract is "make my
    # install work", and without owner-write the very next step died with a raw
    # PermissionError three calls downstream. Say so here instead.
    if not os.access(NO_HUMAN_HOME, os.W_OK | os.X_OK):
        mode = format(NO_HUMAN_HOME.stat().st_mode & 0o777, "04o")
        raise click.ClickException(
            f"{NO_HUMAN_HOME} is mode {mode}: no_human cannot write into it. "
            f"Run `chmod u+rwx {NO_HUMAN_HOME}` and re-run `nh init` (if it "
            f"sits on a read-only mount, move NO_HUMAN_HOME instead). "
            f"(Permissions are left as you set them — only group/other access "
            f"is stripped.)")
    return created


def _env_has_key(key: str) -> bool:
    """Check if a key with a value exists in ``~/.no_human/.env``.

    Uses config's parser so this reader cannot disagree with the writer.
    """
    from ..config import _read_env_file

    return bool(_read_env_file(ENV_PATH).get(key))


def _found_subscription_token() -> tuple[str, str] | None:
    """The OAuth token already on this machine, and where it came from.

    ``.env`` wins over the process environment — the same precedence
    :func:`config.load_env_token` uses, so init reports on the value a RUN
    would actually bill with, not a different one.
    """
    from ..config import _read_env_file

    value = _read_env_file(ENV_PATH).get(SUBSCRIPTION_TOKEN_VAR)
    if value:
        return value, "~/.no_human/.env"
    value = os.environ.get(SUBSCRIPTION_TOKEN_VAR)
    if value:
        return value, "the environment"
    return None


def _found_api_key() -> tuple[str, str] | None:
    """The metered API key already on this machine, and where it came from.

    The BYO-API-key twin of :func:`_found_subscription_token`, with the same
    precedence :func:`config.load_api_key` uses (.env wins over the process
    environment) — and, like it, read-only: nothing is exported here.
    """
    from ..config import _read_env_file

    value = _read_env_file(ENV_PATH).get(API_KEY_VAR)
    if value:
        return value, "~/.no_human/.env"
    value = os.environ.get(API_KEY_VAR)
    if value:
        return value, "the environment"
    return None


def _report_found_token(value: str, where: str, suffix: str = "") -> bool:
    """Report a token init FOUND rather than wrote. True if it can work.

    `nh init` short-circuits on the PRESENCE of a credential, so a value stored
    before the writer was hardened — or exported in a shell — got a
    ``✓`` and a summary card reading ``Auth: ✓ ready`` from an install that
    cannot make a single call (walkthrough B4b; init-fix review advisory 5).
    This runs the writer's own refusal (:func:`config.assert_oauth_token_usable`)
    over the found value so there is still exactly one opinion about what a
    usable token is.

    Validate-ONLY: nothing is written, rewritten or scrubbed. A stored
    credential is the user's, and init's contract is that a re-run destroys
    nothing — so a bad value is reported, and left where it is.
    """
    try:
        assert_oauth_token_usable(value)
    except AuthError as exc:
        console.print(
            f"  [red]✗[/] {SUBSCRIPTION_TOKEN_VAR} is present in {where} but "
            f"invalid: {exc}"
        )
        console.print(
            "    [dim]nothing was changed — the stored value is left exactly "
            "as it is. Replace it with a token from `claude setup-token`.[/]"
        )
        return False
    console.print(f"  [green]✓[/] {SUBSCRIPTION_TOKEN_VAR} found in {where}{suffix}")
    return True


def _metered_key_in_env() -> str | None:
    """Return the name of the first metered-auth var found in the environment."""
    for var in METERED_AUTH_VARS:
        if os.environ.get(var):
            return var
    return None


def setup_token() -> tuple[bool, str]:
    """Guide the user through billing setup. Returns ``(ready, auth_mode)``.

    Two sanctioned paths: a Claude **subscription** (OAuth token — the default;
    personal and enterprise subscriptions are equally first-class) or the user's
    **own Anthropic API key** (BYO-API-key, metered and billed to them — for
    friends/commercial installs). The chosen mode is persisted as
    ``llm.auth_mode`` by :func:`ensure_config`.
    """
    # A re-run on a working install must not re-interrogate the operator: if a
    # mode is already configured and its credential is present, respect it.
    configured = _configured_auth_mode()
    if configured == "api_key" and (
        _env_has_key(API_KEY_VAR) or os.environ.get(API_KEY_VAR)
    ):
        console.print(f"  [green]✓[/] {API_KEY_VAR} found (auth_mode: api_key)")
        return True, "api_key"
    found = _found_subscription_token()
    if configured == "subscription" and found:
        return (
            _report_found_token(*found, suffix=" (auth_mode: subscription)"),
            "subscription",
        )

    console.print(
        "  How will this install pay for Claude?\n"
        "    [bold]1[/] Claude subscription  (OAuth token — recommended)\n"
        "    [bold]2[/] Your own Anthropic API key  (metered, billed to you)"
    )
    choice = click.prompt(
        "    Choice", type=click.Choice(["1", "2"]), default="1", show_default=True
    )
    if choice == "2":
        return _setup_api_key(), "api_key"
    return _setup_subscription_token(), "subscription"


def setup_token_noninteractive(auth_mode: str, token: str | None) -> bool:
    """:func:`setup_token` with the questions removed. True if the install can pay.

    Same two sanctioned modes, same variables, same writers — the only thing
    that changes is where the answers come from. Every write here goes through
    the function the wizard uses (:func:`_save_subscription_token` /
    :func:`_append_env`), so a scripted install and a hand-run one produce the
    same ``.env``, at the same 0600, refused by the same validator.

    Two behaviours are worth stating because a script cannot see them happen:

    * **Never overwrites.** ``nh init`` short-circuits on the PRESENCE of a
      credential (walkthrough B14) and this keeps that exactly. A stored value
      is reported and left alone; re-supplying the SAME token is a no-op so a
      provisioning script stays re-runnable, and supplying a DIFFERENT one is
      refused by name rather than silently ignored or silently applied. The
      comparison is :func:`hmac.compare_digest` — it is comparing two secrets,
      and there is no reason to leak their common prefix through timing.
    * **Never invents one.** No credential and none supplied is an explicit,
      named, non-zero failure (KI-2's second acceptance condition), not a
      ``~/.no_human`` that looks set up and cannot make a call.
    """
    found = _found_api_key() if auth_mode == "api_key" else _found_subscription_token()
    var = API_KEY_VAR if auth_mode == "api_key" else SUBSCRIPTION_TOKEN_VAR
    if found:
        return _keep_found_credential(auth_mode, var, found, token)

    if not token:
        raise click.ClickException(
            f"no credential to install: {var} is in neither ~/.no_human/.env "
            f"nor the environment, and nothing arrived on stdin. Supply one "
            f'(printf %s "$TOKEN" | nh init --non-interactive --token-stdin) '
            f"or export {var} before running. Nothing was written."
        )

    if auth_mode == "api_key":
        # The wizard's own API-key write: no shape check beyond the .env line
        # guard, because the key is the operator's chosen billing path and
        # `_assert_api_key_mode` is what enforces it at run time.
        _append_env(API_KEY_VAR, token)
    elif not _save_subscription_token(token):
        return False
    os.environ[var] = token
    console.print(f"  [green]✓[/] {var} saved to ~/.no_human/.env")
    return True


def _keep_found_credential(auth_mode: str, var: str, found: tuple[str, str],
                           token: str | None) -> bool:
    """Report the credential already on this machine; never replace it."""
    value, where = found
    if token and not hmac.compare_digest(token, value):
        raise click.ClickException(
            f"{var} is already set in {where} and the value on stdin is a "
            f"different one. `nh init` never overwrites a stored credential. "
            + ("To replace it: nh auth set-token"
               if auth_mode == "subscription" else
               "To replace it, edit ~/.no_human/.env")
            + ". Nothing was written."
        )
    if auth_mode == "api_key":
        console.print(f"  [green]✓[/] {var} found in {where} (auth_mode: api_key)")
        ready = True
    else:
        ready = _report_found_token(value, where, suffix=" (auth_mode: subscription)")
    # The wizard asks "Persist it to ~/.no_human/.env?" and defaults to yes; a
    # scripted install that skipped the question must not end up quietly
    # depending on a variable that was exported for one shell.
    if ready and where == "the environment":
        if auth_mode == "api_key":
            _append_env(var, value)
        elif not _save_subscription_token(value):
            return False
        console.print("    [green]saved[/] to ~/.no_human/.env")
    return ready


def _configured_auth_mode() -> str:
    """Read ``llm.auth_mode`` from config.yaml if it exists, else "subscription".

    Best-effort — a malformed config never crashes setup; it just falls back to
    the default mode and re-prompts.
    """
    try:
        if CONFIG_PATH.exists():
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            mode = (data.get("llm") or {}).get("auth_mode")
            if mode in ("subscription", "api_key"):
                return mode
    except Exception:  # noqa: BLE001
        pass
    return "subscription"


def _setup_api_key() -> bool:
    """BYO-API-key path: detect or prompt for ANTHROPIC_API_KEY. True if ready."""
    if _env_has_key(API_KEY_VAR):
        console.print(f"  [green]✓[/] {API_KEY_VAR} found in ~/.no_human/.env")
        return True
    if os.environ.get(API_KEY_VAR):
        console.print(f"  [green]✓[/] {API_KEY_VAR} found in environment")
        if click.confirm("    Persist it to ~/.no_human/.env?", default=True):
            _append_env(API_KEY_VAR, os.environ[API_KEY_VAR])
            console.print("    [green]saved[/] to ~/.no_human/.env")
        return True
    console.print(
        "\n  [yellow]✗ no Anthropic API key found.[/]\n"
        "    Create one at: [bold]https://console.anthropic.com/settings/keys[/]\n"
        "    This bills your own metered account. Paste it here, or press Enter "
        "to skip."
    )
    key = click.prompt(
        "    API key", default="", show_default=False, hide_input=True
    ).strip()
    if key:
        _append_env(API_KEY_VAR, key)
        os.environ[API_KEY_VAR] = key
        console.print("    [green]saved[/] to ~/.no_human/.env")
        return True
    console.print(
        "    [yellow]skipped[/] — add it later:\n"
        f"      echo '{API_KEY_VAR}=<your_key>' >> ~/.no_human/.env"
    )
    return False


def _setup_subscription_token() -> bool:
    """Guide the user through subscription token setup. Returns True if ready."""
    # A token already on the machine (.env first, then the process env) is
    # reported, never rewritten — but it is VALIDATED before it is called ready.
    found = _found_subscription_token()
    if found and found[1] == "~/.no_human/.env":
        return _report_found_token(*found)

    if found:  # process environment
        if not _report_found_token(*found):
            return False
        if click.confirm("    Persist it to ~/.no_human/.env?", default=True):
            if not _save_subscription_token(os.environ[SUBSCRIPTION_TOKEN_VAR]):
                return False
            console.print("    [green]saved[/] to ~/.no_human/.env")
        return True

    # Check for the dangerous metered key.
    metered = _metered_key_in_env()
    if metered:
        console.print(
            f"\n  [bold red]⚠ {metered} is set in your environment.[/]\n"
            f"    In the default subscription mode, startup scrubs this variable\n"
            f"    so a run bills exactly one path. Please unset it:\n"
            f"      [bold]unset {metered}[/]\n"
            f"    (To bill your own Anthropic account with an API key, set\n"
            f"    llm.auth_mode: api_key in config.yaml.)"
        )

    # Guide through setup.
    console.print(
        f"\n  [yellow]✗ {SUBSCRIPTION_TOKEN_VAR} not found.[/]\n"
        f"    You need a Claude subscription token. To create one:\n"
        f"      [bold]claude setup-token[/]\n"
        f"    Then paste the token here, or press Enter to skip for now."
    )
    # hide_input: a credential typed here used to be echoed to the terminal and
    # left in scrollback (walkthrough finding B13). The API-key prompt above has
    # always hidden it; this is the same prompt for the same class of secret.
    token = click.prompt(
        "    Token", default="", show_default=False, hide_input=True
    ).strip()
    if token:
        if not _save_subscription_token(token):
            return False
        os.environ[SUBSCRIPTION_TOKEN_VAR] = token
        console.print("    [green]saved[/] to ~/.no_human/.env")
        return True
    console.print(
        "    [yellow]skipped[/] — add it later:\n"
        f"      echo '{SUBSCRIPTION_TOKEN_VAR}=<your_token>' >> ~/.no_human/.env"
    )
    return False


def _save_subscription_token(token: str) -> bool:
    """Write the OAuth token through the product's OWN validator. True if saved.

    `nh init` used to hand any string straight to :func:`_append_env`, which
    validates the *line*, not the *credential*: an ``sk-ant-api…`` key pasted
    into the OAuth prompt was written verbatim to ``CLAUDE_CODE_OAUTH_TOKEN``
    and the summary card then printed ``Auth: ✓ ready`` (onboarding walkthrough
    2026-08-09, findings B4/B4b). Both the HTTP path (``PUT /api/auth/token``)
    and the desktop app (``desktop/tokenStore.mjs:274-278``) already refused it
    — the CLI was the odd one out.

    :func:`config.set_profile_token` is that shared refusal: empty token,
    over-length, embedded line break, ``sk-ant-api…``. Its message already
    names what the user pasted and where the key belongs, so it is printed as
    it stands rather than paraphrased — one opinion about token shapes, not two.

    ponytail: no format whitelist. Enterprise OAuth tokens are first-class and
    their shape is the vendor's to change; only the demonstrated bad inputs are
    refused.
    """
    ensure_home_dir()
    try:
        set_profile_token(DEFAULT_AUTH_PROFILE, token, ENV_PATH)
    except AuthError as exc:
        console.print(f"\n    [bold red]✗ not saved —[/] {exc}")
        return False
    return True


def _append_env(key: str, value: str) -> None:
    """Upsert a key=value into ``~/.no_human/.env`` (create if absent, 0600).

    Delegates to ``config.upsert_env_var`` rather than doing its own
    read-modify-write. This used to be a THIRD independent writer: it parsed
    with ``splitlines()`` while validating nothing, so the writer and the
    reader disagreed about what a line is — the same defect that let a value
    forge an ``ANTHROPIC_API_KEY=`` entry. It also did ``write_text`` then
    ``chmod``, which is exactly the umask window ``atomic_write_0600`` exists
    to close.
    """
    from ..config import upsert_env_var

    ensure_home_dir()
    upsert_env_var(ENV_PATH, key, value)




# --------------------------------------------------------------------------- #
# Config generation                                                           #
# --------------------------------------------------------------------------- #

def ensure_config(auth_mode: str = "subscription") -> bool:
    """Generate ``~/.no_human/config.yaml`` with git identity if absent, and
    persist the chosen ``llm.auth_mode`` (idempotent even when the file exists).
    Returns True if file was created, False if it already existed."""
    if CONFIG_PATH.exists():
        console.print(f"  [green]✓[/] config.yaml already exists")
        # Persist the chosen billing mode whenever it differs from what is
        # stored, in BOTH directions (api_key <-> subscription), so a re-run
        # of `nh init` that switches modes actually takes effect. Leave the
        # file untouched when the mode is unchanged.
        if auth_mode != _configured_auth_mode():
            _set_auth_mode(auth_mode)
            console.print(f"    set llm.auth_mode: {auth_mode}")
        return False

    # Pre-populate git identity from the user's git config.
    git_name = _git_config("user.name")
    git_email = _git_config("user.email")

    config = load_config(create_if_missing=True)
    # Re-read to update git identity and/or billing mode.
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if git_name or git_email:
        git_section = data.setdefault("git", {})
        if git_name:
            git_section.setdefault("agent_identity_name", "no_human")
        if git_email:
            git_section.setdefault("agent_identity_email", "no-human@acme.com")
    if auth_mode != "subscription":
        data.setdefault("llm", {})["auth_mode"] = auth_mode
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    console.print(f"  [green]✓[/] created config.yaml")
    if auth_mode != "subscription":
        console.print(f"    billing mode: {auth_mode}")
    if git_name:
        console.print(f"    your git identity: {git_name} <{git_email or '?'}>")
        console.print(f"    agent identity:    no_human <no-human@acme.com>")
    return True


def _set_auth_mode(auth_mode: str) -> None:
    """Upsert ``llm.auth_mode`` into an existing config.yaml, preserving the
    rest. Only the mode goes in config; the credential itself lives in .env."""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("llm", {})["auth_mode"] = auth_mode
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _git_config(key: str) -> str:
    """Read a git config value, or return empty string."""
    try:
        out = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


# --------------------------------------------------------------------------- #
# Repo onboard offer                                                          #
# --------------------------------------------------------------------------- #

def resolve_repo_arg(repo: str) -> str | None:
    """Resolve *repo* and refuse what `nh onboard` cannot use. None if unusable.

    Shared with the non-interactive path so a scripted `--repo` is held to the
    same two conditions the wizard's prompt applies, and reports them in the
    same words. The CALLER decides what an unusable path means: the wizard
    carries on without a repo, a provisioning script must fail.
    """
    repo_path = str(Path(repo).expanduser().resolve())
    if not Path(repo_path).is_dir():
        print_path_error(console, "  [red]not a directory:[/]", repo_path)
        return None
    if not (Path(repo_path) / ".git").exists():
        print_path_error(console, "  [red]not a git repo:[/]", repo_path)
        return None
    return repo_path


def offer_onboard() -> str | None:
    """Offer to onboard a repo. Returns the repo path or None if skipped."""
    console.print()
    if not click.confirm("  Onboard a repo now?", default=True):
        return None
    return resolve_repo_arg(click.prompt("  Repo path", default=".",
                                         show_default=True).strip())


# --------------------------------------------------------------------------- #
# Summary card                                                                #
# --------------------------------------------------------------------------- #

def print_summary(*, token_ready: bool, config_path: Path, repo_path: str | None):
    """Print the final summary card."""
    token_status = "[green]✓ ready[/]" if token_ready else "[yellow]✗ not set[/]"
    repo_line = f"  Repo:        {repo_path}" if repo_path else "  Repo:        [dim](none onboarded)[/]"

    card = (
        f"  Config:      {config_path}\n"
        f"  Secrets:     {ENV_PATH} [dim](chmod 600)[/]\n"
        f"  Database:    {DB_PATH}\n"
        f"  Auth:        {token_status}\n"
        f"{repo_line}\n"
        f"\n"
        f"  [bold]Quick start:[/]\n"
        f'    nh task add --title "Fix bug X" --repo ~/my-repo\n'
        f"    nh task add https://github.com/owner/repo/issues/42 --repo ~/my-repo\n"
        f"    nh blocked                  [dim]# check stuck tasks[/]\n"
        f"    nh dashboard                [dim]# open the web board[/]\n"
    )
    if not token_ready:
        card += (
            f"\n  [yellow]⚠ Token not set. Before running tasks:[/]\n"
            f"    [bold]claude setup-token[/]\n"
            f"    [bold]echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.no_human/.env[/]"
        )

    console.print(Panel(card, title="[bold green]no_human initialized[/]", border_style="green"))
