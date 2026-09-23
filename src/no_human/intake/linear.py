"""Linear issue intake — GraphQL search by operator-authored filter, normalize → Task.

Deliberately shaped like ``intake/jira.py``, because it plays the SAME role: a
server-side POLLED issue source, not an argument to ``nh task add``. Config
lives in ``integrations.linear`` (team_key / state_types / label); the API key
is ``LINEAR_API_KEY`` in ``~/.no_human/.env`` (never config, never logged).

API FACTS THIS MODULE DEPENDS ON (linear.app/developers, verified 2026-08-01)
----------------------------------------------------------------------------
* **Endpoint** ``https://api.linear.app/graphql``. POST + JSON only; Linear
  publishes no REST API.
* **Auth header is the RAW key** — ``Authorization: <key>``, *not*
  ``Bearer <key>``. Linear's own curl example on the GraphQL page uses the bare
  form for personal API keys; ``Bearer`` is used only for OAuth access tokens.
  Getting this wrong is a 401 that looks like a bad key. (Whether the API also
  tolerates ``Bearer`` with a personal key is undocumented; we send what is
  documented.)
* **``WorkflowState.type`` is a plain ``String``, not a GraphQL enum**, with
  seven values: ``triage``, ``backlog``, ``unstarted``, ``started``,
  ``completed``, ``canceled``, ``duplicate``. Because it is not an enum, a
  typo'd literal fails at runtime rather than at query validation, and an
  exhaustive six-way mapping breaks on real data — hence :data:`STATE_TYPES`
  and the validation in :meth:`LinearAdapter.transition`.
* **Rate limiting answers HTTP 400, never 429**, with a GraphQL error whose
  ``extensions.code`` is ``RATELIMITED``. Branching on status alone would both
  miss the throttle and misclassify it as a permanent client error, so
  :func:`_raise_for_graphql_errors` branches on the code.
* **Status does not classify the failure.** Field-level errors arrive at 200,
  auth failure at 401, throttling at 400 — every one of them carries an
  ``errors`` array. So the ``errors`` array is parsed on *every* response.

CONFIG THAT MATCHES NOTHING IS REFUSED, NOT ANSWERED WITH AN EMPTY LIST
----------------------------------------------------------------------
Every one of ``team_key``, ``state_types`` and ``label`` is a filter Linear
applies server-side, and Linear answers a filter that matches nothing with
``nodes: []`` and no error — an unset team key, a typo'd one (``N0`` for
``NO``), a ``state.type`` that is not one of the seven (``Backlog`` for
``backlog``, since the field is a plain String), and a label the workspace does
not have ALL produce exactly the same empty page as a team that genuinely has
no open work. Measured: each returned 0 issues, silently, where the correct
config returned 4. That is the failure ``intake/monday.py`` refuses by name —
"a silent empty result is indistinguishable from 'the board is empty' and would
park a misconfigured integration in permanent quiet" — so this adapter now
refuses it on the same terms, with :class:`LinearConfigError`.

The check is SPLIT by what it costs, because the two halves are different
animals:

* **Offline** (:meth:`LinearAdapter._require_config`, pure): ``team_key`` is
  set, and every ``state_types`` entry is one of :data:`STATE_TYPES`. No I/O,
  so it is safe to run anywhere — including from the integrations registry's
  pure status derivation and from the onboarding write path.
* **Online** (:meth:`LinearAdapter._validate_intake_config`): does ``team_key``
  name a team that exists, does ``label`` name a label that exists. Run only
  from :meth:`search`, which is already making requests, and only ONCE per
  adapter (the answers are cached, and a raised config error is remembered and
  re-raised without a second request). Never at config load: an install must
  not need Linear to be reachable to start.

The online half FAILS OPEN. If the validation query itself errors, is throttled
or comes back in a shape this module does not recognise, the check is skipped
and the search proceeds — a config error is raised only on a definitive answer
("the workspace lists these teams and yours is not among them"). A validation
that turned an unrelated API failure into "your config is wrong" would be worse
than the silence it replaces.

Write-back is opt-in (``integrations.linear.write_back``, default false) and
mirrors the Jira adapter: comments plus **type-matched state moves** resolved at
runtime from the team's own workflow states, never a hardcoded state UUID (they
are per-team and per-workspace). The agent still never closes or merges — it
only advances within the workflow the operator already defined, and it never
moves an issue to ``canceled`` or ``duplicate`` (constraint #2).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from ..core.task import Task
from .criteria import extract_acceptance_criteria

log = logging.getLogger("no_human.intake.linear")

API_URL = "https://api.linear.app/graphql"

# The seven documented WorkflowState.type values. Not an enum in the schema —
# this tuple is the only validation there is.
STATE_TYPES = (
    "triage", "backlog", "unstarted", "started",
    "completed", "canceled", "duplicate",
)

# States the agent may never move an issue INTO. Closing an issue as
# canceled/duplicate is a human judgement, not the agent's (constraint #2).
FORBIDDEN_TARGET_STATES = frozenset({"canceled", "duplicate"})

# Default intake scope: issues nobody has started yet. Operator-overridable via
# integrations.linear.state_types.
DEFAULT_STATE_TYPES = ("triage", "backlog", "unstarted")

_ISSUE_FIELDS = """
      id
      identifier
      title
      description
      url
      priority
      priorityLabel
      createdAt
      updatedAt
      state { id name type }
      assignee { id name displayName }
      labels { nodes { id name } }
      team { id key name }
"""

_SEARCH_QUERY = """
query NoHumanIssues($filter: IssueFilter, $first: Int!, $after: String) {
  issues(first: $first, after: $after, orderBy: updatedAt, filter: $filter) {
    nodes {%s
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % _ISSUE_FIELDS

_STATES_QUERY = """
query NoHumanStates($teamKey: String!) {
  workflowStates(first: 100, filter: { team: { key: { eq: $teamKey } } }) {
    nodes { id name type position }
    pageInfo { hasNextPage endCursor }
  }
}
"""

#: The workspace's teams, for validating ``team_key``. ``teams`` and
#: ``issueLabels`` are Linear's documented root connections and are paged with
#: the same Relay cursor every other query here uses. They were NOT re-verified
#: against the live API when this was written — which is exactly why both
#: callers fail OPEN: if either query is wrong, it errors, the check is skipped
#: and intake continues, rather than a bad guess becoming a false accusation
#: about the operator's config.
_TEAMS_QUERY = """
query NoHumanTeams($first: Int!, $after: String) {
  teams(first: $first, after: $after) {
    nodes { id key name }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_LABELS_QUERY = """
query NoHumanLabels($first: Int!, $after: String) {
  issueLabels(first: $first, after: $after) {
    nodes { id name }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_ISSUE_STATE_QUERY = """
query NoHumanIssueState($id: String!) {
  issue(id: $id) { id identifier state { id name type } }
}
"""

_COMMENT_MUTATION = """
mutation NoHumanComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""

_UPDATE_STATE_MUTATION = """
mutation NoHumanSetState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
    issue { id identifier state { id name type } }
  }
}
"""


class LinearError(RuntimeError):
    """A Linear API failure. Carries the GraphQL ``extensions.code`` when the
    response had one. Never carries the API key or the request headers."""

    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class LinearRateLimited(LinearError):
    """``extensions.code == "RATELIMITED"`` — arrives as HTTP 400, not 429."""


class LinearAuthError(LinearError):
    """``extensions.code == "AUTHENTICATION_ERROR"`` — HTTP 401."""


class LinearConfigError(LinearError):
    """Operator config that is absent, or that cannot match anything.

    Its own class because it is the one failure that is NOT the API's fault and
    NOT retryable: no number of poll ticks fixes an unset ``team_key`` or a
    ``state_types`` entry that is not one of the seven documented values.
    Raised instead of returning an empty list, so a misconfigured integration
    is loud rather than quietly idle. Carries no credential — every instance is
    built from config the operator wrote and from names the API returned.
    """


def state_type_problems(state_types) -> list[str]:
    """Offline validation of ``integrations.linear.state_types``.

    A list of human-readable problems, empty when the value is usable. Pure and
    import-cheap on purpose: the integrations registry's *synchronous* status
    derivation and the onboarding write path both call it, and neither may make
    a request. ``WorkflowState.type`` is a plain String in Linear's schema, so
    this tuple is the only validation that exists anywhere — a typo is not a
    query error, it is an empty result set.
    """
    problems: list[str] = []
    if isinstance(state_types, str) or not isinstance(state_types, (list, tuple)):
        return [f"must be a list of state types, not {type(state_types).__name__}"]
    for raw in state_types:
        if not isinstance(raw, str):
            problems.append(f"{raw!r} is not a state type")
            continue
        if raw in STATE_TYPES:
            continue
        hint = ""
        for known in STATE_TYPES:
            if raw.strip().casefold() == known:
                hint = f" (did you mean {known!r}? the values are lowercase)"
                break
        problems.append(f"{raw!r} is not one of Linear's seven "
                        f"WorkflowState.type values{hint}")
    return problems


def _raise_for_graphql_errors(payload: Any, status: int) -> None:
    """Raise the right :class:`LinearError` subclass for a GraphQL error body.

    Called for EVERY response, whatever the status: Linear returns field-level
    errors at 200, auth failure at 401 and throttling at 400, and all three
    carry the same ``errors`` array. Only the message text and the error code
    are surfaced — never the request, which holds the API key.
    """
    if not isinstance(payload, dict):
        raise LinearError(f"malformed Linear response (HTTP {status})", status=status)
    errors = payload.get("errors")
    if not errors:
        return
    first = errors[0] if isinstance(errors, list) and errors else {}
    ext = (first.get("extensions") or {}) if isinstance(first, dict) else {}
    code = str(ext.get("code") or "")
    message = str(first.get("message") or "Linear API error") if isinstance(first, dict) else "Linear API error"
    if code == "RATELIMITED":
        raise LinearRateLimited(message, code=code, status=status)
    if code == "AUTHENTICATION_ERROR":
        raise LinearAuthError(message, code=code, status=status)
    raise LinearError(message, code=code, status=status)


class LinearAdapter:
    kind = "linear"

    def __init__(self, config: dict | None = None):
        cfg = ((config or {}).get("integrations") or {}).get("linear") or {}
        self.team_key = (cfg.get("team_key") or "").strip()
        raw_types = cfg.get("state_types")
        self.state_types = tuple(raw_types) if raw_types else DEFAULT_STATE_TYPES
        self.label = (cfg.get("label") or "").strip()
        self.default_repo = cfg.get("default_repo") or None
        self.write_back = bool(cfg.get("write_back", False))
        self.page_size = int(cfg.get("page_size", 50) or 50)
        # Key from the process env only (loaded from ~/.no_human/.env at the
        # CLI/API boundary). Never read from config, never logged.
        self.api_key = os.environ.get("LINEAR_API_KEY")
        # Per-instance cache of the team's workflow states. They are stable
        # (an operator edits them rarely) and every write-back would otherwise
        # spend a request re-fetching them.
        self._states_cache: list[dict[str, Any]] | None = None
        # The ONLINE intake-config check runs at most once per adapter: the
        # verdict is remembered, so a misconfigured install is loud on every
        # tick without spending a request on every tick. `None` = not attempted
        # yet; a stored error is re-raised as-is.
        self._intake_checked = False
        self._intake_config_error: LinearConfigError | None = None

    @property
    def configured(self) -> bool:
        return bool(self.team_key and self.api_key)

    def _headers(self) -> dict[str, str]:
        # RAW key, not "Bearer <key>" — see module docstring.
        return {
            "Authorization": self.api_key or "",
            "Content-Type": "application/json",
        }

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GraphQL round-trip. Returns the ``data`` object.

        Raises :class:`LinearError` (or a subclass) on any GraphQL error,
        regardless of HTTP status. Never raises anything carrying the key.
        """
        if not self.api_key:
            raise LinearAuthError("LINEAR_API_KEY not set in ~/.no_human/.env")
        resp = httpx.post(
            API_URL, headers=self._headers(), timeout=30.0,
            json={"query": query, "variables": variables or {}},
        )
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body is still a failure
            raise LinearError(
                f"non-JSON Linear response (HTTP {resp.status_code})",
                status=resp.status_code,
            ) from None
        _raise_for_graphql_errors(payload, resp.status_code)
        if not 200 <= resp.status_code < 300:
            # No errors array but a non-2xx status: still a failure, never a
            # silently-empty success.
            raise LinearError(f"Linear HTTP {resp.status_code}", status=resp.status_code)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LinearError(
                f"Linear response had no data (HTTP {resp.status_code})",
                status=resp.status_code,
            )
        return data

    # --------------------------- config guards ------------------------------ #

    def _require_config(self) -> None:
        """Fail loudly on config that cannot match, naming the exact key.

        OFFLINE — no request is made, so this runs before anything is spent on
        the wire and works on a machine with no network. Every entry point that
        would otherwise degrade to an empty list calls it first.
        """
        if not self.team_key:
            raise LinearConfigError(
                "Linear intake is not configured: set integrations.linear.team_key "
                "in ~/.no_human/config.yaml. It is the issue-id PREFIX — 'ENG' in "
                "ENG-123 — not the team's display name."
            )
        problems = state_type_problems(list(self.state_types))
        if problems:
            raise LinearConfigError(
                "integrations.linear.state_types is not usable: "
                + "; ".join(problems)
                + ". Linear's WorkflowState.type is a plain String, so a value "
                "outside " + ", ".join(repr(t) for t in STATE_TYPES) + " matches "
                "no issue and the team would read as empty — intake refuses to "
                "run instead of returning nothing."
            )

    def _paged_field(self, query: str, root: str, field: str) -> tuple[list[str], bool]:
        """Walk a Relay connection collecting one string field per node.

        Returns ``(values, complete)``; ``complete`` is False when the walk
        stopped at the page bound with more pages left, which is the one case a
        membership test must NOT be run on — an incomplete list would accuse a
        perfectly good config of naming something that does not exist.
        """
        values: list[str] = []
        after: str | None = None
        for _ in range(10):
            data = self._post(query, {"first": 250, "after": after})
            conn = data.get(root)
            if not isinstance(conn, dict):
                raise LinearError(f"Linear response had no {root} connection")
            for node in conn.get("nodes") or []:
                value = (node or {}).get(field)
                if isinstance(value, str) and value:
                    values.append(value)
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return values, True
            after = page.get("endCursor")
            if not after:
                return values, True
        return values, False

    def _probe_intake_config(self) -> LinearConfigError | None:
        """The ONLINE half: is the configured team/label a thing that exists?

        Returns the error to raise, or None. Never raises: a validation query
        that fails, is throttled, or answers in an unexpected shape leaves
        intake alone (logged at debug and skipped), because the cost of a wrong
        accusation here is a working install that stops working, while the cost
        of skipping is only the silence that already existed.
        """
        try:
            keys, complete = self._paged_field(_TEAMS_QUERY, "teams", "key")
        except LinearError as exc:
            log.debug("Linear team_key check skipped: %s", type(exc).__name__)
            return None
        if complete and keys and self.team_key not in keys:
            hint = ""
            for key in keys:
                if key.casefold() == self.team_key.casefold():
                    hint = f" Did you mean {key!r}? (team keys are case-sensitive)"
                    break
            return LinearConfigError(
                f"integrations.linear.team_key is {self.team_key!r}, which is not "
                "a team in this workspace, so every poll would return nothing."
                + hint
                + " Teams this API key can see: "
                + ", ".join(repr(k) for k in sorted(keys))
            )
        if not self.label:
            return None
        try:
            labels, complete = self._paged_field(_LABELS_QUERY, "issueLabels", "name")
        except LinearError as exc:
            log.debug("Linear label check skipped: %s", type(exc).__name__)
            return None
        if complete and labels and self.label not in labels:
            hint = ""
            for name in labels:
                if name.casefold() == self.label.casefold():
                    hint = f" Did you mean {name!r}? (label names are case-sensitive)"
                    break
            return LinearConfigError(
                f"integrations.linear.label is {self.label!r}, which is not a label "
                "in this workspace, so every poll would filter every issue out."
                + hint
                + " Labels this API key can see: "
                + ", ".join(repr(x) for x in sorted(set(labels)))
            )
        return None

    def _validate_intake_config(self) -> None:
        """Run the online check once per adapter and re-raise its verdict."""
        if not self._intake_checked:
            self._intake_config_error = self._probe_intake_config()
            self._intake_checked = True
        if self._intake_config_error is not None:
            raise self._intake_config_error

    # ------------------------------- read ---------------------------------- #

    def _filter(self, updated_since: str | None = None) -> dict[str, Any]:
        """The operator-authored intake scope. Never built from a task's text.

        ``state.type`` uses ``in`` over the configured types (default: the
        not-yet-started ones). A configured label narrows further.
        """
        flt: dict[str, Any] = {
            "team": {"key": {"eq": self.team_key}},
            "state": {"type": {"in": list(self.state_types)}},
        }
        if self.label:
            flt["labels"] = {"name": {"in": [self.label]}}
        if updated_since:
            flt["updatedAt"] = {"gt": updated_since}
        return flt

    def search(self, updated_since: str | None = None) -> list[dict[str, Any]]:
        """Run the configured filter and return matching issues.

        Pages via the documented Relay cursor (``pageInfo.hasNextPage`` /
        ``endCursor``) so a backlog bigger than one page is not silently
        truncated. Bounded at 10 pages so a misconfigured filter cannot spin
        the poll tick forever.

        The config is checked against the WORKSPACE before the first page is
        fetched (:meth:`_require_config`, then :meth:`_validate_intake_config`)
        — every filter this sends is one Linear answers with an empty page when
        it names something that does not exist, and an empty page is exactly
        what a team with no open work looks like. A correct config that matches
        zero issues right now still returns ``[]`` and says nothing: what is
        validated is that the config NAMES REAL THINGS, never that the result
        was non-empty.
        """
        self._require_config()
        self._validate_intake_config()
        out: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(10):
            data = self._post(_SEARCH_QUERY, {
                "filter": self._filter(updated_since),
                "first": self.page_size,
                "after": after,
            })
            conn = data.get("issues") or {}
            out.extend(conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                break
        return out

    def search_text(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        """Issue browse/pick for the Backlog page — the Linear half of what
        ``JiraAdapter.search_text`` does, and deliberately the same shape.

        The SCOPE is :meth:`search`'s: the operator-authored intake filter
        (team + state types + optional label), so the picker lists exactly the
        tickets the poller would consider and never a closed one. An empty
        query browses that scope; a non-empty query narrows it.

        The narrowing happens HERE, over the fetched nodes, rather than inside
        the GraphQL filter. That is a deliberate constraint, not laziness: this
        module only sends API shapes its docstring records as verified, and
        Linear's free-text filter fields are not among them — a guessed filter
        key fails at runtime as an opaque 400 (see the module docstring on
        ``WorkflowState.type``, the last time an unverified literal was used
        here). The intake scope is one team's unstarted work, which is the
        page-bounded set ``search`` already pages through in full.
        """
        issues = self.search()
        needle = (q or "").strip().lower()
        if needle:
            issues = [
                i for i in issues
                if needle in " ".join([
                    str(i.get("identifier") or ""),
                    str(i.get("title") or ""),
                    str(i.get("description") or ""),
                ]).lower()
            ]
        return issues[: max(1, limit)]

    def get_issue(self, key: str) -> dict[str, Any] | None:
        """ONE issue by its human identifier ("NO-1"), or None.

        Resolved by scanning the intake scope rather than by
        ``issue(id: "NO-1")``: the API's ``id`` argument is documented for the
        UUID, and the identifier is documented only for display (same reason
        :meth:`normalize` keeps the UUID in ``context``). ``_SEARCH_QUERY``
        already returns the FULL, untruncated description, so unlike Jira there
        is no second, richer fetch to make — the detail route exists to give
        the page a per-ticket call with the same contract, not to un-truncate.
        """
        wanted = (key or "").strip().lower()
        if not wanted:
            return None
        for issue in self.search():
            if str(issue.get("identifier") or "").lower() == wanted:
                return issue
        return None

    def issue_brief(self, issue: dict[str, Any]) -> dict[str, Any]:
        """Shape one issue for the browse list — never the full Task shape
        ``normalize`` builds. Description truncated for the list payload,
        exactly like the Jira adapter's brief."""
        out = self.issue_detail(issue)
        out["description"] = out["description"][:2000]
        return out

    def issue_detail(self, issue: dict[str, Any]) -> dict[str, Any]:
        """The same row with the description whole."""
        ident = issue.get("identifier") or ""
        assignee = issue.get("assignee") or {}
        return {
            "tracker": "linear",
            "key": ident,
            "summary": issue.get("title") or ident or "Linear issue",
            "status": (issue.get("state") or {}).get("name"),
            "assignee": assignee.get("displayName") or assignee.get("name"),
            "updated": issue.get("updatedAt"),
            "url": issue.get("url") or "",
            "description": issue.get("description") or "",
        }

    def workflow_states(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The configured team's workflow states (cached per adapter).

        Guarded by the same offline check as :meth:`search`: with no team key
        this query returns zero states, which makes every write-back a silent
        no-op rather than a reported failure.
        """
        self._require_config()
        if self._states_cache is None or refresh:
            data = self._post(_STATES_QUERY, {"teamKey": self.team_key})
            self._states_cache = (data.get("workflowStates") or {}).get("nodes") or []
        return self._states_cache

    def state_type(self, issue_id: str) -> str:
        """The issue's CURRENT ``state.type``, lowercased.

        The analogue of the Jira adapter's ``status_category``: used by
        write-back so a "needs a human" note is not posted onto an issue a
        human already completed.
        """
        data = self._post(_ISSUE_STATE_QUERY, {"id": issue_id})
        issue = data.get("issue") or {}
        return str(((issue.get("state") or {}).get("type") or "")).lower()

    # ---------------------------- normalize -------------------------------- #

    def normalize(self, issue: dict[str, Any]) -> Task:
        """Pure: map a fetched Linear issue onto an internal Task.

        ``external_id`` is the human ``identifier`` ("ENG-123") — stable,
        readable, and what dedupe keys on. The API's UUID is kept in
        ``context["linear"]["id"]`` because every mutation needs it: the
        identifier is documented only for display, and passing it to
        ``issueUpdate(id:)`` is not a documented contract.
        """
        identifier = issue.get("identifier") or ""
        title = issue.get("title") or identifier or "Linear issue"
        description = issue.get("description") or ""
        task = Task.new(title, source="linear", external_id=identifier,
                        description=description)
        task.acceptance_criteria = extract_acceptance_criteria(
            description, f"Linear issue {task.external_id}")
        state = issue.get("state") or {}
        labels = [
            n.get("name") for n in ((issue.get("labels") or {}).get("nodes") or [])
            if n.get("name")
        ]
        task.context = {
            "linear": {
                "id": issue.get("id") or "",
                "url": issue.get("url") or "",
                "state": state.get("name"),
                "state_type": state.get("type"),
                "labels": labels,
                "priority": issue.get("priorityLabel"),
                "team": (issue.get("team") or {}).get("key"),
            }
        }
        return task

    # ------------------------------- write --------------------------------- #

    def comment(self, issue_id: str, body: str) -> bool:
        """Post a work-note comment. Opt-in (``write_back``).

        Returns False when write-back is disabled (a no-op, not an error), and
        False when Linear answers ``success: false`` — never True for a write
        that did not happen.
        """
        if not self.write_back:
            return False
        data = self._post(_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
        return bool((data.get("commentCreate") or {}).get("success"))

    def resolve_state(self, target_type: str) -> dict[str, Any] | None:
        """The team's own state for ``target_type``, or None.

        Picks the LOWEST ``position`` among that type's states, which is the
        order Linear itself displays them in ("states are displayed in
        ascending order of position within their type group") — so a team with
        several ``started`` states gets its first one, not an arbitrary one.
        Never a hardcoded UUID: state ids are per-team.
        """
        candidates = [s for s in self.workflow_states()
                      if str(s.get("type") or "").lower() == target_type]
        if not candidates:
            return None
        return min(candidates, key=lambda s: (s.get("position") is None, s.get("position")))

    def transition(self, issue_id: str, target_type: str) -> bool:
        """Opt-in (``write_back``). Move the issue into the team's own state of
        type ``target_type``.

        Refuses ``canceled``/``duplicate`` outright — closing an issue that way
        is a human judgement (constraint #2) — and refuses any value outside
        the seven documented types, since ``state.type`` is an unvalidated
        String and a typo would otherwise become a silent no-op. No state of
        that type on the team -> debug log + no-op (``False``).
        """
        if not self.write_back:
            return False
        target_type = (target_type or "").lower()
        if target_type in FORBIDDEN_TARGET_STATES:
            raise ValueError(
                f"no_human never moves an issue to {target_type!r} — "
                "closing/cancelling is a human action"
            )
        if target_type not in STATE_TYPES:
            raise ValueError(f"not a Linear workflow state type: {target_type!r}")
        state = self.resolve_state(target_type)
        if state is None:
            log.debug("Linear %s: team has no state of type %s", issue_id, target_type)
            return False
        data = self._post(_UPDATE_STATE_MUTATION,
                          {"id": issue_id, "stateId": state.get("id")})
        return bool((data.get("issueUpdate") or {}).get("success"))
