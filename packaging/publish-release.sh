#!/usr/bin/env bash
# packaging/publish-release.sh
#
# WHY: `gh release create`/`gh release upload` by themselves upload whatever
# file list a human typed, with nothing checking that the update feeds among
# those files (latest-mac.yml / latest.yml / latest-linux.yml) still resolve
# to what actually ships. That is exactly how v0.2.2 and v0.2.3's
# latest-mac.yml ended up naming a dmg (electron-builder's own dmg target,
# signed but never notarized/stapled) that was never in the upload list —
# and how v0.1.7 and v0.2.0 shipped Windows/Linux assets with no
# latest.yml/latest-linux.yml at all. Both were only found by hand, after
# the release was already public.
#
# This wrapper is THE documented way to publish the desktop build
# (docs/DISTRIBUTION.md §5). It runs scripts/check_release_feeds.py TWICE:
#
#   1. preflight, against the exact local file list about to be uploaded —
#      before anything reaches GitHub, so a bad batch never ships;
#   2. post-publish, against the LIVE release via `gh release view` — after
#      the upload, because that is the only way to catch a partial upload,
#      a stale leftover asset from a previous run, or anything else that
#      makes what GitHub actually serves differ from the local file list
#      that just passed step 1.
#
# Both gates are hard blockers, with no override flag: a mismatch stops the
# script rather than merely warning, because a warning is exactly what let
# v0.2.2/v0.2.3/v0.1.7/v0.2.0 ship broken feeds unnoticed in the first place.
#
# Exit codes:
#   0 published, and both the preflight and post-publish gate passed.
#   1 refused before any upload happened: bad arguments, a missing input
#     file, an UNSIGNED/UNNOTARIZED dmg on the command line, or a preflight
#     gate failure. Nothing was sent to GitHub.
#   2 the upload itself failed, or the post-publish gate failed after a
#     successful upload. The release may now be public and partially wrong;
#     this is NOT retried automatically and needs a human to inspect
#     `gh release view <tag> --repo <repo>` before trying again. The one
#     exception is an asset GitHub itself reports as still mid-upload
#     (state != "uploaded"), which is retried a bounded number of times
#     below, because that is a real "not finished yet", not a mismatch.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="no-human-ai/no_human"
TITLE=""
NOTES_FILE=""
TAG=""
FILES=()

usage() {
  cat >&2 <<'EOF'
Usage: packaging/publish-release.sh --tag vX.Y.Z [--repo owner/repo] \
         [--title "text"] [--notes-file path] FILE [FILE...]

Publishes (creates, or updates via --clobber upload) a GitHub release for
the desktop build, gated by scripts/check_release_feeds.py both BEFORE and
AFTER the upload:

  * before: the exact file list given on this command line must be
    internally consistent — every latest*.yml url:/path: in that list must
    resolve to one of the other files here, and every platform whose
    assets are in the list must bring that platform's feed file too.
  * after: the SAME check, run against what the live release actually
    reports via `gh release view`/`gh release download`, so a partial
    upload or a stale asset left over from a previous run cannot slip
    through even if the preflight passed.

Both gates are hard blockers: this script exits nonzero, without retrying
or an override flag, on any mismatch other than an asset GitHub reports as
still mid-upload.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    --notes-file) NOTES_FILE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "publish-release.sh: unknown flag: $1" >&2; usage; exit 1 ;;
    *) break ;;
  esac
done
FILES+=("$@")

if [ -z "${TAG}" ] || [ "${#FILES[@]}" -eq 0 ]; then
  usage
  exit 1
fi

for f in "${FILES[@]}"; do
  if [ ! -f "${f}" ]; then
    echo "publish-release.sh: refusing: ${f} does not exist" >&2
    exit 1
  fi
  base="$(basename "${f}")"
  if [[ "${base}" == *.dmg && ( "${base}" == *UNSIGNED* || "${base}" == *UNNOTARIZED* ) ]]; then
    echo "publish-release.sh: refusing to publish ${base}: its own filename admits it is not fully signed and notarized (packaging/make-dmg.sh names the verdict into the file — see its header) — a release must never carry a dmg whose name says this" >&2
    exit 1
  fi
done

echo "publish-release.sh: preflight — checking the ${#FILES[@]} file(s) about to ship for feed/asset mismatches"
if ! uv run python "${ROOT}/scripts/check_release_feeds.py" --assets "${FILES[@]}"; then
  echo "publish-release.sh: preflight FAILED — refusing to upload anything. Fix the mismatch above (commonly: rebuild after confirming desktop/electron-builder.config.cjs's mac.target excludes \"dmg\", or include the missing latest*.yml in this file list) and re-run." >&2
  exit 1
fi

if gh release view "${TAG}" --repo "${REPO}" >/dev/null 2>&1; then
  echo "publish-release.sh: ${TAG} already exists on ${REPO} — uploading (--clobber) rather than creating"
  if ! gh release upload "${TAG}" "${FILES[@]}" --repo "${REPO}" --clobber; then
    echo "publish-release.sh: gh release upload FAILED — ${REPO}@${TAG} may now be partially uploaded. This is NOT retried automatically; a human must inspect the release before trying again." >&2
    exit 2
  fi
else
  echo "publish-release.sh: creating ${TAG} on ${REPO}"
  CREATE_ARGS=(release create "${TAG}" --repo "${REPO}")
  [ -n "${TITLE}" ] && CREATE_ARGS+=(--title "${TITLE}")
  if [ -n "${NOTES_FILE}" ]; then
    CREATE_ARGS+=(--notes-file "${NOTES_FILE}")
  else
    CREATE_ARGS+=(--generate-notes)
  fi
  if ! gh "${CREATE_ARGS[@]}" "${FILES[@]}"; then
    echo "publish-release.sh: gh release create FAILED — ${REPO}@${TAG} may now exist partially created. This is NOT retried automatically; a human must inspect the release before trying again." >&2
    exit 2
  fi
fi

echo "publish-release.sh: post-publish — verifying the LIVE release, not just the local files that were just uploaded"
ATTEMPTS=0
MAX_ATTEMPTS=5
SLEEP_SECS=10
while :; do
  ATTEMPTS=$((ATTEMPTS + 1))
  set +e
  OUTPUT="$(uv run python "${ROOT}/scripts/check_release_feeds.py" --tag "${TAG}" --repo "${REPO}" 2>&1)"
  RC=$?
  set -e

  if [ "${RC}" -eq 0 ]; then
    echo "${OUTPUT}"
    echo "publish-release.sh: OK — ${REPO}@${TAG} is published and every feed url resolves to a real asset"
    exit 0
  fi

  if [ "${RC}" -eq 1 ] && printf '%s\n' "${OUTPUT}" | grep -q "still uploading" && [ "${ATTEMPTS}" -lt "${MAX_ATTEMPTS}" ]; then
    echo "${OUTPUT}"
    echo "publish-release.sh: some asset(s) are still mid-upload per GitHub — retrying the live check (attempt ${ATTEMPTS}/${MAX_ATTEMPTS}) in ${SLEEP_SECS}s"
    sleep "${SLEEP_SECS}"
    continue
  fi

  echo "${OUTPUT}" >&2
  echo "publish-release.sh: post-publish check FAILED — ${REPO}@${TAG} is now public with a mismatch between its feed(s) and its assets. This is NOT auto-corrected: fix the release by hand (upload the missing asset/feed, or delete and rebuild), then re-run \`uv run python scripts/check_release_feeds.py --tag ${TAG} --repo ${REPO}\` directly to confirm before telling anyone it shipped." >&2
  exit 2
done
