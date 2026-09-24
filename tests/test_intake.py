"""Intake: source detection + pure normalizers (GitHub/GitLab, verified on fixtures)."""

import pytest

from no_human.intake import is_plain_text_task, parse_source
from no_human.intake.base import clean_html, dv, html_list_items
from no_human.intake.github_issues import GitHubAdapter, parse_issue_url
from no_human.intake.gitlab_issues import GitLabAdapter


@pytest.mark.parametrize("text,kind", [
    ("https://code.example.com/org/repo/issues/12", "github"),
    ("https://github.com/o/r/issues/9", "github"),
    ("https://gitlab.acme.net/g/s/p/-/issues/3", "gitlab"),
    ("just do the thing", "freeform"),
])
def test_parse_source(text, kind):
    assert parse_source(text).kind == kind


def test_html_helpers():
    assert clean_html("<p>a &amp; b</p>") == "a & b"
    items = html_list_items("<ol><li>one</li><li>two &mdash; 2</li></ol>")
    assert items == ["one", "two — 2"]
    # plain numbered text fallback
    assert html_list_items("1. first\n2. second") == ["first", "second"]
    assert dv({"value": "2", "display_value": "Work in progress"}) == "Work in progress"
    assert dv("plain") == "plain"
    assert dv(None) == ""


def test_github_normalize():
    raw = {
        "number": 12, "title": "Fix the parser",
        "body": "Background.\n\n- [ ] handles empty input\n- [x] covers unicode\n",
        "html_url": "https://code.example.com/o/r/issues/12",
        "labels": [{"name": "bug"}], "state": "open",
        "_ref": {"host": "code.example.com", "owner": "o", "repo": "r"},
    }
    task = GitHubAdapter().normalize(raw)
    assert task.source == "github"
    assert task.external_id == "o/r#12"
    assert task.acceptance_criteria == ["handles empty input", "covers unicode"]
    assert task.context["github"]["labels"] == ["bug"]


def test_github_url_parse():
    ref = parse_issue_url("https://code.example.com/myorg/myrepo/issues/99")
    assert (ref.host, ref.owner, ref.repo, ref.number) == (
        "code.example.com", "myorg", "myrepo", "99")


def test_gitlab_normalize():
    raw = {
        "iid": 3, "title": "Add retry", "description": "do it\n- [ ] retries 3x\n",
        "web_url": "https://gitlab.acme.net/g/s/p/-/issues/3",
        "labels": ["enhancement"], "state": "opened",
        "_ref": {"host": "gitlab.acme.net", "project_path": "g/s/p"},
    }
    task = GitLabAdapter().normalize(raw)
    assert task.source == "gitlab"
    assert task.external_id == "g/s/p#3"
    assert task.acceptance_criteria == ["retries 3x"]


@pytest.mark.parametrize(("adapter", "raw", "expected"), [
    (GitHubAdapter(), {
        "number": 17, "title": "Plain criteria",
        "body": "## Acceptance criteria\n\n- parses plain bullets\n- stops at next heading\n\n## Notes\n- not a criterion",
        "_ref": {"host": "github.com", "owner": "o", "repo": "r"},
    }, ["parses plain bullets", "stops at next heading"]),
    (GitLabAdapter(), {
        "iid": 18, "title": "Plain criteria",
        "description": "## Acceptance criteria\n- parses plain bullets\n- keeps order",
        "_ref": {"host": "gitlab.com", "project_path": "g/p"},
    }, ["parses plain bullets", "keeps order"]),
])
def test_issue_adapters_extract_plain_bullets_from_acceptance_criteria(
        adapter, raw, expected):
    task = adapter.normalize(raw)
    assert task.acceptance_criteria == expected


@pytest.mark.parametrize(("adapter", "raw", "issue_name"), [
    (GitHubAdapter(), {
        "number": 19, "body": "## Acceptance criteria\n\nText only.",
        "_ref": {"host": "github.com", "owner": "o", "repo": "r"},
    }, "GitHub issue o/r#19"),
    (GitLabAdapter(), {
        "iid": 20, "description": "## Acceptance criteria\n\nText only.",
        "_ref": {"host": "gitlab.com", "project_path": "g/p"},
    }, "GitLab issue g/p#20"),
])
def test_issue_adapters_warn_for_empty_acceptance_criteria_heading(
        adapter, raw, issue_name, caplog):
    with caplog.at_level("WARNING", logger="no_human.intake.criteria"):
        assert adapter.normalize(raw).acceptance_criteria == []
    assert issue_name in caplog.text
    assert "--criteria" in caplog.text


@pytest.mark.parametrize(("adapter", "raw"), [
    (GitHubAdapter(), {
        "number": 21, "body": "No criteria here.",
        "_ref": {"host": "github.com", "owner": "o", "repo": "r"},
    }),
    (GitLabAdapter(), {
        "iid": 22, "description": "No criteria here.",
        "_ref": {"host": "gitlab.com", "project_path": "g/p"},
    }),
])
def test_issue_adapters_leave_missing_criteria_silent(adapter, raw, caplog):
    with caplog.at_level("WARNING", logger="no_human.intake.criteria"):
        assert adapter.normalize(raw).acceptance_criteria == []
    assert not caplog.records


@pytest.mark.parametrize("text,expected", [
    ("Fix the flaky E2E test", True),
    ("add retries", True),
    ("PROJ-42", False),
    ("owner/repo#12", False),
    ("#12", False),
    ("https://github.com/o/r/issues/9", False),
    ("https://example.com/x", False),
    ("git@github.com:o/r.git", False),
])
def test_is_plain_text_task_separates_prose_from_sources(text, expected):
    assert is_plain_text_task(text) is expected


def test_is_plain_text_task_leaves_url_and_bare_key_intake_unchanged():
    """URL/id intake behaviour is unchanged by the new plain-text predicate."""
    from no_human.intake import ingest_from_url

    assert parse_source("https://github.com/o/r/issues/9").kind == "github"
    with pytest.raises(ValueError, match="not a recognized task URL/id"):
        ingest_from_url("PROJ-42")


def test_a_bare_tracker_key_is_rejected_not_ingested_as_freeform():
    """docs/quickstart.md said `PROJ-42` "is ingested as freeform text". It is not.

    `parse_source` classifies it as freeform, and `ingest_from_url` raises on
    exactly that classification — so `nh task add PROJ-42` prints
    "intake failed:" and exits 1. A quickstart is the first thing a new user
    reads, and this told them a command would work that always fails.

    Both halves are asserted, because the doc's error was believing the first
    one implied the second.
    """
    from no_human.intake import ingest_from_url

    assert parse_source("PROJ-42").kind == "freeform"
    with pytest.raises(ValueError, match="not a recognized task URL/id"):
        ingest_from_url("PROJ-42")


def test_quickstart_does_not_promise_freeform_ingestion_of_a_tracker_key():
    """The doc half of the coupling above.

    Anchored to the behaviour, not to phrasing: the guard reads the sentence
    only to confirm it has not gone back to promising ingestion.
    """
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "quickstart.md").read_text(
        encoding="utf-8")
    assert "PROJ-42" in doc, (
        "quickstart no longer discusses a bare tracker key — if the section was "
        "removed, remove this guard deliberately rather than letting it pass "
        "over a subject that is gone"
    )
    assert "ingested as freeform text" not in doc, (
        "quickstart claims a bare tracker key is ingested as freeform text; "
        "ingest_from_url raises and the CLI exits 1"
    )


# --------------------------------------------------------------------------- #
# _default_utility_model honors an operator's configured llm.utility_model    #
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("isolated_env_file")
def test_default_utility_model_reads_the_configured_value(tmp_path, monkeypatch):
    """Model picker part 2 gap fix: this used to read DEFAULT_CONFIG (the
    shipped default) directly, so `nh config models set utility <id>` (or the
    Settings picker) could change llm.utility_model on disk and every intake
    advisory call (evaluate_spec/resolve_assumptions/grill_spec) would still
    silently run on the OLD default — never the operator's chosen model."""
    import no_human.config as nh_config
    from no_human.intake.evaluator import _default_utility_model

    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(nh_config, "CONFIG_PATH", cfg_path)
    nh_config.set_model_ids({"utility_model": "claude-opus-5"}, cfg_path)

    assert _default_utility_model() == "claude-opus-5"


@pytest.mark.usefixtures("isolated_env_file")
def test_default_utility_model_falls_back_to_the_shipped_default_when_config_is_unreadable(
        tmp_path, monkeypatch):
    """The fallback must never raise — every call site here is advisory."""
    import no_human.config as nh_config
    from no_human.config import DEFAULT_CONFIG
    from no_human.intake.evaluator import _default_utility_model

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("not: valid: yaml: [[[")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", cfg_path)

    assert _default_utility_model() == DEFAULT_CONFIG["llm"]["utility_model"]
