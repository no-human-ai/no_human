"""Bound large gate diffs without letting path order hide changed files.

The trusted exclusion set is intentionally empty. If exclusions are ever added,
they must live in this module (reviewer-owned code), never in target-repository
configuration that the code under review can edit.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

from ..vcs.derived_conflict import DERIVED_ARTEFACTS
from .lint_evidence import unquote_git_path


TRUSTED_COVERAGE_EXCLUSIONS: frozenset[str] = frozenset()
_COVERAGE_NOTE = (
    "\nDIFF COVERAGE — these changed-file patches were cut by the per-file "
    "budget. Inspect every listed path with read/search tools before reaching "
    "a verdict:\n"
)
_DISCLOSURE_NOTE = (
    "\nDIFF COVERAGE — these changed-file patches were cut by the per-file "
    "budget. You have NO tools in this pass and no checkout to read them "
    "from: judge only what is shown, and say in your verdict that these "
    "files were not fully visible rather than clearing them:\n"
)
_MAX_PREFIX_CHARS = 2_000
#: Prefix every coverage rejection carries. `reviewer._agent_review` classifies
#: on it to feed the rejection into the next round; one literal, not two copies.
COVERAGE_REJECTION_PREFIX = (
    "reviewer reached a verdict without referencing truncated "
    "changed file(s): ")


class DiffCoverageError(RuntimeError):
    """The capped representation cannot honestly expose every changed file."""


def _unquote_path(token: str) -> str:
    # git quotes the WHOLE token including its a/b/ prefix and, when it does,
    # C-escapes non-ASCII bytes octal-per-byte (e.g. "a/docs/\303\251val.md"),
    # so the shared decoder must run before the prefix is stripped.
    token = unquote_git_path(token.strip())
    if token.startswith(("a/", "b/")):
        token = token[2:]
    return token


def _patch_path(chunk: str) -> str:
    for line in chunk.splitlines():
        if line.startswith("rename to "):
            return _unquote_path(line[len("rename to "):])
        if line.startswith("+++ "):
            candidate = line[4:].strip()
            if candidate != "/dev/null":
                return _unquote_path(candidate)
    for line in chunk.splitlines():
        if line.startswith("--- "):
            candidate = line[4:].strip()
            if candidate != "/dev/null":
                return _unquote_path(candidate)
    first = chunk.splitlines()[0] if chunk else ""
    try:
        parts = shlex.split(first)
    except ValueError:
        parts = []
    if len(parts) >= 4 and parts[:2] == ["diff", "--git"]:
        return _unquote_path(parts[3])
    raise DiffCoverageError("could not identify a changed path from a patch header")


def _split(raw: str) -> tuple[str, list[str]]:
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", raw)]
    if not starts:
        raise DiffCoverageError("large diff has no per-file patch boundaries")
    prefix = raw[:starts[0]]
    chunks = [
        raw[start:(starts[i + 1] if i + 1 < len(starts) else len(raw))]
        for i, start in enumerate(starts)
    ]
    return prefix, chunks


def _header_len(chunk: str) -> int:
    newline = chunk.find("\n")
    return len(chunk) if newline < 0 else newline + 1


def _allocate(chunks: list[str], budget: int) -> list[int]:
    allocation = [_header_len(chunk) for chunk in chunks]
    minimum = sum(allocation)
    if minimum > budget:
        raise DiffCoverageError(
            f"{len(chunks)} changed files need {minimum:,} chars just for patch headers, "
            f"but only {budget:,} are available"
        )

    remaining = budget - minimum
    active = {i for i, chunk in enumerate(chunks) if allocation[i] < len(chunk)}
    while active and remaining:
        share = max(1, remaining // len(active))
        progressed = False
        for i in list(active):
            need = len(chunks[i]) - allocation[i]
            grant = min(need, share, remaining)
            if grant:
                allocation[i] += grant
                remaining -= grant
                progressed = True
            if allocation[i] == len(chunks[i]):
                active.remove(i)
            if remaining == 0:
                break
        if not progressed:
            break
    return allocation


def budget_diff(
    raw: str, cap: int, *, inspection_required: bool = True
) -> tuple[str, list[str]]:
    """Return small diffs unchanged; fairly bound oversized diffs by file.

    Every changed file keeps at least its ``diff --git`` header. Remaining
    space is shared across incomplete patches. Paths whose patch content is cut
    are appended to the rendered diff, so the caller can require explicit tool
    inspection before accepting any verdict.

    ``inspection_required`` selects the ledger wording: the default asks the
    reviewer to inspect every cut path with tools (the refs path, which has
    them); ``False`` only discloses what was cut, for callers with no tools
    and no checkout to inspect from.
    """
    if len(raw) <= cap:
        return raw, []
    if cap <= 0:
        raise DiffCoverageError("diff cap must be positive")

    note_header = _COVERAGE_NOTE if inspection_required else _DISCLOSURE_NOTE
    prefix, chunks = _split(raw)
    paths = [_patch_path(chunk) for chunk in chunks]
    required_paths = [p for p in paths if p not in TRUSTED_COVERAGE_EXCLUSIONS]

    # Reserve the worst-case ledger first; doing so guarantees the final output
    # never has to hide a path merely because the note itself did not fit.
    worst_note = note_header + "".join(f"- {path}\n" for path in required_paths)
    header_total = sum(_header_len(chunk) for chunk in chunks)
    prefix_budget = min(len(prefix), _MAX_PREFIX_CHARS)
    available = cap - len(worst_note) - prefix_budget
    if available < header_total:
        prefix_budget = max(0, cap - len(worst_note) - header_total)
        available = cap - len(worst_note) - prefix_budget
    if available < header_total:
        raise DiffCoverageError(
            f"{len(chunks)} changed files cannot all expose their patch headers "
            f"and coverage ledger within {cap:,} chars"
        )

    allocation = _allocate(chunks, available)
    cut_paths = [
        path for path, chunk, take in zip(paths, chunks, allocation)
        if take < len(chunk) and path not in TRUSTED_COVERAGE_EXCLUSIONS
    ]
    # Only when something really was cut. A large PREFIX can push `raw` over
    # the cap while every patch still fits, and the header alone told the
    # reviewer patches had been cut and then listed none — an instruction it
    # could not follow, about a thing that did not happen.
    note = ("" if not cut_paths else
            note_header + "".join(f"- {path}\n" for path in cut_paths))
    rendered = prefix[:prefix_budget] + "".join(
        chunk[:take] for chunk, take in zip(chunks, allocation)
    ) + note
    if len(rendered) > cap:
        raise DiffCoverageError("bounded diff exceeded its cap after rendering")
    return rendered, cut_paths


# `\w` is Unicode by default in Python and is a strict superset of
# `A-Za-z0-9_`, so every ASCII token still tokenizes byte-identically; the
# only newly admitted characters are non-ASCII word characters, needed so a
# non-ASCII cut path (e.g. "docs/éval.md") is not split into fragments that
# `_names_path` then rejects. Do not "simplify" this back to the ASCII class.
_PATH_TOKEN = re.compile(r"[\w._/+@-]+")


def _path_tokens(text: str) -> list[str]:
    """The path-shaped runs in a free-form tool-input string.

    A tool input is not one path: it can be `"Read src/a.py then tests/a.py"`,
    a `file.py:14` citation, or a quoted list. Splitting on everything that
    cannot appear in a path leaves the candidates, and `./x` is written
    `x` so the comparison below has one spelling to handle.
    """
    return [token[2:] if token.startswith("./") else token
            for token in _PATH_TOKEN.findall(text)]


def _names_path(token: str, required: str) -> bool:
    """Does `token` name the file at repo-relative path `required`?

    Substring containment is what this replaced, and it let the WRONG file
    satisfy a required path: measured over this repository's own tracked
    files, 92 pairs are substrings of each other — `Dockerfile` inside
    `Dockerfile.mcp`, `.gitignore` inside `web/.gitignore`, and every source
    file that a test-fixture corpus keeps a verbatim copy of, whose copy's
    path ends with the original's. A reviewer that read any of the longer
    ones was recorded as having covered the shorter one.

    A RELATIVE token therefore has to BE the path. Only an ABSOLUTE one may
    carry it as a suffix, which is the case that motivated suffix matching in
    the first place: the reviewer works in a throwaway clone and may name a
    file by its full path under that root. Every spelling of a genuine read
    still passes.

    What that closes, exactly, and no more: all 92 pairs fail when spelled
    RELATIVELY, which is how a reviewer names a file in the clone it works
    in. An ABSOLUTE token is still only checked by suffix, so an absolute
    path to a fixture COPY of a cut file — `/root/testdata/.../src/a.py` for
    required `src/a.py` — satisfies it. Closing that needs the repo root to
    anchor against, which this layer is not given.

    Not handled, and deliberately not claimed: a Windows-style token with
    backslash separators never matched under containment either, because the
    required paths are written with `/`.
    """
    return token == required or (
        token.startswith("/") and token.endswith("/" + required))


class InspectionTracker:
    """Did the reviewer ever bring up each path `budget_diff` had to cut?

    Lives here rather than in the reviewer because it is the other half of
    `_COVERAGE_NOTE` above: that note names the cut paths and asks for them to
    be read, and this decides whether the ask was honoured.

    REFERENCE, never inspection. A required path counts as referenced in
    exactly two ways, and neither proves its contents were read:

    1. A `tool_use` INPUT names it (`_names_path`), so a path that merely
       appears in a search string counts.
    2. A `tool_use` INPUT names the path's parent directory (same matching,
       trailing `/` ignored; never for a repo-root file, whose parent has no
       name), AND that call's `tool_result` — paired by `meta["tool_use_id"]`,
       not an error — has an `AgentEvent.output` whose path tokens include the
       path's basename (or the full path, as `_names_path` spells it). A
       listing that does not name the file leaves it required. A token from
       anywhere in the output counts, so a recursive listing that shows the
       same basename one level down also credits it.

    That is a deliberate floor — it catches the verdict reached without the
    path coming up at all — and it is why `rejection()` says "referencing" and
    not "inspecting": the message must not claim more than the evidence
    carries. Prose, thinking and `denied` events never count.

    Never required: `derived_conflict.DERIVED_ARTEFACTS` (`RELEASE_MANIFEST.
    txt` at the repo root, regenerated by `scripts/check_release_manifest.py
    --write` and byte-checked by CI), because there is nothing in it for a
    reviewer to judge. The match is on the exact repo-relative path, so a
    nested file of that name stays required; a TARGET repository with its own
    hand-written root `RELEASE_MANIFEST.txt` is exempted too, since this layer
    does not know which repository it reviews.

    Scope, so the gap is recorded rather than discovered: only the PRIMARY
    diff's cut paths are tracked. `_linked_repos_review_section` drops the cut
    paths of a linked repo (the `_cut_paths` it names and does not use), so a
    truncated linked-repo patch can still reach a verdict unread. That is the
    same failure in a narrower place than the one this closes, and widening
    the check belongs with whoever gives linked repos coverage that matters.
    """

    def __init__(self, required: Iterable[str] | None = None) -> None:
        self._required = set(required or ()) - DERIVED_ARTEFACTS
        self._seen: set[str] = set()
        # tool_use_id -> required paths whose parent directory that call named,
        # awaiting its tool_result (rule 2 in the class docstring).
        self._listings: dict[str, set[str]] = {}

    def note_event(self, event: object) -> None:
        """Record every required path named anywhere in a tool call's input,
        and credit a listed directory's children from the paired result.

        The input is arbitrary nested JSON, so this walks it rather than
        reading known keys: a path can arrive under `file_path`, inside a
        `pattern`, or in one element of a list of edits, and a walker cannot
        be out of date with the tool schema.
        """
        kind = getattr(event, "kind", "")
        if not self._required or kind not in ("tool_use", "tool_result"):
            return
        meta = getattr(event, "meta", None) or {}
        if kind == "tool_result":
            candidates = self._listings.pop(meta.get("tool_use_id"), set())
            if candidates and not meta.get("is_error"):
                names = set(_path_tokens(getattr(event, "output", None) or ""))
                self._seen.update(
                    path for path in candidates
                    if path.rsplit("/", 1)[-1] in names
                    or any(_names_path(name, path) for name in names))
            return
        tokens: list[str] = []
        stack = [getattr(event, "tool_input", None) or {}]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                stack.extend(value)
            elif isinstance(value, str):
                tokens.extend(_path_tokens(value))
        for token in tokens:
            self._seen.update(
                path for path in self._required if _names_path(token, path))
        listed = {
            path for path in self._required - self._seen if "/" in path
            and any(_names_path(token.rstrip("/"), path.rsplit("/", 1)[0])
                    for token in tokens)}
        if listed and meta.get("tool_use_id"):
            self._listings[meta["tool_use_id"]] = listed

    def unreferenced(self) -> list[str]:
        """Required paths that never appeared in any tool input, sorted."""
        return sorted(self._required - self._seen)

    def rejection(self) -> str:
        """Why this verdict must not stand, or "" when every path came up."""
        missing = self.unreferenced()
        if not missing:
            return ""
        return f"{COVERAGE_REJECTION_PREFIX}{', '.join(missing)}"


def coverage_rejection_paths(reason: str) -> list[str]:
    """The unreferenced paths a coverage rejection names, or [] if `reason`
    is not one.

    A prefix test, not a structural flag, because `_review_once` collapses
    every no-verdict cause into one opaque `reason` string and this module
    owns the only one whose text is ours. Residual risk: `_errored_round_
    reason` builds its reason from backend text, so a reason could in
    principle start with this exact sentence too; the worst case there is a
    harmless extra instruction fed into round 2, never a relaxed gate.
    """
    if not reason.startswith(COVERAGE_REJECTION_PREFIX):
        return []
    remainder = reason[len(COVERAGE_REJECTION_PREFIX):]
    return [path for path in remainder.split(", ") if path]
