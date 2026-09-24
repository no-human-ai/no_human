#!/usr/bin/env python3
"""What the release build fetches over the network, and how often that fails.

Two subcommands, both read-only:

  deps       Enumerate the binaries `electron-builder` and `@electron/get`
             download at package time, read straight from the installed
             build's own toolset-definition files
             (`desktop/node_modules/app-builder-lib/out/toolsets/*.js`), never
             assumed or hand-typed. `--check-doc` fails when
             `docs/DISTRIBUTION.md` §6 has drifted from this list.

  failures   Count fetch failures across prior release dispatches, via `gh`.
             Classification is three-valued (`fetch_failure` / `other_failure`
             / `unclassifiable`) — an expired or inaccessible log is never
             silently read as "no failure", and an unrelated build break is
             never counted as evidence for a CDN outage.

Both fail CLOSED: a missing build (`deps`) or a `gh` error (`failures`) exits
non-zero with a named message, never a quiet empty/zero answer — the same
discipline `scripts/measure_cache_burn.py` uses.

This script never edits `docs/DISTRIBUTION.md`, never talks to GitHub except
read-only `gh run list` / `gh api .../jobs` / `gh api .../logs` calls, and adds
no retry loop of its own — `--gh-timeout` bounds each call and a failure is
reported, not retried.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------
# deps
# --------------------------------------------------------------------------

DEFAULT_MIRROR_ORG_REPO = "electron-userland/electron-builder-binaries"

# The toolset-definition files electron-builder ships with the installed
# package. Read as plain text — never `require()`d — so enumeration needs no
# node process and executes none of node_modules' code.
TOOLSET_FILES = ["windows.js", "linux.js", "7zip.js", "wine.js", "icons.js"]

_CHECKSUM_RE = re.compile(
    r'"([A-Za-z0-9_.\-]+\.(?:7z|tar\.gz|tar\.xz|zip))"\s*:\s*"([0-9a-f]{64})"'
)
_GETBIN_RE = re.compile(
    # Compiled TypeScript interop calls this as `(0, mod.getBinFromUrl)(...)`,
    # so the name can be followed by a stray `)` before the real call's `(`.
    r'getBinFromUrl\)?\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([0-9a-f]{64})"'
    r'(?:\s*,\s*"([^"]+)")?\s*\)'
)
_MIRROR_DEFAULT_RE = re.compile(r'githubOrgRepo\s*=\s*"([^"]+)"')


class MissingBuildDependency(SystemExit):
    """The installed build this enumerator reads from is not on disk.

    A `SystemExit` subclass, not a plain exception: the failure mode this
    guards is "the list came back empty and nobody noticed", so the natural
    call site (a bare `enumerate_binary_deps(...)`) must not be able to
    swallow it with a broad `except Exception`.
    """


@dataclass(frozen=True)
class BinaryDep:
    """One binary the release build fetches, and where that fact came from."""

    filename: str
    sha256: str
    release: Optional[str]
    url: Optional[str]
    platforms: str
    source_file: str
    source_line: int

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "release": self.release,
            "url": self.url,
            "platforms": self.platforms,
            "source_file": self.source_file,
            "source_line": self.source_line,
        }


def _guess_platforms(filename: str, source_file_name: str) -> str:
    """Best-effort platform label from the filename, source file as fallback.

    Checked in this order deliberately: "darwin" contains the substring
    "win" (d-a-r-WIN), so a naive "win" check first would mislabel every mac
    binary as Windows. macOS/Linux markers are checked before the generic
    "win" substring for exactly that reason.
    """
    name = filename.lower()
    if "darwin" in name or "-mac" in name or "macos" in name:
        return "macOS"
    if "linux" in name:
        return "linux"
    if "win" in name:
        return "windows"
    if source_file_name == "windows.js":
        return "windows"
    if source_file_name == "linux.js":
        return "linux"
    return "all"


def _mirror_org_repo(app_builder_lib_out: Path) -> str:
    """The default GitHub org/repo binaries are fetched from.

    Read from `binDownload.js`'s own default-parameter literal rather than
    hard-coded here, so a future electron-builder bump that changes the
    mirror is caught by re-running `deps`, not silently stale.
    """
    bin_download = app_builder_lib_out / "binDownload.js"
    if bin_download.exists():
        m = _MIRROR_DEFAULT_RE.search(bin_download.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return DEFAULT_MIRROR_ORG_REPO


def enumerate_binary_deps(desktop_dir: Path) -> list[BinaryDep]:
    """Every binary fetched at package time, read from the installed build.

    Raises `MissingBuildDependency` (never returns an empty list silently)
    when `desktop/node_modules/app-builder-lib` is absent — the caller needs
    `npm ci` in `desktop/`, not a report that nothing gets downloaded.
    """
    node_modules = desktop_dir / "node_modules"
    out_dir = node_modules / "app-builder-lib" / "out"
    toolsets_dir = out_dir / "toolsets"
    if not toolsets_dir.is_dir():
        raise MissingBuildDependency(
            f"{toolsets_dir} does not exist. This enumerator reads "
            "electron-builder's own toolset-definition files as text — it "
            "never assumes what they contain — so it needs them on disk "
            f"first: run `npm ci` in {desktop_dir}."
        )

    mirror_org_repo = _mirror_org_repo(out_dir)
    deps: list[BinaryDep] = []
    seen: set[tuple[str, str, int]] = set()

    for name in TOOLSET_FILES:
        path = toolsets_dir / name
        if not path.exists():
            continue
        rel_source = f"desktop/node_modules/app-builder-lib/out/toolsets/{name}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for m in _CHECKSUM_RE.finditer(line):
                filename, sha = m.group(1), m.group(2)
                key = (name, filename, lineno)
                if key in seen:
                    continue
                seen.add(key)
                deps.append(BinaryDep(
                    filename=filename,
                    sha256=sha,
                    release=None,
                    url=None,
                    platforms=_guess_platforms(filename, name),
                    source_file=rel_source,
                    source_line=lineno,
                ))
            for m in _GETBIN_RE.finditer(line):
                release, filename, sha, org_repo = m.groups()
                key = (name, filename, lineno)
                if key in seen:
                    continue
                seen.add(key)
                repo = org_repo or mirror_org_repo
                deps.append(BinaryDep(
                    filename=filename,
                    sha256=sha,
                    release=release,
                    url=f"https://github.com/{repo}/releases/download/{release}/{filename}",
                    platforms=_guess_platforms(filename, name),
                    source_file=rel_source,
                    source_line=lineno,
                ))

    # Synthetic row: the Electron zip itself. Not part of app-builder-lib at
    # all — @electron/get fetches it, triggered by packaging/ensure-electron.mjs
    # (electron 42+ defers the download to first run) — but it is the same
    # build-time download surface, and it is what the v0.2.3 desktop-shell job
    # actually hit its 504 on.
    package_json = desktop_dir / "package.json"
    if package_json.exists():
        pkg = json.loads(package_json.read_text(encoding="utf-8"))
        electron_version = pkg.get("devDependencies", {}).get("electron")
        if electron_version:
            deps.append(BinaryDep(
                filename=f"electron-v{electron_version}-<platform>-<arch>.zip",
                sha256="(per-platform SHASUMS256.txt — no single hash)",
                release=f"v{electron_version}",
                url=f"https://github.com/electron/electron/releases/download/v{electron_version}/",
                platforms="all (one archive per platform/arch)",
                source_file="desktop/package.json (devDependencies.electron); "
                             "fetched by packaging/ensure-electron.mjs via @electron/get",
                source_line=0,
            ))

    deps.sort(key=lambda d: (d.filename, d.source_file, d.source_line))
    return deps


DOC_TABLE_HEADER = "| Filename | SHA-256 | Platforms | Source | URL |"
DOC_TABLE_RULE = "| --- | --- | --- | --- | --- |"
DOC_GENERATED_LINE = (
    "Generated by `scripts/measure_release_downloads.py deps`; regenerate "
    "after any electron-builder or electron bump and re-pin the manifest."
)


def render_markdown_table(deps: Iterable[BinaryDep]) -> str:
    rows = [DOC_TABLE_HEADER, DOC_TABLE_RULE]
    for d in deps:
        sha_display = d.sha256 if len(d.sha256) != 64 else f"`{d.sha256[:12]}…`"
        url_display = d.url if d.url else "(see source — release/version is dynamic)"
        source_display = f"`{d.source_file}:{d.source_line}`" if d.source_line else f"`{d.source_file}`"
        rows.append(f"| `{d.filename}` | {sha_display} | {d.platforms} | {source_display} | {url_display} |")
    return "\n".join(rows)


def render_deps_report(deps: Iterable[BinaryDep]) -> str:
    return DOC_GENERATED_LINE + "\n\n" + render_markdown_table(deps)


def check_doc_matches(deps: Iterable[BinaryDep], doc_text: str) -> bool:
    """True iff every enumerated filename appears in the doc's §6 table.

    Deliberately a containment check on filenames rather than a byte-exact
    diff of the whole table: the doc is hand-maintained prose around a
    generated table, and the invariant that matters is "nothing the build
    fetches is missing from the doc", not "the markdown renderer never
    changed a column order".
    """
    missing = [d.filename for d in deps if d.filename not in doc_text]
    return not missing


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------

# Exact CI step names that mark a dispatch as an attempted RELEASE build (as
# opposed to a plain push/PR run of the same workflow). Matched by exact name
# per this repo's own convention — see tests/test_ci_network_step_bounds.py.
RELEASE_STEP_NAMES = {
    "Package the NSIS installer and the zip (release build only)",
    "Package the .deb and the AppImage (x64)",
    "Install the desktop shell's dependencies WITH scripts (release build only)",
}

# Job/step conclusions GitHub actually reports. `cancelled` is in this set
# deliberately: GitHub reports a timed-out job as `cancelled`, not `timed_out`,
# so this must be examined, never dropped as "somebody hit stop".
_FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}

_LOG_UNAVAILABLE_STATUSES = {403, 404, 410, 429}

_FETCH_FAILURE_PATTERNS = [
    re.compile(r"Response code [45]\d\d"),
    re.compile(r"\b(ETIMEDOUT|ECONNRESET|ENOTFOUND|EAI_AGAIN)\b"),
    re.compile(r"socket hang up"),
    re.compile(r"RequestError"),
    re.compile(r"got[/\\]dist[/\\]source"),
    re.compile(r"UNABLE_TO_VERIFY_LEAF_SIGNATURE"),
    re.compile(r"CERT_HAS_EXPIRED"),
    re.compile(r"sha512 checksum mismatch", re.IGNORECASE),
    re.compile(r"checksum mismatch", re.IGNORECASE),
    re.compile(r"HTTPError"),
]

_PROGRESS_LINE_RE = re.compile(r"(progress=|downloaded)", re.IGNORECASE)

# Runs on the same tag branch within this window collapse to one incident —
# the two permitted manual re-dispatches of a failed cut must not be counted
# as three separate failures.
INCIDENT_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class FailureEvent:
    run_id: int
    head_branch: str
    created_at: datetime
    job_id: Optional[int]
    step_name: Optional[str]
    category: str  # "fetch_failure" | "other_failure" | "unclassifiable"
    detail: str


@dataclass(frozen=True)
class Measurement:
    fetch_failures: int
    incidents: int
    other_failures: int
    unclassifiable: int
    release_runs: int
    dispatches_examined: int
    window_start: Optional[str]
    window_end: Optional[str]
    events: list[FailureEvent] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"measured: {self.fetch_failures} fetch-failure(s) "
            f"({self.incidents} distinct incident(s)) / "
            f"{self.release_runs} release run(s) with a packaging step / "
            f"{self.dispatches_examined} dispatch(es) examined / "
            f"{self.unclassifiable} unclassifiable / "
            f"window {self.window_start or 'n/a'}..{self.window_end or 'n/a'}"
        )

    def as_dict(self) -> dict:
        return {
            "fetch_failures": self.fetch_failures,
            "incidents": self.incidents,
            "other_failures": self.other_failures,
            "unclassifiable": self.unclassifiable,
            "release_runs": self.release_runs,
            "dispatches_examined": self.dispatches_examined,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "events": [
                {
                    "run_id": e.run_id,
                    "head_branch": e.head_branch,
                    "created_at": e.created_at.isoformat(),
                    "job_id": e.job_id,
                    "step_name": e.step_name,
                    "category": e.category,
                    "detail": e.detail,
                }
                for e in self.events
            ],
        }


def _parse_created_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify_log_text(text: Optional[str], conclusion: str) -> tuple[str, str]:
    """(`category`, `detail`) for one failing release step's log.

    Three-valued: an unreadable/absent log is `unclassifiable`, never a quiet
    pass and never lumped in with a confirmed CDN failure.
    """
    if text is None:
        return "unclassifiable", "log text unavailable"
    if conclusion == "cancelled":
        tail = text.strip().splitlines()[-5:]
        if any(_PROGRESS_LINE_RE.search(line) for line in tail):
            return "fetch_failure", "cancelled mid-download (progress/downloaded line last)"
        return "other_failure", "cancelled with no in-progress download signature"
    for pattern in _FETCH_FAILURE_PATTERNS:
        m = pattern.search(text)
        if m:
            return "fetch_failure", m.group(0)
    return "other_failure", "failed, but no known fetch-failure signature in the log"


def _release_failure_steps(jobs: list[dict]) -> list[tuple[dict, dict]]:
    """[(job, step), ...] for every named release step that did not succeed."""
    out = []
    for job in jobs:
        for step in job.get("steps") or []:
            if step.get("name") in RELEASE_STEP_NAMES:
                conclusion = step.get("conclusion")
                if conclusion in _FAILURE_CONCLUSIONS:
                    out.append((job, step))
    return out


def _is_release_run(jobs: list[dict]) -> bool:
    """True iff a named packaging step actually RAN (denominator test).

    The named step's own record exists in the Jobs API on every dispatch —
    the windows job runs unconditionally and only its two release-gated
    steps carry the `if:`, so an unmet `if:` still leaves a `"skipped"`
    step entry behind. Counting that as a release attempt would inflate the
    denominator with every ordinary dispatch that never set
    `windows_release`/`linux_release` at all. A step only counts as
    evidence of an attempt when it actually executed.
    """
    return any(
        step.get("name") in RELEASE_STEP_NAMES and step.get("conclusion") != "skipped"
        for job in jobs
        for step in (job.get("steps") or [])
    )


def measure_fetch_failures(
    runs: list[dict],
    fetch_jobs: Callable[[int], list[dict]],
    fetch_log: Callable[[int], dict],
) -> Measurement:
    """Pure aside from the two injected callables — offline-testable.

    `runs`: `gh run list --json databaseId,conclusion,createdAt,headBranch,status`
    output. `fetch_jobs(run_id)` -> list of job dicts (GitHub Jobs API shape,
    each with a `steps` list). `fetch_log(job_id)` -> `{"status": int,
    "text": str | None}`.
    """
    events: list[FailureEvent] = []
    release_runs = 0
    created_ats: list[datetime] = []

    for run in runs:
        run_id = run["databaseId"]
        created_at = _parse_created_at(run["createdAt"])
        created_ats.append(created_at)
        head_branch = run.get("headBranch", "")

        jobs = fetch_jobs(run_id)
        if not _is_release_run(jobs):
            continue
        release_runs += 1

        failing_steps = _release_failure_steps(jobs)
        for job, step in failing_steps:
            job_id = job.get("databaseId") or job.get("id")
            log = fetch_log(job_id) if job_id is not None else {"status": None, "text": None}
            status = log.get("status")
            text = log.get("text")
            if status in _LOG_UNAVAILABLE_STATUSES or (status is None and text is None):
                category, detail = "unclassifiable", f"log status {status}"
            else:
                category, detail = classify_log_text(text, step.get("conclusion", ""))
            events.append(FailureEvent(
                run_id=run_id,
                head_branch=head_branch,
                created_at=created_at,
                job_id=job_id,
                step_name=step.get("name"),
                category=category,
                detail=detail,
            ))

    fetch_events = [e for e in events if e.category == "fetch_failure"]
    other = sum(1 for e in events if e.category == "other_failure")
    unclassifiable = sum(1 for e in events if e.category == "unclassifiable")

    incidents = _count_incidents(fetch_events)

    window_start = min(created_ats).isoformat() if created_ats else None
    window_end = max(created_ats).isoformat() if created_ats else None

    return Measurement(
        fetch_failures=len(fetch_events),
        incidents=incidents,
        other_failures=other,
        unclassifiable=unclassifiable,
        release_runs=release_runs,
        dispatches_examined=len(runs),
        window_start=window_start,
        window_end=window_end,
        events=events,
    )


def _count_incidents(fetch_events: list[FailureEvent]) -> int:
    by_branch: dict[str, list[datetime]] = {}
    for e in fetch_events:
        by_branch.setdefault(e.head_branch, []).append(e.created_at)
    incidents = 0
    for branch, times in by_branch.items():
        times.sort()
        last: Optional[datetime] = None
        for t in times:
            if last is None or (t - last) > INCIDENT_WINDOW:
                incidents += 1
            last = t
    return incidents


# --------------------------------------------------------------------------
# gh wiring (not exercised by tests — tests inject fixtures instead)
# --------------------------------------------------------------------------

class GhError(SystemExit):
    """A `gh` call failed. Fails closed — never treated as '0 failures'."""


def _run_gh(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GhError(f"`gh` is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh {' '.join(args)} timed out after {timeout}s "
                       "(no retry — a hung `gh` call is reported, not retried)") from exc


def _gh_json(args: list[str], timeout: int) -> object:
    proc = _run_gh(args, timeout)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed (exit {proc.returncode}): "
                       f"{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def make_real_fetch_jobs(timeout: int) -> Callable[[int], list[dict]]:
    def _fetch(run_id: int) -> list[dict]:
        proc = _run_gh(
            ["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs",
             "--paginate", "--jq", ".jobs[]"],
            timeout,
        )
        if proc.returncode != 0:
            raise GhError(f"gh api .../runs/{run_id}/jobs failed: {proc.stderr.strip()}")
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return _fetch


def make_real_fetch_log(timeout: int) -> Callable[[int], dict]:
    def _fetch(job_id: int) -> dict:
        proc = _run_gh(
            ["api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs",
             "--allow-escape-sequences"],
            timeout,
        )
        if proc.returncode == 0:
            return {"status": 200, "text": proc.stdout}
        stderr = proc.stderr
        for code in sorted(_LOG_UNAVAILABLE_STATUSES):
            if str(code) in stderr:
                return {"status": code, "text": None}
        raise GhError(f"gh api .../jobs/{job_id}/logs failed unexpectedly: {stderr.strip()}")
    return _fetch


def run_real_measurement(since: Optional[str], limit: int, timeout: int) -> Measurement:
    runs = _gh_json(
        ["run", "list", "--workflow", "ci.yml", "--event", "workflow_dispatch",
         "--json", "databaseId,conclusion,createdAt,headBranch,status",
         "--limit", str(limit)],
        timeout,
    )
    if not isinstance(runs, list):
        raise GhError(f"gh run list returned unexpected shape: {runs!r}")
    if since:
        since_dt = _parse_created_at(since if "T" in since else f"{since}T00:00:00+00:00")
        runs = [r for r in runs if _parse_created_at(r["createdAt"]) >= since_dt]
    return measure_fetch_failures(
        runs,
        fetch_jobs=make_real_fetch_jobs(timeout),
        fetch_log=make_real_fetch_log(timeout),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_deps(args: argparse.Namespace) -> int:
    desktop_dir = Path(args.desktop_dir).resolve()
    deps = enumerate_binary_deps(desktop_dir)

    if args.check_doc:
        doc_path = Path(args.doc)
        if not doc_path.exists():
            print(f"FAIL: {doc_path} does not exist", file=sys.stderr)
            return 1
        doc_text = doc_path.read_text(encoding="utf-8")
        if check_doc_matches(deps, doc_text):
            print(f"OK: all {len(deps)} enumerated binaries appear in {doc_path}")
            return 0
        missing = [d.filename for d in deps if d.filename not in doc_text]
        print(f"FAIL: {len(missing)} enumerated binary(ies) missing from {doc_path}:",
              file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([d.as_dict() for d in deps], indent=2))
    else:
        print(render_deps_report(deps))
    return 0


def _cmd_failures(args: argparse.Namespace) -> int:
    try:
        measurement = run_real_measurement(args.since, args.limit, args.gh_timeout)
    except GhError as exc:
        print(f"FAILED CLOSED — could not measure: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(measurement.as_dict(), indent=2))
    else:
        print(measurement.summary_line())
        for e in measurement.events:
            print(f"  run={e.run_id} branch={e.head_branch} step={e.step_name!r} "
                  f"-> {e.category} ({e.detail})")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(measurement.as_dict(), indent=2) + "\n", encoding="utf-8")

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="measure_release_downloads.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_deps = sub.add_parser("deps", help="enumerate binaries fetched at build time")
    p_deps.add_argument("--desktop-dir", dest="desktop_dir", default="desktop")
    p_deps.add_argument("--json", action="store_true")
    p_deps.add_argument("--check-doc", action="store_true",
                         help="exit 1 if docs/DISTRIBUTION.md has drifted")
    p_deps.add_argument("--doc", default="docs/DISTRIBUTION.md")
    p_deps.set_defaults(func=_cmd_deps)

    p_fail = sub.add_parser("failures", help="measure fetch failures via gh (read-only)")
    p_fail.add_argument("--since", default=None,
                         help="ISO date; only dispatches on/after this are counted")
    p_fail.add_argument("--limit", type=int, default=100)
    p_fail.add_argument("--gh-timeout", type=int, default=60,
                         help="seconds before a single gh call is reported as failed "
                              "(no retry — see the module docstring)")
    p_fail.add_argument("--json", action="store_true")
    p_fail.add_argument("--json-out", default=None)
    p_fail.set_defaults(func=_cmd_failures)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except MissingBuildDependency as exc:
        print(f"FAILED CLOSED — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
