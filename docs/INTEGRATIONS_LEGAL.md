# Integrations: recorded legal/compliance position

This file is the recorded legal/compliance position for each third-party
integration no_human drives — what the vendor's own documentation says, what
it does not settle, and what no_human's code does in response. It is written
so a future reader can *re-judge* the sourcing rather than inherit a
conclusion: every quote below carries a source URL and the date it was
fetched, and every "unresolved" is left unresolved rather than quietly
upgraded to "fine." **Nothing here is legal advice, and nothing here is a
finding of law.**

## Codex

`no_human` can drive OpenAI's Codex CLI as a second coding backend
(`worker.backend: codex`), selected in `agent/codex_backend.py`. Constraint
§6 forbids re-implementing tools an SDK/CLI already provides, which is why
this backend shells out to `codex exec --json` rather than reimplementing a
filesystem/shell loop against the Responses API — see `agent/codex_backend.py`
module docstring for the "why a subprocess" reasoning.

### What OpenAI's own documentation says

Quoted from `https://developers.openai.com/codex/auth` (a page that
308-redirects to `https://learn.chatgpt.com/docs/auth`), fetched **2026-08-22**:

1. "Codex supports two ways for a person to sign in when using OpenAI
   models: Sign in with ChatGPT for subscription access [and] Sign in with
   an API key for usage-based access." (fetched 2026-08-22)
2. "The ChatGPT desktop app, Codex CLI, and IDE extension support both
   sign-in methods for local work." (fetched 2026-08-22)
3. "Use API key authentication for programmatic Codex CLI workflows, such
   as CI/CD jobs. Don't expose Codex execution in untrusted or public
   environments." (fetched 2026-08-22)

Quote 3 is **the unfavourable half, kept rather than dropped**: OpenAI steers
programmatic workflows — CI/CD jobs, which is nearer to what no_human does,
since it drives the CLI unattended with nobody at the keyboard — to the API
key, not to a ChatGPT sign-in. That is exactly why `"api_key"` stays the
default auth mode even though `"subscription"` is now offered
(`config.py:1442`, `codex_backend.py:36-40`).

### #8338: named as a partial answer, not a settled one

Whether a **third-party tool** may drive that ChatGPT sign-in on a user's
behalf is a separate question the quotes above do not answer. `openai/codex`
discussion **#8338** asked exactly this; an OpenAI maintainer answered only
the licensing half and left the policy half unresolved — unanswered, not
settled either way (`codex_backend.py:42-47`, `config.py:111-114`).

### The withdrawn prohibition

An earlier version of this codebase's comments asserted a flat "OpenAI's
terms prohibit..." sentence. That sentence was **never found in OpenAI's
terms and has been withdrawn** — it is not reintroduced here, and no new
prohibition is asserted in its place (`config.py:113-116`). The current,
narrower position is that BYO-API-key was, until 2026-08-22, the only
sanctioned path as "a deliberately conservative choice under unresolved
legal uncertainty, not a known prohibition"
(`tests/test_egress_allowlist.py:588-591`).

### Auth modes on this codebase's current main

Checked in code at **`src/no_human/config.py`**:
`CODEX_AUTH_MODES = ("api_key", "subscription")` (`config.py:147`) — exactly
two modes, no third path:

- **`api_key`** (default). Requires `OPENAI_API_KEY` from `~/.no_human/.env`
  (never `config.yaml`); the run is pointed at a no_human-owned `CODEX_HOME`
  (`~/.no_human/codex-home`) and refuses to start unless `codex login status`
  against *that* directory reports an api_key-backed session
  (`config.py:88-97`, `codex_backend.py:49-76`). `preferred_auth_method` is
  still emitted in argv as belt-and-braces, but it is **not** the gate:
  codex-cli 0.149.0 silently ignores it.
- **`subscription`** (opt-in, added 2026-08-22). The operator runs
  `codex login` themselves; no_human never calls, wraps, or shells out to
  `codex login` — it only ever calls the read-only `codex login status`
  (`codex_backend.py:77-84`, `config.py:98-105`).

**`~/.codex/auth.json` (the local ChatGPT credential file) is never read,
parsed, copied, or even stat'd by either mode** — presence is checked only
via `codex login status`, matching `docs/BACKENDS.md:120-124` and
`config.py:102`. Both modes also scrub the *other* mode's credentials from
the child environment so a run always bills exactly one path
(`CODEX_SUBSCRIPTION_SCRUB_VARS`, `config.py:157-164`).

### Close

The choices above are the operator's call, taken under stated legal
uncertainty — **not a finding of law — a lawyer should still settle it.**
No claim in this section has been adjudicated, and nothing here should be
read as a compliance sign-off from OpenAI or from anyone else.
