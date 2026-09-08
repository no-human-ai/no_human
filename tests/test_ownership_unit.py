"""Unit matrix for `no_human.testing.ownership.owned_failing_ids` — the
resolver `orchestrator.py`'s plain-red branch calls to decide whether a
failing test node id names a test FUNCTION the current attempt's own diff
added or modified (ownership by test id, not by file). Each case builds a
throwaway two-commit git repo and reads the resolver directly, off any
orchestrator machinery — see tests/test_owned_test_attribution.py for the
end-to-end acceptance shapes through `orch.run_task`.

Also covers `orchestrator._attributed_ids`, the pure helper that decides
which failing ids get billed once `owned` is known.
"""

import subprocess

from no_human.core.orchestrator import _attributed_ids
from no_human.testing import ownership


def _repo(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "u@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "u"], cwd=root, check=True)
    return root


def _commit(root, msg):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True,
                   capture_output=True)


# ---------------------------------------------------------------------------
# owned_failing_ids
# ---------------------------------------------------------------------------


def test_added_file_and_modified_function_are_owned_untouched_sibling_is_not(
    tmp_path,
):
    root = _repo(tmp_path, "added_and_modified")
    (root / "test_mod.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
    )
    _commit(root, "base: test_a, test_b")

    (root / "test_mod.py").write_text(
        "def test_a():\n    assert True\n    assert 1 == 1\n\n\n"
        "def test_b():\n    assert True\n"
    )
    (root / "test_new.py").write_text("def test_c():\n    assert True\n")
    _commit(root, "modify test_a, add test_new.py::test_c")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD",
        ["test_mod.py::test_a", "test_mod.py::test_b", "test_new.py::test_c"],
    )
    assert owned == ["test_mod.py::test_a", "test_new.py::test_c"], owned


def test_ids_relative_to_a_routed_cwd_are_owned_through_the_cwd_prefix(tmp_path):
    """Review finding on PR #570: pytest node ids are rootdir/cwd-relative,
    git's diff is repo-root-relative. With the runner routed into `pkg/`,
    the id `tests/test_mod.py::test_a` names `pkg/tests/test_mod.py` — an
    id that, looked up unprefixed, misses the diff ("not owned", the guard
    silently inert) or collides with a same-named ROOT path the diff added
    (falsely owned). `cwd=` prefixes the lookup; the ids come back verbatim."""
    root = _repo(tmp_path, "routed_cwd")
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "tests" / "test_mod.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
    )
    _commit(root, "base: pkg/tests/test_mod.py")

    # The routed project's test_a is modified; an unrelated ROOT-level file
    # with the SAME relative name is ADDED (the collision decoy).
    (root / "pkg" / "tests" / "test_mod.py").write_text(
        "def test_a():\n    assert True\n    assert 1 == 1\n\n\n"
        "def test_b():\n    assert True\n"
    )
    (root / "tests" / "test_mod.py").write_text("def test_zzz():\n    assert True\n")
    _commit(root, "modify pkg test_a; add a root decoy with the same relative name")

    ids = ["tests/test_mod.py::test_a", "tests/test_mod.py::test_b"]
    # Routed cwd: test_a owned (modified), test_b not; ids returned verbatim.
    assert ownership.owned_failing_ids(root, "HEAD~1", "HEAD", ids, cwd="pkg") == [
        "tests/test_mod.py::test_a"]
    # Unprefixed, the same ids hit the root decoy (an ADDED file) and BOTH
    # read as owned — the false attribution the prefix exists to prevent.
    assert ownership.owned_failing_ids(root, "HEAD~1", "HEAD", ids) == ids
    # Root cwd spelled explicitly behaves like no cwd.
    assert ownership.owned_failing_ids(root, "HEAD~1", "HEAD", ids, cwd="") == ids


def test_parametrize_decorator_change_owns_every_variant(tmp_path):
    root = _repo(tmp_path, "parametrize")
    (root / "test_p.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('x', [1])\n"
        "def test_p(x):\n    assert x == 1\n"
    )
    _commit(root, "base: parametrize([1])")

    (root / "test_p.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_p(x):\n    assert x in (1, 2)\n"
    )
    _commit(root, "widen the parametrize list")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD", ["test_p.py::test_p[1]", "test_p.py::test_p[2]"],
    )
    assert owned == ["test_p.py::test_p[1]", "test_p.py::test_p[2]"], owned


def test_class_and_parametrized_node_ids_resolve(tmp_path):
    root = _repo(tmp_path, "class_and_param")
    (root / "test_c.py").write_text(
        "import pytest\n\n\n"
        "class TestC:\n"
        "    def test_a(self):\n        assert True\n\n\n"
        "@pytest.mark.parametrize('x', [1])\n"
        "def test_a(x):\n    assert x == 1\n"
    )
    _commit(root, "base: TestC.test_a and top-level test_a")

    (root / "test_c.py").write_text(
        "import pytest\n\n\n"
        "class TestC:\n"
        "    def test_a(self):\n        assert True\n        assert 1 == 1\n\n\n"
        "@pytest.mark.parametrize('x', [1])\n"
        "def test_a(x):\n    assert x == 1\n"
    )
    _commit(root, "modify only TestC.test_a's body")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD",
        ["test_c.py::TestC::test_a", "test_c.py::test_a[x1]"],
    )
    assert owned == ["test_c.py::TestC::test_a"], owned


def test_directly_modified_fixture_owns_its_users_but_not_transitive_ones(
    tmp_path,
):
    root = _repo(tmp_path, "fixtures")
    (root / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def f2():\n    return 1\n\n\n"
        "@pytest.fixture\n"
        "def f_mid(f2):\n    return f2 + 1\n"
    )
    (root / "test_fx.py").write_text(
        "def test_direct(f2):\n    assert f2 == 1\n\n\n"
        "def test_indirect(f_mid):\n    assert f_mid == 2\n"
    )
    _commit(root, "base: f2, f_mid, two tests")

    (root / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def f2():\n    return 1  # modified\n\n\n"
        "@pytest.fixture\n"
        "def f_mid(f2):\n    return f2 + 1\n"
    )
    _commit(root, "modify only f2's body")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD", ["test_fx.py::test_direct", "test_fx.py::test_indirect"],
    )
    assert owned == ["test_fx.py::test_direct"], (
        "test_indirect only requests f_mid directly — f_mid itself is "
        "untouched, so it must not be owned via f2 transitively: " + str(owned)
    )


def test_unresolvable_id_is_owned_only_when_its_file_was_added(tmp_path):
    root = _repo(tmp_path, "unresolvable")
    (root / "existing.py").write_text("def test_e():\n    assert True\n")
    _commit(root, "base")

    (root / "existing.py").write_text(
        "def test_e():\n    assert True\n\n\ndef test_f():\n    assert True\n"
    )
    (root / "brand_new.py").write_text("def test_new():\n    assert True\n")
    _commit(root, "add brand_new.py, add test_f to existing.py")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD",
        [
            "no_double_colon_here",  # unparseable id -> never owned
            "existing.py::test_missing_func",  # modified file, function absent
            "brand_new.py::test_anything_not_actually_there",  # added file
        ],
    )
    assert owned == ["brand_new.py::test_anything_not_actually_there"], owned


def test_renamed_file_is_not_treated_as_added(tmp_path):
    root = _repo(tmp_path, "renamed")
    (root / "old_name.py").write_text("def test_r():\n    assert True\n")
    _commit(root, "base")

    subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=root, check=True)
    _commit(root, "pure rename, no content change")

    owned = ownership.owned_failing_ids(root, "HEAD~1", "HEAD", ["new_name.py::test_r"])
    assert owned == [], (
        "a pure rename (git status 'R', not 'A') with no content change must "
        "not own an untouched function: " + str(owned)
    )


def test_total_resolution_failure_owns_nothing(tmp_path):
    root = _repo(tmp_path, "resolution_failure")
    (root / "a.py").write_text("def test_a():\n    assert True\n")
    _commit(root, "one commit only")

    owned = ownership.owned_failing_ids(
        root, "this-ref-does-not-exist", "HEAD", ["a.py::test_a"]
    )
    assert owned == [], (
        "a bad before_ref must fail the WHOLE resolution closed, not just "
        "the one id: " + str(owned)
    )


def test_node_id_owned_when_the_diff_modifies_or_adds_its_file(tmp_path):
    """Round-3 review MAJOR: `runner` used to never populate `failing_tests`
    for node runs, so a node id could never even reach `owned_failing_ids` —
    this exercises the file-scoped half of `_is_owned`'s dispatch
    (`parse_file_scoped_id` / `_is_owned_file_scoped`) directly, off the same
    real two-commit git repo harness as every `.py` case above, no stubbing."""
    root = _repo(tmp_path, "node_file_scoped")
    (root / "x.test.mjs").write_text("// base\n")
    (root / "untouched.test.mjs").write_text("// never touched\n")
    _commit(root, "base: x.test.mjs, untouched.test.mjs")

    (root / "x.test.mjs").write_text("// modified by this attempt\n")
    (root / "y.test.mjs").write_text("// a brand new node test file\n")
    _commit(root, "modify x.test.mjs, add y.test.mjs")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD",
        [
            "x.test.mjs::it fails",          # file MODIFIED -> owned
            "y.test.mjs::a new failure",      # file ADDED -> owned
            "untouched.test.mjs::a failure",  # file untouched -> not owned
        ],
    )
    assert owned == ["x.test.mjs::it fails", "y.test.mjs::a new failure"], owned


def test_node_id_deleted_file_is_never_owned(tmp_path):
    root = _repo(tmp_path, "node_deleted")
    (root / "z.test.mjs").write_text("// base\n")
    _commit(root, "base: z.test.mjs")

    (root / "z.test.mjs").unlink()
    _commit(root, "delete z.test.mjs")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD", ["z.test.mjs::a failure"],
    )
    assert owned == [], (
        "a failing id cannot live in a file the diff DELETED: " + str(owned)
    )


def test_node_id_without_a_location_is_never_owned_fail_closed(tmp_path):
    """A node TAP failure with no `location:` line (a bare name, no `::`) has
    no file for `parse_file_scoped_id` to resolve — fail-closed: it is never
    returned as owned, even when the diff adds every file in the repo. This
    must NOT be read as "safe to excuse" — an id ownership cannot resolve is
    only ever billed to the attempt by the caller, never excused as
    environment or pre-existing (see `owned_failing_ids`'s docstring)."""
    root = _repo(tmp_path, "node_bare_name")
    (root / "a.test.mjs").write_text("// base\n")
    _commit(root, "base")
    (root / "a.test.mjs").write_text("// modified\n")
    _commit(root, "modify a.test.mjs")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD", ["a bare failing test name with no file at all"],
    )
    assert owned == [], owned


def test_node_id_with_an_absolute_or_escaping_path_is_never_owned(tmp_path):
    root = _repo(tmp_path, "node_abs_or_escape")
    (root / "b.test.mjs").write_text("// base\n")
    _commit(root, "base")
    (root / "b.test.mjs").write_text("// modified\n")
    _commit(root, "modify b.test.mjs")

    owned = ownership.owned_failing_ids(
        root, "HEAD~1", "HEAD",
        [
            "/abs/b.test.mjs::it fails",
            "../escapes/b.test.mjs::it fails",
        ],
    )
    assert owned == [], owned


def test_parse_file_scoped_id_rejects_python_paths_and_bad_shapes():
    from no_human.testing.ownership import parse_file_scoped_id
    assert parse_file_scoped_id("no_double_colon_here") is None
    assert parse_file_scoped_id("test_x.py::test_a") is None, (
        ".py paths stay on the precise per-function parse_node_id path"
    )
    assert parse_file_scoped_id("/abs/x.test.mjs::name") is None
    assert parse_file_scoped_id("../escapes/x.test.mjs::name") is None
    assert parse_file_scoped_id("x.test.mjs::name") == "x.test.mjs"
    assert parse_file_scoped_id("web/src/x.test.mjs::name") == "web/src/x.test.mjs"


def test_parse_node_id_rejects_shapes_that_are_not_a_python_test_id():
    assert ownership.parse_node_id("no_colons_here") is None
    assert ownership.parse_node_id("not_python.txt::test_x") is None
    assert ownership.parse_node_id("/abs/path/test_x.py::test_x") is None
    assert ownership.parse_node_id("../escapes/test_x.py::test_x") is None
    assert ownership.parse_node_id("test_x.py::") is None


def test_parse_node_id_strips_bracket_suffix_from_last_segment_only():
    assert ownership.parse_node_id("t.py::TestC::test_a[1-2]") == (
        "t.py", ("TestC",), "test_a",
    )
    assert ownership.parse_node_id("t.py::test_a") == ("t.py", (), "test_a")


# ---------------------------------------------------------------------------
# _attributed_ids
# ---------------------------------------------------------------------------


def test_attributed_ids_owned_empty_reproduces_todays_rule_byte_for_byte():
    # inconclusive base check (None) -> fall back to every failing id
    assert _attributed_ids(["a", "b"], None, []) == ["a", "b"]
    # base check ran, found every id pre-existing ([]) -> `[] or failing`
    assert _attributed_ids(["a", "b"], [], []) == ["a", "b"]
    # base check isolated a subset -> exactly that subset
    assert _attributed_ids(["a", "b", "c"], ["b"], []) == ["b"]


def test_attributed_ids_newly_failing_empty_with_owned_bills_only_owned():
    # base check found EVERY id pre-existing, but one is also owned: bill
    # only the owned id, not the genuinely pre-existing siblings.
    assert _attributed_ids(["a", "b", "c"], [], ["b"]) == ["b"]


def test_attributed_ids_mixed_is_an_ordered_union_in_input_order():
    assert _attributed_ids(["a", "b", "c", "d"], ["b"], ["d"]) == ["b", "d"]
    # input-order preserved, not the order either source list names them
    assert _attributed_ids(["d", "c", "b", "a"], ["b"], ["d"]) == ["d", "b"]
