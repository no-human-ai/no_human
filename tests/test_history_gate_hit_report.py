"""Tests for `scripts/history_gate_hit_report.py`.

These tests are HERMETIC on purpose: no private repo, no git history, no real
`verify_public_history.py`. The parsing/grouping/rendering half of the tool is
pure text-in, structure-out and is exercised directly. The scan-driver half is
exercised against a tiny FAKE scanner module (a temp `.py` file implementing
only the handful of names `run_scanner` calls), which is enough to prove the
exit-code contract and the "never modifies what it reads" property without
needing the real, drop-classified scanner.

Style note: assertions are on BEHAVIOR (parsed field values, raised
exceptions, return codes, unchanged file bytes) — never on the tool's own
source text — so a future rewrite of `history_gate_hit_report.py` that keeps
the same behavior cannot spuriously fail these tests, and one that changes
behavior cannot spuriously pass them.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "history_gate_hit_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("_nh_history_gate_hit_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses' own machinery resolves annotations via sys.modules[cls.__module__],
    # so the module must be registered before exec — same registration
    # history_gate_hit_report.py's own _load_module_by_path performs for the
    # scanner module it loads.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hgr = _load()


# --------------------------------------------------------------------------- #
# AC1: grammar parsing
# --------------------------------------------------------------------------- #

def test_parses_each_hit_line_grammar():
    lines = [
        "  LEAK blob abc123def456 src/thing.py :: shape_tracker_id: TRK-9981",
        "  LEAK path path name legacy/attested_export.json :: shape_scoped_app: attested-export",
        "  LEAK message 0011223344ff MESSAGE :: vocab_competitor: acme-corp",
        "  LEAK identity 998877665544 AUTHOR Jane Doe <jane@example.com> :: vocab_competitor: acme-corp",
        "  LEAK tag aa11bb22cc33 TAG refs/tags/v1.0 :: shape_tracker_id: TRK-1200",
        "  LEAK tag aa11bb22cc33 TAG TARGET commit aa11bb22cc33 :: shape_tracker_id: TRK-1200",
    ]
    hits, elided = hgr.parse_hit_lines(lines)
    assert elided == {}
    assert len(hits) == 6

    blob, path, message, identity, tag_ref, tag_target = hits

    assert blob.surface == "blob"
    assert blob.commit == "abc123def456"
    assert blob.path == "src/thing.py"
    assert blob.klass == "shape_tracker_id"
    assert blob.match == "TRK-9981"

    assert path.surface == "path"
    assert path.path == "legacy/attested_export.json"
    assert path.klass == "shape_scoped_app"
    assert path.match == "attested-export"

    assert message.surface == "message"
    assert message.commit == "0011223344ff"
    assert message.klass == "vocab_competitor"
    assert message.match == "acme-corp"

    assert identity.surface == "identity"
    assert identity.commit == "998877665544"
    assert identity.role == "AUTHOR"
    assert identity.identity == "Jane Doe <jane@example.com>"
    assert identity.klass == "vocab_competitor"
    assert identity.match == "acme-corp"

    assert tag_ref.surface == "tag"
    assert tag_ref.ref == "refs/tags/v1.0"
    assert tag_ref.tag_target is None
    assert tag_ref.klass == "shape_tracker_id"

    assert tag_target.surface == "tag"
    assert tag_target.ref is None
    assert tag_target.tag_target == "commit aa11bb22cc33"


def test_parses_hit_body_from_json_dump_shape():
    # The raw HistoryReport.*_hits strings have no "LEAK <surface>" label —
    # that is only added by main()'s print loop — so the JSON path must parse
    # correctly from the bare body too.
    hit = hgr.parse_hit_body("blob", "abc123def456 src/thing.py :: shape_x: match1", "raw")
    assert hit.commit == "abc123def456"
    assert hit.path == "src/thing.py"
    assert hit.klass == "shape_x"
    assert hit.match == "match1"


def test_malformed_hit_line_raises_value_error():
    with pytest.raises(ValueError):
        hgr.parse_hit_body("blob", "abc123def456 src/thing.py (no separator)", "raw")


# --------------------------------------------------------------------------- #
# AC1: the LITERAL marker grammar — a second, structurally different grammar
# from the shape grammar above. Built by `build_public_export.py`'s
# `verify_tree`/`_emit` as "{rel}:{line}:{redacted_term}:{how}", where `rel`
# is a synthetic materialized filename (colon-free by construction: u{n}/f...,
# m{n}, i{n}, t{n}), `line` is always a plain integer, and `redacted_term` is
# `_redact(term)` = first-char + length ONLY (the private term itself is never
# exposed to this tool, by design). Discovered from the real scan output
# (a genuine hit was `... :: u32/f.py:3:n*(14):plaintext`), NOT guessed.
# --------------------------------------------------------------------------- #

def test_parses_literal_family_marker_grammar():
    hit = hgr.parse_hit_body(
        "blob", "a8a04496d438 src/no_human/email/__init__.py :: "
                "u32/f.py:3:n*(14):plaintext",
        "raw",
    )
    assert hit.family == "literal"
    assert hit.klass == "literal:plaintext"
    assert hit.match == "n*(14)"  # the REDACTED term, never the private term
    assert hit.commit == "a8a04496d438"
    assert hit.path == "src/no_human/email/__init__.py"


def test_parses_literal_family_absolute_home_path_variant():
    hit = hgr.parse_hit_body(
        "identity",
        "112233445566 AUTHOR Pat Contributor <pat.contributor@example.com> :: "
        "u79/f.py:1491:/home/o*(8)/...:plaintext (absolute home path)",
        "raw",
    )
    assert hit.family == "literal"
    # the free-text "how" annotation is normalized to a stable class label so
    # hits group by MATCH STYLE, not by every wording variant of "how".
    assert hit.klass == "literal:absolute-home-path"
    assert hit.match == "/home/o*(8)/..."
    assert hit.identity == "Pat Contributor <pat.contributor@example.com>"


def test_shape_and_literal_families_group_separately_even_on_same_surface():
    # identity_hits (and blob_hits) mix BOTH families in the real scan; they
    # must never collapse into one class just because they share a surface.
    hits = [
        hgr.parse_hit_body(
            "identity", "c1 AUTHOR A <a@x.com> :: personal-email: a@x.com",
            "raw1"),
        hgr.parse_hit_body(
            "identity", "c2 AUTHOR B <b@x.com> :: u1/f.py:9:b*(5):plaintext",
            "raw2"),
    ]
    groups = hgr.group_hits(hits)
    assert set(groups) == {"personal-email", "literal:plaintext"}
    assert groups["personal-email"].surfaces == {"identity"}
    assert groups["literal:plaintext"].surfaces == {"identity"}


# --------------------------------------------------------------------------- #
# AC1: grouping by detector class
# --------------------------------------------------------------------------- #

def test_grouping_is_by_detector_class():
    lines = [
        "  LEAK blob c1 a.py :: shape_tracker_id: TRK-1",
        "  LEAK identity c2 AUTHOR A <a@x.com> :: shape_tracker_id: TRK-2",
        "  LEAK message c3 MESSAGE :: vocab_competitor: acme",
        "  LEAK blob c4 b.py :: vocab_competitor: acme",
    ]
    hits, _ = hgr.parse_hit_lines(lines)
    groups = hgr.group_hits(hits)

    assert set(groups) == {"shape_tracker_id", "vocab_competitor"}
    assert groups["shape_tracker_id"].raw_count == 2
    assert groups["shape_tracker_id"].surfaces == {"blob", "identity"}
    assert groups["vocab_competitor"].raw_count == 2
    assert groups["vocab_competitor"].surfaces == {"message", "blob"}


def test_samples_capped_per_class():
    # 8 distinct-match hits of the same class must yield at most 5 samples
    # (the default cap), never all 8 and never fewer than min(3, raw_count).
    lines = [
        f"  LEAK blob c{i} path{i}.py :: shape_tracker_id: TRK-{i}"
        for i in range(8)
    ]
    hits, _ = hgr.parse_hit_lines(lines)
    groups = hgr.group_hits(hits, samples_per_class=5)
    g = groups["shape_tracker_id"]
    assert g.raw_count == 8
    assert 3 <= len(g.samples) <= 5

    groups3 = hgr.group_hits(hits, samples_per_class=3)
    assert len(groups3["shape_tracker_id"].samples) == 3


# --------------------------------------------------------------------------- #
# AC1: completeness — cap elision must never be silently dropped
# --------------------------------------------------------------------------- #

def test_cap_elision_is_detected_and_reported():
    lines = [
        "  LEAK blob c1 a.py :: shape_tracker_id: TRK-1",
        "  ... and 213 more LEAK blob hit(s)",
        "  LEAK identity c2 AUTHOR A <a@x.com> :: shape_tracker_id: TRK-2",
    ]
    hits, elided = hgr.parse_hit_lines(lines)
    assert len(hits) == 2
    assert elided == {"LEAK blob": 213}
    assert hgr.total_elided(elided) == 213


def test_no_elision_marker_means_zero_elided():
    lines = ["  LEAK blob c1 a.py :: shape_tracker_id: TRK-1"]
    _, elided = hgr.parse_hit_lines(lines)
    assert hgr.total_elided(elided) == 0


# --------------------------------------------------------------------------- #
# distinct vs raw counts — the same leak replayed across commits is one leak
# --------------------------------------------------------------------------- #

def test_distinct_vs_raw_counts():
    # Same (path, class, match) hit repeated across 5 different commits: raw
    # count is 5, but it is exactly ONE distinct leak.
    lines = [
        f"  LEAK blob c{i} src/thing.py :: shape_tracker_id: TRK-9981"
        for i in range(5)
    ]
    # Plus one genuinely different leak at a different path.
    lines.append("  LEAK blob c9 other/file.py :: shape_tracker_id: TRK-1200")

    hits, _ = hgr.parse_hit_lines(lines)
    groups = hgr.group_hits(hits)
    g = groups["shape_tracker_id"]

    assert g.raw_count == 6
    assert len(g.distinct_pairs) == 2
    assert len(g.distinct_paths) == 2
    assert len(g.distinct_commits) == 6  # commit shas still all differ


# --------------------------------------------------------------------------- #
# AC2: every class in the report needs an explicit classification
# --------------------------------------------------------------------------- #

def _one_group():
    hits, _ = hgr.parse_hit_lines([
        "  LEAK blob c1 a.py :: shape_tracker_id: TRK-1",
        "  LEAK message c2 MESSAGE :: vocab_competitor: acme",
    ])
    return hgr.group_hits(hits)


def test_every_class_requires_a_classification():
    groups = _one_group()

    # Missing classification for one of the two classes present -> refused.
    with pytest.raises(hgr.ReportError):
        hgr.render_markdown(
            groups,
            {"shape_tracker_id": ("FALSE-POSITIVE", "broad shape, benign sample")},
            elided={},
            enforce_verdict=(False, "no real trace found"),
        )

    # Both classified, with non-empty rationale -> renders and includes both
    # class headings plus verdicts.
    text = hgr.render_markdown(
        groups,
        {
            "shape_tracker_id": ("FALSE-POSITIVE", "broad shape, benign sample"),
            "vocab_competitor": ("REAL-TRACE", "matches a known external name"),
        },
        elided={},
        enforce_verdict=(False, "one class is a false positive, one needs review"),
    )
    assert "shape_tracker_id` — FALSE-POSITIVE" in text
    assert "vocab_competitor` — REAL-TRACE" in text

    # An invalid verdict token is also refused, not silently accepted.
    with pytest.raises(hgr.ReportError):
        hgr.render_markdown(
            groups,
            {
                "shape_tracker_id": ("MAYBE", "unsure"),
                "vocab_competitor": ("REAL-TRACE", "matches a known external name"),
            },
            elided={},
            enforce_verdict=(False, "n/a"),
        )

    # An empty rationale is also refused.
    with pytest.raises(hgr.ReportError):
        hgr.render_markdown(
            groups,
            {
                "shape_tracker_id": ("FALSE-POSITIVE", "   "),
                "vocab_competitor": ("REAL-TRACE", "matches a known external name"),
            },
            elided={},
            enforce_verdict=(False, "n/a"),
        )


# --------------------------------------------------------------------------- #
# AC4: an explicit enforce recommendation is mandatory
# --------------------------------------------------------------------------- #

def test_report_requires_explicit_enforce_verdict():
    groups = _one_group()
    classifications = {
        "shape_tracker_id": ("FALSE-POSITIVE", "broad shape, benign sample"),
        "vocab_competitor": ("REAL-TRACE", "matches a known external name"),
    }

    with pytest.raises(hgr.ReportError):
        hgr.render_markdown(groups, classifications, elided={},
                            enforce_verdict=(True, ""))

    with pytest.raises(hgr.ReportError):
        hgr.render_markdown(groups, classifications, elided={},
                            enforce_verdict=(False, "   "))

    text_yes = hgr.render_markdown(groups, classifications, elided={},
                                   enforce_verdict=(True, "every hit is a real trace"))
    assert "RECOMMENDATION: ENFORCE = YES — every hit is a real trace" in text_yes

    text_no = hgr.render_markdown(groups, classifications, elided={},
                                  enforce_verdict=(False, "one class is unresolved"))
    assert "RECOMMENDATION: ENFORCE = NO — one class is unresolved" in text_no


# --------------------------------------------------------------------------- #
# AC3: this tool must never tune/modify the detector it reads from
# --------------------------------------------------------------------------- #

_FAKE_SCANNER_OK = '''
import dataclasses

class _Builder:
    def _terms_or_die(self, source):
        return {}

def _script_repo_root():
    return None

def _load_builder(repo_root):
    return _Builder()

def load_shapes(source):
    return {}

def load_message_terms(source, builder):
    return {}

@dataclasses.dataclass
class _Report:
    commits: int = 3
    blobs: int = 1
    paths: int = 0
    messages: int = 0
    identities: int = 0
    tags: int = 0
    blob_hits: list = dataclasses.field(
        default_factory=lambda: ["abc123def456 src/thing.py :: shape_x: token"])
    path_hits: list = dataclasses.field(default_factory=list)
    message_hits: list = dataclasses.field(default_factory=list)
    identity_hits: list = dataclasses.field(default_factory=list)
    tag_hits: list = dataclasses.field(default_factory=list)
    missing_files: list = dataclasses.field(default_factory=list)
    extra_files: list = dataclasses.field(default_factory=list)

def scan_history(repo, builder, terms, shapes, message_terms=None,
                 workdir=None, since=None, ref=None, progress=None):
    return _Report()
'''


def test_no_detector_files_modified(tmp_path):
    scanner_path = tmp_path / "fake_verify_public_history.py"
    scanner_path.write_text(_FAKE_SCANNER_OK, encoding="utf-8")
    before_hash = hashlib.sha256(scanner_path.read_bytes()).hexdigest()
    before_mtime = scanner_path.stat().st_mtime_ns

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    out_json = tmp_path / "out.json"

    args = argparse.Namespace(
        scanner=str(scanner_path), source=str(source_dir), repo=str(repo_dir),
        ref=None, since=None, json=str(out_json), progress_log=None,
    )
    rc = hgr.cmd_scan(args)
    assert rc == 0

    after_hash = hashlib.sha256(scanner_path.read_bytes()).hexdigest()
    after_mtime = scanner_path.stat().st_mtime_ns
    assert before_hash == after_hash, "scan must never rewrite the detector it loads"
    assert before_mtime == after_mtime, "scan must never even touch the detector's mtime"

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["blob_hits"] == ["abc123def456 src/thing.py :: shape_x: token"]


# --------------------------------------------------------------------------- #
# exit-code contract: an arming/gate problem is exit 2, never exit 1
# --------------------------------------------------------------------------- #

_FAKE_SCANNER_GATE_ERROR = _FAKE_SCANNER_OK.replace(
    "def scan_history(repo, builder, terms, shapes, message_terms=None,\n"
    "                 workdir=None, since=None, ref=None, progress=None):\n"
    "    return _Report()\n",
    "class GateError(RuntimeError):\n"
    "    pass\n\n"
    "def scan_history(repo, builder, terms, shapes, message_terms=None,\n"
    "                 workdir=None, since=None, ref=None, progress=None):\n"
    "    raise GateError('could not arm the gate')\n",
)


def test_gate_error_is_exit_2_not_1(tmp_path, capsys):
    scanner_path = tmp_path / "fake_verify_public_history.py"
    scanner_path.write_text(_FAKE_SCANNER_GATE_ERROR, encoding="utf-8")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    out_json = tmp_path / "out.json"

    args = argparse.Namespace(
        scanner=str(scanner_path), source=str(source_dir), repo=str(repo_dir),
        ref=None, since=None, json=str(out_json), progress_log=None,
    )
    rc = hgr.cmd_scan(args)
    assert rc == 2, "a gate-arming failure must be exit 2, never conflated with exit 1 (leak found)"
    assert not out_json.exists(), "no JSON should be written when the gate could not be armed"


# --------------------------------------------------------------------------- #
# `report --classify`: the CLI path must actually reach render_markdown.
#
# Without this, render_markdown is only ever exercised by tests handing it
# pre-built classification dicts, and nothing on the command line can turn a
# captured scan into the classified, ENFORCE-verdict deliverable AC2/AC4
# require — the tool would be a framework, not something that produces the
# report. These tests prove `report --classify ... --enforce ...
# --enforce-reason ...` really renders it, end to end, from a JSON hits file.
# --------------------------------------------------------------------------- #

def _report_args(tmp_path, *, classify=None, enforce=None, enforce_reason=None):
    hits_json = tmp_path / "hits.json"
    hits_json.write_text(json.dumps({
        "blob_hits": ["abc123def456 src/thing.py :: shape_x: match1"],
        "path_hits": [],
        "message_hits": [],
        "identity_hits": [],
        "tag_hits": [],
    }), encoding="utf-8")
    return argparse.Namespace(
        json=str(hits_json), stdout=None, check_totals=None,
        classify=classify, enforce=enforce, enforce_reason=enforce_reason,
    )


def test_cmd_report_renders_classified_markdown_via_classify_flag(tmp_path, capsys):
    classify_json = tmp_path / "classify.json"
    classify_json.write_text(json.dumps({
        "shape_x": {"verdict": "FALSE-POSITIVE",
                    "rationale": "match1 is a synthetic test fixture."},
    }), encoding="utf-8")

    args = _report_args(
        tmp_path, classify=str(classify_json), enforce="no",
        enforce_reason="only one false-positive class was scanned; nothing "
                        "here supports flipping the gate.")
    rc = hgr.cmd_report(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "## Class: `shape_x` — FALSE-POSITIVE" in out
    assert "match1 is a synthetic test fixture." in out
    assert "RECOMMENDATION: ENFORCE = NO" in out


def test_cmd_report_classify_requires_enforce_args(tmp_path, capsys):
    classify_json = tmp_path / "classify.json"
    classify_json.write_text(json.dumps({
        "shape_x": {"verdict": "FALSE-POSITIVE", "rationale": "fixture"},
    }), encoding="utf-8")

    args = _report_args(tmp_path, classify=str(classify_json), enforce=None,
                        enforce_reason=None)
    rc = hgr.cmd_report(args)
    assert rc == 2, "missing --enforce/--enforce-reason must not silently render"
    assert "RECOMMENDATION" not in capsys.readouterr().out


def test_cmd_report_classify_propagates_report_error_as_exit_2(tmp_path, capsys):
    # classify.json omits the class actually present in the hits -> render_markdown
    # must raise ReportError (no classification for a class with hits), and
    # cmd_report must surface that as exit 2, not crash or fabricate a verdict.
    classify_json = tmp_path / "classify.json"
    classify_json.write_text(json.dumps({}), encoding="utf-8")

    args = _report_args(tmp_path, classify=str(classify_json), enforce="no",
                        enforce_reason="placeholder")
    rc = hgr.cmd_report(args)
    assert rc == 2
    assert "no classification for class" in capsys.readouterr().err


def test_cmd_report_without_classify_still_prints_raw_group_json(tmp_path, capsys):
    # Backward compatibility: the pre-existing, unclassified stats path (used
    # for quick inspection of a fresh capture before classification exists)
    # must keep working exactly as before when --classify is not given.
    args = _report_args(tmp_path)
    rc = hgr.cmd_report(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shape_x"]["raw_count"] == 1


# --------------------------------------------------------------------------- #
# Range attribution (layer 3): PURE functions only — no git, no scanner. The
# end-to-end behavior (real scratch repo, real merge-base, real `gate`
# subcommand) is covered separately in
# tests/test_history_gate_range_attribution.py; these tests isolate the
# set-difference logic itself so it can be proven correct without git.
# --------------------------------------------------------------------------- #

def test_attribute_hits_splits_on_dedup_key():
    tip_hits, _ = hgr.parse_hit_lines([
        "  LEAK blob c1 src/new.py :: shape_x: NEW",
        "  LEAK blob c0 legacy/old.py :: shape_x: OLD",
    ])
    base_hits, _ = hgr.parse_hit_lines([
        "  LEAK blob c0 legacy/old.py :: shape_x: OLD",
    ])
    introduced, preexisting = hgr.attribute_hits(tip_hits, base_hits)
    assert [h.path for h in introduced] == ["src/new.py"]
    assert [h.path for h in preexisting] == ["legacy/old.py"]


def test_attribute_hits_dedups_by_commit_independent_key_not_raw_hit():
    # The SAME (path, class, match) hit replayed under a DIFFERENT commit sha
    # at the base must still count as pre-existing: dedup_key() is
    # commit-independent by design (Hit.dedup_key's own docstring), and
    # attribute_hits must respect that instead of diffing on raw hit
    # equality (which includes the commit sha and would always miss).
    tip_hits, _ = hgr.parse_hit_lines([
        "  LEAK blob c_tip legacy/old.py :: shape_x: OLD",
    ])
    base_hits, _ = hgr.parse_hit_lines([
        "  LEAK blob c_base legacy/old.py :: shape_x: OLD",
    ])
    introduced, preexisting = hgr.attribute_hits(tip_hits, base_hits)
    assert introduced == []
    assert len(preexisting) == 1


def test_attribute_extra_files_splits_and_flags_untouched():
    tip_extra = ["extra/old.bin", "extra/new.bin", "extra/renamed.bin"]
    base_extra = ["extra/old.bin"]
    range_paths = ["extra/new.bin"]  # git diff never names renamed.bin
    result = hgr.attribute_extra_files(tip_extra, base_extra, range_paths)
    assert result["preexisting_extra"] == ["extra/old.bin"]
    assert set(result["range_extra"]) == {"extra/new.bin", "extra/renamed.bin"}
    # new-at-tip-vs-base but never named by `git diff` for this range: still
    # counted as range-introduced (the base scan genuinely lacked it), but
    # flagged separately so it can't silently be read as "this push added
    # this file" when the range's own diff disagrees.
    assert result["range_extra_untouched"] == ["extra/renamed.bin"]


def test_render_range_verdict_passes_with_nonzero_preexisting():
    # A non-zero PRE-EXISTING backlog at the base must not fail the RANGE
    # verdict, and must not be silently dropped from the summary either.
    tip_hits, _ = hgr.parse_hit_lines([
        "  LEAK blob c0 legacy/old.py :: shape_x: OLD",
    ])
    verdict = hgr.RangeVerdict(
        ref="C", since="B", base="B", base_kind="merge-base",
        range_commit_count=1,
        tip_payload={}, base_payload={},
        tip_hits=tip_hits, tip_extra=[], tip_missing=[],
        introduced_hits=[], preexisting_hits=tip_hits,
        range_extra=[], preexisting_extra=[], range_extra_untouched=[],
        range_missing=[], preexisting_missing=[], range_missing_untouched=[],
    )
    assert verdict.failed is False

    text = hgr.render_range_verdict(verdict)
    assert "RANGE VERDICT: PASSED" in text
    assert ("0 blob, 0 path, 0 message, 0 identity, 0 tag hit(s) introduced "
           "by this range") in text
    assert "1 hit(s) and 0 extra file(s) pre-existing at base" in text
