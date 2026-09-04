"""C1 seed-context diet: every token in the coder's initial context is re-read
on EVERY turn of the session (measured: 30-40k tokens/turn on trivial tasks,
94.6% of all spend is coder cache-read). These tests pin the three diet rules:

  1. User-level skills are relevance-filtered by default (SCRUM-17: a loose
     sub-token matcher let common English words in a ticket ("fields",
     "design", "ui") pull in the operator's entire personal/employer skill
     roster into every session). A skill now matches only on a high-signal
     signal: its whole hyphenated name appears as one cohesive token in the
     task text, or its name equals the task repo's basename.
  2. The repo map (~3k tokens/turn) is skipped when an implementation plan
     exists — the plan already answers "where does X live", and the full plan
     is always on disk at .no_human/PLAN.md.
  3. The final implement-prompt size is emitted as a `prompt_size` event so
     the diet is measurable per attempt, not assumed.
"""

from no_human.core.task import Task, TaskStatus
from no_human.history.skills import SkillInfo, relevant_skill_names, skill_matches_task


def _skill(name, description, source="/home/u/.claude/skills/x"):
    return SkillInfo(name=name, description=description, source=source)


# ---------------------------------------------------------------- matching --

def test_exact_hyphenated_name_still_delivers():
    """A ticket naming the skill exactly (whole hyphenated compound as one
    cohesive token) still matches — the precision fix tightens matching, it
    does not break exact mentions."""
    s = _skill("ui-styling", "Frontend UI styling conventions")
    assert skill_matches_task(s, "please use the ui-styling skill")


def test_irrelevant_skill_does_not_match():
    s = _skill("metrics-core-troubleshoot", "Troubleshoot service deployments and pods")
    assert not skill_matches_task(
        s, "Add a formatBytes helper to web utils")


def test_stopwords_do_not_create_relevance():
    """Common English words shared by any two texts must not count as overlap,
    or the filter keeps everything and filters nothing."""
    s = _skill("analytics-export-orient", "Get oriented with the service framework and its layout")
    assert not skill_matches_task(s, "Fix the pluralize helper and its tests")


def test_repo_name_match_delivers_bare_skill():
    """A bare single-word skill name never matches free text, but it does
    match when it equals the task repo's basename exactly."""
    s = _skill("brand", "Operator's personal brand guide")
    assert skill_matches_task(s, "add a logo", repo_name="brand")
    assert not skill_matches_task(s, "add a logo", repo_name="web")


# --------------------------------------------------------------- filtering --

_ROSTER = ["banner-design", "brand", "design", "design-system", "broker-topic-fields",
           "slides", "ui-styling", "ui-ux-pro-max", "platform-orient", "platform-run-tests",
           "broker-consumer", "platform-figma"]  # 12, all user-level — a real roster size


def _roster_skills(tmp_path):
    root = tmp_path / "user-skills"
    return [_skill(n, f"{n} skill", source=str(root / n)) for n in _ROSTER]


def test_relevant_skill_names_filters_user_skills(tmp_path, monkeypatch):
    import no_human.history.skills as sk
    monkeypatch.setattr(sk, "USER_SKILLS", tmp_path / "user-skills")
    user_src = str(tmp_path / "user-skills" / "metrics-core-troubleshoot")
    disk = [
        _skill("metrics-core-troubleshoot", "Troubleshoot metrics-core pods", source=user_src),
        _skill("repo-helper", "project skill", source=str(tmp_path / "repo" / ".claude")),
    ]
    names = sk.relevant_skill_names(
        disk, set(), "Add a formatBytes helper to web utils")
    # user skill irrelevant -> dropped; project skill never filtered
    assert names == ["repo-helper"]


def test_relevant_skill_names_keeps_matching_user_skill(tmp_path, monkeypatch):
    import no_human.history.skills as sk
    monkeypatch.setattr(sk, "USER_SKILLS", tmp_path / "user-skills")
    user_src = str(tmp_path / "user-skills" / "metrics-core-troubleshoot")
    disk = [_skill("metrics-core-troubleshoot", "Troubleshoot metrics-core pods", source=user_src)]
    names = sk.relevant_skill_names(
        disk, set(), "Investigate the metrics-core-troubleshoot runbook")
    assert names == ["metrics-core-troubleshoot"]


def test_relevant_skill_names_excludes_db_titles_and_honors_disable(tmp_path, monkeypatch):
    import no_human.history.skills as sk
    monkeypatch.setattr(sk, "USER_SKILLS", tmp_path / "user-skills")
    user_src = str(tmp_path / "user-skills" / "metrics-core-troubleshoot")
    disk = [_skill("metrics-core-troubleshoot", "Troubleshoot metrics-core pods", source=user_src)]
    # already delivered via DB memories -> not repeated
    assert sk.relevant_skill_names(disk, {"metrics-core-troubleshoot"}, "anything") == []
    # filter disabled -> user skill kept regardless of relevance
    assert sk.relevant_skill_names(
        disk, set(), "unrelated web helper", filter_user=False,
    ) == ["metrics-core-troubleshoot"]


def test_backend_ticket_delivers_zero_user_skills(tmp_path, monkeypatch):
    """SCRUM-17 held negative: reproduced from a real session — a backend ticket
    mentioning 'fields' with a 12-skill user roster available must deliver ZERO
    user skills."""
    import no_human.history.skills as sk
    monkeypatch.setattr(sk, "USER_SKILLS", tmp_path / "user-skills")
    disk = _roster_skills(tmp_path)
    names = sk.relevant_skill_names(
        disk, set(), "Make the export auth-mode-aware fields", repo_name="no_human")
    assert names == []


def test_ui_ticket_excludes_design_suite(tmp_path, monkeypatch):
    """SCRUM-17 held negative: a UI ticket mentioning 'design system tokens'
    must not pull in the design/brand/banner-design/slides suite."""
    import no_human.history.skills as sk
    monkeypatch.setattr(sk, "USER_SKILLS", tmp_path / "user-skills")
    disk = _roster_skills(tmp_path)
    names = sk.relevant_skill_names(
        disk, set(), "Wire up the design system tokens", repo_name="web")
    assert {"brand", "banner-design", "slides"}.isdisjoint(names)
    assert "design" not in names
    assert "design-system" not in names


# ------------------------------------------------------------ repo-map skip --

def _task(**kw):
    defaults = dict(
        id="aaa", source="test", title="Fix bug",
        status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["Bug is fixed"],
    )
    defaults.update(kw)
    return Task(**defaults)


def _orch():
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


def test_repo_map_skipped_when_plan_exists(monkeypatch):
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAPDATA-SENTINEL")
    t = _task()
    t.context = {"plan": "1. Edit calc.py: add mul()\n2. Add test_mul"}
    prompt = _orch()._build_implement_prompt(t, "/tmp/repo")
    assert "IMPLEMENTATION PLAN" in prompt
    assert "MAPDATA-SENTINEL" not in prompt


def test_repo_map_present_without_plan(monkeypatch):
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAPDATA-SENTINEL")
    t = _task()
    prompt = _orch()._build_implement_prompt(t, "/tmp/repo")
    assert "MAPDATA-SENTINEL" in prompt


def test_repo_map_kept_when_plan_is_truncated(monkeypatch):
    """A plan longer than _PLAN_INLINE_MAX only inlines its head — the file
    list may be beyond it, so complex tasks keep the map (under-resourcing a
    complex task costs more than the map's ~3K tokens)."""
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAPDATA-SENTINEL")
    t = _task()
    t.context = {"plan": "step\n" * 2000}  # 10k chars > _PLAN_INLINE_MAX
    prompt = _orch()._build_implement_prompt(t, "/tmp/repo")
    assert "MAPDATA-SENTINEL" in prompt


# ------------------------------------------------------------- prompt_size --

# Reuse the proven fixtures instead of keeping a third copy in the suite.
from tests.test_e2e_orchestrator import bare_repo  # noqa: E402,F401


class _FakeBackend:
    """Applies a valid feature+test mutation, absorbing new kwargs."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_prompt_size_event_emitted_per_attempt(bare_repo, tmp_path, store):
    """The diet must be measurable: every attempt emits the final implement
    prompt's size before the SDK session starts."""
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    events = []
    orch = Orchestrator(store, cfg.data, _FakeBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    await orch.run_task(t)

    sizes = [e for e in events if e["kind"] == "prompt_size"]
    assert sizes, "no prompt_size event emitted"
    assert sizes[0].get("chars", 0) > 100


# ---------------------------------------------------- user-skill copying --

def _write_skill(root, name, desc="d"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")
    (d / "reference.md").write_text("aux file must be copied too\n")
    return d


def test_materialize_copies_relevant_user_skills_into_tree(tmp_path, monkeypatch):
    """With setting_sources=["project"] the CLI no longer sees ~/.claude/skills,
    so relevant user skills must be COPIED into the working tree (whole dir,
    aux files included) to remain loadable — and tracked for cleanup."""
    import no_human.core.orchestrator as om
    import no_human.history.skills as sk
    from no_human.core.orchestrator import Orchestrator
    from no_human.history.skills import SkillInfo

    user_root = tmp_path / "user-skills"
    src_dir = _write_skill(user_root, "metrics-core-troubleshoot")
    monkeypatch.setattr(sk, "USER_SKILLS", user_root)

    repo = tmp_path / "repo"
    repo.mkdir()
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._active_memories = None
    orch._discovered_skills = ["metrics-core-troubleshoot"]
    orch._discovered_skills_info = [
        SkillInfo(name="metrics-core-troubleshoot", description="d", source=str(src_dir))]
    orch._copied_skill_dirs = []
    events = []
    orch._sink = events.append

    names = orch._materialize_skills(repo)

    dest = repo / ".claude" / "skills" / "metrics-core-troubleshoot"
    assert "metrics-core-troubleshoot" in names
    assert (dest / "SKILL.md").exists() and (dest / "reference.md").exists()
    assert dest in orch._copied_skill_dirs

    # cleanup removes exactly what was copied
    orch._cleanup_copied_skills()
    assert not dest.exists()
    assert orch._copied_skill_dirs == []


def test_materialize_never_overwrites_a_project_skill(tmp_path, monkeypatch):
    """A repo-local skill with the same name wins; the user copy must not
    clobber it and must not be scheduled for cleanup."""
    import no_human.history.skills as sk
    from no_human.core.orchestrator import Orchestrator
    from no_human.history.skills import SkillInfo

    user_root = tmp_path / "user-skills"
    src_dir = _write_skill(user_root, "helper")
    monkeypatch.setattr(sk, "USER_SKILLS", user_root)

    repo = tmp_path / "repo"
    project_copy = repo / ".claude" / "skills" / "helper"
    project_copy.mkdir(parents=True)
    (project_copy / "SKILL.md").write_text("project version")

    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._active_memories = None
    orch._discovered_skills = ["helper"]
    orch._discovered_skills_info = [
        SkillInfo(name="helper", description="d", source=str(src_dir))]
    orch._copied_skill_dirs = []
    orch._sink = lambda e: None

    names = orch._materialize_skills(repo)

    assert "helper" in names
    assert (project_copy / "SKILL.md").read_text() == "project version"
    assert orch._copied_skill_dirs == []


def test_leftover_copy_is_refreshed_not_adopted(tmp_path, monkeypatch):
    """A leftover from a failed cleanup (marked .nh-copied) must be refreshed
    and re-tracked — not adopted as a project skill, which would permanently
    bypass the relevance filter on that checkout."""
    import no_human.history.skills as sk
    from no_human.core.orchestrator import Orchestrator, _COPIED_SKILL_MARKER
    from no_human.history.skills import SkillInfo

    user_root = tmp_path / "user-skills"
    src_dir = _write_skill(user_root, "metrics-core-troubleshoot")
    monkeypatch.setattr(sk, "USER_SKILLS", user_root)

    repo = tmp_path / "repo"
    leftover = repo / ".claude" / "skills" / "metrics-core-troubleshoot"
    leftover.mkdir(parents=True)
    (leftover / _COPIED_SKILL_MARKER).touch()
    (leftover / "SKILL.md").write_text("stale partial copy")

    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._active_memories = None
    orch._discovered_skills = ["metrics-core-troubleshoot"]
    orch._discovered_skills_info = [
        SkillInfo(name="metrics-core-troubleshoot", description="d", source=str(src_dir))]
    orch._copied_skill_dirs = []
    orch._sink = lambda e: None

    orch._materialize_skills(repo)

    assert "body" in (leftover / "SKILL.md").read_text()  # refreshed content
    assert leftover in orch._copied_skill_dirs             # tracked again


# ------------------------------------------------- change-aware final gate --

def test_rules_block_renders_test_routing_table():
    """The coder must see the SAME change-scoped routing the orchestrator's
    gate uses (task 70e3bd1b: a web-only helper ran the full ~6-min python
    suite and then slept in a loop waiting for it — ~10 of 22 turns wasted)."""
    from no_human.core.prompt_blocks import build_rules_block
    rules = build_rules_block(
        "uv run pytest -q", "", None,
        routing_rules=[{"glob": "web/**", "command": "node --test src/", "cwd": "web"}],
    )
    assert "web/**" in rules
    assert "node --test src/" in rules
    assert "uv run pytest -q" in rules          # default still stated
    assert "sleep" in rules.lower()             # the no-babysit rule names it


def test_rules_block_without_routing_keeps_single_final_gate():
    from no_human.core.prompt_blocks import build_rules_block
    rules = build_rules_block("uv run pytest -q", "", None)
    assert "FINAL GATE" in rules
    assert "web/**" not in rules


def test_implement_prompt_carries_profile_routing(monkeypatch):
    t = _task()
    orch = _orch()
    from no_human.profile import ProjectProfile
    orch._active_profile = ProjectProfile(
        repo_path="/tmp/repo", ecosystem="python",
        test_cmd="uv run pytest -q",
        test_commands=[{"glob": "web/**", "command": "node --test src/", "cwd": "web"}])
    prompt = orch._build_implement_prompt(t, "/tmp/repo")
    assert "web/**" in prompt and "node --test src/" in prompt


def test_compaction_surviving_files_carry_routing_note():
    """instructions.md and the verify skill survive compaction; if they still
    command the full default suite unconditionally, the routing guidance dies
    exactly when it matters (after compaction)."""
    from no_human.core.orchestrator import _routing_note
    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path="/r", ecosystem="python", test_cmd="uv run pytest -q",
        test_commands=[{"glob": "web/**", "command": "node --test src/", "cwd": "web"}])
    note = _routing_note(prof)
    assert "web/**" in note and "node --test src/" in note
    assert _routing_note(ProjectProfile(repo_path="/r")) == ""
