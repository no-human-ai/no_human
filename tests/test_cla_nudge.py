"""`scripts/cla_nudge.sh` (run by .github/workflows/cla-nudge.yml on
`pull_request_target`): the comment it posts, the ledger semantics it shares
with the `CLA ledger` job in ci.yml, and the write path (POST / PATCH) — all
driven through a stub `gh` executable placed first on PATH, which records
every call so the API budget and the exact write can be asserted."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "cla_nudge.sh"
CLA_VERSION = next(
    line.split("Version: ")[1].rstrip("*").strip()
    for line in (REPO / "CLA.md").read_text().splitlines() if line.startswith("**Version:"))

STUB_GH = r'''#!/usr/bin/env bash
# Stub `gh` for tests: replays canned answers from $STUB_DIR, logs every call.
set -u
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"
args="$*"
case "$args" in
  *"/pulls/"*"/commits"*)  cat "$STUB_DIR/commits.txt" ;;
  *"/pulls/"*" --jq .merge_commit_sha"*)  cat "$STUB_DIR/merge_sha.txt" 2>/dev/null || echo "" ;;
  *"/commits/"*" --jq .parents[1].sha"*)
      sha="${args##*/commits/}"; sha="${sha%% *}"
      cat "$STUB_DIR/parent2.$sha" 2>/dev/null || echo "" ;;
  *"/contents/contributors?ref="*)
      ref="${args##*ref=}"; ref="${ref%% *}"
      if [ -f "$STUB_DIR/ledger.fail" ]; then echo "gh: HTTP 500: boom" >&2; exit 1; fi
      if [ -f "$STUB_DIR/ledger.badref" ]; then echo "gh: No commit found for the ref $ref (HTTP 404)" >&2; exit 1; fi
      if [ -f "$STUB_DIR/ledger.$ref" ]; then cat "$STUB_DIR/ledger.$ref"; else echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi ;;
  *"/issues/"*"/comments --jq"*)  cat "$STUB_DIR/existing.txt" 2>/dev/null || true ;;
  *"-X PATCH"*)  cp "${args##*body=@}" "$STUB_DIR/patched.md" ;;
  *"-X POST"*)   cp "${args##*body=@}" "$STUB_DIR/posted.md" ;;
  *) echo "unexpected gh call: $args" >&2; exit 2 ;;
esac
'''


@pytest.fixture
def run(tmp_path):
    """Run the script against a stub `gh`; returns (result, stub_dir)."""
    stub_dir = tmp_path / "stub"; stub_dir.mkdir()
    gh = tmp_path / "bin" / "gh"; gh.parent.mkdir(); gh.write_text(STUB_GH); gh.chmod(0o755)

    def _run(*, authors, ledger_at=None, existing="", merge_sha="m1", api_merge_sha="",
             head_sha="h1", parents=None, ledger_fail=False, dry=False, env=None):
        (stub_dir / "commits.txt").write_text("".join(f"{a}\n" for a in authors))
        # every merge commit's second parent is the head unless a test says otherwise
        for sha, parent in {**{merge_sha: head_sha, api_merge_sha: head_sha}, **(parents or {})}.items():
            if sha:
                (stub_dir / f"parent2.{sha}").write_text(parent + "\n")
        for ref, names in (ledger_at or {}).items():
            (stub_dir / f"ledger.{ref}").write_text("".join(f"{n}\n" for n in names))
        (stub_dir / "existing.txt").write_text(existing)
        (stub_dir / "merge_sha.txt").write_text(api_merge_sha + "\n")
        for stale in ("posted.md", "patched.md", "calls.log", "ledger.fail", "ledger.badref"):
            (stub_dir / stale).unlink(missing_ok=True)
        if ledger_fail:
            (stub_dir / ("ledger.badref" if ledger_fail == "badref" else "ledger.fail")).write_text("")
        e = {**os.environ, "PATH": f"{gh.parent}:{os.environ['PATH']}", "STUB_DIR": str(stub_dir),
             "GITHUB_REPOSITORY": "acme/thing", "PR_NUMBER": "7", "MERGE_SHA": merge_sha,
             "HEAD_SHA": head_sha, "MAINTAINER": "eyalgolan", "PR_AUTHOR": "eyalgolan",
             "CLA_NUDGE_POLL_SECONDS": "0",
             **(env or {})}
        if dry:
            e["CLA_NUDGE_DRY_RUN"] = "1"
        res = subprocess.run(["bash", str(SCRIPT)], cwd=REPO, env=e, capture_output=True, text=True)
        return res, stub_dir
    return _run


def _calls(stub_dir):
    return (stub_dir / "calls.log").read_text().splitlines()


def test_unsigned_author_gets_one_posted_nudge_with_the_filled_in_file(run):
    res, d = run(authors=["newperson"], ledger_at={"m1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    body = (d / "posted.md").read_text()
    assert "<!-- cla-nudge -->" in body
    assert "`@newperson`" in body and "@newperson\n" not in body.replace("- GitHub: @newperson\n", "")
    assert "contributors/newperson.md" in body
    assert f"I have read CLA.md version {CLA_VERSION} and I agree to it." in body
    assert f'git commit -m "Agree to CLA.md version {CLA_VERSION}"' in body
    assert "https://github.com/acme/thing/blob/main/CLA.md" in body
    assert not (d / "patched.md").exists()


def test_api_budget_is_two_reads_plus_one_write_regardless_of_author_count(run):
    many = [f"user{i:03d}" for i in range(250)]
    res, d = run(authors=many, ledger_at={"m1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    calls = _calls(d)
    assert sum("/contents/contributors" in c for c in calls) == 1
    assert sum("/pulls/7/commits" in c for c in calls) == 1
    assert sum("/commits/m1 " in c for c in calls) == 1  # the merge commit's parent check
    assert sum("-X POST" in c for c in calls) == 1
    assert len(calls) == 5  # + the comment lookup: bounded whatever the PR carries
    body = (d / "posted.md").read_text()
    assert "250 distinct commit authors" in body
    assert len(body.encode()) < 4000  # far under GitHub's 65,536-byte comment cap


def test_maintainer_and_bot_only_posts_nothing_and_says_nothing_false(run):
    res, d = run(authors=["eyalgolan", "dependabot[bot]"], ledger_at={"m1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert not (d / "posted.md").exists() and not (d / "patched.md").exists()
    assert "nothing missing" in res.stdout


def test_agent_author_on_the_agents_own_pr_is_exempt(run):
    # no-human is the repo's own agent account: on a PR the agent (or the
    # maintainer) opened, its commits are first-party and nothing is posted.
    res, d = run(authors=["no-human"], ledger_at={"m1": ["README.md"]},
                 env={"PR_AUTHOR": "eyalgolan"})
    assert res.returncode == 0, res.stderr
    assert not (d / "posted.md").exists() and not (d / "patched.md").exists()
    assert "nothing missing" in res.stdout


def test_forged_agent_author_on_a_strangers_pr_blocks_the_resolved_text(run):
    # A stranger's PR carrying a commit authored as no-human: forged author
    # email. The nudge must NOT post the green "has what it needs" text —
    # that is the gate-red/comment-green split the review caught — and must
    # never suggest contributors/no-human.md.
    res, d = run(authors=["no-human"], ledger_at={"m1": ["README.md"]},
                 env={"PR_AUTHOR": "somestranger"})
    assert res.returncode == 0, res.stderr
    body = (d / "posted.md").read_text()
    assert "has what it needs" not in body
    assert "forged author email" in body
    assert "contributors/no-human.md" not in body


def test_forged_agent_author_never_patches_an_earlier_comment_green(run):
    # With an existing nudge comment, the same forged-agent PR must PATCH it
    # to the red explanation, not to the resolved text.
    res, d = run(authors=["no-human"], ledger_at={"m1": ["README.md"]},
                 existing="123", env={"PR_AUTHOR": "somestranger"})
    assert res.returncode == 0, res.stderr
    body = (d / "patched.md").read_text()
    assert "has what it needs" not in body
    assert "forged author email" in body


def test_returning_contributor_whose_file_is_on_main_is_not_nudged(run):
    # the merge commit's tree carries main's contributors/ even when the head
    # branch predates the file — exactly what the CLA ledger job's checkout sees
    res, d = run(authors=["Octocat"], ledger_at={"m1": ["README.md", "octocat.md"], "h1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert not (d / "posted.md").exists()


def test_a_pr_that_deletes_a_ledger_file_is_nudged_like_the_gate_fails(run):
    # the file exists on main but not in the merge result: the gate fails, so
    # the nudge must not say all-clear
    res, d = run(authors=["octocat"], ledger_at={"m1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert "contributors/octocat.md" in (d / "posted.md").read_text()


def test_polls_the_api_for_the_merge_commit_when_the_event_lacks_it(run):
    # a returning contributor whose branch predates her file on main: the
    # merge tree (from the API) has it, the head tree does not — no nudge
    res, d = run(authors=["octocat"], merge_sha="", api_merge_sha="m2",
                 ledger_at={"m2": ["README.md", "octocat.md"], "h1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert any("ref=m2" in c for c in _calls(d))
    assert not (d / "posted.md").exists()


def test_a_stale_payload_merge_commit_is_not_trusted(run):
    # the event's merge_commit_sha can be the merge of the PREVIOUS head: its
    # second parent is not this head, so it is "not computed yet" -> poll,
    # and the polled merge (whose second parent IS the head) has her file
    res, d = run(authors=["alice"], merge_sha="m_old", head_sha="h2", api_merge_sha="m_new",
                 parents={"m_old": "h1", "m_new": "h2"},
                 ledger_at={"m_old": ["README.md"], "m_new": ["README.md", "alice.md"]})
    assert res.returncode == 0, res.stderr
    assert any("ref=m_new" in c for c in _calls(d)) and not any("ref=m_old" in c for c in _calls(d))
    assert not (d / "posted.md").exists()


def test_a_polled_candidate_is_held_to_the_same_parent_check(run):
    # the API keeps answering with the merge of the PREVIOUS head: never used
    res, d = run(authors=["alice"], merge_sha="", api_merge_sha="m_old", head_sha="h2",
                 parents={"m_old": "h1"}, ledger_at={"m_old": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert "nothing posted" in res.stdout
    assert not any("/contents/contributors" in c for c in _calls(d))
    assert sum("--jq .merge_commit_sha" in c for c in _calls(d)) == 6


def test_no_merge_commit_at_all_posts_nothing_and_exits_clean(run):
    # conflicting PR, or GitHub has not computed the merge: never guess from
    # another tree, never make a public statement the gate would not
    res, d = run(authors=["octocat"], merge_sha="", api_merge_sha="", ledger_at={"h1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert "nothing posted" in res.stdout
    assert not (d / "posted.md").exists()
    assert not any("/contents/contributors" in c for c in _calls(d))
    assert sum("--jq .merge_commit_sha" in c for c in _calls(d)) == 6  # bounded poll


def test_a_ledger_read_failure_aborts_before_any_write(run):
    # a 500 / rate limit must NOT read as "empty ledger": that would nudge
    # every author at once, on a blip, with the reason discarded
    res, d = run(authors=["alice"], ledger_fail=True, ledger_at={"m1": ["README.md", "alice.md"]})
    assert res.returncode != 0
    assert "HTTP 500" in res.stderr and "not posting on an unread ledger" in res.stderr
    assert not (d / "posted.md").exists() and not (d / "patched.md").exists()


def test_an_unknown_ref_is_a_failed_read_not_an_empty_ledger(run):
    # GitHub answers 404 for a ref it cannot find too ("No commit found for
    # the ref ..."); that is not "no contributors/ here" and must abort
    res, d = run(authors=["alice"], ledger_fail="badref", ledger_at={"m1": ["README.md", "alice.md"]})
    assert res.returncode != 0
    assert "No commit found" in res.stderr and "not posting on an unread ledger" in res.stderr
    assert not (d / "posted.md").exists()


def test_a_missing_contributors_directory_is_an_empty_ledger(run):
    # 404 on the listing = the tree has no contributors/ at all: nudge
    res, d = run(authors=["alice"], ledger_at={})
    assert res.returncode == 0, res.stderr
    assert "contributors/alice.md" in (d / "posted.md").read_text()


def test_ledger_match_is_whole_name_not_substring(run):
    # `ice` must not be satisfied by `alice.md`; `alice` must be
    res, d = run(authors=["ice", "alice"], ledger_at={"m1": ["README.md", "alice.md"]})
    assert res.returncode == 0, res.stderr
    body = (d / "posted.md").read_text()
    assert "contributors/ice.md" in body and "contributors/alice.md" not in body


def test_a_pr_with_no_commits_says_nothing(run):
    res, d = run(authors=[], ledger_at={"m1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    assert "nothing to say" in res.stdout and not (d / "posted.md").exists()


def test_existing_bot_comment_is_updated_in_place_and_resolved_when_signed(run):
    res, d = run(authors=["octocat"], existing="4242\n", ledger_at={"m1": ["README.md", "octocat.md"]})
    assert res.returncode == 0, res.stderr
    assert not (d / "posted.md").exists()
    patched = (d / "patched.md").read_text()
    assert "<!-- cla-nudge -->" in patched and "has what it needs" in patched
    assert any("-X PATCH repos/acme/thing/issues/comments/4242" in c for c in _calls(d))
    # the lookup asks only for the bot's own comments carrying the marker
    lookup = next(c for c in _calls(d) if "/issues/7/comments" in c)
    assert 'user.login == "github-actions[bot]"' in lookup and "cla-nudge" in lookup


def test_unlinked_email_is_explained_not_guessed(run):
    res, d = run(authors=["UNLINKED-EMAIL"], ledger_at={"m1": ["README.md"]})
    assert res.returncode == 0, res.stderr
    body = (d / "posted.md").read_text()
    assert "not attached to any GitHub account" in body
    assert "contributors/unlinked-email.md" not in body


def test_dry_run_prints_and_never_writes(run):
    res, d = run(authors=["newperson"], ledger_at={"m1": ["README.md"]}, dry=True)
    assert res.returncode == 0, res.stderr
    assert "would post (resolved=0)" in res.stdout and "contributors/newperson.md" in res.stdout
    assert not (d / "posted.md").exists() and not (d / "patched.md").exists()
    assert not any("/issues/7/comments" in c for c in _calls(d))


def test_positive_control_a_failing_commits_call_aborts_rather_than_posting(run, tmp_path):
    # the stub exits 2 on an unknown call; make the commits listing itself fail
    res, d = run(authors=["newperson"], ledger_at={"m1": ["README.md"]},
                 env={"GITHUB_REPOSITORY": "acme/thing", "PR_NUMBER": "7"})
    assert (d / "posted.md").exists()  # the control's precondition: the same setup DOES post
    (d / "posted.md").unlink()
    (d / "commits.txt").unlink()  # next run: `cat` fails -> gh exits non-zero
    res2 = subprocess.run(["bash", str(SCRIPT)], cwd=REPO, capture_output=True, text=True, env={
        **os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(d),
        "GITHUB_REPOSITORY": "acme/thing", "PR_NUMBER": "7", "MERGE_SHA": "m1", "HEAD_SHA": "h1",
        "MAINTAINER": "eyalgolan"})
    assert res2.returncode != 0
    assert not (d / "posted.md").exists()
