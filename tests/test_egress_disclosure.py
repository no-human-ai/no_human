"""Every module that reaches the network is named on the egress page, or is
loopback-only and listed here with a reason.

WHY THIS IS NOT A ``grep https://`` GUARD. A string search for URLs fires on
almost every module in ``src/no_human/`` — ``notify/teams.py`` documents three
generations of webhook URL in its docstring, ``brain/client.py`` spends a
40-line docstring on ``http://attacker.example/token`` as an *example of what
not to do*, and every CI backend names its provider's API in prose. A guard that
red-flags a docstring is a guard somebody disables inside a week, and a disabled
guard protects nothing. This one is keyed on the thing that actually causes
egress: an AST call that **constructs an HTTP client or makes a request**
(``httpx``/``requests``/``urllib.request``), or an argv list literal whose
program is a network CLI (``curl``, ``wget``, ``gh``, ``glab``). Prose cannot
satisfy it and prose cannot trip it. In practice it goes red exactly once per
genuinely new outbound channel.

WHAT IT CANNOT SEE — the same honesty ``docs/security.md`` §7 states about
itself, because a guard that oversells its coverage is the defect it is meant
to prevent:

  * **The coder agent's own Bash.** The backend runs with
    ``permission_mode="bypassPermissions"`` and no tool allowlist, so a
    ``curl``, a ``pip install``, or a test suite that calls a staging API leaves
    the machine without passing through any code this test parses. That traffic
    is unbounded and no static check can bound it.
  * **A dependency's own telemetry.** ``httpx``, the Claude Agent SDK, Electron,
    Playwright and everything in ``uv.lock`` can open sockets of their own. This
    test reads *our* source only.
  * **Indirection.** A request behind an injected callable, ``getattr``, an
    ``importlib`` load, or a socket/``asyncio`` connection opened directly is
    invisible here. The detector is deliberately literal; it catches the way
    this repo actually writes network calls, not every way one could be written.
  * **Where a URL points at runtime.** ``ci.base_url``, a webhook URL and
    ``team_brain.control_plane_url`` are operator-supplied. The guard knows a
    module can send; it cannot know where.

Loopback (``127.0.0.1`` / ``localhost``) is NOT egress and is excluded
explicitly, module by module with a reason, in ``LOOPBACK_ONLY`` below — never
by pattern-matching a URL, because ``"http://localhost.evil.com"`` starts with
``http://localhost`` and this repo has already been bitten by exactly that
(see ``brain/client.py``'s ``_base`` docstring).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "no_human"
SECURITY_MD = REPO / "docs" / "security.md"

#: Heading of the section that must name every outbound channel.
EGRESS_SECTION = "## 7. What leaves your machine"

#: ``httpx``/``requests`` attributes that either issue a request or build a
#: client that will. ``Client``/``AsyncClient``/``Session`` count: constructing
#: one is the commitment, even when the call site is a later ``.get``.
_REQUEST_ATTRS = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options",
    "request", "stream", "Client", "AsyncClient", "Session",
})
_HTTP_MODULES = frozenset({"httpx", "requests"})

#: Programs that are, in this repo, always a network round-trip.
_NETWORK_CLIS = frozenset({"curl", "wget", "gh", "glab"})

#: Modules that talk only to 127.0.0.1 — no_human's own API, or a local
#: language server. Loopback is not egress, so these need no disclosure entry.
#: Each is a deliberate, re-checkable judgement, not a pattern match.
LOOPBACK_ONLY: dict[str, str] = {
    "cli/api_client.py":
        "DEFAULT_BASE_URL = http://127.0.0.1:8420 — the CLI calling our own API",
    "cli/pool_probe.py":
        "urlopen(http://<server host, 127.0.0.1 by default>:<port>/api/queue/"
        "health) — `nh status` probing our own local server, same trust model "
        "as cli/api_client.py (the CLI calling our own API)",
    "intake/mcp_bridge.py":
        "http://<server host, 127.0.0.1 by default>:<port> (BASE_URL, resolved "
        "from server.host/server.port at startup) — the MCP bridge calling our "
        "own API",
    "history/extractor.py":
        "host defaults to 127.0.0.1; probes a language server on a local port",
}


def _root_name(node: ast.AST) -> str | None:
    """``httpx.AsyncClient`` -> ``httpx``; ``urllib.request.urlopen`` -> ``urllib``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def outbound_calls(source: str, filename: str) -> set[str]:
    """Return the outbound-request constructs in *source*, as readable labels.

    Empty set == this module, as written, opens no connection of its own.
    """
    found: set[str] = set()
    tree = ast.parse(source, filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            root, attr = _root_name(node.func.value), node.func.attr
            if root in _HTTP_MODULES and attr in _REQUEST_ATTRS:
                found.add(f"{root}.{attr}()")
            elif root == "urllib" and attr in {"urlopen", "Request"}:
                found.add(f"urllib.request.{attr}()")
        # ``cmd = ["curl", ...]`` / ``subprocess.run(["gh", ...])``. Keyed on the
        # program in argv[0], so a mention of curl in prose cannot match.
        if isinstance(node, ast.List) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value in _NETWORK_CLIS:
                found.add(f"argv[0]={first.value!r}")
    return found


def egress_section() -> str:
    text = SECURITY_MD.read_text()
    start = text.index(EGRESS_SECTION)
    nxt = text.find("\n## ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def disclosed_modules() -> set[str]:
    """Module paths cited inside the egress section, e.g. ``context/teams.py``."""
    return set(re.findall(r"\b((?:[a-z0-9_]+/)*[a-z0-9_]+\.py)\b", egress_section()))


def detected_modules() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        calls = outbound_calls(path.read_text(), str(path))
        if calls:
            out[path.relative_to(PKG).as_posix()] = calls
    return out


def test_the_detector_actually_detects() -> None:
    """Control. A zero from the walk below must mean 'nothing found', never
    'the walk broke'. Three shapes this repo really uses, one per detector arm."""
    assert outbound_calls("import httpx\nhttpx.post(u, json=b)\n", "x.py") \
        == {"httpx.post()"}
    assert outbound_calls("import urllib.request\nurllib.request.urlopen(r)\n", "x.py") \
        == {"urllib.request.urlopen()"}
    assert outbound_calls('cmd = ["curl", "-sS", url]\n', "x.py") \
        == {"argv[0]='curl'"}
    # Prose about a URL is not egress, and must never be treated as such.
    assert outbound_calls('"""POSTs to https://graph.microsoft.com/x."""\n', "x.py") \
        == set()
    # And the real tree is non-empty, so an empty result below is a real signal.
    assert len(detected_modules()) > 10


def test_telemetry_egress_is_visible_not_hidden_behind_an_alias() -> None:
    """``telemetry.py`` posts to PostHog via ``urllib.request``. It must use the
    detectable ``import urllib.request`` form, not an aliased
    ``from urllib import request as _x`` that this detector cannot see —
    otherwise the module's real egress silently drops out of every guard in
    this file and out of ``undisclosed`` in ``test_every_outbound_module_is_disclosed``."""
    assert detected_modules().get("telemetry.py") == {
        "urllib.request.urlopen()", "urllib.request.Request()"}


def test_every_outbound_module_is_disclosed() -> None:
    """The drift guard. A module that gains an HTTP client or a network CLI call
    must be named in ``docs/security.md`` §7, or added to ``LOOPBACK_ONLY``."""
    detected = detected_modules()
    disclosed = disclosed_modules()
    assert disclosed, f"no module citations found under {EGRESS_SECTION!r}"

    undisclosed = {
        mod: calls for mod, calls in detected.items()
        if mod not in disclosed and mod not in LOOPBACK_ONLY
    }
    if undisclosed:
        lines = [
            f"  src/no_human/{mod}  ({', '.join(sorted(calls))})"
            for mod, calls in sorted(undisclosed.items())
        ]
        raise AssertionError(
            "these modules reach the network but are not named in "
            f"{SECURITY_MD.relative_to(REPO)} {EGRESS_SECTION!r}:\n"
            + "\n".join(lines)
            + "\n\nAdd an entry naming the destination and whether it is on by "
            "default or configuration-required (read config.DEFAULT_CONFIG for "
            "the default, never a loaded config) — or, if it only ever talks to "
            "127.0.0.1, add it to LOOPBACK_ONLY in this file with the reason."
        )


def test_loopback_exclusions_still_exist() -> None:
    """A stale exclusion is a hole. Every ``LOOPBACK_ONLY`` key must be a real
    module that still makes a request — otherwise the list is fiction."""
    detected = detected_modules()
    for mod, reason in LOOPBACK_ONLY.items():
        assert (PKG / mod).is_file(), f"LOOPBACK_ONLY names a missing module: {mod}"
        assert mod in detected, (
            f"LOOPBACK_ONLY still excuses {mod} ({reason}) but it no longer "
            "makes any request — drop the entry."
        )


def test_microsoft_graph_is_stated_as_opt_in() -> None:
    """The Graph entry must stay CONDITIONAL. ``context/teams.py`` raises before
    building the request when ``context.m365.token`` is unset, and
    ``DEFAULT_CONFIG`` has no ``context.m365`` block, so an unconditional claim
    ('no_human sends your queries to Microsoft') would be false in the opposite
    direction — an egress page that overstates egress is its own defect."""
    from no_human.config import DEFAULT_CONFIG

    assert not (DEFAULT_CONFIG.get("context") or {}).get("m365"), \
        "context.m365 is now a default — the disclosure wording must be revisited"

    section = egress_section()
    assert "If you configure\n  `context.m365.token`, your query text is sent to " \
        "Microsoft Graph" in section, \
        "the Microsoft Graph entry must stay conditional on context.m365.token"
    assert "fails closed and sends nothing" in section


def test_approve_merge_module_is_disclosed() -> None:
    """Direct repro for the 70881915 salvage. `test_every_outbound_module_is_
    disclosed` above is driven by `detected_modules()`, which walks real files
    on disk — on a tree where `vcs/approve_merge.py` does not exist yet, that
    walk finds nothing to flag and the gate passes *vacuously*, not because
    anything is disclosed. This test reads the module directly and fails
    outright if it is absent, so it is red before the module + its §7 bullet
    exist and green only once both do."""
    module_path = PKG / "vcs" / "approve_merge.py"
    assert module_path.exists(), (
        "vcs/approve_merge.py must exist for `nh approve` to land PRs")
    calls = outbound_calls(module_path.read_text(), str(module_path))
    assert calls, "expected vcs/approve_merge.py to make outbound calls (gh/glab)"
    assert "vcs/approve_merge.py" in disclosed_modules(), (
        "vcs/approve_merge.py makes outbound calls but is not named in "
        f"{EGRESS_SECTION!r}")
