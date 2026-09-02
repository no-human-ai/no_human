#!/usr/bin/env bash
# CLA nudge — tell a pull-request author without a contributors/ ledger entry
# exactly which file to add, in one comment on the PR, updated in place.
#
# Run by .github/workflows/cla-nudge.yml on `pull_request_target`, which runs
# with the BASE repository's token the moment a PR is opened or pushed to — no
# "approve and run" gate, so the contributor hears about the ledger before any
# maintainer looks. The fork's code is never checked out or executed here: the
# checkout is the base branch (for CLA.md's version), and the ledger is read
# through the contents API.
#
# It NEVER signs for anyone and never commits: the signature stays the file the
# contributor adds in their own commit (contributors/README.md). The `CLA
# ledger` job in ci.yml remains the gate; this only removes the guessing.
#
# What it looks at, and why it agrees with the gate:
#   * authors — GitHub's `.author.login` for every commit on the PR, the
#     maintainer and bot accounts skipped, a commit whose email is attached to
#     no GitHub account reported as unlinked. Identical to the gate's loop.
#   * the ledger — ONE directory listing of `contributors/` at the PR's merge
#     commit (`merge_commit_sha`: head merged onto CURRENT main, the tree the
#     gate's checkout sees). GitHub computes that commit asynchronously: on
#     `opened` the payload may not carry it yet, and on `synchronize` it may
#     still carry the merge of the PREVIOUS head. A merge commit's second
#     parent is the head it merged, so a value is trusted only when that
#     parent is this event's head; otherwise the script asks the API a few
#     times, and if there still is none — a conflicting PR, or one GitHub has
#     not got to — it posts NOTHING and exits 0 rather than read some other
#     tree and make a public statement the gate would not. The next push
#     tries again. A handful of API reads per run
#     regardless of how many authors a PR carries — a fork can make the
#     author list as long as it likes, and this must not turn into a request
#     per author against the repository's shared token quota. Only a genuine
#     404 (no contributors/ directory in that tree) reads as an empty ledger;
#     any other failure aborts the run, because an empty ledger nudges every
#     author at once and must never come from a rate limit or a 500. (The
#     listing returns at most 1,000 entries; revisit before the ledger nears
#     that.)
#   * size — a PR with more than MAX_NUDGED distinct unsigned authors gets one
#     short comment naming the count, not a block per author: the per-author
#     block is ~500 bytes and a comment body is capped at 65,536.
#   * mentions — handles are written as `@handle` INSIDE backticks, which
#     GitHub does not turn into a notification: the author email on a fork's
#     commit is under the fork's control, so a real mention would let a PR
#     ping any account it names.
#   * its own comment — found by marker AND by author (github-actions[bot]);
#     a person quoting the marker text is not this bot's comment to edit.
#
# Env: GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA, MERGE_SHA (may be
# empty or stale), MAINTAINER. CLA_NUDGE_DRY_RUN=1 prints the comment and exits 0.
# CLA_NUDGE_POLL_SECONDS (default 5) is the wait between merge-commit polls.
set -euo pipefail

: "${GITHUB_REPOSITORY:?}" "${PR_NUMBER:?}" "${HEAD_SHA:?}" "${MAINTAINER:?}"
MERGE_SHA="${MERGE_SHA:-}"
MARKER='<!-- cla-nudge -->'
BOT_LOGIN='github-actions[bot]'
MAX_NUDGED="${MAX_NUDGED:-10}"
# `sed -n 1p` reads to end-of-input, so it cannot SIGPIPE a producer under
# pipefail the way `head -1` can when there is more than one line.
CLA_VERSION=$(sed -n 's/^\*\*Version: \([^*]*\)\*\*.*/\1/p' CLA.md | sed -n 1p)
: "${CLA_VERSION:?could not read the version line from CLA.md}"
TODAY=$(date -u +%F)
DOC_URL="https://github.com/${GITHUB_REPOSITORY}/blob/main/CLA.md"

# The tree the gate sees: THIS head merged onto current main. A merge commit
# is trusted only if its second parent is this head (the payload's value can
# be empty on `opened` or one push stale on `synchronize`); otherwise poll
# briefly, and give up quietly rather than guess from another tree.
merges_this_head() {  # <sha> — true if <sha>'s second parent is HEAD_SHA
  [ -n "$1" ] && [ "$(gh api "repos/${GITHUB_REPOSITORY}/commits/$1" --jq '.parents[1].sha // ""')" = "$HEAD_SHA" ]
}
if ! merges_this_head "$MERGE_SHA"; then
  MERGE_SHA=""
  for attempt in 1 2 3 4 5 6; do
    [ "$attempt" -gt 1 ] && sleep "${CLA_NUDGE_POLL_SECONDS:-5}"
    candidate=$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" --jq '.merge_commit_sha // ""')
    if merges_this_head "$candidate"; then MERGE_SHA="$candidate"; break; fi
  done
  if [ -z "$MERGE_SHA" ]; then
    echo "no merge commit of ${HEAD_SHA} for PR ${PR_NUMBER} yet (conflicting, or not computed): nothing posted"
    exit 0
  fi
fi

authors=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/commits" \
  --jq '.[].author.login // "UNLINKED-EMAIL"' | sort -u)
if [ -z "$authors" ]; then
  echo "PR ${PR_NUMBER} lists no commits: nothing to say"
  exit 0
fi

# ONE listing of contributors/ at the merge commit. A 404 is "no such
# directory in that tree" (an empty ledger); anything else is a failed read
# and the run stops, because an empty ledger would nudge every author at once.
err_file=$(mktemp)
if ! ledger=$(gh api "repos/${GITHUB_REPOSITORY}/contents/contributors?ref=${MERGE_SHA}" \
    --jq '.[].name' 2>"$err_file"); then
  # gh words a missing path "Not Found (HTTP 404)" and an unknown ref
  # "No commit found for the ref ... (HTTP 404)": only the first is an empty
  # ledger; the second is a read that never happened.
  if grep -q "HTTP 404" "$err_file" && ! grep -q "No commit found" "$err_file"; then
    ledger=""
  else
    echo "could not read contributors/ at ${MERGE_SHA}; not posting on an unread ledger:" >&2
    cat "$err_file" >&2
    exit 1
  fi
fi

missing=()
unlinked=0
while IFS= read -r author; do
  [ -n "$author" ] || continue
  case "$author" in
    "$MAINTAINER"|*'[bot]') continue ;;
    UNLINKED-EMAIL) unlinked=1; continue ;;
  esac
  handle=$(printf '%s' "$author" | tr '[:upper:]' '[:lower:]')
  grep -qxF "${handle}.md" <<<"$ledger" || missing+=("$handle")
done <<<"$authors"

body_file=$(mktemp)
if [ "${#missing[@]}" -eq 0 ] && [ "$unlinked" -eq 0 ]; then
  printf '%s\n' "$MARKER" \
    "The \`CLA ledger\` check has what it needs: every non-exempt commit author on this pull request has a \`contributors/\` entry (the maintainer's own commits and bot accounts are exempt). Thank you." \
    > "$body_file"
  resolved=1
else
  resolved=0
  {
    printf '%s\n' "$MARKER"
    printf '%s\n\n' "Thanks for the pull request. Before it can merge, the \`CLA ledger\` check needs one file per commit author recording agreement to [\`CLA.md\`](${DOC_URL}) (version ${CLA_VERSION}). It is a file in git, not a form: you add it in a commit of your own, once, ever. Read \`CLA.md\` first — agreeing is the point."
    if [ "${#missing[@]}" -gt "$MAX_NUDGED" ]; then
      printf '%s\n\n' "This pull request has ${#missing[@]} distinct commit authors without a ledger entry — too many to list here. Each needs \`contributors/<handle>.md\` (lower-case GitHub handle) with the content shown in \`contributors/README.md\`."
    else
      for handle in ${missing[@]+"${missing[@]}"}; do
        printf '%s\n\n' "\`@${handle}\` — add \`contributors/${handle}.md\` with this content (edit the name line if you like; \`Signing as\` is \`myself\` or the employer you are authorised to bind — see section 7 of CLA.md):"
        printf '%s\n' '```markdown' "# ${handle}" "" "I have read CLA.md version ${CLA_VERSION} and I agree to it." "" \
          "- GitHub: @${handle}" "- Name: " "- Date: ${TODAY}" "- Signing as: myself" '```'
        printf '\n%s\n' 'Then, on your branch:'
        printf '%s\n' '```' "git add contributors/${handle}.md" "git commit -m \"Agree to CLA.md version ${CLA_VERSION}\"" "git push" '```'
        printf '\n'
      done
    fi
    if [ "$unlinked" -eq 1 ]; then
      printf '%s\n\n' "At least one commit on this PR has an author email that is not attached to any GitHub account, so the ledger cannot tell who wrote it. Either add that email to your GitHub account (Settings → Emails) or re-author the commits with an address that is, then push again."
    fi
    printf '%s\n' "This comment is updated on each push once GitHub has computed the pull request's merge commit. Nothing here signs anything on your behalf."
  } > "$body_file"
fi

if [ "${CLA_NUDGE_DRY_RUN:-0}" = "1" ]; then
  echo "--- would post (resolved=${resolved}) ---"; cat "$body_file"; exit 0
fi

existing=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
  --jq ".[] | select(.user.login == \"${BOT_LOGIN}\" and (.body | contains(\"${MARKER}\"))) | .id" | sed -n 1p)
if [ -n "$existing" ]; then
  gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing}" -F body=@"$body_file" >/dev/null
  echo "updated comment ${existing} (resolved=${resolved})"
elif [ "$resolved" -eq 0 ]; then
  gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" -F body=@"$body_file" >/dev/null
  echo "posted nudge: ${#missing[@]} unsigned author(s), unlinked=${unlinked}"
else
  echo "nothing missing and no earlier nudge to resolve"
fi
