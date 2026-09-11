"""The four people at the startup, and exactly what each of them does.

The whole value of this harness is that these scripts are written from the
persona's side of the wall. A persona knows the PUBLIC DOCS and nothing else.
They do not know which flag was renamed last week, they do not know that a
directory has to exist before a build works, and they cannot be told. When a
step fails, the harness records the failure and — where a real person could
plausibly have found the workaround themselves — continues with the workaround
recorded as friction. It never quietly does the right thing on their behalf.

Two rules keep this honest and they are worth stating because both are easy to
break by accident when you are the one who wrote the product:

  1. Every step carries ``doc_ref`` — the document and section the persona is
     following. If a step has no doc_ref, the persona had no way to know to do
     it, and that is itself a finding (``UNDOCUMENTED``).
  2. A step never uses knowledge from outside ``doc_ref``. The moment the
     harness reaches past the docs to make something work, it stops measuring
     adoption and starts measuring the author's memory.

SEVERITY, and what each level means for going public
----------------------------------------------------
  fatal    the persona cannot proceed at all, and no amount of re-reading the
           public docs would tell them why. Ships as a support ticket on day one.
  high     the persona is blocked on a documented path but can recover if they
           are stubborn and technical. Costs trust and an hour.
  medium   the docs are wrong or stale; the product still works. Costs
           confidence in the rest of the docs.
  low      cosmetic, confusing, or merely unhelpful.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #

SEVERITIES = ("fatal", "high", "medium", "low")


@dataclass
class Finding:
    persona: str
    step: str
    severity: str
    summary: str
    doc_ref: str
    evidence: str = ""
    workaround: str = ""
    ticket: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona, "step": self.step, "severity": self.severity,
            "summary": self.summary, "doc_ref": self.doc_ref,
            "evidence": self.evidence[-4000:], "workaround": self.workaround,
            "ticket": self.ticket,
        }


@dataclass
class StepResult:
    persona: str
    step: str
    intent: str
    doc_ref: str
    command: str
    exit_code: int | None
    ok: bool
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona, "step": self.step, "intent": self.intent,
            "doc_ref": self.doc_ref, "command": self.command,
            "exit_code": self.exit_code, "ok": self.ok,
            "stdout": self.stdout[-4000:], "stderr": self.stderr[-4000:],
            "seconds": round(self.seconds, 2),
        }


@dataclass
class PersonaRun:
    name: str
    role: str
    goal: str
    knows: str
    steps: list[StepResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "role": self.role, "goal": self.goal,
            "knows": self.knows,
            "steps": [s.to_dict() for s in self.steps],
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Persona A — Dana, CTO. "Can I get this running before standup?"
# --------------------------------------------------------------------------- #

DANA = dict(
    name="Dana",
    role="CTO",
    goal=(
        "Evaluate no_human in one sitting. If it is not running in twenty "
        "minutes I will close the tab and we will keep doing code review by hand."
    ),
    knows=(
        "The README and docs/quickstart.md. Has a Mac with homebrew, git, node "
        "and a Claude subscription. Has never seen this codebase. Will not read "
        "source to make an install work."
    ),
)

_NH_INIT_PLACEHOLDER_TOKEN = "sk-ant-oat01-ADOPTION-HARNESS-PLACEHOLDER"


def _nh_init_scriptable(ctx, run: PersonaRun, product: Path) -> None:
    """Run the install path `nh init --help`/docs/quickstart.md document for
    scripting, and raise a finding only if THAT fails — never for plain
    `nh init` needing a terminal, which it always will.

    Two things a naive version of this check gets wrong, both worth stating:

      * Running plain `nh init` with stdin closed and reading a clean abort as
        "there is no non-interactive mode" tests a form nobody who read the
        docs would run: `nh init --help` documents `--non-interactive
        --token-stdin --no-repo` for exactly this. That plain form is still
        exercised below (a real no-TTY environment should get a clean abort,
        not a hang or a traceback) but a clean abort there is CORRECT and is
        never, by itself, a finding.
      * In `full` mode the persona's own ``ctx.env`` carries a real credential
        (the one deliberate exception the harness makes, so full-mode task
        runs can actually call the model) — and `nh init` correctly REFUSES a
        stdin token that differs from one already present. Feeding it that
        real credential and reading the refusal as "scripting is broken"
        reproduces the exact false-finding shape this check exists to catch.
        The scripted install therefore runs against a copy of the environment
        with that credential stripped, matching the machine the docs describe:
        one with nothing on it yet.
    """
    init_env = {k: v for k, v in ctx.env.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    r = ctx.shell(
        run, "nh-init-scriptable",
        "Script the setup so the rest of the team can repeat it, following "
        "the non-interactive example from `nh init --help`.",
        "nh init --help; docs/quickstart.md",
        "uv run nh init --non-interactive --token-stdin --no-repo",
        cwd=product, env=init_env, input=_NH_INIT_PLACEHOLDER_TOKEN + "\n",
        allow_fail=True)
    if not r.ok:
        run.findings.append(Finding(
            run.name, "nh-init-scriptable", "high",
            ("The scripted install documented by `nh init --help` and "
             "docs/quickstart.md — `nh init --non-interactive --token-stdin "
             f"--no-repo` with a token piped on stdin — fails (exit "
             f"{r.exit_code}): {(r.stderr or r.stdout).strip()[-500:]}"),
            "nh init --help; docs/quickstart.md", (r.stdout + r.stderr)[-1500:],
            ticket="ADOPT-3"))
    else:
        # Proved it works; undo its writes so the rest of this run — and
        # `seed_config()` right after — still see the one known-good
        # ~/.no_human the other personas are written against, rather than
        # whatever defaults `nh init --no-repo` happens to pick today.
        (ctx.home / ".no_human" / "config.yaml").unlink(missing_ok=True)
        (ctx.home / ".no_human" / ".env").unlink(missing_ok=True)

    # Plain `nh init`, stdin closed: what a real no-TTY environment gets. A
    # clean abort here is CORRECT — the interactive wizard has no terminal to
    # prompt on — and must not, by itself, produce a finding; the scripted
    # form above is the one the docs actually recommend for this case.
    ctx.shell(run, "nh-init-interactive-no-tty",
              "Try the bare command first, the way the README shows it.",
              "README.md#install", "uv run nh init", cwd=product,
              stdin_devnull=True, allow_fail=True)


def _doctor_is_documented(ctx, run: PersonaRun, product: Path) -> None:
    """Run `nh doctor`, then check the "is it documented" claim by READING
    README.md and docs/quickstart.md, rather than asserting it.

    ADOPT-4 (2026-08-02, then re-litigated twice more): the original version
    raised a finding whenever `nh doctor` merely ran successfully, hardcoding
    the belief that it is "named in neither the README nor the quickstart".
    The check never read either file, so the finding fired on every run where
    the command worked — which is every run — and stayed wrong long after
    both docs were fixed to mention it.

    A non-zero exit is a different defect (the product is broken) from being
    undocumented (the docs don't mention a working command); this only asks
    the documentation question once the command has proven it works, and the
    exit itself is already fully visible via the `StepResult` `ctx.shell`
    records regardless of outcome.
    """
    r = ctx.shell(run, "doctor", "Check it is actually alive before trusting it.",
                  "README.md#install; docs/quickstart.md", "uv run nh doctor",
                  cwd=product, allow_fail=True)
    if not r.ok:
        return
    search = ctx.onboarding_docs_mentioning(product, "nh doctor")
    if search.missing_from:
        run.findings.append(Finding(
            run.name, "doctor", "low",
            ("`nh doctor` is the one command that tells a new user whether "
             "the install is real, and it is not named in "
             f"{', '.join(search.missing_from)} — the document(s) the public "
             "onboarding path sends a new user to."),
            "README.md#install; docs/quickstart.md", search.evidence(),
            ticket="ADOPT-4"))


def persona_dana(ctx) -> PersonaRun:
    """The first-install walk, following README 'Install' then quickstart §1-3."""
    run = PersonaRun(**DANA)
    sh = ctx.shell

    # -- README: "git clone ... && cd no_human" ---------------------------- #
    r = sh(run, "clone", "Get the code.",
           "README.md#install",
           f"git clone {ctx.origin_url} no_human_eval",
           cwd=ctx.work)
    if not r.ok:
        run.findings.append(Finding(
            run.name, "clone", "fatal", "The documented clone command fails.",
            "README.md#install", r.stderr))
        return run
    product = ctx.work / "no_human_eval"

    # -- README: "uv sync  # installs the `nh` entry point into .venv" ----- #
    r = sh(run, "uv-sync", "Install it, per the README's second line.",
           "README.md#install", "uv sync", cwd=product)
    if not r.ok:
        blocked_on_web_dist = "web/dist" in (r.stderr + r.stdout)
        run.findings.append(Finding(
            run.name, "uv-sync",
            "fatal",
            ("`uv sync` — the README's install command — fails on a clean clone. "
             "The project force-includes `web/dist` into the wheel, `web/dist` is "
             "gitignored and therefore absent from every clone, and `uv sync` "
             "builds an editable wheel, so the force-include fires. No new user "
             "can install no_human by following the README."
             if blocked_on_web_dist else
             "`uv sync` fails on a clean clone."),
            "README.md#install", (r.stdout + r.stderr),
            workaround=("Dana would have to find, unaided, that `cd web && npm "
                        "install && npm run build` must run first. Neither the "
                        "README nor docs/quickstart.md mentions npm, node, or a "
                        "web build anywhere in the install path."),
            ticket="ADOPT-1"))
        # A stubborn CTO reads the traceback, sees a path, and tries to make it
        # exist. Record it as a workaround; do not pretend the step passed.
        if blocked_on_web_dist and shutil.which("npm"):
            w = sh(run, "uv-sync-workaround",
                   "Read the traceback, guess that the web bundle must be built.",
                   "UNDOCUMENTED — inferred from the traceback",
                   "npm install --silent && npm run build", cwd=product / "web",
                   timeout=900)
            if w.ok:
                sh(run, "uv-sync-retry", "Retry the documented install.",
                   "README.md#install", "uv sync", cwd=product, timeout=900)

    # -- Does the quickstart's own text run on a fresh install? ------------ #
    # Read out of the document rather than hardcoded, so this stays true as the
    # document changes. Hardcoding "step 3 says `nh init`" measures what the doc
    # said the day the harness was written; the day someone fixes it, a stale
    # assertion goes on failing and gets muted, and the day someone breaks it
    # again nobody notices.
    unresolvable = ctx.unrunnable_quickstart_commands(product)
    run.steps.append(StepResult(
        run.name, "quickstart-commands-resolve",
        "Type the quickstart's commands, exactly as printed, on a fresh install.",
        "docs/quickstart.md (every fenced bash block)",
        "for each `nh`-invoking line: does its program exist on this PATH?",
        0 if not unresolvable else 1, not unresolvable,
        json.dumps(unresolvable, indent=2)))
    if unresolvable:
        run.findings.append(Finding(
            run.name, "quickstart-commands-resolve", "high",
            (f"{len(unresolvable)} command(s) printed in docs/quickstart.md "
             "cannot run on a fresh install: `uv sync` installs the entry point "
             "into `.venv` and does not put it on PATH, so a bare `nh` is "
             "`command not found`. The quickstart is the document the README "
             "sends people to, and a user hits this on the first command they "
             "copy."),
            "docs/quickstart.md", json.dumps(unresolvable, indent=2)[:1500],
            workaround="Prefix with `uv run`, or activate .venv.",
            ticket="ADOPT-2"))

    # -- `nh init --help` / docs/quickstart.md: the DOCUMENTED scripted path -- #
    # A team scripting setup does not run bare `nh init` with stdin closed and
    # call that a verdict on scriptability — it reads `nh init --help` (or the
    # quickstart), finds the two lines that exist for exactly this, and runs
    # them. Only if THAT fails is scriptability actually broken.
    _nh_init_scriptable(ctx, run, product)

    # Dana finishes setup with whatever the scripted step above produced (or,
    # if it failed, by hand — the same files the wizard would have written).
    # From here the harness is no longer measuring Dana; it is unblocking the
    # personas behind her.
    ctx.seed_config()

    # -- README/quickstart: verify the install, then check the "is it
    #    documented" claim by reading the docs. --------------------------- #
    _doctor_is_documented(ctx, run, product)
    return run


# --------------------------------------------------------------------------- #
# Persona B — Sam, senior developer. "Get the sprint in and go home."
# --------------------------------------------------------------------------- #

SAM = dict(
    name="Sam",
    role="Senior developer",
    goal=(
        "Point no_human at the SkyLine repo and queue the sprint backlog. I want "
        "to come in tomorrow to PRs I can review, not to a pile of questions."
    ),
    knows=(
        "docs/quickstart.md sections 4-8 and docs/adapters.md. Owns the SkyLine "
        "repo. Has not read no_human's source."
    ),
)


def persona_sam(ctx) -> PersonaRun:
    run = PersonaRun(**SAM)
    sh = ctx.shell
    product = ctx.product

    # -- Sam files by ticket key, because that is what he does all day ------ #
    # Two separate things are checked here and conflating them is how this got
    # missed for months:
    #   (a) does any user-facing doc still TELL you to do this? If so, and it
    #       does not work, that is the high finding.
    #   (b) whether or not it is documented, is the failure ACTIONABLE? A
    #       developer will type a ticket key regardless — it is the single most
    #       natural thing to try — so the error is a product surface.
    r = sh(run, "task-add-ticket-key",
           "File the first backlog item from its Jira key — what I'd type first.",
           "docs/quickstart.md#4-add-your-first-task / docs/adapters.md#intake-no_humanintake",
           f"uv run nh task add AVI-1 --repo {ctx.target}",
           cwd=product, allow_fail=True)
    out = r.stdout + r.stderr
    documented = ctx.docs_still_promise_bare_ticket_key(product)
    ctx.note("bare_ticket_key_still_documented", documented)
    if documented and not r.ok:
        run.findings.append(Finding(
            run.name, "task-add-ticket-key", "high",
            ("`nh task add <TICKET-KEY>` is still documented in "
             f"{', '.join(documented)} and it does not work: "
             "intake/base.py:parse_source only recognises URLs containing "
             "/issues/; everything else falls through to `freeform` and "
             "ingest_from_url raises. The tracker adapter those docs describe "
             "was removed."),
            "docs/quickstart.md#4-add-your-first-task", out[-1500:],
            workaround="Use --title/--description/--criteria, or the Jira poller.",
            ticket="ADOPT-5"))
    # The message check runs whether or not the docs still promise the form,
    # and whether or not the command succeeded. Making it conditional (the
    # first version did) meant that on a run where the docs WERE wrong, this
    # step never executed and its assertion read as a failure for the wrong
    # reason — two defects reported as three.
    helpful = r.ok or all(s in out for s in ("--title", "issues/"))
    run.steps.append(StepResult(
        run.name, "task-add-ticket-key-error-is-actionable",
        "Read the error and work out what I should have typed instead.",
        "product surface — the error message itself",
        "does the failure name what IS accepted?",
        0 if helpful else 1, helpful, out[-1200:]))
    if not helpful:
        run.findings.append(Finding(
            run.name, "task-add-ticket-key-error-is-actionable", "medium",
            ("Passing a bare ticket key is the most natural first thing a "
             "developer types, and it fails. The failure does not say what IS "
             "accepted, so the user has no next move."),
            "product surface", out[-1200:], ticket="ADOPT-20"))

    # -- quickstart §4 bullet 2: freeform ---------------------------------- #
    t = ctx.backlog_by_key("AVI-13")
    r = sh(run, "task-add-freeform",
           "Fall back to the freeform form the docs also offer.",
           "docs/quickstart.md#4-add-your-first-task",
           ["uv", "run", "nh", "task", "add", "--title", t.title,
            "--description", t.description, "--repo", str(ctx.target),
            "--external-id", t.key, "--no-run", "--no-grill"],
           cwd=product, allow_fail=True)
    if not r.ok:
        run.findings.append(Finding(
            run.name, "task-add-freeform", "fatal",
            "Even the freeform intake path fails, so no task can be filed at all.",
            "docs/quickstart.md#4-add-your-first-task", (r.stdout + r.stderr)[-2000:],
            ticket="ADOPT-6"))

    # -- quickstart §6: check on tasks ------------------------------------- #
    sh(run, "task-list", "See the board.", "docs/quickstart.md#6-check-on-tasks",
       "uv run nh task list", cwd=product, allow_fail=True)
    r = sh(run, "status-json",
           "Get the lane counts in a form I can put on a dashboard.",
           "docs/quickstart.md#6-check-on-tasks", "uv run nh status --json",
           cwd=product, allow_fail=True)
    if r.ok:
        try:
            json.loads(r.stdout.strip() or "{}")
        except Exception:
            run.findings.append(Finding(
                run.name, "status-json", "medium",
                "`nh status --json` promises a JSON object on stdout and emits "
                "something json.loads cannot parse.",
                "nh status --help", r.stdout[-800:], ticket="ADOPT-7"))

    # -- quickstart §8: onboard the repo (referenced from Troubleshooting) -- #
    r = sh(run, "onboard",
           "Teach it how to test SkyLine before it writes any code.",
           "docs/quickstart.md#troubleshooting (`no profile to confirm`)",
           f"uv run nh onboard {ctx.target}", cwd=product,
           allow_fail=True, timeout=600)
    onboard_out = r.stdout + r.stderr
    if "[FAILED]" in onboard_out and "exit " in onboard_out:
        run.findings.append(Finding(
            run.name, "onboard-proving-opacity", "medium",
            ("When `nh onboard` proves a candidate command and it fails, it "
             "prints the command and the exit code and nothing else — not one "
             "line of the command's own output. The user is told 'fix the repo "
             "or its declarations and re-run' with no way to tell whether the "
             "cause was a missing dependency, an import error, or a genuinely "
             "failing test. Onboarding is the first thing every new user does "
             "after install, and this is the first place they can get stuck with "
             "no next step."),
            "docs/quickstart.md#troubleshooting", onboard_out[-1200:],
            workaround="Run the derived command by hand to see the real error.",
            ticket="ADOPT-18"))
    if not r.ok:
        run.findings.append(Finding(
            run.name, "onboard", "high",
            "`nh onboard <repo>` fails on a conventional uv-managed Python repo "
            "(pyproject + uv.lock + pytest), which is the shape the docs assume.",
            "docs/quickstart.md#troubleshooting", onboard_out[-2000:],
            ticket="ADOPT-8"))
    else:
        c = sh(run, "onboard-confirm", "Confirm the profile it derived.",
               "docs/quickstart.md#troubleshooting",
               f"uv run nh onboard {ctx.target} --confirm", cwd=product,
               allow_fail=True, timeout=600)
        if not c.ok:
            run.findings.append(Finding(
                run.name, "onboard-confirm", "high",
                ("`nh onboard <repo> --confirm` — the documented one-click gate — "
                 "refuses, so the repo never gets a usable profile and no task "
                 "can run against it."),
                "docs/quickstart.md#troubleshooting", (c.stdout + c.stderr)[-1200:],
                ticket="ADOPT-19"))

    # -- quickstart §8: stage the rest of the sprint ----------------------- #
    staged, failed = 0, []
    for t in ctx.backlog:
        if t.key == "AVI-13":
            continue  # already filed above
        cmd = ["uv", "run", "nh", "task", "add", "--title", t.title,
               "--description", t.description, "--repo", str(ctx.target),
               "--external-id", t.key, "--no-run", "--no-grill"]
        for c in t.criteria:
            cmd += ["--criteria", c]
        rr = sh(run, f"stage-{t.key}", f"Queue {t.key} for the overnight drain.",
                "docs/quickstart.md#8-overnight-drain-parallel", cmd,
                cwd=product, allow_fail=True, quiet=True)
        if rr.ok:
            staged += 1
        else:
            failed.append((t.key, (rr.stdout + rr.stderr)[-400:]))
    if failed:
        run.findings.append(Finding(
            run.name, "stage-backlog", "high",
            f"{len(failed)} of {len(ctx.backlog)} backlog items could not be staged.",
            "docs/quickstart.md#8-overnight-drain-parallel",
            json.dumps(failed, indent=2)[:2000], ticket="ADOPT-9"))
    ctx.note("tasks_staged", staged)

    # -- quickstart §8: "Leave `nh serve` running overnight" ---------------- #
    # Sam wants this in CI eventually, so he checks whether it can ever exit.
    r = sh(run, "serve-help", "Can I run the drain from a script and get an exit code?",
           "docs/quickstart.md#8-overnight-drain-parallel", "uv run nh serve --help",
           cwd=product, allow_fail=True)
    helptext = (r.stdout + r.stderr).lower()
    if r.ok and not any(w in helptext for w in ("--once", "--drain", "--exit-when",
                                                "--until-empty", "--no-daemon")):
        run.findings.append(Finding(
            run.name, "serve-help", "medium",
            ("`nh serve` has no drain-and-exit mode: it runs until interrupted, "
             "and its only option is --max-workers. There is therefore no "
             "supported way to run 'work the queue, then stop' from a script, a "
             "CI job or a cron entry, and no exit code that says whether the "
             "queue drained. Anyone automating the overnight drain the "
             "quickstart recommends has to supervise the process themselves and "
             "poll `nh status --json` to know when to stop it."),
            "docs/quickstart.md#8-overnight-drain-parallel", r.stdout[-900:],
            workaround="Background it, poll `nh status --json`, SIGTERM when the "
                       "pending+running lanes hit zero. This harness does exactly "
                       "that in full mode.",
            ticket="ADOPT-17"))
    return run


# --------------------------------------------------------------------------- #
# Persona C — Priya, developer who owns the integrations
# --------------------------------------------------------------------------- #

PRIYA = dict(
    name="Priya",
    role="Developer, owns tooling",
    goal=(
        "Wire it into our Jira, our Slack and our GitHub using only what the "
        "docs tell me to set, so I can hand the team a one-page setup note."
    ),
    knows="docs/configuration.md and docs/adapters.md. Nothing else.",
)


def persona_priya(ctx) -> PersonaRun:
    """Configure the three integrations strictly from the documented keys.

    Every assertion here is of the same shape: *set exactly what the docs say to
    set, then check whether the product noticed*. That is the only way to catch
    a documented key that nothing reads, which is a class of bug no unit test
    can find because the unit test is written from the code's side.
    """
    run = PersonaRun(**PRIYA)
    src = ctx.product / "src"

    # -- Every credential name the docs hand you must be read by something -- #
    # Read out of the document's own table rather than hardcoded. Two doc keys
    # were dead when this was written (`TRACKER_TOKEN`, read by nothing;
    # `SLACK_WEBHOOK_URL`, documented as a .env key while the code only ever
    # reads config.yaml), and both failed the same way: silently. A user sets
    # what the table says, gets no error, and gets no integration. A hardcoded
    # list of the two known-dead names would have gone green the moment they
    # were removed and would never catch the third.
    dead = ctx.documented_env_keys_nothing_reads(ctx.product)
    run.steps.append(StepResult(
        run.name, "documented-env-keys-are-read",
        "Set what the configuration table tells me to set.",
        "docs/configuration.md#no_humanenv-keys",
        "for each key in the .env table: does anything under src/ read it?",
        0 if not dead else 1, not dead, json.dumps(dead, indent=2)))
    if dead:
        run.findings.append(Finding(
            run.name, "documented-env-keys-are-read", "high",
            (f"docs/configuration.md tells users to set {', '.join(dead)} in "
             "~/.no_human/.env, and nothing under src/ reads "
             f"{'them' if len(dead) > 1 else 'it'}. This fails silently: the "
             "user sets the documented key, sees no error, and gets no "
             "integration."),
            "docs/configuration.md#no_humanenv-keys",
            json.dumps(dead, indent=2), ticket="ADOPT-10"))

    # ---------------- Jira ------------------------------------------------ #
    with ctx.fake_jira() as jira:
        cfg = {"integrations": {"jira": {
            "enabled": True, "site": jira.base_url, "project_key": "AVI",
            "email": "priya@skyline.example", "write_back": True}}}
        ok_documented = not dead
        ok_real, detail_real = ctx.probe_jira(cfg, {"JIRA_API_TOKEN": "fake-token"})
        run.steps.append(StepResult(
            run.name, "jira-real-key",
            "Try the key the source actually reads.",
            "SOURCE ONLY — src/no_human/intake/jira.py",
            "JiraAdapter(config).configured + search() against the local fake",
            0 if ok_real else 1, ok_real, detail_real))
        if not ok_real:
            run.findings.append(Finding(
                run.name, "jira-real-key", "high",
                "The Jira adapter fails against a protocol-faithful local fake.",
                "SOURCE ONLY", detail_real, ticket="ADOPT-11"))
        ctx.note("jira_probe", {"documented_key_works": ok_documented,
                                "real_key_works": ok_real,
                                "live": False,
                                "searches_seen": list(jira.state.searches)})

        # The docs never say the site is `integrations.jira.site` in config.yaml
        # — `integrations` does not appear in docs/configuration.md at all.
        conf_text = (ctx.product / "docs" / "configuration.md").read_text(encoding="utf-8")
        if "integrations" not in conf_text or "jira" not in conf_text.lower():
            run.findings.append(Finding(
                run.name, "jira-config-block", "high",
                ("The whole `integrations.jira` config block — site, project_key, "
                 "email, jql, write_back, poll_interval, default_repo — is "
                 "undocumented. docs/configuration.md's YAML sample does not "
                 "contain an `integrations` key at all, and docs/adapters.md "
                 "still describes the removed tracker adapter in its place. "
                 "There is no documented path from 'we use Jira' to working "
                 "Jira intake."),
                "docs/configuration.md#configuration",
                "grep -c 'integrations' docs/configuration.md -> "
                f"{conf_text.count('integrations')}",
                ticket="ADOPT-12"))

    # ---------------- Slack ----------------------------------------------- #
    with ctx.fake_slack() as slack:
        # Setting the env var is what a user does anyway — it is the obvious
        # name — so the probe records whether that silently does nothing, even
        # once the docs stop suggesting it.
        sent_env, detail_env = ctx.probe_slack_env(slack.webhook_url)
        sent_cfg, detail_cfg = ctx.probe_slack_config(slack.webhook_url)
        run.steps.append(StepResult(
            run.name, "slack-config-key",
            "Try the config.yaml path instead.",
            "docs/configuration.md (YAML sample)",
            "SlackNotifier(config.notifications.slack_webhook_url).notify(...)",
            0 if sent_cfg else 1, sent_cfg, detail_cfg))
        if not sent_cfg:
            run.findings.append(Finding(
                run.name, "slack-config-key", "high",
                "The documented config.yaml webhook path does not deliver either.",
                "docs/configuration.md", detail_cfg, ticket="ADOPT-14"))
        ctx.note("slack_probe", {"env_key_works": sent_env,
                                 "config_key_works": sent_cfg,
                                 "live": False,
                                 "posts_received": len(slack.state.posts)})

    # ---------------- GitHub ---------------------------------------------- #
    # No credential is available and none will be requested. What CAN be proven
    # offline is the product's own documented `local` bare-repo backend: a real
    # push through the real code path, returning a real branch.
    ok_push, detail_push = ctx.probe_local_pr()
    run.steps.append(StepResult(
        run.name, "github-local-backend",
        "Prove the VCS layer can push and open a PR at all, without a token.",
        "docs/adapters.md#vcs-no_humanvcs",
        "vcs.open_pr against a local bare remote (documented `local` kind)",
        0 if ok_push else 1, ok_push, detail_push))
    if not ok_push:
        run.findings.append(Finding(
            run.name, "github-local-backend", "high",
            "The offline `local` VCS backend documented in docs/adapters.md "
            "fails, so the PR path cannot be verified at all without credentials.",
            "docs/adapters.md#vcs-no_humanvcs", detail_push, ticket="ADOPT-15"))
    ctx.note("github_probe", {"local_bare_push_works": ok_push, "live": False,
                              "note": "gh pr create NOT exercised — no credential"})

    # docs say GH_ENTERPRISE_TOKEN / `gh auth login`; check the doc at least
    # names the public-github path, since that is what this team uses.
    return run


# --------------------------------------------------------------------------- #
# Persona D — Alex, the developer on review duty
# --------------------------------------------------------------------------- #

ALEX = dict(
    name="Alex",
    role="Developer on review duty",
    goal=(
        "Come in, see what it did overnight, read the evidence, approve what is "
        "good and send back what is not. I am the gate; I need to be able to do "
        "my job in fifteen minutes."
    ),
    knows="docs/quickstart.md §7 and the README's opening list of what you get.",
)


def persona_alex(ctx) -> PersonaRun:
    run = PersonaRun(**ALEX)
    sh = ctx.shell
    product = ctx.product

    sh(run, "blocked", "What is stuck and what does it need from me?",
       "docs/quickstart.md#6-check-on-tasks", "uv run nh blocked",
       cwd=product, allow_fail=True)

    ids = ctx.task_ids()
    if not ids:
        run.findings.append(Finding(
            run.name, "no-tasks", "medium",
            "No task reached a reviewable state, so the review path could not be "
            "walked at all in this run.",
            "docs/quickstart.md#7-review-and-approve",
            "0 task ids in the store", ticket=""))
        return run

    tid = ids[0]
    r = sh(run, "review", "Read the reviewer's evidence checklist.",
           "docs/quickstart.md#7-review-and-approve", f"uv run nh review {tid}",
           cwd=product, allow_fail=True)
    if not r.ok:
        run.findings.append(Finding(
            run.name, "review", "medium",
            "`nh review <id>` on a staged-but-not-yet-run task errors instead of "
            "saying plainly that there is nothing to review yet.",
            "docs/quickstart.md#7-review-and-approve", (r.stdout + r.stderr)[-1200:],
            ticket="ADOPT-16"))
    sh(run, "diff", "See the diff it wants to ship.",
       "docs/quickstart.md#7-review-and-approve", f"uv run nh diff {tid}",
       cwd=product, allow_fail=True)
    return run


# --------------------------------------------------------------------------- #
# Persona E — Marco, who owns the build pipelines
# --------------------------------------------------------------------------- #

MARCO = dict(
    name="Marco",
    role="DevOps",
    goal=(
        "Our tests run on Jenkins for the JVM services and CircleCI for "
        "SkyLine. The website says results from those gate the loop. I want "
        "that wired up, and I want to know it actually blocks — a gate that "
        "quietly stops gating is worse than no gate, because we would stop "
        "reading the PRs."
    ),
    knows="docs/adapters.md and docs/configuration.md. Nothing else.",
)


def _ci_json(raw: str) -> dict[str, Any]:
    for ln in raw.splitlines():
        if ln.startswith("__JSON__"):
            return json.loads(ln[len("__JSON__"):])
    return {}


def persona_marco(ctx) -> PersonaRun:
    """Wire CI from the public docs, then try to prove the gate gates.

    The claim under test is a sentence on getnohuman.com: "Jenkins & CircleCI —
    test layers can run on your CI, and the results gate the loop." Three
    separate things have to hold for that to be true, and they fail
    differently, so they are checked separately:

      1. a person can CONFIGURE it from the public docs;
      2. a red result produces a failing verdict and a green one a passing
         verdict — through the real adapter, not a stub;
      3. a broken or unreachable CI ESCALATES rather than degrading into
         "no gate".

    (3) is the one worth the most: `ci_runner is None` means "no remote CI is
    wired for this repo" to the orchestrator, which then proceeds on local
    tests alone. Anything that can turn a configured CI into None turns the
    advertised gate off silently.
    """
    run = PersonaRun(**MARCO)

    # ---------- 1. Can this be configured from the public docs at all? ----- #
    surface = ctx.undocumented_ci_surface(ctx.product)
    ctx.note("ci_doc_surface", surface)
    run.steps.append(StepResult(
        run.name, "ci-docs-cover-the-backends",
        "Read the two docs I have and configure Jenkins and CircleCI.",
        "docs/adapters.md#ci-no_humanci + docs/configuration.md",
        "every backend / config key / env var in ci/ vs what the docs mention",
        0 if not surface["backends_undocumented"] else 1,
        not surface["backends_undocumented"], json.dumps(surface, indent=2)))

    if surface["backends_undocumented"]:
        run.findings.append(Finding(
            run.name, "ci-docs-cover-the-backends", "high",
            (f"{len(surface['backends_undocumented'])} of the "
             f"{len(surface['backends_supported'])} CI backends the code "
             f"supports ({', '.join(surface['backends_undocumented'])}) have "
             "their `ci.backend` IDENTIFIER in neither docs/adapters.md nor "
             "docs/configuration.md — including `circleci`, which the website "
             "advertises by name. The prose mentions CircleCI; nothing tells "
             "you the string to put in `ci.backend`, and the CI section of "
             "adapters.md documents the GitLab backend only while "
             "configuration.md's `ci:` sample is a GitLab block. A persona has "
             "no documented path from 'our tests run on CircleCI' to a working "
             "gate."),
            "docs/adapters.md#ci-no_humanci", json.dumps(surface, indent=2)[:2000],
            workaround="Read src/no_human/ci/__init__.py for the key names.",
            ticket="ADOPT-21"))

    if surface["ci_env_vars_undocumented"]:
        run.findings.append(Finding(
            run.name, "ci-env-vars-documented", "high",
            (f"{', '.join(surface['ci_env_vars_undocumented'])} is read by the "
             "CI code and named in no user-facing doc. This is the third "
             "instance today of the same class: a credential the product needs, "
             "that the documentation never tells you to set. It fails the same "
             "way as the others — the run proceeds and the gate does not."),
            "docs/configuration.md#no_humanenv-keys",
            json.dumps(surface["ci_env_vars_read"]), ticket="ADOPT-22"))

    if surface["ci_config_keys_undocumented"]:
        run.findings.append(Finding(
            run.name, "ci-config-keys-documented", "medium",
            (f"{len(surface['ci_config_keys_undocumented'])} `ci.*` config keys "
             "the code reads are undocumented: "
             f"{', '.join(surface['ci_config_keys_undocumented'])}. Several are "
             "required — a Jenkins backend needs `job` and `base_url`, and "
             "CircleCI's `project` is a `<vcs>/<org>/<repo>` slug rather than "
             "the `group/subgroup/repo` path the documented GitLab sample "
             "shows, so copying that sample produces a backend that builds and "
             "then 404s."),
            "docs/configuration.md", json.dumps(surface, indent=2)[:1500],
            ticket="ADOPT-23"))

    # ---------- 2. Does a red result actually read as failed? -------------- #
    verdicts: dict[str, dict] = {}
    for backend in ("jenkins", "circleci"):
        for outcome in ("green", "red"):
            _, raw = ctx.probe_ci_verdict(backend, outcome)
            v = _ci_json(raw)
            verdicts[f"{backend}:{outcome}"] = v or {"unparsed": raw[-400:]}
            expected_pass = outcome == "green"
            got = v.get("passed")
            ok = (got is expected_pass)
            run.steps.append(StepResult(
                run.name, f"ci-verdict-{backend}-{outcome}",
                f"Make {backend} come back {outcome} and see what the loop is told.",
                "SOURCE + local fake — no live CI instance",
                f"ci_from_config(...).trigger() against a local fake {backend}",
                0 if ok else 1, ok, json.dumps(v, indent=2)))
            if not ok:
                run.findings.append(Finding(
                    run.name, f"ci-verdict-{backend}-{outcome}",
                    "fatal" if outcome == "red" else "high",
                    (f"A {outcome} {backend} pipeline produced "
                     f"`CIResult.passed={got!r}`. "
                     + ("A failing pipeline that reads as passed means the "
                        "advertised gate does not gate: the loop proceeds to a "
                        "PR on a red build."
                        if outcome == "red" else
                        "A passing pipeline that does not read as passed blocks "
                        "every task behind a gate that can never be satisfied.")),
                    "getnohuman.com — 'the results gate the loop'",
                    json.dumps(v, indent=2)[:1500], ticket="ADOPT-24"))

    # ---------- 3. The failure paths — the ones that fail quietly ---------- #
    for backend in ("jenkins", "circleci"):
        for outcome, expect in (("unauthorized", "access_failure"),
                                ("server_error", "infra_failure"),
                                # A job that starts and never finishes. The
                                # adapter is configured with timeout_minutes=1,
                                # so it must give up and say so rather than
                                # blocking the loop forever.
                                ("running_forever", "infra_failure")):
            _, raw = ctx.probe_ci_verdict(backend, outcome)
            v = _ci_json(raw)
            verdicts[f"{backend}:{outcome}"] = v or {"unparsed": raw[-400:]}
            # The rule: never a silent pass. Either the named flag is set, or
            # the verdict is at minimum NOT passed.
            flagged = bool(v.get(expect))
            not_passed = v.get("passed") is False
            ok = flagged or not_passed
            run.steps.append(StepResult(
                run.name, f"ci-failure-path-{backend}-{outcome}",
                f"What happens when {backend} answers {outcome}?",
                "SOURCE + local fake — no live CI instance",
                f"expect {expect}=True, and never passed=True",
                0 if ok else 1, ok, json.dumps(v, indent=2)))
            if v.get("passed") is True:
                run.findings.append(Finding(
                    run.name, f"ci-failure-path-{backend}-{outcome}", "fatal",
                    (f"A {backend} CI that answers {outcome} produced a PASSING "
                     "verdict. A broken or unreachable CI must never read as a "
                     "green gate."),
                    "product surface", json.dumps(v, indent=2)[:1200],
                    ticket="ADOPT-25"))
            elif not flagged:
                run.findings.append(Finding(
                    run.name, f"ci-failure-path-{backend}-{outcome}", "medium",
                    (f"A {backend} CI answering {outcome} does not set "
                     f"`{expect}`, so it is indistinguishable from a genuine "
                     "test failure. The orchestrator routes the two very "
                     "differently — an access wall parks with MISSING_ACCESS "
                     "naming the key, an infra failure retries then escalates, "
                     "and a test failure loops the coder back to fix code that "
                     "is not broken."),
                    "product surface", json.dumps(v, indent=2)[:1200],
                    ticket="ADOPT-26"))
    ctx.note("ci_verdicts", verdicts)

    # ---------- 4. Can a configured CI silently become no CI? -------------- #
    _, raw = ctx.probe_ci_silent_degradation()
    degraded = _ci_json(raw)
    ctx.note("ci_silent_degradation", degraded)
    # `disabled` SHOULD be None — that is the operator saying no. Everything
    # else asked for CI and must not silently get none.
    silent = sorted(k for k, v in degraded.items()
                    if k != "disabled" and "NO GATE" in str(v))
    run.steps.append(StepResult(
        run.name, "ci-misconfig-is-not-silently-no-gate",
        "Get a config detail wrong, the way anyone would on day one.",
        "SOURCE — src/no_human/ci/__init__.py:ci_from_config",
        "an incomplete/invalid ci block must not build a None runner",
        0 if not silent else 1, not silent, json.dumps(degraded, indent=2)))
    if silent:
        run.findings.append(Finding(
            run.name, "ci-misconfig-is-not-silently-no-gate", "high",
            (f"{len(silent)} CI misconfigurations ({', '.join(silent)}) make "
             "`ci_from_config` return None rather than raise. A user who set "
             "`ci.enabled: true`, asked for a gate, and got one config key "
             "wrong still has NO GATE: the run proceeds and opens a PR having "
             "never been gated on CI. That part is unchanged and is what this "
             "finding is about. What HAS changed (2026-08-02) is that it is no "
             "longer silent: `Orchestrator._resolve_ci_runner` emits an "
             "`advisory` naming the source and the reason, `nh doctor` counts "
             "it under advisory_degradations and reports it statically, and "
             "`ci_skipped` no longer claims CI was unconfigured. So the user "
             "can now find out — but only by looking. Whether this should "
             "ESCALATE and stop the run instead of proceeding ungated is the "
             "open question, tracked as KI-5."),
            "SOURCE — ci/__init__.py:ci_from_config + "
            "orchestrator.py:Orchestrator._resolve_ci_runner",
            json.dumps(degraded, indent=2), ticket="ADOPT-27"))
    return run


PERSONAS: tuple[tuple[str, Callable], ...] = (
    ("Dana", persona_dana),
    ("Sam", persona_sam),
    ("Priya", persona_priya),
    ("Marco", persona_marco),
    ("Alex", persona_alex),
)
