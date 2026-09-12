<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

<!-- mcp-name: io.github.no-human-ai/no_human -->

**From ticket to reviewed pull request.**<br>***Free and open-source, on your machine.***

**English** · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [Quickstart](docs/quickstart.md) · [Docs](docs/README.md) · [Watch it work a sprint](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![Download for macOS](https://img.shields.io/badge/Download%20for-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Download for Windows](https://img.shields.io/badge/Download%20for-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Download for Linux](https://img.shields.io/badge/Download%20for-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="The no_human board: one task waiting on a question in Needs answer, four tasks working in parallel, one pull request ready for review." width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">Watch the loop</a> — a ticket in, a reviewed pull request out; the whole loop in 57 seconds.</sub>

</div>

The AI coding factory you <ins>**can trust**</ins>:

- **A plan before any code**, from the ticket plus what it finds in your repo.
  When planning fails, the coder is told it is working without one; when the
  change is judged trivial the plan is skipped without telling the coder, by
  design — the skip is still stated in the run's event stream.
- **An adversarial review.** A different model, in a session that never saw the
  coder's transcript, told to refute "done". You get a pass/fail checklist
  citing file and line — never a numeric self-score.
- **A tamper guard.** Deleted tests, new skips, an assertion turned into a
  tautology — counted mechanically before the review gate runs, then justified
  against your acceptance criteria or the attempt stops.
- **Proof the fix fixed the bug.** The tests offered as evidence must fail at
  the merge base and pass on the new tree — the reproduction gate runs both.
  Out of the box that binds a Python bug fix; `repro_gate.mode: required` binds
  every kind and every change.
- **Your tests run**, locally and optionally through your CI — and a PR that
  found no test command says **NOT RUN** on its face.
- **An honest stop.** When it cannot finish it stops and says why — a specific
  question when your answer would unblock it, a structured record when it has
  simply run out of budget — never an invented plausible diff.

## Install

### One line (CLI + board)

```bash
uv tool install no-human   # or: pipx install no-human — the wheel ships the board
nh init && nh doctor       # token, config, first repo; then prove the install is real
```

### Desktop app

[![Download for macOS](https://img.shields.io/badge/Download%20for-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Download for Windows](https://img.shields.io/badge/Download%20for-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Download for Linux](https://img.shields.io/badge/Download%20for-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

Each release ships a SHA-256 alongside the artifact. Platform notes and the
first-run walk-through: [docs/quickstart.md](docs/quickstart.md).

### From source

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # installs the `nh` entry point into .venv
(cd web && npm install && npm run build)   # builds the board (cold first install can take minutes)
uv run nh init          # token, config, first repo (about 2 minutes)
uv run nh doctor        # verify the install is real before relying on it
```

The `web` build is not optional if you want the board: a source checkout ships
no `web/dist`, so without it `nh start` serves the API only and renders no UI.
Needs Python 3.12+, [uv](https://github.com/astral-sh/uv), git, and Node with
npm for the board build.

## Product highlights

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>A plan before any code</h3>
      <p>Acceptance criteria you can check, written from the ticket and your repo.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="The task's plan: what we understood as three acceptance criteria, the two files to change, the approach, the test plan, what is out of scope, and the verification command." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>An independent reviewer</h3>
      <p>A second model that never saw the coder's session, told to refute "done". Pass or fail; every finding that blocks cites file and line.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="The reviewer's verdict: PASSED, each acceptance criterion ticked with the file and line that satisfies it, one non-blocking nit with the diff it points at." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Your tests, on the PR's face</h3>
      <p>Run locally or through your CI. No test command found reads <b>NOT RUN</b>, never blank.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="The task's Test results panel: CLEAN, 5 passed of 5 total, with the pytest output underneath." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>A tamper guard</h3>
      <p>Deleted tests, new skips and tautological assertions are counted before review. Unjustified, the attempt stops.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="A stopped attempt: a red TAMPER DETECTED banner, the reviewer verdict FAILED, and the blocking finding that three tests were deleted without an acceptance criterion to justify it." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Proof the fix fixed the bug</h3>
      <p>The tests offered as evidence must fail on the old code and pass on the new. The gate runs both, and the event log shows the verdict.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="The task's event log: tests pass, status reviewing, the reviewer's tamper check reading none, the reproduction gate reading pass, required, then lint, commit and the pull request opening." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>An honest stop</h3>
      <p>When it needs you, it parks with one specific question instead of guessing.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="The board's Needs answer lane: one task parked with its question, 'Dedupe by user, or by digest id?', and an Answer question button; the Working and Review PR lanes beside it." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Your tracker's tickets, on your board</h3>
      <p>Pick Jira or Linear tickets from the backlog (monday.com boards are polled). Each one is scoped with you before it starts.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="The Backlog synced from Jira: four matching tickets selected, and a Start 4 tasks button." width="100%" />
    </td>
  </tr>
</table>

<sub>Stills: the real board on a demo workload.</sub>

## Run one task

Run `nh` with no arguments for the shell: your lanes, a live event tail, and an
intake you describe a task to in plain English. Every command below still works.

```bash
nh                                   # the shell
nh start                             # board + worker on 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # the reviewer's evidence checklist
nh diff <id>                         # the diff it wants to ship
nh approve <id>                      # your approval squash-lands the PR (git.approve_identity)
nh reject <id> --reason "..."        # send it back with feedback
```

## Integrations

Point no_human at the tracker you already use and it pulls the tickets to your
board — a tracker's filter lives in your config, never in a task's own text,
and a transport error logs and retries on the next tick instead of crashing
the pool.

| Tracker | How tickets arrive | Filter you configure |
|---|---|---|
| **Jira Cloud** | Polled via REST `search/jql` (HTTP Basic `email:token`) | `integrations.jira.jql` |
| **Linear** | Polled via the GraphQL API | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | Polled via GraphQL v2 | `integrations.monday.board_id` + `status_column` + `todo_labels` |

With write-back on (`write_back`, off by default), the ticket moves with the
task — matched by status category, type, or the label you name, never a
hard-coded transition id — and gets the PR link; a task that needs a human is commented on, never
transitioned. GitHub and
GitLab issues import as tasks by URL, and PRs or MRs open on your own host;
Slack and Teams get a message when a task needs you; Jenkins and CircleCI can
run your test layers and gate the loop. Setup for each:
[docs/adapters.md](docs/adapters.md).

**Watch the Jira flow end to end** — tickets synced from a Jira board, scoped,
implemented, and delivered as a review-passed pull request (click for the full
video with every step):

[![Jira flow demo](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">Play the full demo</a></strong> — 1:33, from Jira board to review-passed PR</p>

## MCP server — hand it work from the agent you are already in

no_human ships an **MCP (Model Context Protocol) server**: a stdio bridge, built
on the official Python MCP SDK, that lets Claude Code, Cursor or any MCP client
file work with your local no_human and check on it.

```bash
nh mcp-serve        # the MCP server, over stdio
```

Two tools, and no more:

| Tool | What it does |
|---|---|
| `task_add(title, description, repo_path)` | Files a task. no_human then plans it, writes the change, runs your tests, has a second model review it, and opens the pull request. |
| `task_status(task_id_or_external_id)` | Returns that task's current state — status, attempts, the PR link once there is one. |

It talks to your own no_human at `http://127.0.0.1:8420` and nothing else: no
auth, because that address is localhost, and no service of ours in between. For
Claude Code, the same server ships as a plugin — this repository is its own
plugin marketplace, so the two tools appear in your session after:

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

Any other MCP client takes the usual stdio entry:

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## Docs

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | Zero to first task, per platform |
| [configuration.md](docs/configuration.md) | Every setting and default |
| [verification.md](docs/verification.md) | The gates, the bounded loop, the limits |
| [security.md](docs/security.md) | Auth boundary, the never-merge rule, guards |
| [blockers.md](docs/blockers.md) | Escalation, wake watcher, `nh reply` |
| [adapters.md](docs/adapters.md) | Intake, context, VCS and CI backends |
| [eval.md](docs/eval.md) | Golden set, replay scoring, shadow mode |
| [CHANGELOG.md](CHANGELOG.md) | What changed, per release |

## Development

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Issues and pull requests welcome; run `uv run pytest -q` before submitting.

If no_human saved you a review cycle, a star helps other people find it:
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## Community

Questions, bug reports and runs worth showing: join the [Discord](https://discord.gg/mSARvj6yW6),
post on [r/no_human](https://www.reddit.com/r/no_human/), or open a
[GitHub issue](https://github.com/no-human-ai/no_human/issues).

## License

MIT — see [LICENSE](LICENSE). The licence covers the code, not the name:
[TRADEMARK.md](TRADEMARK.md) is the policy on using "no_human" and the logo.
Packaging a binary carries obligations the source tree does not, listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
