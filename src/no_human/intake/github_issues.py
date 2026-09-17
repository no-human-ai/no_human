"""GitHub (incl. GitHub Enterprise, e.g. code.example.com) issue intake via `gh`."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from ..core.task import Task
from .criteria import extract_acceptance_criteria

_ISSUE_URL = re.compile(
    r"https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<num>\d+)"
)


@dataclass
class GitHubRef:
    host: str
    owner: str
    repo: str
    number: str


def parse_issue_url(url: str) -> GitHubRef:
    m = _ISSUE_URL.search(url)
    if not m:
        raise ValueError(f"not a GitHub issue URL: {url}")
    return GitHubRef(m["host"], m["owner"], m["repo"], m["num"])


class GitHubAdapter:
    kind = "github"

    def fetch_raw(self, ref: str) -> dict[str, Any]:
        gh = parse_issue_url(ref)
        proc = subprocess.run(
            ["gh", "api", "--hostname", gh.host,
             f"repos/{gh.owner}/{gh.repo}/issues/{gh.number}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh api failed: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        data["_ref"] = {"host": gh.host, "owner": gh.owner, "repo": gh.repo}
        return data

    def normalize(self, raw: dict[str, Any]) -> Task:
        title = raw.get("title", "") or f"issue #{raw.get('number')}"
        body = raw.get("body") or ""
        ref = raw.get("_ref", {})
        task = Task.new(title, source="github",
                        external_id=f"{ref.get('owner')}/{ref.get('repo')}#{raw.get('number')}",
                        description=body)
        task.acceptance_criteria = extract_acceptance_criteria(
            body, f"GitHub issue {task.external_id}")
        task.context = {
            "github": {
                "url": raw.get("html_url"),
                "labels": [l.get("name") for l in raw.get("labels", [])],
                "state": raw.get("state"),
                "host": ref.get("host"),
            }
        }
        return task
