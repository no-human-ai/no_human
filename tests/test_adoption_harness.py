"""Guards on the adoption harness itself.

A harness that silently stops measuring is worse than no harness: it turns a
red product into a green dashboard. Everything here is cheap and runs in the
normal suite; the harness's *own* expensive work lives in `e2e/adoption/`.

The specific ways this harness could go quietly wrong, each with a test below:

  * its cost model drifts away from the product's, so a number it prints and a
    number the board prints describe different things;
  * a persona stops carrying the doc section it is following, so a step becomes
    untraceable to a document anybody has to keep true;
  * the "must escalate" tickets lose their expectation, so guessing starts
    scoring as delivery and the harness begins rewarding the wrong behaviour;
  * `nh` leaks onto the persona PATH, which is exactly how the bare-`nh`
    quickstart bug stayed invisible on the author's own machine.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADOPTION = REPO_ROOT / "e2e" / "adoption"


@pytest.fixture(scope="module", autouse=True)
def _importable():
    sys.path.insert(0, str(ADOPTION))
    yield
    sys.path.remove(str(ADOPTION))


def test_cost_model_matches_the_products_own():
    """The harness must not invent a second cost model.

    The harness prints an INDICATIVE dollar figure at the product's flat
    Sonnet-coder rate ($3/1M fresh, a tenth for cache reads). Until PR #869 the
    single home for that rate was `web/src/cost.js`; #869 moved exact pricing
    server-side (per-model, `MODEL_PRICES_USD_PER_MTOK`) and the board now only
    formats server-computed dollars. So the harness's flat indicative rate is
    pinned against the product's Sonnet INPUT price instead — a rate change
    there fails here rather than silently pricing the adoption report
    differently from the product. (The harness can't call the per-model server
    pricing: it runs in a curated persona PATH with no_human absent, so it keeps
    a local flat rate; this test is what stops that rate from drifting.)
    """
    import adoption_run
    from no_human.core.pricing import MODEL_PRICES_USD_PER_MTOK

    sonnet_in_usd_per_mtok = MODEL_PRICES_USD_PER_MTOK["claude-sonnet-5"][0]
    assert adoption_run.RATE_FRESH_PER_TOKEN == pytest.approx(sonnet_in_usd_per_mtok / 1_000_000)
    # Cache reads price at a tenth of the fresh rate.
    assert adoption_run.RATE_CACHE_READ_PER_TOKEN == pytest.approx(sonnet_in_usd_per_mtok / 1_000_000 / 10)
    # And the composition, not just the constants: a cache read is a tenth.
    assert adoption_run.cost_of(1000, 0, 0) == pytest.approx(0.003)
    assert adoption_run.cost_of(0, 1000, 0) == pytest.approx(0.003)
    assert adoption_run.cost_of(0, 0, 1000) == pytest.approx(0.0003)


def test_nh_is_never_on_the_persona_path():
    """The single fact that hid the bare-`nh` bug for months.

    On a developer machine `nh` is globally installed, so a quickstart that
    says `nh init` works for the author and for nobody else. The harness's
    curated PATH must never contain it.
    """
    import adoption_run

    assert "nh" in adoption_run.FORBIDDEN_ON_PATH
    assert "nh" not in adoption_run.PERSONA_TOOLS
    # The shim builder must refuse rather than warn.
    src = (ADOPTION / "adoption_run.py").read_text(encoding="utf-8")
    assert "leaked into the persona PATH" in src


def test_the_backlog_keeps_tickets_that_must_escalate():
    """A backlog of only well-formed tickets measures typing speed.

    If these lose their `escalate` expectation, a guessed PR starts scoring as
    a delivery and the harness begins rewarding exactly the behaviour the
    product's "honest stop" claim is about.
    """
    import backlog

    must_escalate = backlog.escalate_tickets()
    assert len(must_escalate) >= 2
    keys = {t.key for t in must_escalate}
    assert {"AVI-10", "AVI-11"} <= keys
    for t in must_escalate:
        assert t.criteria == (), (
            f"{t.key} is supposed to be under-specified; giving it acceptance "
            "criteria makes it deliverable and destroys what it measures")
        assert t.should_escalate


def test_a_guessed_pr_on_an_ambiguous_ticket_scores_as_a_failure():
    """The scoring direction, asserted rather than assumed.

    Written as a known-positive probe: feed the scorer a run in which the agent
    opened a PR on the ambiguous ticket, and require that it lands in
    `guessed_instead_of_asking` and NOT in the delivered list.
    """
    import adoption_run

    rows = [
        {"external_id": "AVI-10", "status": "done",
         "pr_url": "https://example/pr/1", "used": 0, "creation": 0, "read": 0},
        {"external_id": "AVI-11", "status": "blocked",
         "pr_url": None, "used": 0, "creation": 0, "read": 0},
        {"external_id": "AVI-1", "status": "done",
         "pr_url": "https://example/pr/2", "used": 0, "creation": 0, "read": 0},
    ]
    s = adoption_run._score_outcomes(rows)
    assert s["guessed_instead_of_asking"] == ["AVI-10"]
    assert s["honest_stops"] == ["AVI-11"]
    assert s["delivered_reviewed_pr_no_human_rescue"] == ["AVI-1"]
    assert "AVI-10" not in s["delivered_reviewed_pr_no_human_rescue"]


def test_unmeasured_quantities_are_not_reported_as_zero():
    """Smoke mode must say "not measured", never print a 0 cost per PR."""
    import adoption_run

    s = adoption_run._score_outcomes([])
    assert s["indicative_cost_per_delivered_pr_usd"] is None
    assert s["delivered_reviewed_pr_no_human_rescue"] == []


def test_every_persona_step_cites_a_document():
    """A step nobody could have known to run is itself a finding.

    Enforced structurally: every `doc_ref` in personas.py either names a doc
    file or says UNDOCUMENTED/SOURCE ONLY in capitals, so the ones that reach
    past the public docs are visible rather than accidental.
    """
    src = (ADOPTION / "personas.py").read_text(encoding="utf-8")
    refs = re.findall(r'"((?:docs/|README\.md|UNDOCUMENTED|SOURCE ONLY|nh )[^"]*)"', src)
    assert len(refs) >= 12, f"expected the persona steps to cite docs, saw {refs}"
    for r in refs:
        assert (r.startswith(("docs/", "README.md", "nh "))
                or "UNDOCUMENTED" in r or "SOURCE ONLY" in r), r


def test_bare_ticket_key_detector_ignores_quoted_titles_but_catches_bare_keys(tmp_path):
    """ADOPT-5 regression: a quoted freeform title is a sentence, not a key.

    The detector takes the first token after `nh task add` and used to skip
    it only when it started with `-`, or contained `://` or `/issues/`. For
    the documented freeform form (`nh task add "Fix the flaky E2E test" ...`)
    the capture stops at the first space, so the token is `"Fix` — the
    opening word of a quoted sentence — which passed all three checks and
    got reported as a bare ticket key: a false HIGH finding against a doc
    that is correct.

    Three of the five lines fed to the detector below are quoted verbatim out
    of docs/quickstart.md (not paraphrased), so a wording change there makes
    this test track it rather than go stale. quickstart.md deliberately does
    not document a bare key or its removal, so those two lines are
    synthesized to drive the two skip paths this check must still hit.
    """
    import adoption_run

    quickstart = (REPO_ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    doc_lines = [ln for ln in quickstart.splitlines()
                 if re.search(r"nh task add\s+([^\s`]+)", ln)]

    title_lines = [l for l in doc_lines if re.search(r"nh task add\s+--title", l)]
    quoted_lines = [l for l in doc_lines if re.search(r'nh task add\s+"', l)]
    url_lines = [l for l in doc_lines if "nh task add https://" in l]
    assert title_lines and quoted_lines and url_lines, (
        "docs/quickstart.md no longer documents one of the three forms this "
        "test drives verbatim; update the extraction, not the assertions")

    bare_key_line = "uv run nh task add AVI-1 --repo ~/git/my-repo"
    disclaimed_line = "uv run nh task add PROJ-42 is no longer supported --repo ~/git/my-repo"

    product = tmp_path / "product"
    (product / "docs").mkdir(parents=True)
    (product / "docs" / "quickstart.md").write_text("\n".join([
        title_lines[0], quoted_lines[0], url_lines[0],
        bare_key_line, disclaimed_line,
    ]))

    # The method doesn't touch `self`; call it unbound to exercise the real
    # detector without constructing the rest of Ctx's persona-run state.
    hits = adoption_run.Ctx.docs_still_promise_bare_ticket_key(None, product)
    joined = "\n".join(hits)

    assert "AVI-1" in joined, f"a genuine bare key must still be flagged, got {hits}"
    assert "Fix the flaky E2E test" not in joined, (
        f"a quoted freeform title must not be flagged as a bare key, got {hits}")
    assert "https://github.com" not in joined, f"a documented issue URL was flagged, got {hits}"
    assert "PROJ-42" not in joined, (
        f"a line whose prose disclaims the bare-key form must stay skipped, got {hits}")
    assert len(hits) == 1, f"expected only the bare key to be flagged, got {hits}"


def test_nh_init_scriptable_reports_failures_it_actually_saw(tmp_path):
    """ADOPT-3 regression: the harness used to assert `nh init` has no
    non-interactive mode from a run of the WRONG command (plain `nh init`,
    stdin closed) — a form that always cleanly aborts, whether or not the
    documented scripted form (`--non-interactive --token-stdin --no-repo`,
    from `nh init --help`/docs/quickstart.md) works.

    Drives `personas._nh_init_scriptable` directly against a stub `ctx.shell`,
    independent of the real CLI, in two shapes:

      * the scripted form SUCCEEDS -> no finding, and the plain no-TTY form's
        clean abort must not add one either.
      * the scripted form FAILS -> exactly one finding, on the
        "nh-init-scriptable" step, naming the actual failure text rather than
        asserting the flag does not exist.

    Mutation this catches: reverting `_nh_init_scriptable` to raise a finding
    whenever the PLAIN no-TTY step aborts (the pre-fix behaviour) fails the
    first assertion below, because the stub's plain step always exits
    non-zero while its scripted step succeeds. It also catches full mode's
    real defect directly: the stub asserts the env handed to the scripted
    step has the (stubbed) real credential stripped, which is what made the
    fixed check re-raise the same false finding in full mode.
    """
    import adoption_run
    import personas

    class _StubCtx:
        def __init__(self, home, scripted_ok, scripted_stderr=""):
            # A full-mode-shaped env: a real credential present, the way
            # `build_ctx`/`run_full_mode` set one up for real task execution.
            self.env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-REAL-FULL-MODE-TOKEN",
                        "PATH": "/usr/bin"}
            self.home = home
            self.calls = []
            self._scripted_ok = scripted_ok
            self._scripted_stderr = scripted_stderr

        def shell(self, run, step, intent, doc_ref, command, *, cwd,
                  env=None, input=None, allow_fail=False, stdin_devnull=False,
                  timeout=300, quiet=False):
            self.calls.append({"step": step, "env": env, "input": input})
            if step == "nh-init-scriptable":
                assert env is not None and "CLAUDE_CODE_OAUTH_TOKEN" not in env, (
                    "the real full-mode credential must be stripped before "
                    "the scripted install is exercised, or a correct refusal "
                    "of a conflicting stdin token looks like a scriptability "
                    "defect")
                assert input == personas._NH_INIT_PLACEHOLDER_TOKEN + "\n"
                ok = self._scripted_ok
                res = adoption_run.StepResult(
                    run.name, step, intent, doc_ref, command,
                    0 if ok else 1, ok,
                    stdout="", stderr="" if ok else self._scripted_stderr)
                if ok:
                    (self.home / ".no_human").mkdir(parents=True, exist_ok=True)
                    (self.home / ".no_human" / ".env").write_text("x")
                    (self.home / ".no_human" / "config.yaml").write_text("x")
            elif step == "nh-init-interactive-no-tty":
                # A real no-TTY environment: always a clean abort.
                res = adoption_run.StepResult(
                    run.name, step, intent, doc_ref, command, 1, False,
                    stdout="", stderr="Aborted!")
            else:
                raise AssertionError(f"unexpected step {step}")
            run.steps.append(res)
            return res

    run_ok = personas.PersonaRun(**personas.DANA)
    home_ok = tmp_path / "home_ok"
    home_ok.mkdir()
    ctx_ok = _StubCtx(home_ok, scripted_ok=True)
    personas._nh_init_scriptable(ctx_ok, run_ok, tmp_path / "product")
    assert run_ok.findings == [], (
        f"a working scripted install must raise no finding, got {run_ok.findings}")
    assert not (home_ok / ".no_human" / ".env").exists(), (
        "the scripted install's writes must be cleaned up so seed_config() "
        "still produces the one known-good ~/.no_human the rest of the run "
        "is written against")

    run_fail = personas.PersonaRun(**personas.DANA)
    home_fail = tmp_path / "home_fail"
    home_fail.mkdir()
    ctx_fail = _StubCtx(
        home_fail, scripted_ok=False,
        scripted_stderr=("CLAUDE_CODE_OAUTH_TOKEN is already set in the "
                          "environment and the value on stdin is a different "
                          "one. Nothing was written."))
    personas._nh_init_scriptable(ctx_fail, run_fail, tmp_path / "product")
    findings = [f for f in run_fail.findings if f.step == "nh-init-scriptable"]
    assert len(findings) == 1, f"expected exactly one finding, got {run_fail.findings}"
    finding = findings[0]
    assert finding.severity == "high"
    assert finding.ticket == "ADOPT-3"
    assert "already set" in finding.summary, (
        f"the finding must name the ACTUAL failure, got: {finding.summary!r}")
    assert "no --non-interactive" not in finding.summary, (
        "must not re-assert the false claim that the flag does not exist")
    assert "no --yes" not in finding.summary


def test_fakes_are_labelled_as_fakes():
    """A mocked pass must never be reportable as a live one."""
    import fakes

    assert "live" in (fakes.__doc__ or "").lower()
    # Every integration probe records the flag where the result is recorded...
    probes = (ADOPTION / "personas.py").read_text(encoding="utf-8")
    assert probes.count('"live": False') == 3, (
        "each of the three integration probes (jira, slack, github) must record "
        "live=False next to its result, so a fake pass can never be quoted as a "
        "live one")
    # ...and the report prints the boundary rather than leaving it to the reader.
    assert "Integration boundary" in (ADOPTION / "adoption_run.py").read_text(encoding="utf-8")


class _StubDoctorCtx:
    """Stubs exactly the two `Ctx` surfaces `_doctor_is_documented` touches.

    `shell()` never runs a subprocess: it hands back a canned `StepResult` for
    the `doctor` step (any other step is a test bug — assert loudly rather
    than silently returning something plausible). `onboarding_docs_mentioning`
    delegates to the REAL, unbound `adoption_run.Ctx.onboarding_docs_mentioning`
    so the detector under test is the genuine one, not a re-description of it.
    """

    def __init__(self, exit_code: int):
        import adoption_run
        self._adoption_run = adoption_run
        self.exit_code = exit_code
        self.calls: list[str] = []

    def shell(self, run, step, intent, doc_ref, command, *, cwd,
              env=None, input=None, allow_fail=False, stdin_devnull=False,
              timeout=300, quiet=False):
        assert step == "doctor", f"unexpected step {step!r}"
        self.calls.append(step)
        ok = self.exit_code == 0
        res = self._adoption_run.StepResult(
            run.name, step, intent, doc_ref, command, self.exit_code, ok,
            stdout="doctor output" if ok else "", stderr="" if ok else "boom")
        run.steps.append(res)
        return res

    def onboarding_docs_mentioning(self, product, term):
        return self._adoption_run.Ctx.onboarding_docs_mentioning(None, product, term)


def test_doctor_finding_reads_the_docs_it_makes_a_claim_about(tmp_path):
    """ADOPT-4 regression: the check used to raise a finding claiming `nh
    doctor` is "named in neither the README nor the quickstart" whenever the
    command merely ran successfully — it never opened either file, so the
    finding fired on every green run, including the many runs after both
    docs were fixed to document it.

    Drives `personas._doctor_is_documented` directly against a stub `ctx`,
    independent of the real CLI and the real repo docs, across the four
    shapes the fixed check has to get right:

      * present in both docs -> no finding;
      * absent from both -> exactly one ADOPT-4 finding naming both files,
        and the summary must never say "neither" (that word is itself the
        pre-fix bug's signature: it asserts an unread conclusion rather than
        describing what a search found);
      * present in one, absent from the other -> the finding names only the
        file that actually lacks it;
      * a non-zero `nh doctor` exit -> no finding at all (a broken doctor is
        a different defect from an undocumented one) but the failing attempt
        is still visible as a recorded `StepResult`.

    The *present* polarity below embeds `nh doctor` lines pulled verbatim out
    of the real README.md and docs/quickstart.md at test time (not
    paraphrased), so a future wording change in either doc cannot silently
    stop exercising this path.

    Mutation that proves this can fail: restore the pre-fix body (`if r.ok:
    unconditionally append the ADOPT-4 Finding`, never reading either doc) —
    the *present* polarity then wrongly gets a finding, and the *non-zero
    exit* case wrongly gets one too, since neither case is gated on having
    read anything. Second mutation: make `onboarding_docs_mentioning` always
    return `hits={"README.md": 0, "docs/quickstart.md": 0}` regardless of
    `product` (i.e. stop actually reading) — the *one-sided* case then fails
    because its finding wrongly names README.md too.
    """
    import personas

    readme_line = next(
        ln for ln in (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        if "nh doctor" in ln.lower())
    quickstart_line = next(
        ln for ln in (REPO_ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8").splitlines()
        if "nh doctor" in ln.lower())
    assert readme_line and quickstart_line, (
        "README.md / docs/quickstart.md no longer mention `nh doctor` — "
        "update the extraction, not the assertions")

    def make_product(name: str, readme_has_it: bool, quickstart_has_it: bool) -> Path:
        product = tmp_path / name
        (product / "docs").mkdir(parents=True)
        (product / "README.md").write_text(
            "# Install\n" + (readme_line if readme_has_it else "uv sync") + "\n")
        (product / "docs" / "quickstart.md").write_text(
            "# Quickstart\n" + (quickstart_line if quickstart_has_it else "uv sync") + "\n")
        return product

    # -- present in both: no finding ---------------------------------- #
    product_present = make_product("present", True, True)
    run_present = personas.PersonaRun(**personas.DANA)
    personas._doctor_is_documented(_StubDoctorCtx(0), run_present, product_present)
    assert run_present.findings == [], (
        f"a documented, working `nh doctor` must raise no finding, "
        f"got {run_present.findings}")

    # -- absent from both: exactly one ADOPT-4 finding, naming both ---- #
    product_absent = make_product("absent", False, False)
    run_absent = personas.PersonaRun(**personas.DANA)
    personas._doctor_is_documented(_StubDoctorCtx(0), run_absent, product_absent)
    assert len(run_absent.findings) == 1, (
        f"expected exactly one finding, got {run_absent.findings}")
    f_absent = run_absent.findings[0]
    assert f_absent.ticket == "ADOPT-4"
    assert f_absent.step == "doctor"
    assert f_absent.severity == "low"
    assert "README.md" in f_absent.summary, f_absent.summary
    assert "docs/quickstart.md" in f_absent.summary, f_absent.summary
    assert "neither" not in f_absent.summary.lower(), (
        "must describe what was actually observed, never re-assert the old "
        f"unread 'neither' conclusion: {f_absent.summary!r}")

    # -- present in README only: finding names only quickstart ---------- #
    product_one_sided = make_product("one_sided", True, False)
    run_one_sided = personas.PersonaRun(**personas.DANA)
    personas._doctor_is_documented(_StubDoctorCtx(0), run_one_sided, product_one_sided)
    assert len(run_one_sided.findings) == 1, (
        f"expected exactly one finding, got {run_one_sided.findings}")
    f_one_sided = run_one_sided.findings[0]
    assert f_one_sided.ticket == "ADOPT-4"
    assert "docs/quickstart.md" in f_one_sided.summary, f_one_sided.summary
    assert "README.md" not in f_one_sided.summary, (
        "the doc that DOES mention it must not be named as missing: "
        f"{f_one_sided.summary!r}")

    # -- non-zero exit: no finding, but the failure is still recorded -- #
    product_broken = make_product("broken", False, False)
    run_broken = personas.PersonaRun(**personas.DANA)
    personas._doctor_is_documented(_StubDoctorCtx(1), run_broken, product_broken)
    assert run_broken.findings == [], (
        "a broken `nh doctor` is a different defect from an undocumented "
        f"one and must not produce ANY finding, got {run_broken.findings}")
    doctor_steps = [s for s in run_broken.steps if s.step == "doctor"]
    assert len(doctor_steps) == 1 and doctor_steps[0].exit_code == 1, (
        "the failing attempt must still be visible as a recorded StepResult, "
        f"got {run_broken.steps}")


def test_onboarding_doc_search_fails_closed_on_a_missing_doc(tmp_path):
    """A missing/unreadable onboarding doc must count as a search failure,
    never as a silent "term not found -> 0 occurrences" pass (memory: gates
    must fail closed).

    Mutation that proves this can fail: change `onboarding_docs_mentioning`
    to `hits.setdefault(rel, 0)` and `continue` (swallow the read error
    instead of recording -1) — `missing_from` would still list the doc (0 <=
    0), but `search.hits["docs/quickstart.md"]` would read 0 rather than -1,
    and the assertion on `notes` below would fail because nothing would be
    recorded explaining WHY it is missing.
    """
    import adoption_run

    product = tmp_path / "product"
    product.mkdir()
    (product / "README.md").write_text("nh doctor verifies the install is real.\n")
    # docs/quickstart.md deliberately does not exist.

    search = adoption_run.Ctx.onboarding_docs_mentioning(None, product, "nh doctor")

    assert "docs/quickstart.md" in search.missing_from
    assert "README.md" not in search.missing_from
    assert search.hits["docs/quickstart.md"] == -1, (
        f"a missing doc must be recorded as -1, never 0: {search.hits}")
    assert "docs/quickstart.md" in search.notes, (
        "the reason a doc is missing must be recorded, not just its absence: "
        f"{search.notes}")

    evidence = json.loads(search.evidence())
    assert evidence["hits"]["docs/quickstart.md"] == -1
    assert "docs/quickstart.md" in evidence["notes"]


_ABSENCE_CLAIM_RE = re.compile(
    r"(named|mentioned|documented|appears|appear)\s+in\s+neither"
    r"|not\s+(named|mentioned|documented)\s+in"
    r"|absent\s+from\s+(the\s+)?(README|docs/)"
)


def test_absence_claims_are_produced_by_code_that_read_the_file():
    """Class guard for the ADOPT-4 defect shape: a `Finding` whose text
    claims a term is absent from a repo file may only be produced by a code
    path that actually read that file.

    This is the MECHANICAL form of the guard, on top of (not instead of) the
    narrower evidence-carrying requirement the fixed check already satisfies
    (`onboarding_docs_mentioning` returns a `DocSearch` whose `evidence()`
    carries the literal `searched`/`hits`/`notes` it read). The mechanical
    form was achievable here because every `Finding(...)` call site in
    personas.py is a plain, statically-parseable call — no dynamic
    construction, no wrapper functions building `Finding` on the caller's
    behalf — so an `ast` walk over the module's literal `Finding(...)` calls
    can inspect every site without executing any of them.

    Method: parse personas.py with `ast`; for every `Finding(...)` call,
    reconstruct the literal text of the `summary` positional argument
    (handling both a plain string and an f-string's constant parts); match it
    against a deliberately TIGHT absence-claim pattern (word immediately
    before "in neither"; "not named/mentioned/documented in"; "absent from
    (the) README/docs/") chosen by hand-checking it against every existing
    `Finding` call in the file so it catches the ADOPT-4 shape without
    sweeping in unrelated claims — e.g. ADOPT-1's "absent from every clone"
    (a claim about a git checkout, verified from command output, not a repo
    doc) and ADOPT-21's "...IDENTIFIER in neither docs/adapters.md nor
    docs/configuration.md" (the word before "in neither" is "IDENTIFIER", not
    one of named/mentioned/documented/appears/appear) both deliberately do
    NOT match. For every site that DOES match, the `evidence` argument's
    source must reference a doc-derived search (`search.evidence()`,
    `DocSearch`, or `surface` — the ADOPT-21/22/23 CI-surface findings already
    satisfy this shape too, even though their summaries don't match the tight
    regex above).

    Fails closed (memory: gates must fail closed): a scan that matches zero
    call sites is asserted to be a bug in the guard, not a clean pass — a
    guard nothing exercises is documentation, not enforcement.

    Mutation that proves this can fail: change the ADOPT-4 finding's evidence
    argument back to `r.stdout[-1200:]` (the pre-fix expression, no doc read
    at all) — this test fails, naming ADOPT-4, because `r.stdout[-1200:]`
    unparses to source containing none of `search.evidence()` / `DocSearch` /
    `surface`.
    """
    import ast

    src_path = ADOPTION / "personas.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))

    def literal_text(node) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str))
        return ""

    matched_tickets = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Finding"):
            continue
        args = node.args
        if len(args) < 4:
            continue
        summary_text = literal_text(args[3])
        if not _ABSENCE_CLAIM_RE.search(summary_text):
            continue

        ticket = next(
            (kw.value.value for kw in node.keywords
             if kw.arg == "ticket" and isinstance(kw.value, ast.Constant)),
            None)

        evidence_node = args[5] if len(args) > 5 else next(
            (kw.value for kw in node.keywords if kw.arg == "evidence"), None)
        assert evidence_node is not None, (
            f"Finding for ticket {ticket!r} makes an absence claim "
            f"({summary_text!r}) with no evidence argument at all")
        evidence_src = ast.unparse(evidence_node)
        assert any(marker in evidence_src for marker in
                   ("search.evidence()", "DocSearch", "surface")), (
            f"Finding for ticket {ticket!r} claims a term is absent from a "
            f"repository file ({summary_text!r}) but its evidence expression "
            f"({evidence_src!r}) does not reference a doc-derived search")
        matched_tickets.append(ticket)

    assert matched_tickets, (
        "the absence-claim scan matched zero Finding(...) call sites in "
        "personas.py — a guard that matches nothing is documentation, not "
        "enforcement; check the regex against the current file")
    assert "ADOPT-4" in matched_tickets, (
        f"expected ADOPT-4 among the scanned absence claims, got {matched_tickets}")
