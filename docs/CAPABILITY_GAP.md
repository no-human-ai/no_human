# Capability-gap event contract

This is the canonical, machine-checked contract for the opt-in capability-gap
channel (issue #20). It exists so a reviewer — or a consumer writing a parser —
can answer "what can this event possibly carry" without reading
`src/no_human/capability_gap.py` line by line. `tests/test_capability_gap.py`
pins this file against the code's own closed sets in both directions, so a
value can never ship undocumented and a documented value can never survive
being removed from the code.

The channel is **off by default** and sends nothing anywhere until an operator
turns it on. See `docs/configuration.md`'s "Capability-gap events" section for
the keys, and `docs/security.md` §7 for the security-review framing.

## What a capability gap is, and is not

A capability gap is a **bounded task attempt that stopped because something
the machine needed was missing** — a dead backend, a spent quota, an access it
does not hold, a budget it may not exceed, a decision only a human can make.

An ordinary failure of the change itself is **not** a capability gap and emits
nothing: a red test, a review that rejected the diff, a tamper block, an
exhausted attempt cap, or an escalation the harness could not classify. A
human pausing or cancelling a task is not one either. That exclusion is the
point of the channel — an event stream that counted every failure would say
nothing about capability.

## The event

```json
{
  "schema": "no_human.capability_gap/v1",
  "event_id": "0f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
  "ts": "2026-09-21T09:14:02.481337+00:00",
  "capability_class": "capacity_blocked",
  "reason_code": "quota_exhausted",
  "constraints": {
    "blocker_category": "QUOTA",
    "outcome": "parked",
    "attempts_bucket": "2-5",
    "task_kind": "feature",
    "backend": "claude"
  },
  "source": {"product": "no_human", "version": "0.2.4", "instance": "<pseudonym>"},
  "synthetic": false
}
```

Every field is a closed set or a generated value. **There is no free-text
field**, so no ticket title, description, prompt, source code, diff, log line,
exception body, transcript, path, repo name, credential or personal datum can
travel on this channel — not by configuration, and not by accident.
`_validate` in `capability_gap.py` raises `ValueError` for anything outside
the sets below, even when the channel is disabled, and `_sendable` re-runs the
same validation on every spooled line before it is POSTed.

## `capability_class` — the seven classes

| Class | Means |
|---|---|
| `capability_unavailable` | a required capability, backend or tool was not available |
| `candidate_rejected` | a candidate was found and refused by an explicit constraint |
| `fallback_selected` | a fallback or human escalation was chosen because the intended capability was unavailable |
| `capacity_blocked` | a timeout, rate limit or capacity wall |
| `authorization_blocked` | an authorization or access blocker |
| `verification_unavailable` | an evidence/verification capability was missing |
| `budget_exhausted` | the configured budget was spent |

## `reason_code`

`backend_unavailable`, `dependency_unavailable`, `quota_exhausted`,
`rate_limited`, `timeout`, `access_denied`, `scope_constraint`,
`infeasible_request`, `ambiguous_requirement`, `no_progress`,
`budget_exhausted`, `review_unavailable`.

## `constraints` — closed keys, closed values

| Key | Values |
|---|---|
| `blocker_category` | the blocker taxonomy name that produced the gap: `TRANSIENT_INFRA`, `DEPENDENCY_WAIT`, `STAGNATION`, `QUOTA`, `MISSING_ACCESS`, `SCOPE_EXPLOSION`, `IMPOSSIBLE`, `AMBIGUITY`, `BUDGET_EXHAUSTED` |
| `outcome` | `failed`, `escalated`, `parked`, `needs_answer` |
| `attempts_bucket` | `0`, `1`, `2-5`, `6+` — bucketed, never the precise count |
| `task_kind` | `feature`, `bugfix`, `ci_fix`, `traceability`, `test_gap`, `unknown`, `other` |
| `backend` | `claude`, `codex`, `other` |

A key may be absent; a key that is present must carry a value from its own
list. Any other key is a `ValueError`.

## `synthetic`

`true` for anything that is not a real install — a pytest run, a bench replay,
a CI job, a developer's own checkout — classified by `telemetry.environment`
unless `capability_gap.synthetic` forces the answer. Issue #20 asks for this
flag precisely so a pilot cannot mistake dogfood volume for demand.

## What the built-in producer emits

The orchestrator derives events in exactly one place, `_capability_gap_hook`
in `core/orchestrator.py`, from metadata that is *already* a closed enum on
the event it hooks (`blocker_category`, `reason_category`). It never reads the
blocker's prose, which rides along on the same event.

| Blocker category | Class | Reason |
|---|---|---|
| `TRANSIENT_INFRA` | `capability_unavailable` | `backend_unavailable` |
| `DEPENDENCY_WAIT` | `capability_unavailable` | `dependency_unavailable` |
| `STAGNATION` | `capability_unavailable` | `no_progress` |
| `QUOTA` | `capacity_blocked` | `quota_exhausted` |
| `MISSING_ACCESS` | `authorization_blocked` | `access_denied` |
| `SCOPE_EXPLOSION` | `candidate_rejected` | `scope_constraint` |
| `IMPOSSIBLE` | `candidate_rejected` | `infeasible_request` |
| `AMBIGUITY` | `fallback_selected` | `ambiguous_requirement` |
| `BUDGET_EXHAUSTED` | `budget_exhausted` | `budget_exhausted` |
| `NOVEL_UNKNOWN` | *nothing* | unclassified by construction — a capability claim with no named cause is the overclaim issue #20 rules out |
| `USER_PAUSED` | *nothing* | a human stopped the task; nothing was missing |

Off-ramps that carry no blocker category fall back to `reason_category`:
`infra` → `capability_unavailable` / `backend_unavailable`, `budget_exhausted`
→ `budget_exhausted` / `budget_exhausted`. `review_failed`, `max_attempts`,
`tamper_blocked`, `blocker_parked` and `other` emit nothing.

**`verification_unavailable` is in the vocabulary and the built-in producer
cannot currently emit it.** Stated here rather than left implied: a review
gate that could not *run* is reported through the ordinary `TRANSIENT_INFRA` /
`NOVEL_UNKNOWN` blockers, which are indistinguishable at the hook from any
other infra death, and inventing a distinction from the blocker's prose would
put free text on a wire whose whole guarantee is that there is none. The class
stays published because the vocabulary is the boundary, and `record` in
`capability_gap.py` accepts it from any producer that *can* tell the
difference. `tests/test_capability_gap.py` pins both halves, so the day the
harness grows that signal the gap is visible rather than forgotten.

## Sinks

`capability_gap.sink: "jsonl"` (the default) appends to
`capability_gap.path` — `~/.no_human/capability-gap.jsonl` unless set — and
**nothing leaves the machine**. A local consumer tails the file.

`capability_gap.sink: "http"` makes the same file a spool that a daemon thread
drains to `capability_gap.endpoint` in batches of 50, as
`{"schema": "...", "events": [...]}`. The endpoint must be `https://`, or
`http://` on loopback; anything else — `file://` above all, which `urllib`
would otherwise honour — resolves no destination, which disables the channel
rather than writing somewhere unintended.

Both sinks are fail-open: a full disk, an unreachable endpoint or a corrupt
spool line can never break a task run. A batch with nothing sendable in it is
dropped rather than left at the head of the spool, so one poisoned line cannot
wedge every later flush behind it.

## The pseudonym

`source.instance` is minted independently of `telemetry.instance_id` and kept
in `~/.no_human/capability-gap-id`, never in `config.yaml` (so it never
appears in `/api/config`'s echo either). A recipient of one channel therefore
cannot join it to the other. Set `capability_gap.instance_pseudonym` to
supply your own.
