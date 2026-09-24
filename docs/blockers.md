# Blockers & escalation (Part 22)

> A blocker is **never** resolved by lowering the bar. When stuck, the agent
> makes verifiable progress, parks with a wake condition, or escalates with a
> precise diagnosis — it never weakens a test, expands scope, edits acceptance
> criteria, or fakes "done".

## Taxonomy → routing

| Category | Route | Notify |
|----------|-------|--------|
| `TRANSIENT_INFRA` | auto-retry (max 2), then escalate | silent until exhausted |
| `QUOTA` | `paused_quota`; watcher resumes on refresh. A billing wall pauses the whole pool until the reset (a dead SDK session parks only its own task — the pool's response to those is the 3-strike infra breaker); a restarted server re-reads the newest park for its own auth profile and keeps honouring that wall (`nh auth use <other>` + restart is the way past it). A dated reset ("resets Sep 8 at 10am (…)") parses up to 8 days out, vs. 6 hours for an undated one; when the pool only guessed the wall's reset (the 6-hour fallback hour, not a parsed time), the lapse dispatches a single probe task instead of `max_workers` at once, repeating per lapse until a probe finds the wall open (issue #431) | silent |
| `DEPENDENCY_WAIT` | `blocked` + wake condition; watcher polls | silent |
| `MISSING_ACCESS` | escalate immediately | **now** |
| `AMBIGUITY` | `awaiting_input`; ask ONE question | **now** |
| `SCOPE_EXPLOSION` | escalate with a proposed smaller scope | **now** |
| `IMPOSSIBLE` | escalate with evidence | **now** |
| `NOVEL_UNKNOWN` | escalate with the full report | **now** |
| `USER_PAUSED` | `blocked`, parked; a forgotten pause escalates at `max_park` (48h default) — `nh task resume` / `POST /resume` still resume in one step within that window. Harness-only: the agent can never self-declare this category | silent until `max_park` |

Low confidence (`< escalate_on_low_confidence_below`, default 0.6) on an
otherwise-parkable blocker **escalates instead** — unsure what's wrong means ask
a human, don't thrash silently.

## How a blocker is raised

The agent self-reports rather than lowering the bar, emitting a fenced block:

```
BLOCKER_JSON_START
{ "category": "...", "transient": false, "wake_condition": "pr_merged:org/repo#42",
  "root_cause_hypothesis": "...", "confidence": 0.0-1.0,
  "tried": ["alt 1 + result", "alt 2 + result"],
  "question": "the ONE decision needed, or null", "options": ["a", "b"],
  "goal": "...", "evidence": "exact command + output" }
BLOCKER_JSON_END
```

The orchestrator also raises blockers for deterministic failures (size limits →
`SCOPE_EXPLOSION`, CI infra exhausted → escalate, etc.). On any blocker, WIP is
committed as `[WIP-BLOCKED]` so work is never lost (22.5).

## The escalation report (22.4)

Never "I'm stuck." Always the six-part report a human can act on in under a
minute: **Goal · What happened (evidence) · Why blocked · What I tried · What I
need from you · State & resume**.

## Wake-condition watcher (22.7)

A poller re-evaluates parked tasks and resumes them when the machine-checkable
wake condition fires:

- `after:2h` (relative to when parked)
- `quota_refreshed` (resolves against `wake_check_at`)
- `ci_green_on:<branch>` (delegated to a CI checker)
- `pr_merged:<ref>` / `PR <ref> merged` (delegated to a PR checker)

Each parked task has a **max park duration** (`blockers.max_park_duration`,
default 48h) → escalate on timeout so nothing is silently abandoned.
`awaiting_input` never auto-resumes by time — only a human reply moves it.

## CLI

```bash
nh blocked              # list parked/escalated tasks + each one's question
nh blocked --full       # include the full six-part report
nh reply <id> "answer"  # record the answer, resume from the checkpoint, run
nh unblock <id>         # manually resume to implementing
nh unblock <id> --fail  # abandon the task
```

On resume, the task re-enters in a **fresh session** seeded with the prior
diagnosis + your reply (not a stale, bloated context).

## Learning from blockers (22.8 → Part 4.5)

A resolved structural blocker proposes an **anti-pattern** into the
human-confirmed learning queue (`nh learnings`). Confirmed lessons enter the
active rule set so the next occurrence is an anticipated, auto-handled case.
