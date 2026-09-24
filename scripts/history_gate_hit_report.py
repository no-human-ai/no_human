#!/usr/bin/env python3
"""Read the history gate's own hits instead of guessing from a count.

WHY THIS FILE EXISTS (2026-09-16). `scripts/verify_public_history.py` (private,
drop-classified) reports a one-line summary — "214 blob, 0 path, 33 message, 32
identity, 0 tag hit(s)" — and prints per-hit detail via `main()`, but caps each
group at 200 lines, and the pre-push guard (`nh-guard`) that invokes it logs
only the last 600 characters of that output. Between the two truncations,
NOBODY had read what the 214 blob hits and 32 identity hits actually ARE. A
count cannot distinguish "214 genuine employer-trace leaks" from "214 matches
of a broad shape class against ordinary contributor identities" — those two
readings demand opposite responses, and guessing between them is exactly what
this file refuses to do.

THIS FILE DOES NOT DETECT ANYTHING NEW. It has two independent halves:

  1. A pure parser/grouper (`parse_hit_lines`, `group_hits`) over the EXACT text
     `verify_public_history.py`'s `main()` already prints — `LEAK <surface>
     <locator> :: <class>: <match>` and its `... and N more ... hit(s)` cap
     marker. No git, no scanner import, no shape pattern of its own: it only
     reads what the gate already said. This half is what the unit tests below
     exercise, and it needs neither a private repo nor git history.
  2. An UNCAPPED scan driver (`run_scanner`, wired to the `scan` subcommand)
     that loads the private scanner MODULE BY PATH — the same
     `importlib.util.spec_from_file_location` mechanism the scanner uses on
     itself — and calls `scan_history` directly, the way `main()` does, except
     it serialises the whole `HistoryReport` (every `*_hits` list, uncapped) to
     JSON instead of printing the first 200 of each. It adds no rule, narrows
     no pattern, and never writes into the scanner's own file or the guard
     test's `_SHAPES` list: the classes it reports are whatever the scanner
     already decided fired.

The `report` subcommand renders the grouped, classified markdown from either a
captured stdout `.txt` (subject to the printer's 200-cap; elided counts are
detected and surfaced, never silently dropped) or an uncapped `--json` file
from `scan`. `render_markdown` REFUSES to render a group without an explicit
`REAL-TRACE` / `FALSE-POSITIVE` classification and a non-empty rationale, and
REFUSES to render without an explicit enforce recommendation — a report that
can print without either of those defeats the reason this file exists.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable


class ReportError(RuntimeError):
    """This tool's own gate problem — never conflated with a leak verdict."""


# --------------------------------------------------------------------------- #
# layer 1: parsing the scanner's own hit-line grammar (pure, no I/O)
# --------------------------------------------------------------------------- #

#: `label` here is the FULL token `main()` prints before the locator, e.g.
#: "LEAK blob". Order matters only for readability; matching is by exact
#: prefix so "LEAK identity" is never mistaken for "LEAK id...".
SURFACE_LABELS: dict[str, str] = {
    "blob": "LEAK blob",
    "path": "LEAK path",
    "message": "LEAK message",
    "identity": "LEAK identity",
    "tag": "LEAK tag",
}

#: `  ... and 14 more LEAK blob hit(s)` — the exact text `main()` prints for a
#: group past its 200-cap. Matching this is what turns "we captured *some*
#: hits" into a checked claim about whether the capture is COMPLETE.
_ELISION_RE = re.compile(
    r"^\s*\.\.\.\s+and\s+(\d+)\s+more\s+(LEAK \w+)\s+hit\(s\)\s*$"
)


@dataclasses.dataclass
class Hit:
    """One parsed hit, split into the fields the scanner's grammar encodes."""

    surface: str
    family: str  # "shape" (vocabulary-free regex) or "literal" (term-list)
    klass: str
    match: str
    raw: str
    commit: str | None = None
    path: str | None = None
    identity: str | None = None
    role: str | None = None
    ref: str | None = None
    tag_target: str | None = None
    #: Set only for hits attributed as RANGE-introduced (see `run_range_scan`
    #: below): the commit, IN THE SCANNED RANGE, that `git log` shows first
    #: touching this hit's path — the per-hit provenance that makes a RANGE
    #: verdict checkable instead of merely asserted. `None` for a hit that was
    #: never range-attributed (e.g. every hit from plain `scan`/`report`, and
    #: every PRE-EXISTING hit from `gate`).
    introduced_by: str | None = None
    #: How `introduced_by` was derived: "git-log" (found via `git log` on the
    #: hit's path within the range) or "hit-commit" (fell back to the
    #: scanner's own commit field, for surfaces with no path) or "unknown"
    #: (neither yielded a commit). `None` when `introduced_by` is `None`.
    provenance_source: str | None = None

    def dedup_key(self) -> tuple:
        """The COMMIT-INDEPENDENT identity of this hit.

        The scanner appends one blob hit per (commit, path) that reaches a
        matching blob, and one identity hit per (commit, role) that names a
        matching identity — so raw hit count is not leak count: the same
        blob at the same path replayed across 40 commits is ONE distinct leak
        counted 40 times. Grouping on this key is what tells the difference
        apart from the raw count.
        """
        if self.surface in ("blob", "path"):
            return (self.surface, self.path, self.klass, self.match)
        if self.surface == "identity":
            return (self.surface, self.identity, self.klass, self.match)
        if self.surface == "tag":
            return (self.surface, self.ref or self.tag_target, self.klass,
                     self.match)
        return (self.surface, self.klass, self.match)


#: Shape-family marker: `"{shape.name}: {match}"` — built by `shape_hits()` as
#: `f"{s.name}: {m.group()}"`. The matched text is shown IN FULL: shapes are
#: vocabulary-free (a structural/regex class, e.g. "personal-email" or
#: "corporate-home-path"), so there is no private term to protect by hiding it.
_SHAPE_MARKER_RE = re.compile(r"^(?P<name>\S+): (?P<match>.+)$")


def _normalize_how(how: str) -> str:
    """Collapse the scanner's own free-text `how` annotation into a stable
    grouping label. This adds NO new detection and reveals nothing that the
    scanner did not already print — it only buckets wording variants of the
    same match style (e.g. every "...(absolute home path)" suffix collapses
    to one label) so hits group sensibly instead of one bucket per phrasing.
    """
    if "(absolute home path)" in how:
        return "absolute-home-path"
    if "whitespace-stripped" in how:
        return "whitespace-stripped-adjacency"
    if "wrapped across a line break" in how:
        return "wrapped-line-break"
    if how in ("in path", "in symlink target", "plaintext"):
        return how.replace(" ", "-")
    return how.strip().replace(" ", "-") or "unknown"


def _parse_marker(marker: str, raw: str) -> tuple[str, str, str]:
    """Split a hit's marker into `(family, klass, match)`.

    Two, and only two, marker grammars exist in the scanner's own output:

    1. LITERAL family (from `verify_tree`'s own hit key, unmodified):
       `"{rel}:{line}:{redacted_term}:{how}"` — always 4 colon-delimited
       fields with the SECOND one a plain integer line number, e.g.
       `"u32/f.py:3:n*(14):plaintext"` or, for the absolute-home-path check,
       `"u79/f.py:1491:/home/o*(8)/...:plaintext (absolute home path)"`. The
       term itself is REDACTED by the scanner (`_redact`: first character +
       length) before this file ever sees it — `match` here is that already-
       redacted string, never the private term.
    2. SHAPE family (from `shape_hits()`): `"{shape_name}: {match}"` — one
       literal colon-space, matched text shown in full because shapes are
       vocabulary-free regex classes, not list membership.

    Distinguishing them structurally (not by surface) is required because
    surfaces mix both families — `identity_hits` and `blob_hits` each contain
    both literal AND shape hits in the real scan.
    """
    parts = marker.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        _rel, _line, redacted, how = parts
        return "literal", f"literal:{_normalize_how(how)}", redacted
    m = _SHAPE_MARKER_RE.match(marker)
    if m:
        return "shape", m.group("name"), m.group("match")
    raise ValueError(f"malformed hit marker (neither literal nor shape "
                     f"grammar): {raw!r}")


def _split_locator_marker(body: str, raw: str) -> tuple[str, str, str, str]:
    if " :: " not in body:
        raise ValueError(f"malformed hit line (no ' :: ' separator): {raw!r}")
    locator, marker = body.split(" :: ", 1)
    family, klass, match = _parse_marker(marker, raw)
    return locator, family, klass, match


def parse_hit_body(surface: str, body: str, raw: str) -> Hit:
    """Parse the part of a hit AFTER the `LEAK <surface> ` label.

    Shared by the stdout-line parser below and by the JSON loader, which feeds
    it the raw `HistoryReport.*_hits` strings directly — those already ARE
    `body` (the scanner never prepends the `LEAK <surface>` label to the
    dataclass fields; that label is only added when `main()` prints them).
    """
    locator, family, klass, match = _split_locator_marker(body, raw)
    hit = Hit(surface=surface, family=family, klass=klass, match=match, raw=raw)
    if surface == "blob":
        commit, path = locator.split(" ", 1)
        hit.commit, hit.path = commit, path
    elif surface == "path":
        if not locator.startswith("path name "):
            raise ValueError(f"malformed path hit locator: {raw!r}")
        hit.path = locator[len("path name "):]
    elif surface == "message":
        commit, _label = locator.split(" ", 1)
        hit.commit = commit
    elif surface == "identity":
        commit, role, who = locator.split(" ", 2)
        hit.commit, hit.role, hit.identity = commit, role, who
    elif surface == "tag":
        commit, _tag, rest = locator.split(" ", 2)
        hit.commit = commit
        if rest.startswith("TARGET "):
            hit.tag_target = rest[len("TARGET "):]
        else:
            hit.ref = rest
    else:  # pragma: no cover - SURFACE_LABELS is the only caller
        raise ValueError(f"unknown surface {surface!r}")
    return hit


def parse_hit_line(line: str) -> Hit | None:
    """Parse one line of CAPTURED STDOUT. `None` for anything not a hit line."""
    stripped = line.strip()
    if not stripped:
        return None
    for surface, label in SURFACE_LABELS.items():
        prefix = label + " "
        if stripped.startswith(prefix):
            return parse_hit_body(surface, stripped[len(prefix):], line)
    return None


def parse_hit_lines(lines: Iterable[str]) -> tuple[list[Hit], dict[str, int]]:
    """Parse a captured scanner run into hits plus per-label ELISION counts.

    The elision dict is the completeness check: a `... and N more LEAK blob
    hit(s)` line means the capture is a stdout dump subject to `main()`'s
    200-cap, and N hits of that label were never printed at all. Feeding this
    an uncapped source (a `--json` dump) yields an empty dict — there is no
    elision marker to find because nothing was elided.
    """
    hits: list[Hit] = []
    elided: dict[str, int] = {}
    for line in lines:
        m = _ELISION_RE.match(line)
        if m:
            count, label = int(m.group(1)), m.group(2)
            elided[label] = elided.get(label, 0) + count
            continue
        hit = parse_hit_line(line)
        if hit is not None:
            hits.append(hit)
    return hits, elided


def total_elided(elided: dict[str, int]) -> int:
    return sum(elided.values())


# --------------------------------------------------------------------------- #
# grouping: the analytical core — raw count vs distinct leak count
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ClassGroup:
    klass: str
    surfaces: set[str] = dataclasses.field(default_factory=set)
    raw_count: int = 0
    distinct_matches: set[str] = dataclasses.field(default_factory=set)
    distinct_paths: set[str] = dataclasses.field(default_factory=set)
    distinct_commits: set[str] = dataclasses.field(default_factory=set)
    distinct_pairs: set[tuple] = dataclasses.field(default_factory=set)
    samples: list[str] = dataclasses.field(default_factory=list)


def group_hits(hits: list[Hit], *, samples_per_class: int = 5) -> dict[str, ClassGroup]:
    """Group parsed hits by DETECTOR CLASS: the shape name for shape-family
    hits (e.g. "personal-email"), or a `literal:<how>` label for literal/
    vocabulary-family hits (e.g. "literal:plaintext",
    "literal:absolute-home-path") — see `_parse_marker`. A class never
    reveals which private term fired; literal-family classes bucket by MATCH
    STYLE only, exactly the information the scanner itself already prints.

    Within each class, tracks the raw hit count alongside distinct matched
    strings / paths / commits / (path, marker) pairs, and a capped set of
    representative sample lines — so a report never has to choose between
    "show everything" (unreadable at 214 lines) and "show a count" (which is
    the exact blind spot this file exists to close).
    """
    groups: dict[str, ClassGroup] = {}
    for hit in hits:
        g = groups.setdefault(hit.klass, ClassGroup(klass=hit.klass))
        g.raw_count += 1
        g.surfaces.add(hit.surface)
        g.distinct_matches.add(hit.match)
        if hit.path:
            g.distinct_paths.add(hit.path)
        if hit.commit:
            g.distinct_commits.add(hit.commit)
        g.distinct_pairs.add(hit.dedup_key())

    # second pass for samples: prefer variety (distinct matched text) first,
    # then pad with whatever is left so a large, low-variety class still
    # yields the 3-5 samples a reviewer needs to eyeball.
    by_class: dict[str, list[Hit]] = {}
    for hit in hits:
        by_class.setdefault(hit.klass, []).append(hit)
    for klass, g in groups.items():
        seen_matches: set[str] = set()
        samples: list[str] = []
        for hit in by_class[klass]:
            if hit.match not in seen_matches:
                seen_matches.add(hit.match)
                samples.append(hit.raw.strip())
            if len(samples) >= samples_per_class:
                break
        if len(samples) < min(3, g.raw_count):
            for hit in by_class[klass]:
                if hit.raw.strip() in samples:
                    continue
                samples.append(hit.raw.strip())
                if len(samples) >= min(3, g.raw_count):
                    break
        g.samples = samples[:samples_per_class]
    return groups


# --------------------------------------------------------------------------- #
# layer 2: the uncapped scan driver (real git history, real private rules)
# --------------------------------------------------------------------------- #

def _load_module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable
        raise ReportError(f"cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_scanner(mod, *, source: Path, repo: Path, since: str | None,
                ref: str | None, progress) -> dict:
    """Arm the scanner module and run the FULL, uncapped scan.

    `mod` is the already-imported `verify_public_history` module (or, in
    tests, a stand-in with the same four names). Mirrors `main()`'s own arming
    sequence exactly — `_load_builder` / `_terms_or_die` / `load_shapes` /
    `load_message_terms` / `scan_history` — so an arming failure here means
    exactly what it means in `main()`: the gate could not be armed, which is a
    GateError and NEVER a leak verdict. Returns the `HistoryReport` as a plain
    dict (via `dataclasses.asdict`), uncapped.
    """
    builder = mod._load_builder(mod._script_repo_root())
    terms = builder._terms_or_die(source)
    shapes = mod.load_shapes(source)
    message_terms = mod.load_message_terms(source, builder)
    with tempfile.TemporaryDirectory() as work:
        rep = mod.scan_history(
            repo, builder, terms, shapes,
            message_terms=message_terms, workdir=Path(work),
            since=since, ref=ref, progress=progress,
        )
    return dataclasses.asdict(rep)


def cmd_scan(args: argparse.Namespace) -> int:
    scanner_path = Path(args.scanner).resolve()
    mod = _load_module_by_path("_history_gate_scanner_ext", scanner_path)
    source = Path(args.source).resolve()
    repo = Path(args.repo).resolve()

    def progress(msg: str) -> None:
        print(msg, flush=True)
        if args.progress_log:
            with open(args.progress_log, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")

    try:
        payload = run_scanner(mod, source=source, repo=repo, since=args.since,
                              ref=args.ref, progress=progress)
    except Exception as exc:
        # `GateError`/`ExportError` by NAME, exactly as `main()` narrows them —
        # the class lives in the dynamically-loaded module, so there is no
        # import to catch it by. A gate-arming problem is exit 2, NEVER 1;
        # collapsing the two is the confusion the scanner's own docstring
        # warns about.
        if type(exc).__name__ in ("GateError", "ExportError"):
            print(f"history gate could not be armed or could not finish: {exc}",
                  file=sys.stderr)
            return 2
        raise

    Path(args.json).write_text(json.dumps(payload, indent=2, sort_keys=True),
                               encoding="utf-8")
    total_hits = sum(len(payload[k]) for k in payload if k.endswith("_hits"))
    print(f"wrote {total_hits} total hit(s) across "
          f"{sum(1 for k in payload if k.endswith('_hits'))} surface(s) to "
          f"{args.json}")
    return 0


# --------------------------------------------------------------------------- #
# reporting: grouped, classified, markdown
# --------------------------------------------------------------------------- #

_JSON_SURFACE_KEYS = (
    ("blob", "blob_hits"),
    ("path", "path_hits"),
    ("message", "message_hits"),
    ("identity", "identity_hits"),
    ("tag", "tag_hits"),
)


def hits_from_json(data: dict) -> list[Hit]:
    hits: list[Hit] = []
    for surface, key in _JSON_SURFACE_KEYS:
        for body in data.get(key, []):
            hits.append(parse_hit_body(surface, body, body))
    return hits


def render_markdown(
    groups: dict[str, ClassGroup],
    classifications: dict[str, tuple[str, str]],
    *,
    elided: dict[str, int],
    enforce_verdict: tuple[bool, str],
    totals: dict[str, int] | None = None,
) -> str:
    """Render the grouped, classified report.

    `classifications[klass] = (verdict, rationale)` with `verdict` one of
    `REAL-TRACE` / `FALSE-POSITIVE`. Raises `ReportError` if any class present
    in `groups` has no classification or an empty rationale — a report that
    can render half-classified defeats the reason this file exists (AC2).
    `enforce_verdict = (True, reason)` / `(False, reason)` renders the single
    `RECOMMENDATION: ENFORCE = YES|NO` line; an empty reason also raises
    (AC4).
    """
    missing = [k for k in groups if k not in classifications]
    if missing:
        raise ReportError(
            f"no classification for class(es) {sorted(missing)}; every class "
            "with hits must be explicitly REAL-TRACE or FALSE-POSITIVE before "
            "a report is rendered")
    for klass, (verdict, rationale) in classifications.items():
        if klass not in groups:
            continue
        if verdict not in ("REAL-TRACE", "FALSE-POSITIVE"):
            raise ReportError(
                f"class {klass!r} has verdict {verdict!r}, must be "
                "REAL-TRACE or FALSE-POSITIVE")
        if not rationale.strip():
            raise ReportError(f"class {klass!r} has an empty rationale")

    enforce_yes, enforce_reason = enforce_verdict
    if not enforce_reason.strip():
        raise ReportError("enforce recommendation needs a non-empty reason")

    elided_total = total_elided(elided)
    lines: list[str] = []
    lines.append("# History gate hit report")
    lines.append("")
    if totals:
        lines.append("Captured totals: " +
                     ", ".join(f"{v} {k}" for k, v in totals.items()))
    if elided_total:
        lines.append(f"**INCOMPLETE** — {elided_total} hit(s) elided by the "
                     f"200-cap in the captured source: {elided}")
    else:
        lines.append("**COMPLETE** — no hits elided; every hit below was "
                     "captured, none were cut by a print cap.")
    lines.append("")

    for klass in sorted(groups):
        g = groups[klass]
        verdict, rationale = classifications[klass]
        lines.append(f"## Class: `{klass}` — {verdict}")
        lines.append("")
        lines.append(f"- raw hits: {g.raw_count}")
        lines.append(f"- distinct matched strings: {len(g.distinct_matches)}")
        lines.append(f"- distinct paths: {len(g.distinct_paths)}")
        lines.append(f"- distinct commits: {len(g.distinct_commits)}")
        lines.append(f"- distinct (path/identity, match) pairs: "
                     f"{len(g.distinct_pairs)}")
        lines.append(f"- surfaces: {', '.join(sorted(g.surfaces))}")
        lines.append("")
        lines.append(f"Classification rationale: {rationale}")
        lines.append("")
        lines.append("Samples:")
        for sample in g.samples:
            lines.append(f"    {sample}")
        lines.append("")

    lines.append(
        f"RECOMMENDATION: ENFORCE = {'YES' if enforce_yes else 'NO'} — "
        f"{enforce_reason}")
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    if args.json:
        data = json.loads(Path(args.json).read_text(encoding="utf-8"))
        hits = hits_from_json(data)
        elided: dict[str, int] = {}
        totals = {surface: len(data.get(key, []))
                 for surface, key in _JSON_SURFACE_KEYS}
    else:
        lines = Path(args.stdout).read_text(encoding="utf-8").splitlines()
        hits, elided = parse_hit_lines(lines)
        totals = {}
        for hit in hits:
            totals[hit.surface] = totals.get(hit.surface, 0) + 1

    if args.check_totals:
        expected = {}
        for pair in args.check_totals.split(","):
            k, v = pair.split("=")
            expected[k] = int(v)
        actual = {k: totals.get(k, 0) for k in expected}
        elided_total = total_elided(elided)
        if actual != expected or elided_total:
            print(f"TOTALS CHECK FAILED: expected={expected} actual={actual} "
                  f"cap_elided={elided_total}", file=sys.stderr)
            return 1
        print(f"totals match: {actual}; cap_elided=0 (complete capture)")
        return 0

    groups = group_hits(hits)

    if args.classify:
        if not args.enforce or not args.enforce_reason:
            print("error: --classify requires --enforce {yes,no} and "
                  "--enforce-reason", file=sys.stderr)
            return 2
        raw_classifications = json.loads(
            Path(args.classify).read_text(encoding="utf-8"))
        classifications = {
            klass: (entry["verdict"], entry["rationale"])
            for klass, entry in raw_classifications.items()
        }
        enforce_verdict = (args.enforce == "yes", args.enforce_reason)
        try:
            markdown = render_markdown(
                groups, classifications, elided=elided,
                enforce_verdict=enforce_verdict, totals=totals)
        except ReportError as exc:
            print(f"report error: {exc}", file=sys.stderr)
            return 2
        print(markdown)
        return 0

    print(json.dumps(
        {k: {"raw_count": g.raw_count,
             "distinct_matches": len(g.distinct_matches),
             "distinct_paths": len(g.distinct_paths),
             "distinct_commits": len(g.distinct_commits),
             "distinct_pairs": len(g.distinct_pairs),
             "surfaces": sorted(g.surfaces),
             "samples": g.samples}
         for k, g in groups.items()},
        indent=2, sort_keys=True))
    return 0



# --------------------------------------------------------------------------- #
# layer 3: range attribution — a verdict about the RANGE, not the tree
# --------------------------------------------------------------------------- #
#
# `verify_public_history.py --ref X --since Y` scans the whole tip TREE at X
# regardless of Y: its verdict is a property of the repository, not of the
# commits being pushed (measured: a single-commit range and a completely
# different 7-file range produced byte-identical 19-blob-hit, 195-extra-file
# verdicts). The scanner itself cannot be patched (private, drop-classified).
#
# The fix asks it TWICE instead — once at `ref` (the tip of what's being
# pushed) and once at the merge-base of `since`/`ref` (the last state the
# other side already had) — and attributes each hit and each extra/missing
# file by set difference on `Hit.dedup_key()` / path identity: anything in
# the tip scan not also in the base scan was INTRODUCED BY the range;
# anything in both was already PRE-EXISTING before the range started. That
# split is what turns "the tree has 19 leaks" into "this push adds 1 leak; 18
# were already there" — two different answers this file must never let get
# read as each other.

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True)


def _git_out(repo: Path, *args: str) -> str:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise ReportError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()}")
    return proc.stdout.strip()


def _is_commit_ish(repo: Path, rev: str) -> bool:
    """Whether `rev` resolves to a commit, so it may legally sit on the
    excluded side of a `base..ref` revision range. False for a tree object
    such as the well-known empty-tree SHA (`4b825dc6...`) that a brand-new
    remote ref falls back to as `since` — that hash is a valid git object,
    so `rev-parse` accepts it, but it is not a commit `rev-list`/`log` can
    exclude by.
    """
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return proc.returncode == 0


def merge_base(repo: Path, since: str, ref: str) -> tuple[str, str]:
    """The base of the range being scanned, and how it was derived.

    `base_kind` is `"merge-base"` in the normal case. If `since`/`ref` share
    no ancestry, `git merge-base` fails and this falls back to `since`
    resolved directly. That fallback is labelled `"since"` when it still
    resolves to a commit (unrelated histories), or `"no-common-history"`
    when it does not — e.g. `since` is the empty-tree SHA a brand-new remote
    ref uses to mean "nothing pre-existing". The label never claims
    `merge-base` succeeded when it did not, and `"no-common-history"` tells
    callers the base is not usable on the excluded side of a revision range.
    """
    proc = _git(repo, "merge-base", since, ref)
    if proc.returncode == 0:
        return proc.stdout.strip(), "merge-base"
    base = _git_out(repo, "rev-parse", since)
    kind = "since" if _is_commit_ish(repo, base) else "no-common-history"
    return base, kind


def range_commits(repo: Path, base: str | None, ref: str) -> list[str]:
    """Commits in `base..ref`, or ALL commits reachable from `ref` when
    `base` is `None` — the brand-new-ref/no-common-history case, where
    there is no commit to exclude by, so the whole ref IS the range.
    """
    if base is None:
        out = _git_out(repo, "rev-list", "--reverse", ref)
    else:
        out = _git_out(repo, "rev-list", "--reverse", f"{base}..{ref}")
    return [line for line in out.splitlines() if line]


def range_paths(repo: Path, base: str | None, ref: str) -> list[str]:
    """Paths changed in `base..ref`, or every path in `ref`'s tree when
    `base` is `None` (no-common-history: everything in `ref` is new)."""
    if base is None:
        out = _git_out(repo, "ls-tree", "-r", "--name-only", ref)
    else:
        out = _git_out(repo, "diff", "--name-only", base, ref)
    return [line for line in out.splitlines() if line]


def first_touching_commit(repo: Path, base: str | None, ref: str,
                          path: str) -> str | None:
    """The first commit IN THE RANGE `base..ref` that touches `path` — the
    per-hit provenance value that makes a RANGE attribution checkable against
    plain `git log` instead of merely asserted by this tool. When `base` is
    `None` (no-common-history), the range is all of `ref`'s history, so this
    looks at `ref`'s full history for `path` instead of a `base..ref` range.
    """
    if base is None:
        out = _git_out(repo, "log", "--reverse", "--format=%H", ref,
                       "--", path)
    else:
        out = _git_out(repo, "log", "--reverse", "--format=%H",
                       f"{base}..{ref}", "--", path)
    lines = [line for line in out.splitlines() if line]
    return lines[0] if lines else None


def attribute_hits(tip_hits: list[Hit],
                   base_hits: list[Hit]) -> tuple[list[Hit], list[Hit]]:
    """Split the tip scan's hits into `(introduced, preexisting)` by set
    difference on `Hit.dedup_key()` against the base scan's hits. A hit whose
    dedup key is not present at the base was introduced somewhere in
    `base..ref`; one whose key is already present at the base was there
    before the range started.
    """
    base_keys = {h.dedup_key() for h in base_hits}
    introduced = [h for h in tip_hits if h.dedup_key() not in base_keys]
    preexisting = [h for h in tip_hits if h.dedup_key() in base_keys]
    return introduced, preexisting


def attribute_extra_files(tip_extra: list[str], base_extra: list[str],
                          range_paths_: list[str]) -> dict[str, list[str]]:
    """The same range/pre-existing split, applied to a flat file-path list
    (`extra_files` or `missing_files`) instead of `Hit` objects — per the
    resolved intake assumption that the "extra file(s)" count gets the same
    treatment as hits, for consistency.

    `range_extra_untouched` flags paths that are new-at-tip-vs-base but that
    the range's own diff (`range_paths_`, from `git diff --name-only
    base..ref`) never touched — e.g. a file the scanner newly considers
    "extra" for a reason unrelated to any change in this push (a rule change
    on the base side, or a rename it doesn't track). Those are still counted
    as range-introduced (the base scan genuinely didn't have them), but are
    flagged separately so they don't silently read as "this push added this
    file" when `git diff` disagrees.
    """
    base_set = set(base_extra)
    range_set = set(range_paths_)
    range_extra = [p for p in tip_extra if p not in base_set]
    preexisting_extra = [p for p in tip_extra if p in base_set]
    range_extra_untouched = [p for p in range_extra if p not in range_set]
    return {
        "range_extra": range_extra,
        "preexisting_extra": preexisting_extra,
        "range_extra_untouched": range_extra_untouched,
    }


@dataclasses.dataclass
class RangeVerdict:
    """The result of asking the scanner about a RANGE (`since`..`ref`)
    instead of a tree. Every *_extra/_missing list below is a plain path
    list; every *_hits list is `Hit` objects (range-introduced ones carry
    `introduced_by`/`provenance_source`)."""

    ref: str
    since: str
    base: str
    base_kind: str
    range_commit_count: int
    tip_payload: dict
    base_payload: dict
    tip_hits: list[Hit]
    tip_extra: list[str]
    tip_missing: list[str]
    introduced_hits: list[Hit]
    preexisting_hits: list[Hit]
    range_extra: list[str]
    preexisting_extra: list[str]
    range_extra_untouched: list[str]
    range_missing: list[str]
    preexisting_missing: list[str]
    range_missing_untouched: list[str]

    @property
    def failed(self) -> bool:
        """PASSED means zero hits and zero extra/missing files were
        INTRODUCED BY this range — a non-zero pre-existing backlog at the
        base does not fail the range verdict; it is reported, not enforced,
        by the default `--fail-on range`."""
        return bool(self.introduced_hits or self.range_extra
                   or self.range_missing)


def run_range_scan(mod, *, source: Path, repo: Path, ref: str, since: str,
                   progress) -> RangeVerdict:
    """Ask the scanner TWICE (tip at `ref`, base at `merge-base(since, ref)`)
    via the existing `run_scanner`, then attribute every hit and every extra/
    missing file to the range or to pre-existing backlog. Reuses
    `run_scanner` UNCHANGED for both calls — this file adds attribution
    around it, it does not alter how the scanner itself is armed or run.
    """
    base, base_kind = merge_base(repo, since, ref)
    # `range_base` is what `range_commits`/`range_paths`/`first_touching_commit`
    # are allowed to put on the excluded side of a `base..ref` revision range.
    # When there is no common history (`base_kind == "no-common-history"`,
    # e.g. `since` fell back to the empty-tree SHA for a brand-new remote
    # ref), `base` is a tree object, not a commit, and cannot appear there —
    # so `range_base` is `None` and those helpers walk all of `ref` instead,
    # which is the correct answer anyway: with nothing pre-existing, the
    # whole ref IS the range.
    range_base = None if base_kind == "no-common-history" else base
    commits = range_commits(repo, range_base, ref)
    r_paths = range_paths(repo, range_base, ref)

    tip_payload = run_scanner(mod, source=source, repo=repo, since=None,
                              ref=ref, progress=progress)
    base_payload = run_scanner(mod, source=source, repo=repo, since=None,
                               ref=base, progress=progress)

    tip_hits = hits_from_json(tip_payload)
    base_hits = hits_from_json(base_payload)
    introduced, preexisting = attribute_hits(tip_hits, base_hits)

    for hit in introduced:
        commit = None
        prov_source = "unknown"
        if hit.path is not None:
            commit = first_touching_commit(repo, range_base, ref, hit.path)
            if commit:
                prov_source = "git-log"
        if commit is None and hit.commit:
            commit = hit.commit
            prov_source = "hit-commit"
        hit.introduced_by = commit
        hit.provenance_source = prov_source if commit else "unknown"

    tip_extra = list(tip_payload.get("extra_files", []))
    base_extra = list(base_payload.get("extra_files", []))
    extra = attribute_extra_files(tip_extra, base_extra, r_paths)

    tip_missing = list(tip_payload.get("missing_files", []))
    base_missing = list(base_payload.get("missing_files", []))
    missing = attribute_extra_files(tip_missing, base_missing, r_paths)

    return RangeVerdict(
        ref=ref, since=since, base=base, base_kind=base_kind,
        range_commit_count=len(commits),
        tip_payload=tip_payload, base_payload=base_payload,
        tip_hits=tip_hits, tip_extra=tip_extra, tip_missing=tip_missing,
        introduced_hits=introduced, preexisting_hits=preexisting,
        range_extra=extra["range_extra"],
        preexisting_extra=extra["preexisting_extra"],
        range_extra_untouched=extra["range_extra_untouched"],
        range_missing=missing["range_extra"],
        preexisting_missing=missing["preexisting_extra"],
        range_missing_untouched=missing["range_extra_untouched"],
    )


def _surface_counts(hits: list[Hit]) -> dict[str, int]:
    counts = {surface: 0 for surface, _ in _JSON_SURFACE_KEYS}
    for hit in hits:
        counts[hit.surface] = counts.get(hit.surface, 0) + 1
    return counts


def _format_surface_counts(counts: dict[str, int]) -> str:
    return (", ".join(f"{counts.get(surface, 0)} {surface}"
                      for surface, _ in _JSON_SURFACE_KEYS)
           + " hit(s)")


def render_range_verdict(verdict: RangeVerdict) -> str:
    """Render the RANGE verdict. The summary line names BOTH the
    range-introduced and the pre-existing-at-base numbers explicitly, so
    neither can be read as the other (AC2) — and it is the LAST line printed,
    after the full RANGE hit list, so a caller that only keeps the tail of
    this output (e.g. a 600-character truncation) still keeps the verdict and
    at least the most recent hit(s) (AC3).
    """
    lines: list[str] = []
    if verdict.base_kind == "no-common-history":
        lines.append(
            f"history gate: scanned ALL of {verdict.ref} "
            f"({verdict.range_commit_count} commit(s)); no common history "
            f"with since={verdict.since} (resolved to {verdict.base}, not a "
            "commit) — the whole ref is treated as the range")
    else:
        lines.append(
            f"history gate: scanned {verdict.base}..{verdict.ref} "
            f"({verdict.range_commit_count} commit(s)); base {verdict.base} "
            f"({verdict.base_kind})")

    pre_counts = _surface_counts(verdict.preexisting_hits)
    lines.append(
        "history gate: PRE-EXISTING at base: "
        + _format_surface_counts(pre_counts)
        + f"; {len(verdict.preexisting_extra)} extra, "
          f"{len(verdict.preexisting_missing)} missing file(s) "
          "[backlog, tracked separately — not this push]")

    tip_counts = _surface_counts(verdict.tip_hits)
    lines.append(
        f"history gate: TIP-WIDE total at {verdict.ref}: "
        + _format_surface_counts(tip_counts)
        + f"; {len(verdict.tip_extra)} extra, "
          f"{len(verdict.tip_missing)} missing file(s)")

    range_lines: list[str] = []
    for hit in verdict.introduced_hits:
        prov = f"{hit.introduced_by or 'unknown'} via {hit.provenance_source or 'unknown'}"
        range_lines.append(
            f"  RANGE {hit.surface} {hit.raw.strip()}  [introduced by {prov}]")
    for path in verdict.range_extra:
        range_lines.append(f"  RANGE EXTRA {path}")
    for path in verdict.range_missing:
        range_lines.append(f"  RANGE MISSING {path}")

    if range_lines:
        lines.append(
            f"history gate: RANGE HITS (introduced by "
            f"{verdict.base}..{verdict.ref}) - {len(range_lines)}:")
        lines.extend(range_lines)

    intro_counts = _surface_counts(verdict.introduced_hits)
    total_preexisting = sum(pre_counts.values())
    lines.append(
        f"history gate: RANGE VERDICT: "
        f"{'FAILED' if verdict.failed else 'PASSED'} - "
        + _format_surface_counts(intro_counts)
        + " introduced by this range; "
          f"{len(verdict.range_extra)} extra, "
          f"{len(verdict.range_missing)} missing file(s) introduced by this "
          "range; "
          f"{total_preexisting} hit(s) and "
          f"{len(verdict.preexisting_extra)} extra file(s) pre-existing at "
          "base (NOT this range)")

    return "\n".join(lines) + "\n"


def _verdict_payload(verdict: RangeVerdict) -> dict:
    return {
        "ref": verdict.ref,
        "since": verdict.since,
        "base": verdict.base,
        "base_kind": verdict.base_kind,
        "range_commit_count": verdict.range_commit_count,
        "failed": verdict.failed,
        "introduced_hits": [dataclasses.asdict(h) for h in verdict.introduced_hits],
        "preexisting_hits": [dataclasses.asdict(h) for h in verdict.preexisting_hits],
        "range_extra": verdict.range_extra,
        "preexisting_extra": verdict.preexisting_extra,
        "range_extra_untouched": verdict.range_extra_untouched,
        "range_missing": verdict.range_missing,
        "preexisting_missing": verdict.preexisting_missing,
        "range_missing_untouched": verdict.range_missing_untouched,
        "tip_payload": verdict.tip_payload,
        "base_payload": verdict.base_payload,
    }


def _render_range_detail(verdict: RangeVerdict) -> str:
    """The UNCAPPED per-hit detail for `--detail`: every range-introduced and
    pre-existing hit/extra/missing entry, one per line — never just the
    summary line, and never capped, regardless of how many there are (AC3)."""
    lines: list[str] = []
    for hit in verdict.introduced_hits:
        prov = f"{hit.introduced_by or 'unknown'} via {hit.provenance_source or 'unknown'}"
        lines.append(f"RANGE {hit.surface} {hit.raw.strip()}  [introduced by {prov}]")
    for path in verdict.range_extra:
        lines.append(f"RANGE EXTRA {path}")
    for path in verdict.range_missing:
        lines.append(f"RANGE MISSING {path}")
    for hit in verdict.preexisting_hits:
        lines.append(f"PRE-EXISTING {hit.surface} {hit.raw.strip()}")
    for path in verdict.preexisting_extra:
        lines.append(f"PRE-EXISTING EXTRA {path}")
    for path in verdict.preexisting_missing:
        lines.append(f"PRE-EXISTING MISSING {path}")
    return "\n".join(lines) + ("\n" if lines else "")


def cmd_gate(args: argparse.Namespace) -> int:
    scanner_path = Path(args.scanner).resolve()
    mod = _load_module_by_path("_history_gate_scanner_ext", scanner_path)
    source = Path(args.source).resolve()
    repo = Path(args.repo).resolve()

    def progress(msg: str) -> None:
        print(msg, flush=True)
        if args.progress_log:
            with open(args.progress_log, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")

    try:
        verdict = run_range_scan(mod, source=source, repo=repo, ref=args.ref,
                                 since=args.since, progress=progress)
    except Exception as exc:
        # Same exit-2-by-class-name contract as `cmd_scan`: a gate-arming
        # problem (from either the tip or the base scan) is never conflated
        # with a leak verdict.
        if type(exc).__name__ in ("GateError", "ExportError"):
            print(f"history gate could not be armed or could not finish: {exc}",
                  file=sys.stderr)
            return 2
        raise

    # The RANGE verdict is printed LAST (see render_range_verdict) so it is
    # the part a tail-truncating caller keeps.
    print(render_range_verdict(verdict), end="")

    if args.json:
        Path(args.json).write_text(
            json.dumps(_verdict_payload(verdict), indent=2, sort_keys=True),
            encoding="utf-8")
    if args.detail:
        Path(args.detail).write_text(_render_range_detail(verdict),
                                     encoding="utf-8")

    if args.fail_on == "tip":
        failed = bool(verdict.introduced_hits or verdict.preexisting_hits
                     or verdict.range_extra or verdict.preexisting_extra
                     or verdict.range_missing or verdict.preexisting_missing)
    else:
        failed = verdict.failed
    return 1 if failed else 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="history_gate_hit_report.py")
    sub = ap.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser(
        "scan", help="run the private scanner UNCAPPED and dump JSON")
    p_scan.add_argument("--scanner", required=True,
                        help="path to verify_public_history.py")
    p_scan.add_argument("--source", required=True,
                        help="private source repo holding the shape/term rules")
    p_scan.add_argument("--repo", required=True,
                        help="the published repository to scan")
    p_scan.add_argument("--ref", default=None)
    p_scan.add_argument("--since", default=None)
    p_scan.add_argument("--json", required=True, help="output JSON path")
    p_scan.add_argument("--progress-log", default=None)
    p_scan.set_defaults(func=cmd_scan)

    p_report = sub.add_parser(
        "report", help="render the grouped report from a scan capture")
    src = p_report.add_mutually_exclusive_group(required=True)
    src.add_argument("--json", help="uncapped JSON from `scan`")
    src.add_argument("--stdout", help="captured stdout text (may be capped)")
    p_report.add_argument("--check-totals", default=None,
                          help="e.g. blob=214,message=33,identity=32,path=0,tag=0")
    p_report.add_argument(
        "--classify", default=None,
        help="JSON file: {class: {\"verdict\": REAL-TRACE|FALSE-POSITIVE, "
             "\"rationale\": str}}; when given, renders classified markdown "
             "via render_markdown() instead of the raw group-stats JSON")
    p_report.add_argument("--enforce", choices=["yes", "no"], default=None,
                          help="required with --classify: the ENFORCE verdict")
    p_report.add_argument("--enforce-reason", default=None,
                          help="required with --classify: non-empty reason")
    p_report.set_defaults(func=cmd_report)

    p_gate = sub.add_parser(
        "gate",
        help="scan a RANGE (since..ref), not the whole tip tree, and emit a "
             "verdict that distinguishes hits INTRODUCED BY the range from "
             "hits already PRE-EXISTING at the merge base")
    p_gate.add_argument("--scanner", required=True,
                        help="path to verify_public_history.py")
    p_gate.add_argument("--source", required=True,
                        help="private source repo holding the shape/term rules")
    p_gate.add_argument("--repo", required=True,
                        help="the published repository to scan")
    p_gate.add_argument("--ref", required=True, help="tip of the range")
    p_gate.add_argument("--since", required=True,
                        help="the other end of the range; merge-base(since, "
                             "ref) is the base the range is diffed against")
    p_gate.add_argument("--json", default=None,
                        help="write the full structured range verdict here")
    p_gate.add_argument("--detail", default=None,
                        help="write the uncapped per-hit RANGE/PRE-EXISTING "
                             "detail here (never just the summary line)")
    p_gate.add_argument("--progress-log", default=None)
    p_gate.add_argument(
        "--fail-on", choices=["range", "tip"], default="range",
        help="exit 1 on range-introduced hits only (default — what a "
             "pre-push gate should refuse on), or on any tip-wide hit "
             "(escape hatch for a full-history audit)")
    p_gate.set_defaults(func=cmd_gate)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
