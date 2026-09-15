---
name: release-notes
description: Write release notes for a tagged release — a business-impact summary of what changed for the user, plus a thanks section naming every contributor whose commits are in the release. Short by construction.
---

# Release notes

Turn one release (a tag, or a range of commits) into notes a reader skims in
under a minute: what the release does for them, stated as impact, and a thank-
you to everyone whose work is in it. Not a changelog, not a commit list.

Three rules, in priority order:

1. **Lead with business impact, not mechanism.** Every line answers "what does
   this change let me do / stop worrying about," not "what file moved." Group by
   the value to the reader, not by subsystem. Drop anything a user never sees
   (CI wiring, test-only changes, internal refactors) unless it changed a
   guarantee they rely on.
2. **Thank every contributor whose commits are in the release** (see below).
   Completeness here is not optional — a missing name is the one error people
   notice.
3. **Short.** If a section is not impact or thanks, cut it. No methodology
   hedging, no "we're excited to", no filler. A reader should reach the end
   without scrolling twice.

## Step 1 — establish the exact range, then read it

Never write from memory or from a PR list. Read the real commits.

- Find the previous release tag and this one: `git tag --sort=-creatordate | head`.
- List the commits: `git log <prev-tag>..<this-tag> --format='%h %an%x09%s'`.
  (If there is no tag yet, use `<prev-tag>..HEAD` and say so.)
- Verify both endpoints resolve (`git rev-parse <tag>`), and that the range is
  non-empty. An empty range means the wrong tags — stop and fix it.

Read every subject line. Cluster them by what the user gets. A cluster of five
commits that together fix one thing is ONE bullet about that thing.

## Step 2 — write the impact, and claim only what the range establishes

For each cluster, write one line of impact. Constraints:

- State only what the commits actually did. If you cannot point to the commit
  that establishes a claim, do not make the claim. No numbers without a source
  you can name (a command, a file). If you quote a measurement, say where it
  was measured.
- Platform coverage and signing, when relevant, are stated plainly and
  honestly — what is signed, what is not, and what the user must do about it.
  Never imply a signature or an auto-update path that does not exist.
- Keep the project's own voice. Do not add a tagline the project has not
  adopted; if it has a pinned one, use it verbatim and do not reword it.
- Banned, because they read as machine-written or as marketing haze:
  "verbatim", "delve", "seamless", "robust", "we're thrilled/excited",
  "leverage" (as a verb), "in today's fast-paced". Say the plain thing instead.

## Step 3 — the thanks section: every contributor, once, by handle

The contributors are the distinct **authors** of the commits in the range —
the AUTHOR, never the committer (many projects normalise the committer to one
maintainer identity, so the committer tells you nothing about who wrote it).

1. List the authoring commits: `git log <prev-tag>..<this-tag> --format='%H%x09%an%x09%ae'`.
2. **Resolve each commit to a GitHub handle authoritatively — do NOT parse the
   email.** An email does not reliably encode the handle: a GitHub noreply
   address like `103962359+L4XB@users.noreply.github.com` happens to embed the
   login, but a personal address like `rahulpamula123@gmail.com` embeds nothing,
   and the login is not the display name (author `Rahul-pamula` with that email
   is handle `Rahul-pamula`, whose CLA ledger file is `rahul-pamula.md` — a dash
   and a case the email never shows). Get the login from the forge instead:
   `gh api repos/<owner>/<repo>/commits/<sha> --jq '.author.login'` per commit
   (or `.commit.author` cross-checked against the PR that landed it). That is
   the handle; the email is only a hint you must confirm.
3. **Also enumerate co-authors — the author query cannot see them.** `%an/%ae`
   and the commits API each return exactly ONE author per commit; a
   `Co-authored-by:` trailer (used for pairing, and by some squash-merges to
   credit a second person) is invisible to both. Read them explicitly:
   `git log <prev-tag>..<this-tag> --format='%(trailers:key=Co-authored-by,valueonly)'`,
   which yields `Name <email>` lines. Resolve each to a handle the same way as a
   primary author (by the ledger, or a forge lookup on the email) and add it to
   the set. Do not skip this because "we don't use trailers" — verify it on the
   range; a single co-authored commit crediting an external is a name you would
   otherwise drop.
4. Remove the maintainer/release identity and any bots (`dependabot[bot]`,
   `github-actions[bot]`, and the maintainer's own login) from the combined set
   of primary authors and co-authors. What remains is the external contributors.
5. **Deduplicate to one person.** The same human appears under multiple author
   identities in one range (a noreply email on one commit, a personal email on
   another; a display name and a login) — e.g. `Prince Panchani` and
   `PrinceXDev` are one person, handle `PrinceXDev`. Collapse by resolved
   handle, not by email or name, so each person is thanked exactly once.
6. If the project keeps a CLA ledger (`contributors/<handle>.md` — the filename
   IS the handle, lowercased), confirm each resolved handle has a matching
   ledger file and print it in the ledger's spelling. The CLA gate guarantees
   every external author has a ledger entry, so a resolved author with no ledger
   match means your handle resolution is wrong (you parsed the email, or missed
   a dash/case), not that they are unlisted — re-resolve that author's handle
   (list item 2 above), do not drop them.
   Without a ledger, and when a commit's `.author.login` comes back `null` (the
   commit email is tied to no GitHub account), resolve the person by the
   `Name <email>` from `git show -s --format='%an <%ae>'` and, if you still
   cannot get a handle, name them by that display name rather than dropping
   them — a missing contributor is worse than an un-linked one.
7. Verify by UNION, not by recounting one query. The distinct people you print
   must equal the deduped union of {primary-author handles} ∪ {co-author
   handles} ∪ {any commit whose author login was null}, minus maintainer/bots.
   Do NOT re-derive the check from the same `%ae` set you thanked from — a
   count built from the query that already dropped the co-authors and the
   null-logins will "confirm" a list that is missing exactly them. Recompute
   each input set independently and compare.

Write the section as a plain thank-you naming each `@handle`, separated so it
scans (e.g. `·`-joined on one line, or a short list). One sentence of context is
enough; do not explain the contribution model at length.

## Step 4 — self-review before you hand it over

- Every impact line: is it true of the code in this range, and is it impact
  (not mechanism)?
- Thanks: re-run the author query and confirm every distinct external person is
  present exactly once, spelled as their real handle.
- Length: could a reader skim it in under a minute? If not, cut.
- Then have it independently reviewed before it ships — release notes are
  outward-facing text, and a false or missing claim there is the expensive kind.

## Shape (adapt; keep it short)

```
<product> <version> — <platforms in one clause>.

<one or two lines: the headline of the release, as impact.>

## Fixed / Added / Changed   (only the sections you actually have)
**<impact, bold lead>.** <one or two sentences, plain.>
…

## <Signing / install, if the release changes it, stated plainly>

## Thanks
<one sentence>, then: @handle · @handle · @handle
```
