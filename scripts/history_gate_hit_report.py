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
    p_report.set_defaults(func=cmd_report)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
