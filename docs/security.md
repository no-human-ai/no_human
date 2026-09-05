# Security & safety model

no_human is designed for **unattended** runs, so its safety properties are
correctness requirements, not preferences. They are enforced in code and covered
by tests.

## 1. One billing path per run

Two sanctioned auth modes exist. The default, `llm.auth_mode: "subscription"`,
runs on a Claude OAuth token (personal or enterprise, from
`claude setup-token`); `llm.auth_mode: "api_key"` is an operator opt-in that
bills the operator's own Anthropic account with their own `ANTHROPIC_API_KEY`.
The Claude Agent SDK honours `ANTHROPIC_API_KEY` **over**
`CLAUDE_CODE_OAUTH_TOKEN` when both are present, so in either mode startup
(`config.assert_subscription_mode`) guarantees a run bills exactly the one
configured path:

1. Every auth variable that could redirect billing elsewhere is **scrubbed**
   from the process environment: `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
   the Bedrock/Vertex set, `GOOGLE_APPLICATION_CREDENTIALS`,
   `AWS_BEARER_TOKEN_BEDROCK` — and, in the default mode, `ANTHROPIC_API_KEY`.
2. In the default mode, if `ANTHROPIC_API_KEY` was present the process
   **refuses to start** (exit 2) — the scrub already protected this run, but
   startup aborts so you fix the source rather than masking a misconfiguration.
3. The credential for the configured mode (OAuth token, or API key in
   `api_key` mode) is loaded from `~/.no_human/.env`
   (`chmod 600`, gitignored, **never** in the repo) with a process-env fallback.

Credentials are never read from or written to anywhere in the repo, and
`ANTHROPIC_API_KEY` is rejected if it appears in `config.yaml` — the *mode*
may live in config; the key never does.

## 2. The agent never merges

The orchestrator opens a PR/MR and **stops** at `awaiting_approval`. There is no
auto-merge anywhere; merge is always a human action. The board's **Approve and
merge** button and `nh approve` are that action: they squash-land the PR as a
local commit under the operator identity and push it (§7, `vcs/approve_merge.py`)
— a human runs them, never the agent. `approval.auto_merge_on_approval` defaults
to `false` and nothing in the code reads it: no state change, webhook or timer
merges anything; only that human command does. Every coder subprocess is
stamped with an agent-session mark (`agent/session_mark.py`); the `nh` CLI, the
board's gate-ending routes and `land_task` itself (`vcs/approve_merge.py`) each
refuse a caller that carries it. The mark is an environment variable, so this
is a tripwire against an agent that follows its instructions, not a lock
against one that unsets it — §6 states that residual.

## 3. Deterministic VCS under a distinct identity

Git is owned by the orchestrator, never the LLM: branch, commit (as
`no_human <no-human@acme.com>`, distinct from you), push, open PR. The
PreToolUse guard blocks `git merge`, force-push, `rm -rf`, and writes to
`forbidden_paths`. `never_push_to` (`main`, `master`, `release/*`) is refused at
the git layer. During review the guard refuses the file-edit tools (Write, Edit,
NotebookEdit, MultiEdit), every git or forge mutation, and subagents; Bash stays,
so a shell redirection is not prevented by the guard — a change the reviewer
leaves in the tree is a gate-integrity question, not a tool one. (This sentence
said "read-only: all write tools are blocked unconditionally" until 2026-08-22.
`6ef8921ae` corrected it and `da3599ae4` reverted it; `guard.py:WRITE_TOOLS`
defines those four names and Bash is in no read-only denial set.)

The guard does not enumerate ways to spoof the identity (`--author=`, `env -u
GIT_AUTHOR_*`, `git -c user.email=`, ...); instead `Orchestrator._foreign_authored_commits`
reads back every commit's actual author/committer after the attempt and fails
it on any mismatch. This *detects* a misattributed commit and stops the
attempt before review or push — it does not and cannot make forging the
identity impossible.

## 4. Trust only verifiable signals

- **Tamper guard**: any net reduction in test count / assertions between the base
  and the change is blocked *before* the reviewer runs (cheap, deterministic).
- **Independent reviewer**: a fresh-context subagent on a separate Opus-tier
  model (`llm.review_model`) told to
  refute "done", producing an evidence-cited pass/fail checklist — **never a
  numeric self-score**. Reviewer crash → fail-closed.
- **Held-out tests**: `tests/held_out/` are run by the orchestrator and given to
  the reviewer as evidence the implementer never saw.
- **CI retry** only on infra failures (max 2); real failures never auto-retry.

Each of those gates, with the code that enforces it and what it does *not*
cover: [verification.md](verification.md).

## 5. Bounded loop + honest blockers

`max_attempts`, per-attempt `max_turns`, and stuck detection (same error
signature twice → reset context, don't stack corrections). A blocker is **never**
resolved by weakening tests, expanding scope, editing acceptance criteria, or
faking done — the agent makes verifiable progress, parks with a wake condition,
or escalates with a structured report (see [blockers.md](blockers.md)).

## 6. What to review before trusting an unattended run

- `~/.no_human/.env` is `chmod 600` and holds the credential for the
  configured mode (`CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_API_KEY` in
  `api_key` mode), alongside any integration secrets (Jira, CircleCI...).
- `nh eval --gate` is green and the red-team suite shows zero tamper / faked-done
  incidents.
- `never_push_to` and `forbidden_paths` match your repo's protected surface.

## 7. What leaves your machine

**Read this first: no_human is not an offline tool, and this page does not claim
to be an exhaustive list of its network traffic.** It cannot be one. The coder
session is a Claude Agent SDK session built with **no tool restrictions** and
`permission_mode="bypassPermissions"`
(`agent/claude_backend.py:ClaudeBackend.__init__:520`, `:ClaudeBackend.__init__:545`) —
no tool allowlist, no tool denylist, no per-call permission callback. It has
Bash. Anything an agent decides to run — `curl`, `pip install`, `npm i`, a test
suite that hits a staging API — leaves your machine, and nothing in no_human sits
between it and the network. An exhaustive egress claim cannot survive that, so
this page does not make one. If that is unacceptable for your codebase, the
control is the machine (a container, a network policy, an egress proxy), not a
config key here.

What follows is the traffic that is **ours** — deliberate, in our code, and the
part we are accountable for. It is split by whether it happens on a default
install or only after you configure it; "configuration-required" below always
means the default in `config.DEFAULT_CONFIG` is off/empty, never merely that a
key exists. `tests/test_egress_disclosure.py` fails if a module in
`src/no_human/` gains an outbound HTTP client or a network CLI call and is not
named here.

### On by default

- **Prompts to Anthropic**, on your own credential (`llm.auth_mode`). Source
  files, diffs, test output and ticket text go into these. This is the point of
  the tool.
- **`git push` of the task branch to your git remote**,<!-- egress:push --> followed by opening a
  pull request (`GitRepo.push` in `vcs/git.py`, via `open_pr` in
  `vcs/__init__.py`, called from `Orchestrator._finalize` in
  `core/orchestrator.py`). <!-- egress:push:no-optout -->**This ships your source to your git host, and
  there is no key that disables it**<!-- /egress:push:no-optout --> — a task's deliverable IS the PR.
  `never_push_to` (default `main`, `master`, `release/*`) chooses **where** a
  push may land, never **whether** one happens; a branch it protects is refused
  at the git layer (`ProtectedBranch`), and an agent session is denied
  `gh pr merge` / `glab mr merge` for the spellings the guard models — a
  lexical rule plus an argv-shaped one that peels shell wrappers two levels
  deep (`_FORGE_MERGE` and `_forge_invocations` in `agent/guard.py`). The
  modelled set is not closed; see §2 and `verification.md`'s "The merge ban,
  in code".<!-- /egress:push -->
- **PR body, and review comments that quote your code**, posted through `gh` /
  `glab` (`vcs/github.py`, `vcs/gitlab.py`, `vcs/comment_poster.py`). The PR
  body carries the commit summary and test evidence; reviewer findings cite file
  and line and quote the lines they are about. Same destination as the push.
- **PR receipt and status polling** — `gh` / `glab` calls for the PR's head SHA
  and its mergeability (`vcs/pr_watcher.py:default_pr_state`, `vcs/receipts.py`), plus
  `git fetch origin` (`vcs/git.py:GitRepo._have_remote_commit:934`, `:GitRepo.fetch:1103`),
  while a task waits on CI or review.
  These read; they send only the identifiers of a PR you just created.
- **`nh merge-stack run` calls `gh pr merge`** against your git host
  (`cli/commands.py:merge_stack_run:2865`). This is *your* command, not the agent's — an agent
  session's Bash is denied it for the spellings the rule models
  (`_LEXICAL_MERGE_STACK` in `agent/guard.py`, plus the argv check beside it),
  in both session modes; see §2 for the bound.
  Until 2026-08-08 this sentence overstated: the guard denied the direct
  spellings but not this wrapper.
- **`nh approve` lands the PR** — a local squash commit made as the operator
  identity, then `git push` of that commit to your default branch and a
  `gh pr close` / `glab mr close` of the PR (`vcs/approve_merge.py`). This is
  **your** command (CLI `nh approve` or `no-human approve` — the same entry
  point under both console scripts — or the board's "Approve and merge"
  button).

  An agent session's Bash is denied it in every mode (§2). The rule reads
  **argv**, not the command line: it joins backslash-continuations, decodes
  `$'...'` escapes, masks quoted payloads and heredocs so a `;` inside code is
  not read as a shell separator, splits the line into commands, peels wrappers
  (`uv run`, `uvx`, `env`, `sudo`, `sh -c`, `timeout`, `xargs` and the rest of
  the file's runner set), and asks what each command IS.

  What it covers, each measured in both session modes and most of it executed
  in a real shell before being believed:

  - the CLI under either console script, either case, with options,
    redirections (including one glued onto the verb) or a line continuation
    between the binary and the verb;
  - a python interpreter — `python`, `python3.12`, `ipython`, `pypy` — that
    imports the landing code, imports it dynamically, drives the click entry
    point, or shells the CLI back out, however the interpreter is flagged or
    fed;
  - a tool that runs what it reads: `find -exec`, `awk`'s `system()`, `sed`'s
    `/e`, `osascript`'s `do shell script`, and a shell runner nested **two**
    deep;
  - a command substitution inside another command's argument;
  - a request to `/api/tasks/<id>/approve`, `/approve-landed`, `/shipped` or
    `/finish-review` from anything that can make one — `curl`, `wget`, `gh
    api`, `node`, `bun`, `perl`, `ruby`, a python payload, a raw socket — with
    the argument percent-decoded and its path normalised, so neither an escaped
    byte nor `..` padding slips past.

  **Input it cannot resolve is refused, not allowed.** `shlex` handles quoting
  and backslashes and nothing else — not a variable, not a substitution nested
  inside another. When the command or verb position holds one of those AND the
  command names one of these actions, the guard refuses and says so. This is
  the polarity the `git push` rule in the same file already takes.

  **Naming the act is not doing it.** A reviewer can read, grep and `git log`
  the landing code, grep the route in the file that defines it, run its tests
  (through `pytest`, `uv run pytest`, `subprocess`, or behind `xargs`), and
  write a commit message or PR title that mentions the command. The exemption
  is a property of the command that will actually run, after wrapper peeling —
  not of `argv[0]` as typed, and not of a message-option grammar.

  **WHAT THIS RULE IS, stated plainly after seven rounds of getting it wrong.**
  It RAISES THE COST of the obvious spellings. It is not a closed door, and
  this paragraph no longer offers a list of exceptions as if it were one —
  that list was published three times and found incomplete three times, which
  is worse than not publishing it. A guard that reads a command line is playing
  a different game from a shell, which parses a grammar, expands it, and then
  executes; the gap between those is not enumerable by inspection.

  Some things it demonstrably cannot see, as illustration rather than as a
  boundary: a command assembled at runtime (`base64 -d | sh`); a script written
  by one tool call and run by the next, where only the path reaches Bash; shell
  nesting past two levels; aliases, functions and `PATH` shims; and shell
  grammar the tokeniser does not model. On the Codex backend it cannot act at
  all before the fact — `codex exec` offers no PreToolUse veto, so the same
  rules are evaluated on an already-executed call.

  **Two more spellings, considered in the 2026-08-23 round and named here
  rather than left silent.** `ssh host "gh ... pr merge 7"` executes the
  mention on a REMOTE host under credentials this process cannot account
  for, so recursing into it would assert a policy about a machine this guard
  has no authority over. `find . -exec gh ... pr merge 7 \;` has its own
  `\;`-vs-`+` argument-batching grammar across matched files, which earns
  its own parser rather than inheriting this one's false-negative surface.
  Neither is a narrowing of an existing rule — both were already unreached
  before this round — so both are disclosed gaps, not regressions. A third
  spelling in the same family, `case x in x) gh pr merge 7;; esac`, is also
  unreached and is disclosed here for the first time for THIS rule — the
  `case...esac` gap already on record in `CHANGELOG.md` names `nh approve`,
  a different check, and disclosing it there does not cover this one.

  **A check at the ACT sits behind this one. It is implemented, and it is a
  BETTER-PLACED layer of the same kind — not a closed door.**
  `agent/session_mark.py` stamps every subprocess the coding backends
  launch — all three of them: `ClaudeBackend._options()` (which is also the
  harness the `local` backend runs on — `make_backend`'s `local` branch in
  `agent/backend.py` returns a `ClaudeBackend`, so the mark covers it too)
  and `CodexBackend._child_env()` — with an
  env-var mark that is inherited by every descendant of that session, no
  matter how it is invoked. `nh approve` and `nh merge-stack run`
  (`_refuse_agent_gate_act`, `cli/commands.py:approve:5085`, `:merge_stack_run:2835`) refuse before
  `_bootstrap` runs when the calling process carries that mark, and an HTTP
  middleware in `api/app.py` (`_refuse_marked_gate_acts`, by `_csp_header`)
  refuses
  the four gate-ending routes — `/approve`, `/approve-landed`,
  `/finish-review`, `/shipped` — whenever the request carries the mark header
  (a marked CLI client sends one, see `cli/api_client.py`) or the server
  process itself was launched inside an agent session. Both checks run
  before any state mutates. This is additive to the lexical guard above, not
  a replacement for it.

  **What that placement actually buys, stated without overclaiming.** Two
  things the argv layer cannot give, and nothing more:

  1. **One choke point instead of twelve spellings.** The check is at the
     act, not on the way to it, so an invocation the lexical rules never
     modelled — a runtime-assembled command, an alias, a `PATH` shim, a
     script written by one tool call and run by the next — arrives at the
     same single refusal, *provided the mark is still in the environment*.
     One place to get right instead of a list that went stale three times.
  2. **It is the only layer that works at all on the Codex backend.**
     `codex exec` offers no PreToolUse veto, so the lexical rules there can
     only be evaluated on an already-executed call. The mark is stamped into
     the child env and checked inside the act itself, which is before the
     fact on every backend.

  **The two residuals, so nobody reads the above as "closed".**

  - **The mark travels in the environment, and an environment variable can
    be dropped.** The argv layer denies `env -u NO_HUMAN_AGENT_SESSION nh
    approve <id>` (`_peel_runners` recognises that spelling, see below), but
    it cannot see the same two lines inside a script that one tool call
    writes and the next runs by path — that is the disclosed gap two
    paragraphs up. A session that dodges the argv layer *and* unsets the var
    in the same step is past both layers. Script indirection defeats the
    argv layer; unsetting an inherited variable is not hard once you are
    past it.
  - **The HTTP check is honour-system.** The local server cannot see a
    caller's environment, so a marked caller is recognised only if it SENDS
    `X-No-Human-Agent-Session` — which `cli/api_client.py` does and a raw
    `POST` need not. A marked caller that talks HTTP without the header is
    not refused by this check at all, unless the server process itself is
    marked. The argv layer denies the obvious `curl`- and `gh`-driven
    spellings of those routes, which is exactly the composition being
    described: two cost-raising layers, not one closed door.

  So, plainly: the act-level check RAISES THE COST of ending the gate, moves
  the enforcement to the one place every spelling arrives at, and covers a
  backend nothing else covers. It does not make the act impossible for a
  determined session, and the spelling of the invocation has NOT stopped
  mattering — it decides whether the mark survives to the act. (Four commit
  subjects on the branch that shipped this say "so the spelling of the
  invocation stops mattering". That is backwards; landed commit subjects
  cannot be corrected without rewriting history, so the correction is
  recorded here and in `CHANGELOG.md`.) The control that actually closes the
  door is not in this repo: it is the human holding the merge button —
  `nh approve` is an operator act, the agent never merges — and forge-side
  branch protection making the human the only party who can land a PR.

  The paragraph overstated between 2026-08-12 and 2026-08-22, when `nh approve`
  gained a real `git merge --squash` and push while neither it nor the API
  routes were denied. Twelve reachable spellings, found by fact-checking a
  public article rather than by an exploit. **Fix round after fix round followed, each one wrong somewhere the last
  shape could not reach, and every miss found by an independent review**; the list above is what
  survived the last of them. Rounds one to four were pattern-matching over the
  command text and each was wrong somewhere the previous shape could not reach;
  from round five the rule reads argv. The reviews stopped reasoning about
  spellings and started EXECUTING them, which is how most of the list was
  found.
- **One `GET https://pypi.org/pypi/no-human/json` per day**, to notice a newer
  release (`updates.py:44`). No identifier, no repo name, no telemetry — the
  same request `pip install` makes. On by default (`updates.enabled: true`,
  `interval_seconds: 86400`); off with `updates.enabled: false` in
  `~/.no_human/config.yaml` or `NH_NO_UPDATE_CHECK=1`
  (`updates.py:57`, which also covers CI).
- **The desktop app checks GitHub Releases at startup**, once a day
  (`desktop/main.mjs:240` → `desktop/updater.mjs:113`, called at startup from
  `desktop/main.mjs:1098`, feed `provider: github, owner: no-human-ai, repo:
  no_human` — `desktop/electron-builder.config.cjs:366`). It never downloads on its own
  (`autoDownload` is off, `desktop/updater.mjs:66`). **This is a separate code
  path from the PyPI check above and neither `NH_NO_UPDATE_CHECK` nor
  `updates.enabled` exists in `desktop/` — those switches do not reach it.**
  Today the only way to stop it is to not run the desktop app. That gap is a
  defect, not a design.
- **Usage telemetry** (`telemetry.py`): a closed allowlist of event names —
  `app_started`, `task_created`, `task_completed`, `task_failed`,
  `approve_clicked`, `feature_used` — each carrying only the props listed in
  `docs/configuration.md` "Usage insights", an anonymous uuid4
  `telemetry.instance_id` and the app version. POSTed via `urllib.request` to
  `https://us.i.posthog.com/batch/` (`telemetry.posthog_host`) with the
  publishable client token, or to `telemetry.endpoint` when you set one
  (`telemetry.py:_destination`). Never a task title, repo name, path, prompt,
  diff or token. On by default (`telemetry.enabled: true`), off with
  `telemetry.enabled: false` in `~/.no_human/config.yaml`. The browser board
  sends its own PostHog analytics and session replay on the same id (same doc
  section) — that channel is independent of this one.

### Only if you configure it

Every channel below sends nothing at all on a default install. Each names the
config key that turns it on and the default that keeps it off.

- **Your CI provider.** `ci.enabled` defaults to **`false`**, and with it off
  `ci_from_config` returns `None` and no backend is ever constructed. Every
  backend additionally needs a `ci.project`/`ci.job`/`ci.repo`; asked for a
  backend with none of them, `ci_from_config` raises `CIMisconfigured` — still
  no backend, still nothing sent anywhere (`ci/__init__.py`). For the **global
  `ci:` block** the run then escalates rather than proceeding ungated. That
  escalation does **not** cover a project profile whose `ci` block names no
  pipeline target: `nh onboard` writes a bare `{"backend": "gitlab"}` as a
  *detection hint* on seeing a `.gitlab-ci.yml`, and a hint is not a request,
  so such a profile is not treated as a CI source at all — no backend, and no
  escalation either (see `docs/KNOWN_ISSUES.md` KI-5).
  Once you enable one:
  - **GitLab CI** (`ci.backend: "gitlab"`, the default *choice* but not a
    default *state*) POSTs a pipeline via
    `glab api --method POST projects/<project>/pipeline`, sending the **branch
    name** and the **key/value pairs you put in `ci.variables`** (default `{}`)
    as the request body (`ci/gitlab.py:403`). It has no watch-only mode: if
    it is enabled, it triggers.
  - **Jenkins** (`ci.backend: "jenkins"`) reaches `ci.base_url` over `curl`
    (`ci/jenkins.py:301-330`). `ci.mode` defaults to **`watch`**, which only
    polls `…/lastBuild/api/json`; **`ci.mode: "trigger"` is opt-in** and POSTs
    `…/buildWithParameters` with `ci.variables` in the query string
    (`ci/jenkins.py:154-169`). The same job API is used by the PR-image
    enrichment path (`ci_gate/enrich.py:70-83`).
  - **CircleCI** (`ci.backend: "circleci"`) talks to
    `https://circleci.com/api/v2` with the `CIRCLECI_TOKEN` from
    `~/.no_human/.env`. `ci.mode` defaults to **`watch`** — a `GET` of the
    pipeline your PR push already started (`ci/circleci.py:169-180`).
    **`ci.mode: "trigger"` is opt-in** and sends a JSON POST
    `{"branch": "<your branch>"}` to `POST /project/<slug>/pipeline`
    (`ci/circleci.py:182-186`).
  - **GitHub / GHE check runs** (`ci.backend: "github_actions"` or
    `"ghe_checkruns"`) read status via `gh` and never write
    (`ci/ghe_checkruns.py`, `ci/github_actions.py`).
- **Ticket trackers**, for intake and optional write-back:
  `integrations.jira.enabled`, `integrations.linear.enabled` and
  `integrations.monday.enabled` all default to **`false`** and hold no
  credential (`intake/jira.py`, `intake/linear.py` →
  `https://api.linear.app/graphql`, `intake/monday.py` →
  `https://api.monday.com/v2`). monday intake additionally needs
  `integrations.monday.board_id` and `.status_column`, both empty by default —
  with either unset the adapter raises rather than calling out. Write-back on
  every tracker is separately opt-in (`write_back`, also `false`). GitHub/GitLab
  issue intake goes out over `gh` / `glab` and only runs for a ticket ref you
  hand it (`intake/github_issues.py`, `intake/gitlab_issues.py`).
- **Microsoft Graph (Teams + Outlook context).** If you configure
  `context.m365.token`, your query text is sent to Microsoft Graph — a POST to
  `https://graph.microsoft.com/v1.0/search/query` carrying the task's external
  ID or up to three keywords derived from the task title
  (`context/teams.py:35`, `:55-66`; `context/outlook.py` shares the same
  client). `DEFAULT_CONFIG` has no `context.m365` block at all, so with no token
  configured the client **fails closed and sends nothing** — it raises before
  building the request (`context/teams.py:50-54`). It is opt-in, not default-on.
- **Slack / Teams notification webhooks.** `notifications.slack_webhook_url` and
  `notifications.teams_webhook_url` both default to **`null`**; with a URL set,
  a task-status line (and, for Teams, a card linking to your board) is POSTed to
  it (`notify/slack.py:53`, `notify/teams.py:205`).
- **Integration health checks.** The board's integrations page authenticates
  against whichever of Jira, Linear, monday, CircleCI, GitHub, GitLab and
  Jenkins you have configured, to show a live status
  (`integrations/__init__.py:test_integration:1591`). Slack and Teams are views
  and send nothing: a webhook cannot be probed without posting a message into
  someone's channel, so it is verified by the notifier at run time instead
  (`:VIEW_ONLY_CHECKS:1588`). A **configured** GitHub is
  checked like the rest: the token is sent to `api.github.com`, or to your GHES
  host, to authenticate and to read the configured repo
  (`:_check_github:1462`). Only an **unconfigured** GitHub is presence-only,
  with no network round-trip (`:_probe_github_ambient:515`,
  `:_probe_github_ambient:549`). Nothing configured → nothing sent.
- **Team brain control plane.** `team_brain.enabled` defaults to **`false`** and
  `team_brain.control_plane_url` to **`""`**; when set, the client exchanges
  task patterns with that URL over `https` (loopback excepted)
  (`brain/client.py:89-133`).

### Not egress: loopback

The CLI, the desktop app and the MCP bridge talk to no_human's **own** API on
`server.host`:`server.port`, which is `127.0.0.1:8420` by default
(`cli/api_client.py`, `intake/mcp_bridge.py:40`,
`cli/commands.py:print_no_task_matching:84`), and the transcript-research reader probes a language
server on localhost (`history/extractor.py:65-72`). These never leave the
machine, and `server.host` defaults to `127.0.0.1`.

That API is unauthenticated, and a loopback address is not an authentication
boundary on its own: a page the operator visits while the board is up can reach
`127.0.0.1:8420` from the browser. `api/local_boundary.py` closes that in one
place — every request must carry an allowed `Host` (else 400; the DNS-rebinding
defence), a state-changing request whose `Origin` is present and not allowed is
refused (403), the CORS grant is exact-host loopback only, and the `/ws`
handshake repeats the `Host`/`Origin` gate because a WebSocket bypasses CORS.
Allowed means loopback (`127.0.0.1`, `localhost`, `::1`) **plus the configured
`server.host`**, because the `nh` CLI addresses the board by that configured
value; a rebinding page cannot make its own domain equal it. A local non-browser
client — the `nh` CLI, the MCP bridge — sends no `Origin` and is unaffected by
the origin check: a same-user process is inside the trust boundary (an attacker
with shell as the nh user is out of scope). **Consequence:** a board bound to
`0.0.0.0` (`nh start --host 0.0.0.0`, the container image's default) still
serves its CLI and a browser at `localhost`, but a browser addressing it by a
LAN name or IP that is not `server.host` gets `400 bad_host`; set `server.host`
to that name, or reach it through an SSH tunnel or a loopback port-forward. The
one credential-carrying fetch that follows forge-supplied data, the Jenkins
console-log excerpt, sends its SSO credentials only to a link under
`ci_gate.jenkins_controller` (https, same host, port and path prefix) over
verified TLS (`ci_gate.jenkins_ca_bundle` names an internal CA) — any other link
gets no request at all.

The coder subprocess (both backends) does **not** inherit the launcher's
environment credentials. `agent/child_env.py` removes every environment
variable whose NAME is credential-shaped — `*TOKEN*`, `*SECRET*`, `*PASSWORD*`,
`*API_KEY*`, `*CREDENTIAL*`, `*WEBHOOK*`, any `*_KEY` (deliberately broad:
`SORT_KEY`, `PARTITION_KEY`, `LICENSE_KEY` go too), credential-file pointers
(`SSH_AUTH_SOCK`, `GOOGLE_APPLICATION_CREDENTIALS`, `NETRC`, `PGPASSFILE`,
`KUBECONFIG`, `DOCKER_CONFIG`, `DOCKER_AUTH_CONFIG`), the named connection
URLs (`DATABASE_URL`, `REDIS_URL`, `MONGODB_URI`, `SENTRY_DSN`) plus **any**
variable whose value is a URL with a password in it
(`postgres://app:s3cret@host/db`, whatever its name), and the whole
`AWS_*`/`GCP_*`/`AZURE_*` namespaces except `AWS_REGION`, `AWS_DEFAULT_REGION`,
`AWS_PAGER`, `AWS_CA_BUNDLE` and `AWS_ENDPOINT_URL*`; `TOKENIZERS_PARALLELISM`
is exempt from the `*TOKEN*` rule. Kept: the credential that pays for that
child (`ANTHROPIC_*`/`CLAUDE_*` for Claude, `OPENAI_*` for Codex), the
agent-session mark, and every `*_PROXY` variable even when its URL carries a
password — the child is the process that reaches its model, and on a network
that requires an authenticated proxy an empty `HTTPS_PROXY` would mean
"connect directly" and cut every session off. A prompt injection in the child cannot read
`GITHUB_TOKEN`, a cloud key or an integration token out of `env`. It is a
name-shape rule plus one value rule, so a bare credential under an unusual
name survives, and the Claude child sees the removed names as empty strings
(the SDK merges over the parent environment) while the Codex child does not
see them at all. The runtime's own `git push`/PR-open run in the parent and
are unaffected.

What this does **not** do: the child keeps `HOME` and the filesystem, so a
keyring login, `~/.aws/credentials` or `~/.config/gh/hosts.yml` remain
reachable — `gh` and `git` in the child still authenticate as the operator
through them. `forbidden_paths` (§3) refuses the paths it lists; everything
else on disk is the host's to deny (a container, a separate user). Inside the
child, an ssh-agent-only key is unusable, `AWS_PROFILE` is gone, and a private
registry token (`NPM_TOKEN`, `CARGO_REGISTRY_TOKEN`, ...) a task's own test
suite needs must come from the machine, not from the launcher's shell.

### What you actually control

| | |
|---|---|
| Which credential pays, and that only one does | `llm.auth_mode`, `nh auth use <profile>` |
| Which branches can never be pushed to | `git.never_push_to` |
| Which paths the agent may not touch | `forbidden_paths` |
| Whether the CLI checks PyPI | `updates.enabled`, `NH_NO_UPDATE_CHECK=1` |
| Whether any CI provider is contacted | `ci.enabled` (**off** by default) |
| Whether a CI run is *triggered* rather than watched | `ci.mode` (Jenkins, CircleCI); GitLab always triggers |
| Whether your query text reaches Microsoft | `context.m365.token` (**unset** by default) |
| Whether notifications leave the machine | `notifications.*_webhook_url` (**null** by default) |
| Whether a ticket tracker is contacted | `integrations.jira.enabled`, `integrations.linear.enabled`, `integrations.monday.enabled` (**off**) |
| Whether a PR is pushed at all | **nothing — it always is** |
| Whether the desktop app checks for updates | **nothing — don't run it** |
| What else the coder session may reach | **nothing in-process — use the OS** |

### History

Until 2026-08-01 this page and the README said prompts were the only thing that
left the machine. That was false when written: `open_pr` terminates every task.
The first correction added the PyPI check and repeated the false claim in a new
form ("two things, and nothing else"), which was worse — it read as an audited
enumeration. The lesson is recorded here rather than quietly fixed: **an
exhaustive claim about egress cannot be made about a process that gives an agent
an unrestricted shell.** Name the channels that are yours, name the unbounded
one, and let the reader decide.
