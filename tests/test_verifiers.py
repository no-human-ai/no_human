"""Tests for no_human.review.verifiers — loader/selector/diff-filter/judge/runner.

Pure unit tests: tmp_path only, no model call, no network, no subprocess, no
`time.sleep`. Async tests need no marker (pyproject.toml: asyncio_mode = "auto").
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

from no_human.review.verifiers import (
    LoadReport,
    Verifier,
    VerifierResult,
    _resolve_block_path,
    build_prompt,
    filter_diff,
    load_verifiers,
    parse_result,
    run_verifiers,
    select,
    summary_line,
    to_checklist_item,
)

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+    # extra\n"
    "diff --git a/docs/guide.md b/docs/guide.md\n"
    "new file mode 100644\n"
    "index 0000000..3333333\n"
    "--- /dev/null\n"
    "+++ b/docs/guide.md\n"
    "@@ -0,0 +1,2 @@\n"
    "+# Guide\n"
    "+content\n"
    "diff --git a/web/src/old.ts b/web/src/old.ts\n"
    "deleted file mode 100644\n"
    "index 4444444..0000000 100644\n"
    "--- a/web/src/old.ts\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-export const x = 1;\n"
    "-export const y = 2;\n"
)


def _write(tmp_path: Path, text: str) -> Path:
    d = tmp_path / ".no_human"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "verifiers.yaml"
    p.write_text(text)
    return p


def _ok_json(verifier_id: str, *, passed: bool, file: str = "", line: int = 0) -> str:
    return (
        "VERIFIER_JSON_START\n"
        f'{{"verifier_id": "{verifier_id}", "passed": {"true" if passed else "false"}, '
        f'"evidence": "e", "file": {json.dumps(file)}, "line": {line}, "comment": "c"}}\n'
        "VERIFIER_JSON_END"
    )


# --------------------------------------------------------------------------
# 1. Public surface
# --------------------------------------------------------------------------


def test_public_surface_is_exactly_the_documented_names():
    import no_human.review.verifiers as mod

    names = {
        "Verifier",
        "LoadReport",
        "VerifierResult",
        "load_verifiers",
        "select",
        "filter_diff",
        "build_prompt",
        "parse_result",
        "to_checklist_item",
        "run_verifiers",
        "summary_line",
    }
    for name in names:
        assert hasattr(mod, name), name

    assert dataclasses.is_dataclass(mod.Verifier)
    assert mod.Verifier.__dataclass_params__.frozen is True
    assert dataclasses.is_dataclass(mod.LoadReport)
    assert dataclasses.is_dataclass(mod.VerifierResult)

    sig = inspect.signature(mod.load_verifiers)
    assert list(sig.parameters) == ["repo_path", "home"]
    assert sig.parameters["home"].default is None

    sig = inspect.signature(mod.select)
    assert list(sig.parameters) == ["verifiers", "changed_paths"]

    sig = inspect.signature(mod.filter_diff)
    assert list(sig.parameters) == ["diff_text", "paths"]

    sig = inspect.signature(mod.build_prompt)
    assert list(sig.parameters) == ["verifier", "diff_hunks", "file_texts"]

    sig = inspect.signature(mod.parse_result)
    assert list(sig.parameters) == ["raw_output", "verifier", "files_checked"]

    sig = inspect.signature(mod.to_checklist_item)
    assert list(sig.parameters) == ["result"]

    sig = inspect.signature(mod.run_verifiers)
    params = sig.parameters
    # `retry_judge` is new (the bounded-retry fix): additive and optional,
    # defaulting to None (meaning "reuse `judge` itself"), so every existing
    # single-arg call site is untouched.
    assert list(params) == [
        "judge", "verifiers", "diff_text", "read_file", "changed_paths", "retry_judge",
    ]
    assert params["judge"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    for kw in ("verifiers", "diff_text", "read_file", "changed_paths", "retry_judge"):
        assert params[kw].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["retry_judge"].default is None

    sig = inspect.signature(mod.summary_line)
    assert list(sig.parameters) == ["results"]


# --------------------------------------------------------------------------
# 2-17. load_verifiers / _load_file
# --------------------------------------------------------------------------


def test_load_valid_repo_file(tmp_path):
    _write(
        tmp_path,
        """
verifiers:
  - id: rule-one
    statement: First rule statement.
    paths: src/**/*.py
  - id: rule-two
    statement: Second rule statement.
    paths:
      - docs/*
      - "*.md"
    severity: critical
""",
    )
    report = load_verifiers(tmp_path)
    assert isinstance(report, LoadReport)
    assert report.problems == []
    assert [v.id for v in report.verifiers] == ["rule-one", "rule-two"]
    v1, v2 = report.verifiers
    assert v1.paths == ("src/**/*.py",)
    assert v1.severity == "high"
    assert v1.source == "repo"
    assert v2.paths == ("docs/*", "*.md")
    assert v2.severity == "critical"


def test_load_string_paths_becomes_one_tuple(tmp_path):
    _write(tmp_path, "verifiers:\n  - id: r1\n    statement: s\n    paths: src/a.py\n")
    report = load_verifiers(tmp_path)
    assert report.verifiers[0].paths == ("src/a.py",)


def test_load_missing_file_is_not_a_problem(tmp_path):
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert report.problems == []


def test_load_malformed_yaml_reports_and_does_not_raise(tmp_path):
    _write(tmp_path, "verifiers:\n\t- id: r1\n\t  statement: s\n\t  paths: a.py\n")
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 1


def test_load_top_level_not_a_mapping_reported(tmp_path):
    _write(tmp_path, "- 1\n- 2\n")
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 1
    assert "mapping" in report.problems[0]


def test_load_verifiers_key_not_a_list_reported(tmp_path):
    _write(tmp_path, "verifiers: not-a-list\n")
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 1
    assert "list" in report.problems[0]


def test_load_unknown_key_skips_entry_with_problem(tmp_path):
    _write(
        tmp_path,
        "verifiers:\n  - id: r1\n    statement: s\n    paths: a.py\n    extra: nope\n",
    )
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 1
    assert "extra" in report.problems[0]


def test_load_bad_id_rejected(tmp_path):
    entries = ["Upper", "a", '"-leading"']
    yaml_text = "verifiers:\n"
    for bad_id in entries:
        yaml_text += f"  - id: {bad_id}\n    statement: s\n    paths: a.py\n"
    _write(tmp_path, yaml_text)
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 3


def test_load_empty_and_over_600_char_statement_rejected(tmp_path):
    long_statement = "x" * 601
    yaml_text = (
        "verifiers:\n"
        '  - id: r1\n    statement: ""\n    paths: a.py\n'
        f'  - id: r2\n    statement: "{long_statement}"\n    paths: a.py\n'
    )
    _write(tmp_path, yaml_text)
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 2


def test_load_missing_or_empty_paths_rejected(tmp_path):
    yaml_text = (
        "verifiers:\n"
        "  - id: r1\n    statement: s\n"
        "  - id: r2\n    statement: s\n    paths: []\n"
    )
    _write(tmp_path, yaml_text)
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 2


def test_load_bad_severity_rejected(tmp_path):
    _write(
        tmp_path,
        "verifiers:\n  - id: r1\n    statement: s\n    paths: a.py\n    severity: urgent\n",
    )
    report = load_verifiers(tmp_path)
    assert report.verifiers == []
    assert len(report.problems) == 1


def test_load_global_appends_after_repo_with_source_global(tmp_path):
    _write(tmp_path, "verifiers:\n  - id: r1\n    statement: s\n    paths: a.py\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "verifiers.yaml").write_text(
        "verifiers:\n  - id: r2\n    statement: s\n    paths: b.py\n"
    )
    report = load_verifiers(tmp_path, home=home)
    assert [v.id for v in report.verifiers] == ["r1", "r2"]
    assert report.verifiers[0].source == "repo"
    assert report.verifiers[1].source == "global"
    assert report.problems == []


def test_load_duplicate_id_repo_wins_and_global_is_a_problem(tmp_path):
    _write(
        tmp_path,
        "verifiers:\n  - id: r1\n    statement: repo version\n    paths: a.py\n",
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "verifiers.yaml").write_text(
        "verifiers:\n  - id: r1\n    statement: global version\n    paths: b.py\n"
    )
    report = load_verifiers(tmp_path, home=home)
    assert len(report.verifiers) == 1
    assert report.verifiers[0].statement == "repo version"
    assert report.verifiers[0].source == "repo"
    assert len(report.problems) == 1
    assert "r1" in report.problems[0]


def test_load_one_bad_entry_does_not_drop_the_good_ones(tmp_path):
    _write(
        tmp_path,
        "verifiers:\n"
        "  - id: r1\n    statement: s\n    paths: a.py\n"
        "  - id: BAD\n    statement: s\n    paths: a.py\n"
        "  - id: r2\n    statement: s\n    paths: a.py\n",
    )
    report = load_verifiers(tmp_path)
    assert [v.id for v in report.verifiers] == ["r1", "r2"]
    assert len(report.problems) == 1


def test_load_never_raises_on_any_garbage(tmp_path):
    # binary bytes in place of text
    repo1 = tmp_path / "repo1"
    (repo1 / ".no_human").mkdir(parents=True)
    (repo1 / ".no_human" / "verifiers.yaml").write_bytes(bytes(range(256)))
    report1 = load_verifiers(repo1)
    assert report1.verifiers == []
    assert report1.problems

    # a directory sitting where the file should be
    repo2 = tmp_path / "repo2"
    (repo2 / ".no_human" / "verifiers.yaml").mkdir(parents=True)
    report2 = load_verifiers(repo2)
    assert report2.verifiers == []
    assert report2.problems

    # top-level YAML list, not a mapping
    repo3 = tmp_path / "repo3"
    _write(repo3, "- 1\n- 2\n")
    report3 = load_verifiers(repo3)
    assert report3.verifiers == []
    assert report3.problems


def test_load_problems_are_single_sentences_naming_the_file(tmp_path):
    path = _write(tmp_path, "verifiers: not-a-list\n")
    report = load_verifiers(tmp_path)
    assert len(report.problems) == 1
    sentence = report.problems[0]
    assert "\n" not in sentence
    assert str(path) in sentence


# --------------------------------------------------------------------------
# 18-23. select
# --------------------------------------------------------------------------


def test_select_src_double_star_py():
    v = Verifier(id="v1", statement="s", paths=("src/**/*.py",))
    assert select([v], ["src/a.py"]) == [v]
    assert select([v], ["src/a/b/c.py"]) == [v]
    assert select([v], ["src/a.txt"]) == []


def test_select_star_md_is_root_only():
    v = Verifier(id="v1", statement="s", paths=("*.md",))
    assert select([v], ["README.md"]) == [v]
    assert select([v], ["docs/x.md"]) == []


def test_select_web_src_double_star_suffix():
    v = Verifier(id="v1", statement="s", paths=("web/src/**",))
    assert select([v], ["web/src/x.ts"]) == [v]
    assert select([v], ["web/src/a/b.ts"]) == [v]
    assert select([v], ["web/other.ts"]) == []


def test_select_docs_star_does_not_match_nested():
    v = Verifier(id="v1", statement="s", paths=("docs/*",))
    assert select([v], ["docs/a.md"]) == [v]
    assert select([v], ["docs/a/b.md"]) == []


def test_select_preserves_order_and_dedupes():
    v1 = Verifier(id="a", statement="s", paths=("src/**",))
    v2 = Verifier(id="b", statement="s", paths=("docs/*",))
    result = select([v2, v1, v1], ["src/x.py", "docs/y.md"])
    assert [v.id for v in result] == ["b", "a"]
    assert select([v1], []) == []


def test_select_normalises_leading_dot_slash_and_backslashes():
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    assert select([v], ["./src/a.py"]) == [v]
    assert select([v], ["src\\a.py"]) == [v]


# --------------------------------------------------------------------------
# 24-28. filter_diff
# --------------------------------------------------------------------------


def test_filter_diff_keeps_only_matching_blocks():
    kept, matched = filter_diff(DIFF, ["src/**/*.py"])
    assert "src/a.py" in kept
    assert "docs/guide.md" not in kept
    assert "old.ts" not in kept
    assert matched == ["src/a.py"]


def test_filter_diff_deletion_uses_the_a_path():
    kept, matched = filter_diff(DIFF, ["web/src/**"])
    assert "old.ts" in kept
    assert matched == ["web/src/old.ts"]


def test_filter_diff_empty_and_no_match_return_empty():
    assert filter_diff("", ["src/**"]) == ("", [])
    assert filter_diff("   ", ["src/**"]) == ("", [])
    kept, matched = filter_diff(DIFF, ["nomatch/**"])
    assert kept == ""
    assert matched == []


def test_filter_diff_drops_preamble_before_first_block():
    preamble = "commit abc123\nAuthor: x\n\n" + DIFF
    kept, matched = filter_diff(preamble, ["src/**/*.py"])
    assert "commit abc123" not in kept
    assert matched == ["src/a.py"]


def test_filter_diff_handles_quoted_and_spaced_paths():
    quoted = (
        'diff --git "a/weird name.py" "b/weird name.py"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/weird name.py"\n'
        '+++ "b/weird name.py"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    kept, matched = filter_diff(quoted, ["*.py"])
    assert matched == ["weird name.py"]
    assert "weird name.py" in kept


def test_filter_diff_drops_an_unresolvable_block_without_crashing():
    weird = "diff --git onlyonepath\nsome content\n"
    kept, matched = filter_diff(weird, ["*"])
    assert kept == ""
    assert matched == []


# --------------------------------------------------------------------------
# 28b. _resolve_block_path — carry-over fix: only header lines before the
# first @@ hunk marker (or the diff --git line) are eligible.
# --------------------------------------------------------------------------


def test_a_content_line_beginning_with_plus_plus_is_not_read_as_a_file_header():
    block = (
        "diff --git a/src/x.py b/src/x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def f():\n"
        "     return 1\n"
        "+++ foo\n"
    )
    assert _resolve_block_path(block) == "src/x.py"


def test_header_paths_after_the_first_hunk_marker_are_ignored():
    block = (
        "diff --git a/src/x.py b/src/x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def f():\n"
        "+++ b/evil.py\n"
    )
    assert _resolve_block_path(block) == "src/x.py"


# --------------------------------------------------------------------------
# 29-31. build_prompt
# --------------------------------------------------------------------------


def test_build_prompt_contains_statement_untrusted_clause_and_markers():
    v = Verifier(id="rule1", statement="Every public function has a docstring.", paths=("src/**",))
    prompt = build_prompt(
        v,
        "diff --git a/src/a.py b/src/a.py\n...",
        {"src/a.py": "def f():\n    pass\n"},
    )
    assert "Every public function has a docstring." in prompt
    assert "DATA" in prompt
    assert "VERIFIER_JSON_START" in prompt
    assert "VERIFIER_JSON_END" in prompt
    assert "MUST cite" in prompt
    assert "### src/a.py" in prompt
    assert "rule1" in prompt


def test_build_prompt_caps_each_file_at_20k_with_truncated_marker():
    v = Verifier(id="rule1", statement="s", paths=("src/**",))
    big = "x" * 25_000
    prompt = build_prompt(v, "hunks", {"src/a.py": big})
    assert "[truncated]" in prompt
    section = prompt.split("### src/a.py", 1)[1].split("\n\n", 1)[0]
    assert section.count("x") <= 20_000


def test_build_prompt_caps_total_payload_at_120k_hunks_first():
    v = Verifier(id="rule1", statement="s", paths=("src/**",))
    hunks = "H" * 1000
    # each file is under the per-file cap on its own, but nine of them
    # together (135k) exceed the remaining total-payload budget (~119k)
    files = {f"src/f{i}.py": "y" * 15_000 for i in range(9)}
    prompt = build_prompt(v, hunks, files)
    assert "H" * 1000 in prompt
    assert prompt.count("y") < 9 * 15_000
    assert "omitted" in prompt
    assert "src/f0.py" in prompt


# --------------------------------------------------------------------------
# 32-40. parse_result
# --------------------------------------------------------------------------


def test_parse_result_pass():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = _ok_json("r1", passed=True)
    result = parse_result(raw, v, ["src/a.py"])
    assert isinstance(result, VerifierResult)
    assert result.passed is True
    assert result.no_verdict is False
    assert result.evidence == "e"


def test_parse_result_fail_with_citation():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = _ok_json("r1", passed=False, file="src/a.py", line=12)
    result = parse_result(raw, v, ["src/a.py"])
    assert result.passed is False
    assert result.no_verdict is False
    assert result.file == "src/a.py"
    assert result.line == 12


def test_parse_result_no_block_is_no_verdict():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    result = parse_result("nothing here", v, [])
    assert result.passed is False
    assert result.no_verdict is True
    assert "no verdict" in result.evidence


def test_parse_result_invalid_json_is_no_verdict():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = "VERIFIER_JSON_START\n{not json}\nVERIFIER_JSON_END"
    result = parse_result(raw, v, [])
    assert result.no_verdict is True


def test_parse_result_non_dict_json_is_no_verdict():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = "VERIFIER_JSON_START\n[1, 2, 3]\nVERIFIER_JSON_END"
    result = parse_result(raw, v, [])
    assert result.no_verdict is True


def test_parse_result_non_bool_passed_is_no_verdict():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    for passed_literal in ('"true"', "1"):
        raw = (
            "VERIFIER_JSON_START\n"
            '{"verifier_id": "r1", "passed": ' + passed_literal + ", "
            '"evidence": "e", "file": "", "line": 0, "comment": "c"}\n'
            "VERIFIER_JSON_END"
        )
        result = parse_result(raw, v, [])
        assert result.no_verdict is True, passed_literal


def test_parse_result_wrong_verifier_id_is_no_verdict():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = _ok_json("other", passed=True)
    result = parse_result(raw, v, [])
    assert result.no_verdict is True


def test_parse_result_tolerates_prose_and_json_fence():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = (
        "Here is my analysis...\n"
        "VERIFIER_JSON_START\n"
        "```json\n"
        '{"verifier_id": "r1", "passed": true, "evidence": "e", '
        '"file": "", "line": 0, "comment": "c"}\n'
        "```\n"
        "VERIFIER_JSON_END\n"
        "Thanks!"
    )
    result = parse_result(raw, v, [])
    assert result.no_verdict is False
    assert result.passed is True


def test_parse_result_out_of_scope_file_gets_suffix():
    v = Verifier(id="r1", statement="s", paths=("src/**",))
    raw = _ok_json("r1", passed=False, file="other/file.py", line=1)
    result = parse_result(raw, v, ["src/a.py"])
    assert result.file == "other/file.py"
    assert "[cites a file outside the verifier scope]" in result.evidence


def test_parse_result_coerces_line_and_normalises_file():
    v = Verifier(id="r1", statement="s", paths=("src/**",))

    def make(line_val, file_val):
        raw = (
            "VERIFIER_JSON_START\n"
            '{"verifier_id": "r1", "passed": true, "evidence": "e", '
            f'"file": {json.dumps(file_val)}, "line": {json.dumps(line_val)}, "comment": "c"}}\n'
            "VERIFIER_JSON_END"
        )
        return parse_result(raw, v, ["src/a.py"])

    assert make("12", "./src/a.py").line == 12
    assert make("12", "./src/a.py").file == "src/a.py"
    assert make(-3, "src/a.py").line == 0
    assert make("abc", "src/a.py").line == 0


# --------------------------------------------------------------------------
# 41-46. run_verifiers
# --------------------------------------------------------------------------


async def test_run_verifiers_happy_path_records_prompts_and_propagates_tokens():
    v1 = Verifier(id="r1", statement="py rule statement", paths=("src/**/*.py",))
    v2 = Verifier(id="r2", statement="docs rule statement", paths=("docs/*",))
    calls = []

    async def judge(prompt):
        calls.append(prompt)
        vid = "r1" if "py rule statement" in prompt else "r2"
        return _ok_json(vid, passed=True), 42

    def read_file(path):
        return "content of " + path

    results = await run_verifiers(
        judge,
        verifiers=[v1, v2],
        diff_text=DIFF,
        read_file=read_file,
        changed_paths=["src/a.py", "docs/guide.md"],
    )
    assert len(results) == 2
    assert len(calls) == 2
    assert "src/a.py" in calls[0]
    assert "docs/guide.md" not in calls[0]
    assert "docs/guide.md" in calls[1]
    assert "src/a.py" not in calls[1]
    assert all(r.tokens_used == 42 for r in results)
    assert all(r.passed for r in results)


async def test_run_verifiers_one_judge_raises_others_unaffected():
    va = Verifier(id="a", statement="python rule statement", paths=("src/**/*.py",))
    vb = Verifier(id="b", statement="docs rule statement", paths=("docs/*",))
    vc = Verifier(id="c", statement="web rule statement", paths=("web/src/**",))

    async def judge(prompt):
        if "docs rule statement" in prompt:
            raise RuntimeError("boom")
        vid = "a" if "python rule statement" in prompt else "c"
        return _ok_json(vid, passed=True), 5

    results = await run_verifiers(
        judge,
        verifiers=[va, vb, vc],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py", "docs/guide.md", "web/src/old.ts"],
    )
    by_id = {r.verifier_id: r for r in results}
    assert len(results) == 3
    assert by_id["a"].passed is True and by_id["a"].no_verdict is False
    assert by_id["c"].passed is True and by_id["c"].no_verdict is False
    assert by_id["b"].no_verdict is True
    assert "RuntimeError" in by_id["b"].evidence
    # A raise on the first call gets one bounded retry (no `retry_judge` was
    # given, so it reuses `judge` itself); raising again on retry means the
    # judge is unavailable, not that a genuine finding exists — this must
    # never be billed to the coder as a defect (see run_verifiers's retry).
    assert by_id["b"].unavailable is True


async def test_run_verifiers_skips_unreadable_files_and_read_file_exceptions():
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    calls = []

    async def judge(prompt):
        calls.append(prompt)
        return _ok_json("a", passed=True), 0

    def read_file(path):
        raise OSError("cannot read")

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=read_file,
        changed_paths=["src/a.py"],
    )
    assert len(results) == 1
    assert results[0].no_verdict is False
    assert len(calls) == 1
    assert "### src/a.py" not in calls[0]


async def test_run_verifiers_no_matching_hunks_yields_no_verdict_without_calling_judge():
    v = Verifier(id="a", statement="s", paths=("nomatch/**",))
    called = False

    async def judge(prompt):
        nonlocal called
        called = True
        return "x", 0

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["nomatch/foo.py"],
    )
    assert len(results) == 1
    assert results[0].no_verdict is True
    assert called is False
    # No judge call was ever made, so a retry could not change anything —
    # this deterministic diff-filter outcome must stay `unavailable=False`
    # (never escalate; it is not an infra/config signal at all).
    assert results[0].unavailable is False


async def test_run_verifiers_only_runs_selected_verifiers():
    va = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    vb = Verifier(id="b", statement="s", paths=("nomatch/**",))
    calls = []

    async def judge(prompt):
        calls.append(prompt)
        return _ok_json("a", passed=True), 0

    results = await run_verifiers(
        judge,
        verifiers=[va, vb],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    assert len(results) == 1
    assert results[0].verifier_id == "a"
    assert len(calls) == 1


async def test_run_verifiers_tolerates_a_malformed_judge_return():
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))

    async def judge(prompt):
        return "not a tuple"

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    assert len(results) == 1
    assert results[0].tokens_used == 0
    assert results[0].no_verdict is True
    # Same malformed return on the retry (this test gives no `retry_judge`,
    # so the retry reuses the same always-malformed `judge`) — still no
    # verdict after the bounded retry, so this must be flagged `unavailable`.
    assert results[0].unavailable is True


# --------------------------------------------------------------------------
# 50-55. run_verifiers — bounded retry on no_verdict (escalation semantics)
# --------------------------------------------------------------------------
#
# Precedent: `reviewer.py` never lets a review round that reached no verdict
# render as a failing, coder-facing finding — it raises `ReviewerUnavailable`
# after one bounded retry so the task escalates honestly instead of billing
# an attempt for a defect nobody found. These tests pin the equivalent
# contract at the `run_verifiers` layer: `result.unavailable` is the signal
# the orchestrator uses to escalate instead of failing the round (exercised
# at the orchestrator level in test_verifiers_gate.py); here we pin exactly
# when it gets set, and that it never gets set for a genuine, first-call
# verdict.


async def test_run_verifiers_no_verdict_twice_is_unavailable_not_a_high_severity_fail():
    """TEST (a): a transport-style no-verdict (judge produced no text at all,
    on both the first call and the retry) must come back `unavailable=True`
    — and render (via `to_checklist_item`) as advisory, NOT as a blocking
    high-severity finding. That rendering is exactly what used to bill the
    coder for a defect nobody found."""
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    calls = 0

    async def judge(prompt):
        nonlocal calls
        calls += 1
        return "", 0  # no text at all — the orchestrator's judge() shape for "unavailable"

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    assert calls == 2, "one bounded retry, not more"
    (result,) = results
    assert result.no_verdict is True
    assert result.unavailable is True
    assert result.passed is False, "an unavailable rule must never read as satisfied"

    item = to_checklist_item(result)
    assert item.passed is False, "the RENDERED row must never show as passing"
    assert item.severity == "low", (
        "unavailable is advisory, not a blocking high-severity finding — "
        "the exact anti-pattern this fix exists to stop")
    assert "judge unavailable" in result.evidence, (
        "a transport-style no-verdict (no text on either call) must be worded "
        "as the judge being unavailable, not as a malformed rule")
    assert "never produced a parseable verdict" not in result.evidence, (
        "the transport wording and the malformed-response wording are "
        "mutually exclusive — this call never produced any raw text at all")


async def test_run_verifiers_no_verdict_once_then_a_verdict_uses_the_retry():
    """TEST (b): the retry is not decorative — a verdict recovered on the
    second call is what governs the result, and exactly one extra judge
    call is made (not more)."""
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    calls = 0

    async def judge(prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "", 0
        return _ok_json("a", passed=True), 7

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    assert calls == 2
    (result,) = results
    assert result.no_verdict is False
    assert result.unavailable is False
    assert result.passed is True


async def test_run_verifiers_a_no_verdict_consumes_exactly_one_retry_and_no_more():
    """Arithmetic guard: the judge would return a real verdict on a THIRD
    call, yet `run_verifiers` must stop after the first call plus its one
    bounded retry — a policy widening (looping until success) would flip
    `unavailable` to False and `calls` to 3. Also pins that the retry's
    token spend is additive with the first call's, not overwritten."""
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    calls = 0

    async def judge(prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "", 5
        if calls == 2:
            return "", 7
        return _ok_json("a", passed=True), 100  # would succeed if ever reached

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    assert calls == 2, "must stop after the bounded retry, never a third call"
    (result,) = results
    assert result.unavailable is True
    assert result.no_verdict is True
    assert result.tokens_used == 12, (
        "the retry's token spend (7) must add to the first call's (5)")


async def test_run_verifiers_no_marker_twice_names_the_verifier_id_and_source_file():
    """TEST (c): when the judge DOES respond (non-empty text) but never emits
    a parseable marker, on both the first call and the retry, that is a
    malformed-rule/confused-judge signal — a config problem the operator can
    fix, so the message must name which rule and which file to look at."""
    v = Verifier(
        id="weird-rule", statement="s", paths=("src/**/*.py",),
        source_file=".no_human/verifiers.yaml",
    )

    async def judge(prompt):
        return "I have thoughts, but no marker here.", 3

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    (result,) = results
    assert result.unavailable is True
    assert result.passed is False, "an unavailable rule must never read as satisfied"
    assert "weird-rule" in result.evidence
    assert ".no_human/verifiers.yaml" in result.evidence
    assert "never produced a parseable verdict" in result.evidence, (
        "a responded-but-unparseable no-verdict must be worded as a "
        "malformed rule, not as the judge being unavailable")
    assert "judge unavailable" not in result.evidence, (
        "the transport wording and the malformed-response wording are "
        "mutually exclusive — this call always produced raw text")

    # TEST (d): the rendered checklist row, not just the raw flag.
    item = to_checklist_item(result)
    assert item.passed is False
    assert item.severity == "low"


async def test_run_verifiers_a_genuine_first_call_failure_never_retries():
    """TEST (e), unit-level regression: a real, parseable failing verdict on
    the FIRST call must fail exactly as before the retry existed — no
    retry fires, and the result is never marked `unavailable`."""
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    calls = 0

    async def judge(prompt):
        nonlocal calls
        calls += 1
        return _ok_json("a", passed=False, file="src/a.py", line=1), 9

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
    )
    assert calls == 1, "a genuine verdict on the first call must never trigger a retry"
    (result,) = results
    assert result.passed is False
    assert result.no_verdict is False
    assert result.unavailable is False


async def test_run_verifiers_retry_judge_argument_is_used_when_given():
    """The orchestrator passes a distinct `retry_judge` (a shorter-timeout
    call) — confirm `run_verifiers` actually calls IT for the retry, not the
    first `judge` again."""
    v = Verifier(id="a", statement="s", paths=("src/**/*.py",))
    primary_calls = 0
    retry_calls = 0

    async def judge(prompt):
        nonlocal primary_calls
        primary_calls += 1
        return "", 0

    async def retry_judge(prompt):
        nonlocal retry_calls
        retry_calls += 1
        return _ok_json("a", passed=True), 1

    results = await run_verifiers(
        judge,
        verifiers=[v],
        diff_text=DIFF,
        read_file=lambda p: None,
        changed_paths=["src/a.py"],
        retry_judge=retry_judge,
    )
    assert primary_calls == 1
    assert retry_calls == 1
    (result,) = results
    assert result.passed is True
    assert result.unavailable is False


# --------------------------------------------------------------------------
# 47-49. summary_line / to_checklist_item
# --------------------------------------------------------------------------


def test_summary_line_empty_all_pass_and_failures_sorted():
    assert summary_line([]) == ""
    passing = [
        VerifierResult("a", True, "", "", 0, "", "high", [], 0, "", False),
        VerifierResult("b", True, "", "", 0, "", "high", [], 0, "", False),
    ]
    assert summary_line(passing) == "2 of 2 satisfied"
    mixed = [
        VerifierResult("z", False, "", "", 0, "", "high", [], 0, "", False),
        VerifierResult("a", True, "", "", 0, "", "high", [], 0, "", False),
        VerifierResult("m", False, "", "", 0, "", "high", [], 0, "", False),
    ]
    assert summary_line(mixed) == "2 of 3 failed — m, z"


def test_summary_line_treats_an_unavailable_verifier_as_advisory_not_failed():
    # An unavailable-only round reads as satisfied-with-a-caveat, never as
    # a failure — the round it feeds (orchestrator._run_review) continues
    # to the reviewer, so its own event must not say "failed".
    unavailable_only = [
        VerifierResult("no-todo", False, "", "", 0, "", "high", [], 0, "", True, True),
    ]
    assert summary_line(unavailable_only) == (
        "0 of 1 satisfied, 1 no verdict (advisory) — no-todo"
    )


def test_summary_line_a_genuine_failure_is_never_diluted_by_an_unavailable_sibling():
    # Mixed round: one rule genuinely failed, one never reached a verdict.
    # The failed count must count only the genuine failure — the sibling's
    # unavailability is reported, not blamed on the coder.
    mixed = [
        VerifierResult("rule-a", False, "boom", "f.py", 1, "", "high", [], 0, "", False, False),
        VerifierResult("rule-b", False, "", "", 0, "", "high", [], 0, "", True, True),
    ]
    assert summary_line(mixed) == (
        "1 of 2 failed — rule-a; 1 no verdict (advisory) — rule-b"
    )


def test_to_checklist_item_label_and_fields():
    result = VerifierResult("rule-x", False, "ev", "f.py", 3, "cm", "critical", ["f.py"], 0, "", False)
    item = to_checklist_item(result)
    assert item.label == "rule:rule-x"
    assert item.passed is False
    assert item.severity == "critical"
    assert item.file == "f.py"
    assert item.line == 3
    assert item.comment == "cm"


def test_to_checklist_item_no_verdict_forces_high_severity():
    result = VerifierResult("rule-x", False, "no verdict: x", "", 0, "", "low", [], 0, "", True)
    item = to_checklist_item(result)
    assert item.severity == "high"
