#!/usr/bin/env python3
"""Verify that every published update feed names only assets that ship.

electron-builder's per-platform updater feed (`latest-mac.yml`, `latest.yml`,
`latest-linux.yml`) is generated as a build artifact and then, separately and
by hand, some subset of the build's other artifacts is uploaded to a GitHub
release. Nothing has ever checked that those two lists agree, and both of the
possible mismatches have already shipped:

  * v0.2.2 and v0.2.3's `latest-mac.yml` name a second file
    (`no_human-<version>-arm64.dmg`, electron-builder's own dmg target) that
    the release never uploads — only the make-dmg.sh output
    (`no_human-<version>.dmg`, notarized and stapled) is published, because
    electron-builder's dmg bypasses that script and is signed but never
    notarized. `path:` still points at the zip, which IS published, so the
    normal update path keeps working — but a second `electron-updater` code
    path that reads the `files:` list would fetch a URL that 404s.
  * v0.1.7 and v0.2.0 shipped Windows/Linux assets with NO `latest.yml` /
    `latest-linux.yml` at all, so every "Check for updates" 404s outright.

Both were found by hand, after the fact. This script is the automated gate:
for a release (real, via `gh release view`, or a local file list about to be
uploaded via `--assets`), it asserts every `url:`/`path:` in every feed
resolves to an asset of that same release, and that every platform with
assets present in the release has its feed.

Exit codes: 0 clean, 1 a feed/asset mismatch (or no such release, or a draft
release), 2 the check itself could not run (gh missing/unauthenticated, a
feed could not be downloaded). 2 is never collapsed into 0 or 1: a broken
runner must not look like a pass.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import posixpath
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote

import yaml

DEFAULT_REPO = "no-human-ai/no_human"

#: platform key -> (glob(s) that mark the platform as present, expected feed name)
_PLATFORM_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("macOS", "latest-mac.yml", ("*-mac.zip", "*.dmg")),
    ("Windows", "latest.yml", ("*.exe",)),
    ("Linux", "latest-linux.yml", ("*.AppImage",)),
]


class GhUnavailable(Exception):
    """The check could not run at all — environment, not content, is wrong."""


class ReleaseNotFound(Exception):
    """`gh` ran fine and reported no such release."""


# --------------------------------------------------------------------------- #
# Pure core — no IO, this is what the tests drive directly.
# --------------------------------------------------------------------------- #

def feed_names_for(asset_names: set[str]) -> dict[str, str]:
    """``{platform label -> expected feed filename}`` for platforms present.

    A platform counts as "present in this release" only from the artifacts a
    human actually downloads or Squirrel/AppImage-updater installs from — not
    from the feed files themselves, which is what makes this usable to detect
    a MISSING feed in the first place.
    """
    expected: dict[str, str] = {}
    for label, feed_name, globs in _PLATFORM_RULES:
        if any(fnmatch.fnmatch(name, g) for name in asset_names for g in globs):
            expected[label] = feed_name
    return expected


def feed_referenced_names(feed_text: str) -> set[str]:
    """Every asset filename an update feed points at (`path:` + `files[].url`).

    electron-builder writes `safeArtifactName`, which can be percent-encoded,
    so URLs are decoded before taking the basename.
    """
    data = yaml.safe_load(feed_text)
    if not isinstance(data, dict):
        raise ValueError("feed does not parse to a mapping")
    names: set[str] = set()
    path = data.get("path")
    if path:
        names.add(posixpath.basename(unquote(str(path))))
    files = data.get("files")
    if files:
        for entry in files:
            url = entry.get("url") if isinstance(entry, dict) else None
            if url:
                names.add(posixpath.basename(unquote(str(url))))
    if not names:
        raise ValueError("feed names no files at all (no path: or files[].url)")
    return names


def check(asset_names: set[str], feeds: dict[str, str]) -> list[str]:
    """Problem strings for a release whose assets are ``asset_names``.

    ``feeds`` maps a feed's filename (``"latest-mac.yml"``) to its text, for
    whichever feeds the caller has content for. A feed's mere PRESENCE is
    read from ``asset_names`` (feed files are assets too), so "this platform's
    feed is missing entirely" is checkable even with an empty ``feeds`` dict —
    exactly the v0.1.7 / v0.2.0 shape, where there is no feed to download.
    """
    problems: list[str] = []

    expected = feed_names_for(asset_names)
    for label, feed_name in expected.items():
        if feed_name not in asset_names:
            problems.append(
                f"this release ships {label} assets but carries no "
                f"{feed_name} — every {label} 'Check for updates' 404s"
            )

    for feed_name in sorted(feeds):
        text = feeds[feed_name]
        try:
            referenced = feed_referenced_names(text)
        except ValueError as exc:
            problems.append(f"{feed_name} could not be checked: {exc}")
            continue
        for name in sorted(referenced):
            if name not in asset_names:
                problems.append(
                    f"{feed_name} names {name}, which is not an asset of "
                    "this release"
                )
    return problems


def uploaded_asset_names(assets: list[dict]) -> set[str]:
    """Names of assets that have actually finished uploading.

    A GitHub release asset mid-upload reports `state: "starter"` (or anything
    other than `"uploaded"`); until it flips, the URL 404s exactly like a
    permanently missing one, so it must count as absent here too.
    """
    return {a["name"] for a in assets if a.get("state", "uploaded") == "uploaded"}


def uploading_asset_problems(assets: list[dict]) -> list[str]:
    """One problem per asset that exists but has not finished uploading."""
    return [
        f"{a['name']} is still uploading (state={a.get('state')}); treated "
        "as absent until it finishes"
        for a in assets
        if a.get("state", "uploaded") != "uploaded"
    ]


# --------------------------------------------------------------------------- #
# IO layer — `gh` is the only external system.
# --------------------------------------------------------------------------- #

def _gh(args: list[str]) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise GhUnavailable(f"gh is not on PATH: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "release not found" in stderr.lower():
            raise ReleaseNotFound(stderr or "release not found")
        raise GhUnavailable(
            f"gh exited {proc.returncode}: {stderr or '(no stderr)'}"
        )
    return proc.stdout


def fetch_release(tag: str, repo: str) -> dict:
    """`gh release view` as a dict, or raise ReleaseNotFound / GhUnavailable."""
    out = _gh([
        "release", "view", tag, "--repo", repo,
        "--json", "tagName,isDraft,isPrerelease,assets",
    ])
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhUnavailable(f"gh printed unparseable JSON: {exc}") from exc


def download_feeds(tag: str, repo: str, feed_names: set[str]) -> dict[str, str]:
    """Text of every named feed asset actually present in the release."""
    if not feed_names:
        return {}
    with tempfile.TemporaryDirectory(prefix="check-release-feeds-") as tmp:
        tmp_path = Path(tmp)
        try:
            _gh([
                "release", "download", tag, "--repo", repo,
                "--pattern", "*.yml", "--dir", str(tmp_path), "--clobber",
            ])
        except (GhUnavailable, ReleaseNotFound):
            raise
        feeds: dict[str, str] = {}
        for name in sorted(feed_names):
            p = tmp_path / name
            if not p.exists():
                raise GhUnavailable(
                    f"{name} is listed as an asset but could not be downloaded"
                )
            feeds[name] = p.read_text(encoding="utf-8")
        return feeds


def check_live(tag: str, repo: str) -> list[str]:
    """The full live-release check: fetch, then run the pure core."""
    data = fetch_release(tag, repo)
    if data.get("isDraft"):
        return [
            f"{tag} is a draft release — a draft is invisible to "
            "electron-updater and its asset URLs are not public, so "
            "nothing downstream can resolve them yet"
        ]

    assets = data.get("assets") or []
    asset_names = uploaded_asset_names(assets)
    problems = uploading_asset_problems(assets)

    expected = feed_names_for(asset_names)
    feeds = download_feeds(tag, repo, set(expected.values()) & asset_names)
    problems.extend(check(asset_names, feeds))
    return problems


def check_local(paths: list[str]) -> list[str]:
    """Preflight mode: identical checks over a local file list, no network."""
    asset_names = {Path(p).name for p in paths}
    feeds: dict[str, str] = {}
    for p in paths:
        name = Path(p).name
        if name.endswith(".yml"):
            try:
                feeds[name] = Path(p).read_text(encoding="utf-8")
            except OSError as exc:
                raise GhUnavailable(f"could not read local feed {p}: {exc}") from exc
    return check(asset_names, feeds)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify every published update feed's url:/path: "
                     "resolves to an asset actually present in the release.")
    ap.add_argument("--tag", help="release tag, e.g. v0.2.3")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                     help=f"owner/repo (default: {DEFAULT_REPO})")
    ap.add_argument("--assets", nargs="+", metavar="PATH",
                     help="preflight mode: check this local file list "
                          "instead of a live release (no network)")
    ap.add_argument("--json", action="store_true",
                     help="print the problem list as JSON instead of text")
    args = ap.parse_args(argv)

    if args.assets:
        try:
            problems = check_local(args.assets)
        except GhUnavailable as exc:
            print(f"check-release-feeds: {exc}", file=sys.stderr)
            return 2
        source = f"{len(args.assets)} local asset(s)"
    else:
        if not args.tag:
            ap.error("either --tag <release> or --assets <path>... is required")
        try:
            problems = check_live(args.tag, args.repo)
        except ReleaseNotFound as exc:
            print(f"check-release-feeds: nothing to check: no release "
                  f"{args.tag} in {args.repo} ({exc})", file=sys.stderr)
            return 1
        except GhUnavailable as exc:
            print(f"check-release-feeds: {exc}", file=sys.stderr)
            return 2
        source = f"{args.repo}@{args.tag}"

    if args.json:
        print(json.dumps({"source": source, "problems": problems}, indent=2))
    elif problems:
        print(f"check-release-feeds: {len(problems)} problem(s) in {source}:")
        for p in problems:
            print(f"  - {p}")
    else:
        print(f"check-release-feeds: OK — every feed url in {source} "
              "resolves to a published asset")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
