"""Every outbound forge write that carries free text — PR titles (create/edit),
PR bodies, draft-PR titles, commit messages built from task titles, and
verification comments — is scrubbed of vendor terms before it leaves the
machine. Operator directive 2026-08-12: a P1 task's own PR title carried
flagged terms verbatim because PR titles built from task titles had no scrub.

Terms are taken from `BANNED_TERMS` programmatically, never spelled as
literals — the repo-wide sweep in `tests/test_no_banned_terms.py` would flag
a literal. Forge calls are exercised through `monkeypatch` on `subprocess.run`
(or the async `_run_cli`), capturing argv — nothing reaches a network.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from pathlib import Path

import pytest

import no_human.vcs as vcs_pkg
from no_human.eval.vendor_terms import BANNED_TERMS, find_banned_terms
from no_human.vcs import comment_poster, github, open_pr
from no_human.vcs.git import GitRepo
from no_human.vcs.outbound_scrub import PLACEHOLDER, ScrubDrainedError, scrub_outbound
from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER, is_agent_comment

GHE = "https://code.example.com/dev/acme-test/pull/7001"
MR = "https://gitlab.acme.net/ci_gate/subgroup/metrics-core/-/merge_requests/7006"


def _term(i: int) -> str:
    return BANNED_TERMS[i % len(BANNED_TERMS)]


def _value_after_field(argv: list[str], prefix: str) -> str:
    """The value of a `-f`/`--field` argument shaped `f"{prefix}{value}"`."""
    for a in argv:
        if a.startswith(prefix):
            return a[len(prefix):]
    raise AssertionError(f"no argv entry starts with {prefix!r}: {argv}")


class _FakeRepo:
    """Duck-types the subset of `GitRepo` `open_pr()` actually uses."""

    def __init__(self, url: str, sha: str = "deadbeef"):
        self._url = url
        self._sha = sha
        self.path = Path("/tmp/fake-repo")

    def remote_url(self) -> str:
        return self._url

    def push(self, branch: str, force_with_lease: bool = False) -> str:
        return self._sha


def _sequence_run(monkeypatch, module, results):
    """Queue (returncode, stdout, stderr) tuples on `module.subprocess.run`;
    record every argv seen."""
    calls: list[list[str]] = []
    queue = list(results)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        rc, out, err = queue.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    """A minimal work repo on a non-protected branch, ready to `commit_all`."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    repo = GitRepo(work, identity_name="no_human", identity_email="no-human@acme.com")
    repo.create_branch("no-human/t1", base="main")
    return repo


# --------------------------------------------------------------------------- #
# scrub_outbound / redact_terms — the choke point itself                      #
# --------------------------------------------------------------------------- #


def test_scrub_outbound_no_hit_is_unchanged(caplog):
    with caplog.at_level(logging.ERROR, "no_human.vcs.scrub"):
        out = scrub_outbound("an ordinary PR title with nothing flagged", "pr_title")
    assert out == "an ordinary PR title with nothing flagged"
    assert not caplog.records


def test_scrub_output_passes_the_detector():
    """Round-trip property: scrubbing any term leaves the detector clean."""
    for t in BANNED_TERMS:
        cleaned = scrub_outbound(f"fix {t} thing", "pr_body")
        assert find_banned_terms(cleaned) == [], (t, cleaned)


def test_placeholder_is_consistent():
    for field, text in [
        ("pr_title", f"NH-1: fix the {_term(0)} bug"),
        ("pr_body", f"See {_term(1)} for details"),
        ("pr_title_retitle", f"Retitle mentioning {_term(2)}"),
        ("pr_comment", f"addressed the {_term(3)} feedback"),
        ("commit_message", f"NH-1: {_term(4)} fix"),
    ]:
        out = scrub_outbound(text, field)
        assert PLACEHOLDER in out, (field, out)
        assert "<redacted>" not in out, (field, out)  # the publish-path marker, never here


def test_embedded_term_is_not_scrubbed():
    """Word-boundary negative: a term embedded in a longer word is untouched —
    the reSTORE-APProval trap the task description names."""
    for t in BANNED_TERMS:
        glued = f"re{t.upper()}-APProval"
        surrounded = f"x{t}y"
        for text in (glued, surrounded):
            out = scrub_outbound(text, "pr_title")
            assert out == text, (t, text, out)


def test_hyphen_glued_term_is_still_scrubbed():
    """Pins that the intended rule-(1) glue case still hits, so the negative
    test above can't be satisfied by disabling matching altogether."""
    for t in BANNED_TERMS:
        text = f"{t}-metrics-core-pipeline"
        out = scrub_outbound(text, "pr_title")
        assert PLACEHOLDER in out, (t, out)
        assert find_banned_terms(out) == [], (t, out)


def test_scrub_emits_loud_event_named_field(caplog):
    fields = ["pr_title", "pr_body", "pr_title_retitle", "pr_comment", "commit_message"]
    for i, field in enumerate(fields):
        caplog.clear()
        term = _term(i)
        with caplog.at_level(logging.ERROR, "no_human.vcs.scrub"):
            scrub_outbound(f"mentions {term} right here", field)
        assert len(caplog.records) == 1, caplog.records
        rec = caplog.records[0]
        assert rec.levelno == logging.ERROR
        assert field in rec.getMessage()
        assert term not in rec.getMessage()


def test_title_that_is_only_a_term_is_refused():
    with pytest.raises(ScrubDrainedError):
        scrub_outbound(_term(0), "pr_title")
    # whitespace padding around a lone term still drains to nothing meaningful
    with pytest.raises(ScrubDrainedError):
        scrub_outbound(f"  {_term(1)}  ", "pr_title")


# --------------------------------------------------------------------------- #
# PR title (create) + PR body + draft-PR title                                #
# --------------------------------------------------------------------------- #


def test_open_pr_scrubs_title_and_body(monkeypatch):
    term_t, term_b = _term(5), _term(6)
    calls = _sequence_run(monkeypatch, github, [(0, "https://github.com/o/r/pull/1", "")])
    repo = _FakeRepo("https://github.com/o/r.git")
    pr = open_pr(repo, "nh-1", f"NH-1: fix the {term_t} bug",
                 f"See {term_b} for details", base="main")
    assert pr.url == "https://github.com/o/r/pull/1"
    argv = calls[0]
    assert argv[:3] == ["gh", "pr", "create"]
    title_val = argv[argv.index("--title") + 1]
    body_val = argv[argv.index("--body") + 1]
    assert PLACEHOLDER in title_val
    assert PLACEHOLDER in body_val
    assert find_banned_terms(title_val) == []
    assert find_banned_terms(body_val) == []
    assert "--draft" in argv  # this create call IS the draft-PR-title path


def test_open_pr_edit_body_scrubbed(monkeypatch):
    term = _term(7)
    calls = _sequence_run(monkeypatch, github, [
        (1, "", "GraphQL: a pull request for branch \"nh-1\" already exists:https://github.com/o/r/pull/2"),
        (0, "https://github.com/o/r/pull/2", ""),  # gh pr list
        (0, "", ""),                                 # gh pr edit --body
    ])
    repo = _FakeRepo("https://github.com/o/r.git")
    pr = open_pr(repo, "nh-1", "t", f"body carrying {term} inline",
                 base="main", update_existing_body=True)
    assert pr.url == "https://github.com/o/r/pull/2"
    edit_call = calls[-1]
    assert edit_call[:3] == ["gh", "pr", "edit"]
    body_val = edit_call[edit_call.index("--body") + 1]
    assert PLACEHOLDER in body_val
    assert find_banned_terms(body_val) == []


# --------------------------------------------------------------------------- #
# PR title (edit) / retitle                                                   #
# --------------------------------------------------------------------------- #


def test_set_pr_title_scrubs(monkeypatch):
    term = _term(8)
    calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.set_pr_title(GHE, f"Retitle mentioning {term}")
    assert res["ok"] is True
    title_val = _value_after_field(calls[0], "title=")
    assert PLACEHOLDER in title_val
    assert find_banned_terms(title_val) == []


def test_gitlab_retitle_scrubs(monkeypatch):
    term = _term(9)
    calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.set_pr_title(MR, f"Retitle mentioning {term}")
    assert res["ok"] is True
    title_val = _value_after_field(calls[0], "title=")
    assert PLACEHOLDER in title_val
    assert find_banned_terms(title_val) == []
    assert calls[0][:3] == ["glab", "api", "--hostname"]


def test_set_pr_title_refused_when_drained(monkeypatch):
    calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.set_pr_title(GHE, _term(0))
    assert res["ok"] is False
    assert not calls  # refused before any forge call


# --------------------------------------------------------------------------- #
# Verification comments (post_to_pr)                                          #
# --------------------------------------------------------------------------- #


def test_post_to_pr_scrubs_comment_body(monkeypatch):
    term = _term(10)
    calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.post_to_pr(GHE, f"finding mentions {term} here")
    assert res["ok"] is True
    body_val = _value_after_field(calls[-1], "body=")
    assert PLACEHOLDER in body_val
    assert find_banned_terms(body_val) == []
    assert body_val.startswith(AGENT_COMMENT_MARKER)  # dedupe marker unbroken
    assert is_agent_comment(body_val)


def test_post_to_pr_refused_when_drained(monkeypatch):
    calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.post_to_pr(GHE, _term(0))
    assert res["ok"] is False
    assert not calls  # refused before any forge call


# --------------------------------------------------------------------------- #
# pr_watcher's own free-text comment writers — CI-gate + reply paths           #
# (blockers/wake.py:251 posts through upsert_agent_comment in production)     #
# --------------------------------------------------------------------------- #


async def test_post_reply_comment_scrubs_message(monkeypatch):
    from no_human.vcs import pr_watcher

    term = _term(11)
    captured = {}

    async def fake_run_cli(cmd):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("host/o/r#5", f"addressed the {term} feedback")
    assert ok is True
    body = captured["cmd"][captured["cmd"].index("--body") + 1]
    assert PLACEHOLDER in body
    assert find_banned_terms(body) == []
    assert is_agent_comment(body)


async def test_post_reply_comment_gitlab_scrubs_message(monkeypatch):
    from no_human.vcs import pr_watcher

    term = _term(12)
    captured = {}

    async def fake_run_cli(cmd):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("42!5", f"addressed the {term} feedback")
    assert ok is True
    body_val = _value_after_field(captured["cmd"], "body=")
    assert PLACEHOLDER in body_val
    assert find_banned_terms(body_val) == []


async def test_post_reply_comment_refuses_when_drained(monkeypatch):
    from no_human.vcs import pr_watcher

    calls = {"n": 0}

    async def fake_run_cli(cmd):
        calls["n"] += 1
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("host/o/r#5", _term(0))
    assert ok is False
    assert calls["n"] == 0  # refused before any forge call


async def test_upsert_agent_comment_scrubs_message(monkeypatch):
    from no_human.vcs import pr_watcher

    term = _term(13)
    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        if " ".join(cmd).endswith("--paginate"):
            return "[]"  # no existing comment -> falls through to POST
        return "{}"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run)
    ok = await pr_watcher.upsert_agent_comment(
        "code.example.com/dev/query-service#7004", f"status update: {term} failing", key="ci_gate")
    assert ok is True
    post_call = next(c for c in calls if "POST" in c)
    body_val = _value_after_field(post_call, "body=")
    assert PLACEHOLDER in body_val
    assert find_banned_terms(body_val) == []


async def test_upsert_agent_comment_refuses_when_drained(monkeypatch):
    from no_human.vcs import pr_watcher

    calls = {"n": 0}

    async def fake_run(cmd):
        calls["n"] += 1
        return "[]"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run)
    ok = await pr_watcher.upsert_agent_comment(
        "code.example.com/dev/query-service#7004", _term(1), key="ci_gate")
    assert ok is False
    assert calls["n"] == 0


# --------------------------------------------------------------------------- #
# Commit messages generated from task titles                                  #
# --------------------------------------------------------------------------- #


def test_commit_message_scrubbed(git_repo):
    term = _term(14)
    (git_repo.path / "feature.py").write_text("y = 2\n")
    git_repo.commit_all(f"NH-1: {term} fix")
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=git_repo.path, capture_output=True, text=True,
    ).stdout
    assert find_banned_terms(msg) == [], msg
    assert PLACEHOLDER in msg, msg


# --------------------------------------------------------------------------- #
# Red-first, end-to-end: one planted term flows scrubbed to all five outputs  #
# --------------------------------------------------------------------------- #


async def test_planted_task_title_flows_scrubbed_to_all_outputs(monkeypatch, git_repo):
    """Plant ONE flagged term in a task-title-shaped string and drive it
    through every outbound surface this task covers. Run on the PRE-CHANGE
    tree first (see PR body) — it fails there because none of these call
    sites scrub at all; it passes here because every one now routes through
    `scrub_outbound`."""
    from no_human.vcs import pr_watcher

    term = _term(15)
    planted = f"NH-1: {term} needs a fix"

    # 1) Commit message generated from the task title. Done FIRST and with no
    # subprocess monkeypatch installed yet: `subprocess` is one shared stdlib
    # module object, so `module.subprocess.run` below patches it globally for
    # every `vcs` submodule that imported it (git.py included) — this real
    # git commit must run before any of that patching starts.
    (git_repo.path / "feature.py").write_text("y = 2\n")
    git_repo.commit_all(planted)
    msg = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=git_repo.path,
                          capture_output=True, text=True).stdout
    assert find_banned_terms(msg) == []

    # 2) PR title (create) + PR body + draft title
    calls = _sequence_run(monkeypatch, github, [(0, "https://github.com/o/r/pull/9", "")])
    repo = _FakeRepo("https://github.com/o/r.git")
    pr = open_pr(repo, "nh-1", planted, planted, base="main")
    assert pr.url == "https://github.com/o/r/pull/9"
    argv = calls[0]
    assert "--draft" in argv
    assert find_banned_terms(argv[argv.index("--title") + 1]) == []
    assert find_banned_terms(argv[argv.index("--body") + 1]) == []

    # 3) PR title (edit/retitle)
    retitle_calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.set_pr_title(GHE, planted)
    assert res["ok"] is True
    assert find_banned_terms(_value_after_field(retitle_calls[0], "title=")) == []

    # 4) Verification comment
    comment_calls = _sequence_run(monkeypatch, comment_poster, [(0, "", "")])
    res = comment_poster.post_to_pr(GHE, planted)
    assert res["ok"] is True
    assert find_banned_terms(_value_after_field(comment_calls[-1], "body=")) == []

    # 5) pr_watcher's CI-gate / reply comment path
    captured = {}

    async def fake_run_cli(cmd):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("host/o/r#5", planted)
    assert ok is True
    assert find_banned_terms(captured["cmd"][captured["cmd"].index("--body") + 1]) == []


# --------------------------------------------------------------------------- #
# Structural discovery: no NEW gh/glab free-text writer can bypass the scrub  #
# --------------------------------------------------------------------------- #
#
# Attempt 1 on this task was failed by review for exactly the bypass this
# section exists to catch structurally instead of by hand-enumeration:
# `pr_watcher.post_reply_comment`/`upsert_agent_comment` posted free-text
# bodies to `gh`/`glab` without ever calling `scrub_outbound`. A hand-written
# list of "the five call sites we remembered to check" has the same blind
# spot every time — a NEW writer added later needs no one to remember it.
# This scans every module in `vcs/` for a `gh`/`glab` argv literal that
# carries a free-text `--title`/`--body`/`--description` (or `-f field=`)
# entry and requires the enclosing function to either call `scrub_outbound`
# itself, or be one of a small, explicitly-audited set of delegates whose
# documented caller does — and that caller is itself checked, not assumed.

_FORGE_ARGV0 = {"gh", "glab"}
_FREE_TEXT_FLAGS = {"--title", "--body", "--description"}
_FREE_TEXT_FIELD_PREFIXES = ("body=", "title=", "description=")

# (file, function) -> (file, function) it delegates its scrubbing to. Every
# entry here is a DELIBERATE, reviewed exception — not a way to silence the
# test — and the delegate side is itself verified to call scrub_outbound.
# The `_create` entries (removed 2026-08-15 with the pr_labels feature) named
# a nested helper that only existed to retry `gh`/`glab` create once without
# labels; github.py/gitlab.py now build the argv directly in open_pr/open_mr.
_DELEGATED_TO = {
    ("github.py", "open_pr"): ("__init__.py", "open_pr"),
    ("gitlab.py", "open_mr"): ("__init__.py", "open_pr"),
}


def _static_prefix(node: ast.AST) -> str | None:
    """Best-effort leading string-literal text of a `Constant` or f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _find_forge_free_text_sinks(path: Path) -> set[str]:
    """Names of functions in *path* that build a `gh`/`glab` argv list
    carrying a free-text title/body/description field."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sinks: set[str] = set()
    func_stack: list[ast.AST] = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            func_stack.append(node)
            self.generic_visit(node)
            func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_List(self, node):
            elts = node.elts
            if elts and _static_prefix(elts[0]) in _FORGE_ARGV0:
                for el in elts[1:]:
                    s = _static_prefix(el)
                    if s and (s in _FREE_TEXT_FLAGS
                              or s.startswith(_FREE_TEXT_FIELD_PREFIXES)):
                        if func_stack:
                            sinks.add(func_stack[-1].name)
            self.generic_visit(node)

    V().visit(tree)
    return sinks


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"no function {name!r} found in {path}")


def test_every_forge_free_text_write_routes_through_scrub_outbound():
    vcs_dir = Path(vcs_pkg.__file__).resolve().parent
    total_sinks = 0
    unaccounted: list[str] = []
    for path in sorted(vcs_dir.glob("*.py")):
        if path.name == "outbound_scrub.py":
            continue
        for fn in sorted(_find_forge_free_text_sinks(path)):
            total_sinks += 1
            if "scrub_outbound(" in _function_source(path, fn):
                continue
            delegate = _DELEGATED_TO.get((path.name, fn))
            if delegate is None:
                unaccounted.append(f"{path.name}::{fn}")
                continue
            delegate_file, delegate_fn = delegate
            delegate_src = _function_source(vcs_dir / delegate_file, delegate_fn)
            if "scrub_outbound(" not in delegate_src:
                unaccounted.append(
                    f"{path.name}::{fn} (delegate {delegate_file}::{delegate_fn} "
                    "does not call scrub_outbound)"
                )
    # Not vacuous: this must actually have found the known writers, or the
    # AST walk itself is broken and the assertion below is trivially true.
    # Was >= 7 until the pr_labels removal (2026-08-15) inlined github.py's
    # nested `_create` helper into `open_pr`: the create-call and the
    # existing-PR body-edit call are now both sinks named "open_pr", so the
    # set of distinct sink NAMES lost one entry even though both call sites
    # (and their scrub_outbound coverage) are still scanned and accounted for.
    assert total_sinks >= 6, (
        f"expected to discover the known gh/glab free-text writers, found "
        f"only {total_sinks} — the discovery scan itself may be broken"
    )
    assert unaccounted == [], (
        "forge free-text writer(s) do not route through scrub_outbound, "
        f"directly or via an audited delegate in _DELEGATED_TO: {unaccounted}"
    )
