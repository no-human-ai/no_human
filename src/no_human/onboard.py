"""`nh onboard`: derive a ProjectProfile from a repo's own declarations, then
PROVE each command by running it.

Two phases, deliberately split (design fork resolved 2026-06-22):

  - DERIVE candidate install/test/lint commands from what the repo itself
    declares — lockfiles, ``package.json`` scripts, Makefile targets, CI yaml —
    never a hardcoded ``if repo == "metrics-core"``. The deriver is pluggable:
    ``DeclarationDeriver`` (deterministic, the default) reads the declarations
    directly; ``AgentDeriver`` runs a bounded *read-only* recon session for
    repos whose declarations are nonstandard.

  - PROVE each candidate by **running it in a direct subprocess** and checking
    the exit status. Proving is never delegated to an agent: ``proven['test_cmd']``
    must mean *that exact command string* exited clean in that cwd, on the same
    execution path the orchestrator will later use (``testing.runner``). An agent
    in the prove loop could silently mutate the command (add a flag, install a
    dep, ``cd`` away) and record a proof for a command we never actually run —
    that is faking a step. Adaptiveness helps when *deriving* candidates (it
    cannot fake a signal there) and corrupts the signal when *proving*. If a
    command only passes after ad-hoc fixes beyond ``install_cmd``, the profile is
    genuinely not usable yet and stays unproven for a human to see.

The result is a proven-but-unconfirmed profile, proposed to a human. Confirming
it (``nh onboard <repo> --confirm``) reuses the ``nh learnings`` gate pattern:
nothing is trusted until a human confirms, and a profile drives a task only when
``confirmed AND proven['test_cmd']`` (``ProjectProfile.is_usable``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from rich.markup import escape

from .profile import ProjectProfile
from .testing import runner

if TYPE_CHECKING:
    from .core.db import Store

log = logging.getLogger(__name__)

# Commands we prove, in the order we prove them. install runs first so the test
# command's dependencies exist; test is the trust anchor; lint is best-effort.
_KINDS = ("install", "test", "lint")

# The coding backend always needs the subscription token (constraint §3.1).
_ALWAYS_REQUIRED = ("CLAUDE_CODE_OAUTH_TOKEN",)


def _strip_remote_credentials(url: str) -> str:
    """Drop any ``user[:token]@`` embedded in an https remote so we never store
    or echo a credential that was baked into the origin URL."""
    return re.sub(r"(https?://)[^/@]+@", r"\1", url.strip())


def _host_from_remote(url: str) -> str:
    """Extract the host from an origin URL (https or scp-style git@host:path)."""
    url = url.strip()
    m = re.match(r"^[a-zA-Z][\w+.-]*://(?:[^/@]*@)?([^/:]+)", url)  # scheme://[user@]host
    if m:
        return m.group(1).lower()
    m = re.match(r"^[^@]+@([^:]+):", url)  # git@host:owner/repo.git
    if m:
        return m.group(1).lower()
    return ""


def derive_required_credentials(ci: dict[str, Any], vcs_host: str,
                                human_gated_steps: list[str] | None = None,
                                github_hosts: list[str] | None = None) -> list[str]:
    """The ~/.no_human/.env keys this repo needs, derived from its CI backend and
    VCS host — never hardcoded per repo. Returned in a stable, de-duplicated
    order. Values are never read here; only the key names a human must set.

    ``github_hosts`` is the operator's configured list of GitHub-Enterprise hosts
    (``git.github_hosts``); a VCS host on it that isn't public github.com needs an
    enterprise token to open PRs.
    """
    keys: list[str] = list(_ALWAYS_REQUIRED)
    backend = (ci or {}).get("backend", "")
    steps_text = " ".join(human_gated_steps or []).lower()
    ghe = {h.lower() for h in (github_hosts or [])}

    if backend == "jenkins" or "jenkins" in steps_text:
        keys += ["JENKINS_USER", "JENKINS_API_TOKEN"]
    if backend == "gitlab" or (vcs_host and "gitlab" in vcs_host):
        keys.append("GITLAB_TOKEN")
    # PR opening: public github.com uses `gh auth login` (no env key); a GHE host
    # (configured, or simply not github.com but github-flavored) needs a token.
    if vcs_host and vcs_host != "github.com" and (
        vcs_host in ghe or "github" in vcs_host or vcs_host == "code.example.com"
    ):
        keys.append("GH_ENTERPRISE_TOKEN")

    seen: set[str] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


@dataclass
class CommandCandidate:
    kind: str            # "install" | "test" | "lint"
    command: str
    source: str          # which declaration it came from, e.g. "package.json:scripts.test"


@dataclass
class DerivedCommands:
    ecosystem: str = ""
    candidates: list[CommandCandidate] = field(default_factory=list)
    ci: dict[str, Any] = field(default_factory=dict)
    human_gated_steps: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # all declarations consulted
    # no-human-67 follow-up: a detected `npm run dev` convention (see
    # detect_dev_server below), or None when nothing was detected. AgentDeriver
    # never sets this (leaves the dataclass default) — the agentic path has no
    # equivalent recon step, by design (out of scope for this ticket).
    dev_server: dict[str, Any] | None = None

    def of_kind(self, kind: str) -> list[CommandCandidate]:
        return [c for c in self.candidates if c.kind == kind]


@dataclass
class ProveOutcome:
    kind: str
    command: str
    ok: bool
    exit_code: int
    output: str
    source: str

    @property
    def summary(self) -> str:
        verdict = "PROVEN" if self.ok else "FAILED"
        return f"[{verdict}] {self.kind}: {self.command}  (from {self.source}, exit {self.exit_code})"

    def failure_tail(self, max_lines: int = 12, max_width: int = 200) -> str:
        """The tail of this command's own output, for a FAILED candidate.

        Empty for anything that passed, and empty when the command said nothing,
        so a clean prove run reads exactly as it does today. Refusing to confirm
        an unproven command is right; refusing without a diagnostic is where the
        reader stops, unable to tell a missing dependency from an import error
        from a genuinely failing test (KI-4 in docs/KNOWN_ISSUES.md).

        Only ``output`` is ever rendered, which is the command's own stdout and
        stderr, and it goes through ``_redact`` first. Nothing derived from the
        environment is printed, but a proved command can print a credential of
        its own, so the tail is masked the same way a verification receipt is.

        Bounded on both axes: the last ``max_lines`` lines, each clipped to
        ``max_width``, so neither a 10,000-line pytest failure nor a single
        enormous line can flood the terminal. Whatever is dropped is announced
        rather than silently cut.
        """
        if self.ok:
            return ""
        from .agent.verification_receipts import _redact

        lines = _redact(self.output).strip().splitlines()
        if not lines:
            return ""

        elided = len(lines) - max_lines
        shown = lines[-max_lines:] if elided > 0 else lines
        rendered = []
        if elided > 0:
            rendered.append(f"      [dim]... {elided} earlier line(s) not shown[/]")
        for line in shown:
            clipped = line[:max_width]
            if len(line) > max_width:
                clipped += f"... (+{len(line) - max_width} chars)"
            # `[` opens a Rich markup tag, and this is the command's output, not
            # ours: an unescaped `[foo]` in a traceback would be swallowed or
            # would raise on an unclosed tag. Rich's own escape is used rather
            # than replacing `[`, because a line already ending in a backslash
            # (a Windows path at the end of a traceback) would otherwise escape
            # the closing tag and print it literally.
            rendered.append(f"      [dim]{escape(clipped)}[/]")
        return "\n" + "\n".join(rendered)


@dataclass
class OnboardResult:
    profile: ProjectProfile
    proofs: list[ProveOutcome]


# --------------------------------------------------------------------------- #
# Derivers                                                                     #
# --------------------------------------------------------------------------- #


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _make_targets(makefile: Path) -> set[str]:
    targets: set[str] = set()
    for line in _read_text(makefile).splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", line)
        if m:
            targets.add(m.group(1))
    return targets


# Framework -> its documented default dev-server port. Deliberately a closed
# table: an unknown framework yields NO suggestion rather than a guessed port,
# because the value ends up in a base_url the harness probes. Detection scope
# is deliberately rigid (no-human-67 follow-up) — only the `dev` script name
# plus this table; no `start`/`serve` scripts, no `--port` flag parsing, no
# non-npm package managers. Widening this is a separate ticket.
_DEV_SERVER_PORTS = {
    "vite": 5173,
    "@sveltejs/kit": 5173,
    "next": 3000,
    "nuxt": 3000,
    "react-scripts": 3000,
    "astro": 4321,
    "@angular/cli": 4200,
    "@vue/cli-service": 8080,
}


_WEB_BUILD_CMD = "npm --prefix web ci && npm --prefix web run build"


def _detect_web_build_cmd(repo: Path) -> str | None:
    """`web/package.json` declaring a `build` script -> the ci+build chain, so a
    FRESH worktree (no node_modules, gitignored `web/dist`) still has a UI to
    serve. Missing file / bad JSON / no build script -> None (no key at all)."""
    pkg = repo / "web" / "package.json"
    try:
        data = json.loads(_read_text(pkg) or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts") or {}
    if "build" not in scripts:
        return None
    return _WEB_BUILD_CMD


def detect_dev_server(repo_path: str | Path, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Detect a `npm run dev` convention from a repo's own package.json.

    Reuses the already-parsed package.json ``data`` when the caller (usually
    ``_derive_node``) has one, rather than re-reading/re-parsing the file — the
    extension point the plan calls for, not a new detector. Returns None
    unless BOTH hold: a ``dev`` script exists, AND a known framework (a key of
    ``_DEV_SERVER_PORTS``) appears in ``dependencies``/``devDependencies`` —
    the framework name is what tells us which port its own dev server binds,
    not a guess.
    """
    repo = Path(repo_path).expanduser()
    if data is None:
        pkg = repo / "package.json"
        if not pkg.exists():
            return None
        try:
            data = json.loads(_read_text(pkg) or "{}")
        except json.JSONDecodeError:
            return None
    scripts = data.get("scripts") or {}
    if "dev" not in scripts:
        return None
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    framework = next((name for name in _DEV_SERVER_PORTS if name in deps), None)
    if framework is None:
        return None
    port = _DEV_SERVER_PORTS[framework]
    out = {
        "start_cmd": "npm run dev",
        "base_url": f"http://localhost:{port}",
        "port": port,
        "framework": framework,
        "source": "package.json:scripts.dev",
    }
    build_cmd = _detect_web_build_cmd(repo)
    if build_cmd:
        out["build_cmd"] = build_cmd
    return out


class DeclarationDeriver:
    """Derive candidate commands from a repo's own declared build/test config.

    Reads only shallow, well-known files — never a deep recursive walk on a large
    repo. Produces candidates in priority order (most-specific declaration
    first); the engine proves them in that order and keeps the first that runs
    clean.
    """

    def derive(self, repo_path: str | Path) -> DerivedCommands:
        repo = Path(repo_path).expanduser()
        d = DerivedCommands()

        self._detect_ci(repo, d)
        self._derive_node(repo, d)
        self._derive_python(repo, d)
        self._derive_maven(repo, d)
        self._derive_make(repo, d)
        return d

    # -- CI backend + human gates (declaration-driven) -- #

    def _detect_ci(self, repo: Path, d: DerivedCommands) -> None:
        if (repo / ".gitlab-ci.yml").exists():
            d.ci = {"backend": "gitlab"}
            d.sources.append(".gitlab-ci.yml")
        elif (repo / ".github" / "workflows").is_dir() and any(
            (repo / ".github" / "workflows").glob("*.y*ml")
        ):
            d.ci = {"backend": "github_actions"}
            d.sources.append(".github/workflows")
        if (repo / "Jenkinsfile").exists():
            d.human_gated_steps.append("build/CI gated on Jenkins (Jenkinsfile)")
            d.sources.append("Jenkinsfile")

    # -- Node -- #

    def _derive_node(self, repo: Path, d: DerivedCommands) -> None:
        pkg = repo / "package.json"
        if not pkg.exists():
            return
        try:
            data = json.loads(_read_text(pkg) or "{}")
        except json.JSONDecodeError:
            return
        scripts = data.get("scripts") or {}
        d.ecosystem = d.ecosystem or "node"
        d.sources.append("package.json")
        d.dev_server = detect_dev_server(repo, data)
        if (repo / "package-lock.json").exists():
            install = "npm ci"
        elif (repo / "yarn.lock").exists():
            install = "yarn install --frozen-lockfile"
        elif (repo / "pnpm-lock.yaml").exists():
            install = "pnpm install --frozen-lockfile"
        else:
            install = "npm install"
        d.candidates.append(CommandCandidate("install", install, "package.json"))
        if "test" in scripts:
            d.candidates.append(
                CommandCandidate("test", "npm test", "package.json:scripts.test")
            )
        if "lint" in scripts:
            d.candidates.append(
                CommandCandidate("lint", "npm run lint", "package.json:scripts.lint")
            )

    # -- Python (pytest) -- #

    def _derive_python(self, repo: Path, d: DerivedCommands) -> None:
        if not runner._looks_like_pytest(repo):
            return
        d.ecosystem = d.ecosystem or "python-pytest"
        pyproject = _read_text(repo / "pyproject.toml")
        if (repo / "uv.lock").exists():
            d.candidates.append(CommandCandidate("install", "uv sync", "uv.lock"))
            # Same rule as runner.detect_command: parallelize only when the
            # repo declares pytest-xdist. An onboarded serial test_cmd is what
            # made every attempt run a 7,700-test suite serially (2026-08-10) —
            # the profile overrides the runner heuristic, so the derivation
            # here is the value that actually governs.
            if runner._declares_xdist(repo):
                test_cmd, run_prefix = "uv run pytest -q -n 4", "uv run "
            else:
                test_cmd, run_prefix = "uv run pytest -q", "uv run "
            d.sources.append("uv.lock")
        elif (repo / "poetry.lock").exists():
            d.candidates.append(CommandCandidate("install", "poetry install", "poetry.lock"))
            test_cmd, run_prefix = "poetry run pytest -q", "poetry run "
            d.sources.append("poetry.lock")
        else:
            reqs = sorted(repo.glob("requirements*.txt"))
            if reqs:
                d.candidates.append(CommandCandidate(
                    "install", f"pip install -r {reqs[0].name}", reqs[0].name))
            test_cmd, run_prefix = "pytest -q", ""
            d.sources.append("pyproject.toml" if (repo / "pyproject.toml").exists() else "pytest")
        d.candidates.append(CommandCandidate("test", test_cmd, "python/pytest"))
        if "ruff" in pyproject:
            d.candidates.append(CommandCandidate("lint", f"{run_prefix}ruff check .", "pyproject.toml:ruff"))

    # -- Maven -- #

    def _derive_maven(self, repo: Path, d: DerivedCommands) -> None:
        if not (repo / "pom.xml").exists():
            return
        d.ecosystem = d.ecosystem or "maven"
        d.sources.append("pom.xml")
        d.candidates.append(CommandCandidate("install", "mvn -q -DskipTests install", "pom.xml"))
        d.candidates.append(CommandCandidate("test", "mvn -q test", "pom.xml"))

    # -- Makefile (fallback for kinds nothing else declared) -- #

    def _derive_make(self, repo: Path, d: DerivedCommands) -> None:
        makefile = next((repo / n for n in ("Makefile", "makefile") if (repo / n).exists()), None)
        if makefile is None:
            return
        targets = _make_targets(makefile)
        added = False
        for kind in _KINDS:
            if kind in targets and not d.of_kind(kind):
                d.candidates.append(CommandCandidate(kind, f"make {kind}", f"Makefile:{kind}"))
                added = True
        if added:
            d.ecosystem = d.ecosystem or "make"
            d.sources.append("Makefile")


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class AgentDeriver:
    """Derive candidates via a bounded, read-only Agent SDK recon session — the
    path for repos whose declarations are nonstandard (the deterministic
    ``DeclarationDeriver`` covers the common ecosystems).

    The session may only *read* (the backend runs ``readonly=True``, so its
    PreToolUse guard blocks writes); it proposes candidates as a fenced JSON
    block which we parse. It never proves — proving stays a subprocess in the
    engine, so an agent cannot fake the proof signal.
    """

    PROMPT = (
        "You are onboarding a repository at {repo}. Do NOT modify anything — only "
        "read files (grep/glob/read).\n\n"
        "Inspect the repo's OWN declarations (lockfiles, package manifests, "
        "Makefile, CI yaml, README) and report the exact commands a developer "
        "runs to install dependencies, run the unit tests, and lint. Prefer "
        "commands the repo actually declares over generic guesses.\n\n"
        "Respond with ONLY a fenced ```json block of the form:\n"
        '{{"ecosystem": "...", "ci": {{"backend": "gitlab|github_actions|jenkins|"}}, '
        '"human_gated_steps": ["..."], '
        '"candidates": [{{"kind": "install|test|lint", "command": "...", "source": "<declaration>"}}]}}'
    )

    def __init__(self, backend: Any, *, max_turns: int = 12):
        self.backend = backend
        self.max_turns = max_turns

    async def derive(self, repo_path: str | Path) -> DerivedCommands:
        repo = Path(repo_path).expanduser()
        result = await self.backend.run(
            self.PROMPT.format(repo=repo),
            cwd=repo,
            max_turns=self.max_turns,
            effort="low",
        )
        return self.parse(result.final_text or "")

    @staticmethod
    def parse(text: str) -> DerivedCommands:
        blocks = _JSON_BLOCK.findall(text)
        if not blocks:
            return DerivedCommands()
        try:
            data = json.loads(blocks[-1])
        except json.JSONDecodeError:
            return DerivedCommands()
        cands = [
            CommandCandidate(c.get("kind", ""), c.get("command", ""), c.get("source", "agent"))
            for c in (data.get("candidates") or [])
            if c.get("kind") in _KINDS and c.get("command")
        ]
        return DerivedCommands(
            ecosystem=data.get("ecosystem", ""),
            candidates=cands,
            ci=data.get("ci") or {},
            human_gated_steps=list(data.get("human_gated_steps") or []),
            sources=["agent-recon"],
        )


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #


class OnboardEngine:
    """Derive candidates, prove each by running it, build a proposed profile."""

    def __init__(self, deriver: Any | None = None, *, prove_timeout: int = 600,
                 github_hosts: list[str] | None = None,
                 on_event: "Any | None" = None):
        self.deriver = deriver or DeclarationDeriver()
        self.prove_timeout = prove_timeout
        self.github_hosts = github_hosts or ["github.com"]
        # Optional progress sink: called with small JSON-able dicts as proving
        # happens, so a caller (the web wizard) can show the REAL output live
        # instead of a spinner. Advisory only — it can never change a verdict.
        self.on_event = on_event

    def _emit(self, frame: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(frame)
        except Exception:  # noqa: BLE001 — a broken sink never fails a proof
            pass

    async def onboard(self, repo_path: str | Path,
                      overrides: dict[str, str] | None = None) -> OnboardResult:
        """Derive + prove. ``overrides`` maps a kind ("test"/"install"/"lint")
        to a command the OPERATOR typed — used when the web wizard's first
        derived command failed and the human corrected it. An override REPLACES
        that kind's derived candidates: we prove exactly the string the human
        gave us, byte for byte, and never a "helpful" variant of it. That is the
        same discipline as the derive/prove split in this module's docstring —
        the human may choose the command, but only a real clean exit proves it.
        """
        repo = Path(repo_path).expanduser().resolve()
        overrides = {k: v.strip() for k, v in (overrides or {}).items()
                     if isinstance(v, str) and v.strip()}
        derived = self.deriver.derive(repo)
        if inspect.isawaitable(derived):
            derived = await derived

        vcs_host, vcs_remote = await asyncio.to_thread(self._derive_vcs, repo)

        proofs: list[ProveOutcome] = []
        chosen: dict[str, CommandCandidate] = {}
        proven: dict[str, bool] = {}

        def _candidates(kind: str) -> list[CommandCandidate]:
            if kind in overrides:
                return [CommandCandidate(kind, overrides[kind], "operator-supplied")]
            return derived.of_kind(kind)

        self._emit({
            "kind": "derived",
            "ecosystem": derived.ecosystem,
            "sources": sorted(set(derived.sources)),
            "candidates": {k: [c.command for c in _candidates(k)] for k in _KINDS},
        })

        # Prove in install → test → lint order; the first candidate of each kind
        # that runs clean wins. install runs before test so deps are present.
        for kind in _KINDS:
            for cand in _candidates(kind):
                self._emit({"kind": "prove_start", "cmd_kind": kind,
                            "command": cand.command, "source": cand.source})
                outcome = await self._prove(repo, cand)
                proofs.append(outcome)
                self._emit({"kind": "prove_result", "cmd_kind": kind,
                            "command": cand.command, "ok": outcome.ok,
                            "exit_code": outcome.exit_code})
                if outcome.ok:
                    chosen[kind] = cand
                    proven[f"{kind}_cmd"] = True
                    break

        required = derive_required_credentials(
            derived.ci, vcs_host, derived.human_gated_steps, self.github_hosts)

        profile = ProjectProfile(
            repo_path=str(repo),
            ecosystem=derived.ecosystem,
            install_cmd=chosen["install"].command if "install" in chosen else "",
            test_cmd=chosen["test"].command if "test" in chosen else "",
            lint_cmd=chosen["lint"].command if "lint" in chosen else "",
            ci=derived.ci,
            human_gated_steps=derived.human_gated_steps,
            vcs_host=vcs_host,
            vcs_remote=vcs_remote,
            required_credentials=required,
            derived_from=sorted(set(derived.sources)),
            proven=proven,
            confirmed=False,
            notes=self._notes(derived, proofs),
        )
        return OnboardResult(profile=profile, proofs=proofs)

    @staticmethod
    def _derive_vcs(repo: Path) -> tuple[str, str]:
        """Read the repo's ``origin`` remote → (host, credential-stripped URL).
        Returns ("", "") when there is no origin (a fresh/local-only repo)."""
        import subprocess
        try:
            proc = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return "", ""
        if proc.returncode != 0:
            return "", ""
        url = proc.stdout.strip()
        return _host_from_remote(url), _strip_remote_credentials(url)

    def _line_sink(self, kind: str, command: str) -> "Any | None":
        """A per-line callback that forwards raw output to ``on_event``, or None
        when nobody is watching (the CLI path, unchanged)."""
        if self.on_event is None:
            return None

        def _sink(line: str) -> None:
            self._emit({"kind": "output", "cmd_kind": kind,
                        "command": command, "line": line})
        return _sink

    async def _prove(self, repo: Path, cand: CommandCandidate) -> ProveOutcome:
        on_line = self._line_sink(cand.kind, cand.command)
        if cand.kind == "test":
            # Reuse the orchestrator's own test path so we prove what it will run.
            res = await asyncio.to_thread(
                runner.run_tests, repo, cand.command, timeout=self.prove_timeout,
                on_line=on_line,
            )
            ok = res.ran and res.ok
            # `runner.run_tests` has a bounded self-correcting retry that can
            # rewrite a broken invocation (python->python3, stripping addopts).
            # The orchestrator calls the SAME function, so the proof still holds
            # for `cand.command`; but a human confirming on evidence must be
            # told the string that actually exited clean was not the one shown.
            actual = getattr(res, "command", "") or cand.command
            if actual != cand.command:
                self._emit({"kind": "rewritten", "cmd_kind": cand.kind,
                            "command": cand.command, "actual_command": actual})
            return ProveOutcome(
                cand.kind, cand.command, ok, 0 if ok else 1, res.output, cand.source
            )
        ok, code, out = await asyncio.to_thread(
            runner.run_command, repo, cand.command, timeout=self.prove_timeout,
            on_line=on_line,
        )
        return ProveOutcome(cand.kind, cand.command, ok, code, out, cand.source)

    @staticmethod
    def _notes(derived: DerivedCommands, proofs: list[ProveOutcome]) -> str:
        proven = [p for p in proofs if p.ok]
        unproven = [p for p in proofs if not p.ok]
        lines = [f"derived by nh onboard (ecosystem: {derived.ecosystem or 'unknown'})"]
        if unproven:
            lines.append(
                "unproven candidates: "
                + "; ".join(f"{p.kind}={p.command!r} (exit {p.exit_code})" for p in unproven)
            )
        return " | ".join(lines)


# --------------------------------------------------------------------------- #
# The human confirm gate                                                       #
# --------------------------------------------------------------------------- #


class ProfileNotProven(RuntimeError):
    """Raised when something tries to confirm a profile whose test command was
    never proven. Deliberately an exception rather than a False return: every
    caller must handle it, and none may quietly downgrade to "confirmed anyway".
    """


def confirm_profile(profile: ProjectProfile) -> ProjectProfile:
    """Flip ``confirmed`` on a profile whose TEST COMMAND was proven — the one
    human gate that makes a profile usable (``ProjectProfile.is_usable``).

    Single source of truth for `nh onboard --confirm` (CLI) and the web
    wizard's confirm step, so the two can never drift into different notions of
    what may be confirmed. It only ever flips the flag: it does not run
    anything, and it CANNOT create a proof — the proof must already exist,
    written by a real clean exit in ``OnboardEngine._prove``.
    """
    if not profile.test_cmd or not profile.proven.get("test_cmd"):
        raise ProfileNotProven(
            "cannot confirm: the test command is not proven "
            f"(test_cmd={profile.test_cmd!r}). Run the test command until it "
            "exits clean — trust requires proof, and nothing here fakes one."
        )
    profile.confirmed = True
    return profile


# --------------------------------------------------------------------------- #
# UI-evidence provisioning (no-human-67 follow-up)                            #
# --------------------------------------------------------------------------- #
#
# No flow ever configured `ProjectProfile.ui_evidence` — the visual-proof-walks
# feature (testing/ui_evidence.py, out of scope here) is unreachable for any
# customer even with Playwright installed, because nothing ever writes
# start_cmd/base_url for their repo. This section only PROVISIONS that config
# (a detect -> one-confirm-offer -> dual-write pipeline); the consumer runtime
# that reads it is untouched.

UI_EVIDENCE_PROMPT = "Enable visual-proof walks?"


def ui_evidence_configured(profile: ProjectProfile) -> bool:
    """True once a repo's ui_evidence is configured — manually or via a prior
    accept — so a suggestion never re-fires and never overwrites a human's own
    choice. Checked against the persisted profile, never re-derived."""
    ui = getattr(profile, "ui_evidence", None) or {}
    return bool(ui.get("enabled") or ui.get("start_cmd") or ui.get("base_url"))


def ui_evidence_suggestion(
    profile: ProjectProfile,
    repo_path: str | Path | None = None,
    dev_server: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The one suggestion object shared by the CLI offer, `nh doctor`'s
    advisory, and the wizard's API row — so all three read the exact same gap
    text and the exact same start_cmd/base_url. Returns None when the repo is
    already configured (manual config wins — never re-prompt) OR nothing was
    detected (``detect_dev_server``'s closed table found no known framework).

    ``dev_server`` may be passed directly (the API already has ``derived``
    from this request); otherwise it is (re-)detected from ``repo_path``, file
    reads only, no network, no subprocess.
    """
    if ui_evidence_configured(profile):
        return None
    if dev_server is None:
        repo = repo_path if repo_path is not None else profile.repo_path
        dev_server = detect_dev_server(repo)
    if not dev_server:
        return None
    gap = (
        "visual-proof walks: repo not configured — detected "
        f"`{dev_server['start_cmd']}` on :{dev_server['port']}. Accepting "
        "means no_human will RUN this command to start your dev server "
        "during visual-proof walks (and stop it after) — enable?"
    )
    suggestion = {
        "start_cmd": dev_server["start_cmd"],
        "base_url": dev_server["base_url"],
        "port": dev_server["port"],
        "framework": dev_server["framework"],
        "gap": gap,
    }
    if dev_server.get("build_cmd"):
        suggestion["build_cmd"] = dev_server["build_cmd"]
    return suggestion


def apply_ui_evidence_suggestion(profile: ProjectProfile, suggestion: dict[str, Any]) -> ProjectProfile:
    """Turn an accepted suggestion into the enabled ui_evidence config, in
    place. ``ready_path``/``ready_timeout_s``/``ui_paths`` keep whatever the
    dataclass default already put on ``profile.ui_evidence`` — only the three
    keys the suggestion actually knows about change. Pure: persisting the
    result (yml + DB) is the caller's job, via ``persist_profile``."""
    ui = dict(profile.ui_evidence or {})
    ui["enabled"] = True
    ui["start_cmd"] = suggestion["start_cmd"]
    ui["base_url"] = suggestion["base_url"]
    if suggestion.get("build_cmd"):
        ui["build_cmd"] = suggestion["build_cmd"]
    profile.ui_evidence = ui
    return profile


class ProjectYmlPersistError(RuntimeError):
    """Raised by `persist_profile` when `project.yml` cannot be written.

    Unlike `onboarding_confirm_repo`'s older pairing (which logs the OSError
    from `profile.save()` and writes the DB row regardless), this feature's
    own acceptance criteria promise the two artifacts as a matching pair —
    "verify both artifacts contain matching config". Writing only the DB row
    would leave a live, "enabled" row with no on-disk config to match it,
    while every caller up the chain (CLI, API) reports full success. So on a
    yml failure the DB write is skipped too, and this is raised instead of
    swallowed: no caller may report success, and no split-brain state (DB
    says enabled, project.yml does not) is ever created.
    """


async def persist_profile(store: "Store", profile: ProjectProfile) -> None:
    """The one dual-write: `<repo>/.no_human/project.yml` AND the DB row, in
    that order. Raises `ProjectYmlPersistError` — and skips the DB write —
    if the yml write fails; see that class for why. Callers (CLI, API) must
    catch it and surface the failure honestly rather than reporting success.
    """
    try:
        profile.save()
    except OSError as exc:
        log.warning("could not write project.yml for %s: %s", profile.repo_path, exc)
        raise ProjectYmlPersistError(
            f"could not write project.yml for {profile.repo_path}: {exc}"
        ) from exc
    await store.upsert_profile(profile)


async def offer_ui_evidence(
    store: "Store",
    profile: ProjectProfile,
    suggestion: dict[str, Any],
    *,
    ask: Callable[[str], bool],
) -> bool:
    """Ask once (via ``ask``, the only interactive part — a plain callable so
    CLI and API callers can each supply their own prompt/decline mechanics)
    and, on acceptance, apply + persist the suggestion. Returns whether it was
    enabled. A decline (``ask`` returns False) writes NOTHING — no yml write,
    no DB write — a decline must never create config, exactly as a repo with
    no dev script leaves no trace anywhere.

    On acceptance this propagates ``ProjectYmlPersistError`` from
    ``persist_profile`` rather than swallowing it — a caller that got back
    ``True`` here has a real, matching dual-write; a caller that gets the
    exception must not print/return a success message.

    ``ask`` receives ``UI_EVIDENCE_PROMPT`` — the ONE prompt text ("Enable
    visual-proof walks?") — not the longer gap description, which the caller
    prints separately as context before asking."""
    if not ask(UI_EVIDENCE_PROMPT):
        return False
    apply_ui_evidence_suggestion(profile, suggestion)
    await persist_profile(store, profile)
    return True
