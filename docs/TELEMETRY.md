# Telemetry contract

This is the canonical, machine-checked contract for every server-side
telemetry event `no_human` can ever send. It exists so a reviewer (human or
test) can answer "what can this event possibly carry" without reading
`src/no_human/telemetry.py` line by line. Two tests in `tests/test_telemetry.py`
keep this file and `docs/configuration.md` pinned to the code's actual closed
allowlist (`_ALLOWED_EVENTS`) in both directions —
`test_every_server_event_kind_is_documented` and
`test_documented_list_has_no_phantom_events` — so an event can never ship
undocumented, and a documented event can never survive being removed from
the code.

See also `docs/configuration.md`'s "Usage insights" section (the
user-facing summary of the same table) and `docs/security.md` (the
security-review framing of this same channel).

## The complete event list

There are exactly thirteen possible event kinds, twelve sent by the server
and one (`screen_viewed`) by the browser.

| Event | Channel | Props |
|---|---|---|
| `app_started` | server | `environment` |
| `task_created` | server | `source`, `environment` |
| `task_completed` | server | `status`, `duration_bucket`, `attempts`, `environment` |
| `task_failed` | server | `category`, `reason_category`, `environment` |
| `approve_clicked` | server | `environment` |
| `feature_used` | server | `name`, `environment` |
| `task_ended` | server | `outcome`, `attempts`, `duration_bucket`, `environment` |
| `tasks_orphaned` | server | `count_bucket`, `environment` |
| `onboarding_step_viewed` | server | `step`, `environment` |
| `onboarding_repo_selected` | server | `environment` |
| `onboarding_completed` | server | `path`, `environment` |
| `task_create_refused` | server | `reason`, `environment` |
| `screen_viewed` | browser | `screen` (the lane name — `board`/`backlog`/`done`/`failed`/`stats`/`settings`/…, never content) |

Every prop name is validated against `_ALLOWED_EVENTS`; an unknown kind or
prop name raises `ValueError` in `telemetry.record()` (never silently
dropped — an unlisted event/prop is a privacy bug, not an operational
hiccup).

## Closed value enums

A handful of props are additionally VALUE-validated (`_ALLOWED_PROP_VALUES`)
against a closed enum — never free text, so no task id, title, repo name,
path, prompt, or failure detail can ever leave the machine through them.

- `task_failed.reason_category` — `FAILURE_REASON_CATEGORIES`: one of
  `budget_exhausted`, `review_failed`, `max_attempts`, `infra`,
  `tamper_blocked`, `blocker_parked`, `other`.
- `task_ended.outcome` — `TASK_END_OUTCOMES`: one of `escalated`,
  `parked_quota`, `parked_infra`, `needs_answer`, `cancelled`.
- `task_ended.duration_bucket` — `DURATION_BUCKETS`: one of `<10m`,
  `10-30m`, `30-60m`, `>60m`, `unknown`. (`task_completed.duration_bucket`
  is produced by the same `duration_bucket()` helper but is intentionally
  NOT value-validated — `task_completed` is a pre-existing event kept
  byte-identical by this change.)
- `tasks_orphaned.count_bucket` — `ORPHAN_COUNT_BUCKETS`: one of `0`, `1`,
  `2-5`, `6+`.
- `onboarding_step_viewed.step` — `ONBOARDING_STEPS`: the wizard's own step
  keys (`api/app.py`'s `_WIZARD_STEPS`, kept equal to `Onboarding.jsx`'s
  `BASE_STEPS` by
  `tests/test_onboarding_funnel_telemetry.py::test_step_key_matches_the_wizards_own_list`,
  which parses `BASE_STEPS` out of `Onboarding.jsx`'s own source rather than
  trusting a separate literal to stay in sync).
- `onboarding_completed.path` — `ONBOARDING_PATHS`: one of `minimal`, `full`.
- `task_create_refused.reason` — `TASK_REFUSAL_REASONS`: one of
  `setup_mode`. Only this machine-readable reason ships; the human-facing
  `SETUP_MODE_DETAIL` string (which carries a filesystem path) never does.

## `task_completed`: the ordinary successful-delivery path

`task_completed` fires the first time a task reaches `awaiting_approval` or
`done`, via either of the two real code paths that carry that status:

- `kind == "pr_open"` with `status="awaiting_approval"` riding along — the
  ORDINARY path, fired by `_open_pr` the moment a PR is actually opened (and
  also by the already-satisfied/draft-promotion leg, which carries the same
  `status`). This is how most tasks reach `task_completed`: opening the PR
  *is* the delivery event, there is no separate `kind=="state"` event for
  this leg.
- `kind == "state"` with `status` in `done`/`awaiting_approval` — the
  orchestrator's own done legs (`_run_attempt`'s and the code-review
  paths). `nh approve` is NOT one of them: it writes `DONE` through the
  store (`api/app.py`), where no `Orchestrator` exists, so it emits
  nothing and fires no `task_completed`.

A `pr_open` emit with no `status` kwarg at all (e.g. the linked-repo PR
follow-up emit) does not match and does not fire `task_completed`. Both
branches share one `_tel_terminal_sent` latch (below), so a `pr_open`
delivery followed later by one of the orchestrator's own `state`/`done`
emits on the same instance fires `task_completed` exactly once, not twice. Props (`status`,
`duration_bucket`, `attempts`) are read identically regardless of which kind
carried the status — the schema is unchanged by which kind triggers it.

## `task_ended`: every non-done/non-failed task end

`task_failed` fires only for the `kind=="failed"` off-ramp. Every OTHER way
a task stops — escalated to a human, parked on quota or transient infra,
waiting on a human answer, or cancelled — used to emit nothing, so the
north-star funnel (created → reviewed PR) could not tell "still in
progress" from "abandoned". `task_ended` closes that gap: it is emitted
exactly once per task end (guarded by the same `_tel_terminal_sent` latch
`task_completed`/`task_failed` already use — first writer wins) from
`Orchestrator._telemetry_hook`, the one server-side telemetry sink, mapping
the emit kind (and, for the ambiguous `blocked` kind, `blocker_category`)
onto exactly one `outcome`:

| emit `kind` | `blocker_category` | `outcome` |
|---|---|---|
| `escalated` | — | `escalated` |
| `paused_quota` | — | `parked_quota` |
| `awaiting_input` | — | `needs_answer` |
| `cancelled_hard` | — | `cancelled` |
| `blocked` | `TRANSIENT_INFRA` / `DEPENDENCY_WAIT` | `parked_infra` |
| `blocked` | anything else / missing | `needs_answer` (safe default: a human is being waited on) |

`cancelled_hard` is a genuine task end: a human explicitly cancelled a live
attempt (`Orchestrator.request_task_cancel` tearing down
`_active_backend_task`), emitted from `_run_attempt`'s `CancelledError`
branch. This is distinct from plain `kind="cancelled"`, which
`_honor_cancel` emits for the COOPERATIVE PAUSE path (`nh task cancel` on a
task that yields at its next checkpoint rather than being torn down mid-
attempt) — that path sets the task to `BLOCKED`, is resumable via `nh task
resume`, and is *not* a member of `_TASK_END_KINDS`, so it never reaches
`task_ended`. Both real code paths (a hard cancel mid-attempt, and cancelling
a queued/parked task with no live attempt at all — via the shared
`telemetry.record_task_cancelled` helper called from both the
`POST /api/tasks/{id}/cancel` handler and `nh task cancel`'s direct-write
branch, whichever one actually observed no live in-process session) are
covered by tests that drive the real cancel entry points, not a synthetic
kind fed straight into `_telemetry_hook`.

`interrupted` is deliberately NOT an outcome. An app/server closed mid-run
cannot emit anything at the moment it dies — there is no code left running
to emit it — so that case is instead detected and reported, once per server
start and in aggregate, as `tasks_orphaned` (below), never as an immediate
`task_ended`.

Known, accepted limits: a task parked and later resumed by a *fresh*
orchestrator process has no in-memory `_tel_started_at`, so its eventual
`task_ended.duration_bucket` reads `"unknown"` rather than a real bucket.
An out-of-process cancel (no live `Orchestrator` instance to ask —  a
queued/`PENDING` task, or a hard cancel whose owning process is gone)
bypasses `emit`/`_telemetry_hook` entirely by construction, so
`telemetry.record_task_cancelled` recomputes `attempts` and
`duration_bucket` straight from durable storage (`store.count_attempts`,
`task.created_at`) instead; that recompute is a real read-path, not a
guess, but it means an unparsable/missing `created_at` degrades to
`duration_bucket="unknown"` rather than failing the cancel itself
(fail-open, like every other telemetry call).

## `tasks_orphaned`: the app/server-closure case

When the app or server process is killed mid-run, no code is left running
to emit a terminal event for the tasks it was mid-run on. Instead, on the
NEXT server start, `no_human.api.app` counts every mid-run task
(`no_human.core.scheduler.MID_RUN_STATUSES`: `context`, `planning`,
`implementing`, `reviewing`, `testing`) that has NO live attempt — either
its open attempt's heartbeat (the newer of the task row's `updated_at` and
its newest persisted `task_event`, exactly the liveness signal
`Scheduler._row_is_live` already uses elsewhere) is older than
`Scheduler._STRANDED_GRACE_S` (900s), or it has no open attempt at all
because its latest attempt was closed `interrupted` by a graceful stop
(`_honor_server_stop`) — that second case carries no heartbeat to age, so
it counts immediately.
That count is bucketed (`telemetry.orphan_bucket`) and sent as ONE
`tasks_orphaned` event, always — including `count_bucket="0"` when nothing
was found dead, since a zero is itself a meaningful metric state (it says
the previous shutdown was clean). This runs once per server process, before
the scheduler's own recovery sweep starts, so the count reflects what was
actually found dead rather than what the sweep has already fixed up.

`tasks_orphaned` is read-only: `count_dead_attempt_tasks` never mutates a
task, attempt, or event — it does not recover, requeue, or touch anything
the scheduler's existing crash-recovery path (`_recover_orphans`) is
responsible for. It is purely an observability count layered on top of
unrelated, unchanged recovery behavior.

## The `_LAMBDA_EVENTS` wire filter

The default destination is PostHog, which accepts everything in
`_ALLOWED_EVENTS` immediately. The optional first-party ingestion Lambda
(`telemetry.endpoint`) validates a batch WHOLESALE against ITS OWN closed
allowlist and 400s the entire batch on one unrecognized event name — and a
rejected batch stays queued forever, wedging every later flush behind it.
`task_ended`, `tasks_orphaned`, and the four onboarding-funnel events
(`onboarding_step_viewed`, `onboarding_repo_selected`, `onboarding_completed`,
`task_create_refused`) are new: none of them have shipped to the Lambda's
server-side allowlist yet. Until they do, `telemetry.flush()` drops any
event whose name is not in `_LAMBDA_EVENTS` on the `kind == "lambda"` wire
path only — never affecting PostHog, and never wedging the queue (an
all-dropped batch is deleted, never re-POSTed empty).
`tests/test_telemetry.py::test_client_allowlist_matches_the_deployed_lambda_contract`
pins `_LAMBDA_EVENTS` as the Lambda's deployed six names and asserts it is a
strict subset of `_ALLOWED_EVENTS` — this file (and that test) must be
updated together the day the Lambda actually ships any of the new ones.

## The onboarding funnel

`app_started` and `task_created` alone could not tell "abandoned the wizard"
from "never opened the app", or "closed the tab" from "hit a wall and gave
up" — 276 of 291 sampled installs emitted exactly one event ever, with
nothing recorded in between. Four events close that gap:

- `onboarding_step_viewed` fires when the wizard shows a step, via
  `POST /api/onboarding/step` (`api/app.py`). The server checks `step`
  against its own closed `_WIZARD_STEPS` and 422s an unlisted value rather
  than ever passing it to telemetry — a stale or tampered client can't
  smuggle free text through this route.
- `onboarding_repo_selected` fires once an install has onboarded at least
  one repo — once per INSTALL, not once per repo (a per-repo count would
  itself be a cardinality leak).
- `onboarding_completed` fires when the wizard finishes, carrying which path
  was taken (`minimal` or `full`).
- `task_create_refused` fires when `POST /api/tasks` refuses to create a
  task for lack of credentials — the reason is always the fixed string
  `setup_mode`; the human-facing detail string is never read for this
  purpose.

All four are latched to fire at most once per install (`_record_onboarding_once`
in `api/app.py`, keyed under `config.onboarding.funnel_once`, distinct
markers per step/repo-selection/completion/refusal) so a user revisiting a
step, or onboarding several repos, does not inflate the count.

Desktop first-launch blind spot (documented, not fixed): `desktop/main.mjs`'s
pre-credential native setup screen returns before `ensureServer(...)` ever
boots a server, so no server-side telemetry — not even `app_started` — is
reachable on that path. Out of scope for this change; pinned by
`tests/test_onboarding_funnel_telemetry.py::test_desktop_first_launch_gate_is_documented_as_uninstrumented`.

## Never sent

No task id, title, repo name, file path, prompt, spec, diff, or credential/
token ever appears in a server event. Every prop above is either a
structural count (`attempts`), a bucketed value (`duration_bucket`,
`count_bucket`), or a value from a closed enum (`status`, `category`,
`reason_category`, `outcome`, `source`, `name` on `feature_used` — itself a
closed set of feature identifiers, not a title). `environment`
(`real`/`bench`/`test`/`ci`/`dev`) is stamped on every event by
`telemetry.environment()` so real installs stay countable amid bench/pytest/
CI dogfood volume; it never suppresses an event, only tags it.
