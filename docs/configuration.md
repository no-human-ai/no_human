# Configuration

## Settings at a glance

The settings most installs touch. This table moved off the README on 2026-08-01
(the front page now links here instead of restating it); it is pinned to
`config.DEFAULT_CONFIG` by `tests/test_readme_claims.py`, so a default that
changes in code and not here fails the suite.

| Setting | Default | What it does |
|---|---|---|
| `llm.auth_mode` | `subscription` | `subscription` (OAuth) or `api_key` (your own key) |
| `llm.primary_model` | `claude-sonnet-5` | The implementer |
| `llm.review_model` | `claude-opus-4-8` | The fresh-context reviewer |
| `llm.review_timeout_seconds` | `1500` | Wall-clock seconds one reviewer session gets before it is cut off. Raise it if reviews time out; a review round measured ~1078s (worst 1357s) on the Opus reviewer tier |
| `llm.code_review_timeout_seconds` | `1800` | The same wall for `code_review` mode, which reads a whole PR diff at twice the gate's cap |
| `bounds.max_attempts` | `3` | Implement/review cycles in one loop |
| `bounds.max_turns_per_attempt` | `500` | Agent turns before an attempt is cut off |
| `bounds.min_viable_attempt_weighted_tokens` | `250000` | The cost-weighted floor the loop-head gate refuses to START an attempt under: an attempt's startup alone (re-accumulated context, implement prompt, skills, map) costs ~110–160k weighted before its first real turn, so a remaining lifetime budget below this can only buy a turn-0 budget-abort. Used only when the task has no measured history — a real prior attempt's first-10-message `cache_burn` figure is preferred. Refusal parks the task `BUDGET_EXHAUSTED` naming both numbers, distinct from the over-cap refusal |
| `server.port` | `8420` | Web board bind port |
| `concurrency.enabled` | `false` | Parallel task workers, each in its own worktree |
| `concurrency.stop_grace_s` | `60` | Seconds a stopping server (`nh stop`, SIGTERM) waits for running attempts to checkpoint (`[WIP-PARTIAL]` commit + `resume_from`) and unwind before exiting anyway. `nh stop --timeout` defaults to this plus 15 |
| `worker.backend` | `claude` | Which coding backend the IMPLEMENTER runs on: `claude` (the Claude Agent SDK), `codex` (the OpenAI Codex CLI, on your own `OPENAI_API_KEY`) or `local` (the Claude Agent SDK pointed at an Anthropic-compatible server on loopback/RFC1918). By default only the coder moves — planner, supervisor, utility and intake stay on Claude unconditionally; the reviewer stays on Claude too unless an explicit `llm.role_backends.reviewer` Settings choice overrides it (constraint §6d, below). One task can override the coder: `nh task add --backend codex` (or `backend` on `POST /api/tasks`). See [BACKENDS.md](BACKENDS.md) |
| `worker.abort_non_converging` | `true` | Turn-cap convergence early-abort: fail an attempt that keeps calling tools (varied reads/greps, never a repeat) but has made no file edit and run no test for `worker.convergence_window_turns` turns, once past `worker.convergence_check_after_turns`. Checkpointed `[WIP-PARTIAL]`, exactly like a hard doom-loop abort — the bounded loop retries with fresh context; `false` restores pre-P2 behaviour, where only `bounds.max_turns_per_attempt` and the hard stuck tiers can end an attempt |
| `worker.convergence_check_after_turns` | `80` | Turns before the convergence check applies at all — below this, exploring with no edit yet is normal |
| `worker.convergence_window_turns` | `40` | Turns since the last file edit or test run that count as stalled, once past `convergence_check_after_turns` |
| `ci.enabled` | `false` | Trigger and poll GitLab CI, GitHub Actions, Jenkins or CircleCI |
| `pipeline.review_routing.enabled` | `true` | Review depth scales with diff size — see below |
| `pipeline.review_routing.max_diff_lines` | `200` | The single-turn-gate threshold, in added+deleted lines |
| `usage_ledger.retention_days` | `90` | Age past which `unattributed_usage` rows are rolled up (not deleted — totals stay exact, per-row `ts`/`task_id` detail is lost); `0` disables compaction |
| `approve_merge.enabled` | `true` | Whether `nh approve` lands the PR itself — squash the branch into one commit, push the default branch, close the PR. `false` records the approval and leaves the merge to you; neither is a failure |
| `approve_merge.test_timeout_seconds` | `1800` | Wall-clock seconds the change-scoped test run that gates a landing gets. Exceeding it fails the landing at the test step, so nothing is pushed |
| `approve_merge.full_test_timeout_seconds` | `5400` | Wall-clock seconds the FULL suite gets when it runs instead of the change-scoped one — squash result's tree diverges from (or is unknown relative to) the attempt's recorded tested tree. Exceeding it fails the landing at the test step, so nothing is pushed |
| `review.post_checklist_comment` | `true` | Post the independent reviewer's checklist (verdict, rounds, every finding with severity and `file:line`) as its own PR comment, once per commit. `false` skips posting — the checklist still lives in the DB and the PR body's one-row summary, just not as its own comment |

Review depth scales with diff size: a gate review of a diff at or under
`max_diff_lines` changed lines runs SINGLE-TURN, no tools — the diff, the full
text of every changed file, lint and wiring evidence are already in the
prompt, so the exploration turns buy nothing. Any diff containing a
risk-flagged pattern ALWAYS gets the full multi-round review regardless of
size: a guard/scrub function touched (detected by path AND by diff content,
so a guard function in an otherwise generic file — e.g. `install_pre_push_guard`
in `vcs/push_hook.py` — is still caught), a test file deleted or renamed away,
or a security-sensitive path (`auth`, `crypt`, `secret`, `credential`, `token`,
`key`, `.env`, `config.yaml`, `config.py`, `.github/workflows/**`,
`.githooks/**`) — as does a diff too big to measure (binary) or a re-review
after a prior round already failed. `enabled: false` restores the
pre-2026-08-14 behaviour (every gate review is full). See
`src/no_human/core/review_routing.py`.

Concurrency ships off, and `concurrency.max_workers` defaults to 2 when you turn
it on. In the default `subscription` mode a present `ANTHROPIC_API_KEY` aborts
startup rather than being silently ignored
([`assert_subscription_mode`](../src/no_human/config.py)) — silently scrubbing it would hide
a misconfiguration that costs real money. In `api_key` mode the reverse holds:
your key is the billing path and every *other* metered route is scrubbed, so a
run bills exactly one thing and records which.

Config lives at `~/.no_human/config.yaml`, auto-generated with defaults on first
run. The user's values are deep-merged over the defaults. The metered
`ANTHROPIC_API_KEY` must never appear here — in **every** `llm.auth_mode`,
including `api_key` mode (below), the key itself lives only in `~/.no_human/.env`,
never in `config.yaml` (loading rejects it). Only the auth *mode* is a config value.

Secrets live separately in `~/.no_human/.env` (`chmod 600`, gitignored). They are
loaded into the process env on startup, never read from or written to the repo,
and never logged or echoed.

## `~/.no_human/.env` keys

`nh onboard <repo>` derives which keys a given repo needs (from its CI backend +
VCS host) and prints a present/✗-missing checklist. When a task hits a missing
credential at runtime, no_human escalates a `MISSING_ACCESS` blocker naming the
**exact** key to set — set it, then `nh reply` to resume.

| Key | When you need it |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Default `llm.auth_mode: subscription` — **required**, the coding backend's subscription auth. Create with `claude setup-token`. |
| `ANTHROPIC_API_KEY` | Only when `llm.auth_mode: api_key` — the operator's own metered key, BYO-API-key billing (see below). Never set otherwise. |
| `OPENAI_API_KEY` | **Required** when the coder runs on the `codex` backend (`worker.backend: codex`, or a task's `--backend codex`) in the default `llm.codex_auth_mode: api_key`. Not needed in codex `subscription` mode, where a `codex login` ChatGPT session pays instead. See [BACKENDS.md](BACKENDS.md). |
| `LOCAL_LLM_API_KEY` | Only when the coder runs on the `local` backend (`worker.backend: local`) **and** your local server enforces a key — otherwise omit it. Never goes in `config.yaml`. See [BACKENDS.md](BACKENDS.md). |
| `JIRA_API_TOKEN` | Jira intake (`integrations.jira.enabled: true`). An Atlassian Cloud API token; auth is HTTP Basic `integrations.jira.email` + this token. See [adapters.md](adapters.md#jira). |
| `LINEAR_API_KEY` | Linear intake (`integrations.linear.enabled`). Create at Linear → Security & access settings. |
| `MONDAY_API_TOKEN` | monday.com intake (`integrations.monday.enabled`). Create at monday.com → Administration → Connections → API. Sent raw as `Authorization`, not `Bearer`. See [adapters.md](adapters.md#mondaycom-specifics). |
| `JENKINS_USER`, `JENKINS_API_TOKEN` | Repos whose CI is Jenkins (`build.example.com`) or human-gated on a `Jenkinsfile`. Basic auth — the default `ci.auth: token` mode. |
| `SSO_USERNAME`, `SSO_PASSWORD` | Jenkins controllers that reject API-token basic auth, i.e. `ci.auth: cookie`. Used once to capture a session cookie. |
| `CIRCLECI_TOKEN` | `ci.backend: circleci`. A CircleCI personal API token; sent as the `Circle-Token` header. |
| `GITLAB_TOKEN` | Repos whose CI backend is GitLab, or whose VCS host is a GitLab. |
| `GH_ENTERPRISE_TOKEN` | Opening PRs against a GitHub-Enterprise host (e.g. `code.example.com`). Public `github.com` uses `gh auth login` instead. |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Only for the opt-in Slack Socket-Mode worker (`integrations.slack.intake`). **The worker connects but does not yet create tasks from mentions — the intake handler is not wired in `nh serve`.** Unrelated to notify-out. |

> **The notify-out webhooks are NOT `.env` keys.** Slack's and Teams' webhook
> URLs live in `config.yaml` under `notifications.slack_webhook_url` and
> `notifications.teams_webhook_url` — that is where the code reads them from
> (`notify/build_notifier`). They are still treated as secrets: never echoed
> back by the settings UI, and scrubbed from `/api/config`. Earlier revisions of
> this table listed a `SLACK_WEBHOOK_URL` env var and a `TRACKER_TOKEN`; **no
> code has ever read either one.** They are removed rather than reworded.

In the default `subscription` mode, `ANTHROPIC_API_KEY` and any Bedrock/Vertex
var must **never** appear here — they are scrubbed on startup and a present
`ANTHROPIC_API_KEY` aborts the run (it would silently bill metered API). In the
opt-in `api_key` mode, `ANTHROPIC_API_KEY` is instead the one **chosen** billing
path and is kept; every *other* metered redirect (`ANTHROPIC_AUTH_TOKEN`,
Bedrock, Vertex) is still scrubbed so a run only ever bills one path. In both
modes the key lives only in this `.env` file, never in `config.yaml`.

```yaml
server:
  host: 127.0.0.1                 # requests must address this host or loopback;
  port: 8420                      # any other Host header is refused (400) — docs/security.md §7

llm:
  auth_mode: subscription         # subscription (default) | api_key — see auth modes below
  primary_model: claude-sonnet-5  # implementer (coder)
  review_model: claude-opus-4-8   # fresh-context reviewer + eval judge (different model)
  review_timeout_seconds: 1500    # wall-clock per review session; a round that
                                  # dies on this wall escalates UNREVIEWED
  code_review_timeout_seconds: 1800  # same, for `nh review` on a whole PR diff
  local_model: null              # see llm.local_* below
  local_base_url: null            # e.g. http://localhost:8000
  local_cli_path: null            # null ⇒ the SDK-bundled CLI

database:
  path: ~/.no_human/no_human.db   # SQLite (WAL). No Postgres/Redis.

notifications:                    # write-only notify-out; null = log only
  slack_webhook_url: null         # Slack incoming webhook
  teams_webhook_url: null         # Microsoft Teams — a Power Automate WORKFLOWS
                                  # webhook. NOT a classic Office 365 connector:
                                  # those were disabled in May 2026 and a
                                  # connector URL is refused, not posted to.
  board_url: null                 # optional deep link; becomes the Teams card's
                                  # "Open in no_human" button. Leave null unless
                                  # the board is reachable from where Teams is
                                  # read — a 127.0.0.1 link is dead on a phone.
  email_to: dev@example.com

integrations:
  jira:                           # polled intake (not `nh task add`); off by default
    enabled: false
    site: ""                      # https://you.atlassian.net
    project_key: ""
    email: ""                     # paired with JIRA_API_TOKEN as Basic auth
    jql: ""                       # operator-authored; blank = open issues in project
    default_repo: ""              # where polled-in tasks run
    write_back: false             # opt-in: comment + category-matched transition
    poll_interval: 5m             # floor 60s
  linear:                         # polled intake; off by default
    enabled: false
    team_key: ""                  # e.g. "ENG" — the prefix in ENG-123
    state_types: [triage, backlog, unstarted]   # which workflow states to pull in
    label: ""                     # optional: only issues carrying this label
    default_repo: ""
    write_back: false             # opt-in: comment + type-matched state move
    poll_interval: 5m             # floor 60s
  monday:                         # polled intake; off by default
    # monday has NO typed workflow state — a status column is user-defined
    # labels that differ per board — so the label→meaning mapping is stated
    # here explicitly and is never inferred. With board_id/status_column unset
    # the adapter RAISES rather than silently returning nothing.
    enabled: false
    board_id: ""                  # numeric board id, as a string
    status_column: ""             # the column's ID (e.g. bug_status), NOT its title
    todo_labels: []               # labels meaning "not started", e.g. ["Ready for Dev"]
    in_progress_label: ""         # optional: label to move to when work starts
    done_label: ""                # optional: label to move to on completion
    default_repo: ""
    write_back: false             # opt-in: update (comment) + status-label move
    poll_interval: 5m             # floor 60s
  # No circleci block: like github_actions / gitlab / jenkins, CircleCI is
  # configured in the `ci:` block — set backend: circleci and project to the
  # API v2 project slug "<vcs>/<org>/<repo>" (e.g. gh/your-org/your-repo), with
  # CIRCLECI_TOKEN in ~/.no_human/.env. It used to live here as
  # org_slug + project + enabled, and nothing read any of the three.
  slack:
    intake: false                 # opt-in Socket-Mode worker; needs SLACK_BOT_TOKEN
                                  # + SLACK_APP_TOKEN in .env. NOTE: connects only —
                                  # mention-to-task intake is not yet wired in serve
  teams:
    enabled: true                 # mute switch over the notify-OUT channel.
                                  # The webhook URL itself is NOT here — it
                                  # stays at notifications.teams_webhook_url,
                                  # where the notifier reads it. Set this false
                                  # to silence Teams without deleting the URL.

The first-run wizard's **Connect your tools** step edits everything in this
`integrations:` block, and Settings → Integrations edits it afterwards. Neither
takes a credential: every token stays in `~/.no_human/.env`, and the wizard
names the variable rather than accepting a value — `config.yaml` is
world-readable. `enabled` (and Slack's `intake`) is what actually starts a
poller or a worker (for Slack: the connection only — mention intake is not yet
wired), so an integration with every setting filled in but
`enabled: false` does nothing, and both UIs say so rather than reporting it as
configured.

approval:                         # RESERVED — no code reads any key in this
                                  # block (grep the names under src/no_human:
                                  # each resolves only to its DEFAULT_CONFIG
                                  # line). They are not live controls:
  require_before_merge: true      #   the never-merge guarantee is structural
                                  #   (constraint #2), not driven by this key;
  auto_merge_on_approval: false   #   the key exists but no code path acts on
                                  #   it — there is simply no auto-merge; and
  approval_timeout: 24h           #   nothing re-notifies or times out on this.
                                  # What actually governs whether `nh approve`
                                  # LANDS the PR is the `approve_merge:` block
                                  # (top-of-file table), which IS read.

git:
  branch_prefix: "no-human/"
  commit_prefix: ""
  never_push_to: ["main", "master", "release/*"]
  agent_identity_name: "no_human"
  agent_identity_email: "no-human@acme.com"   # distinct from you
  approve_identity:               # who a human merge is attributed to
    name: ""                      # empty -> resolved from this repo's git
    email: ""                     # config (user.name/user.email)
  merge_identity_name: ""         # flat aliases for the same merge identity;
  merge_identity_email: ""        # lower precedence than approve_identity,
                                   # higher than the repo's own git config

`approve_merge.enabled` is what makes `nh approve` LAND the pull request:
squash the branch into one commit, push it to the default branch, and close
the PR. Set it to `false` and `nh approve` still records your approval and
still marks the task approved — it just does not merge, leaving the PR for you
to merge in your git host. The same record-only path is taken when there is no
PR or no `gh` on PATH; none of those is a failure.
`approve_merge.test_timeout_seconds` bounds the test run that gates that
landing. Both defaults are in the table at the top of this file, which is
pinned to `DEFAULT_CONFIG` by the suite; stating a default here as well would
put an unguarded second copy in the file.

Landing runs one of two test gates, never both: FOCUSED (change-scoped) when
the squash result's tree is byte-for-byte the tree the attempt's recorded
full-suite evidence ran on, or FULL — bounded by
`approve_merge.full_test_timeout_seconds` — when it differs, or the attempt's
tested tree is unknown or unresolvable. Conflict-round merges and a moved
default branch are the common way a landed tree diverges from what was
actually tested; the full gate re-establishes that evidence on what is
about to be pushed instead of trusting a claim about a tree that no longer
exists. `nh approve`'s output states which gate ran and why.

`git.approve_identity.name`/`.email` is the identity the ONE commit `nh
approve` lands when it squash-merges a PR is attributed to — the human merge
action (constraint #2), never the agent's. Left empty (the shipped default),
it resolves to git's own `user.name`/`user.email` for that repo (repo-local
config overriding global), the same identity a plain `git commit` there
would use; it is deliberately never `git.agent_identity_name`/`_email`. Set
both fields to override per install. If neither the config nor git yields
both `name` and `email`, `nh approve` refuses with an explicit message
rather than guessing.

`git.merge_identity_name`/`.email` are flat aliases for the same merge
identity, for installs where a flat pair of keys is more convenient to
template or override than the nested `approve_identity` block. Resolution
order, highest precedence first: `git.approve_identity.{name,email}`, then
`git.merge_identity_name`/`.email`, then the repo's own resolved
`user.name`/`user.email`. Like `approve_identity`, these are never a fallback
to `git.agent_identity_name`/`_email` — an unresolvable identity still
refuses rather than attributing the merge commit to the agent.

safety:
  max_files_changed: null         # no size cap by default; set an int to escalate
  max_lines_changed: null         # SCOPE_EXPLOSION past it. The human is the gate.
  forbidden_paths: [".env", "secrets/", "*.key", "*.pem"]

feasibility:
  hint_signals_enabled: true      # the pre-flight card's HINT-ONLY signal families
                                   # (e.g. `multi_family`) — extra transparency shown
                                   # alongside the tier's own signals, never fed back
                                   # into compute_tier's MoA/thinking gates. false
                                   # restores the card to exactly the tier's signals.

bounds:
  max_attempts: 3
  max_turns_per_attempt: 500
  lifetime_attempts: 9             # across resumes; exhausting it ends the task (see budget:)
  max_correction_rounds: 2         # also caps autonomous PR-comment->revise rounds;
                                   # exceeding it escalates to a human (no infinite revise)

budget:
  exhaustion_terminal: true       # an exhausted lifetime budget ENDS the task (status
                                  # failed) with its full BUDGET_EXHAUSTED record and a
                                  # wake condition naming what would revive it - it does
                                  # not ask "spend more, or stop here?". The answer to
                                  # that question was standing policy ("stop; the ticket
                                  # was too big - refile it smaller"), and asking it was
                                  # 69 of 119 human-blocking questions. Set false to be
                                  # asked. Raising a cap is human-only either way:
                                  # `nh task config <id> lifetime_tokens=N`.

hooks:
  per_edit_lint: true             # B1: after each Edit/Write, lint the changed file and
                                  # feed hard errors straight back to the agent. A no-op
                                  # unless the repo has a confirmed lint command.

blockers:                         # Part 22
  max_park_duration: "48h"        # parked past this => escalate (never abandon)
  wake_poll_interval: "10m"
  escalate_on_low_confidence_below: 0.6   # unsure what's wrong => ask, don't thrash
  challenge: true                 # ONE supervisor-checked challenge per task, for the
                                  # judgment-call blocker categories only (AMBIGUITY,
                                  # NOVEL_UNKNOWN, IMPOSSIBLE). A "resolvable" verdict
                                  # costs that attempt and re-enters the bounded loop
                                  # under a recorded reversible assumption; every
                                  # external category, every second blocker and every
                                  # check failure park exactly as before. Set false to
                                  # park on the first blocker, unchallenged.

usage_ledger:
  retention_days: 90              # unattributed_usage rows older than this are rolled
                                   # up (not deleted) into one row per (site, model);
                                   # 0 disables compaction

ci:                               # opt-in; the install-wide fallback (see below)
  enabled: false
  # One of: gitlab | github_actions | jenkins | circleci | ghe_checkruns.
  # Each reads a DIFFERENT required key — see docs/adapters.md#ci-no_humanci for the
  # per-backend table. A key another backend needs is ignored, not rejected.
  backend: gitlab
  project: ""                     # gitlab: "group/subgroup/repo"
                                  # circleci: the slug "<vcs>/<org>/<repo>",
                                  #   e.g. "gh/acme/svc" — NOT a path
  # repo: "org/repo"              # github_actions / ghe_checkruns
  # workflow: "ci.yml"            # github_actions
  # job: "job/folder/job/main"    # jenkins
  # base_url: https://build.example.com   # jenkins — required, the default is
                                  #   a placeholder and will not resolve
  # auth: token                   # jenkins: token (basic) | cookie (SSO)
  hostname: gitlab.acme.net
  mode: watch                     # watch = poll the pipeline your push started
                                  # trigger = start one (jenkins/circleci opt-in)
  variables: {}                   # extra pipeline variables (POST body array)
  timeout_minutes: 60
  max_infra_retries: 2            # retry infra failures after 120s, max 2
  poll_interval: 30
  result_parser: pytest           # or "surefire" for Maven

  # NOTE: if `enabled` is true but the chosen backend cannot be built — a
  # missing required key above, or a misspelled `backend` — the run proceeds
  # with the LOCAL test suite as its only gate. It is no longer silent about
  # that (see "which source wins" below), but it does not stop either. Whether
  # it SHOULD stop is open: docs/KNOWN_ISSUES.md KI-5.

ci_gate:                          # post-PR CI gate (WakeWatcher rung 5)
  enabled: false                  # `nh ci-gate run <task>` force-enables for one run
  project_id: 12345               # your CI project's numeric id
  hostname: gitlab.example.com
  ref: main
  repos: [<your-service>]         # PR repos this gate governs
  namespace_template: "ci-gate-pr{pr_number}"   # throwaway, collision-guarded
  namespace_variable: CI_GATE_NAMESPACE         # pipeline var carrying the namespace
  variables: {}                   # extra pipeline variables
  poll_interval: 30
  kubeconfig: ~/.kube/configs/<your-ci-cluster>.yaml   # latest_dev images + ns guard
  pr_build: true                  # code PRs: build the image FROM the PR via the
                                  # Jenkins enrich job (external SSO trigger); false
                                  # = escalate code PRs honestly instead
  enrich_job_url: https://build.example.com/<controller>/job/<folder>/.../<image-build-job>
  jenkins_controller: https://build.example.com/<controller>
  jenkins_ca_bundle: /etc/ssl/internal-ca.pem   # PEM the CI-log fetch verifies the
                                  # Jenkins TLS chain against; empty = system trust
                                  # store. The console-log fetch sends its SSO
                                  # credentials only to jenkins_controller and never
                                  # disables verification.
  registry_prefix: registry.example.com/<org>/<image-path>
```

### `ci:` — which source wins

A run's CI backend is resolved from three places, **most specific first**:

1. an explicit backend injected by an embedder (rare; tests use this),
2. the **project profile's** `ci` block, written by `nh onboard` and confirmed
   by you — it describes *this* repo,
3. the global **`ci:`** block above — the install-wide fallback.

The profile wins over the global block because it is the more specific
statement: `~/.no_human/config.yaml` describes every repo this install will
ever touch, so setting both can only mean "this one is different". A profile
block that names no pipeline target (`project` / `repo` / `job`) is treated as
a detection hint rather than a claim — `nh onboard` writes a bare
`{backend: gitlab}` just for seeing a `.gitlab-ci.yml` — so it falls through to
the global block instead of overriding it.

If a source asks for CI but cannot produce a backend (say `enabled: true` with
an empty `project`), the run does **not** silently proceed ungated: it emits an
`advisory` event naming the source and the reason, and `nh doctor` reports
`CI BACKEND UNUSABLE`. A gate you believe in but do not have is worse than no
gate, so this case is always visible.

### `llm.auth_mode` — two modes

**`auth_mode: subscription` (default).** The coding backend runs on
`CLAUDE_CODE_OAUTH_TOKEN`, loaded from `~/.no_human/.env` with a process-env
fallback. All other metered vars are scrubbed from the process on startup, and
a stray `ANTHROPIC_API_KEY` aborts the run rather than silently billing the
metered API. `auth_profile` (`nh auth use <profile>`) selects which
subscription — personal or enterprise — pays; a run never spans two profiles.

**`auth_mode: api_key` (operator-chosen BYO-API-key).** The one sanctioned
exception, for friends/commercial installs that pay Anthropic directly with
**their own** `ANTHROPIC_API_KEY`. Specifics:

1. The key lives **only** in `~/.no_human/.env` (`chmod 600`) — it is never
   written to or read from `config.yaml`; only the `auth_mode` string itself
   is a config value.
2. This is the operator's **chosen** billing path: the run pays Anthropic
   directly through the operator's own metered key, not the shared
   subscription.
3. Every **other** metered redirect — `ANTHROPIC_AUTH_TOKEN`, Bedrock
   (`AWS_BEARER_TOKEN_BEDROCK`), Vertex (`GOOGLE_APPLICATION_CREDENTIALS`) — is
   still scrubbed from the process, so a run bills exactly one path.
4. No OAuth token is exported into the process env in this mode.
5. The run is attributed to the `api_key` profile for cost/audit tracking.

### `llm.codex_auth_mode` — two modes for the `codex` coder backend

Only consulted when `worker.backend` (or a task's `--backend`) is `codex`.
Reviewer, planner, supervisor and utility stay on Claude in **both** modes —
`CLAUDE_PINNED_ROLES` is untouched by this setting. See
[BACKENDS.md](BACKENDS.md) for the full comparison table.

**`codex_auth_mode: api_key` (default, unchanged).** `OPENAI_API_KEY` — never
from `config.yaml`. Precedence is `~/.no_human/.env` first, falling back to
the ambient process environment if the key isn't in `.env`
(`assert_codex_api_key_mode` in `config.py`; `nh doctor`'s `credential_status`
check agrees). The CLI is still invoked with `preferred_auth_method="apikey"`,
but codex-cli 0.149.0 silently ignores that flag — it is not what stops a
silent ChatGPT fallback. Enforcement is via an isolated `CODEX_HOME`
(`~/.no_human/codex-home/`, this module's own, never the operator's) holding
only the configured key, gated by `assert_api_key_billing_path()`
(`agent/codex_backend.py`), which calls `codex login status` against that
home and refuses the run unless the CLI itself reports an api_key-backed
session for it. Default model: `gpt-5.3-codex` (`llm.codex_model`,
overridable).

**`codex_auth_mode: subscription` (opt-in, added 2026-08-22).** Drives the
coder from a Codex CLI session signed in via `codex login` — the operator's
own ChatGPT plan. no_human is never the authenticating party: it never calls,
wraps, or shells out to `codex login` itself, and never reads, parses, or
`stat()`s `~/.codex/auth.json`. The operator runs `codex login` themselves
before selecting this mode; no_human only checks the resulting session
read-only, via `codex login status` (`codex_login_status()` in
`agent/codex_backend.py`, and `nh doctor`'s Codex row). `preferred_auth_method`
is omitted entirely from the CLI invocation in this mode — there is no key to
force, and forcing `"apikey"` would make the CLI refuse the very session this
mode exists to use. Default model: `gpt-5.6-terra` (`llm.codex_model`,
overridable) — codex-branded ids (`gpt-5.3-codex`, `gpt-5.1-codex*`) are
refused by a live ChatGPT session with "not supported when using Codex with a
ChatGPT account," so the api_key default is not reused here.

Each mode scrubs the other's metered routes (`assert_codex_mode` in
`config.py`) so a run bills exactly one path.

### `llm.codex_network_access` — network for the `codex` coder's sandbox

Default `true`. codex-cli's `--sandbox workspace-write` has no network access
on its own — a coder session needs it for `git fetch`/`push`, `gh`, and
dependency installs. When this is `true` (and the session is not `readonly`),
`_command` emits an explicit `--sandbox workspace-write` paired with
`--config sandbox_workspace_write.network_access=true`; both must be present
together, since the grant is silently inert unless that sandbox mode is
actively selected. (One exception, stated rather than glossed: on a `resume`
invocation `--sandbox` is refused by the CLI, so the grant is emitted without
it and its effect there is untested. The only resume caller is the zero-diff
reformat nudge, which needs no network.) Set to `false` to keep the coder network-less (the CLI's
default). Never emitted for a read-only session — `--sandbox read-only` has
no `sandbox_workspace_write` table for the key to attach to. See
[BACKENDS.md](BACKENDS.md) and `tests/test_codex_sandbox_network.py` for the
measured evidence behind this default.

### `llm.local_model` / `llm.local_base_url` / `llm.local_cli_path`

Reserved for `worker.backend: local`. `local_base_url` is **required** in
that mode — an ambient `ANTHROPIC_BASE_URL` is scrubbed and never trusted as a
fallback. It must be `http` or `https`, and the host must be `localhost` or a
**literal** loopback/RFC1918 IP address: a DNS name is refused (it is
re-resolved at connect time, which is a rebinding surface) and a
public/routable IP is refused (local mode must not leave the machine). Port
numbers and paths are not validated — `http://localhost:8000` and
`http://127.0.0.1:1234/v1` are both fine. The URL must not embed userinfo
(`http://user:pass@host`) or a key-looking query parameter — the mode lives in
config, the key never does. If the local server enforces a key, it goes in
`~/.no_human/.env` as `LOCAL_LLM_API_KEY`, never in `config.yaml`.
`local_cli_path` is optional; `null` uses the SDK-bundled CLI.
**These keys are live.** `local` is in `SUPPORTED_BACKENDS`
(`agent/backend.py:260`) and `make_backend` has a real branch for it
(`backend.py:505`); `worker.backend: local` resolves. It fails only when
`llm.local_base_url` / `llm.local_model` are unset, or when the URL is not a
loopback/RFC1918 address (`config.assert_local_backend_mode`).

### Model picker — `GET /api/models`, `PUT /api/config/models`, `nh config models`

The five model-tier config keys — `llm.primary_model` (coder),
`llm.review_model` (reviewer), `llm.planner_model` (planner),
`llm.supervisor_model` (supervisor) and `llm.utility_model` (utility, also
what intake's `evaluate_spec` / `resolve_assumptions` run on) — can be read
and changed without hand-editing `config.yaml`, through either front door
below. Both call the exact same functions in `core/model_settings.py`
(`models_payload` / `apply_model_changes`), so they can never disagree about
what is allowed or how a write is persisted.

**`GET /api/models`** returns, for each of the five roles: its config key,
its current value, the shipped default, and the full list of allowed
options (id, coarse price class — `low` / `medium` / `high`, on the input
rate — and whether picking it would also require a `worker.backend` change).
It also returns a top-level `restart_required` flag — a true file-vs-process
comparison between what is on disk and what the running server actually
loaded, the same shape `/api/auth/status` already uses for the auth
profile.

**`PUT /api/config/models`** takes a JSON body of `{"<config-key>":
"<model-id>", ...}` — only the five keys above are ever accepted. Every
write goes through the same two rules `model_catalog.validate` enforces
everywhere else in the picker: a vendor-pinned role (every role except
coder) may only be set to a Claude model id, and `review_model` may never
equal `primary_model` in either direction (a fresh-context reviewer is the
product's headline gate). A third rule is layered on top for the coder role
only: `llm.primary_model` may only hold a Claude id, even though the id is
otherwise priced and valid — the coder's actual backend (Claude/Codex/local)
is `worker.backend`'s job, with its own `llm.codex_model` /
`llm.local_model` keys, so a non-Claude id here is refused with a message
naming where it does belong. An unpriced/unknown id is refused outright
(no free-text "custom model" escape hatch) rather than silently pricing at a
fallback later. Any refusal is a `422` and writes nothing. A write that
changes nothing (the submitted id already matches what's on disk) is a
no-op: `200`, empty `changes`, nothing written, nothing logged. A write that
does change something is a comment-preserving splice of `config.yaml` (the
rest of the file — comments, key order, unrelated sections — is untouched)
and persists exactly one `source: human` `task_events` row (`task_id
"__config__"`, `kind: "human_config_models_set"`) naming the old and new
value per changed key. **It does not reload the running server's config** —
that is exactly what `restart_required` on the next `GET` is for; a change
here takes effect on restart.

**`llm.role_backends`** (constraint §6d) is a separate, additive key on the
same body: `{"role_backends": {"reviewer": {"backend": ..., "model": ...} |
null}}`. Today only `"reviewer"` is accepted — every other role stays pinned
to Claude with no override front door. This does not replace `llm.
review_model`: `review_model` still names the model id in the vendor-pinned
default case; `role_backends.reviewer`, when present, is what actually
constructs the reviewer's backend, and `GET /api/models`'s reviewer row
carries an extra `backend` block (`{backend, model, is_default}`) alongside
the plain five-role shape so a caller can tell which is in effect. Writing it
goes through the exact same validation the plain five keys get (unpriced
Claude ids refused, unsupported/unavailable backends refused with the
credential each backend's normal construction path requires — Codex's
`llm.codex_auth_mode`, local's loopback/RFC1918 check) plus one more: an
unknown role name in `role_backends` is a `422`, never silently ignored. A
non-default reviewer choice is disclosed, never silent: the `models` event
carries a `role_backends.reviewer` `{backend, model}` kwarg which
`web/src/summaries.js` renders as its own, un-clipped "Reviewer backend"
digest fact on the task detail (the models line itself is clipped at 110
chars — a four-role production string is already past that, so an appended
suffix there could never render) — and the PR body's evidence table gets an
additional `| Reviewer model | ... |` row — the existing
`| Independent review | ... |` verdict row is untouched either way.

**`nh config models`** (no args) prints the same five roles/current
values/options `GET /api/models` returns, from a freshly loaded config file
rather than a running server's state (there may be no server running).
**`nh config models set <role> <model-id>`** (role is one of `coder`,
`reviewer`, `planner`, `supervisor`, `utility` — not the config key) applies
one change through the identical `apply_model_changes` the PUT endpoint
calls, and prints a `restart required` reminder on success. A refusal exits
non-zero with the same message text the API would have returned in its 422
body.

**Settings → Models** (board UI) is a third front door onto the same two
endpoints — it adds no new server behavior. It renders one row per role in
the order `GET /api/models` returns them, each row's `<select>` built
entirely from that role's `options`: an option the server marked
`requires_backend` is rendered `disabled`, with the server's
`disabled_reason` as the option's title (the coder role is the only one with
such a menu rendered today — part 3 ships the reviewer's picker). Planner,
supervisor and utility model ids stay pinned to Claude with no override
front door; the reviewer's *backend* may already be overridden via
`llm.role_backends.reviewer` (disclosed on the task detail and in the PR
body), even though its Settings-pane picker is not live in this slice. A
role whose `note` is
non-empty (every pinned role) shows it under the row; the reviewer row alone
may also carry a `cost_note` — the same evidence sentence documented next to
`review_model`'s default, describing the operator's A/B revert — and no
other role has one. "Save" PUTs only the roles the user actually changed;
"Reset to defaults" PUTs every role currently off its default back to it,
or an empty body if nothing has drifted. Either action refreshes the pane
from the response, and a `restart_required: true` on that response shows the
same restart banner the Account panel uses for a profile switch. A `422`
reverts every pending edit in the pane, not just the field that caused it —
the server validates the whole submitted body before writing any of it, so a
partial revert would misrepresent what's actually on disk — and shows the
server's message text verbatim.

## `learning:` — memory lifecycle

Memory lifecycle C (`docs/design/memory-lifecycle-triage.md`) — retirement and
flood control for rules/skills. Defaults, pinned to `config.DEFAULT_CONFIG` the
same way the table above is:

| Setting | Default | What it does |
|---|---|---|
| `learning.archive_unconfirmed_days` | `45` | Unconfirmed (`confirmed = 0`), `source="proposed"` rows older than this are auto-archived by the daily `RetirementSweepJob` — reversible, never deleted |
| `learning.retire_suggest_days` | `90` | Confirmed rules unused this long surface in the `retire?` SUGGEST-only section — never auto-archived |
| `learning.sweep_interval_seconds` | `86400` | How often the retirement sweep ticks; the first tick runs immediately at boot |
| `learning.sweep_enabled` | `true` | Kill switch for the unattended daily sweep (the CLI triage path stays reachable either way) |
| `learning.propose_on_success` | `false` | The flood-kill: the per-success templated skill proposal only fires when this is explicitly turned on |
| `learning.auto_manage` | `true` | D3 (2026-08-31 operator directive): auto-activate a harvested proposal that passes the dedupe/PII/provenance/term screens — `confirmed=1`, `source="auto"` — instead of queuing it for a human. `false` is the KILL SWITCH for this write path specifically: it restores the pre-D3 harvest/confirm-queue behaviour exactly, and also turns off the 90-day auto-activated retirement sweep below. It does NOT revert the word-boundary trigger-matching fix or `reject()` aliasing `pause()` for an already-confirmed row — see mechanism 4 below |
| `learning.auto_activate_daily_cap` | `10` | The ceiling on how many proposals `HarvestJob` may auto-activate per rolling 24h window — the compensating control for `auto_manage`'s flipped default |

Four mechanisms, concretely:

1. **45-day auto-archive.** `Store.archive_unconfirmed_older_than` sweeps
   unconfirmed, `source="proposed"` rows past `archive_unconfirmed_days`
   (default 45) once a day and once at boot. Reversible — it sets
   `archived = 1`, never `DELETE`s.
2. **Flood-kill.** The per-success templated proposal (`learning/queue.py`'s
   `_build`) is gated behind `propose_on_success`, **off** by default — the
   flood source that historically produced ~394 near-duplicate pending rows
   is simply not invoked unless an operator opts in. `nh learnings
   --triage-templated [--apply]` cleans up any pre-existing backlog from
   before this gate existed.
3. **Supersede-on-confirm.** `Store.supersede_memory`, called from
   `LearningQueue.confirm`, archives the oldest matching *active* row with
   `superseded_by` pointing at the newly confirmed survivor when a confirmed
   near-duplicate exists — never more than one hop, never a chain.
4. **Auto-activation (D3, 2026-08-31 operator directive).** With
   `auto_manage` at its default (`true`), `HarvestJob` promotes a harvested
   proposal that passes dedupe/PII/provenance/term screens
   (`LearningQueue.auto_activate`) straight into the active set, capped at
   `auto_activate_daily_cap` per rolling day; a screen-failing proposal is
   archived immediately rather than queued. Every activation, screen-failing
   archive, pause, delete and retirement is written to the `learning_events`
   audit table. `nh learnings`/`POST /api/learnings/{id}/confirm` still work
   for compatibility; `reject`/`POST .../reject` now PAUSES an already-active
   learning rather than deleting it (a still-pending proposal keeps its old
   per-origin archive/delete behaviour). `POST /api/learnings/{id}/pause` and
   `POST /api/learnings/{id}/delete` (which archives, never a real delete)
   are the Second-brain UI's direct actions; `POST .../restore` undoes either.
   An auto-activated row (`confirmed_by = 'auto'`) also retires itself
   automatically after `retire_suggest_days` (default 90) unused — an
   operator-pinned or manually-added row can never match that query and so
   is never auto-retired. `auto_manage: false` is the kill switch for the
   AUTO-ACTIVATION WRITE PATH — harvested proposals stop auto-confirming and
   the auto-retirement sweep stops, byte-for-byte the pre-D3 harvest
   behaviour. It is NOT a global revert: the word-boundary trigger-matching
   fix (a memory tagged `fact` no longer fires inside "artefact") and
   `reject()` aliasing `pause()` for an already-confirmed row are both
   correctness fixes that apply unconditionally, whatever `auto_manage` is
   set to.

The Rules/Skills panel surfaces all three: an **Archived**/**Superseded**
badge, a "Show archived" filter, and **Restore**/**Dismiss** triage actions —
see `docs/design/memory-lifecycle-triage.md`'s "Rules/Skills UI" section for
the exact contract.

## Usage insights: the complete event list

`telemetry.enabled` (default `true`) turns on anonymous, opt-out usage
telemetry (PostHog) — on unless it is turned off in `config.yaml`
(`telemetry.enabled: false`). This section is the complete, machine-checked list of
what can ever be sent — two tests keep it that way:
`tests/test_telemetry.py::test_every_server_event_kind_is_documented` /
`test_documented_list_has_no_phantom_events` fail if the server's closed
allowlist (`src/no_human/telemetry.py`'s `_ALLOWED_EVENTS`) and this table
ever disagree in either direction, and `web/src/telemetry.test.mjs`'s
disclosure sweep fails if any `posthog.capture(...)` call site in `web/src`
sends an event name not listed here.

There are exactly seven possible event kinds, six sent by the server and one
by the browser:

| Event | Channel | Props |
|---|---|---|
| `app_started` | server | — |
| `task_created` | server | `source` |
| `task_completed` | server | `status`, `duration_bucket`, `attempts` |
| `task_failed` | server | `category`, `reason_category` (closed enum — which STAGE failed: `budget_exhausted`/`review_failed`/`max_attempts`/`infra`/`tamper_blocked`/`blocker_parked`/`other` — never a reason string) |
| `approve_clicked` | server | — |
| `feature_used` | server | `name` |
| `screen_viewed` | browser | `screen` (the lane name — `board`/`backlog`/`done`/`failed`/`stats`/`settings`/…, never content) |

Both channels stamp every event with the same two identifiers: `instance_id`
(a random uuid4 minted server-side on first consent, persisted in config —
never minted in, or accepted from, the browser) and `app_version`. Person
profiles are created (`person_profiles: "always"`), one per `instance_id`;
no human identity (name, email, IP-derived identity) is ever attached — the
board never calls PostHog's `identify()`.

**Autocapture and the browser's implicit/element channels are on.**
`autocapture`, `capture_pageview`, `capture_pageleave`, `capture_dead_clicks`,
`capture_heatmaps`, `capture_performance` and `capture_exceptions` are all
enabled in the PostHog client init (`web/src/telemetry.js`, operator decision
2026-09-03), so PostHog's own `$autocapture`/`$pageview`/`$pageleave`/
`$dead_click`/`$$heatmap`/`$web_vitals`/`$exception` events are collected
alongside the app's own events. The seven event kinds in the table above are
scoped to what the app itself sends via `posthog.capture(...)`; the disclosure
sweep (`web/src/telemetry.test.mjs`) only ever checks those. Autocapture's
`$el_text` (the text of the clicked/changed element) is bounded by the
`ph-no-capture` masking described below, not by the channel being off.

**Session replay, honestly stated.** When telemetry is on, PostHog session
recording captures the app's own interface. All form inputs are masked
(`session_recording.maskAllInputs: true`), and every element known to render
operator content — task titles, specs/descriptions, diffs, activity logs, the
composer, backlog ticket titles — carries a hand-applied `ph-no-capture`
block-mask, enforced by a source-level test sweep so a new surface can't ship
unmasked silently. This is a masked-surface guarantee, not a claim that replay
"cannot capture content" — it can capture pixels of anything not on the masked
list, which is why the list is enforced by tests rather than left to review.
Replay also records network request/response **headers and bodies**
(`session_recording: { recordHeaders: true, recordBody: true }`) — these are
**not** masked.

**Never sent via the server channel:** the closed event allowlist in the table
above carries only the columns listed there — no task titles, repo names, file
paths, prompts/specs, diffs, or credential/token ever appears in a server
event. The browser's autocapture and session-replay **pixels** are bounded the
same way, by `ph-no-capture` and `maskAllInputs`. Session-replay **network**
capture is the one channel that is not scoped like this: with `recordBody:
true`, request/response bodies from calls such as `/api/tasks` and
`/api/config` are recorded as sent, unmasked — so they DO carry task titles,
specs, diffs and repo paths into PostHog. `posthog-js` only redacts
credential-shaped headers/keys automatically; it does not redact application
content.

Telemetry defaults to **on** (`telemetry.enabled: true` in
`config.DEFAULT_CONFIG`); everything above is sent unless it is opted out in
`config.yaml` (`telemetry.enabled: false`). The onboarding "Usage insights" step
and the Settings > Usage insights pane were removed (operator, 2026-08-26), so
`config.yaml` is the one opt-out. Server-side events post to
`telemetry.posthog_host`'s `/batch/` endpoint by default; a configured
`telemetry.endpoint` (the first-party Lambda) takes precedence when set.

## Tests command

`tests.command` (optional) overrides test detection for the local suite the
orchestrator runs after review. If unset, a sensible default is detected
(`pytest`, etc.). Held-out tests go in `tests/held_out/` and are run separately.

## Lint command

`lint.command` (optional) overrides lint detection the same way. It decides
whether the lint gate exists at all: with no explicit command and no proven
`lint_cmd` on the repo profile, the gate is SKIPPED — no lint, no gate — which
is deliberate, because linting a repo with a command nobody confirmed produces
noise the agent then "fixes". Neither this key nor `tests.command` appears in
the defaults file; both are read straight from your config, so setting either
one is how you turn the behaviour on.

## Intake grill

`intake.grill` (default **true**) decides whether the clarifying-questions
stage runs before planning. It is the most expensive pre-plan stage — two LLM
sessions on every task — so setting it `false` is the way to turn that cost
off; a small prose-only change skips it automatically regardless. Like
`lint.command` and `tests.command`, the `intake` section is not written into
the defaults file: set it yourself to change the behaviour.

## Verifiers

`verifiers.enabled` (default **true**) is the kill switch for the
path-scoped verifier gate described in
[verification.md](verification.md#verifiers--a-recorded-verdict-per-rule):
set it `false` to skip straight to the agentic reviewer even when
`.no_human/verifiers.yaml` (repo or global) defines rules. The `verifiers`
section is not written into the defaults file; set it yourself to change the
behaviour, the same as `lint.command` and `tests.command` above.

## UI evidence

```yaml
ui_evidence:
  enabled: false   # the harness drives a headless browser at the attempt's own
                   # dev server from a coder-written walk manifest and keeps the
                   # screenshots, video and console errors as evidence. Opt-in per
                   # install: the browser fetches whatever the page references, so
                   # this is the key the egress allowlist charges that channel to.
```

The switch above is the install-wide kill switch. The *per-repo* half — telling
the walk which dev server to boot — lives in `<repo>/.no_human/project.yml`
(and its mirrored DB row), not `~/.no_human/config.yaml`:

```yaml
# <repo>/.no_human/project.yml
ui_evidence:
  enabled: true
  start_cmd: npm run dev
  base_url: http://localhost:5173
```

`start_cmd` is no longer just documentation: at attempt time, once tests pass
and a coder-written walk manifest is present, the harness itself boots
`start_cmd` in the attempt's worktree — but only if nothing already answers
at the *manifest's* `base_url` (loopback hosts only: `127.0.0.1`, `localhost`
or `::1` — never a remote host). It polls `base_url` + `ready_path` until
something answers or `ready_timeout_s` elapses (clamped to `[1, 300]`
regardless of the configured value), then runs the walk, then stops the
process it started — whether the walk finished cleanly or raised. If a
server was already answering at that URL, the harness never starts or stops
anything; it just walks against whatever is already running. Either way the
PR body says which happened: "Dev server booted by the harness for this
walk (`{start_cmd}`), stopped afterwards." or "Dev server was already
running at {base_url} before the walk; the harness did not start it and did
not verify which checkout it serves."

Nothing wrote this before no-human-67: a repo could have Playwright installed
and the kill switch on, and the walk still had no `start_cmd`/`base_url` to
boot. `nh onboard <repo>` now detects a `dev` script in the repo's own
`package.json` — only when a known framework (vite, `@sveltejs/kit`, next,
nuxt, `react-scripts`, astro, `@angular/cli`, `@vue/cli-service`) is also a
declared dependency, so the port is read off the framework, never guessed —
and offers a single "Enable visual-proof walks?" (Yes/No, default No) prompt.
Accepting writes the block above to both `project.yml` and the DB row in one
step; declining, or a repo with no `dev` script, writes nothing. Once
`ui_evidence` is configured — by this prompt or by hand — it is never
suggested again: manual config always wins. `nh doctor` names the same gap
("visual-proof walks: repo not configured — detected `npm run dev` on :5173,
enable?") for any known repo that still has no `ui_evidence` configured; it is
an advisory only and never affects doctor's exit code.

## Timeouts read straight from your config

Two ceilings are read the same way and default generously so a legitimately
long run is never cut off, but they bound different things:

- `bounds.attempt_timeout_s` (default 3600) — one coder attempt, bounded by
  INACTIVITY, not by the attempt's total wall clock (B20 follow-up). Every
  progress event the coder session emits (tool use, agent text, a subagent
  round, a context compaction, a streamed usage update) resets the clock, so
  a backend that keeps producing events is never cancelled by this however
  many hours it legitimately runs — only a backend that produces NOTHING at
  all for the full window is. `max_turns` remains the only bound on
  productive work. On expiry the failure reason names the window and the
  last-progress timestamp, e.g. "no coder progress for 3600s (last progress
  at 2026-08-22T07:23:37Z)"; spend already streamed in before the cutoff is
  recorded on the attempt, not lost. It was the single unbounded call before
  it existed.
- `bounds.shadow_timeout_s` (default 1800) — one shadow/bench run in the
  throwaway sandbox. This one IS a plain wall clock — it bounds a
  disposable eval/bench sandbox run, not a coder attempt whose partial
  progress must survive a cutoff.

## Keys the doc gate cannot see

`tests/test_config.py` sweeps the source for settable keys, but it only sees a
two-level chain read directly off the config (`config.get("a", {}).get("b")`).
A section pulled into a local variable first, and a single top-level key, are
both invisible to it — so these are documented by hand. If you add a key of
either shape, add it here too: nothing will remind you.

- `reanalysis.enabled` (default **true**), `reanalysis.interval_seconds`
  (86400, floored at 60), `reanalysis.days` (30), `reanalysis.max_proposals`
  (20) — the periodic job that mines **IDE conversation transcripts** from the
  last N days and proposes learnings from them. It is not reading this
  product's task history: it asks the running IDE language servers for their
  transcripts, and with no IDE running it finds nothing and proposes nothing.
  `max_proposals` does not cap anything — the proposals are already committed
  when the count is checked, so exceeding it logs a warning and leaves them
  for you to triage. Turning `enabled` off stops this unattended pass;
  `nh history --analyze` ignores the flag and still works. (`nh serve` starts
  the job, so it does honour it.) The `reanalysis` section is not written into
  the defaults file.
- `harvest.enabled` (default **true**), `harvest.interval_seconds` (43200,
  floored at 60) — the periodic pass, inside the same `nh serve` loop, that
  runs BOTH existing harvest loops: the bench-candidate harvest
  (`eval/harvest.py`, one `runnable: false` YAML per harvest-worthy terminal
  task) and the learning-proposal harvest (`LearningQueue`, supervisor
  corrections plus escalations/reviewer-FAIL-findings/tamper trips, clustered
  the same `>=2` recurrence rule as `nh learnings --harvest`). It makes no
  backend call (`distill=None`) and never applies anything — every row lands
  a proposal for `nh learnings` to review. `nh serve --no-harvest` skips it
  for that run regardless of the config value.
- `onboarding.extra_scan_roots` — extra directories the repo-discovery scan
  looks in, beyond the conventional clone roots. A single string is accepted
  as well as a list, and a leading `~` means the home the scan is bound to.
  **It cannot reach outside your home directory**: a root that resolves
  elsewhere is refused, by design. For repos on another volume use the
  onboarding UI's "Search another folder", which takes any path.
- `max_thinking_tokens` (default 10000) — a TOP-LEVEL key, not nested under
  `llm`. It caps extended thinking on models that support it, and applies only
  when the task's computed complexity tier turns thinking on; there is no way
  to request it per task.

`ci.workflow` and `ci.repo` are two more keys of this shape. Both are already
shown in the `ci:` block above, as commented-out lines the gate's matcher
cannot parse (it skips `#`-led lines, by design — a commented example is not a
declaration).

## Per-task config snapshot

Each task stores the `config` it ran under (`tasks.config`), so a task's
behaviour is reproducible even if the global config later changes.

## Dispatch priority

`tasks.priority` is `high`, `medium` (default) or `low`; unlike the keys
above it is a task column, not part of `tasks.config`. It only orders the
PENDING queue — quota-parked resumes and prior-work tasks still dispatch
ahead of it as before, running tasks are never preempted, and there is no
aging term, so a `low` task can wait indefinitely behind a busy
`medium`/`high` stream. Two write points, both validated against the same
`high|medium|low` vocabulary and rejecting anything else:

```
nh task add --priority high …
nh task config <id> priority=high    # human-only; writes a human_priority event
```

## `.no_human.yml` — config the repo carries itself (C3-G2)

A target repo can ship its own hints so no_human works well on it without the
operator re-teaching every install. The file is **untrusted input** (whoever
wrote the repo wrote it), so the contract is narrow: **it may only ADD hints or
TIGHTEN safety.**

```yaml
# <repo>/.no_human.yml
test_commands:                       # change-scoped routing, not the gate itself
  - glob: "web/**"
    command: "node --test src/"
    cwd: "web"                       # must stay inside the repo
playbook_hints:                      # advisory lines shown to the coder
  - "run `make check` before pushing; CI runs it too"
forbidden_paths_extra:               # append-only: adds to safety.forbidden_paths
  - "infra/**"
```

Exactly those three keys are read; **everything else is ignored** — a repo can
never set `test_cmd`, `never_push_to`, models, or auth. Further limits:

- The **operator's onboarded profile always wins**: repo routing rules apply only
  where `nh onboard` left none. The default test command stays operator-owned and
  proven, and routing only applies at all once the repo has a usable profile.
- A rule whose glob matches **everything** (`**`, `*`, …) is rejected — that is a
  gate override, not change-scoped routing.
- The file is **snapshotted once per run**, before the coder session starts, so an
  agent cannot rewrite the gate it is judged by mid-attempt.
- Malformed, oversized (>16KB), or absent ⇒ ignored entirely; it never fails a run.
- Hints are advisory: they never outrank the acceptance criteria, and they cannot
  cross a safety rail (the guard, not the prompt, enforces those).

Applied config is visible in the task timeline as a `repo_config` event.
