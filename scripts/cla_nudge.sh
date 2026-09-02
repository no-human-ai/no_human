#!/usr/bin/env bash
# CLA nudge — tell a pull-request author without a contributors/ ledger entry
# exactly which file to add, in one comment on the PR, updated in place.
#
# Run by .github/workflows/cla-nudge.yml on `pull_request_target`, which runs
# with the BASE repository's token the moment a PR is opened or pushed to — no
# "approve and run" gate, so the contributor hears about the ledger before any
# maintainer looks. The fork's code is never checked out or executed here: the
# checkout is the base branch (for CLA.md's version), and the ledger lookup
# goes through the contents API against the PR head's commit.
#
# It NEVER signs for anyone and never commits: the signature stays the file the
# contributor adds in their own commit (contributors/README.md). The `CLA
# ledger` job in ci.yml remains the gate; this only removes the guessing. The
# author resolution is the same as that job's: GitHub's `.author.login` for
# every commit on the PR, the maintainer and bot accounts skipped, a commit
# whose email is attached to no GitHub account reported as unlinked.
#
# Env: GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA, BASE_SHA, MAINTAINER.
# CLA_NUDGE_DRY_RUN=1 prints the comment that would be posted and exits 0.
set -euo pipefail

: "${GITHUB_REPOSITORY:?}" "${PR_NUMBER:?}" "${HEAD_SHA:?}" "${BASE_SHA:?}" "${MAINTAINER:?}"
MARKER='<!-- cla-nudge -->'
CLA_VERSION=$(sed -n 's/^\*\*Version: \([^*]*\)\*\*.*/\1/p' CLA.md | head -1)
: "${CLA_VERSION:?could not read the version line from CLA.md}"
TODAY=$(date -u +%F)

authors=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/commits" \
  --jq '.[].author.login // "UNLINKED-EMAIL"' | sort -u)

ledger_has() {  # <handle> — true if contributors/<handle>.md exists at the PR head or on the base
  local path="contributors/$1.md" ref
  for ref in "$HEAD_SHA" "$BASE_SHA"; do
    gh api "repos/${GITHUB_REPOSITORY}/contents/${path}?ref=${ref}" >/dev/null 2>&1 && return 0
  done
  return 1
}

missing=()
unlinked=0
for author in $authors; do
  case "$author" in
    "$MAINTAINER"|*'[bot]') continue ;;
    UNLINKED-EMAIL) unlinked=1; continue ;;
  esac
  handle=$(printf '%s' "$author" | tr '[:upper:]' '[:lower:]')
  ledger_has "$handle" || missing+=("$handle")
done

body_file=$(mktemp)
if [ "${#missing[@]}" -eq 0 ] && [ "$unlinked" -eq 0 ]; then
  printf '%s\n' "$MARKER" \
    "Every commit author on this pull request has a CLA ledger entry under \`contributors/\`. Thank you." \
    > "$body_file"
  resolved=1
else
  resolved=0
  {
    printf '%s\n' "$MARKER"
    printf '%s\n\n' "Thanks for the pull request. Before it can merge, the \`CLA ledger\` check needs one file per commit author recording agreement to [\`CLA.md\`](https://github.com/${GITHUB_REPOSITORY}/blob/main/CLA.md) (version ${CLA_VERSION}). It is a file in git, not a form: you add it in a commit of your own, once, ever. Read \`CLA.md\` first — agreeing is the point."
    for handle in ${missing[@]+"${missing[@]}"}; do  # empty-array-safe on bash 3.2 too
      printf '%s\n\n' "**@${handle}** — add \`contributors/${handle}.md\` with this content (edit the name line if you like; \`Signing as\` is \`myself\` or the employer you are authorised to bind — see section 7 of CLA.md):"
      printf '%s\n' '```markdown' "# ${handle}" "" "I have read CLA.md version ${CLA_VERSION} and I agree to it." "" \
        "- GitHub: @${handle}" "- Name: " "- Date: ${TODAY}" "- Signing as: myself" '```'
      printf '\n%s\n' 'Then, on your branch:'
      printf '%s\n' '```' "git add contributors/${handle}.md" "git commit -m \"Agree to CLA.md version ${CLA_VERSION}\"" "git push" '```'
      printf '\n'
    done
    if [ "$unlinked" -eq 1 ]; then
      printf '%s\n\n' "At least one commit on this PR has an author email that is not attached to any GitHub account, so the ledger cannot tell who wrote it. Either add that email to your GitHub account (Settings → Emails) or re-author the commits with an address that is, then push again."
    fi
    printf '%s\n' "This comment updates itself on every push. Nothing here signs anything on your behalf."
  } > "$body_file"
fi

if [ "${CLA_NUDGE_DRY_RUN:-0}" = "1" ]; then
  echo "--- would post (resolved=${resolved}) ---"; cat "$body_file"; exit 0
fi

existing=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
  --jq ".[] | select(.body | contains(\"${MARKER}\")) | .id" | head -1)
if [ -n "$existing" ]; then
  gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing}" -F body=@"$body_file" >/dev/null
  echo "updated comment ${existing} (resolved=${resolved})"
elif [ "$resolved" -eq 0 ]; then
  gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" -F body=@"$body_file" >/dev/null
  echo "posted nudge for: ${missing[*]:-}$([ "$unlinked" -eq 1 ] && echo " (+ an unlinked email)")"
else
  echo "nothing missing and no earlier nudge to resolve"
fi
