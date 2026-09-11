# What stops it shipping something broken — and what it does not cover

This is the detail the front page links to. It was the README's longest section
until 2026-08-01; nothing here was deleted, only moved off a page whose job is
to get you to a first task.

Four gates, and the deterministic inputs they run on. All of it is code, not prompt
instructions, and the two deterministic gates run before the review gate does.
(The tamper guard's adjudicator runs on the reviewer tier, so a flagged attempt
does spend reviewer-tier tokens — just not on the gate review.)

## The pipeline

```
ticket ──► context ──► plan ──► implement ──► review ──► test ──► PR ──► you merge
              │                      │           │         │
              │                      │           │         └── local runner + optional CI
              │                      │           └── fresh-context reviewer, edit tools refused
              │                      └── Claude Agent SDK, your credential, your checkout
              └── grep, git log, past sessions
```

Tickets come from a GitHub or GitLab issue URL, or a plain-English `--title`.
Jira is supported as an opt-in server-side poller, not as an argument to
`nh task add`.

Implementation runs behind a `PreToolUse` hook
([`_make_guard_hook`](../src/no_human/agent/claude_backend.py)) that
enforces forbidden paths, protected branches, the merge ban and a
destructive-shell circuit breaker. A failed review loops back to implement.
Branching, committing and pushing are done by no_human's own git code, not by
the model. The PR lands in `awaiting_approval` and waits.

## An adversarial reviewer that is not the author

[`src/no_human/review/reviewer.py`](../src/no_human/review/reviewer.py) opens a
fresh Agent SDK session whose `Write`, `Edit`, `NotebookEdit` and `MultiEdit`
tools are refused, along with the direct git and forge write commands.

Three things that refusal is NOT, because this page is where someone decides
whether to rely on it. Bash is not refused, so a shell redirection can still
write a file, and [`review/reviewer.py`](../src/no_human/review/reviewer.py)'s
own module docstring says so rather than implying otherwise. The refusal happens **at the tool call**, in a PreToolUse
guard that reads a command line — so it is a cost, not a proof, and a spelling
it does not model gets through. A global option before the subcommand *was*
one such spelling — measured 2026-08-22, `git -C . commit` and
`git -C . push origin <branch>` were both allowed in a review session; both
are now denied by the argv-shaped read-only check in
[`agent/guard.py`](../src/no_human/agent/guard.py), which resolves the
subcommand past global options rather than matching the literal. That closes
the spellings it models and no more: the modelled set is not closed.
Staging is not a global-option case at all: `git add` is absent from the
write-verb list, so plain `git add -A` is allowed too. And nothing re-reads the tree after the reviewer runs, so a write
that did get through would be what the test gate then scores.

Treat this as the layer it is. The control that would make the property
structural belongs at the act, and is tracked separately.

It runs on a different model from the implementer by default (an Opus-tier reviewer over the Sonnet-tier coder —
the current IDs are `llm.review_model` and `llm.primary_model` in
[`DEFAULT_CONFIG`](../src/no_human/config.py)), and tells it to refute
"done". It returns a checklist of findings with `file`, `line` and severity — a
boolean verdict, never a score. Three things make that verdict hard to game:
every cited location is checked against the actual tree, and a finding citing a
location that does not exist is demoted to advisory (`_verify_citations` in
`reviewer.py`); the pass/fail is recomputed deterministically from the checklist
rather than taken on the model's word (`_gate_verdict` in `reviewer.py`); and a
reviewer that crashes, times out, or emits no parseable verdict fails closed
(`_parse_review_output`, `AdversarialReviewer._fast_review` and
`AdversarialReviewer._agent_review` in `reviewer.py`).

## Verifiers — a recorded verdict per rule

[`src/no_human/review/verifiers.py`](../src/no_human/review/verifiers.py) loads
project-specific rules from `.no_human/verifiers.yaml` (repo-scoped) and a
second, global file under `~/.no_human` — each rule a plain-English
`statement` plus a glob `paths` list and a `severity`. Before the agentic
reviewer runs, `Orchestrator._run_review` selects the rules whose `paths`
match the changed files and puts each one, independently, to a fresh
bounded judge call (max one turn) with the diff and read-only file access.
Every verdict is recorded — pass or fail, with `evidence`, `file`/`line`
when it names one, and which files it actually checked — never only the
failures. A verifier judge that reaches no parseable verdict (a timeout, a
crash, an unparseable response) gets exactly one bounded retry. If the retry
*also* reaches no verdict, the outcome is recorded as `no_verdict`/
`unavailable` on that verifier's result — it is **advisory only**: it never
causes the round to fail or escalate on its own, it is reported as one
advisory log line and as a `⚠️` item in the PR Evidence table's per-rule
fold, and the round proceeds to the agentic reviewer. (The Evidence table's
`Verifiers` ROW is `⚠️` only when nothing else failed; a round that also
carries a genuine FAIL keeps a `❌` row, because the row summarises the whole
set.) On a round where no *other* verifier answered FAIL, it does not add
a `rule:<verifier id>` item to the reviewer's
checklist at all — `_run_review` does not escalate the task or end the
attempt, and the round proceeds to the agentic reviewer exactly as it would
if every verifier had passed. On a round where another verifier *does*
answer FAIL, the unavailable one still rides along on that failing round's
own checklist (at advisory/`low` severity, per `to_checklist_item`) so it
stays visible rather than silently disappearing — it just never fails the
round by itself. A verifier that never reaches a verdict is an
infrastructure gap in the gate, not evidence about the change, so it must
never be charged to the coder as a defect nobody found — merge-policy's
`verifiers_all_satisfied` check treats an unavailable-only round as ready,
naming the unavailable verifiers in its detail rather than blocking on them.

The merge into the review decision is monotonic for **answered** verdicts,
not advisory noise the reviewer can talk itself past: **any** verifier that
*answers* FAIL ends the round before the agentic reviewer ever runs,
appearing on the checklist as `rule:<verifier id>`. Only a genuine FAIL
short-circuits the round this way — a no-verdict-after-retry outcome does
not, even when it is the only failing result that round. Every verifier
verdict is persisted on the attempt row (`attempts.verifier_results`)
and keyed into `task.context.verifier_results` by the commit SHA it judged,
so a later attempt's verdicts never overwrite an earlier one's. The same
verdicts are rendered for a human in more than one place: as a `Verifiers`
row in the PR body's Evidence table (`core/pr_evidence.py`'s `verifiers_pin()` — `"N of N
satisfied"`, `"K of N failed — id1, id2"`, or, when one or more reached no
verdict, a variant naming them separately as `"no verdict (advisory)"` rather
than folding them into the failed count, folded behind a `<details>` list
of every rule), and as a per-verifier list in the board's Review tab.
`nh verifiers list` and `nh approve --ready` surface the same distinction on
the command line; the evidence ledger's `verifiers.md` is not a rendering at
all — it dumps the stored result objects verbatim, so it shows the raw
`unavailable` flag rather than a glyph. No
`.no_human/verifiers.yaml` (repo or global), verifiers disabled in config, no
usable diff, or a changed-path set that matches none of the loaded rules all
skip the step entirely and proceed straight to the agentic reviewer — this is
an added gate, not a replacement for it, and an empty rule set changes
nothing about what already ran.

### Managing verifiers from the CLI

`nh verifiers` ([`src/no_human/cli/verifiers_cmd.py`](../src/no_human/cli/verifiers_cmd.py))
is an authoring and inspection surface over the same `verifiers.yaml` files —
none of its subcommands make a model call, a network call, or construct an
`Orchestrator`; `Orchestrator._run_review` remains the only caller of
`run_verifiers`.

- `nh verifiers list [--repo] [--json]` — prints every configured verifier
  (repo + global, repo wins on id collision) and any load problems, plus a
  `runs` / `no verdict` count per verifier (`no_verdict_count` in `--json`)
  aggregated read-only from every persisted attempt's `verifier_results`, so
  an operator can spot a verifier that never answers. Always exits 0 — a
  missing or unreadable DB degrades the counts to zero rather than failing;
  it is a read-only inspection command.
- `nh verifiers add --id ID --statement TEXT --path GLOB [--path GLOB ...]
  [--severity high] [--repo] [--global/-g]` — additively defines a new
  verifier. It never rewrites the whole YAML file, only appends the new
  entry, so hand-authored comments and ordering survive untouched; a
  write-then-verify step reloads the merged config and rolls back the
  written bytes if the new id doesn't come back clean. Refuses (exit 1,
  nothing written) on a duplicate id or an entry that fails the same
  validation the loader itself applies.
- `nh verifiers check [--repo] [--against REF] [--path PATH ...]` — a
  config/selection gate: which verifiers would be selected for the changed
  paths, and which of those would fail closed for lack of a matching diff
  hunk. Makes no model call and builds no verifier prompt. Exits 1 if the
  config failed to load, zero verifiers are configured, or `--against`
  doesn't resolve; exits 0 otherwise.
- `nh verifiers propose TASK_ID [--repo] [--apply] [--severity high]` — turns
  a task's already-persisted review findings (`attempts.review_checklist`,
  falling back to `task.context.draft_review_comments`) into candidate
  verifier YAML, skipping a verifier's own `rule:`-labelled verdicts and
  findings that don't cite a file. Without `--apply` nothing is written;
  with `--apply` it appends the candidates the same additive, write-then-
  verify way `add` does, and is idempotent — an id that's already defined is
  named and skipped rather than duplicated.

## Deterministic lint evidence — not a gate, an input

[`src/no_human/review/lint_evidence.py`](../src/no_human/review/lint_evidence.py)
runs ruff over the changed Python files and attaches the findings to the review
context, so the reviewer judges against machine output instead of reading the
diff cold. It uses the target repo's own ruff config and attaches nothing if the
repo has none, so no_human never imposes its style on yours. It cannot block on
its own: any failure returns empty rather than stalling the review.

## Net-new type diagnostics — also an input, not a gate

[`src/no_human/review/type_evidence.py`](../src/no_human/review/type_evidence.py)
answers one question for the reviewer: **what type errors does this diff
introduce that the merge base did not already have?** If the repo under review
configures a type checker — `pyrightconfig.json` or `[tool.pyright]`,
`[tool.mypy]`/`mypy.ini`/a `[mypy]` section in `setup.cfg`, or `tsconfig.json` —
the same checker is run twice with the same arguments, over whatever that
checker's own configuration admits —
once at the merge base, once at the reviewed commit — and the base result is
subtracted from the after one. A repo carrying 400 pre-existing errors therefore
reports `net-new: 0`, not 400.

Both runs happen in a throwaway `git worktree`, and neither ever runs in the
tree under review. That is not housekeeping. The collector runs inside the
window the orchestrator brackets with `reviewer_worktree.snapshot` and
`.compare`, so a checker that drops `.mypy_cache/` or a `*.tsbuildinfo` into the
attempt's tree makes that compare report an added path — and the orchestrator
charges it to the reviewer as `reviewer_wrote`, reverting and replacing a real
verdict with an integrity failure that nobody caused. Running both sides in
worktrees also means neither sees untracked files, so a scratch file left in the
working tree cannot produce a diagnostic that is "net-new" only because the base
could never have had it.

Four properties are worth knowing, because each one is a defect this design
had to avoid rather than a feature:

- **Not scoped to changed lines**, unlike the lint evidence above. The
  characteristic net-new type error appears at a *call site the diff never
  touched* — narrow a parameter and every caller lights up — so a changed-line
  filter would discard exactly the diagnostics worth having.
- **Fingerprints ignore line numbers.** One inserted import shifts every
  diagnostic below it; a key that included the line would read the whole file
  as newly broken. `_fingerprint` in `type_evidence.py` keys on path, code and
  a digit-normalised message, counted as a multiset.
- **It reports that it did not run, by saying nothing.** A missing binary, a
  crashed checker, output that does not parse, a base worktree that cannot be
  built, or a base tree whose environment is poorer than the after tree's
  (`_environments_comparable` in `type_evidence.py`) all yield `ran=False`, and
  `format_type_evidence` then renders **no section at all** — so absence never
  reads as a clean bill of health. A check that did run and found nothing
  renders `net-new: 0` explicitly, which is a different and usable fact.
- **A worktree carries no installed dependencies**, and that bounds what this
  can see. `mypy` is unaffected, because it resolves imports from no_human's own
  environment and therefore behaves identically on both sides. `tsc` and
  `pyright` are not: a project whose dependencies live in `node_modules` or a
  project `.venv` is degraded on both sides equally, so unresolved-import errors
  cancel in the subtraction and a `net-new: 0` from those two is weaker evidence
  than it reads as — a diff that breaks a call into a dependency can be missed.
  Treat a Python repo checked by `mypy` as the case this is measured on.

Like lint, it uses only the repo's own configuration and adds no dependency: a
repo that configures no type checker spawns no subprocess. The checker binary is
resolved from no_human's own `PATH`, never from a `.venv` or `node_modules`
inside the repo under review. It changes no merge rule — the diagnostics go to
the reviewer and the human, who draw the conclusion.

Two things the checker itself does are worth knowing before you enable one,
because they are the checker's behaviour and not no_human's; both are declared
against `review/type_evidence.py` in
[`tests/test_egress_allowlist.py`](../tests/test_egress_allowlist.py), which is
the module that spawns. The PyPI
`pyright` distribution is a launcher whose first run **downloads** a node
runtime and the `pyright` npm package, so a review on a machine that resolves
that wrapper makes a network call. And `mypy` imports the modules a `plugins =`
line names, read from the config of the repo under review — the harness already
runs that repo's tests, so it is not a new trust boundary, but it is the first
time the review half executes anything the reviewed repo wrote.

Because of that second one, the checker is spawned with **no credential**: the
child environment is this process's with every secret-shaped variable removed
(`_checker_env`, over `agent/child_env.drop_foreign_secrets` with an empty
keep-list), so a repo-authored plugin never holds our OAuth token, API key or
cloud credential. `PATH`, `HOME` and the proxy variables stay. That trade has a
real cost and it is not hypothetical: a plugin that *reads* one of those
variables no longer loads — `mypy_django_plugin` is the canonical case, since
it imports the settings module and that reads `SECRET_KEY`/`DATABASE_URL`. mypy
then exits non-zero, the run is correctly distrusted, and the section would
simply vanish. Rather than vanish, it renders a **`TYPE EVIDENCE: NOT
COLLECTED`** line naming the cause — a statement about the collector, never
about the code, and the line says so itself. Every other failure (a crash, a
timeout, unparseable output) still renders nothing at all, so absence keeps
meaning absence.

## Per-edit type feedback — the same signal, one turn earlier

[`src/no_human/agent/type_hook.py`](../src/no_human/agent/type_hook.py) puts type
diagnostics in front of the **coder** rather than the reviewer. It is a
`PostToolUse` hook beside the per-edit lint hook: after an `Edit`/`Write` to a
`.py` or `.pyi` file it runs the checker the repo already configures over *that
one file* and feeds back what the checker did not report on that file before the
edit. Off by default — set `hooks.per_edit_type: true` — and a no-op on a repo
that configures neither `pyright` nor `mypy`.

The baseline is the difference from the section above. The gate diffs against
the merge base, which is the right question to ask once per review and far too
expensive to ask per edit. This hook diffs against **the same file, moments
earlier, in the same tree**: the same checker, the same arguments, the same
interpreter, seconds apart. What that identity buys is an identical
*environment* between the two runs, and that is what makes the comparison sound
without a comparability check — the degraded-base problem the gate has to detect
and refuse cannot arise here.

What it does **not** buy is attribution, and the feedback text says so rather
than implying otherwise. A difference between the two runs means something the
checker *read* changed, not that the triggering edit changed it. `mypy <file>`
follows imports, so a change anywhere in the transitive graph lands in the
comparison — edit `lib.py`, then `app.py`, and `lib.py`'s new error is reported
at the edit to `app.py`, whose bytes never changed. And `Bash` is not one of the
tools the hook watches, so `sed -i`, `patch`, `ruff --fix` and `black` are
invisible to it and whatever they broke first surfaces at the next `Edit`. So
the block reads "*N diagnostics on or reachable from X that were not reported
when this file was last checked*", naming both ways it can be wrong, instead of
borrowing the lint hook's "your edit introduced" — which ruff can say honestly
because ruff sees exactly one file.

What that buys is speed, and what it costs is coverage. Four ceilings, all of
them named in the module and one of them printed in the feedback itself:

- **The first edit to a file reports nothing.** It has nothing to subtract
  against, and inventing a baseline would reintroduce exactly the asymmetry the
  gate guards against. The gate is the backstop.
- **Dependencies, not dependents.** `mypy <file>` and `pyright <file>` analyse
  that file and what it imports, never its callers — so the characteristic type
  break, a narrowed parameter lighting up every call site, is invisible here and
  shows up at the gate. This is why phase 2 does not replace the gate, and the
  feedback text says so in the turn.
- **Python only.** Handed a single file, `tsc` ignores the repo's
  `tsconfig.json`, so a per-file TypeScript run answers a different question
  than the project's own configuration asks. TypeScript repos get the gate-time
  evidence and nothing here.
- **Reported once.** A report advances that file's baseline, so a diagnostic the
  coder chose not to fix is not re-sent every turn. The gate still has the last
  word on it.

Bounded twice, because an advisory signal must never be the reason an attempt
runs long: 20s per run, and 240s cumulative per attempt, after which the hook
goes quiet for the rest of the attempt and says so once on the event stream. Any
checker failure — missing binary, crash, timeout, unparseable output — reports
nothing and records no baseline, so a failed run can never become the thing the
next edit is compared against. Nothing is written inside the repo: `mypy`'s
cache is relocated under the system temp root, and a run that cannot get such a
directory does not happen.

That cache is **one directory per attempt, removed with the attempt** — warm
from the second edit onward within a run (measured 4.6s cold, 0.2s warm on a
468-line file) and deliberately not shared across runs. It could not be: the
path it would be keyed on is the attempt's worktree, which is minted per run,
and mypy keys its own cache on source paths. Its parent is a single predictable
directory created `0700` and refused outright unless it is ours alone; the
per-attempt child inside it is random, so two attempts never share one — mypy
takes no cross-process lock. A child left behind by a run that died is
reclaimed by owner-pid liveness, the same rule `core/worktree.py` uses for
worktrees.

## A tamper guard against a self-gutted test suite

[`src/no_human/testing/tamper_guard.py`](../src/no_human/testing/tamper_guard.py)
diffs test files separately from product code and fails on a net drop in test or
assertion count, a net increase in skip/xfail markers, a real assertion replaced
by a tautology, or a behaviour-faking `autouse` fixture appearing in a
`conftest.py`. No model judgement is involved. It covers Python, JS/TS, Java and
the `e2e/` tree.

## A structural-size ratchet over the codebase itself

[`tests/test_structural_budget.py`](../tests/test_structural_budget.py) walks
every file under `src/no_human` with the stdlib `ast` module and freezes
today's offenders against three thresholds: a function longer than 300
lines, a function with an estimated cyclomatic complexity over 60, or a file
longer than 2,500 lines. It fails if a new offender appears outside the
freeze, if a frozen entry grows past its frozen value, or if a frozen entry
shrinks below its threshold (or its symbol disappears) without being deleted
from the allow-list — the budget can only move down. It is a size ratchet,
not a design review or a lint config, and adds no dependency.

## A reproduction gate that proves the fix fixed the bug

[`src/no_human/testing/repro_gate.py`](../src/no_human/testing/repro_gate.py)
takes the tests the coder says demonstrate its change, copies them into a
worktree at the merge base, and requires them to **fail there** and **pass on
the new tree**. A bugfix whose test also passes on the unfixed code has proved
nothing. Default mode is `advisory`, which still enforces for a Python bugfix — where
"Python" means the coder's edits reached `.py` files **through the edit tools**,
since that is the hook the check reads, so a `.py` file written by `sed` or a
heredoc is not bound. `repro_gate.mode: required` drops both conditions and
enforces for every kind and every change (`repro_gate.mode` in
[`DEFAULT_CONFIG`](../src/no_human/config.py), which says the same in its own
words).

## Merge-ready policy

[`src/no_human/core/merge_policy.py`](../src/no_human/core/merge_policy.py)
evaluates a fixed set of rules — review passed, tests ran and passed, the
tamper guard is clear, the reproduction gate passed or was not required,
every selected verifier is satisfied, CI is a success or unknown — against
the gates that already ran for a commit, and returns a single `ready: bool`
plus a per-rule breakdown. A repo can override the rule set with
`.no_human/merge_policy.yaml`; a policy file that fails to load (missing,
malformed, or too large) is a recorded `problem`, not a silent pass, and
evaluation falls back to the default rule set. A diff that edits
`.no_human/merge_policy.yaml` itself is flagged as `policy_changed_in_diff`
— a coder cannot author its own merge gate, so this always forces
`ready: false` (recorded as a `problems` entry, not a second flag to keep in
sync) and renders as a `⚠️` row rather than `✅`/`❌`, even when every rule the
diff's own policy demands happens to pass.

`Orchestrator._finalize` computes this verdict once per commit, from the
same gate outputs the PR body's Evidence table already gathered, and
persists it on the task (`task.context.merge_policy.<head sha>`) before
opening or updating the PR — so a later render and the API read the same
recorded verdict rather than each re-deriving their own. The PR body prints
it as the Evidence table's last row plus a per-rule `<details>` fold; the
API exposes it as `merge_ready` on a task summary (`GET /api/tasks?
merge_ready=1` filters to it) and the full verdict on task detail.

**This is advisory to the human; nothing merges on it.** The verdict is a
recorded opinion a human reads before deciding — no code path in this repo
merges, blocks a push, or gates PR delivery on it (a failed or missing
compute is caught and logged, never a reason a PR does not ship — see the
`try`/`except` around the block in `_finalize`). `nh approve <task_id>` is
the base merge path, and it does not read this verdict at all — running it
IS the human decision. What it *does* check before landing is the
independent reviewer's own PASS on the branch head (`_review_pass_evidence`,
in both the CLI and the API path, which refuse the merge otherwise); the
merge-ready verdict is not among those preconditions. The verdict is keyed
by head sha precisely because nothing re-evaluates it: a verdict stamped for
an older commit is shown as absent (`merge_ready: null`) for the commit
sitting in the PR now, rather than carried forward as if it still applied.

`nh approve --ready` is a convenience LISTING over that same base path — it
prints every `awaiting_approval` task whose verdict is `ready: true` for its
*current* branch head (a stale-sha verdict, or one with
`policy_changed_in_diff: true`, is excluded, same rule as above) alongside
its `rules passed/total` and PR URL, and does nothing else. Add `--yes` and
it walks that list through `nh approve <task_id>`'s own procedure — one task
at a time, in listed order, stopping at the first failure — so every
precondition `nh approve <task_id>` already enforces (the reviewer PASS
above included) still applies per task; the verdict only decides what gets
offered to a human to land, never whether a task is *allowed* to land. The
board shows the same verdict as a `MERGE-READY` chip on a task's card. This
does not change who merges: `--yes` still runs the identical git-identity
squash-land as a single `nh approve <task_id>`, and a human still has to
type it.

## When it cannot finish

The loop is bounded and it is allowed to give up. `bounds.max_attempts` is 3 per
loop, `bounds.max_turns_per_attempt` is 500, and `bounds.lifetime_attempts` is 9
across resumes (the `bounds` block of
[`DEFAULT_CONFIG`](../src/no_human/config.py)). An identical tool call repeated
in a loop, or the same agent-error signature seen again, trips stuck detection —
`StuckDetector.record_tool_call` and `StuckDetector.record` in
[`core/bounds.py`](../src/no_human/core/bounds.py) — which resets context
instead of stacking more corrections on a confused session.

When it runs out, it does not invent a plausible diff. It classifies the blocker
into one of eleven categories — `MISSING_ACCESS`, `AMBIGUITY`, `SCOPE_EXPLOSION`,
`IMPOSSIBLE`, `QUOTA`, `BUDGET_EXHAUSTED` and five more
([`src/no_human/blockers/taxonomy.py`](../src/no_human/blockers/taxonomy.py)) —
and either parks with a wake condition or escalates with a structured report and
one specific question.

With one exception, and it is not a rare one: an exhausted budget. Under
`budget.exhaustion_terminal` — the default — `BUDGET_EXHAUSTED` takes a route of
its own, ahead of every other, that ends the task `failed` and asks nothing. The
structured record is still written, with a root-cause hypothesis, evidence and a
wake condition; what it does not carry is a question, because there is no answer
you could give that would buy more budget. So it does not park, it does not
notify, and `nh blocked` does not list it — `nh status` shows it failed, and the
record is on the task. Set `budget.exhaustion_terminal: false` and it escalates
instead: the route becomes `ESCALATED` with a notification
(`blockers/taxonomy.py`) and the blocker is written with a question and its
raise/stop options (`core/orchestrator.py`, `config.py`'s `budget` section).
`Route.parked` is still false — but `ESCALATED` IS one of the CLI's parked
states, so unlike the terminal route this one does show up in `nh blocked`
and `nh reply <id>` resumes it.

`nh blocked` lists what is parked; `nh reply <id> "answer"` resumes it. Routing per category: [blockers.md](blockers.md).

An honest escalation costs a minute to triage. A confident wrong diff costs an
hour to review.

## Limits — things this does not do, and numbers it does not have

- **Ambitious tasks are not the target.** It is aimed at well-scoped work:
  bugfixes, test gaps, small features, investigations. A vague ticket produces
  an escalation, which is the intended behaviour, not a workaround.
- **Published catch-rate for the reviewer.** The reviewer tier moved to
  `claude-opus-5` on 2026-07-26 and was reverted to `claude-opus-4-8` on
  2026-08-11, after an A/B scored Opus 5 lower on the seeded-defect corpus at
  roughly 3x the round duration. The corpus and its control set have also grown
  across those runs, and older run records do not state which model they
  measured, so quoting any of them would attribute a number to a configuration
  it may not describe. The confirmation run at the current corpus size:
  2026-08-11, `claude-opus-4-8`, 19 seeded + 10 controls — recall **15/19
  (79%)**, specificity **7/10**. Method, class breakdown and instrument
  discipline are in
  [REVIEWER_RECALL_METHOD.md](REVIEWER_RECALL_METHOD.md); regenerate with
  `nh bench report --reviewer-recall`.
- **The benchmark is self-run and you cannot reproduce it.** There is a harness
  that replays real past tasks through the real pipeline and scores against what
  the human actually did; the committed run is
  [NORTH_STAR_BENCH.md](NORTH_STAR_BENCH.md). Its specs pin to the author's
  local repo paths, so `nh bench run` skips them on your machine. The harness is
  reusable, the corpus is not. Success rate also moves several points between
  runs on identical specs because the coder is non-deterministic, so treat any
  single figure as a point estimate rather than a score. The card now says so
  in its own numbers: `nh bench run --trials N` replays each spec N times, and
  the three surfaces that print the headline in this repo — the `bench run`
  console line, the `bench publish` console line and the published report —
  take it from one function (`success_headline` in
  [`eval/northstar_card.py`](../src/no_human/eval/northstar_card.py)), so none
  of them can print the percentage without its Wilson 95% interval and its `n`.
  The web Stats panel is the one surface that does NOT call it — it renders the
  interval the card recorded, over the API — so it agrees by carrying the same
  fields rather than by construction.
  `pass^N` — the share of specs that passed EVERY trial, which is what
  separates a capability from a coin flip — rides with it above one trial.
  A results file that records neither is refused by `nh bench publish` unless a
  human overrides it, and the override is printed at the top of the report.
  Two honest limits on that interval, because it is easy to over-read:
  it is computed on the **effective** n, not the row count — trials of one spec
  are correlated, so `specs × trials` rows are worth somewhere between `specs`
  and `specs × trials` independent observations and the card discounts them by
  the measured intracluster correlation (a nominal 95% interval over pooled
  rows covered the true rate about half the time). And it bounds SAMPLING error
  only: it says nothing about whether this corpus resembles your work, which is
  the limit the first three sentences of this bullet are about.
- **No dollar figure is a billed number.** Every task carries an enforced spend
  cap, and the cap is denominated in **cost-weighted** tokens, not raw ones: a
  cache read counts 0.1 of a fresh input token and a cache write 1.25
  (`CACHE_READ_WEIGHT` and `CACHE_CREATION_WEIGHT` in
  [`core/pricing.py`](../src/no_human/core/pricing.py); enforced by
  `Orchestrator._check_lifetime_budget` in
  [`core/orchestrator.py`](../src/no_human/core/orchestrator.py), and the
  per-task ledger sums the raw classes for reporting — `compute_metrics` in
  [`core/metrics.py`](../src/no_human/core/metrics.py)). Summing the classes 1:1 measures conversation
  *length* rather than cost — one task was killed at "12.4M/12M tokens" having
  spent about a fourteenth of that in fresh-equivalent terms, which is why the
  cap was re-denominated on 2026-07-31. Cache reads still dominate the traffic:
  in this project's own lifetime measurement over 100 attempts they were
  **95.6%** of all tokens burned ([COST_LEVERS.md](COST_LEVERS.md)) — and even
  at a tenth of the weight they are the largest single line in the bill.
  Tooling that reports "tokens used" without them is reporting roughly 1% of
  the traffic. Real-work
  attempts in that record measure 12k–32k output tokens each, and a one-surface PR takes one
  to three attempts. Any dollar figure derived from that is an estimate, not an
  invoice. `nh logs <id>` shows spend against the cap per task.
- **The reviewer and implementer being different models is a default, not an
  enforced invariant.** You can configure them to the same model. Nothing stops
  you.
- **There is no deploy step.** The pipeline ends at an open PR. Shipping is a
  separate problem and not one this solves.
- **Language coverage is uneven.** `nh onboard` auto-derives a test command for
  pytest, `npm test` and `mvn`
  ([`DeclarationDeriver`](../src/no_human/onboard.py)); anything else you
  configure by hand. The tamper guard reads Python, JS/TS and Java test files.
  The reproduction gate defaults to pytest and routes other ecosystems through
  the project profile's `test_cmd`.

## The merge ban, in code

`gh pr merge`, `glab mr merge` and the equivalent REST calls are denied before
they execute (`_FORGE_MERGE` in
[`agent/guard.py`](../src/no_human/agent/guard.py)) — for the spellings the
matcher models, with the caveat from the reviewer section above, which applies
here in the same words: the modelled set is not closed. The lexical
`_FORGE_MERGE` anchors on `gh pr merge` / `glab mr merge`, so a global option
between the binary and the subcommand walks past IT — which is why the
argv-shaped check exists beside it: `_forge_invocations` reads the resolved
argv, so `gh -R <o/r> pr merge <n>`, `gh --repo <o/r> pr merge <n>`,
`gh pr -R <o/r> merge <n>` and the `glab mr` equivalents are denied in both
session modes. It also recurses into shell-runner wrappers (`bash -c`, `sh -c`,
`timeout`, `xargs`, …) and subshell/grouping heads (`$(...)`, `{ ...; }`), up
to two levels deep — the same bound `_git_invocations` already used — so
`bash -c "gh -R <o/r> pr merge <n>"` is denied, not just the unwrapped
spelling (`tests/test_guard.py::test_a_shell_runner_wrapper_does_not_hide_the_forge_merge`).
That is a raise in the cost of the obvious wrapped spellings, not a closed
door — see security.md's "WHAT THIS RULE IS" for what a command-line guard
structurally cannot see, including nesting past two levels. `glab mr accept`
— glab's own alias for `merge` — is denied too: `("mr", "accept")` is in
`_FORGE_MERGE_PAIRS` in [`agent/guard.py`](../src/no_human/agent/guard.py),
alongside `("pr", "merge")` and `("mr", "merge")`, and the lexical
`_FORGE_MERGE` carries the same alternation. That is one more spelling
modelled, not a closed door — the modelled set is not closed. Treat
the matcher as a cost on the obvious spellings, not as the door: the control
that closes it is a check at the act, not a longer pattern.

Pushes to `main`, `master` and `release/*` are refused too, and that rule has
a second enforcement point, which is the part worth knowing. The first is
`_push_targets_protected` in `agent/guard.py`: it looks for a protected branch
name **anywhere** in the argv of a push, so an option before the subcommand does
not hide it — `git -C . push origin main` is refused by the same rule as
`git push origin main`. But it is still lexical, and lexical analysis cannot
resolve shell expansion: `git push origin $(echo main)` reaches `main` carrying
no token that reads as `main`. A push whose branch comes out of an expansion is
therefore refused outright rather than parsed — measurably so, along with
`B=main; git push origin $B` — and below even that there is a `pre-push` hook
installed into every agent worktree ([`vcs/push_hook.py`](../src/no_human/vcs/push_hook.py)),
which git runs *below* the expansion — it is handed the refspec git has already
resolved, and sees `refs/heads/main` however the command was spelled. That is
what a control at the act looks like, and it is why the protected-branch rule is
on firmer ground than the merge ban above. The default patterns are
`git.never_push_to` in [`DEFAULT_CONFIG`](../src/no_human/config.py). The full
safety model, including the one-billing-path-per-run rule, is in
[security.md](security.md).
