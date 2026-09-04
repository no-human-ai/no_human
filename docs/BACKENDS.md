# Coding backends

no_human's implementer ("coder") runs on a **coding backend**. There are three.
The default is unchanged and always will be the default unless you change it.

| | `claude` (default) | `codex` | `local` |
|---|---|---|---|
| Harness | Claude Agent SDK → `claude` CLI | `codex exec --json` → `codex` CLI | Claude Agent SDK → `claude` CLI, pointed at your own server |
| Credential | `CLAUDE_CODE_OAUTH_TOKEN` (subscription) or `ANTHROPIC_API_KEY` (BYO) | `OPENAI_API_KEY` (`llm.codex_auth_mode: api_key`, default) **or** a `codex login` ChatGPT session (`llm.codex_auth_mode: subscription`) | `ANTHROPIC_API_KEY`, per-subprocess only (see below) |
| Model | `llm.primary_model` | `llm.codex_model` (per-mode default — see below) | `llm.local_model` |
| Install | `npm install -g @anthropic-ai/claude-code` | `npm install -g @openai/codex` | none — reuses the Claude CLI |

Everything except the coder — planner, supervisor, utility, intake — stays on
Claude regardless of this setting; those model tiers are fixed by the
project's non-negotiable constraints, and the amendment that sanctioned a
second *coding* backend moved none of them. The reviewer stays on Claude
(`claude-opus-4-8`) by DEFAULT too, but an explicit `llm.role_backends.reviewer`
Settings choice is honored independent of the coder's backend — disclosed on
the task detail as its own "Reviewer backend" fact and in the PR body, never
silently. So selecting `codex` for the coder (and leaving the reviewer at its
default) means your run bills **two** vendors: OpenAI for the implementer,
Anthropic for everything else. Both credentials must be present.

## Switching

For every task, in config — or for ONE task, on the task: `nh task add … --backend codex`
(or `"backend": "codex"` on `POST /api/tasks`). The per-task value wins for that
task's coder only and gets the same credential/CLI preflight as the global key.

```yaml
# ~/.no_human/config.yaml
worker:
  backend: codex          # default: claude
llm:
  codex_auth_mode: api_key          # default; the other value is "subscription"
  codex_model: null                 # null ⇒ per-mode default, see below
  codex_reasoning_effort: null      # null ⇒ the CLI's own default
  codex_cli_path: null              # null ⇒ resolve `codex` on PATH
  codex_network_access: true        # default; false ⇒ no network in the sandbox
```

```bash
# ~/.no_human/.env  (chmod 600, gitignored)
OPENAI_API_KEY=sk-...
```

The **mode** lives in config; the **key never does**. `nh` refuses to load a
config file containing either vendor's API key, and refuses to start with
`worker.backend: codex`, `llm.codex_auth_mode: api_key` and no `OPENAI_API_KEY`
on file.

## CLI compatibility is probed, never assumed

Verified live against **codex-cli 0.149.0**: `--ask-for-approval` — the flag
this backend used to hardcode — is no longer on `codex exec` at all (it
survives only on the root `codex` command); `codex exec --help` on that build
lists `-c, --config <key=value>` instead. Passing the old flag to `exec` is
`unexpected argument '--ask-for-approval'`, rc=2, before a single turn runs —
a probe verified the pre-fix argv reproduces exactly that.

So the non-interactive approval flag is resolved from the **installed CLI's
own `codex exec --help` output**, at the time the command is built — never
hardcoded, never assumed from a version string. The ladder, in order:

1. `--ask-for-approval never`, when `exec --help` still documents it (older CLIs).
2. `--config approval_policy="never"`, when it doesn't — verified live to be
   syntactically accepted by codex-cli 0.149.0's `codex exec` and to actually
   suppress the interactive prompt (a control run with an unknown config key
   fails with `unknown configuration field`, so the flag is confirmed
   *recognised*, not merely silently ignored).

If neither is present in the installed CLI's help text, the backend refuses to
launch rather than risk an indefinite hang on an approval prompt nobody can
answer — it never falls back to `--dangerously-bypass-approvals-and-sandbox`
or any other flag that removes the sandbox boundary. `nh doctor` runs the same
probe (whenever `worker.backend: codex`, or a live task asks for it) and prints
a `codex backend` row naming the CLI path, the verified version, which
approval mode was chosen (or `UNSUPPORTED`), the selected `llm.codex_auth_mode`,
and credential presence for that mode (never its value).

**Resuming a thread is probed separately, because its flag surface is
narrower.** `codex exec resume <thread-id>` accepts neither `--cd` nor
`--sandbox` (verified live: `codex exec resume --help` documents neither — a
resumed thread inherits both from the session it is resuming); the backend
omits both on the resume launch and validates that argv against
`codex exec resume --help`, not the non-resume one. A CLI that accepts the
fresh-attempt argv can still reject the resume one, so `nh doctor` checks and
reports both independently rather than assuming compatibility transfers.

Both probes (`codex exec --help` and `codex --version`) are cached per
resolved CLI path for the life of the process, so a long-running daemon
spawning many attempts against the same CLI does not re-spawn the probe on
every attempt.

**Model entitlement is a separate, unchecked question.** `nh doctor`'s codex
row always carries a note that `llm.codex_model`'s entitlement cannot be
verified without a **billed** call to `/v1/responses` — a model id appearing
in `GET /v1/models` is not the same as being entitled to call it on
`/v1/responses`, and no diagnostic here spends OpenAI quota to find out. If
the configured model 404s at run time, or is refused as unsupported under the
active `llm.codex_auth_mode` (see below), that surfaces as an ordinary attempt
failure naming `llm.codex_model`, not a doctor contradiction.

## Two sanctioned Codex auth paths

`llm.codex_auth_mode` selects how the Codex coder backend authenticates.
**`"api_key"` is the default and is unchanged** from the original Codex
integration: a `OPENAI_API_KEY` the operator pays OpenAI for
directly, loaded from `~/.no_human/.env` only. The CLI is still invoked with
`preferred_auth_method="apikey"`, but that flag is not what stops a silent
ChatGPT fallback — codex-cli 0.149.0 silently ignores it and will still bill
a live ChatGPT session on the machine if the key is bad. What actually
enforces this: every `api_key`-mode run is pointed at a `CODEX_HOME` this
module owns (`codex_api_key_home()`, under `~/.no_human/codex-home/`) holding
only the configured key, and `assert_api_key_billing_path()` calls
`codex login status` against that isolated home and refuses the run unless
the CLI itself reports an api_key-backed session for it — never a ChatGPT
one, never absent. See `_child_env_api_key()` in `agent/codex_backend.py`.

**`"subscription"` is an opt-in** added 2026-08-22, letting a Codex CLI signed
in via `codex login` (a ChatGPT plan) drive the coder. no_human is never the
authenticating party: it never calls, wraps, or shells out to `codex login`
itself, and never reads, parses, or copies `~/.codex/auth.json`. The operator
runs `codex login` themselves, on their own machine, before selecting this
mode; no_human only *checks* the resulting session via the read-only, no-op
`codex login status` (`codex_login_status()` in `agent/codex_backend.py`), the
same way `nh doctor`'s Codex row does. Selecting this mode omits
`preferred_auth_method` entirely from the CLI invocation — there is no key to
force, and forcing `"apikey"` here would make the CLI refuse the very session
this mode exists to use.

Each mode scrubs the other's metered routes so a run bills exactly one path,
and picks its own default model (below) — a live ChatGPT session and an
`OPENAI_API_KEY` do not accept the same model ids.

(The Claude default staying OAuth-only, and Codex defaulting to `api_key`
rather than subscription, both trace to the same asymmetry: a Claude
*subscription* used locally is the operator's own credential on the operator's
own machine with no no_human server in the path, which is one of two arguments
that survived legal review for Anthropic specifically. That reasoning is
Anthropic-specific and was never assumed to transfer to OpenAI by default —
which is exactly why the Codex subscription path is opt-in, not the default,
and why no_human still refuses to be the one that signs in. See the project's
non-negotiable constraints for the full, hedged reasoning and its counter-
evidence on the Anthropic side; the OpenAI-side sourcing this amendment rests
on is `learn.chatgpt.com/docs/auth`, cross-referenced against an internal
per-vendor terms review that is not part of this export.)

### Model defaults

| `llm.codex_auth_mode` | Default `llm.codex_model` |
|---|---|
| `api_key` (default) | `gpt-5.3-codex` |
| `subscription` | `gpt-5.6-terra` |

The two model families are not interchangeable: a live ChatGPT session refuses
codex-branded ids (`gpt-5.3-codex`, `gpt-5.1-codex*`) with "not supported when
using Codex with a ChatGPT account", while those same ids are the entitled
ones on an API key. `agent/backend.py`'s `default_codex_model(mode)` picks the
right default per mode; a vendor refusal that names the mismatch is surfaced
as a typed `CodexModelUnsupportedError` rather than degrading silently. Set
`llm.codex_model` explicitly to override either default.

## What you give up by switching

These are not bugs to be fixed later; they are things `codex exec` does not
expose. Each is declared in code as a `BackendCapabilities` field, so the
orchestrator can gate on it rather than pretending.

**1. The safety guard becomes detection, not prevention.**
On the Claude path, `agent/guard.py` runs as a *PreToolUse hook*: a push to a
protected branch, a write to `.env`, an `rm -rf`, a `gh pr merge` is **denied
before it executes**. `codex exec` has no such hook. The same pure policy runs
here on the *observed* event, and a violation kills the session and fails the
attempt — but the command has already run when we see it. Denial events carry
`meta["post_hoc"] = True` so nothing can confuse the two.

The mitigation that *is* real prevention is the sandbox: coder sessions run
`--sandbox workspace-write`, read-only sessions `--sandbox read-only`.
`--dangerously-bypass-approvals-and-sandbox` is never used. But a sandbox
enforces "inside the workspace"; it does not know about `.env` or about which
branches are protected.

A coder session also needs the network (git fetch/push, `gh`, pip installs)
to do its job at all. codex-cli's `workspace-write` sandbox has **no**
network access by default — measured directly (`git ls-remote` inside a bare
`workspace-write` sandbox fails with "Could not resolve host"; the same
command outside any sandbox, or with the grant below, succeeds). The fix is
`sandbox_workspace_write.network_access=true`, paired with an explicit
`--sandbox workspace-write` override — the key is silently inert unless that
mode is active, which is why a naive `-c
sandbox_workspace_write.network_access=true` with no `sandbox_mode` override
still measures blocked. `llm.codex_network_access` (default `true`) controls
this grant; setting it `false` restores the network-less default. It is
never emitted for a read-only session, which has no `sandbox_workspace_write`
table for the key to attach to. See `tests/test_codex_sandbox_network.py`
for the measured evidence.

**2. No supervisor, no lint feedback, no scope guard.**
All three are PostToolUse hooks. `codex exec` has none, so the orchestrator
switches them off for the attempt and emits a `backend_degraded` event saying
so. It does not report "supervisor active" for a session where nothing
supervised.

**3. No Agent Skills and no named subagents.** Both are Claude Agent SDK
concepts. The coder still has the full local toolset the Codex CLI ships; it
just cannot delegate to `no_human_researcher` or load a `SKILL.md`.

**4. The mid-attempt budget watch can only bite between turns.**
The Claude stream reports usage per assistant message, so a runaway attempt is
aborted mid-flight. Codex reports usage at the end of a turn. The lifetime and
per-attempt ceilings are still enforced; the granularity is coarser, so a single
turn can overshoot before the abort fires.

**5. `max_turns` is enforced by no_human, not by the vendor.** Turns are counted
from the event stream (each command execution or file change is one) and the
session is killed when the ceiling is crossed. Same ceiling, different enforcer.
A session that only ever emits assistant text or reasoning — no tool calls —
never advances that turn counter, so it is bounded separately by a per-turn
**event** ceiling (`max_turns` times a fixed events-per-turn factor, or an
absolute ceiling when `max_turns` does not bound the session). Hitting it
still ends the session with `stop_reason="max_turns"`, so it is reported the
same way as an ordinary turn exhaustion.

**6. Cost figures are less precise.** OpenAI has no billed cache-*write* class,
so `cache_creation_tokens` is legitimately 0 rather than unmeasured — but
`core/pricing.py`'s per-model output premium carries published **Anthropic**
prices only. A Codex model id is unknown to it and prices at the conservative
fallback premium, and is reported by `unknown_pricing_models()`. Dollar figures
for Codex runs are estimates with a wider error bar than Claude ones.

## `local` — your own model server

> **Your model must support extended thinking, or the first turn fails.**
> The harness enables thinking on coder sessions; a server whose model
> rejects it 500s before any token (measured 2026-09-01: `qwen2.5-coder`
> behind an Anthropic-compatible proxy returned HTTP 500 "does not support
> thinking"). Details below.

`local` is still the Claude Agent SDK harness (the same `claude` CLI the
default backend runs) — only three environment variables change what it talks
to. It is for a self-hosted or third-party server that speaks the Anthropic
`/v1/messages` API, not a different agent loop.

```yaml
# ~/.no_human/config.yaml
worker:
  backend: local
llm:
  local_model: <the model id the local server exposes>   # REQUIRED, no default
  local_base_url: http://localhost:8000                  # REQUIRED, no default
  local_cli_path: null                                    # null ⇒ the SDK-bundled CLI
```

```bash
# ~/.no_human/.env  (chmod 600, gitignored) — only if your server enforces a key
LOCAL_LLM_API_KEY=whatever-your-server-expects
```

**The child process env is exactly three entries**, injected into that one
subprocess's environment only — never into `os.environ`, never into any other
role's session (planner/supervisor/utility stay on Claude regardless of this
setting, per `CLAUDE_PINNED_ROLES`; the reviewer stays on Claude too unless an
explicit `llm.role_backends.reviewer` Settings choice says otherwise, which is
independent of and unaffected by this local-backend setting):

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `llm.local_base_url`, verbatim |
| `ANTHROPIC_API_KEY` | `LOCAL_LLM_API_KEY` from `~/.no_human/.env` if set, else the literal `no-key-local-backend` |
| `CLAUDE_CODE_OAUTH_TOKEN` | explicitly set to `""` |

That last line is deliberate, not incidental: a local run must never carry your
real subscription/enterprise OAuth token to a third-party server, so it is
overridden to empty rather than left to whichever credential the CLI happens to
prefer.

**Your model must support extended thinking.** The harness enables thinking on
coder sessions, and a server whose model rejects it fails the attempt on turn
one (measured 2026-09-01: `qwen2.5-coder` behind an Anthropic-compatible proxy
returned HTTP 500 "does not support thinking" before any token; a
thinking-capable model on the same stack ran normally). Capability is on the
model, not the harness: pick one that both supports thinking and is strong
enough to drive an agent loop.

**`llm.local_base_url` is validated, not trusted, before any subprocess
starts** (`config.assert_local_backend_mode`):

- it must be set — an ambient `ANTHROPIC_BASE_URL` in your shell is scrubbed
  and never used as a fallback;
- `http`/`https` only;
- the host must be `localhost` or a **literal** loopback/RFC1918 IP address —
  a DNS name is refused even if it currently resolves to one, because a name is
  resolved again at connect time, which is a rebinding surface;
- a public/routable IP is refused — local mode must not leave the machine;
- no userinfo credentials embedded in the URL (`http://user:pass@host` is
  refused) — if your server needs a key, it goes in `.env` as
  `LOCAL_LLM_API_KEY`, never in the URL or in config.yaml.

**Honest limits.** This is still your own local model, not Claude, wearing the
Claude harness:

- answer quality is entirely the local model's, not Anthropic's — no_human does
  not evaluate or curate it;
- `thinking_budget` is off (`BackendCapabilities.thinking_budget=False`) —
  extended-thinking wiring is Anthropic-specific and a third-party server has
  no reason to implement it;
- `cache_creation_accounting` is off — most local servers do not bill or report
  prompt-cache writes the way Anthropic's API does, so that figure is not
  tracked rather than reported as zero;
- nothing here is billed to Anthropic — but `core/pricing.py` has no published
  price for an arbitrary local model id, so cost figures for `local` runs price
  at the conservative unknown-model fallback and the model id is named in
  `unknown_pricing_models()`;
- only loopback/RFC1918 addresses are accepted — there is no supported way to
  point `local` at a remote/hosted server; that is what `claude`'s BYO-API-key
  mode or `codex` are for;
- the reviewer, planner, supervisor and utility tiers are unaffected — only
  `role="coder"` ever consults `worker.backend` (`resolve_backend_name`) or a
  task's `--backend`, so a `local` run still bills Anthropic for everything
  except the implementer.

**A dead local server parks as infra, never as a quota wall.** If the coder's
SDK session dies before producing any tokens (a proxy/server 500, a model that
rejects a capability the CLI requested, an unreachable base URL), the
classifier used to route this into `paused_quota` — which names a
*subscription* reset. `local`'s child env has no subscription (`_local_child_env`
always blanks `CLAUDE_CODE_OAUTH_TOKEN`), so that park would sit forever
waiting on a reset that can never come. `local_run_without_subscription`
(`agent/backend.py`) detects this — true only when the resolved backend is
`local` **and** the actual child env carries no OAuth token — and routes the
death to a `TRANSIENT_INFRA` park instead, with a 30-minute auto-retry wake and
the local server's own error text (its stderr/500 body) carried in the
blocker's evidence. `backend=claude` quota routing is unchanged.

## Adding a fourth backend

`agent/backend.py` is the seam: implement `CodingBackend`, declare a
`BackendCapabilities`, add a branch to `make_backend`. The orchestrator is typed
against the protocol and does not import either vendor's SDK on the coder path.
Read the seam module's docstring first — it states which parts of the contract
are load-bearing and why, including the one that is easy to get wrong: an
exception raised by the `on_event` callback must propagate out of `run`, because
that raise is how task cancellation, the budget abort and doom-loop detection
all stop a running attempt.
