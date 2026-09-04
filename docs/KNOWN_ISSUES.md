# Known issues

Defects that are real, reproduced, and not yet fixed. Each entry says what was
measured, what was ruled out, and what a fix would have to prove. An entry
leaves this file when the defect is fixed, not when it stops being convenient.

---

## KI-1 — concurrent tasks can crash a `Store` commit

**Status:** mitigated in code — every `Store` write now goes through the
`serialized_write` lock (`src/no_human/core/db.py`, landed 2026-07-30), so two
orchestrators can no longer interleave statements on one connection. The test
below stays deselected in CI (`.github/workflows/ci.yml`) because its failure
rate has not been re-measured since the lock landed; the numbers in this entry
are from BEFORE it. Re-measure, then re-enable or close.

**Symptom**

```
sqlite3.OperationalError: cannot commit transaction - SQL statements in progress
  src/no_human/core/db.py:2296 in update_attempt   (await self.db.commit())
  <- src/no_human/core/orchestrator.py:4699 in _run_attempt
```

**This is a product defect, not a test defect.** The traceback is entirely in
shipped code — `Orchestrator._run_attempt` calling `Store.update_attempt` — and
the condition that triggers it, two tasks running at once against one `Store`,
is a supported configuration (`concurrency.enabled: true` with `max_workers`
above 1). A user running two tasks in parallel can lose an attempt to this. The
deselect below keeps the CI badge truthful; it does not make anyone safer. Until
KI-1 is fixed, `max_workers: 1` is the configuration with no known exposure.

The affected test is
`tests/test_scheduler.py::test_two_repos_run_concurrently_in_worktrees`, the
Phase 7 definition-of-done for two tasks in two repos running through the pool
at once. It is the only test that drives two orchestrators against one `Store`
concurrently, which is why it is the only one that trips this.

**Measured failure rate** (2026-07-30, macOS/Darwin 25.5.0 arm64,
Python 3.12.13, aiosqlite 0.22):

| condition                             | failures | measured by             |
| ------------------------------------- | -------- | ----------------------- |
| this test alone, serial, no xdist     | 3 / 8    | this note               |
| this test alone, serial, no xdist     | 1 / 3    | the branch review       |
| whole suite, `-n 4`                   | 1 / 3    | the branch review       |

**It is not an xdist problem.** It fails with no xdist at all, and it fails
running the one test on its own. Lowering the worker count does not help, and
any description of it as "intermittent under `-n 4`" is wrong. The concurrency
that matters is *inside* the test — two `asyncio` tasks sharing one `Store` —
not between pytest workers.

**Mechanism, as far as it has been established**

`Store` holds a single `aiosqlite.Connection`, and `aiosqlite` drives one
`sqlite3` connection from one worker thread. Every coroutine in the process
shares it, including the implicit transaction that `sqlite3`'s legacy
transaction handling opens before a DML statement. When one coroutine issues
`COMMIT` while another statement on that connection is still active, SQLite
refuses the commit with the message above.

**Ruled out.** The obvious candidate — a `SELECT` cursor left unexhausted
across an `await`, of the form `cur = await db.execute(...)` then
`row = await cur.fetchone()` — was instrumented (patching
`aiosqlite.Connection.execute` / `Cursor.fetchone` / `fetchall` / `close` plus a
`weakref.finalize` per cursor) and the set of live read cursors was **empty** at
every failing commit across four captured failures. So the culprit statement is
not one the store code is still holding a Python reference to.

**Lead, not a fix.** Opening the connection in autocommit mode
(`aiosqlite.connect(path, isolation_level=None)`, a one-line change at
`db.py:Store.connect`) took the isolated test from 3/8 failures to **0/12**. That is a
strong signal about where the problem lives, but it is not a fix that can be
adopted on that evidence: it removes multi-statement atomicity from every write
path in the product (`create_attempt`'s `UPDATE` + `INSERT` pair, `_migrate`,
and others), and twelve green runs of one test say nothing about crash
consistency. It is recorded here so the next person does not have to rediscover
it.

**What a fix has to prove**

1. The concurrency test passes at least 10 consecutive serial runs and 10
   consecutive `-n 4` runs. One green run proves nothing about a flake.
2. The full suite stays green.
3. If the fix changes transaction semantics, it says which multi-statement
   writes lose atomicity and why that is acceptable — or it keeps them atomic.

Until then the test is deselected in CI and should be run locally, repeatedly,
by anyone touching `core/db.py` or the scheduler.

---

## KI-3 — `nh serve` cannot drain-and-exit

**Status:** CLOSED 2026-08-09 by `nh serve --until-empty`. Found 2026-08-01 by
the adoption harness, persona "Sam, senior developer", step `serve-help`. The
symptom below is kept as found.

**Symptom.** `nh serve`'s only option is `--max-workers`. It runs until
interrupted. There is no `--once` / `--drain` / `--until-empty`, and therefore
no exit code that says whether the queue drained, whether anything failed, or
whether it stopped early.

**Why it matters.** `docs/quickstart.md` §8 recommends leaving `nh serve`
running overnight, which is fine for a person at a laptop and unusable from
anything automated. To run "work the queue, then stop" — a nightly cron, a CI
job, a benchmark, or an adoption test — the caller has to background the
process, poll `nh status --json` on a timer, and send it a signal. Every
consumer reimplements the same supervisor, and each one invents its own
definition of "done".

**Workaround.** `e2e/adoption/adoption_run.py::run_full_mode` does exactly
that supervision loop and is a working reference for it.

**What a fix has to prove**

1. `nh serve --until-empty` exits 0 when the pending and running lanes reach
   zero, without a signal.
2. It exits non-zero when it stops for any other reason (budget, timeout,
   crash), with the reason on stderr.
3. In-flight tasks still drain on Ctrl-C exactly as they do today.

**What was built, and where it differs from that list.** `--until-empty` sets
the same `stop` event a signal sets (`Scheduler.run_forever`), so there is one
shutdown path, not two, and (3) is untouched — the default `nh serve` has no
new behaviour at all. Exit 1 is keyed on `task.status == FAILED` for the ids
this process dispatched (`Scheduler.failed_dispatched`), or on the drain being
cut short by a signal with work still claimable (`queue_is_drained`); the
reason goes to stderr. It deliberately reads **narrower** than (2): a task
awaiting input, escalated, blocked or quota-paused exits 0, because those are
the honest off-ramps the loop is built to produce and failing the batch on them
would make a non-zero exit meaningless. A caller who needs "did anything park"
reads `nh status --json`; only a FAILED task is a failure.

*(Amended 2026-08-09, same day, by `budget.exhaustion_terminal`.* The paragraph
above originally read "a task parked BLOCKED **for budget**, awaiting input …
exits 0". That is no longer true and the change is in (2)'s favour: an exhausted
lifetime budget now ENDS the task in FAILED rather than parking it, so
`--until-empty` exits **1** on it, which is exactly the "budget" clause of (2).
Nothing about `--until-empty` changed — the same one field, `task.status`, is
still read; what changed is which status a budget cross produces.)

*(Amended 2026-08-22, follow-up to an independent review of PR #585.* `queue_is_drained`
read `_inflight` and `_claimable()` but not a third case: a mid-run row
(`CONTEXT`/`PLANNING`/`REVIEWING`/`TESTING`) that no worker in *this* process
owns — a crash orphan, or a row a sibling process is or was driving — and that
is younger than `_STRANDED_GRACE_S` (900s), so `_recover_orphans` won't touch
it yet either. Nothing was live, nothing was claimable, so the old check
reported `0` — "drained" — while that row was still mid-run. Exit 1 still
means FAILED-dispatched, or a signal that cut off claimable work — but it is
NOT unchanged: the stranded check runs BEFORE the signal check, so a run that
was signalled AND left a stranded row now exits 2, where it used to exit 1.
The ordering is deliberate (a stranded row makes `queue_is_drained` false, so
checking the signal first would report "signalled" for a run nothing
signalled), and the cost is that exit 2's message names only the stranded row:
it does not say a signal cut the run short, nor that claimable work remains.
Read exit 2 as "the queue state is unknown", not as "wait 900s and re-run".
A **new exit 2** means neither of exit 1's cases: the queue is not drained, it is
*unknown*, because a row exists that this process cannot claim and cannot yet
prove abandoned. `--until-empty` does not wait out the grace to find out — it
exits 2 immediately, naming the task id and the seconds remaining until the
row becomes claimable (once the grace lapses, an orphan-recovery sweep picks it
up — but not from this run, which has already exited, and which was refused
permission to boot beside a live sibling scheduler: recovery arrives with the next
scheduler boot, or from the third process that owns the row if one does).
Automation that treated exit 0 as "safe to tear down the pool" was the actual
bug this closes; `_STRANDED_GRACE_S`,
`_row_is_live`, and `_recover_orphans` are untouched — only `queue_is_drained`
and the `--until-empty` exit path gained the missing case.)

Separately, `nh stop --timeout` defaulted to 3s — shorter than one
Agent SDK turn, so it SIGKILLed the drain SIGTERM had just requested — and now
defaults to `concurrency.stop_grace_s` (60s) plus a 15s margin, i.e. 75s.

---

## KI-4 — `nh onboard` reports a failed command without its output

**Status:** open. Found 2026-08-01 by the adoption harness, persona "Sam",
step `onboard-proving-opacity`.

**Symptom**

```
proving (running each candidate):
  ✗ [FAILED] test: pytest -q  (from python/pytest, exit 1)
...
test command NOT proven — profile is not usable until it runs clean. Nothing
faked; fix the repo or its declarations and re-run.
```

The exit code is shown; not one line of the command's own stdout or stderr is.
The user cannot tell whether the cause was a missing dependency, an import
error, a collection error, or a genuinely failing test.

**Why it matters.** Onboarding is the first thing every user does after
install, and refusing to confirm an unproven test command is *correct* — the
message even says so well. But a correct refusal with no diagnostic is where a
new user stops. In the run that found this, the cause was a one-line fixture
problem that the captured stderr would have named immediately.

**What a fix has to prove**

1. A failed proving candidate prints the last N lines of its combined output,
   attributed to the command.
2. Output is truncated, not unbounded — a 10,000-line pytest failure must not
   flood the terminal.
3. Nothing secret is echoed: the proving subprocess inherits the process env,
   which by then holds loaded `.env` values.

---

## KI-5 — a misconfigured CI backend silently becomes "no CI gate"

**Status:** CLOSED 2026-08-02. Found 2026-08-01 by the adoption harness
(`e2e/adoption/`), persona "Marco, DevOps", step
`ci-misconfig-is-not-silently-no-gate`. The history below is kept because the
shape of this defect — a fix that lands, is believed, and leaves the thing it
was about — is worth being able to re-read.

**Symptom (as found).** `ci_from_config` returned `None` — not an error — when
`ci.enabled` was true but the selected backend's required key was absent.
Measured, by calling the real function:

```
gitlab_missing_project        -> None (NO GATE)
jenkins_missing_job           -> None (NO GATE)
circleci_missing_slug         -> None (NO GATE)
github_actions_missing_repo   -> None (NO GATE)
typo_in_backend_name          -> raises ValueError
disabled                      -> None   (correct: the operator said no)
```

`Orchestrator._run_attempt` reads `self.ci_runner is None` as "no remote CI is
wired for this repo" and proceeds with the local suite as the only gate. The
`ValueError` case is worse: `orchestrator.py` catches it into a `log.warning`,
so a misspelled `ci.backend` produced nothing on any surface a user looks at.

**Why this is the one that matters.** getnohuman.com advertises "Jenkins &
CircleCI — test layers can run on your CI, and the results gate the loop." A
user who sets `ci.enabled: true`, gets one key wrong — easy, since until today
the per-backend keys were undocumented — receives no error, no blocker, and no
event, and their tasks open PRs having never been gated on CI. They believe
they have a gate. The failure is invisible precisely to the person relying on
it.

**What was fixed, and what deliberately was not.** The silence is fixed, and
by a wider fix than this entry originally described. `ci_backend_unavailable`,
the event this branch added, is gone: it was guarded on `prof.ci.get("enabled")`
and nothing in `onboard.py` or `profile.py` ever writes an `enabled` key, so it
could not fire for anybody. `Orchestrator._resolve_ci_runner` (2026-08-02)
replaced it. A source that asks for CI and cannot produce a backend now emits
an `advisory` naming the origin and the reason — counted by `nh doctor` under
`advisory_degradations` — and `doctor.py::ci_config_problems()` reports the
same condition statically, which matters because this failure mode leaves no
events at all on a run that never happens. The `ci_skipped` event no longer
claims "no remote CI configured" — false in exactly this case — but says "no
remote CI ran".

That fix also closed the wider hole this entry used to be bounded by: the
global `ci:` block documented in `docs/configuration.md` was read by nothing,
so a user who configured CI the documented way got no gate and no warning.
Both production routes to a backend were inert. They are wired now, with
stated precedence (injection > profile > global config).

**The part that was deferred, and is now done (2026-08-02).** Making the
failure visible did not make it BINDING: `_resolve_ci_runner` left
`self.ci_runner = None`, and the only reader of that means "no remote CI is
wired for this repo, the local suite is the only gate" — so the run still
completed and still opened an ungated PR. A user who mistyped one key got
exactly the run a user who deliberately declined CI gets. Two changes closed
it, at the two different altitudes the defect lived at:

* `ci_from_config` now returns `None` for EXACTLY ONE reason — CI is switched
  off — and raises `CIMisconfigured` (a `ValueError`, carrying `.backend` and
  `.missing`) for every other way to fail, including an unknown `ci.backend`.
  A sentinel or a second return value would have removed the ambiguity while
  leaving the DEFAULT wrong: both need every caller, forever, to remember a
  check, and forgetting is invisible. An exception cannot be defaulted past.
  All five backends had the identical hole and all five are closed the same
  way (gitlab/`project`, github_actions/`repo`, jenkins/`job`,
  ghe_checkruns/`repo`, circleci/`project`).
* `_resolve_ci_runner` returns the reason the run has no gate, and `_drive`
  escalates on it with an `IMPOSSIBLE` blocker naming the exact key. It sits
  at the TOP of `_drive`, above the first metered call — above
  `_gather_context`, the intake evaluator, `_run_intake_grill` and the MoA
  `_generate_plan`. That placement is the point, not a detail: the escalation
  is deterministic, so anywhere below it a broken `ci:` block buys a full
  planning round on every run and every retry before saying the one thing it
  knew at second zero. Knowing costs `_usable_profile` — a SQLite row and a
  `project.yml` read, no LLM. Fires only when EVERY claiming source failed
  (one working source is a working install) and only for task kinds that can
  open a PR — a standalone `code_review`, `investigation` or `design_doc`
  produces a document, never a PR, so a missing gate cannot make it dishonest.

**What the fix proves**

1. ✅ `ci.enabled: true` with an unbuildable backend does not reach `open_pr`.
   It escalates with a blocker naming the missing key.
   (`test_ci_enabled_without_a_target_does_not_open_an_ungated_pr`, both
   gitlab and github_actions, through `run_task`.)
2. ✅ `ci.enabled: false` still proceeds silently on the local suite — the
   operator declining CI is not an error and must not become one.
   (`test_ci_deliberately_disabled_still_opens_a_pr`, the control that decides
   whether the fix was safe to ship at all.)
3. ⬜ An unknown `ci.backend` is still rejected at ATTEMPT time, not at config
   load. It now escalates instead of proceeding ungated, so the run is no
   longer spent — but the user still hears about the typo one run late.
   Config-load validation remains open and is a separate change.
4. ✅ The existing zero-config path (no `ci` block at all) is untouched: it
   still emits `ci_skipped` and proceeds.
   (`test_no_ci_block_at_all_stays_silent_and_proceeds`.)

**What is STILL open, and it is the ticket's own sentence.** The escalation
covers the global `ci:` block. It does **not** cover a **project profile**
whose `ci` block names no pipeline target — including the literal case "a user
enabled CI and mistyped `project`", if they mistyped it in the profile:

```
profile ci={'backend':'gitlab'}                     -> no source, no advisory, UNGATED
profile ci={'backend':'gitlab','project':''}        -> no source, no advisory, UNGATED
profile ci={'backend':'gitlab','projct':'a/b'}      -> no source, no advisory, UNGATED
```

`_resolve_ci_runner` admits a profile as a source only when one of
`project`/`repo`/`job` is non-blank, so none of these reaches
`ci_from_config`, and `doctor.ci_config_problems` reads only the GLOBAL block,
so `nh doctor` is silent on it too.

The gating is defensible and predates this fix: `nh onboard` writes a bare
`{"backend": "gitlab"}` the moment it sees a `.gitlab-ci.yml`, and a detection
hint is not a request for a gate — treating it as one would escalate every
onboarded GitLab repo on its first run, which is a worse failure than the one
being fixed. The real cure is for onboarding to record INTENT (this repo's CI
is to be driven) separately from DETECTION, which is a different change.

It is pinned rather than left incidental, in both directions:
`test_profile_ci_with_no_target_and_no_global_block_is_not_a_source`
(tests/test_ci.py, five profile shapes) and
`test_profile_ci_hint_with_no_global_block_still_opens_a_pr`
(tests/test_e2e_orchestrator.py). Both should FAIL when someone closes this —
that is what they are for. Without them, widening `_CI_TARGET_KEYS` would park
every onboarded repo with the whole suite green.

**Verified to work, in the same run** (real adapters, local fakes — see the
boundary note below): for both Jenkins and CircleCI, a green pipeline yields
`passed=True`, a red one `passed=False`, a 401 sets `access_failure` with the
correct `.env` key named (`JENKINS_USER` / `CIRCLECI_TOKEN`), and a 503 sets
`infra_failure`. None of the failure modes ever produced a passing verdict. So
the gate's *verdict* logic is sound; it is the *wiring* that can vanish.

**Boundary.** All of the above was measured against local HTTP fakes on
127.0.0.1, driving the real `JenkinsCI` and `CircleCICI` adapters (Jenkins over
its real `curl` transport at a configured `base_url`; CircleCI with only the
module-level `_API` constant redirected). **No live Jenkins or CircleCI
instance was contacted.** These results say nothing about either vendor's real
auth, scopes, rate limits or payload shapes. A live smoke test against one real
instance of each remains unperformed and is the obvious next step.

