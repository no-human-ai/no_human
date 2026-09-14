# Quickstart — from zero to first task in 5 minutes

## Installed the Windows app? Start here instead

**If you ran `no_human-<version>.exe`, sections 1–3 below are NOT for you.** The
same reasoning as the Mac section that follows: the app carries its own Python,
its own server and its own dependencies, so there is nothing to clone and no
`uv sync` to run. Doing any of it would set up a *second*, unrelated copy. The
same exception applies too — the bundle does **not** carry Node.js or the
Claude Code CLI, and the coding backend shells out to that CLI for every task,
so install both first: Node.js from nodejs.org, then
`npm install -g @anthropic-ai/claude-code`.

Two things specific to Windows, both expected:

* The installer is **not code-signed yet**, so if you downloaded it, Windows SmartScreen says *"Windows protected your PC"*. Choose **More info → Run anyway**. Signing is planned; until then this prompt is normal.
* It installs **per user** — no administrator prompt — into `%LOCALAPPDATA%\Programs\no-human-desktop`. The folder is named after the package while the app itself is called **no_human**; that is normal.

What you actually do:

1. Open **no_human** from the Start Menu.
2. It shows **Connect Claude** and asks for a credential — either a Claude subscription token (it looks like `sk-ant-oat…`) or an Anthropic API key. Paste one and continue.
3. The board opens. Create your first task there.

Then confirm the install is actually working:

```powershell
& "$env:LOCALAPPDATA\Programs\no-human-desktop\resources\nh-server\nh.exe" doctor
```

Expect a `coding backend` line, a mechanism-liveness table, and
`no contradictions, no evidence gaps` with exit code 0. Same command, same
output and same exit codes as the Mac install — only the path to the bundled
binary differs. See
[INSTALLER.md#verify-your-install-is-real](INSTALLER.md#verify-your-install-is-real),
and `WINDOWS.md` for the Windows build and its known limits.

To uninstall: **Settings → Apps → no_human**. Your tasks and credential live in
`~/.no_human` and are deliberately left behind, so reinstalling picks up where
you left off; delete that folder yourself if you want a clean slate.

## Installed the Linux app? Start here instead

> Walked on a real Ubuntu 24.04 desktop on 2026-08-18 (install → first run →
> board → task detail → settings → quit → uninstall); `LINUX.md` §6/§7 record
> what was observed, including the two frictions worth knowing before you
> start.

**If you installed `no_human-<version>-linux-amd64.deb` or ran the AppImage,
sections 1–3 below are NOT for you.** The app carries its own Python, its own
server and its own dependencies — nothing to clone, no `uv sync`. Doing any of
it would set up a *second*, unrelated copy. As on the other platforms the
bundle does **not** carry the Claude Code CLI, which the coding backend shells
out to for every task, so install it first:
`curl -fsSL https://claude.ai/install.sh | bash` (or `npm install -g
@anthropic-ai/claude-code`), then `claude setup-token`.

Two things specific to Linux, both expected:

* The `.deb` is the recommended format: it installs from a double-click (or `sudo apt install ./no_human-<version>-linux-amd64.deb`), adds **no_human** to the application menu, and its installer sets up the sandbox permissions Ubuntu 24.04 needs.
* The AppImage needs FUSE 2 on Ubuntu 22.04+ (`sudo apt install libfuse2t64` on 24.04) and may hit Ubuntu 24.04's user-namespace restriction; `LINUX.md §5` has the exact messages and the way round each. When in doubt, use the `.deb`.

What you actually do:

1. Open **no_human** from the application menu.
2. It shows **Connect Claude** and asks for a credential — either a Claude subscription token (it looks like `sk-ant-oat…`) or an Anthropic API key. Paste one and continue.
3. The board opens. Create your first task there.

Then confirm the install is actually working:

```bash
/opt/no_human/resources/nh-server/nh doctor
```

The same `nh doctor` as the Mac and Windows installs — only the path to the
bundled binary differs. See
[INSTALLER.md#verify-your-install-is-real](INSTALLER.md#verify-your-install-is-real),
and `LINUX.md` for the Linux build, what has actually been verified on Linux,
and its known limits.

To uninstall: `sudo apt remove no-human-desktop`. Your tasks and credential
live in `~/.no_human` and are deliberately left behind.

## Installed the Mac app? Start here instead

**If you opened a `.dmg` and dragged no_human to Applications, sections 1–3
below are NOT for you.** The app carries its own Python, its own server and its
own dependencies — there is nothing to clone and no `uv sync` to run. Doing any
of it would set up a *second*, unrelated copy. The Connect Claude screen below
does what `nh init` (section 3) does for a source install: it writes your
credential to `~/.no_human/`.

The app bundle is self-contained, but the product still needs **two things it
does not ship**: **Node.js** (from nodejs.org) and the **Claude Code CLI**,
which the coding backend shells out to for every task, whichever credential
type you choose. Install the CLI once with
`npm install -g @anthropic-ai/claude-code`. Without it the app starts, the
board loads, and every task dies at the first call.

What you actually do:

1. Install Node.js if you don't have it, then `npm install -g @anthropic-ai/claude-code`.
2. Open **no_human** from Applications.
3. It shows **Connect Claude** and asks for a credential — either a Claude subscription token (it looks like `sk-ant-oat…`, from `claude setup-token`) or an Anthropic API key from console.anthropic.com. Paste one and continue.
4. The board opens. Create your first task there.

If you ever need that screen again — a revoked or mistyped token strands the
app otherwise — it is **File → Re-enter Claude Token…**.

Then confirm the install is actually working — the same liveness check
section 3 points source installs to, reachable without one:

```bash
/Applications/no_human.app/Contents/Resources/nh-server/nh doctor
```

`nh doctor` is a subcommand of the binary the app already bundles, not
something the source install adds — see
[INSTALLER.md#verify-your-install-is-real](INSTALLER.md#verify-your-install-is-real)
for expected output (both a healthy and a failing run) and troubleshooting.

Everything from section 4 onward applies to you too, EXCEPT that commands are
written as `uv run nh …` for the source install. The packaged app runs the same
server internally, so use the board rather than the CLI unless you have also
installed from source.

---

## 1. Install prerequisites

```bash
# macOS
brew install python@3.12 uv git
npm install -g @anthropic-ai/claude-code   # for the Claude CLI

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# ensure python 3.12+ and git are installed via your package manager
npm install -g @anthropic-ai/claude-code
```

## 2. Clone and install no_human

```bash
git clone https://github.com/no-human-ai/no_human.git no_human && cd no_human
uv sync
cd web && npm install && npm run build && cd ..
```

The `web` build is **not** optional if you want the board. A source checkout
ships no `web/dist`, so without it `nh start` boots the API and prints
`board not found … serving the API only` — no UI at all, including the board
the README leads with. Warm caches build in seconds; a cold first `npm install` can take minutes.

`uv sync` installs the `nh` entry point into `.venv/bin/nh`. It does **not** put
`nh` on your `PATH`, so every command below is written as `uv run nh …`. If you
would rather type a bare `nh`, either install it as a tool —

```bash
uv tool install --editable .      # then `nh` works from anywhere
```

— or activate the venv once per shell (`source .venv/bin/activate`) and drop
the `uv run` prefix everywhere.

Don't want a checkout at all? The released wheel on PyPI ships the board
built in, so one line gives you a bare `nh` everywhere:

```bash
uv tool install no-human          # or: pipx install no-human
```

## 3. Run `nh init`

```bash
uv run nh init
```

`nh init` is an interactive wizard: it asks how you want to pay for Claude and
walks you through the token.

For a provisioning script, a Dockerfile or CI, add `--non-interactive` — it asks
nothing and writes exactly what the wizard writes:

```bash
printf %s "$CLAUDE_CODE_OAUTH_TOKEN" | uv run nh init --non-interactive \
  --token-stdin --repo ~/git/my-repo
```

- `--auth-mode subscription|api_key` picks which credential pays (default: subscription)
- `--token-stdin` reads it as one line from stdin — there is no `--token` flag, because argv is visible in `ps` and lands in shell history
- `--repo <path>` onboards a repo; `--no-repo` says so explicitly, and omitting both also skips it
- It never overwrites a credential you already have: the same token is a no-op, a different one is refused — to replace one, use `nh auth set-token` (it reads the token from stdin too)
- No credential at all — none stored, none on stdin — is an error, not a `~/.no_human` that cannot make a call

This guided wizard will:
- Check that python, git, uv, and claude CLI are installed
- Create `~/.no_human/` with secure permissions
- Guide you through subscription token setup (`claude setup-token`)
- Generate `~/.no_human/config.yaml` with a distinct agent git identity (your own identity is read and shown, but the agent commits under its own)
- Offer to onboard your first repo

Then confirm the install is actually working before you rely on it:

```bash
uv run nh doctor
```

`nh doctor` is a liveness check, and it answers a question no other command
does: **which guarded mechanisms have actually ever fired.** It enumerates every
mechanism's lifetime firings, flags the known silent-death patterns (a gate that
has never run, a watcher that has persisted nothing), and checks the coding
backend is *present* — that the `claude` CLI resolves, and that a credential is
on file. It exits non-zero on a contradiction or an evidence gap, so
`nh doctor || exit 1` works in a pipeline.

Read that last part precisely: by default doctor checks the CLI is present and
a credential *exists* — **not that it works** — because no diagnostic here
spends quota unless you ask. A token that is expired, revoked, or simply
mistyped is still "present", so the default run prints a green backend line
with an explicit "(presence only — no live auth call)" marker and exits 0.
When you want the stronger claim, `uv run nh doctor --verify-auth` asks for
it: one cheap live call, and an expired or revoked token that passes every
other check fails this one. If the network drops the call before the API ever
sees it, doctor says the credential was NOT verified — a transport failure is
reported as exactly that, never as a dead credential.

On a brand-new install most counters will read zero, which is expected — nothing
has run yet. Its value is later: run it whenever something behaves oddly, and
paste it into any bug report.

## 4. Add your first task

```bash
# From a freeform title (works with no tracker at all):
uv run nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo \
  --description "..." --criteria "the test passes 20 runs in a row"

# From a plain sentence (equivalent to --title above):
uv run nh task add "Fix the flaky E2E test" --repo ~/git/my-repo

# From a GitHub or GitLab issue URL:
uv run nh task add https://github.com/org/repo/issues/42 --repo ~/git/my-repo
```

Either form then opens the **scoping grill**: a few interactive questions that
sharpen the spec before any code is written. It needs a terminal, so a piped or
scripted `nh task add` aborts on the first question. Pass `--no-grill` to skip
it and stage the task as written:

```bash
uv run nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo \
  --criteria "the test passes 20 runs in a row" --no-grill
```

`nh task add` takes **a GitHub/GitLab issue URL, a plain sentence, or
`--title`**. A plain sentence (anything that isn't a URL or a source-shaped
token) is filed directly, using it as the task title — same as `--title`. A
bare ticket key such as `PROJ-42` is *not* a supported argument: `ingest_from_url`
raises, the CLI prints `intake failed: not a recognized task URL/id` and exits
1 — the standalone tracker adapter that once accepted it has been removed. Use
`--title` (or a plain sentence) if you want that text as a freeform task. Jira
issues come in through the **poller** instead, not through `nh task add`; see
[adapters.md](adapters.md#jira) for the `integrations.jira` config block.

## 5. Run one in the foreground

```bash
uv run nh watch <task-id>
```

This opens a live Textual TUI showing tool calls, agent reasoning, and progress.

> ⚠️ Despite the name, `nh watch` **runs** the task in a foreground TUI — it is
> not a read-only viewer (`cli/commands.py`: "Run a staged task in the live
> Textual TUI"). Point it only at a *staged* task. Do **not** point it at one
> that `nh start`'s worker is already working, or the task runs twice. To just
> look at a running task, use `uv run nh status`, `uv run nh logs <task-id>`,
> or the web board.

## 6. Check on tasks

```bash
uv run nh task list          # board as a table
uv run nh blocked            # parked/escalated tasks + what each needs
uv run nh status             # portfolio overview
```

`nh dashboard` is an alias for `nh start`: it also starts a task worker and
the wake watcher, so it *runs* queued work rather than just showing it. To only
look, use `nh status` / `nh logs`, or open the board a running server already
serves.

## 7. Review and approve

When a task produces a PR:

```bash
uv run nh review <task-id>   # evidence-backed review checklist
uv run nh diff <task-id>     # the git diff
uv run nh approve <task-id>  # your approval squash-lands the PR (as git.approve_identity)
```

With several tasks awaiting approval, `nh approve --ready` lists every one
whose merge-ready policy verdict is `ready` for its current branch head (the
board shows the same verdict as a `MERGE-READY` chip), and — checked fresh
every time, never cached — whether that branch still merges into its
*current* base right now. Passing the quality rules does not by itself mean
a branch is landable: a sibling PR landing can rewrite generated files
(e.g. `RELEASE_MANIFEST.txt`) and put the branch in conflict with the base
it will actually land onto, without touching its own rules verdict. Both
halves show up on the same line, independently:

```
a1b2c3d4 · Add retry backoff · rules 6/6 · merge: clean · https://github.com/…/pull/12
e5f6a7b8 · Fix flaky timeout · rules 6/6 · merge: CONFLICT with main (RELEASE_MANIFEST.txt) · https://github.com/…/pull/13

1 task(s) ready to land; 1 task(s) pass the quality rules but do NOT merge
into their current base right now — rebase before approving.
```

The conflicted task is never hidden and never auto-resolved; add `--yes` to
land only the ready (non-conflicted) tasks, one at a time through the exact
same approve path, stopping at the first failure — a conflicted task is
skipped with a visible "not landed" message instead. It is still advisory
and still your approval — `--ready`/`--yes` never merges anything
`nh approve <task-id>` wouldn't:

```bash
uv run nh approve --ready        # list what's merge-ready; lands nothing
uv run nh approve --ready --yes  # land the ready (non-conflicted) tasks
```

If you want changes:

```bash
uv run nh reject <task-id> --reason "The error handling needs a retry"
```

## 8. Overnight drain (parallel)

Queue up several tasks with `--no-run` so they stage as PENDING instead of
running immediately, then drain them all in one bounded pool:

```bash
uv run nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo --no-run
uv run nh task add --title "Add input validation to isqrt" --repo ~/git/my-repo --no-run
uv run nh task add https://github.com/org/repo/issues/42 --repo ~/git/my-repo --no-run

uv run nh serve --max-workers 3
```

`--max-workers N` runs the pool for this invocation even if
`concurrency.enabled` is `false` in `config.yaml` — no config edit needed.
Every task — one at a time or many — runs isolated in its own git
**worktree** (`isolation.enabled`, on by default), so a run never touches the
checkout you are working in, and parallel tasks never share a checkout or stomp
each other's branch. Isolation is a separate switch from parallelism: turning
it off (`isolation.enabled: false`) puts the agent in your primary checkout,
and a pool wider than one worker is then refused outright. Leave `nh serve` running
overnight; wake up to open PRs and review with `nh review` / `nh approve` as
in step 7 — **merge always stays a human action**, `nh serve` never merges.

From cron or CI, add `--until-empty`: same pool, same graceful drain, but it
stops once nothing is claimable and nothing is in flight. Parked tasks —
blocked, awaiting input, escalated, quota-paused — end the drain without
failing it; they are waiting for you, not broken. The exit code tells the
automation what happened:

| Exit | Meaning |
|------|---------|
| `0` | Drained: nothing claimable, nothing in flight, nothing unaccounted for. |
| `1` | A task this run dispatched ended FAILED, or the drain was cut short by a signal while work was still claimable **and no row was stranded** — a signalled run that also left a stranded row exits `2`, because that check runs first. |
| `2` | A mid-run row exists that no worker in this process owns — a crash orphan, or a row a different process is (or was) driving — and it is not yet claimable. The queue is **not** drained; it is unknown. The output names the task and the seconds remaining until it becomes claimable (once past `_STRANDED_GRACE_S`, 900s, an orphan-recovery sweep picks it up — but not from this run, which has already exited, and which was refused permission to boot beside a live sibling scheduler: recovery arrives with the next scheduler boot, or from the third process that owns the row if one does). |

`--until-empty` never waits out that 900s grace itself — it exits `2`
immediately and names the row, so a cron/CI operator sees why, rather than
silently reporting `0` while the row is still mid-run.

---

## Key files

| Path | Purpose |
|------|---------|
| `~/.no_human/.env` | Secrets (chmod 600): subscription token, CI tokens |
| `~/.no_human/config.yaml` | Configuration: models, git, safety, bounds |
| `~/.no_human/no_human.db` | SQLite database: tasks, attempts, profiles |
| `<repo>/.no_human/project.yml` | Per-repo profile (test/lint/CI commands) |

## Troubleshooting

**`auth error: ANTHROPIC_API_KEY is set`**
→ The default subscription mode scrubs a stray `ANTHROPIC_API_KEY` and aborts
startup so a run bills exactly one path. `unset ANTHROPIC_API_KEY`, or opt in
to `llm.auth_mode: api_key` to bill that key deliberately.

**`auth error: No subscription token found. Expected CLAUDE_CODE_OAUTH_TOKEN in …`**
→ Run `claude setup-token`, then add the token to `~/.no_human/.env`

**`no profile to confirm`**
→ Run `uv run nh onboard <repo>` first, then `uv run nh onboard <repo> --confirm`.
If the proving step prints `[FAILED] test: … (exit N)`, run that command yourself
in the repo to see the real error — onboarding does not yet show it.

**`intake failed: not a recognized task URL/id`**
→ `nh task add` takes an issue URL, a plain sentence, or `--title "…"`. A bare
tracker key is not an accepted argument; see step 4.

**`nh: command not found`**
→ `uv sync` installs `nh` into `.venv`, not onto your `PATH`. Use `uv run nh …`,
or `source .venv/bin/activate` once per shell.
