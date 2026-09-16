"""GitLab issue intake via `glab` (e.g. gitlab.acme.net)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..core.task import Task

_ISSUE_URL = re.compile(
    r"https?://(?P<host>[^/]+)/(?P<path>.+?)/-/issues/(?P<iid>\d+)"
)


@dataclass
class GitLabRef:
    host: str
    project_path: str   # group/subgroup/project
    iid: str


def parse_issue_url(url: str) -> GitLabRef:
    m = _ISSUE_URL.search(url)
    if not m:
        raise ValueError(f"not a GitLab issue URL: {url}")
    return GitLabRef(m["host"], m["path"], m["iid"])


class GitLabAdapter:
    kind = "gitlab"

    def fetch_raw(self, ref: str) -> dict[str, Any]:
        gl = parse_issue_url(ref)
        encoded = quote(gl.project_path, safe="")
        proc = subprocess.run(
            ["glab", "api", "--hostname", gl.host,
             f"projects/{encoded}/issues/{gl.iid}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"glab api failed: {proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        data["_ref"] = {"host": gl.host, "project_path": gl.project_path}
        return data

    def normalize(self, raw: dict[str, Any]) -> Task:
        title = raw.get("title", "") or f"issue !{raw.get('iid')}"
        body = raw.get("description") or ""
        ref = raw.get("_ref", {})
        task = Task.new(title, source="gitlab",
                        external_id=f"{ref.get('project_path')}#{raw.get('iid')}",
                        description=body)
        task.acceptance_criteria = [
            m.strip() for m in re.findall(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$", body, re.M)
        ]
        task.context = {
            "gitlab": {
                "url": raw.get("web_url"),
                "labels": raw.get("labels", []),
                "state": raw.get("state"),
                "host": ref.get("host"),
            }
        }
        return task
