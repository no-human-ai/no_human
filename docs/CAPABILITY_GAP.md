# Capability-gap event contract

This is the canonical, machine-checked contract for the opt-in capability-gap
channel (issue #20). It exists so a reviewer — or a consumer writing a parser —
can answer "what can this event possibly carry" without reading
`src/no_human/capability_gap.py` line by line. `tests/test_capability_gap.py`
pins this file against the code's own closed sets in both directions, so a
value can never ship undocumented and a documented value can never survive
being removed from the code.

The channel is **off by default** and sends nothing anywhere until an operator
turns it on. It is independent of `telemetry.enabled` in both directions:
turning usage insights off does not turn this channel off, and turning this
channel on does not turn usage insights on. See `docs/configuration.md`'s "Capability-gap events" section for
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

**There is no free-text field.** `capability_class`, `reason_code`, every
`constraints` key and value, and `schema` are closed sets; `_validate` in
`capability_gap.py` raises `ValueError` for anything outside them, even when
the channel is disabled. The remaining fields are shape-checked by `_sendable`
before an event is written and again on every spooled line before it is
POSTed: `event_id` must be a canonical uuid string, `ts` a UTC timestamp in
Python `isoformat` form, `synthetic` a boolean, `source.product` the literal
`"no_human"`, and `source.version` / `source.instance` 1–64 characters of
`A-Za-z0-9._+-` — no `/`, space, `:` or `@`, so neither can hold a path or a
URL. A spooled line that fails any of these checks, or is not valid JSON, is
dropped, never sent. The pseudonym is operator-settable
(`capability_gap.instance_pseudonym`); the shape check bounds what it can
look like, not what an operator chooses to put in it.

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
| `backend` | `claude`, `codex`, `local`, `other` |

A key may be absent; a key that is present must carry a value from its own
list. Any other key is a `ValueError`.

## `synthetic`

`true` for anything `telemetry.environment` does not classify as a real
install — a pytest run, a bench replay (`NH_ENV=bench`), a CI job, a source
checkout run under a throwaway HOME — unless `capability_gap.synthetic` forces
the answer. A developer running a checkout with their normal HOME is
classified as real: set `capability_gap.synthetic: true` there. Issue #20 asks for this
flag precisely so a pilot cannot mistake dogfood volume for demand.

## What the built-in producer emits

The orchestrator derives events in exactly one place: `Orchestrator.emit`
hands every emit to `capability_gap.observe`, which reads only metadata that
is *already* a closed enum on that emit (`task_kind`, `blocker_category`,
`reason_category`). It never reads the blocker's prose, which rides along on
the same emit. Only these emit kinds can produce an event (`OUTCOMES_BY_KIND`):
`failed`, `escalated`, `blocked`, `paused_quota`, `awaiting_input`.

A `paused_quota` emit carries no `blocker_category`, so `observe` reads it as
`QUOTA`. Caveat: the same park also records a dead SDK session that was
spared as infra (`QuotaExhausted(infra=True)`), and its emit does not say
which it was — such a park is reported as `QUOTA` too.

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

The spool is always a file named `capability-gap.jsonl` inside
`capability_gap.dir` (`~/.no_human` unless set). The directory is
configurable, the filename is not: compaction and `flush` rewrite this file,
so no setting can aim that rewrite at another file. A spool that is a symlink
is refused (nothing is written or flushed).

`capability_gap.sink: "jsonl"` (the default) appends to that file and
**nothing leaves the machine**. A local consumer tails the file.

`capability_gap.sink: "http"` makes the same file a spool that a daemon thread
drains to `capability_gap.endpoint` in batches of 50, as
`{"schema": "...", "events": [...]}`. Only one flush runs at a time; a flush
that finds another in flight returns without sending, and the next recorded
event starts another. The endpoint must be `https://`, or
`http://` on loopback; anything else — `file://` above all, which `urllib`
would otherwise honour — resolves no destination, which disables the channel
rather than writing somewhere unintended.

Both sinks are fail-open: a full disk, an unreachable endpoint or a corrupt
spool line can never break a task run. Lines that fail `_sendable` are
dropped from the batch and removed with it, so one poisoned line cannot wedge
every later flush behind it.

## The pseudonym

`source.instance` is minted independently of `telemetry.instance_id` and kept
in `~/.no_human/capability-gap-id`, never in `config.yaml` (so it never
appears in `/api/config`'s echo either). A recipient of one channel therefore
cannot join it to the other. Set `capability_gap.instance_pseudonym` to
supply your own; a value that is not 1–64 characters of `A-Za-z0-9._+-` is
ignored, and a pseudonym file whose content is not is re-minted.
