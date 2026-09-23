"""Tests for scripts/check_release_feeds.py — the gate this ticket adds.

Nothing before this checked that a release's update feed(s) actually name
assets the release publishes. Two shapes of that defect have already
shipped, both found only by hand, after the fact:

  * v0.2.2 and v0.2.3's `latest-mac.yml` name a second file
    (`no_human-<version>-arm64.dmg`, electron-builder's own dmg target,
    signed but never notarized/stapled) that the release never uploads —
    only the `packaging/make-dmg.sh` output ships.
  * v0.1.7 and v0.2.0 shipped Windows/Linux assets with no
    `latest.yml`/`latest-linux.yml` at all.

The fixtures under testdata/release_feeds/ are REAL captures (see that
directory's README.md for the exact `gh` commands and capture date), not
synthetic data — the tests below that use them are what proves the gate
against a real published release's asset list, not only a local dist
directory, and demonstrate both mismatch shapes actually failing.

Per this repo's convention (see tests/test_verify_artefact.py's module
docstring), tests assert on OBSERVED subprocess/function behaviour, never
on the gate script's own source text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.testing.pytest_isolated_home import REAL_HOME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_release_feeds as gate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "testdata" / "release_feeds"
SCRIPT = ROOT / "scripts" / "check_release_feeds.py"
PUBLISH_SH = ROOT / "packaging" / "publish-release.sh"


def _load_asset_names(fixture: str) -> set[str]:
    data = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    return gate.uploaded_asset_names(data["assets"])


def _feed(fixture: str) -> str:
    return (FIXTURES / fixture).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# AC1/AC2 core semantics, against real captured release data.
# --------------------------------------------------------------------------- #

def test_v022_feed_names_a_dmg_the_release_never_published():
    asset_names = _load_asset_names("v0.2.2.assets.json")
    feeds = {"latest-mac.yml": _feed("v0.2.2.latest-mac.yml")}

    problems = gate.check(asset_names, feeds)

    assert len(problems) == 1, problems
    assert "no_human-0.2.2-arm64.dmg" in problems[0]
    assert "latest-mac.yml" in problems[0]
    # The real asset list has the notarized make-dmg.sh output instead — a
    # different filename — which is exactly why this is a live mismatch.
    assert "no_human-0.2.2-arm64.dmg" not in asset_names
    assert "no_human-0.2.2.dmg" in asset_names


def test_good_feed_with_the_phantom_row_removed_passes():
    asset_names = _load_asset_names("good.assets.json")
    feeds = {"latest-mac.yml": _feed("good.latest-mac.yml")}

    assert gate.check(asset_names, feeds) == []


def test_v020_shipped_windows_assets_with_no_latest_yml():
    asset_names = _load_asset_names("v0.2.0.assets.json")
    assert "latest.yml" not in asset_names
    assert any(n.endswith(".exe") for n in asset_names)

    problems = gate.check(asset_names, feeds={})

    assert any("latest.yml" in p and "Windows" in p for p in problems), problems


def test_v017_shipped_linux_assets_with_no_latest_linux_yml():
    asset_names = _load_asset_names("v0.1.7.assets.json")
    assert "latest-linux.yml" not in asset_names
    assert any(n.endswith(".AppImage") for n in asset_names)
    # v0.1.7 DOES carry latest.yml (unlike v0.2.0) — only the Linux feed is
    # missing here, so the Windows problem must NOT fire for this release.
    assert "latest.yml" in asset_names

    problems = gate.check(asset_names, feeds={})

    assert any("latest-linux.yml" in p and "Linux" in p for p in problems), problems
    assert not any("latest.yml" in p and "Windows" in p for p in problems), problems


def test_missing_feed_check_does_not_require_feed_content():
    """A feed known only by NAME (asset list) still trips the missing-feed
    check even though no --assets/--tag caller downloaded its content — this
    is what makes v0.1.7/v0.2.0 (no feed to download at all) detectable."""
    asset_names = {"no_human-1.0.0-UNSIGNED.exe", "latest.yml"}
    assert gate.check(asset_names, feeds={}) == []


def test_url_resolution_problem_only_fires_for_feeds_with_content_supplied():
    """Conversely: a feed that DOES exist as an asset, but whose content the
    caller never supplied, cannot be url-checked — only missing-feed and
    content-based checks are independent axes."""
    asset_names = {"no_human-1.0.0-UNSIGNED.exe", "latest.yml"}
    # latest.yml's own content is never handed to check(); nothing should
    # complain about its (unknown) internal urls.
    problems = gate.check(asset_names, feeds={})
    assert not any("latest.yml names" in p for p in problems)


# --------------------------------------------------------------------------- #
# feed_referenced_names — url/path parsing, percent-decoding, malformed feeds.
# --------------------------------------------------------------------------- #

def test_feed_referenced_names_reads_path_and_files_urls():
    names = gate.feed_referenced_names(_feed("v0.2.2.latest-mac.yml"))
    assert names == {"no_human-0.2.2-arm64-mac.zip", "no_human-0.2.2-arm64.dmg"}


def test_feed_referenced_names_url_decodes_percent_encoding():
    text = (
        "version: 1.0.0\n"
        "path: no%20human-1.0.0.exe\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no%20human-1.0.0.exe\n"
        "    sha512: x\n"
        "    size: 1\n"
    )
    assert gate.feed_referenced_names(text) == {"no human-1.0.0.exe"}


def test_feed_referenced_names_rejects_a_feed_naming_nothing():
    with pytest.raises(ValueError):
        gate.feed_referenced_names("version: 1.0.0\n")


def test_feed_referenced_names_rejects_a_non_mapping_feed():
    with pytest.raises(ValueError):
        gate.feed_referenced_names("- just\n- a\n- list\n")


def test_check_reports_an_unparseable_feed_rather_than_crashing():
    asset_names = {"latest.yml", "no_human-1.0.0-UNSIGNED.exe"}
    problems = gate.check(asset_names, feeds={"latest.yml": "version: 1.0.0\n"})
    assert any("latest.yml could not be checked" in p for p in problems)


# --------------------------------------------------------------------------- #
# uploaded_asset_names / uploading_asset_problems — the "state" field.
# --------------------------------------------------------------------------- #

def test_an_asset_still_uploading_is_treated_as_absent():
    assets = [
        {"name": "latest.yml", "state": "uploaded"},
        {"name": "no_human-1.0.0-UNSIGNED.exe", "state": "starter"},
    ]
    assert gate.uploaded_asset_names(assets) == {"latest.yml"}
    problems = gate.uploading_asset_problems(assets)
    assert len(problems) == 1
    assert "no_human-1.0.0-UNSIGNED.exe" in problems[0]
    assert "starter" in problems[0]


def test_an_asset_with_no_state_field_counts_as_uploaded():
    # gh's JSON always includes `state`, but the parser must not crash if a
    # caller hands it a hand-built dict without one (e.g. --assets is local
    # files, which have no "state" concept at all).
    assert gate.uploaded_asset_names([{"name": "x"}]) == {"x"}
    assert gate.uploading_asset_problems([{"name": "x"}]) == []


# --------------------------------------------------------------------------- #
# check_local / --assets preflight mode (no network).
# --------------------------------------------------------------------------- #

def test_check_local_passes_for_an_internally_consistent_file_list(tmp_path):
    zip_path = tmp_path / "no_human-1.0.0-arm64-mac.zip"
    zip_path.write_bytes(b"zip")
    dmg_path = tmp_path / "no_human-1.0.0.dmg"
    dmg_path.write_bytes(b"dmg")
    feed_path = tmp_path / "latest-mac.yml"
    feed_path.write_text(
        "version: 1.0.0\n"
        "path: no_human-1.0.0-arm64-mac.zip\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no_human-1.0.0-arm64-mac.zip\n"
        "    sha512: x\n"
        "    size: 3\n",
        encoding="utf-8",
    )
    problems = gate.check_local([str(zip_path), str(dmg_path), str(feed_path)])
    assert problems == []


def test_check_local_catches_the_v022_shape(tmp_path):
    zip_path = tmp_path / "no_human-1.0.0-arm64-mac.zip"
    zip_path.write_bytes(b"zip")
    dmg_path = tmp_path / "no_human-1.0.0.dmg"  # the make-dmg.sh output
    dmg_path.write_bytes(b"dmg")
    feed_path = tmp_path / "latest-mac.yml"
    # electron-builder's own (never-shipped) dmg name in the files: list.
    feed_path.write_text(
        "version: 1.0.0\n"
        "path: no_human-1.0.0-arm64-mac.zip\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no_human-1.0.0-arm64-mac.zip\n"
        "    sha512: x\n"
        "    size: 3\n"
        "  - url: no_human-1.0.0-arm64.dmg\n"
        "    sha512: y\n"
        "    size: 4\n",
        encoding="utf-8",
    )
    problems = gate.check_local([str(zip_path), str(dmg_path), str(feed_path)])
    assert len(problems) == 1
    assert "no_human-1.0.0-arm64.dmg" in problems[0]


# --------------------------------------------------------------------------- #
# CLI (main) — exit codes, --json, error paths. Invoked as a real subprocess
# so a fake `gh` on PATH is exercised exactly as it would be from the shell.
# --------------------------------------------------------------------------- #

def _fakebin(tmp_path: Path, script_body: str) -> str:
    """A PATH entry containing only a fake `gh`, prepended to the real PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(script_body, encoding="utf-8")
    gh.chmod(0o755)
    return f"{bindir}:{os.environ.get('PATH', '')}"


def _run_cli(args: list[str], path: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


def _self_consistent_mac_fileset(tmp_path: Path) -> list[str]:
    zip_path = tmp_path / "no_human-1.0.0-arm64-mac.zip"
    zip_path.write_bytes(b"zip")
    feed_path = tmp_path / "latest-mac.yml"
    feed_path.write_text(
        "version: 1.0.0\n"
        "path: no_human-1.0.0-arm64-mac.zip\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no_human-1.0.0-arm64-mac.zip\n"
        "    sha512: x\n"
        "    size: 3\n",
        encoding="utf-8",
    )
    return [str(zip_path), str(feed_path)]


def test_cli_local_ok_exits_0(tmp_path):
    files = _self_consistent_mac_fileset(tmp_path)
    proc = _run_cli(["--assets", *files])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_cli_local_problem_exits_1_and_lists_it(tmp_path):
    exe = tmp_path / "no_human-1.0.0-UNSIGNED.exe"
    exe.write_bytes(b"exe")
    proc = _run_cli(["--assets", str(exe)])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "latest.yml" in proc.stdout


def test_cli_local_json_output(tmp_path):
    files = _self_consistent_mac_fileset(tmp_path)
    proc = _run_cli(["--assets", *files, "--json"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["problems"] == []


def test_cli_requires_tag_or_assets(tmp_path):
    proc = _run_cli([])
    assert proc.returncode != 0
    assert proc.returncode != 0 and "required" in (proc.stderr or "").lower()


def test_cli_gh_unavailable_exits_2_not_0(tmp_path):
    # An empty fakebin dir with nothing else on PATH: `gh` cannot be found.
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    proc = _run_cli(["--tag", "v9.9.9"], path=str(empty_bin))
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_cli_release_not_found_exits_1_not_2(tmp_path):
    path = _fakebin(tmp_path, (
        "#!/bin/sh\n"
        "echo 'release not found' >&2\n"
        "exit 1\n"
    ))
    proc = _run_cli(["--tag", "v9.9.9"], path=path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "no release" in proc.stderr.lower() or "no release" in proc.stdout.lower()


def test_cli_draft_release_is_a_problem_not_a_crash(tmp_path):
    payload = json.dumps({
        "tagName": "v9.9.9", "isDraft": True, "isPrerelease": False, "assets": [],
    })
    path = _fakebin(tmp_path, (
        "#!/bin/sh\n"
        f"echo '{payload}'\n"
        "exit 0\n"
    ))
    proc = _run_cli(["--tag", "v9.9.9"], path=path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "draft" in proc.stdout.lower()


def test_check_live_downloads_only_the_expected_present_feeds(tmp_path, monkeypatch):
    """A fake `gh` that serves the real v0.2.2 fixture data end-to-end through
    check_live(), proving the live path (not just the pure `check()` core)
    reproduces the same single problem."""
    assets_payload = (FIXTURES / "v0.2.2.assets.json").read_text(encoding="utf-8")
    mac_feed_path = tmp_path / "mac_feed_source.yml"
    mac_feed_path.write_text(_feed("v0.2.2.latest-mac.yml"), encoding="utf-8")
    # v0.2.2 also ships Windows and Linux assets (it is a real full release),
    # so check_live will expect latest.yml/latest-linux.yml too. Their
    # content is irrelevant to THIS test's assertion (the mac problem), so
    # these are self-consistent stand-ins naming real assets from the
    # fixture, just to keep the Windows/Linux axis clean of its own noise.
    win_feed_path = tmp_path / "win_feed_source.yml"
    win_feed_path.write_text(
        "version: 0.2.2\n"
        "path: no_human-0.2.2-UNSIGNED.exe\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no_human-0.2.2-UNSIGNED.exe\n"
        "    sha512: x\n"
        "    size: 1\n",
        encoding="utf-8",
    )
    linux_feed_path = tmp_path / "linux_feed_source.yml"
    linux_feed_path.write_text(
        "version: 0.2.2\n"
        "path: no_human-0.2.2-linux-x86_64.AppImage\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no_human-0.2.2-linux-x86_64.AppImage\n"
        "    sha512: x\n"
        "    size: 1\n",
        encoding="utf-8",
    )

    script = f"""#!/bin/sh
if [ "$1" = "release" ] && [ "$2" = "view" ]; then
  cat <<'JSON'
{assets_payload}
JSON
  exit 0
fi
if [ "$1" = "release" ] && [ "$2" = "download" ]; then
  # find --dir argument
  prev=""
  for a in "$@"; do
    if [ "$prev" = "--dir" ]; then dir="$a"; fi
    prev="$a"
  done
  cp "{mac_feed_path}" "$dir/latest-mac.yml"
  cp "{win_feed_path}" "$dir/latest.yml"
  cp "{linux_feed_path}" "$dir/latest-linux.yml"
  exit 0
fi
echo "unexpected gh invocation: $@" >&2
exit 3
"""
    path = _fakebin(tmp_path, script)
    monkeypatch.setenv("PATH", path)

    problems = gate.check_live("v0.2.2", "no-human-ai/no_human")

    assert len(problems) == 1, problems
    assert "no_human-0.2.2-arm64.dmg" in problems[0]


# --------------------------------------------------------------------------- #
# packaging/publish-release.sh — usage/preflight smoke, no network required.
# --------------------------------------------------------------------------- #

def test_publish_release_no_args_prints_usage_and_exits_1():
    proc = subprocess.run(["bash", str(PUBLISH_SH)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "Usage" in proc.stderr


def test_publish_release_refuses_an_unsigned_dmg(tmp_path):
    dmg = tmp_path / "no_human-1.0.0-UNSIGNED.dmg"
    dmg.write_bytes(b"dmg")
    proc = subprocess.run(
        ["bash", str(PUBLISH_SH), "--tag", "v1.0.0", str(dmg)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing" in proc.stderr.lower()
    assert "UNSIGNED" in proc.stderr


def test_publish_release_refuses_an_unnotarized_dmg(tmp_path):
    dmg = tmp_path / "no_human-1.0.0-UNNOTARIZED.dmg"
    dmg.write_bytes(b"dmg")
    proc = subprocess.run(
        ["bash", str(PUBLISH_SH), "--tag", "v1.0.0", str(dmg)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing" in proc.stderr.lower()


def test_publish_release_preflight_blocks_before_touching_gh(tmp_path, monkeypatch):
    """A mismatched local file list must be refused at the preflight step —
    provably before any `gh` call, by putting a `gh` on PATH that always
    fails loudly, and confirming its failure message never appears."""
    zip_path = tmp_path / "no_human-1.0.0-arm64-mac.zip"
    zip_path.write_bytes(b"zip")
    feed_path = tmp_path / "latest-mac.yml"
    feed_path.write_text(
        "version: 1.0.0\n"
        "path: no_human-1.0.0-arm64-mac.zip\n"
        "sha512: x\n"
        "files:\n"
        "  - url: no_human-1.0.0-arm64.dmg\n"
        "    sha512: y\n"
        "    size: 4\n",
        encoding="utf-8",
    )
    path = _fakebin(tmp_path, (
        "#!/bin/sh\n"
        "echo 'gh must never be called during a failing preflight' >&2\n"
        "exit 9\n"
    ))
    env = dict(os.environ)
    env["PATH"] = path
    proc = subprocess.run(
        ["bash", str(PUBLISH_SH), "--tag", "v1.0.0", str(zip_path), str(feed_path)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "preflight FAILED" in proc.stderr
    assert "gh must never be called" not in proc.stderr
    assert "gh must never be called" not in proc.stdout


# --------------------------------------------------------------------------- #
# AC4 — verified against a REAL published release, not only local fixtures.
# Live, needs `gh` authenticated against github.com; skipped otherwise so the
# push lane (which never talks to the network) is unaffected. Runs in the
# nightly lane per tests/test_test_lanes.py's marker wiring.
# --------------------------------------------------------------------------- #

def _gh_ready() -> bool:
    if shutil.which("gh") is None:
        return False
    try:
        # This suite's conftest redirects HOME to an isolated temp directory
        # (no_human.testing.pytest_isolated_home) so no test can touch the
        # operator's real ~/.no_human — but `gh`'s own auth config
        # (~/.config/gh, and the OS keychain lookup it drives) lives under
        # the REAL HOME, not the isolated one. Without restoring it here,
        # `gh auth status` reports "not logged in" inside every test
        # regardless of the operator's actual session, and this live check
        # would always (silently, wrongly) skip.
        # Scoped to github.com specifically: `gh auth status` with no
        # --hostname checks EVERY configured host and fails overall if any
        # one of them (e.g. an unrelated enterprise host) is unreachable,
        # even when github.com itself is fine — which is all this gate uses.
        proc = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "HOME": str(REAL_HOME)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@pytest.mark.nightly
@pytest.mark.skipif(not _gh_ready(), reason="gh not installed/authenticated here")
def test_v023_real_published_release_currently_names_an_unshipped_dmg(monkeypatch):
    """The defect this ticket is about, proven against the actual, currently
    published no-human-ai/no_human v0.2.3 release — not a local dist
    directory or a captured fixture. If this ever turns green on its own,
    someone already fixed v0.2.3's feed by hand; re-check before deleting."""
    # See _gh_ready()'s comment: `gh` needs the operator's REAL HOME, not
    # this suite's isolated one, to find its auth config.
    monkeypatch.setenv("HOME", str(REAL_HOME))
    problems = gate.check_live("v0.2.3", "no-human-ai/no_human")
    assert any("arm64.dmg" in p for p in problems), problems
