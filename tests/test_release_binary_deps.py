"""The release build's unpinned, uncached third-party downloads (#v0.2.3 504).

The v0.2.3 Windows release build (run 34855505802) died on a 504 from
electron-builder's binary CDN mid-build, after both nsis archives had already
downloaded, with every code gate green -- the release simply was not
produced. Nothing in the repo enumerated what the build fetches, mitigated a
CDN outage, or recorded how often that outage has actually happened.

Four acceptance criteria, tested here:

  AC1 -- the binaries the release build fetches are enumerated FROM THE BUILD
         ITSELF (never assumed) and recorded where a release engineer sees it.
  AC2 -- the mitigation (an `actions/cache@v4` step in both the windows and
         linux jobs) is wired, and its reason and residual exposure -- a cold
         cache still hits the CDN -- are written down, not implied away.
  AC3 -- how often this has actually failed is measured across real release
         runs, and the number is reported either way.
  AC4 -- no retry loop is added around the download; a genuine outage must
         stay visible, not get hidden behind a slow, silent spin.

`scripts/measure_release_downloads.py` is loaded by file path (like
`tests/test_check_release_manifest.py` loads `check_release_manifest.py`):
it lives in `scripts/`, not on the normal import path, and is never
`require()`d or executed as node -- it only ever reads toolset files as text.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent, text=True,
    ).strip()
)
SCRIPT = REPO_ROOT / "scripts" / "measure_release_downloads.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DIST_DOC = REPO_ROOT / "docs" / "DISTRIBUTION.md"
WINDOWS_DOC = REPO_ROOT / "docs" / "WINDOWS.md"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "release_downloads"
REAL_DESKTOP_DIR = REPO_ROOT / "desktop"


def _load_module():
    spec = importlib.util.spec_from_file_location("_nh_measure_release_downloads", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Registered in sys.modules before exec: the module uses `@dataclass`,
    # whose machinery looks itself up via `sys.modules[cls.__module__]` --
    # without this it finds nothing and raises deep inside dataclasses.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


def _load_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _job(workflow, name):
    jobs = workflow["jobs"]
    assert name in jobs, f"the {name} job was not found in the workflow"
    return jobs[name]


def _step(job, name):
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in job")


def _step_index(job, name):
    for i, step in enumerate(job["steps"]):
        if step.get("name") == name:
            return i
    raise AssertionError(f"no step named {name!r} in job")


def _fixture_runs():
    import json
    return json.loads((FIXTURES / "runs.json").read_text(encoding="utf-8"))


def _fixture_fetch_jobs(run_id):
    import json
    return json.loads((FIXTURES / "jobs" / f"{run_id}.json").read_text(encoding="utf-8"))


def _fixture_fetch_log(job_id):
    path = FIXTURES / "logs" / f"{job_id}.txt"
    if path.exists():
        return {"status": 200, "text": path.read_text(encoding="utf-8")}
    return {"status": 404, "text": None}


# ---------------------------------------------------------------------------
# AC1 -- enumerated from the build, recorded where a release engineer sees it
# ---------------------------------------------------------------------------

def test_enumeration_reads_filenames_and_checksums_from_the_toolset_sources():
    deps = m.enumerate_binary_deps(FIXTURES / "desktop_tree")
    by_filename = {d.filename: d for d in deps}

    assert "nsis-3.0.4.1.7z" in by_filename
    nsis = by_filename["nsis-3.0.4.1.7z"]
    assert nsis.sha256 == "d7f021beaf8ae82c721dbc8407b5b9da04da931aec2d9202aff031356852e573"
    assert nsis.source_file.endswith("toolsets/windows.js")
    assert nsis.source_line > 0

    assert "nsis-bundle-3.12.tar.gz" in by_filename
    bundle = by_filename["nsis-bundle-3.12.tar.gz"]
    assert bundle.sha256 == "7db0f756b1cebe7ae7c6a630f7516dc5e59e4d073f9054ff56d5f05426f2cdb3"

    assert "7zip-win.7z" in by_filename
    assert "fpm-1.9.3-2.3.1-linux-x86_64.7z" in by_filename


def test_enumeration_refuses_an_absent_app_builder_lib(tmp_path):
    empty_desktop = tmp_path / "desktop"
    empty_desktop.mkdir()
    with pytest.raises(SystemExit) as exc:
        m.enumerate_binary_deps(empty_desktop)
    assert isinstance(exc.value, m.MissingBuildDependency)
    assert "npm ci" in str(exc.value)


def test_enumeration_of_the_real_tree_includes_the_labels_from_the_v023_failure():
    toolsets_dir = REAL_DESKTOP_DIR / "node_modules" / "app-builder-lib" / "out" / "toolsets"
    if not toolsets_dir.is_dir():
        pytest.skip("desktop/node_modules/app-builder-lib is not installed here; "
                    "run `npm ci` in desktop/ to exercise this against the real build")
    deps = m.enumerate_binary_deps(REAL_DESKTOP_DIR)
    filenames = {d.filename for d in deps}
    assert "nsis-3.0.4.1.7z" in filenames
    assert "nsis-resources-3.4.1.7z" in filenames
    assert any(f.startswith("electron-v43.4.1-") for f in filenames)


def test_the_documented_table_matches_the_enumerator():
    assert DIST_DOC.exists()
    doc_text = DIST_DOC.read_text(encoding="utf-8")
    assert m.DOC_TABLE_HEADER in doc_text, "no generated-table header found in docs/DISTRIBUTION.md §6"
    assert re.search(r"^## 6\.", doc_text, re.MULTILINE), "§6 heading missing"

    toolsets_dir = REAL_DESKTOP_DIR / "node_modules" / "app-builder-lib" / "out" / "toolsets"
    if not toolsets_dir.is_dir():
        pytest.skip("desktop/node_modules/app-builder-lib is not installed here; "
                    "run `npm ci` in desktop/ to check the table against the real build")
    deps = m.enumerate_binary_deps(REAL_DESKTOP_DIR)
    assert m.check_doc_matches(deps, doc_text), (
        "docs/DISTRIBUTION.md §6 is missing one or more binaries the real "
        "build fetches -- regenerate with "
        "`uv run python scripts/measure_release_downloads.py deps`"
    )


def test_the_record_lives_in_the_release_doc():
    doc_text = DIST_DOC.read_text(encoding="utf-8")
    heading_match = re.search(r"^## 6\.\s*(.+)$", doc_text, re.MULTILINE)
    assert heading_match, "no ## 6. heading in docs/DISTRIBUTION.md"
    assert "download" in heading_match.group(1).lower() or "third-party" in heading_match.group(1).lower()
    assert "scripts/measure_release_downloads.py" in doc_text


# ---------------------------------------------------------------------------
# AC2 -- mitigation wired, reason and residual exposure named honestly
# ---------------------------------------------------------------------------

def test_both_release_jobs_cache_the_electron_builder_binaries():
    workflow = _load_workflow()
    for job_name in ("windows", "linux"):
        job = _job(workflow, job_name)
        cache_step = _step(job, "Cache electron-builder's downloaded binaries")
        assert cache_step.get("uses", "").startswith("actions/cache@v4")

        path = cache_step["with"]["path"]
        assert "electron-builder" in path
        assert "electron" in path

        key = cache_step["with"]["key"]
        assert "hashFiles('desktop/package-lock.json')" in key

        restore_keys = cache_step["with"]["restore-keys"]
        assert "eb-binaries-${{ runner.os }}-" in restore_keys

        cache_idx = _step_index(job, "Cache electron-builder's downloaded binaries")
        install_idx = _step_index(job, "Install the desktop shell's dependencies")
        assert cache_idx < install_idx, (
            f"{job_name}: the cache step must run before "
            "'Install the desktop shell's dependencies'"
        )


def test_the_windows_cache_is_warmed_on_main_or_the_doc_says_it_is_cold():
    workflow = _load_workflow()
    job = _job(workflow, "windows")
    warm_step = None
    for step in job["steps"]:
        if step.get("name", "").startswith("Warm the electron-builder binary cache"):
            warm_step = step
            break

    if warm_step is not None:
        cond = warm_step.get("if", "")
        assert "github.event_name == 'push'" in cond
        assert "cache-hit != 'true'" in cond
        assert "dist:win" in warm_step.get("run", "")
        return

    doc_text = DIST_DOC.read_text(encoding="utf-8")
    assert "a first cut is always cold" in doc_text, (
        "no warm step was found, and docs/DISTRIBUTION.md §6 does not say the "
        "explicit fallback sentence -- silence here is the failure mode this "
        "test exists to catch"
    )


def test_the_warm_step_is_bounded():
    workflow = _load_workflow()
    job = _job(workflow, "windows")
    warm_step = None
    for step in job["steps"]:
        if step.get("name", "").startswith("Warm the electron-builder binary cache"):
            warm_step = step
            break
    if warm_step is None:
        pytest.skip("no warm step present -- the doc fallback was taken instead")
    assert "timeout-minutes" in warm_step
    assert warm_step["timeout-minutes"] > 0


def test_doc_names_the_chosen_mitigation_and_its_reason():
    doc_text = DIST_DOC.read_text(encoding="utf-8")
    section = doc_text[doc_text.index("## 6."):]
    assert re.search(r"\bcach(e|ing)\b", section, re.IGNORECASE)
    assert "actions/cache" in section
    # The reason: one incident measured, not "just in case".
    assert re.search(r"incident", section, re.IGNORECASE)
    # An escalation condition -- when would we switch to vendoring instead.
    assert re.search(r"escalat", section, re.IGNORECASE)


def test_doc_names_the_cold_cache_residual_exposure():
    doc_text = DIST_DOC.read_text(encoding="utf-8")
    section = doc_text[doc_text.index("## 6."):]
    heading_match = re.search(r"^#{2,4}\s*Residual exposure", section, re.MULTILINE | re.IGNORECASE)
    assert heading_match, "no 'Residual exposure' heading found in §6"
    body = section[heading_match.end():]
    next_heading = re.search(r"^#{2,4}\s", body, re.MULTILINE)
    body = body[:next_heading.start()] if next_heading else body

    assert "cold cache" in body.lower()
    assert re.search(r"lockfile bump", body, re.IGNORECASE)
    assert re.search(r"evict", body, re.IGNORECASE)
    assert re.search(r"(fork|new branch|branch with no)", body, re.IGNORECASE)
    assert ("does not remove" in body.lower()) or ("does not close" in body.lower())


def test_the_cache_step_comment_points_at_the_doc():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    for anchor in (
        "Cache electron-builder's downloaded binaries",
    ):
        idx = text.index(anchor)
        preceding = text[max(0, idx - 1600):idx]
        assert "docs/DISTRIBUTION.md" in preceding, (
            f"no comment naming docs/DISTRIBUTION.md immediately before {anchor!r}"
        )
    # There are two such steps (windows, linux); check both occurrences.
    first_idx = text.index("Cache electron-builder's downloaded binaries")
    second_idx = text.index("Cache electron-builder's downloaded binaries", first_idx + 1)
    preceding_second = text[max(0, second_idx - 1600):second_idx]
    assert "docs/DISTRIBUTION.md" in preceding_second


def test_windows_doc_cross_references_distribution_section_6():
    doc_text = WINDOWS_DOC.read_text(encoding="utf-8")
    assert "DISTRIBUTION.md" in doc_text
    assert re.search(r"§\s*6|section 6", doc_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# AC3 -- measured across previous release runs, reported either way
# ---------------------------------------------------------------------------

def test_counts_a_fetch_failure_on_a_known_label():
    text = (FIXTURES / "logs" / "20001.txt").read_text(encoding="utf-8")
    category, detail = m.classify_log_text(text, "failure")
    assert category == "fetch_failure"
    assert "504" in detail


def test_does_not_count_an_unrelated_failure_as_a_fetch_failure():
    text = (FIXTURES / "logs" / "20004.txt").read_text(encoding="utf-8")
    category, _detail = m.classify_log_text(text, "failure")
    assert category == "other_failure"


@pytest.mark.parametrize("code", [403, 404, 429, 502, 504])
def test_classify_status_buckets(code):
    text = f"⨯ Response code {code} (some gateway text)  url=https://example/whatever"
    category, detail = m.classify_log_text(text, "failure")
    assert category == "fetch_failure"
    assert str(code) in detail


@pytest.mark.parametrize("marker", ["ETIMEDOUT", "ECONNRESET", "EAI_AGAIN", "socket hang up"])
def test_classify_transport_errors(marker):
    text = f"some preamble\nError: connect {marker} somewhere\nmore text"
    category, _detail = m.classify_log_text(text, "failure")
    assert category == "fetch_failure"


def test_classify_checksum_mismatch_is_a_fetch_failure_not_a_build_failure():
    text = "downloading nsis-3.0.4.1.7z\nsha512 checksum mismatch for nsis-3.0.4.1.7z"
    category, _detail = m.classify_log_text(text, "failure")
    assert category == "fetch_failure"


def test_a_truncated_download_in_a_cancelled_run_counts():
    text = (FIXTURES / "logs" / "20003.txt").read_text(encoding="utf-8")
    category, detail = m.classify_log_text(text, "cancelled")
    assert category == "fetch_failure"
    assert "progress" in detail or "download" in detail

    text_no_download = "starting up\nchecking out repo\ncancelled by user"
    category2, _detail2 = m.classify_log_text(text_no_download, "cancelled")
    assert category2 == "other_failure"


def test_runs_without_a_packaging_step_are_excluded_from_the_denominator():
    runs = _fixture_runs()
    non_release_run = next(r for r in runs if r["databaseId"] == 1006)
    jobs = _fixture_fetch_jobs(non_release_run["databaseId"])
    assert m._is_release_run(jobs) is False, (
        "a dispatch whose named steps are all 'skipped' must not count as a "
        "release attempt -- GitHub still records the step when its `if:` is "
        "unmet, and counting it would inflate the denominator"
    )


def test_an_expired_log_is_unclassifiable_not_a_pass():
    runs = _fixture_runs()
    measurement = m.measure_fetch_failures(runs, _fixture_fetch_jobs, _fixture_fetch_log)
    unclassifiable_events = [e for e in measurement.events if e.category == "unclassifiable"]
    assert len(unclassifiable_events) == 1
    assert unclassifiable_events[0].run_id == 1007
    # An unclassifiable event must never be silently counted as a fetch failure.
    assert unclassifiable_events[0] not in [e for e in measurement.events if e.category == "fetch_failure"]


def test_an_in_progress_run_is_unclassifiable():
    category, detail = m.classify_log_text(None, "failure")
    assert category == "unclassifiable"
    assert detail


def test_retries_of_one_cut_collapse_to_one_incident():
    runs = _fixture_runs()
    measurement = m.measure_fetch_failures(runs, _fixture_fetch_jobs, _fixture_fetch_log)
    # Fixture v0.2.3 branch carries three fetch-failure events within an hour.
    v023_events = [e for e in measurement.events
                   if e.head_branch == "v0.2.3" and e.category == "fetch_failure"]
    assert len(v023_events) == 3
    assert measurement.incidents == 1


def test_the_report_prints_the_number_even_when_zero():
    runs = [{
        "databaseId": 9001, "conclusion": "success",
        "createdAt": "2026-01-01T00:00:00Z", "headBranch": "v9.9.9", "status": "completed",
    }]

    def fetch_jobs(_run_id):
        return [{
            "databaseId": 30001, "name": "windows", "conclusion": "success",
            "steps": [{
                "name": "Package the NSIS installer and the zip (release build only)",
                "conclusion": "success",
            }],
        }]

    def fetch_log(_job_id):
        return {"status": 200, "text": "never called"}

    measurement = m.measure_fetch_failures(runs, fetch_jobs, fetch_log)
    assert measurement.fetch_failures == 0
    line = measurement.summary_line()
    assert "0 fetch-failure" in line


def test_gh_error_fails_closed():
    def fetch_jobs(_run_id):
        raise m.GhError("gh api call failed (simulated)")

    with pytest.raises(SystemExit):
        m.measure_fetch_failures(_fixture_runs(), fetch_jobs, _fixture_fetch_log)


def test_doc_records_the_measured_number_with_its_window():
    doc_text = DIST_DOC.read_text(encoding="utf-8")
    section = doc_text[doc_text.index("## 6."):]
    assert re.search(r"\bmeasured\b", section, re.IGNORECASE)
    # A count and a denominator ("N fetch-failure(s) ... M release run(s)").
    assert re.search(r"\d+\s+fetch-failure", section)
    assert re.search(r"\d+\s+release run", section)
    # A date range, not a vague "recently" or a placeholder.
    assert re.search(r"20\d\d-\d\d-\d\d", section)
    lowered = section.lower()
    assert "rare" not in lowered
    assert "tbd" not in lowered
    assert "todo" not in lowered
    assert "placeholder" not in lowered


# ---------------------------------------------------------------------------
# AC4 -- no retry loop added around the download
# ---------------------------------------------------------------------------

FORBIDDEN_RETRY_MARKERS = ("while ", "until ", "n=$((", "for i in", "Start-Sleep", "retry", "-Retry")


def test_no_retry_loop_in_the_packaging_or_install_steps():
    workflow = _load_workflow()
    windows = _job(workflow, "windows")
    linux = _job(workflow, "linux")

    steps_to_check = [
        _step(windows, "Package the NSIS installer and the zip (release build only)"),
        _step(windows, "Install the desktop shell's dependencies"),
        _step(linux, "Package the .deb and the AppImage (x64)"),
        _step(linux, "Install the desktop shell's dependencies"),
    ]
    for step in steps_to_check:
        body = step.get("run", "")
        for marker in FORBIDDEN_RETRY_MARKERS:
            assert marker not in body, (
                f"forbidden retry marker {marker!r} found in step "
                f"{step.get('name')!r} -- a retry loop around the download "
                "hides a genuine outage instead of surviving it"
            )


def test_the_mitigation_is_a_cache_action_not_a_shell_loop():
    workflow = _load_workflow()
    for job_name in ("windows", "linux"):
        job = _job(workflow, job_name)
        cache_step = _step(job, "Cache electron-builder's downloaded binaries")
        assert "uses" in cache_step
        assert "run" not in cache_step


def test_the_two_existing_bounded_retries_are_untouched():
    workflow = _load_workflow()
    linux = _job(workflow, "linux")
    uv_step = _step(linux, "Install dependencies")
    assert "uv sync --frozen" in uv_step["run"]
    assert "n=$((" in uv_step["run"] or "n -ge 2" in uv_step["run"]

    apt_step = None
    for step in linux["steps"]:
        if "apt" in step.get("name", "").lower() or "Xvfb" in step.get("name", ""):
            apt_step = step
            break
    assert apt_step is not None, "the apt lists + Xvfb step must still exist"


def test_the_doc_forbids_the_retry_loop_explicitly():
    doc_text = DIST_DOC.read_text(encoding="utf-8")
    section = doc_text[doc_text.index("## 6."):]
    assert re.search(r"retry", section, re.IGNORECASE)
    assert re.search(r"not\s+(a\s+)?retry|no\s+retry", section, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Cross-cutting: both jobs pin the electron/electron-builder cache dirs
# ---------------------------------------------------------------------------

def test_both_release_jobs_pin_electron_cache_env_vars():
    workflow = _load_workflow()
    for job_name in ("windows", "linux"):
        job = _job(workflow, job_name)
        env = job.get("env", {})
        assert "ELECTRON_CACHE" in env
        assert "ELECTRON_BUILDER_CACHE" in env


def test_the_pinned_cache_dirs_are_not_a_bare_tilde():
    """electron-builder resolves ELECTRON_BUILDER_CACHE/ELECTRON_CACHE with
    Node's path.resolve(), which does NOT expand a leading '~' -- it becomes
    a literal '~' directory under the step's cwd. A value like
    '~\\AppData\\Local\\electron-builder\\Cache' therefore never matches
    where actions/cache restores/saves ('~' expanded to the runner's home),
    so the cache can never hit. The env value must be an absolute path with
    no leading tilde (e.g. built from the `runner.temp`/`runner.workspace`
    contexts, which are already absolute).
    """
    workflow = _load_workflow()
    for job_name in ("windows", "linux"):
        job = _job(workflow, job_name)
        env = job["env"]
        for name in ("ELECTRON_BUILDER_CACHE", "ELECTRON_CACHE"):
            value = env[name]
            assert not value.startswith("~"), (
                f"{job_name}.env.{name} = {value!r} starts with a literal "
                "'~', which Node's path.resolve() does not expand -- this "
                "is the exact bug that makes the cache never hit"
            )


def test_the_cache_path_matches_the_pinned_env_vars_exactly():
    """The cache step's `path:` entries must be the SAME strings as the
    pinned ELECTRON_BUILDER_CACHE/ELECTRON_CACHE env vars, so the directory
    actions/cache restores into is provably the directory electron-builder
    reads from -- not merely two paths that happen to resolve the same way
    under two different expansion rules.
    """
    workflow = _load_workflow()
    for job_name in ("windows", "linux"):
        job = _job(workflow, job_name)
        env = job["env"]
        cache_step = _step(job, "Cache electron-builder's downloaded binaries")
        path_lines = [
            line for line in cache_step["with"]["path"].splitlines() if line.strip()
        ]
        assert env["ELECTRON_BUILDER_CACHE"] in path_lines
        assert env["ELECTRON_CACHE"] in path_lines
