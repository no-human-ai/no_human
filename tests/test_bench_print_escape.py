"""v11 live crash @34/48: spec ns-cbb81747's title begins '@[/Users/…]' and
rich parsed '[/…]' as a closing tag — MarkupError killed the RUNNER mid-run
(first process death of the program). Every spec- or exception-derived string
printed inside the bench loop must be markup-escaped."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src/no_human/cli/commands.py"


def _bench_loop_region(text: str) -> str:
    # The bench loop body now lives in `_run_spec` (the --parallel pool
    # worker); same prints, same escaping obligations.
    # Anchored on the `def` line, not on its parameter list: `_run_spec` gained a
    # `trial` argument for `--trials`, and an anchor that spelled out the old
    # signature took the whole region with it — every test in this file died
    # with ValueError instead of checking a single print. A guard that a
    # routine signature change can silence is a guard that will be silent when
    # it matters.
    start = text.index("async def _run_spec(")
    end = text.index("def bench_report", start)
    return text[start:end]


def test_spec_title_print_is_escaped():
    region = _bench_loop_region(SRC.read_text(encoding="utf-8"))
    line = next(l for l in region.splitlines() if "· {" in l or "· " in l and "spec.id" in l)
    assert "escape(" in line, f"unescaped title print: {line.strip()}"


def test_crash_handler_print_is_escaped():
    region = _bench_loop_region(SRC.read_text(encoding="utf-8"))
    line = next(l for l in region.splitlines() if "crashed" in l and "console.print" in l)
    assert "escape(" in line, f"unescaped exception print: {line.strip()}"


def test_spec_id_prints_are_escaped():
    """spec.id comes from the spec FILE (`--specs-dir` accepts arbitrary
    corpora), so a markup-shaped id (`[/x]`) in any console line is the same
    v11 crash class as the title — and in the completion line it would kill
    the run AFTER run_one succeeded but BEFORE the checkpoint append."""
    region = _bench_loop_region(SRC.read_text(encoding="utf-8"))
    printing = [l for l in region.splitlines()
                if "spec.id" in l and ("console.print" in l or "f\"" in l)]
    offenders = [l for l in printing
                 if "{spec.id}" in l and "escape(spec.id)" not in l]
    assert not offenders, f"unescaped spec.id print(s): {offenders}"
    # And the id must actually be printed somewhere in the region, escaped.
    assert any("escape(spec.id)" in l for l in region.splitlines())


#: Interpolations exempted from the AST guard below. EVERY entry is a shape that
#: cannot carry `[/...]`, and every entry is justified — because an allowlist is
#: exactly where this guard would rot back into the enumeration it replaced.
#: `escape()` is NOT the fix for these: it takes a str, and escaping an int
#: raises. Keyed by the unparsed expression, so a RENAMED variable drops out of
#: the allowlist and must be re-justified rather than silently inheriting it.
#:
#: THE CONVERSE IS A REAL LIMITATION, found by self-review and stated here rather
#: than discovered later: the same NAME in a different scope silently INHERITS the
#: exemption. `where` is the live example — at commands.py:4539 it is a redacted
#: term shape (`f*(13)`, no brackets possible, safe), but at :3351 it is
#: `f"{it.file}:{it.line}"`, a FILE PATH. Today that is sound only because :3351
#: sits outside the scanned region. **If this guard is ever widened past the bench
#: group, this allowlist must be re-derived per scope, not carried over.**
#: That limitation is also the argument for the boundary fix (a `cprint()` helper
#: that escapes its str arguments): it needs no allowlist at all, so it has no
#: scope to get wrong.
_PROVABLY_SAFE = {
    # ints — counts computed in this module, never model- or file-authored
    # (`stranded`: rows counted out of checkpoint cards by this function)
    "runnable", "loaded", "stranded",
    # aggregate metrics: floats/ints out of card.as_dict()["aggregate"]
    "agg['success_rate']", "agg['median_cost_ratio']",
    "agg['total_nh_tokens']", "agg['corrections_avoided']",
    # closed-set string label out of card.as_dict()["aggregate"] — always one
    # of northstar.BASIS_TIER_WEIGHTED/BASIS_CACHE_WEIGHTED/BASIS_MIXED or
    # the literal "n/a" (cost_ratio_basis()/NorthStarCard.
    # median_cost_ratio_basis), never model- or file-authored text
    "agg['median_cost_ratio_basis']",
    # a status enum's own value, from a closed set defined in this repo
    "score.outcome_status",
    # ✅/❌ glyph and a formatted ratio, both built literally a few lines above
    "mark", "ratio",
    # int from enumerate() in the startup-sprint listing — escape() would raise
    "position",
    # either "" or a literal built two lines above that deliberately CARRIES
    # markup (" [yellow](must escalate)[/]"); escape() would print it raw.
    # Scope caveat from the header applies: any other `tag` in the scanned
    # region inherits this exemption and must be re-justified.
    "tag",
    # module-level Path constants, not user input
    "REPORT_MD",
    # already-redacted term SHAPES ("f*(13)"), built by the refusal itself
    "where",
    # a slug-sanitised results filename; _slug() strips everything but [a-z0-9-]
    "out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out",
}


def test_no_bench_print_interpolates_an_unescaped_value():
    """AST guard: EVERY interpolation is escaped or provably safe.

    Three textual predecessors each missed the case they existed for, because
    each matched something a human had written down:

        v1  the FIRST ⛔ line          -> blind to three other ⛔ lines
        v2  EVERY ⛔ line              -> blind to ⚠ lines with the same values
        v3  a list of variable NAMES   -> blind to {legacy.label} and {rows}

    A reviewer found v3's gap by CRASHING `nh bench run --resume`: it died with
    MarkupError and an EMPTY stdout, on `legacy.label` — the same field escaped
    five times elsewhere under two other names.

    So this knows no names. It walks every `console.print` and requires each
    interpolation to be escape()-wrapped or provably safe BY SHAPE. A new
    *f-string* print fails by default; bare-Name/.format()/%-format/concat
    shapes are NOT seen — see `tests/_bench_ast_guard.py`'s docstring for the
    full blind-spot list. It is also immune to two bypasses the text versions
    had: parens inside string literals (v3 counted characters, so one extra ")"
    ended the scan early) and multi-value prints where only the first value is
    escaped.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bench_ast_guard import find_unescaped_prints

    text = SRC.read_text(encoding="utf-8")
    lo = text[:text.index('@cli.group("bench")')].count("\n") + 1
    hi = text[:text.index('@cli.command("shadow")')].count("\n") + 1

    found = find_unescaped_prints(text, lo, hi)
    # Non-vacuity that a shrunk region CANNOT satisfy: the region must still
    # contain the publish AND report commands. The previous anchors were happy
    # with `bench run` alone, so a stray comment naming the end-marker shrank the
    # region past both and the guard stayed green.
    region = text[text.index('@cli.group("bench")'):text.index('@cli.command("shadow")')]
    for marker in ('@bench.command("publish")', '@bench.command("report")'):
        assert marker in region, (
            f"{marker} is outside the scanned region — the markers moved and "
            "this guard is now dark")

    offenders = [(n, e) for n, e in found if e not in _PROVABLY_SAFE]
    assert not offenders, (
        f"{len(offenders)} console.print interpolation(s) are neither escaped "
        f"nor provably safe:\n" +
        "\n".join(f"  {SRC.name}:{n}: {{{e}}}" for n, e in offenders) +
        "\nA label, path or exception can hold `@[/Users/x]`, which rich parses "
        "as a closing tag and raises MarkupError. Wrap it in escape(), or add it "
        "to _PROVABLY_SAFE with a comment saying why it cannot carry markup.")



def test_gate_reason_print_is_escaped():
    """EVERY reason line, not the first one.

    This guard used to read `next(l for l in ... if "⛔" in l)` — the FIRST
    match. That line was escaped, so the test passed for as long as it existed
    while THREE other reason lines were not: the north-star gate's, the publish
    refusal's (which embeds `previous.label`, so it was a live MarkupError and
    not a theoretical one), and a fourth added later. An independent reviewer
    found the newest of them by crashing it, not by reading it.

    A first-match assertion cannot see the case a guard exists for, because the
    case it exists for is the NEXT line someone writes. Assert over all of them.
    """
    text = SRC.read_text(encoding="utf-8")
    lines = [(n, l) for n, l in enumerate(text.splitlines(), 1) if "⛔" in l]
    # Discovery that finds nothing passes vacuously; the glyph has been in this
    # file since the gate landed, so zero means it was renamed and this guard is
    # now measuring nothing at all.
    assert lines, "no ⛔ reason lines found — the glyph changed and this guard is dark"
    unescaped = [(n, l.strip()) for n, l in lines if "escape(" not in l]
    assert not unescaped, (
        f"{len(unescaped)} of {len(lines)} reason line(s) interpolate unescaped "
        f"text into rich markup:\n" +
        "\n".join(f"  {SRC.name}:{n}: {l}" for n, l in unescaped) +
        "\nA reason string can carry a card label or a path; `@[/Users/x]` reads "
        "to rich as a closing tag and raises MarkupError, so the refusal becomes "
        "a traceback. Wrap it in escape().")


def test_the_crashing_title_renders():
    """End-to-end: the exact v11 killer string renders through rich."""
    from rich.console import Console
    from rich.markup import escape
    import io
    title = "@[/Users/dev/Downloads/db_issue.csv] go over these results"
    c = Console(file=io.StringIO(), force_terminal=False)
    c.print(f"[dim]· ns-cbb81747 {escape(title[:60])}[/]")  # must not raise
