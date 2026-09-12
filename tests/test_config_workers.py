"""`config.set_concurrency` — the Settings worker-count row's writer.

Covers the happy path (both scalars land and reload back), each shape guard
(the positive controls: an out-of-range int, a non-int, a bool-as-int, a
non-bool `enabled`, and the no-args call all raise), and the
create-the-section-when-absent branch.
"""
import pytest

from no_human import config
from no_human.config import AuthError, load_config, set_concurrency


def _seed(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_sets_both_scalars_and_they_reload(tmp_path):
    p = _seed(tmp_path, "concurrency:\n  enabled: false\n  max_workers: 2\n")
    out = set_concurrency(p, max_workers=8, enabled=True)
    assert out == {"max_workers": 8, "enabled": True}
    data = load_config(p).data["concurrency"]
    assert data["max_workers"] == 8 and data["enabled"] is True


def test_sets_only_the_key_passed_leaving_the_other(tmp_path):
    p = _seed(tmp_path, "concurrency:\n  enabled: true\n  max_workers: 4\n")
    set_concurrency(p, max_workers=1)
    data = load_config(p).data["concurrency"]
    assert data["max_workers"] == 1
    assert data["enabled"] is True  # untouched


def test_creates_the_section_when_absent(tmp_path):
    p = _seed(tmp_path, "server:\n  port: 8420\n")
    set_concurrency(p, max_workers=3, enabled=True)
    data = load_config(p).data["concurrency"]
    assert data["max_workers"] == 3 and data["enabled"] is True


@pytest.mark.parametrize("bad", [0, -1, 65, 1000])
def test_rejects_out_of_range_max_workers(tmp_path, bad):
    p = _seed(tmp_path, "concurrency:\n  max_workers: 2\n")
    with pytest.raises(ValueError, match="between 1 and"):
        set_concurrency(p, max_workers=bad)
    assert load_config(p).data["concurrency"]["max_workers"] == 2  # unchanged


def test_rejects_bool_as_max_workers(tmp_path):
    # bool is an int subclass; a True slipping through would write `max_workers: True`.
    p = _seed(tmp_path, "concurrency:\n  max_workers: 2\n")
    with pytest.raises(ValueError, match="must be an int"):
        set_concurrency(p, max_workers=True)


def test_rejects_non_int_max_workers(tmp_path):
    p = _seed(tmp_path, "concurrency:\n  max_workers: 2\n")
    with pytest.raises(ValueError, match="must be an int"):
        set_concurrency(p, max_workers="4")  # type: ignore[arg-type]


def test_rejects_non_bool_enabled(tmp_path):
    p = _seed(tmp_path, "concurrency:\n  enabled: false\n")
    with pytest.raises(ValueError, match="must be a bool"):
        set_concurrency(p, enabled="yes")  # type: ignore[arg-type]


def test_no_args_is_a_programming_error(tmp_path):
    p = _seed(tmp_path, "concurrency:\n  max_workers: 2\n")
    with pytest.raises(ValueError, match="nothing to set"):
        set_concurrency(p)


def test_restores_file_when_a_duplicate_write_would_result(tmp_path, monkeypatch):
    # Same discipline as set_model_ids: a splice that duplicates the top-level
    # key must abort and restore, never leave a half-written file.
    def _buggy(lines, key, value):
        lines.extend(["concurrency:", f"  {key}: {value}"])

    monkeypatch.setattr(config, "_splice_concurrency_scalar", _buggy)
    seed = "concurrency:\n  max_workers: 2\n"
    p = _seed(tmp_path, seed)
    with pytest.raises(AuthError, match=r"duplicate top-level key.*'concurrency'"):
        set_concurrency(p, max_workers=8)
    assert p.read_text(encoding="utf-8") == seed
