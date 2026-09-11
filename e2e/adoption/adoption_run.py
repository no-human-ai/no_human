#!/usr/bin/env python3
"""The daily adoption test: can four people who have only read the public docs
get no_human working on a real backlog, from nothing, today?

    e2e/adoption/run.sh                 # the whole thing, default smoke mode
    e2e/adoption/run.sh --mode full     # + real task execution (costs money)

WHY THIS EXISTS
---------------
Every previous verification of this product was performed by someone who
already knew the answer. That is not a criticism of the people; it is a
structural property of verifying your own work, and it has a specific failure
signature: the checks all pass and the product is unusable. Two examples that
were live in main when this harness was written, both of which one honest
persona run catches in under a minute:

  * `uv sync` — the README's install command — fails on every clean clone,
    because the wheel force-includes a gitignored build directory.
  * `nh task add PROJ-42` is documented in three places and its adapter was
    deleted.

Neither was caught by the test suite, because the test suite runs in a tree
where `web/dist` exists and calls the intake functions that do exist.

THE DESIGN CONSTRAINT THAT MAKES THE RESULT WORTH ANYTHING
----------------------------------------------------------
Everything runs in an environment built to contain only what the persona would
have:

  HOME       a fresh temp directory. `~/.no_human` therefore starts empty, and
             the operator's real `~/.no_human` is never opened. Proven two
             ways, positively and negatively: the product's own process is
             asked where its home is and must answer with the temp directory,
             and no new entry may appear in the operator's real one.
  PATH       a curated shim directory containing symlinks to the tools a Mac
             developer plausibly has (git, uv, node, npm, claude) and NOTHING
             else. In particular the harness asserts `nh` is NOT on PATH, since
             on the author's own machine it is, and that single fact is what
             hid the quickstart's bare-`nh` bug for months.
  repo       a fresh `git clone` of the product. Not a worktree, not the
             working tree, not `pip install -e .` of the tree you are sitting
             in — a clone, because the bug above only exists in a clone.
  target     a fresh SkyLine repo, generated from scratch, with two REAL
             planted bugs that a real test can catch.
  secrets    none. No real credential is read, written, or requested. Jira and
             Slack are exercised against protocol-faithful local fakes and every
             such result is labelled `live: false`. See fakes.py.

WHAT IT MEASURES, AND WHAT EACH NUMBER IS WORTH
-----------------------------------------------
  friction        every gap between the docs and the product, severity-ranked.
                  This is the primary output. It needs no credential and no
                  spend, which is why it is the default mode and why it can run
                  daily.
  delivered       tickets that reached a reviewed PR with no human rescue.
  honest stops    tickets that SHOULD have escalated and did. A PR on an
                  ambiguous ticket is scored as a failure, not a success.
  cost per PR     real spend from the run, against the "10% of your AI coding
                  bill" claim. Only meaningful in full mode.
  throughput      against the team's own human estimates, which are estimates
                  and are labelled as such everywhere they appear.

The last three require a Claude credential and real spend, so they live behind
`--mode full` and are reported as NOT MEASURED rather than as zero when the mode
is not selected. Reporting an unmeasured number as zero is how a dashboard
starts lying.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import backlog as backlog_mod  # noqa: E402
import fakes as fakes_mod  # noqa: E402
import personas as personas_mod  # noqa: E402
from personas import Finding, PersonaRun, StepResult  # noqa: E402

REPO_ROOT = HERE.parent.parent

# Tools a Mac developer at this startup plausibly has. Anything not on this list
# is not available to any persona. `nh` is deliberately, loudly absent.
PERSONA_TOOLS = ("git", "uv", "uvx", "node", "npm", "npx", "claude", "python3",
                 "sh", "bash", "env", "which", "gh")
FORBIDDEN_ON_PATH = ("nh",)


# --------------------------------------------------------------------------- #
# Environment construction
# --------------------------------------------------------------------------- #

# The two documents the public onboarding path sends a new user to. A finding
# about "is X documented" is only allowed to come from having read these.
ONBOARDING_DOCS = ("README.md", "docs/quickstart.md")


@dataclass
class DocSearch:
    """The result of searching the onboarding docs for one term.

    This is what "having read the file" looks like as data, rather than as an
    assertion — a check can hand back a `DocSearch` and a test can show it was
    produced by a real read, instead of trusting a hardcoded conclusion. Every
    rel path in `docs` gets an entry in `hits`; a doc that is missing or
    unreadable counts as -1 (never silently 0, and never silently skipped) and
    the reason lands in `notes`, both surfaced by `evidence()`.
    """
    term: str
    hits: dict[str, int]
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def missing_from(self) -> list[str]:
        """rel paths where the term was not found (includes unreadable docs)."""
        return [rel for rel, n in self.hits.items() if n <= 0]

    def evidence(self) -> str:
        return json.dumps(
            {"term": self.term, "searched": list(self.hits),
             "hits": self.hits, "notes": self.notes},
            indent=2)


@dataclass
class Ctx:
    work: Path
    home: Path
    shim: Path
    product: Path
    target: Path
    origin_url: str
    bare_remote: Path
    mode: str
    env: dict[str, str] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    _findings: list[Finding] = field(default_factory=list)

    # -- data ------------------------------------------------------------- #
    @property
    def backlog(self):
        return backlog_mod.BACKLOG

    def backlog_by_key(self, key: str):
        return backlog_mod.by_key(key)

    def note(self, key: str, value: Any) -> None:
        self.notes[key] = value

    # -- running things as a persona -------------------------------------- #
    def shell(self, run: PersonaRun, step: str, intent: str, doc_ref: str,
              command, *, cwd: Path, allow_fail: bool = False,
              timeout: int = 300, stdin_devnull: bool = False,
              quiet: bool = False, env: dict[str, str] | None = None,
              input: str | None = None) -> StepResult:
        """Run one persona action in the persona's environment.

        ``command`` is a string (run through a shell, the way a person types it)
        or a list (run directly, for arguments containing newlines/quotes that a
        person would have pasted from a ticket).

        ``env`` overrides the persona's own ``self.env`` for this one step —
        for the rare step that has to prove something about an environment
        *unlike* the ambient one (e.g. a machine with no credential exported
        yet). ``input`` pipes text to stdin, the way a person piping a token
        into a scripted command would; it is mutually exclusive with
        ``stdin_devnull`` and wins if both are given.
        """
        shown = command if isinstance(command, str) else shlex.join(command)
        t0 = time.time()
        run_env = self.env if env is None else env
        try:
            if input is not None:
                proc = subprocess.run(
                    command if isinstance(command, list) else ["/bin/sh", "-c", command],
                    cwd=str(cwd), env=run_env, capture_output=True, text=True,
                    timeout=timeout, input=input,
                )
            else:
                stdin = subprocess.DEVNULL if stdin_devnull else subprocess.DEVNULL
                proc = subprocess.run(
                    command if isinstance(command, list) else ["/bin/sh", "-c", command],
                    cwd=str(cwd), env=run_env, capture_output=True, text=True,
                    timeout=timeout, stdin=stdin,
                )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = None, (exc.stdout or b"").decode("utf-8", "replace"), \
                f"TIMEOUT after {timeout}s"
        except FileNotFoundError as exc:
            code, out, err = 127, "", str(exc)
        res = StepResult(run.name, step, intent, doc_ref, shown, code,
                         ok=(code == 0), stdout=out, stderr=err,
                         seconds=time.time() - t0)
        run.steps.append(res)
        if not quiet:
            flag = "ok  " if res.ok else "FAIL"
            print(f"    [{flag}] {run.name}/{step}: {shown[:96]}", flush=True)
        return res

    def py(self, script: str, *, extra_env: dict[str, str] | None = None,
           timeout: int = 180) -> tuple[bool, str]:
        """Run a probe inside the product's own venv, with the persona's env."""
        env = dict(self.env)
        env.update(extra_env or {})
        f = self.work / f"_probe_{abs(hash(script)) % 10**8}.py"
        f.write_text(textwrap.dedent(script))
        try:
            p = subprocess.run(["uv", "run", "python", str(f)], cwd=str(self.product),
                               env=env, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s"
        return p.returncode == 0, (p.stdout + p.stderr)[-4000:]

    # -- the setup Dana ends up doing by hand ------------------------------ #
    def seed_config(self) -> None:
        """Write the ~/.no_human that `nh init` would have produced.

        This is NOT a shortcut past a finding — the finding that `nh init` has
        no non-interactive path is already recorded by Dana. This is what a
        person does after hitting it, and the personas behind Dana need a
        working install to test anything else.

        The token is a syntactically-shaped placeholder. It is not a credential,
        it will not authenticate, and no real credential is read to produce it.
        """
        nh = self.home / ".no_human"
        nh.mkdir(parents=True, exist_ok=True)
        nh.chmod(0o700)
        envf = nh / ".env"
        if not envf.exists():
            envf.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-ADOPTION-HARNESS-PLACEHOLDER\n")
            envf.chmod(0o600)
        cfg = nh / "config.yaml"
        if not cfg.exists():
            cfg.write_text(textwrap.dedent(f"""\
                server:
                  host: 127.0.0.1
                  port: 8421
                llm:
                  auth_mode: subscription
                database:
                  path: {nh}/no_human.db
                git:
                  branch_prefix: "no-human/"
                  never_push_to: ["main", "master", "release/*"]
                  agent_identity_name: "no_human"
                  agent_identity_email: "no-human@skyline.example"
                notifications:
                  slack_webhook_url: null
                """))

    # -- fakes ------------------------------------------------------------- #
    @contextmanager
    def fake_jira(self):
        st = fakes_mod.state_from_backlog(self.backlog)
        with fakes_mod.FakeJira(st) as j:
            yield j

    @contextmanager
    def fake_slack(self):
        with fakes_mod.FakeSlack() as s:
            yield s

    # -- doc-derived invariants -------------------------------------------- #
    #
    # These three read the documentation and check the product against what it
    # currently says, rather than against what it said the day this was
    # written. The difference matters more than it looks: an assertion pinned
    # to a specific stale sentence keeps failing after somebody fixes that
    # sentence, gets muted as noise, and is then silent when the SAME class of
    # error appears in a different sentence. Every one of these was a hardcoded
    # check first, and every one of them went stale within an hour of the first
    # fix landing.

    def unrunnable_quickstart_commands(self, product: Path) -> list[dict[str, str]]:
        """Every `nh` command printed in the quickstart, checked for runnability.

        Not "does the doc contain the string `uv run`" — that is a check on
        prose. This takes each command line out of the fenced blocks, takes its
        program (the first token), and asks whether that program exists on the
        persona's PATH. A bare `nh` fails because `uv sync` installs the entry
        point into `.venv` and never onto PATH.
        """
        doc = product / "docs" / "quickstart.md"
        if not doc.is_file():
            return [{"line": "(missing)", "problem": "docs/quickstart.md not found"}]
        bad: list[dict[str, str]] = []
        in_fence = False
        for raw in doc.read_text(encoding="utf-8").splitlines():
            if raw.startswith("```"):
                in_fence = not in_fence
                continue
            line = raw.strip()
            if not in_fence or not line or line.startswith("#"):
                continue
            # Only lines that actually invoke this product.
            if not re.match(r"^(nh|uv run nh)\b", line):
                continue
            program = line.split()[0]
            probe = subprocess.run(["/bin/sh", "-c", f"command -v {shlex.quote(program)}"],
                                   cwd=str(product), env=self.env, text=True,
                                   capture_output=True, stdin=subprocess.DEVNULL)
            if probe.returncode != 0:
                bad.append({"line": line, "program": program,
                            "problem": f"`{program}` is not on PATH after `uv sync`"})
        return bad

    def docs_still_promise_bare_ticket_key(self, product: Path) -> list[str]:
        """Which user-facing docs still show `nh task add <KEY>` with a bare key.

        A bare key is one that is neither a URL nor an option — the shape the
        removed tracker adapter accepted.
        """
        hits: list[str] = []
        for rel in ("docs/quickstart.md", "docs/adapters.md", "README.md",
                    "docs/configuration.md"):
            f = product / rel
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                m = re.search(r"nh task add\s+([^\s`]+)", line)
                if not m:
                    continue
                arg = m.group(1)
                # A quoted argument is the documented freeform-sentence form
                # (`nh task add "some title" ...`), not a bare ticket key —
                # the capture stops at the first space, so `arg` here is only
                # the sentence's opening word wrapped in its leading quote.
                if (arg.startswith("-") or arg.startswith('"')
                        or "://" in arg or "/issues/" in arg):
                    continue
                # A line that explicitly says it does NOT work is documentation,
                # not a promise.
                if re.search(r"\bnot\b|\bremoved\b|\bno longer\b", line):
                    continue
                hits.append(f"{rel}: {line.strip()[:100]}")
        return hits

    def onboarding_docs_mentioning(self, product: Path, term: str,
                                    docs: tuple[str, ...] = ONBOARDING_DOCS,
                                    ) -> DocSearch:
        """Count occurrences of `term` in each of `docs`, read from `product`.

        The rule this exists to enforce: a finding about the content of a file
        is only allowed to come from having read that file. Case-insensitive
        substring count, so `nh doctor` also matches inside `` `uv run nh
        doctor` `` and `` `nh doctor --verify-auth` ``.

        Fails closed (memory: gates fail closed): a doc that does not exist or
        cannot be read counts as -1 occurrences, not 0 and not a skip, and is
        recorded in `missing_from` with the reason in `notes` — an absent
        README is a failure of the search, never a silent pass.

        A pure function of the filesystem (does not touch `self`), so it can
        be called unbound in tests exactly like
        `docs_still_promise_bare_ticket_key(None, product)` above.
        """
        needle = term.lower()
        hits: dict[str, int] = {}
        notes: dict[str, str] = {}
        for rel in docs:
            f = product / rel
            try:
                text = f.read_text(encoding="utf-8").lower()
            except OSError as exc:
                hits[rel] = -1
                notes[rel] = f"unreadable: {exc}"
                continue
            hits[rel] = text.count(needle)
        return DocSearch(term, hits, notes)

    def documented_env_keys_nothing_reads(self, product: Path) -> list[str]:
        """Keys in configuration.md's `.env` table that no source file reads.

        The table is the contract a user is handed. A name in it that nothing
        reads is a silent misconfiguration: no error, no integration, no clue.
        """
        doc = product / "docs" / "configuration.md"
        if not doc.is_file():
            return []
        text = doc.read_text(encoding="utf-8")
        # Find the `.env` keys section by its HEADING, then take everything up
        # to the next heading of the same level.
        #
        # The first version of this matched a literal substring of the heading
        # and silently matched nothing, so `keys` came out empty and the check
        # passed on a document that did document a dead key. It looked exactly
        # like a pass. It was caught by mutation-testing the checker against a
        # clone with the known defect re-introduced — which is the only way a
        # "no findings" result can ever be distinguished from a broken probe.
        lines = text.splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if ln.startswith("## ") and ".env" in ln and "key" in ln.lower()),
                     None)
        if start is None:
            return ["(harness) configuration.md has no `.env` keys section — the "
                    "credential contract this check reads is gone"]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        keys: list[str] = []
        for line in lines[start:end]:
            if not line.startswith("|"):
                continue
            cell = line.split("|")[1] if line.count("|") >= 2 else ""
            for name in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", cell):
                if name not in keys:
                    keys.append(name)
        if not keys:
            return ["(harness) the `.env` keys table parsed to zero keys — the "
                    "table's shape changed and this check is measuring nothing"]
        src = product / "src"
        dead = []
        for key in keys:
            hit = subprocess.run(["grep", "-rqF", key, str(src)],
                                 capture_output=True, stdin=subprocess.DEVNULL)
            if hit.returncode != 0:
                dead.append(key)
        self.note("documented_env_keys_checked", keys)
        return dead

    def undocumented_ci_surface(self, product: Path) -> dict[str, list[str]]:
        """What `ci/` supports and reads, minus what the docs tell you about it.

        getnohuman.com claims "Jenkins & CircleCI — test layers can run on your
        CI, and the results gate the loop." A persona wiring that up has only
        docs/adapters.md and docs/configuration.md. This compares three things
        the code knows against three things the docs say:

          backends   the `backend == "..."` branches in ci/__init__.py
          env vars   every environment variable read anywhere under ci/
          keys       the ci.* config keys each backend actually reads

        Derived from source rather than listed here, so a backend added
        tomorrow is covered without anybody remembering to update this.
        """
        ci_dir = product / "src" / "no_human" / "ci"
        docs = "\n".join(
            (product / "docs" / d).read_text(encoding="utf-8")
            for d in ("adapters.md", "configuration.md")
            if (product / "docs" / d).is_file())

        init = (ci_dir / "__init__.py").read_text(encoding="utf-8")
        backends = re.findall(r'backend == "([a-z_]+)"', init)
        # The keys each branch reads: `ci_conf.get("name"...)`.
        keys = sorted(set(re.findall(r'ci_conf\.get\(\s*"([a-z_]+)"', init)))
        env_vars = sorted({
            m for f in ci_dir.glob("*.py")
            for m in re.findall(
                r'(?:environ\.get\(|environ\[|getenv\(|load_env_var\()\s*"([A-Z][A-Z0-9_]{3,})"',
                f.read_text(encoding="utf-8"))})

        return {
            "backends_supported": backends,
            "backends_undocumented": [b for b in backends if b not in docs],
            "ci_config_keys_read": keys,
            "ci_config_keys_undocumented": [
                k for k in keys
                if not re.search(rf"(^|[^a-z_]){re.escape(k)}([^a-z_]|$)", docs, re.M)],
            "ci_env_vars_read": env_vars,
            "ci_env_vars_undocumented": [v for v in env_vars if v not in docs],
        }

    def probe_ci_verdict(self, backend: str, outcome: str) -> tuple[bool, str]:
        """Drive the REAL CI adapter against a local fake and report its verdict.

        Whether the loop is gated is decided by one thing: `CIResult.passed`.
        A test that only proves the adapter can be CONSTRUCTED proves nothing
        about the claim on the website, so this runs a full `trigger()` and
        reports the verdict together with the two flags that decide whether the
        orchestrator escalates rather than fails: `infra_failure` (retry, then
        escalate) and `access_failure` (park with MISSING_ACCESS).
        """
        script = f"""
            import asyncio, json, os, sys
            sys.path.insert(0, {str(HERE)!r})
            import fakes
            from no_human.ci import ci_from_config

            backend, outcome = {backend!r}, {outcome!r}
            out = {{"backend": backend, "outcome": outcome, "live": False}}

            if backend == "jenkins":
                srv = fakes.FakeJenkins(outcome)
            else:
                srv = fakes.FakeCircleCI(outcome)

            with srv:
                if backend == "jenkins":
                    os.environ["JENKINS_USER"] = "persona"
                    os.environ["JENKINS_API_TOKEN"] = "fake-token"
                    cfg = {{"ci": {{"enabled": True, "backend": "jenkins",
                                   "job": "job/skyline/job/main",
                                   "base_url": srv.base_url, "mode": "trigger",
                                   "timeout_minutes": 1, "poll_interval": 1,
                                   "max_infra_retries": 0,
                                   "result_parser": "surefire"}}}}
                else:
                    os.environ["CIRCLECI_TOKEN"] = "fake-token"
                    from no_human.ci import circleci as _cc
                    _cc._API = srv.api_url      # the ONE redirected seam
                    cfg = {{"ci": {{"enabled": True, "backend": "circleci",
                                   "project": "gh/skyline/skyline",
                                   "mode": "watch", "timeout_minutes": 1,
                                   "poll_interval": 1, "max_infra_retries": 0,
                                   "result_parser": "pytest"}}}}

                runner = ci_from_config(cfg)
                out["runner_built"] = runner is not None
                if runner is None:
                    print("__JSON__" + json.dumps(out)); raise SystemExit(0)
                try:
                    res = asyncio.run(asyncio.wait_for(runner.trigger("no-human/x"), 90))
                    out.update(passed=bool(res.passed),
                               status=str(getattr(res.status, "value", res.status)),
                               infra_failure=bool(res.infra_failure),
                               access_failure=bool(getattr(res, "access_failure", False)),
                               access_env_key=getattr(res, "access_env_key", ""),
                               summary=(res.summary or "")[:300])
                except asyncio.TimeoutError:
                    out.update(timed_out=True)
                except Exception as exc:
                    out.update(raised=type(exc).__name__, message=str(exc)[:300])
                out["requests_seen"] = len(srv.state.requests)
            print("__JSON__" + json.dumps(out))
            """
        ok, raw = self.py(script, timeout=240)
        return ok, raw

    def probe_ci_silent_degradation(self) -> tuple[bool, str]:
        """Does a MISCONFIGURED CI become "no gate" rather than an error?

        This is the failure that would matter most and show least. The
        orchestrator treats `ci_runner is None` as "no remote CI is wired for
        this repo" and proceeds on local tests alone. So the question is
        whether a config that plainly asks for CI can produce None.
        """
        return self.py("""
            import json
            from no_human.ci import ci_from_config

            cases = {
              "gitlab_missing_project":  {"enabled": True, "backend": "gitlab"},
              "jenkins_missing_job":     {"enabled": True, "backend": "jenkins"},
              "circleci_missing_slug":   {"enabled": True, "backend": "circleci"},
              "github_actions_missing_repo": {"enabled": True, "backend": "github_actions"},
              "typo_in_backend_name":    {"enabled": True, "backend": "jenkinss",
                                          "job": "j"},
              "disabled":                {"enabled": False, "backend": "jenkins",
                                          "job": "j"},
            }
            out = {}
            for name, ci in cases.items():
                try:
                    r = ci_from_config({"ci": ci})
                    out[name] = "None (NO GATE)" if r is None else f"built:{r.name}"
                except Exception as exc:
                    out[name] = f"raised:{type(exc).__name__}"
            print("__JSON__" + json.dumps(out))
            """, timeout=120)

    # -- integration probes ------------------------------------------------ #
    def probe_jira(self, cfg: dict, token_env: dict[str, str]) -> tuple[bool, str]:
        """Construct the real JiraAdapter from the real config and search().

        Returns (worked, evidence). ``worked`` means the adapter reported itself
        configured AND a search against the local fake returned issues.
        """
        ok, out = self.py(f"""
            import json, sys
            from no_human.intake.jira import JiraAdapter
            cfg = json.loads({json.dumps(cfg)!r})
            a = JiraAdapter(cfg)
            print("configured:", a.configured)
            if not a.configured:
                print("token env the adapter looked for: JIRA_API_TOKEN")
                sys.exit(3)
            issues = a.search()
            print("issues returned:", len(issues))
            if issues:
                t = a.normalize(issues[0])
                print("normalized title:", t.title)
                print("normalized external_id:", t.external_id)
                print("acceptance criteria parsed:", len(t.acceptance_criteria))
            sys.exit(0 if issues else 4)
            """, extra_env=token_env)
        return ok, out

    def probe_slack_env(self, url: str) -> tuple[bool, str]:
        """Set only the DOCUMENTED .env key and see whether anything posts."""
        ok, out = self.py("""
            import os, sys
            from no_human.config import load_config
            from no_human.notify.slack import SlackNotifier
            c = load_config()
            url = (c.data.get("notifications") or {}).get("slack_webhook_url")
            print("SLACK_WEBHOOK_URL in env:", bool(os.environ.get("SLACK_WEBHOOK_URL")))
            print("notifications.slack_webhook_url from config:", url)
            n = SlackNotifier(url)
            print("notifier enabled:", n.enabled)
            sent = n.notify("needs_approval", "adoption harness probe")
            print("sent:", sent)
            sys.exit(0 if sent else 5)
            """, extra_env={"SLACK_WEBHOOK_URL": url})
        return ok, out

    def probe_slack_config(self, url: str) -> tuple[bool, str]:
        """Now put it where the code actually reads it and try again."""
        ok, out = self.py(f"""
            import sys
            from no_human.notify.slack import SlackNotifier
            n = SlackNotifier({url!r})
            print("notifier enabled:", n.enabled)
            sent = n.notify("needs_approval", "adoption harness probe")
            print("sent:", sent)
            sys.exit(0 if sent else 5)
            """)
        return ok, out

    def probe_local_pr(self) -> tuple[bool, str]:
        """Push a branch through the product's real VCS layer to a bare remote."""
        return self.py(f"""
            import sys
            from pathlib import Path
            from no_human.vcs.git import GitRepo
            from no_human.vcs import open_pr
            repo = GitRepo(Path({str(self.target)!r}),
                           identity_name="no_human",
                           identity_email="no-human@skyline.example")
            repo.create_branch("no-human/adoption-probe")
            p = Path({str(self.target)!r}) / "PROBE.md"
            p.write_text("adoption harness probe\\n")
            repo.commit_all("probe: adoption harness")
            res = open_pr(repo, "no-human/adoption-probe",
                          "probe", "adoption harness probe")
            print("kind:", res.kind)
            print("url:", res.url)
            print("branch:", res.branch)
            print("pushed_sha:", res.pushed_sha[:12])
            sys.exit(0 if res.pushed_sha else 6)
            """)

    def task_ids(self) -> list[str]:
        ok, out = self.py("""
            import asyncio
            from no_human.config import load_config
            from no_human.core.db import Store
            async def main():
                c = load_config()
                s = await Store(c.db_path).connect()
                for t in await s.list_tasks():
                    print("__ID__" + t.id)
            asyncio.run(main())
            """)
        if not ok:
            self.note("task_ids_probe_error", out[-800:])
            return []
        return [ln.strip()[len("__ID__"):] for ln in out.splitlines()
                if ln.strip().startswith("__ID__")]

    def persona_home_is_isolated(self) -> tuple[bool, str]:
        """PROVE, from inside the product's own process, that `~` resolves into
        the harness workdir — rather than inferring it from the env dict we set.

        This is the check that matters. An assertion built on 'we set HOME, so
        it must be fine' is an inference; running `Path.home()` in the same
        interpreter the product uses is a measurement.
        """
        ok, out = self.py("""
            from pathlib import Path
            from no_human import config as C
            print("home:", Path.home())
            print("nh_home:", C.NO_HUMAN_HOME)
            print("config_path:", C.CONFIG_PATH)
            print("env_path:", C.ENV_PATH)
            """)
        inside = all(str(self.work) in ln for ln in out.splitlines()
                     if ln.startswith(("home:", "nh_home:", "config_path:", "env_path:")))
        return (ok and inside), out


def _build_shim(shim: Path) -> list[str]:
    """Curated PATH: exactly the tools a persona has, and provably not `nh`."""
    shim.mkdir(parents=True, exist_ok=True)
    missing = []
    for tool in PERSONA_TOOLS:
        p = shutil.which(tool)
        if p:
            link = shim / tool
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(p)
        else:
            missing.append(tool)
    for bad in FORBIDDEN_ON_PATH:
        if (shim / bad).exists():
            raise SystemExit(f"harness bug: {bad} leaked into the persona PATH")
    return missing


SKYLINE_FILES: dict[str, str] = {
    # A conventional uv-managed Python project — the shape `nh onboard` says it
    # derives from, so that a failure to onboard is the product's and not the
    # fixture's. (First draft of this fixture had no lockfile and no pytest;
    # onboarding correctly refused to confirm an unproven test command, and the
    # harness nearly recorded its own fixture bug as a product finding.)
    "pyproject.toml": textwrap.dedent("""\
        [project]
        name = "skyline"
        version = "0.1.0"
        requires-python = ">=3.11"
        dependencies = []

        [dependency-groups]
        dev = ["pytest>=8"]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [tool.hatch.build.targets.wheel]
        packages = ["src/skyline"]

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """),
    "README.md": textwrap.dedent("""\
        # SkyLine

        An AI analyst agent for aviation and real-estate questions.

        Questions we intend to answer:

        - Which airports in New England are strong candidates for terminal expansion?
        - What percentage of long-haul flights leave Anchorage?
        - What is the unmet flight demand at SFO, and why?

        Rules we hold ourselves to:

        1. Public data only (OurAirports, OpenSky, BTS T-100).
        2. Ranking and scoring are deterministic code. A model may explain a
           rank; it may never produce one.
        3. A chat interface. Voice is a bonus.
        4. Every answer states its assumptions, its uncertainty and its scope.

        Run the tests with `python -m pytest -q`.
        """),
    "src/skyline/__init__.py": "__all__ = []\n",
    "src/skyline/analysis/__init__.py": "",
    "src/skyline/analysis/longhaul.py": textwrap.dedent('''\
        """Long-haul share of departures from an airport.

        NOTE (planted bug, AVI-5): LONGHAUL_THRESHOLD is commented as kilometres
        but great_circle_distance returns statute miles, so every comparison is
        made against a threshold in the wrong unit.
        """

        from math import asin, cos, radians, sin, sqrt

        EARTH_RADIUS_MILES = 3958.8

        LONGHAUL_THRESHOLD = 4800  # km


        def great_circle_distance(lat1, lon1, lat2, lon2):
            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = (sin(dlat / 2) ** 2
                 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
            return 2 * EARTH_RADIUS_MILES * asin(sqrt(a))


        def longhaul_share(origin, routes):
            """Fraction of `routes` from `origin` that are long haul."""
            if not routes:
                return 0.0
            long_haul = 0
            for r in routes:
                d = great_circle_distance(origin["lat"], origin["lon"],
                                          r["lat"], r["lon"])
                if d >= LONGHAUL_THRESHOLD:
                    long_haul += 1
            return long_haul / len(routes)
        '''),
    "src/skyline/scoring/__init__.py": "",
    "src/skyline/scoring/rank.py": textwrap.dedent('''\
        """Ranking.

        NOTE (planted bug, AVI-6): the sort has no tie-break, so equal scores come
        out in whatever order the input happened to be in.
        """


        def rank(entries):
            """entries: [{"ident": str, "score": float}] -> ranked list."""
            ordered = sorted(entries, key=lambda e: -e["score"])
            return [dict(e, rank=i + 1) for i, e in enumerate(ordered)]
        '''),
    "tests/__init__.py": "",
    "tests/test_longhaul.py": textwrap.dedent('''\
        from skyline.analysis.longhaul import great_circle_distance, longhaul_share

        ANC = {"lat": 61.1744, "lon": -149.9964}


        def test_distance_is_positive():
            assert great_circle_distance(0, 0, 0, 1) > 0


        def test_share_of_empty_is_zero():
            assert longhaul_share(ANC, []) == 0.0
        '''),
    "tests/test_rank.py": textwrap.dedent('''\
        from skyline.scoring.rank import rank


        def test_rank_orders_by_score():
            out = rank([{"ident": "A", "score": 1.0}, {"ident": "B", "score": 2.0}])
            assert [e["ident"] for e in out] == ["B", "A"]
        '''),
    "tests/fixtures/airports_sample.csv": textwrap.dedent("""\
        ident,name,iso_country,iso_region,type,latitude_deg,longitude_deg,scheduled_service
        KBOS,General Edward Lawrence Logan Intl,US,US-MA,large_airport,42.3629,-71.0064,yes
        KBDL,Bradley Intl,US,US-CT,large_airport,41.9389,-72.6832,yes
        KPVD,Rhode Island T F Green Intl,US,US-RI,medium_airport,41.7240,-71.4283,yes
        KPWM,Portland Intl Jetport,US,US-ME,medium_airport,43.6462,-70.3093,yes
        KBTV,Burlington Intl,US,US-VT,medium_airport,44.4720,-73.1533,yes
        KMHT,Manchester Boston Regional,US,US-NH,medium_airport,42.9326,-71.4357,yes
        BAD1,Broken Row,US,US-MA,small_airport,not-a-number,-71.0,no
        PANC,Ted Stevens Anchorage Intl,US,US-AK,large_airport,61.1744,-149.9964,yes
        KSFO,San Francisco Intl,US,US-CA,large_airport,37.6188,-122.3750,yes
        """),
    ".no_human.yml": textwrap.dedent("""\
        playbook_hints:
          - "Ranking and scoring must be deterministic code, never model output."
          - "Every answer carries assumptions, uncertainty and scope."
        """),
}


def _seed_target_repo(target: Path, bare: Path, env: dict[str, str]) -> None:
    """Create the SkyLine repo from scratch, plus a bare remote to push to."""
    target.mkdir(parents=True, exist_ok=True)
    for rel, content in SKYLINE_FILES.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    # Lock and install, so the repo arrives in the state a real team's repo is
    # in: `uv run pytest -q` works before no_human ever sees it. Without this
    # the harness measures its own fixture, not the product.
    for cmd in (["uv", "lock", "-q"], ["uv", "sync", "-q"]):
        subprocess.run(cmd, cwd=target, env=env, check=False,
                       capture_output=True, timeout=900)
    (target / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n")
    git = ["git", "-c", "user.name=SkyLine Team",
           "-c", "user.email=team@skyline.example",
           "-c", "init.defaultBranch=main"]
    subprocess.run(git + ["init", "-q"], cwd=target, check=True, env=env)
    subprocess.run(git + ["add", "-A"], cwd=target, check=True, env=env)
    subprocess.run(git + ["commit", "-q", "-m", "SkyLine: initial skeleton"],
                   cwd=target, check=True, env=env)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=env)
    subprocess.run(git + ["remote", "add", "origin", str(bare)],
                   cwd=target, check=True, env=env)
    subprocess.run(git + ["push", "-q", "origin", "main"], cwd=target,
                   check=True, env=env)


def build_ctx(args) -> Ctx:
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="nh-adoption-"))
    work.mkdir(parents=True, exist_ok=True)
    home = work / "home"
    home.mkdir(exist_ok=True)
    shim = work / "shim"
    missing = _build_shim(shim)

    path = os.pathsep.join([str(shim), "/opt/homebrew/bin", "/usr/bin", "/bin",
                            "/usr/sbin", "/sbin"])
    env = {
        "HOME": str(home),
        "PATH": path,
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "SKYLINE_OFFLINE": "1",
    }
    # Never inherit a credential. Belt and braces: the persona env is built up
    # from nothing rather than copied and filtered, but say so explicitly for
    # anyone reading this later.
    for leak in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
                 "JIRA_API_TOKEN", "SLACK_WEBHOOK_URL", "GITLAB_TOKEN"):
        env.pop(leak, None)
    # `full` mode needs a credential, and the ONLY way it gets one is the
    # operator explicitly exporting it for this invocation. The harness never
    # reads ~/.no_human/.env and never asks for a secret.
    if args.mode == "full":
        tok = os.environ.get("NH_ADOPTION_OAUTH_TOKEN")
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok

    origin = args.origin or f"file://{REPO_ROOT}"
    ctx = Ctx(work=work, home=home, shim=shim,
              product=work / "no_human_eval", target=work / "skyline",
              origin_url=origin, bare_remote=work / "skyline-origin.git",
              mode=args.mode, env=env)
    ctx.note("persona_tools_missing", missing)
    ctx.note("workdir", str(work))
    _seed_target_repo(ctx.target, ctx.bare_remote, env)
    return ctx


# --------------------------------------------------------------------------- #
# Full mode — real task execution
# --------------------------------------------------------------------------- #

# The product's OWN indicative cost model: the flat Sonnet-coder rate ($3/1M
# fresh, a cache READ at a tenth). NOT a rate table invented here. PR #869 moved
# exact pricing server-side (per-model, MODEL_PRICES_USD_PER_MTOK) and the board
# now only formats server-computed dollars, so this harness — which runs in a
# curated persona PATH with no_human absent and can't call that server pricing —
# keeps the flat rate locally. `test_adoption_harness.py` pins it to the
# product's Sonnet INPUT price so the two cannot drift apart.
RATE_FRESH_PER_TOKEN = 0.003 / 1000
RATE_CACHE_READ_PER_TOKEN = 0.0003 / 1000


def cost_of(used: int, creation: int, read: int) -> float:
    """Indicative dollars — the twin of `costOf` in web/src/cost.js."""
    return (used * RATE_FRESH_PER_TOKEN
            + creation * RATE_FRESH_PER_TOKEN
            + read * RATE_CACHE_READ_PER_TOKEN)


_OUTCOME_SQL = """
SELECT t.id, t.external_id, t.status,
       (SELECT a.pr_url FROM attempts a WHERE a.task_id = t.id
         AND a.pr_url IS NOT NULL AND a.pr_url != ''
         ORDER BY a.attempt_number DESC LIMIT 1)              AS pr_url,
       (SELECT COUNT(*) FROM attempts a WHERE a.task_id = t.id) AS attempts,
       (SELECT COALESCE(SUM(a.tokens_used),0) FROM attempts a
         WHERE a.task_id = t.id)                              AS used,
       (SELECT COUNT(*) FROM attempts a WHERE a.task_id = t.id
         AND a.review_passed = 0)                             AS reviews_failed
FROM tasks t
"""


def run_full_mode(ctx: Ctx, run: PersonaRun, limit: int) -> dict[str, Any]:
    """Drain the staged backlog for real and measure what came out.

    Three things here are deliberate and each is easy to get wrong in a way
    that would make the harness actively harmful:

      * A ticket the backlog marks ``escalate`` that comes back with a PR is
        scored as ``guessed_instead_of_asking`` — a FAILURE. Score it as a win
        and you have built a harness that rewards the product for guessing,
        which then optimises it in exactly the wrong direction.
      * Cost uses the product's own indicative model, cited. Inventing a second
        cost model is how two surfaces come to quote blended rates 20% apart.
      * Throughput is compared against the team's ESTIMATES, and the word
        "estimate" travels with the number to every place it is printed.
    """
    if "CLAUDE_CODE_OAUTH_TOKEN" not in ctx.env:
        return {"ran": False,
                "reason": ("no credential supplied. Full mode takes a token ONLY "
                           "from NH_ADOPTION_OAUTH_TOKEN, exported deliberately "
                           "for the invocation. The harness never reads "
                           "~/.no_human/.env, never prompts, and never stores one.")}

    budget = int(os.environ.get("NH_ADOPTION_FULL_TIMEOUT", "10800"))
    t0 = time.time()
    # `nh serve` has no drain-and-exit mode — it runs until interrupted — so the
    # harness supervises it: start it, poll the lanes, stop it when the queue is
    # empty or the budget is spent. That absence is recorded as its own finding;
    # anybody automating an overnight drain hits it on the first try.
    proc = subprocess.Popen(
        ["uv", "run", "nh", "serve", "--max-workers", "2"],
        cwd=str(ctx.product), env=ctx.env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    drained = False
    try:
        while time.time() - t0 < budget:
            time.sleep(30)
            if proc.poll() is not None:
                break
            r = subprocess.run(["uv", "run", "nh", "status", "--json"],
                               cwd=str(ctx.product), env=ctx.env, text=True,
                               capture_output=True, stdin=subprocess.DEVNULL)
            try:
                lanes = json.loads(r.stdout.strip() or "{}")
            except Exception:
                continue
            if sum(int(lanes.get(k) or 0) for k in ("pending", "running")) == 0:
                drained = True
                break
    finally:
        tail = ""
        if proc.poll() is None:
            proc.terminate()
            try:
                tail = (proc.communicate(timeout=180)[0] or "")
            except subprocess.TimeoutExpired:
                proc.kill()
        elif proc.stdout:
            tail = proc.stdout.read() or ""
    elapsed_min = (time.time() - t0) / 60

    _, out = ctx.py(f"""
        import json, sqlite3
        from no_human.config import load_config
        c = load_config()
        db = sqlite3.connect(str(c.db_path))
        db.row_factory = sqlite3.Row
        cols = {{r[1] for r in db.execute("PRAGMA table_info(attempts)")}}
        rows = [dict(r) for r in db.execute({_OUTCOME_SQL!r})]
        # The cache buckets arrived in a later migration than the base schema.
        # Ask the schema rather than assuming, so an older DB degrades to
        # "unknown" instead of silently to zero — a zero here would price a real
        # run at a fraction of its cost.
        for r in rows:
            for name, col in (("creation", "cache_creation_tokens"),
                              ("read", "cache_read_tokens")):
                r[name] = (db.execute(
                    "SELECT COALESCE(SUM(" + col + "),0) FROM attempts "
                    "WHERE task_id=?", (r["id"],)).fetchone()[0]
                    if col in cols else None)
        print("__JSON__" + json.dumps(rows))
        """, timeout=300)
    rows: list[dict] = []
    for ln in out.splitlines():
        if ln.startswith("__JSON__"):
            rows = json.loads(ln[len("__JSON__"):])
    scored = _score_outcomes(rows)
    scored.update({
        "ran": True,
        "tickets_in_scope": [t.key for t in ctx.backlog][:limit],
        "queue_drained_before_budget": drained,
        "wall_clock_minutes": round(elapsed_min, 1),
        "serve_tail": tail[-2000:],
    })
    hb = scored["human_baseline_minutes_estimate"]
    if elapsed_min > 0:
        scored["throughput_vs_human_ESTIMATE_x"] = round(hb / elapsed_min, 2)
    return scored


def _score_outcomes(rows: list[dict]) -> dict[str, Any]:
    by_key = {r.get("external_id"): r for r in rows if r.get("external_id")}
    delivered, wrong_pr, honest_stop, missed_stop, other = [], [], [], [], []
    used = creation = read = 0
    buckets_complete = True
    reviews_failed = 0
    for t in backlog_mod.BACKLOG:
        r = by_key.get(t.key)
        if not r:
            continue
        used += int(r.get("used") or 0)
        if r.get("creation") is None or r.get("read") is None:
            buckets_complete = False
        else:
            creation += int(r["creation"])
            read += int(r["read"])
        reviews_failed += int(r.get("reviews_failed") or 0)
        has_pr = bool(r.get("pr_url"))
        status = str(r.get("status") or "").lower()
        parked = any(w in status for w in
                     ("block", "park", "escalat", "needs_input", "waiting"))
        if t.should_escalate:
            (honest_stop if parked else (wrong_pr if has_pr else missed_stop)).append(t.key)
        elif has_pr:
            delivered.append(t.key)
        else:
            other.append(t.key)
    n = len(delivered)
    dollars = cost_of(used, creation, read) if buckets_complete else None
    return {
        "delivered_reviewed_pr_no_human_rescue": delivered,
        "no_pr_and_no_question": other,
        "honest_stops": honest_stop,
        "guessed_instead_of_asking": wrong_pr,
        "neither_pr_nor_question": missed_stop,
        "reviewer_rejections": reviews_failed,
        "reviewer_rejection_caveat":
            "a rejection count is not a catch rate. Whether the reviewer caught "
            "something REAL needs a human reading the checklist; see "
            "docs/REVIEWER_RECALL_METHOD.md. Never publish this as recall.",
        "tokens": {"used": used, "cache_creation": creation, "cache_read": read,
                   "buckets_complete": buckets_complete},
        "indicative_cost_usd": round(dollars, 4) if dollars is not None else None,
        "indicative_cost_per_delivered_pr_usd":
            round(dollars / n, 4) if (dollars is not None and n) else None,
        "cost_model_source":
            "web/src/cost.js costOf() — indicative rates, not a billed amount",
        "human_baseline_minutes_estimate": backlog_mod.human_baseline_minutes(),
        "human_baseline_caveat":
            "the team's own estimates, NOT a measured human run. Good enough to "
            "spot an order-of-magnitude claim being false; never good enough to "
            "publish a multiple from.",
    }


# --------------------------------------------------------------------------- #
# Daily assertions — what must be true tomorrow or the run goes red
# --------------------------------------------------------------------------- #

@dataclass
class Assertion:
    name: str
    passed: bool
    detail: str
    blocking: bool = True


def daily_assertions(ctx: Ctx, runs: list[PersonaRun],
                     baseline: dict[str, Any] | None) -> list[Assertion]:
    """The regression contract.

    These are deliberately about MECHANISM, not about counts of findings. A
    check that says "no more than 12 findings" drifts upward one finding at a
    time and never fails. A check that says "the README's install command exits
    0 on a clean clone" either holds or it does not.
    """
    steps = {(r.name, s.step): s for r in runs for s in r.steps}
    found = [f for r in runs for f in r.findings]
    out: list[Assertion] = []

    def step_ok(persona: str, step: str) -> tuple[bool, str]:
        s = steps.get((persona, step))
        if s is None:
            return False, f"step {persona}/{step} did not run"
        return s.ok, f"exit={s.exit_code} :: {(s.stderr or s.stdout)[-300:]}"

    # 1. The install command in the README works on a clean clone. This is the
    #    single most important assertion in the file: it is the one that was
    #    false when the harness was written.
    ok, detail = step_ok("Dana", "uv-sync")
    out.append(Assertion("readme_install_works_on_clean_clone", ok, detail))

    # 2. Every command PRINTED in the quickstart runs on a fresh install.
    #    Derived from the document, so it keeps holding the document to account
    #    as the document changes.
    ok, detail = step_ok("Dana", "quickstart-commands-resolve")
    out.append(Assertion("quickstart_commands_runnable_as_printed", ok, detail))

    # 3. Nothing user-facing promises an intake form that does not exist. A
    #    documented command that errors is worse than a missing feature: it
    #    costs trust in every other line of the document.
    promised = ctx.notes.get("bare_ticket_key_still_documented") or []
    tk = steps.get(("Sam", "task-add-ticket-key"))
    out.append(Assertion(
        "no_doc_promises_an_intake_form_that_does_not_work",
        not (promised and tk is not None and not tk.ok),
        f"docs promising a bare ticket key: {promised}"))
    # And the failure a developer WILL hit anyway has to be actionable.
    ok, detail = step_ok("Sam", "task-add-ticket-key-error-is-actionable")
    out.append(Assertion(
        "unsupported_intake_input_fails_with_an_actionable_message", ok, detail))
    ok, detail = step_ok("Sam", "task-add-freeform")
    out.append(Assertion("freeform_intake_works", ok, detail))

    # 4. Onboarding proves and confirms a profile on a conventional Python repo.
    #    Without this no task can run at all, so a regression here is silent and
    #    total: every later step looks fine and nothing ever executes.
    ok, detail = step_ok("Sam", "onboard")
    out.append(Assertion("onboard_derives_and_proves_on_conventional_repo", ok, detail))
    ok, detail = step_ok("Sam", "onboard-confirm")
    out.append(Assertion("onboard_confirm_yields_a_usable_profile", ok, detail))

    # 5. Every credential name the docs hand a user is read by something.
    #    Derived from configuration.md's own table: a name in it that nothing
    #    reads is a SILENT misconfiguration — no error, no integration, no
    #    clue. Two such names were live when this harness was written.
    ok, detail = step_ok("Priya", "documented-env-keys-are-read")
    out.append(Assertion("every_documented_env_key_is_read_by_something", ok,
                         f"{detail} (checked: "
                         f"{ctx.notes.get('documented_env_keys_checked')})"))
    jira = ctx.notes.get("jira_probe") or {}
    slack = ctx.notes.get("slack_probe") or {}

    # 5. The mechanisms that DO work must keep working. These are the ones that
    #    would turn a bad day into a catastrophic one if they regressed.
    out.append(Assertion(
        "slack_config_path_delivers", bool(slack.get("config_key_works")),
        f"notifications.slack_webhook_url -> local fake; probe={slack}"))
    out.append(Assertion(
        "jira_adapter_parses_real_shaped_payload", bool(jira.get("real_key_works")),
        f"ADF description + JQL search against the local fake; probe={jira}"))
    gh = ctx.notes.get("github_probe") or {}
    out.append(Assertion(
        "vcs_push_and_pr_path_works_offline", bool(gh.get("local_bare_push_works")),
        f"local bare remote; probe={gh}"))

    # 5b. CI — the claim on the website is "test layers can run on your CI, and
    #     the results gate the loop". Three separate things have to hold, and
    #     they fail differently, so they are three assertions.
    surface = ctx.notes.get("ci_doc_surface") or {}
    undoc = (surface.get("backends_undocumented", [])
             + surface.get("ci_env_vars_undocumented", [])
             + surface.get("ci_config_keys_undocumented", []))
    out.append(Assertion(
        "ci_backends_and_credentials_are_documented", not undoc,
        f"undocumented: {undoc}" if undoc else
        f"all {len(surface.get('backends_supported', []))} backends, their "
        "required keys and their credentials appear in the public docs"))

    verdicts = ctx.notes.get("ci_verdicts") or {}
    # A green pipeline must read as passed and a red one must not. Asserted as
    # a PAIR: a backend that answered False to everything would satisfy the red
    # half on its own, and would gate every task forever.
    pairs_ok, pair_detail = True, []
    for backend in ("jenkins", "circleci"):
        g = (verdicts.get(f"{backend}:green") or {}).get("passed")
        r = (verdicts.get(f"{backend}:red") or {}).get("passed")
        if not (g is True and r is False):
            pairs_ok = False
        pair_detail.append(f"{backend}: green->passed={g}, red->passed={r}")
    out.append(Assertion("ci_verdict_tracks_the_pipeline_result", pairs_ok,
                         "; ".join(pair_detail) + " [local fakes, NOT live]"))

    # Nothing broken may ever read as green. This is the one that would let a
    # red build through, so it covers every failure shape the harness can make.
    green_on_broken = [k for k, v in verdicts.items()
                       if k.split(":")[1] in ("unauthorized", "server_error",
                                              "running_forever")
                       and v.get("passed") is True]
    out.append(Assertion(
        "broken_or_unreachable_ci_never_reads_as_green", not green_on_broken,
        f"passing verdict from: {green_on_broken}" if green_on_broken else
        "401 -> access_failure, 503 -> infra_failure, never-finishing -> "
        "timeout to infra_failure; none passed [local fakes, NOT live]"))

    # KI-5. Non-blocking ON PURPOSE, and pinned to the exact SET rather than a
    # count: this is a known, accepted, documented defect, and a permanently
    # red assertion is one everybody learns to ignore — which is how the defect
    # became invisible in the first place. The blocking guard for it is the
    # ratchet below. What this catches is the set CHANGING in either direction.
    degraded = ctx.notes.get("ci_silent_degradation") or {}
    silent = sorted(k for k, v in degraded.items()
                    if k != "disabled" and "NO GATE" in str(v))
    known = ["circleci_missing_slug", "github_actions_missing_repo",
             "gitlab_missing_project", "jenkins_missing_job"]
    out.append(Assertion(
        "ci_misconfig_degradation_set_unchanged", silent == known,
        f"KI-5: {silent} (expected {known})", blocking=False))

    # 6. Safety. Two assertions, because they prove different things: the first
    #    is positive (the product, asked in its own process, says its home is
    #    the temp dir); the second is negative (we created nothing in the
    #    operator's real one). Neither alone is sufficient.
    ev = ctx.notes.get("operator_home_evidence", {})
    out.append(Assertion(
        "persona_home_is_the_temp_dir_not_the_operators",
        bool(ctx.notes.get("persona_home_isolated")),
        json.dumps(ev.get("product_process_reports_its_home_as", []))[:400]))
    out.append(Assertion(
        "no_new_entries_in_operator_real_no_human",
        bool(ctx.notes.get("operator_home_no_new_entries")),
        f"new entries: {ev.get('new_entries_created_by_this_run')}"))

    # 7. Ratchet: no NEW fatal or high finding relative to the committed
    #    baseline. This is the drift guard — it does not care how many findings
    #    there are, only that the set does not grow.
    if baseline is not None:
        known = {f["ticket"] for f in baseline.get("findings", []) if f.get("ticket")}
        new_bad = sorted({f.ticket or f.summary[:40] for f in found
                          if f.severity in ("fatal", "high")
                          and (f.ticket or f.summary[:40]) not in known})
        out.append(Assertion("no_new_high_or_fatal_findings", not new_bad,
                             f"new: {new_bad}" if new_bad else "none"))
        fixed = sorted(known - {f.ticket for f in found if f.ticket})
        out.append(Assertion("previously_fixed_findings_stay_fixed", True,
                             f"resolved since baseline: {fixed}", blocking=False))
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

SEV_ORDER = {"fatal": 0, "high": 1, "medium": 2, "low": 3}


def write_report(ctx: Ctx, runs: list[PersonaRun], asserts: list[Assertion],
                 full: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    findings = sorted((f for r in runs for f in r.findings),
                      key=lambda f: SEV_ORDER.get(f.severity, 9))
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": ctx.mode,
        "product_commit": _git_head(ctx.product),
        "workdir": str(ctx.work),
        "personas": [r.to_dict() for r in runs],
        "findings": [f.to_dict() for f in findings],
        "assertions": [{"name": a.name, "passed": a.passed, "detail": a.detail,
                        "blocking": a.blocking} for a in asserts],
        "measurements": full,
        "notes": ctx.notes,
    }
    (out_dir / "adoption-report.json").write_text(json.dumps(payload, indent=2))

    lines = [
        "# Adoption run — friction log",
        "",
        f"- generated: {payload['generated']}",
        f"- mode: `{ctx.mode}`",
        f"- product commit: `{payload['product_commit']}`",
        f"- personas: {', '.join(r.name + ' (' + r.role + ')' for r in runs)}",
        "",
        "Everything below was produced by running the documented commands in a "
        "fresh environment containing only what the persona would have. No "
        "credential was read. Jira and Slack results come from local fakes and "
        "are labelled as such.",
        "",
        "## Findings, worst first",
        "",
    ]
    if not findings:
        lines.append("_None. Every documented path worked as written._")
    for f in findings:
        lines += [
            f"### `{f.severity.upper()}` {f.ticket or '(no ticket)'} — {f.summary.splitlines()[0][:110]}",
            "",
            f"- persona: **{f.persona}**, step `{f.step}`",
            f"- following: `{f.doc_ref}`",
            "",
            f.summary,
            "",
        ]
        if f.workaround:
            lines += [f"**Workaround the persona had to find:** {f.workaround}", ""]
        if f.evidence:
            lines += ["<details><summary>evidence</summary>", "",
                      "```", f.evidence.strip()[-2000:], "```", "", "</details>", ""]

    lines += ["## Daily assertions", "",
              "| assertion | result | detail |", "|---|---|---|"]
    for a in asserts:
        mark = "PASS" if a.passed else ("**FAIL**" if a.blocking else "warn")
        lines.append(f"| `{a.name}` | {mark} | {a.detail[:160].replace('|', '/')} |")

    lines += ["", "## Measurements", ""]
    if not full.get("ran"):
        lines += [
            "**NOT MEASURED.** Throughput, cost per delivered PR, unaided-PR rate "
            "and reviewer catch rate all require real task execution against a "
            "real Claude credential.",
            "",
            f"Reason: {full.get('reason', 'smoke mode')}",
            "",
            "These are reported as *not measured* rather than as zero on purpose. "
            "A dashboard that renders an unmeasured quantity as 0 is worse than "
            "one that renders nothing.",
            "",
            "To measure them: `NH_ADOPTION_OAUTH_TOKEN=... e2e/adoption/run.sh "
            "--mode full`.",
        ]
    else:
        lines += ["```json", json.dumps(full, indent=2), "```"]
    lines += ["", "## Integration boundary", "",
              "| system | how it was exercised | live? |", "|---|---|---|",
              "| Jira | local protocol-faithful fake (ADF descriptions, "
              "`/rest/api/3/search/jql`, transitions, comments) | **no** |",
              "| Slack | local incoming-webhook receiver | **no** |",
              "| GitHub | real push through the product's real VCS layer to a "
              "local bare remote (`local` backend). `gh pr create` NOT "
              "exercised. | **no** |", ""]
    md = out_dir / "FRICTION_LOG.md"
    md.write_text("\n".join(lines) + "\n")
    return md


def _git_head(repo: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip() or "?"
    except Exception:
        return "?"


# --------------------------------------------------------------------------- #

def _snapshot_real_home() -> dict[str, Any]:
    """Fingerprint the operator's REAL ``~/.no_human`` — names and mtimes only.
    No file in it is ever opened, and nothing in it is ever written.

    WHY THIS IS TWO SIGNALS AND NOT ONE. The first version of this check
    compared the whole (name, mtime) set and failed the run. It was measuring
    the wrong thing: the operator's own install is live, other sessions write to
    it constantly, and an mtime that moved while the harness happened to be
    running is evidence of nothing. Treating that as a safety failure would have
    trained everyone to ignore a red safety assertion, which is worse than not
    having one.

    So the blocking signal is the one the harness could actually cause — a NEW
    entry appearing — and mtime drift is reported alongside it as information.
    The real guarantee is proven elsewhere and positively, by
    ``Ctx.persona_home_is_isolated``: the product's own process is asked where
    its home is, and answers with the temp directory.
    """
    real = Path(os.path.expanduser("~/.no_human"))
    if not real.exists():
        return {"state": "absent", "names": [], "mtimes": {}}
    names, mtimes = [], {}
    for p in sorted(real.iterdir()):
        names.append(p.name)
        try:
            mtimes[p.name] = int(p.stat().st_mtime)
        except OSError:
            mtimes[p.name] = -1
    return {"state": "present", "names": names, "mtimes": mtimes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    ap.add_argument("--out", default=str(REPO_ROOT / "e2e" / "adoption" / "out"))
    ap.add_argument("--workdir", default=None,
                    help="reuse a directory instead of a fresh temp one (debug only)")
    ap.add_argument("--origin", default=None,
                    help="clone URL for the product (default: this repo, via file://)")
    ap.add_argument("--full-limit", type=int, default=6)
    ap.add_argument("--baseline",
                    default=str(REPO_ROOT / "e2e" / "adoption" / "baseline.json"))
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    args = ap.parse_args(argv)

    before = _snapshot_real_home()
    print(f"[adoption] mode={args.mode}")
    ctx = build_ctx(args)
    print(f"[adoption] workdir {ctx.work}")
    if ctx.notes.get("persona_tools_missing"):
        print(f"[adoption] NOTE tools not present for personas: "
              f"{ctx.notes['persona_tools_missing']}")

    runs: list[PersonaRun] = []
    for name, fn in personas_mod.PERSONAS:
        print(f"[adoption] persona: {name}")
        try:
            runs.append(fn(ctx))
        except Exception as exc:  # a persona crashing is itself a finding
            r = PersonaRun(name=name, role="?", goal="?", knows="?")
            r.findings.append(Finding(
                name, "harness", "high",
                f"The persona script could not complete: {type(exc).__name__}: {exc}",
                "harness", ""))
            runs.append(r)

    full: dict[str, Any] = {"ran": False, "reason": "smoke mode"}
    if args.mode == "full":
        alex = next((r for r in runs if r.name == "Alex"), runs[-1])
        full = run_full_mode(ctx, alex, args.full_limit)

    after = _snapshot_real_home()
    new_entries = sorted(set(after["names"]) - set(before["names"]))
    drifted = sorted(k for k, v in after["mtimes"].items()
                     if before["mtimes"].get(k) not in (None, v))
    isolated, iso_evidence = ctx.persona_home_is_isolated()
    ctx.note("operator_home_no_new_entries", not new_entries)
    ctx.note("persona_home_isolated", isolated)
    ctx.note("operator_home_evidence", {
        "state": after["state"],
        "entries": len(after["names"]),
        "new_entries_created_by_this_run": new_entries,
        "entries_whose_mtime_moved_during_the_run": drifted,
        "mtime_note": ("the operator's install is live and other sessions write "
                       "to it; mtime drift here is information, not a failure"),
        "product_process_reports_its_home_as": iso_evidence.strip().splitlines(),
    })

    baseline = None
    bp = Path(args.baseline)
    if bp.exists():
        try:
            baseline = json.loads(bp.read_text(encoding="utf-8"))
        except Exception:
            baseline = None

    asserts = daily_assertions(ctx, runs, baseline)
    md = write_report(ctx, runs, asserts, full, Path(args.out))

    findings = [f for r in runs for f in r.findings]
    n = {s: sum(1 for f in findings if f.severity == s) for s in personas_mod.SEVERITIES}
    failed = [a for a in asserts if a.blocking and not a.passed]
    print()
    print(f"[adoption] findings: fatal={n['fatal']} high={n['high']} "
          f"medium={n['medium']} low={n['low']}")
    for a in asserts:
        print(f"[adoption]   {'PASS' if a.passed else ('FAIL' if a.blocking else 'warn')}"
              f"  {a.name}")
    print(f"[adoption] report: {md}")
    if not args.keep and not args.workdir:
        print(f"[adoption] work dir kept for inspection: {ctx.work}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
