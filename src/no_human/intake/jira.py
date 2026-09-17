"""Jira Cloud issue intake — search by operator-authored JQL, normalize → Task.

The successor to the (removed) TRACKER adapter for issue-tracker intake. Config lives
in ``integrations.jira`` (site / project_key / jql / email); the API token is
``JIRA_API_TOKEN`` in ``~/.no_human/.env`` (never config, never logged).

Auth is HTTP Basic ``email:token`` (Atlassian Cloud's API-token scheme). Search
uses ``/rest/api/3/search/jql`` — the successor endpoint; the older
``/rest/api/3/search`` is on Atlassian's deprecation path.

Write-back is opt-in (``integrations.jira.write_back``, default false) and now
covers **comments and category-matched transitions** (SCRUM-21): as a task
advances the agent moves its issue into the workflow's own In Progress / Done
status *category* (resolved at runtime from the issue's available transitions,
never a hardcoded transition id). The agent still never closes or merges —
only advances within the workflow the operator already defined (constraint #2
— closing/merging is a human action).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..core.task import Task
from .criteria import extract_acceptance_criteria

log = logging.getLogger("no_human.intake.jira")


def _adf_text(desc: Any) -> str:
    """Flatten a Jira description (plain string or Atlassian Document Format) to
    text. ADF is a nested ``{type, content:[...]}`` tree of ``text`` nodes."""
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    if not isinstance(desc, dict):
        return str(desc)
    out: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "heading":
            level = (node.get("attrs") or {}).get("level", 1)
            level = level if isinstance(level, int) and 1 <= level <= 6 else 1
            out.append("#" * level + " ")
        if node.get("type") == "listItem":
            out.append("- ")
        if node.get("type") == "taskItem":
            state = (node.get("attrs") or {}).get("state")
            out.append("- [x] " if state == "DONE" else "- [ ] ")
        if node.get("type") == "text" and "text" in node:
            out.append(node["text"])
        for child in node.get("content") or []:
            walk(child)
        if node.get("type") in ("paragraph", "heading", "taskItem", "listItem"):
            out.append("\n")

    walk(desc)
    return "".join(out).strip()


def _text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text as a minimal ADF document for the comment API."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class JiraAdapter:
    kind = "jira"

    def __init__(self, config: dict | None = None):
        j = ((config or {}).get("integrations") or {}).get("jira") or {}
        self.site = (j.get("site") or "").rstrip("/")
        self.project_key = j.get("project_key") or ""
        self.jql = j.get("jql") or ""            # operator-authored; never task text
        self.email = j.get("email") or ""
        self.default_repo = j.get("default_repo") or None
        self.write_back = bool(j.get("write_back", False))
        # Token from the process env only (loaded from ~/.no_human/.env at the
        # CLI/API boundary). Never read from config, never logged.
        self.token = os.environ.get("JIRA_API_TOKEN")

    @property
    def configured(self) -> bool:
        return bool(self.site and self.project_key and self.email and self.token)

    def _auth(self) -> tuple[str, str]:
        return (self.email, self.token or "")

    def _search_jql(self) -> str:
        # Operator-authored JQL wins; otherwise a safe default scoped to the
        # configured project (never constructed from any task's text).
        if self.jql:
            return self.jql
        return (f'project = "{self.project_key}" AND statusCategory != Done '
                "ORDER BY updated DESC")

    def _run_search(self, jql: str, max_results: int, fields: str) -> list[dict[str, Any]]:
        """Shared GET against the successor search endpoint — same auth/URL for
        both the operator-authored poll (``search``) and the user-driven free
        text browse (``search_text``)."""
        url = f"{self.site}/rest/api/3/search/jql"
        params = {"jql": jql, "maxResults": max_results, "fields": fields}
        r = httpx.get(url, params=params, auth=self._auth(), timeout=30.0,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json().get("issues", []) or []

    def search(self) -> list[dict[str, Any]]:
        """Run the configured JQL and return the matching issues (with fields)."""
        return self._run_search(
            self._search_jql(), 50, "summary,description,status,labels,issuetype")

    def search_text(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        """Issue browse/pick for the user-driven "Import from Jira" flow —
        distinct from the operator-authored JQL ``search()`` uses for the
        background poller. Never built from any task's own text, only from what
        the operator types into the picker.

        Always scoped to the CONFIGURED project so the picker shows the project
        the integration is wired to. An EMPTY query browses that project's open
        tickets (so the picker lists everything to choose from without the user
        guessing a keyword — the project key itself is not ticket text and would
        match nothing); a non-empty query filters those tickets by free text.
        """
        proj = f'project = "{self.project_key}"' if self.project_key else ""
        q = (q or "").strip()
        if q:
            escaped = q.replace("\\", "\\\\").replace('"', '\\"')
            clause = f'statusCategory != Done AND text ~ "{escaped}"'
        else:
            # Browse: the project's open tickets, the same set the poller sees.
            clause = "statusCategory != Done"
        where = f"{proj} AND {clause}" if proj else clause
        jql = f"{where} ORDER BY updated DESC"
        return self._run_search(
            jql, limit, "summary,description,status,assignee,updated,issuetype")

    def issue_brief(self, issue: dict[str, Any]) -> dict[str, Any]:
        """Shape one search hit for the browse/pick list — a short summary,
        never the full Task shape ``normalize`` builds. Description is
        truncated (list view, not the detail the created task will carry)."""
        key = issue.get("key") or ""
        fields = issue.get("fields") or {}
        assignee = fields.get("assignee") or {}
        description = _adf_text(fields.get("description"))
        return {
            # Which tracker the row came from. The Backlog page lists Jira and
            # Linear together, and a row has to say which one it is: the detail
            # fetch, the created task's `source`, and the dedupe key all differ.
            "tracker": "jira",
            "key": key,
            "summary": fields.get("summary") or key or "Jira issue",
            "status": (fields.get("status") or {}).get("name"),
            "assignee": assignee.get("displayName"),
            "updated": fields.get("updated"),
            "url": f"{self.site}/browse/{key}" if key else self.site,
            "description": description[:2000],
        }

    def issue_detail(self, issue: dict[str, Any]) -> dict[str, Any]:
        """Same shape as ``issue_brief`` but with the FULL, untruncated
        description — used only when a single picked issue is fetched for
        import (SCRUM-9). The browse list keeps ``issue_brief``'s [:2000]
        truncation; only this detail path carries the whole spec."""
        brief = self.issue_brief(issue)
        fields = issue.get("fields") or {}
        brief["description"] = _adf_text(fields.get("description"))
        return brief

    def status_category(self, key: str) -> str:
        """Lean read of an issue's CURRENT Jira status-category key
        (``"new"``/``"indeterminate"``/``"done"``), lowercased — only the
        status field. Used by write-back to avoid posting a "needs a human"
        note on an issue a human already moved to Done (SCRUM-53)."""
        url = f"{self.site}/rest/api/3/issue/{quote(key, safe='')}"
        r = httpx.get(url, params={"fields": "status"}, auth=self._auth(),
                      timeout=30.0, headers={"Accept": "application/json"})
        r.raise_for_status()
        fields = (r.json() or {}).get("fields") or {}
        return (((fields.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()

    def get_issue(self, key: str) -> dict[str, Any]:
        """Fetch one issue by key with full fields — the detail GET behind
        the picker's "pick" action (SCRUM-9), so the created task carries the
        issue's full description instead of ``issue_brief``'s list-view
        truncation. Same shape as one ``search``/``search_text`` hit."""
        url = f"{self.site}/rest/api/3/issue/{quote(key, safe='')}"
        params = {"fields": "summary,description,status,assignee,updated,issuetype"}
        r = httpx.get(url, params=params, auth=self._auth(), timeout=30.0,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    def normalize(self, issue: dict[str, Any]) -> Task:
        key = issue.get("key") or ""
        fields = issue.get("fields") or {}
        summary = fields.get("summary") or key or "Jira issue"
        description = _adf_text(fields.get("description"))
        task = Task.new(summary, source="jira", external_id=key, description=description)
        task.acceptance_criteria = extract_acceptance_criteria(
            description, f"Jira issue {task.external_id}")
        task.context = {
            "jira": {
                "url": f"{self.site}/browse/{key}" if key else self.site,
                "status": (fields.get("status") or {}).get("name"),
                "labels": fields.get("labels") or [],
                "issue_type": (fields.get("issuetype") or {}).get("name"),
            }
        }
        return task

    def comment(self, key: str, body: str) -> bool:
        """Post a work-note comment on an issue. Opt-in (``write_back``).
        Returns False when write-back is disabled (a no-op, not an error).
        See ``transition`` for the (separate) category-matched status move."""
        if not self.write_back:
            return False
        url = f"{self.site}/rest/api/3/issue/{quote(key, safe='')}/comment"
        r = httpx.post(url, auth=self._auth(), json={"body": _text_to_adf(body)},
                       timeout=30.0, headers={"Accept": "application/json"})
        r.raise_for_status()
        return True

    def transitions(self, key: str) -> list[dict[str, Any]]:
        """GET the issue's currently-available workflow transitions (only the
        ones valid from its current status)."""
        url = f"{self.site}/rest/api/3/issue/{quote(key, safe='')}/transitions"
        r = httpx.get(url, auth=self._auth(), timeout=30.0,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json().get("transitions", []) or []

    def transition(self, key: str, target_category: str) -> bool:
        """Opt-in (``write_back``). Move the issue via the FIRST available
        transition whose target status category key == ``target_category``
        (``"indeterminate"`` for In Progress, ``"done"`` for Done). Never
        hardcodes a transition id or name, so custom workflows survive. No
        matching transition -> debug log + no-op (``False``)."""
        if not self.write_back:
            return False
        for t in self.transitions(key):
            cat = (((t.get("to") or {}).get("statusCategory") or {}).get("key") or "").lower()
            if cat == target_category:
                url = f"{self.site}/rest/api/3/issue/{quote(key, safe='')}/transitions"
                r = httpx.post(url, auth=self._auth(),
                               json={"transition": {"id": t.get("id")}},
                               timeout=30.0, headers={"Accept": "application/json"})
                r.raise_for_status()
                return True
        log.debug("Jira %s: no transition into category %s", key, target_category)
        return False
