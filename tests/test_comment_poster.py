"""Cross-VCS review posting: each finding is attributed to the change set that
owns its file, so it lands on the RIGHT PR/MR (the cross-repo case: GHE + 2
GitLab)."""

import ast
import json
import pathlib
import re
import subprocess

import pytest

from no_human.vcs import comment_poster
from no_human.vcs.comment_poster import files_in_diff, pick_pr_for_file
from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER, is_agent_comment

GHE = "https://code.example.com/dev/acme-test/pull/7001"
MR7006 = "https://gitlab.acme.net/ci_gate/subgroup/metrics-core/-/merge_requests/7006"
MR7007 = "https://gitlab.acme.net/acme-k8s/apps/metrics-core/metrics-core/-/merge_requests/7007"

_PR_FILES = {
    GHE: ["acme-sample-test/src/test/java/com/acme/sample/props/SamplePropsIntegrationIT.java"],
    MR7006: ["workload/backend-service.yaml", "workload/overlays/sample-sync-service/ci_gate/values.yaml.gotmpl"],
    MR7007: ["apps/metrics-core/deploy.yaml"],
}


def test_files_in_diff_extracts_both_grammars():
    diff = (
        "diff --git a/workload/backend-service.yaml b/workload/backend-service.yaml\n"
        "--- a/workload/backend-service.yaml\n+++ b/workload/backend-service.yaml\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n"
    )
    files = files_in_diff(diff)
    assert "workload/backend-service.yaml" in files
    assert "new.txt" in files
    assert "/dev/null" not in files


def test_gitlab_finding_routes_to_its_own_MR_not_the_ghe_pr():
    # backend-service.yaml lives in MR !7006, NOT the acme-test GHE PR — this is the exact
    # bug the user hit (a GitLab finding posted to the GitHub PR).
    assert pick_pr_for_file("workload/backend-service.yaml", _PR_FILES, fallback=GHE) == MR7006
    assert pick_pr_for_file(
        "workload/overlays/sample-sync-service/ci_gate/values.yaml.gotmpl",
        _PR_FILES, fallback=GHE,
    ) == MR7006


def test_ghe_finding_routes_to_the_ghe_pr():
    assert pick_pr_for_file(
        "acme-sample-test/src/test/java/com/acme/sample/props/SamplePropsIntegrationIT.java",
        _PR_FILES, fallback=MR7006,
    ) == GHE


def test_trailing_segment_match_handles_partial_paths():
    # reviewer cited just "backend-service.yaml"; diff has the full path.
    assert pick_pr_for_file("backend-service.yaml", _PR_FILES, fallback=GHE) == MR7006


def test_unknown_file_falls_back_to_the_anchor_pr():
    assert pick_pr_for_file("nowhere/x.go", _PR_FILES, fallback=GHE) == GHE
    assert pick_pr_for_file("", _PR_FILES, fallback=GHE) == GHE


# ---------------------------------------------------------------------------
# Every posted body carries a self-marker (R18, 2026-08-10)
# ---------------------------------------------------------------------------
# The wake watcher filters the product's own PR comments by the `<!-- no_human`
# marker family (pr_watcher.is_agent_comment) — comments are posted under the
# operator's own gh login, so author identity cannot do it. An UNMARKED
# self-comment therefore reads as human feedback and re-wakes the very task
# that posted it. Two product paths did exactly that, both through post_to_pr:
# the abandoned-draft note (orchestrator) and approved draft review comments
# (orchestrator + api/app.py). These tests drive every branch of post_to_pr and
# assert the body that reaches the forge is marked.


class _Recorder:
    """Stands in for subprocess.run: answers the reads, records the writes."""

    def __init__(self, inline_ok=True):
        self.bodies: list[str] = []
        self.inline_ok = inline_ok

    def __call__(self, argv, **kw):
        joined = " ".join(argv)
        if "--jq" in argv:  # _head_sha
            return subprocess.CompletedProcess(argv, 0, "deadbeef", "")
        if "merge_requests/7006" in joined and "-X" not in argv:  # _gitlab_diff_refs
            refs = {"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(refs), "")
        if kw.get("input"):  # GitLab inline discussion (JSON payload)
            self.bodies.append(json.loads(kw["input"])["body"])
            return subprocess.CompletedProcess(argv, 0 if self.inline_ok else 1, "", "no")
        for a in argv:
            if a.startswith("body="):
                self.bodies.append(a[len("body="):])
        rc = 0
        if "pulls/" in joined and "comments" in joined and not self.inline_ok:
            rc = 1  # GitHub inline rejected → issue-comment fallback
        return subprocess.CompletedProcess(argv, rc, "", "no")


@pytest.mark.parametrize("url,file,line,inline_ok,expect_mode", [
    (GHE, None, None, True, "issue_comment"),          # GitHub plain
    (GHE, "src/a.py", 12, True, "inline"),             # GitHub inline
    (GHE, "src/a.py", 12, False, "issue_comment"),     # GitHub inline → fallback
    (MR7006, None, None, True, "mr_note"),             # GitLab note
    (MR7006, "workload/backend-service.yaml", 3, True, "inline"),   # GitLab inline
    (MR7006, "workload/backend-service.yaml", 3, False, "mr_note"),  # GitLab fallback
])
def test_every_post_to_pr_branch_stamps_a_self_marker(
        monkeypatch, url, file, line, inline_ok, expect_mode):
    rec = _Recorder(inline_ok=inline_ok)
    monkeypatch.setattr(comment_poster.subprocess, "run", rec)
    res = comment_poster.post_to_pr(url, "Abandoned by no_human.", file, line)
    assert res["mode"] == expect_mode, res
    assert rec.bodies, "nothing was posted"
    for body in rec.bodies:
        assert is_agent_comment(body), f"unmarked self-comment posted: {body!r}"


def test_a_body_that_already_carries_a_family_marker_is_not_stamped_twice(monkeypatch):
    """The verification-receipts comment brings its own `<!-- no_human:...`
    marker (orchestrator.VERIFICATION_COMMENT_MARKER). Stamping a second one
    would put two invisible markers on one comment for no gain."""
    rec = _Recorder()
    monkeypatch.setattr(comment_poster.subprocess, "run", rec)
    comment_poster.post_to_pr(GHE, "<!-- no_human:verification-receipts -->\nhi")
    assert rec.bodies[0].count("<!-- no_human") == 1, rec.bodies[0]
    assert AGENT_COMMENT_MARKER not in rec.bodies[0]
    assert is_agent_comment(rec.bodies[0]), rec.bodies[0]


def test_an_already_marked_body_keeps_its_marker_at_column_zero(monkeypatch):
    """`is_agent_comment` requires the marker to OPEN a line, so the location
    prefix must go after it. Prefixing an already-marked body un-marks our own
    comment and it reads back as human feedback — R18, from the other end."""
    rec = _Recorder(inline_ok=False)
    monkeypatch.setattr(comment_poster.subprocess, "run", rec)
    comment_poster.post_to_pr(
        GHE, "<!-- no_human:verification-receipts -->\nhi", "src/a.py", 12)
    assert rec.bodies[-1] == (
        "<!-- no_human:verification-receipts -->\n`src/a.py:12` — hi"), rec.bodies[-1]
    assert is_agent_comment(rec.bodies[-1])


def test_the_location_prefix_still_leads_the_visible_text(monkeypatch):
    """The marker is invisible, so it goes first; `file:line — ` stays the first
    thing a human reads."""
    rec = _Recorder(inline_ok=False)
    monkeypatch.setattr(comment_poster.subprocess, "run", rec)
    comment_poster.post_to_pr(GHE, "finding", "src/a.py", 12)
    assert rec.bodies[-1] == f"{AGENT_COMMENT_MARKER}\n`src/a.py:12` — finding"


# ---------------------------------------------------------------------------
# close_pr — the abandon-path close primitive (refile of 1dfed378)
# ---------------------------------------------------------------------------
# An abandoned draft used to be retitled and left OPEN with a note attached.
# The note channel is gone (R18/b1fd13ca comment-resume hazard); this is the
# primitive that actually closes the PR — a state transition on one field,
# never a comment.


def test_close_pr_github_patches_state_closed(monkeypatch):
    calls = []

    def fake_run(argv, timeout=15):
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(comment_poster, "_run", fake_run)
    res = comment_poster.close_pr("https://github.com/o/r/pull/106")
    assert calls == [["gh", "api", "--hostname", "github.com", "-X", "PATCH",
                       "repos/o/r/pulls/106", "-f", "state=closed"]], calls
    assert res == {"ok": True, "error": ""}


def test_close_pr_gitlab_uses_state_event_close(monkeypatch):
    calls = []

    def fake_run(argv, timeout=15):
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(comment_poster, "_run", fake_run)
    res = comment_poster.close_pr(MR7006)
    assert calls == [["glab", "api", "--hostname", "gitlab.acme.net", "-X", "PUT",
                       "projects/ci_gate%2Fsubgroup%2Fmetrics-core/merge_requests/7006",
                       "-f", "state_event=close"]], calls
    assert res == {"ok": True, "error": ""}


def test_close_pr_never_posts_a_comment(monkeypatch):
    calls = []

    def fake_run(argv, timeout=15):
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(comment_poster, "_run", fake_run)
    comment_poster.close_pr(GHE)
    comment_poster.close_pr(MR7006)
    joined = [" ".join(a) for a in calls]
    assert calls, "close_pr never shelled out"
    assert not any("comments" in j or "notes" in j for j in joined), calls


def test_close_pr_on_unparseable_url_fails_closed(monkeypatch):
    def fake_run(argv, timeout=15):
        raise AssertionError("close_pr shelled out for an unparseable URL")

    monkeypatch.setattr(comment_poster, "_run", fake_run)
    res = comment_poster.close_pr("not-a-url")
    assert res == {"ok": False, "error": "unparseable PR URL: not-a-url"}


# ---------------------------------------------------------------------------
# The guard over the guard: RECOMPUTED, never hand-listed
# ---------------------------------------------------------------------------
# A first version of this section excluded the two stampers by BASENAME and
# matched endpoints with a tight regex over double-quoted argv. Both failed
# open. Basename exclusion made a SIXTH send point added inside
# `comment_poster.py` — precisely where one would be added — invisible; the
# regex was defeated by four ordinary spellings (implicit-concat f-strings, a
# variable last path segment, single-quoted argv, and `gh pr review
# --comment`). So the matching is done over the AST — `ast.unparse` normalises
# quoting and folds implicit concatenation for free — and it is LOOSE by
# design, with the few safe sites enumerated and COUNTED below rather than
# excluded by a pattern that can silently drop a real positive.

#: Any path fragment of a forge endpoint that creates or carries a PR/MR
#: comment. Deliberately loose (`pr.{0,4}comment` catches `["gh","pr","comment"]`
#: in either quoting); the write/read split below is what keeps it precise.
_ENDPOINT_FRAGMENTS = re.compile(
    r"/comments|/notes|/discussions|pr.{0,4}comment|pr.{0,4}review", re.I)
#: A comment body being handed to a forge — `-f body=…`, `--field body=…`,
#: `--body …`, or a JSON payload key. A write is a call that carries one.
_BODY_ARG = re.compile(r"""body\s*=|['"]--body['"]""")
#: Anything that TALKS to a forge — a shelled-out CLI however the argv is
#: quoted, or an in-process HTTP client. The CLI-only version of this was the
#: guard's premise walking through its own front door: `httpx` is already a
#: dependency (pyproject.toml), so a module posting a comment with
#: `httpx.post(".../issues/1/comments", json={"body": ...})` matched nothing
#: and was skipped before any endpoint or body check ran.
#:
#: BEST-EFFORT TRIPWIRE — NOT EXHAUSTIVE, AND DELIBERATELY NOT WIDENED FURTHER.
#: This is a matcher over SOURCE TEXT, so it cannot enumerate every transport,
#: and three rounds of widening it each produced five more evasions. Known and
#: accepted misses, stated so the next reader does not mistake this for proof:
#:   * shell strings — `os.system("gh api …")`, `os.popen`,
#:     `asyncio.create_subprocess_shell`, and anything else that reaches a CLI
#:     without naming `subprocess.` or quoting `gh`/`glab`/`curl` in the argv;
#:   * HTTP CLIENT OBJECTS — `S = httpx.Client(); S.post(url, json=…)`. The
#:     construction matches, the send does not. This is the idiomatic form of
#:     the very library the last widening was written for, and it is already
#:     used at `src/no_human/context/teams.py:65`;
#:   * INDIRECTED ENDPOINTS — a URL or path built from a module constant, a
#:     config value, or a variable, so no comment-endpoint fragment appears in
#:     the call's source at all.
#: What it IS: a cheap tripwire over the shapes an unmarked poster is actually
#: written in, so adding one the obvious way turns a test red. What makes the
#: invariant hold today is enumeration, not this regex: sweeping `/comments`,
#: `/notes`, `/discussions`, `gh pr comment` and `gh pr review` across `src/`,
#: `scripts/` and `e2e/` finds comment WRITES in exactly two files —
#: `pr_watcher.py` and `comment_poster.py` — both of which stamp, and both of
#: which are checked internally by the two tests below. Growing this regex to
#: chase a hypothetical third poster has cost a review cycle every time; if one
#: ever appears, the fix is to make it stamp, not to teach the matcher a new
#: transport.
_FORGE_SHELL = re.compile(
    r"""['"](gh|glab|curl)['"]|subprocess\.|\b(httpx|requests|aiohttp|urllib)\b""")
#: Proof that the body reaching a call was marked.
_STAMP_EVIDENCE = re.compile(r"_stamped\(|AGENT_COMMENT_MARKER")

_SRC_ROOT = pathlib.Path(comment_poster.__file__).parents[3]
#: Every tree that can ship code posting to a forge, mapped to the fewest `.py`
#: files it may contribute. `src/no_human` is the live one; the rest are latent
#: (clean today) but are scanned so a helper script cannot become the unmarked
#: path. Both halves are load-bearing: `Path("/typo").rglob("*.py")` returns []
#: without raising, so a misspelt root scanned NOTHING and reported clean, and a
#: single `scanned > 100` over all roots together could not see it because
#: src/no_human alone contributes 175. A floor rather than an exact count, so
#: adding a module is not a test change. `integrations/` holds no Python at all
#: today — it is kept at 0 so the root is still checked to EXIST and anything
#: added there is scanned from the first file.
#: `eval/` (123 files) and `packaging/` (1) are NOT scanned. That is a real gap
#: and it is left alone deliberately: widening the guard's reach is its own
#: change with its own allowlist review, not a rider on this one.
_SCAN_ROOTS = {"src/no_human": 150, "scripts": 5, "e2e": 10, "integrations": 0}
#: The two modules that stamp. Excluded from the "third module" scan by
#: RESOLVED PATH, not basename — a same-named file elsewhere is not exempt —
#: and then checked internally by the two tests below.
_MARKER_STAMPERS = {
    pathlib.Path(comment_poster.__file__).resolve(),
    (pathlib.Path(comment_poster.__file__).parent / "pr_watcher.py").resolve(),
}
#: `_post_gitlab_inline` takes an ALREADY-stamped body and forwards it into a
#: JSON payload, so the stamp is not visible at its own send point. Excluded by
#: name — and the exclusion is then discharged by asserting every call to it
#: passes `_stamped(`, so this is an enumeration, not a hole.
_BODY_FORWARDERS = {"_post_gitlab_inline"}
#: The only sites outside the two stampers that the (deliberately loose) matcher
#: above flags, and why each is safe. `gh pr create --body` / `gh pr edit --body`
#: write the PR DESCRIPTION, not a comment: no comment endpoint ever returns it,
#: so it cannot reach `is_agent_comment` and cannot wake anything. Listed by
#: module:function -> HOW MANY calls there, so an exclusion cannot silently grow
#: over a real positive: a set of names excused every matching call in the
#: function forever, and an unmarked `gh api .../issues/1/comments` planted
#: inside open_pr left the guard green. The function named is the INNERMOST one:
#: `open_pr` builds its `gh pr create` argv directly (the nested `_create`
#: helper it used to retry once without labels was removed with pr_labels,
#: 2026-08-15) and also its own `gh pr edit` body-refresh call, so both of
#: open_pr's non-comment writes now count under the one name. Naming the outer
#: function was one of the ways a planted call got laundered. An entry here can
#: only ever excuse a call matched by its BODY argument — a call that names a
#: comment ENDPOINT is never excusable, whatever function it sits in.
_SAFE_NON_COMMENT_WRITES = {"vcs/github.py:open_pr": 2}


def _forge_calls(path: pathlib.Path):
    """``(lineno, enclosing_func, source)`` for every forge send point in *path*
    that touches a comment endpoint or carries a comment body."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # INNERMOST function wins. `ast.walk` is breadth-first, so an outer function
    # claims its whole subtree before a nested one is reached; `setdefault` left
    # the OUTER owning a call inside a nested def, which laundered the nested
    # call through the outer function's `_stamped(` evidence and through an
    # allowlist keyed on the outer name. Plain assignment lets the nested def,
    # visited later, take it back.
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner[n] = fn
    out, matched = [], []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        src = ast.unparse(n)
        if not _FORGE_SHELL.search(src):
            continue
        if not (_ENDPOINT_FRAGMENTS.search(src) or _BODY_ARG.search(src)):
            continue
        fn = owner.get(n)
        out.append((n.lineno, fn.name if fn else "<module>", src))
        matched.append(n)
    # A send point does not have to fit in one Call node: `argv = [...]; argv +=
    # [...]; _run(argv)` unparses at the call site as `_run(argv)`, with no forge
    # token, no endpoint and no body — three statements is all it takes to become
    # invisible. So a function that mentions ALL THREE and exposes none of them
    # at a single call is reported at the function level. The three-way
    # requirement (versus forge + endpoint-OR-body per call) is what keeps a
    # whole-function scan from crying wolf.
    # ponytail: O(functions x nodes) per file; files are ~1k lines, so it is
    # microseconds. Index the calls by enclosing function if that ever changes.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        inside = {id(n) for n in ast.walk(fn)}
        if any(id(c) in inside for c in matched):
            continue
        src = ast.unparse(fn)
        if (_FORGE_SHELL.search(src) and _ENDPOINT_FRAGMENTS.search(src)
                and _BODY_ARG.search(src)):
            out.append((fn.lineno, fn.name, src))
    return out


def test_no_module_outside_the_two_stampers_posts_a_pr_comment():
    """A third module shelling a comment endpoint is unmarked by construction.

    Best-effort: this catches the shapes an unmarked poster is written in, not
    every transport — see `_FORGE_SHELL` for what it knowingly misses. Passing
    is evidence, not proof."""
    scanned, excluded = {}, {}
    offenders = []
    for root in _SCAN_ROOTS:
        d = _SRC_ROOT / root
        if _SCAN_ROOTS[root] == 0 and not d.is_dir():
            # A floor of 0 already declares "may contain no scannable files".
            # `integrations/` ships nothing (no pinned .py; a JS Forge app) and
            # the public export drops the directory outright, so on that tree
            # existence itself is the wrong assertion — but a root we EXPECT
            # files under must still exist, or the scan went dark (2026-08-17,
            # first public CI run). Recorded as 0 SCANNED files — the floor
            # check below still runs for it, and `excluded` stays a
            # {function-key: count} map the final allowlist equality compares
            # (a reason-string entry here broke that equality AND the floor
            # loop KeyError'd on the missing root: the absent-dir branch was
            # only ever exercised on the tree where the dir exists — caught by
            # the second public CI run, 2026-08-17).
            scanned[root] = 0
            continue
        assert d.is_dir(), f"scan root does not exist, so it scanned nothing: {d}"
        scanned[root] = 0
        for p in sorted(d.rglob("*.py")):
            if p.resolve() in _MARKER_STAMPERS:
                continue
            scanned[root] += 1
            rel = str(p.relative_to(_SRC_ROOT / "src/no_human")) \
                if root == "src/no_human" else str(p.relative_to(_SRC_ROOT))
            for lineno, fn, src in _forge_calls(p):
                key = f"{rel}:{fn}"
                # An allowlisted FUNCTION excuses a PR-description write, never a
                # comment endpoint: the exclusion exists because `gh pr create
                # --body` writes the PR body, which no comment endpoint returns.
                # A call naming /comments in the same function is a different
                # thing and is never excusable.
                if key in _SAFE_NON_COMMENT_WRITES and not _ENDPOINT_FRAGMENTS.search(src):
                    excluded[key] = excluded.get(key, 0) + 1
                    continue
                offenders.append(f"{rel}:{lineno} ({fn}) {src[:90]}")
    for root, floor in _SCAN_ROOTS.items():
        assert scanned[root] >= floor, (
            f"{root} contributed {scanned[root]} files, expected at least "
            f"{floor} — moved, renamed, or no longer where the guard looks")
    assert not offenders, (
        "these modules build a comment-posting request themselves and so cannot "
        f"be stamped by the two posters: {offenders}")
    assert excluded == _SAFE_NON_COMMENT_WRITES, (
        f"the allowlist no longer matches what the scan finds: {excluded}")


def test_every_posting_call_in_the_stampers_carries_marker_evidence():
    """Inside the two stampers, every call that hands a BODY to a forge must
    show a stamp — in the call itself, or in the function that built the body."""
    unstamped, no_body = [], set()
    for path in sorted(_MARKER_STAMPERS):
        text = path.read_text(encoding="utf-8")
        funcs = {f.name: ast.unparse(f) for f in ast.walk(ast.parse(text))
                 if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for lineno, fn, src in _forge_calls(path):
            if not _BODY_ARG.search(src):
                no_body.add(f"{path.name}:{fn}")
                continue
            if not _STAMP_EVIDENCE.search(src) and not _STAMP_EVIDENCE.search(funcs.get(fn, "")):
                unstamped.append(f"{path.name}:{lineno} ({fn}) {src[:90]}")
    assert not unstamped, f"forge writes with no marker anywhere in scope: {unstamped}"
    # Everything the write/read split discarded, ENUMERATED — a new site
    # appearing here fails this test rather than slipping through unexamined.
    # Three are comment LISTINGS (they post nothing); `_post_gitlab_inline`
    # sends its body in a JSON payload and is covered by the AST body test
    # below.
    assert no_body == {
        "comment_poster.py:_post_gitlab_inline",
        "pr_watcher.py:fetch_github_pr_comments",
        "pr_watcher.py:fetch_gitlab_mr_comments",
        "pr_watcher.py:upsert_agent_comment",
    }, sorted(no_body)


def test_every_body_comment_poster_builds_for_a_forge_is_stamped():
    """The AST recompute that kills the basename hole: whatever a future send
    point inside this module is spelled like, the body it binds must be
    `_stamped(...)`. Covers the JSON-payload path the argv scan cannot see."""
    tree = ast.parse(pathlib.Path(comment_poster.__file__).read_text(encoding="utf-8"))
    unstamped, forwarded = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            bound = None
            if isinstance(node, ast.JoinedStr):
                src = ast.unparse(node)
                if re.match(r"^f?['\"]body=", src):
                    bound = src
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "body":
                        bound = f"body={ast.unparse(v)}"
            if bound is None:
                continue
            if fn.name in _BODY_FORWARDERS:
                forwarded.append(f"{fn.name}: {bound}")
            elif "_stamped(" not in bound:
                unstamped.append(f"{fn.name}: {bound}")
    assert not unstamped, f"unstamped comment bodies built for a forge: {unstamped}"
    # Discharge the forwarder exclusion: it is safe ONLY because every call
    # site stamps. Recomputed, so deleting the stamp at line 101 fails here.
    calls = [ast.unparse(c) for c in ast.walk(tree)
             if isinstance(c, ast.Call) and getattr(c.func, "id", "") in _BODY_FORWARDERS]
    assert len(forwarded) == 1, f"body-forwarder bodies excluded: {forwarded}"
    assert calls, "the forwarder allowlist names a function nothing calls"
    for c in calls:
        assert "_stamped(" in c, f"forwarder called with an unstamped body: {c}"


# ---------------------------------------------------------------------------
# The matcher's own evasion corpus
# ---------------------------------------------------------------------------
# Every entry here is a way a real module could post an unmarked PR comment
# that the guard above once walked straight past. They are run against
# `_forge_calls` directly rather than by planting a file in the tree, so a
# regression is a red test and not a red repo.

def _flags(tmp_path, source: str) -> list:
    p = tmp_path / "candidate.py"
    p.write_text(source)
    return _forge_calls(p)


def test_the_matcher_sees_an_in_process_http_post(tmp_path):
    """`httpx` is already a dependency, so the CLI-only matcher let a module
    post a comment with no shell-out at all and stay green."""
    assert _flags(tmp_path, (
        "import httpx\n"
        "def post(repo, num, body):\n"
        "    httpx.post(f'https://api.github.com/repos/{repo}/issues/{num}/comments',\n"
        "               json={'body': body})\n"
    )), "an httpx comment POST is invisible to the guard"


def test_the_matcher_does_not_let_an_outer_function_launder_a_nested_send(tmp_path):
    """`owner.setdefault` under a breadth-first walk gave a call inside a nested
    def to the OUTER function, whose own source contains `_stamped(` — so a
    nested, unmarked send point read as marked, and an allowlist keyed on the
    outer name excused it."""
    flagged = _flags(tmp_path, (
        "import subprocess\n"
        "def post_to_pr(url, body):\n"
        "    def _sneak():\n"
        "        subprocess.run(['gh', 'api', '-X', 'POST',\n"
        "                        'repos/o/r/issues/1/comments',\n"
        "                        '-f', 'body=unmarked'])\n"
        "    _sneak()\n"
        "    return _stamped(body)\n"
    ))
    assert flagged, "a nested send point is invisible"
    assert [fn for _, fn, _ in flagged] == ["_sneak"], flagged


def test_the_matcher_sees_an_argv_assembled_across_statements(tmp_path):
    """`_forge_calls` matches ONE `ast.Call` node, so an argv built over three
    statements unparses at the call site as `_run(argv)` — no forge token, no
    endpoint, no body. The function-level pass is what catches it."""
    flagged = _flags(tmp_path, (
        "import subprocess\n"
        "def _run(argv):\n"
        "    return subprocess.run(argv)\n"
        "def post(slug, number, body):\n"
        "    argv = ['gh', 'api', '-X', 'POST']\n"
        "    argv += [f'repos/{slug}/issues/{number}/comments']\n"
        "    argv.extend(['-f', f'body={body}'])\n"
        "    _run(argv)\n"
    ))
    assert [fn for _, fn, _ in flagged] == ["post"], flagged


def test_the_function_level_pass_does_not_fire_on_an_ordinary_helper(tmp_path):
    """It demands a forge token AND an endpoint AND a body in the same function,
    and stays quiet where a per-call match already reported the site — a scan
    that flags everything gets an allowlist that excuses everything."""
    listing = _flags(tmp_path, (
        "import subprocess\n"
        "def list_comments(slug, number):\n"
        "    return subprocess.run(['gh', 'api', f'repos/{slug}/issues/{number}/comments'])\n"
    ))
    # The per-call rule reports a LISTING (endpoint, no body) and the existing
    # write/read split enumerates it; the function-level pass must not add a
    # second, unattributable entry on top.
    assert len(listing) == 1, listing
    assert not _BODY_ARG.search(listing[0][2]), listing
    once = _flags(tmp_path, (
        "import subprocess\n"
        "def post(slug, number, body):\n"
        "    subprocess.run(['gh', 'api', '-X', 'POST',\n"
        "                    f'repos/{slug}/issues/{number}/comments',\n"
        "                    '-f', f'body={body}'])\n"
    ))
    assert len(once) == 1, f"a single send point reported twice: {once}"
