"""parse_pr_refs — full URLs + human shorthand (the cross-repo review failure)."""

from no_human.vcs.pr_refs import parse_pr_refs
from no_human.vcs.pr_watcher import parse_pr_url


# The exact description that failed the gate: three refs, two forges, shorthand.
_STRY = (
    "Perform a code review of the three change sets: "
    "(1) code.example.com/dev/acme-test PR #7001, "
    "(2) gitlab.acme.net ci_gate/subgroup/metrics-core MR !7006, "
    "(3) gitlab.acme.net acme-k8s/apps/metrics-core/metrics-core MR !7007."
)


def test_extracts_all_three_shorthand_refs():
    urls = parse_pr_refs(_STRY)
    assert urls == [
        "https://code.example.com/dev/acme-test/pull/7001",
        "https://gitlab.acme.net/ci_gate/subgroup/metrics-core/-/merge_requests/7006",
        "https://gitlab.acme.net/acme-k8s/apps/metrics-core/metrics-core/-/merge_requests/7007",
    ]


def test_each_result_is_parseable_by_the_canonical_grammar():
    for url in parse_pr_refs(_STRY):
        assert parse_pr_url(url) is not None, url


def test_full_urls_still_work_and_dedupe_against_shorthand():
    text = (
        "https://github.com/o/r/pull/5 and also github.com/o/r PR #5 "
        "plus https://gitlab.com/g/p/-/merge_requests/9"
    )
    urls = parse_pr_refs(text)
    assert "https://github.com/o/r/pull/5" in urls
    assert "https://gitlab.com/g/p/-/merge_requests/9" in urls
    # the shorthand for the same PR #5 normalises to the same URL → not duplicated
    assert urls.count("https://github.com/o/r/pull/5") == 1


def test_first_seen_order_across_mixed_full_and_shorthand():
    # A shorthand ref mentioned BEFORE a full URL must stay first: callers use
    # parse_pr_refs(...)[0] as the primary anchor (orchestrator
    # ._run_code_review's single-URL comment/UI fields). Gathering every full
    # URL ahead of every shorthand ref hoisted a later, secondary PR to the
    # front; the result must follow the order the refs appear in the text.
    text = (
        "Primary: gitlab.acme.net grp/metrics MR !7006, "
        "context in https://github.com/o/r/pull/3"
    )
    assert parse_pr_refs(text) == [
        "https://gitlab.acme.net/grp/metrics/-/merge_requests/7006",
        "https://github.com/o/r/pull/3",
    ]


def test_no_refs_returns_empty():
    assert parse_pr_refs("just review the code please") == []
    assert parse_pr_refs("") == []


def test_ghe_diff_fetch_forwards_hostname(monkeypatch):
    """The GHE PR-diff fetch must pass --hostname; without it a code.example.com
    PR is queried against github.com and 404s (the acme-test empty-diff bug)."""
    import subprocess
    from no_human.core.orchestrator import Orchestrator

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        class R:
            returncode = 0
            stdout = "diff --git a/x b/x\n"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    Orchestrator._fetch_diff_via_gh_api(
        "https://code.example.com/dev/acme-test/pull/7001", "7001", ".")
    assert "--hostname" in seen["argv"] and "code.example.com" in seen["argv"]
    assert "repos/dev/acme-test/pulls/7001" in seen["argv"]

    # github.com must NOT get a --hostname arg (default host).
    Orchestrator._fetch_diff_via_gh_api("https://github.com/o/r/pull/5", "5", ".")
    assert "--hostname" not in seen["argv"]
