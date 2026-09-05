#!/usr/bin/env python3
"""Verify the repository tree against RELEASE_MANIFEST.txt.

The manifest pins every tracked file's content with a SHA-256, like a lockfile
for the tree itself: a review of any change is a review of a visible manifest
row. This script checks, in both directions, that the checkout and the manifest
agree:

  * every tracked file is listed, with a matching hash;
  * every listed file exists in the tree, with a matching hash;
  * the manifest itself is the one file that carries no row (it cannot pin its
    own content).

"Tracked but not listed" means two opposite things in the two trees this script
runs in, and collapsing them cost the exit code its signal. On a RELEASE
checkout every tracked file ships, so a missing row is a defect. In the SOURCE
repo most of what is tracked is classified `drop` and is unpinned ON PURPOSE —
~93 paths — so the script was already exit 1 before anything went wrong, and a
genuinely bad merge resolution (a real hash mismatch) moved nothing an automated
caller could see. Three categories now, deliberately kept apart, and the
classification is consulted FIRST so it decides before completeness is asked
about at all:

  * a path `EXPORT_CLASSIFICATION.txt` marks `drop`, or that matches no rule at
    all, is CORRECTLY unpinned. Never a problem, never fatal — not even under
    `--strict` — and counted in the success line as the healthy state it is.
  * a genuinely unlisted SHIP file — or any unlisted file in a tree carrying no
    classification — says the manifest is incomplete. It WARNS and does not set
    the exit code; `--strict` makes it fatal, and that is what CI runs on the
    released tree, where completeness is a hard invariant.
  * a hash mismatch, a row pointing at a file that is gone, or a row pinning a
    path that must not ship, says the tree is not the tree the manifest was
    reviewed against. Always exit 1.

`--write` regenerates the manifest from the current tree. That is safe ONLY in a
tree where every tracked file ships — a release checkout, which is the tree this
script is shipped to verify. It is NOT safe in the source repo, where most of
what is tracked is classified `drop`:

  * the manifest is itself a SHIPPED file, so a row publishes its path. The
    classification says so in as many words — "sha256 + path for every approved
    file, and nothing else — never a dropped name". Regenerating from the source
    tree writes ~93 dropped paths (private guards, an employer-term inventory,
    adverse-legal working notes) into a public inventory, names and content
    hashes both.
  * the manifest is not an inventory only, it is the APPROVAL LEDGER that
    `scripts/build_public_export.py` gates on. A file whose bytes changed loses
    its pin until a human re-approves it through `scripts/export_guard.py`,
    which runs the term scan at that moment. Rehashing the tree forges those
    approvals in bulk, with no scan and nobody looking.

So when `EXPORT_CLASSIFICATION.txt` is present beside the tree, `--write`
REFUSES and names every path it would have wrongly pinned, and the remedy this
script prints points at `scripts/export_guard.py approve <path>` — the one
sanctioned write path — never at `--write`. Where no classification exists there
is nothing to get wrong: `--write` behaves as it always did, and it is what the
remedy recommends.

The classification is read with the builder's own parser
(`scripts/build_public_export.py`), never a second copy: two readers of one
grammar is how a guard comes to disagree with the gate it protects.

Standard library + `git` only. Exit 0 when the tree matches, 1 on a mismatch (or
on an unlisted ship file under `--strict`), 2 when `--write` refuses.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

MANIFEST_NAME = "RELEASE_MANIFEST.txt"
CLASSIFICATION_NAME = "EXPORT_CLASSIFICATION.txt"
BUILDER_REL = "scripts/build_public_export.py"

APPROVE_CMD = "python scripts/export_guard.py approve <path> [...]"

HEADER = """\
# RELEASE_MANIFEST.txt — the release file inventory.
#
# One row per file: `<sha256>  <path>`. CI verifies a checkout against this
# file with scripts/check_release_manifest.py, so every content change is
# visible here, like a lockfile. Rows are sorted by path.
"""


def repo_root() -> Path:
    return Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent, text=True).strip())


def tracked_files(root: Path) -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True)
    return sorted(p for p in out.split("\0") if p)


def hash_path(root: Path, rel: str) -> str:
    """SHA-256 of the file's bytes; for a symlink, of its target string."""
    p = root / rel
    if p.is_symlink():
        return hashlib.sha256(os.readlink(p).encode("utf-8")).hexdigest()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    """``{path: sha256}``. A row that cannot be parsed is an error, not a skip."""
    out: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.rstrip("\n")
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        digest, sep, path = s.partition("  ")
        if not sep or not path or len(digest) != 64 or any(
                c not in "0123456789abcdef" for c in digest):
            raise SystemExit(
                f"{MANIFEST_NAME}:{lineno}: cannot parse {line!r} — every row "
                "is `<sha256>  <path>`")
        if path in out:
            raise SystemExit(f"{MANIFEST_NAME}:{lineno}: {path} is listed twice")
        out[path] = digest
    return out


class Refused(Exception):
    """A refusal to act, never a silent fallback to acting anyway."""


class Unpinnable:
    """Which paths in ``root`` must never be pinned, and why.

    ``reason(rel)`` returns a short phrase when ``rel`` may not carry a manifest
    row, else None. Two kinds qualify, and both fail closed:

      * `drop` — the classification says it does not ship, so its name must not
        appear in a file that does;
      * matching no rule at all — the classification's whole design is that an
        unknown file is refused rather than defaulted. A path nobody has
        classified cannot be shown to ship, so it cannot be pinned either.
    """

    def __init__(self, verdicts: dict[str, str]):
        self._verdicts = verdicts

    def reason(self, rel: str) -> str | None:
        return self._verdicts.get(rel)

    def __len__(self) -> int:
        return len(self._verdicts)


def load_unpinnable(root: Path, builder_root: Path | None = None
                    ) -> Unpinnable | None:
    """Classify ``root``, or None when the tree carries no classification.

    None is the RELEASE case and it is a real answer, not a fallback: a tree
    with no `EXPORT_CLASSIFICATION.txt` has no dropped files to protect, which
    is exactly the tree this script ships to. Present-but-unparseable is a
    refusal instead — if the classification exists we cannot tell ship from
    drop without it, and guessing is the failure being fixed.

    The classification TEXT comes from ``root``, because it describes that tree.
    The PARSER comes from this script's own repo, matching what
    `scripts/export_guard.py` does: the grammar is universal, so a `--root`
    pointing elsewhere is still read by the one implementation the export gate
    uses. Two readers of one grammar is how a guard comes to disagree with the
    gate it protects.
    """
    if not (root / CLASSIFICATION_NAME).exists():
        return None
    if builder_root is None:
        try:
            builder_root = repo_root()
        except (subprocess.CalledProcessError, OSError) as exc:
            raise Refused(
                f"{CLASSIFICATION_NAME} is present in {root}, but this script "
                f"is not inside a git repository, so {BUILDER_REL} cannot be "
                f"located to parse it: {exc}") from exc
    builder_path = builder_root / BUILDER_REL
    if not builder_path.exists():
        raise Refused(
            f"{CLASSIFICATION_NAME} is present in {root} but {BUILDER_REL} is "
            f"not in {builder_root}, so the classification cannot be parsed "
            "with the same grammar the export gate uses. A tree that has a "
            "ship/drop split we cannot read is not a tree to pin.")
    spec = importlib.util.spec_from_file_location(
        "_nh_build_public_export", builder_path)
    builder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = builder
    spec.loader.exec_module(builder)

    rules = builder.parse_classification(
        (root / CLASSIFICATION_NAME).read_text(encoding="utf-8"))
    cls = builder.classify(rules, tracked_files(root))
    verdicts = {rel: "classified `drop`" for rel in cls.dropped}
    verdicts.update({rel: f"matches no rule in {CLASSIFICATION_NAME}"
                     for rel in cls.unclassified})
    return Unpinnable(verdicts)


def _previous_rows(manifest_path: Path) -> dict[str, str]:
    """The rows the manifest holds now, or empty when it cannot be read.

    `--write` is the documented way out of a manifest left mid-merge, so it has
    to survive reading a file that does not parse — conflict markers and all.
    `parse_manifest` raises `SystemExit` on the first bad row, which is correct
    for the check side and fatal for this one, so a failure here costs only the
    comparison note below and never the regeneration itself.
    """
    if not manifest_path.exists():
        return {}
    try:
        return parse_manifest(manifest_path.read_text(encoding="utf-8"))
    except (SystemExit, OSError, UnicodeDecodeError):
        return {}


def _warn_if_nearly_every_row_changed(previous: dict[str, str],
                                      rows: dict[str, str]) -> None:
    """Say so when a regeneration rewrites most of the manifest.

    A dependency bump or a docs sweep can legitimately touch a large share of
    the rows, so this is a warning and never a refusal. It exists because the
    one cause that is never legitimate, a checkout whose line endings differ
    from the ones the rows were hashed over, looks exactly like a huge honest
    diff and is otherwise silent.
    """
    if not previous:
        return
    changed = sum(1 for rel, digest in rows.items()
                  if previous.get(rel) not in (None, digest))
    if changed * 4 > len(previous):
        print(f"  NOTE: {changed} of {len(previous)} existing row(s) changed. "
              "That is usual for a dependency bump or a wide sweep. If you did "
              "not expect it, check your checkout's line endings "
              "(git config core.autocrlf false).", file=sys.stderr)


def write_manifest(root: Path) -> int:
    tracked = [rel for rel in tracked_files(root) if rel != MANIFEST_NAME]
    unpinnable = load_unpinnable(root)
    manifest_path = root / MANIFEST_NAME
    previous = _previous_rows(manifest_path)

    if unpinnable is not None:
        refused = [(rel, why) for rel in tracked
                   if (why := unpinnable.reason(rel)) is not None]
        if refused:
            print(f"--write: REFUSED — regenerating {MANIFEST_NAME} from this "
                  f"tree would pin {len(refused)} path(s) that must not ship. "
                  f"{MANIFEST_NAME} is itself a shipped file, so a row "
                  "publishes the path it names:", file=sys.stderr)
            for rel, why in refused:
                print(f"  {rel} — {why}", file=sys.stderr)
            print(f"\n  {MANIFEST_NAME} is also the approval ledger the export "
                  "gate enforces, and rehashing the tree would forge an "
                  "approval for every changed file without the term scan that "
                  "approval exists to run. Pin deliberately instead:\n"
                  f"    {APPROVE_CMD}", file=sys.stderr)
            return 2

    rows = {rel: hash_path(root, rel) for rel in tracked}
    body = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(rows.items()))
    # newline="\n" so the bytes are identical on every platform. Without it the
    # write goes through the platform's text layer, which on Windows makes every
    # row CRLF; the compare side reads back through the same layer and stays
    # quiet, so the tool looks fine while git reports the whole file as changed.
    manifest_path.write_text(HEADER + body, encoding="utf-8", newline="\n")
    _warn_if_nearly_every_row_changed(previous, rows)
    print(f"{MANIFEST_NAME}: wrote {len(rows)} row(s)")
    return 0


def check_manifest(root: Path, *, strict: bool = False) -> int:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"FAIL: {MANIFEST_NAME} not found at the repository root",
              file=sys.stderr)
        return 1
    listed = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    tracked = tracked_files(root)
    unpinnable = load_unpinnable(root)

    def unpinnable_reason(rel: str) -> str | None:
        return None if unpinnable is None else unpinnable.reason(rel)

    # Three buckets, kept apart rather than one tagged list so that no future
    # edit can append a real mismatch to a non-fatal one by accident:
    # `problems` fail the run; `unlisted` fails it only under --strict; and the
    # correctly-unpinned are counted, never reported as either.
    problems: list[str] = []
    unlisted: list[str] = []
    if MANIFEST_NAME in listed:
        problems.append(f"{MANIFEST_NAME} lists itself; it cannot pin its own "
                        "content")
    tracked_set = set(tracked) - {MANIFEST_NAME}
    listed_set = set(listed) - {MANIFEST_NAME}

    # Tracked and unpinned is the NORMAL state for a file that does not ship —
    # it is what the classification asks for, not a defect. Reporting it as one
    # is what buried the real signal under ~93 rows of healthy condition, and a
    # check that cannot tell the two apart gets its advice followed blindly. So
    # the classification is asked FIRST, and its answer is terminal: a
    # correctly-unpinned path never reaches `unlisted`, and therefore --strict
    # cannot make it fatal either. Only what survives that test is a question
    # about completeness at all.
    normal = 0
    for rel in sorted(tracked_set - listed_set):
        if unpinnable_reason(rel) is not None:
            normal += 1
            continue
        unlisted.append(f"{rel}: tracked but not listed")
    for rel in sorted(listed_set - tracked_set):
        problems.append(f"{rel}: listed but not in the tree")
    for rel in sorted(listed_set & tracked_set):
        # The detector for this hazard having already fired: a pinned path that
        # does not ship has published its own name in a shipped inventory.
        why = unpinnable_reason(rel)
        if why is not None:
            problems.append(
                f"{rel}: pinned in {MANIFEST_NAME} but {why} — the manifest "
                "ships, so this row publishes a path that must not appear in it")
            continue
        actual = hash_path(root, rel)
        if actual != listed[rel]:
            problems.append(
                f"{rel}: content differs from the manifest "
                f"(listed {listed[rel][:12]}…, actual {actual[:12]}…)")

    if unlisted:
        label = "FAIL" if strict else "WARN"
        print(f"{label}: {len(unlisted)} tracked file(s) not listed in "
              f"{MANIFEST_NAME}:", file=sys.stderr)
        for u in unlisted:
            print(f"  {u}", file=sys.stderr)
        if not strict:
            print("  (warning only — re-run with --strict to fail on these; "
                  "the exit code below reflects hash mismatches only)",
                  file=sys.stderr)

    if problems:
        print(f"FAIL: the tree does not match {MANIFEST_NAME}:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)

    # One remedy, printed for either bucket, because both are answered by the
    # same act: putting a row in the manifest. WHICH command does that is the
    # thing this text exists to get right — in a classified tree `--write` is
    # the damaging action, not the cure, so it is never recommended there.
    if problems or unlisted:
        print("\n  REMEDY: " + (
            f"pin each file deliberately — {APPROVE_CMD} — which is the one "
            f"write path the export gate trusts. Do NOT run --write in this "
            f"tree: it regenerates from the tree and {CLASSIFICATION_NAME} "
            f"marks {len(unpinnable)} tracked path(s) that must never be pinned."
            if unpinnable is not None else
            f"regenerate with `python scripts/check_release_manifest.py "
            f"--write`. That is safe here: this tree carries no "
            f"{CLASSIFICATION_NAME}, so every tracked file ships and no "
            f"private path can be pinned. In the source repo, which does carry "
            f"one, use `scripts/export_guard.py approve` instead."),
            file=sys.stderr)

    if problems:
        return 1
    if unlisted and strict:
        return 1
    matched = len(listed_set & tracked_set)
    counts = []
    if unlisted:
        counts.append(f"{len(unlisted)} tracked file(s) unlisted")
    if unpinnable is not None:
        counts.append(f"{normal} tracked file(s) correctly unpinned")
    suffix = f" ({', '.join(counts)})" if counts else ""
    print(f"OK: {matched} file(s) match {MANIFEST_NAME}{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_release_manifest.py",
        description="Verify (or regenerate) the release file inventory.")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the manifest from the current tree")
    ap.add_argument("--root", type=Path, default=None,
                    help="repository root (default: the repo this script is in)")
    ap.add_argument("--strict", action="store_true",
                    help="also fail on SHIP files with no manifest row (the "
                         "released tree's invariant; a working repository "
                         "legitimately carries these). Paths the "
                         "classification marks `drop`, or that match no rule, "
                         "are correctly unpinned and stay non-fatal here.")
    args = ap.parse_args(argv)
    root = (args.root or repo_root()).resolve()
    try:
        if args.write:
            return write_manifest(root)
        return check_manifest(root, strict=args.strict)
    except Refused as exc:
        print(f"REFUSED — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
