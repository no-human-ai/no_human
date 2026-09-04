# The CLA ledger

This directory is where agreement to [`CLA.md`](../CLA.md) is recorded.

A project that wants to keep relicensing possible has to be able to show, later
and to a stranger, that every person whose code is in the tree agreed to the
terms that make it possible. A tick-box in a pull-request template is not that.
A file in git is: it has an author, a timestamp, a commit, and a diff, and it
cannot be edited afterwards without leaving a trace.

There is no signature service and nothing to sign up for. CI checks that the
file exists, and a comment on your PR shows you this file filled in; neither
signs anything for you. One file,
once, in your first pull request.

## How to sign

Add **one file** named after your GitHub handle, in lower case, with a `.md`
extension. If your handle is `octocat`, the file is `contributors/octocat.md`.

Copy this and fill it in:

```markdown
# octocat

I have read CLA.md version 1.0 and I agree to it.

- GitHub: @octocat
- Name: Mona Lisa Octocat
- Date: 2026-01-31
- Signing as: myself
```

Notes on the fields:

- **CLA version.** Name the version at the top of [`CLA.md`](../CLA.md) as it
  stands when you sign. That is the point of the version number: a later
  revision cannot silently rewrite what you agreed to.
- **Name** is optional. The handle is what CI matches on.
- **Signing as** is `myself`, or the name of the employer you are authorised to
  bind. If your employer owns your work, read section 7 of `CLA.md` first.

Commit it in your first pull request, alongside your change. You never have to
do it again.

## What CI checks

The `CLA ledger` job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
asks GitHub for the authors of the commits in your pull request and fails if any
of them has no file here. It checks that a file exists, nothing more — it does
not read the contents and it cannot verify a signature. The record is the point;
the check only stops one from being forgotten.

Three kinds of author are skipped: the maintainer, who does not sign their own
agreement; bot accounts such as `dependabot[bot]`, which hold no copyright to
license; and `no-human`, this repo's own agent account, whose commits are
first-party work — but only on pull requests the agent or the maintainer
opened. On anyone else's PR a `no-human`-authored commit reads as a forged
author email, and no file in this directory can satisfy it.

One thing the check cannot see: a `Co-authored-by:` trailer. GitHub resolves a
commit's *author*, and that is all this job asks for. If someone co-authored
your work, they contributed too and they need their own file here — add it, or
say so in the PR so the maintainer can. CI will not catch that for you.

If the job says your author is `UNLINKED-EMAIL`, the email on your commits is
not attached to any GitHub account, so GitHub cannot tell CI who you are. Add
that address to your GitHub account, or set `user.email` to one that is already
there, and push again.
