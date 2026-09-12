"""Tests for the repo-wiki generator (docs_gen.py).

Covers: JSON parsing, file writing, CLAUDE.md delimiter logic (the critical
replace-in-place behavior), and the WikiGenerator integration with a fake
backend.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from no_human.docs_gen import (
    WIKI_DIR,
    WikiGenerator,
    WikiResult,
    _parse_wiki_json,
    _write_wiki_files,
    upsert_wiki_block,
    _WIKI_BLOCK_START,
    _WIKI_BLOCK_END,
)


# --------------------------------------------------------------------------- #
# _parse_wiki_json                                                             #
# --------------------------------------------------------------------------- #

def test_parse_wiki_json_valid():
    text = (
        'Here is the wiki:\n```json\n'
        '{"architecture": "# Arch", "modules": "# Mods", "conventions": "# Conv"}\n'
        '```\n'
    )
    result = _parse_wiki_json(text)
    assert result is not None
    assert result["architecture"] == "# Arch"
    assert result["modules"] == "# Mods"
    assert result["conventions"] == "# Conv"


def test_parse_wiki_json_no_block():
    assert _parse_wiki_json("no json here") is None


def test_parse_wiki_json_missing_keys():
    text = '```json\n{"unrelated": "data"}\n```'
    assert _parse_wiki_json(text) is None


def test_parse_wiki_json_partial_keys():
    text = '```json\n{"architecture": "# Arch"}\n```'
    result = _parse_wiki_json(text)
    assert result is not None
    assert result["architecture"] == "# Arch"


# --------------------------------------------------------------------------- #
# _write_wiki_files                                                            #
# --------------------------------------------------------------------------- #

def test_write_wiki_files(tmp_path):
    data = {"architecture": "# Architecture", "modules": "# Modules"}
    written = _write_wiki_files(tmp_path, data)
    assert ".no_human/wiki/architecture.md" in written
    assert ".no_human/wiki/modules.md" in written
    assert (tmp_path / WIKI_DIR / "architecture.md").read_text(encoding="utf-8") == "# Architecture\n"
    assert (tmp_path / WIKI_DIR / "modules.md").read_text(encoding="utf-8") == "# Modules\n"
    # conventions.md not written (empty content)
    assert not (tmp_path / WIKI_DIR / "conventions.md").exists()


def test_write_wiki_files_empty_data(tmp_path):
    written = _write_wiki_files(tmp_path, {})
    assert written == []


# --------------------------------------------------------------------------- #
# upsert_wiki_block — the critical delimiter logic                             #
# --------------------------------------------------------------------------- #

def test_upsert_wiki_block_fresh_empty_file():
    result = upsert_wiki_block("")
    assert _WIKI_BLOCK_START in result
    assert _WIKI_BLOCK_END in result
    assert result.count(_WIKI_BLOCK_START) == 1


def test_upsert_wiki_block_fresh_nonempty_file():
    original = "# My Project\n\nSome custom content.\n"
    result = upsert_wiki_block(original)
    # Original content preserved.
    assert result.startswith("# My Project\n\nSome custom content.\n")
    assert _WIKI_BLOCK_START in result
    assert result.count(_WIKI_BLOCK_START) == 1


def test_upsert_wiki_block_replaces_not_duplicates():
    """Running upsert twice must produce exactly one block — the critical
    invariant that prevents context cost creep."""
    first = upsert_wiki_block("# Existing\n")
    second = upsert_wiki_block(first)
    assert second.count(_WIKI_BLOCK_START) == 1
    assert second.count(_WIKI_BLOCK_END) == 1
    # Content outside the block unchanged.
    assert second.startswith("# Existing\n")


def test_upsert_wiki_block_preserves_surrounding_content():
    """User content before and after the block must survive a regeneration."""
    text = (
        "# Rules\n\nDo not break things.\n\n"
        f"{_WIKI_BLOCK_START}\nold wiki pointer\n{_WIKI_BLOCK_END}\n\n"
        "## Custom section\n\nKeep this.\n"
    )
    result = upsert_wiki_block(text)
    assert "# Rules" in result
    assert "Do not break things." in result
    assert "## Custom section" in result
    assert "Keep this." in result
    assert "old wiki pointer" not in result  # replaced
    assert result.count(_WIKI_BLOCK_START) == 1


def test_upsert_wiki_block_three_runs_still_one_block():
    text = ""
    for _ in range(3):
        text = upsert_wiki_block(text)
    assert text.count(_WIKI_BLOCK_START) == 1


# --------------------------------------------------------------------------- #
# WikiGenerator with fake backend                                              #
# --------------------------------------------------------------------------- #

class FakeBackend:
    """Returns canned agent output for testing WikiGenerator."""

    def __init__(self, output: str = "", *, final_text: str | None = None,
                 structured_output=None):
        self.output = final_text if final_text is not None else output
        self.structured_output = structured_output
        self.calls: list[dict] = []

    async def run(self, prompt, *, cwd, max_turns, effort, output_format=None):
        self.calls.append({"prompt": prompt, "cwd": str(cwd),
                           "max_turns": max_turns, "effort": effort,
                           "output_format": output_format})
        return SimpleNamespace(final_text=self.output,
                               structured_output=self.structured_output)


@pytest.fixture
def fake_repo(tmp_path):
    """A minimal git repo for testing."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()  # fake git dir
    return repo


def test_wiki_generator_success(fake_repo):
    output = (
        '```json\n{"architecture": "# High-level design\\n\\nThis is the arch.",'
        ' "modules": "# Modules\\n\\nSource layout.",'
        ' "conventions": "# Conventions\\n\\nCode style."}\n```'
    )
    backend = FakeBackend(output)
    gen = WikiGenerator(backend, max_turns=12)
    result = asyncio.run(gen.generate(fake_repo))
    assert result.error is None
    assert len(result.files_written) == 3
    assert (fake_repo / WIKI_DIR / "architecture.md").exists()
    assert (fake_repo / WIKI_DIR / "modules.md").exists()
    assert (fake_repo / WIKI_DIR / "conventions.md").exists()
    # CLAUDE.md created with the wiki block.
    claude_md = (fake_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert _WIKI_BLOCK_START in claude_md
    assert claude_md.count(_WIKI_BLOCK_START) == 1
    # Backend was called with correct bounds.
    assert backend.calls[0]["max_turns"] == 12
    assert backend.calls[0]["effort"] == "low"


def test_wiki_generator_bad_output(fake_repo):
    backend = FakeBackend("I couldn't analyze the repo, sorry.")
    gen = WikiGenerator(backend)
    result = asyncio.run(gen.generate(fake_repo))
    assert "failed to parse" in result.error
    assert result.files_written == []


def test_wiki_generator_not_a_directory(tmp_path):
    backend = FakeBackend("")
    gen = WikiGenerator(backend)
    result = asyncio.run(gen.generate(tmp_path / "nope"))
    assert "not a directory" in result.error


def test_wiki_generator_regeneration_replaces_block(fake_repo):
    """Running generation twice must not duplicate the CLAUDE.md block."""
    output = '```json\n{"architecture": "# Arch v1"}\n```'
    backend = FakeBackend(output)
    gen = WikiGenerator(backend, max_turns=12)
    asyncio.run(gen.generate(fake_repo))
    # Second run.
    backend.output = '```json\n{"architecture": "# Arch v2"}\n```'
    asyncio.run(gen.generate(fake_repo))
    claude_md = (fake_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_md.count(_WIKI_BLOCK_START) == 1
    # Latest content.
    assert (fake_repo / WIKI_DIR / "architecture.md").read_text(encoding="utf-8").startswith("# Arch v2")


import subprocess as _sp


def _init_repo(path):
    _sp.run(["git", "init", "-q"], cwd=path, check=True)
    _sp.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    _sp.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    _sp.run(["git", "add", "-A"], cwd=path, check=True)
    _sp.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return _sp.run(["git", "rev-parse", "HEAD"], cwd=path,
                   capture_output=True, text=True).stdout.strip()


async def test_wiki_refresh_skips_when_head_unchanged(tmp_path):
    """W3.6: HEAD == last wiki commit → no backend call, skipped=True."""
    sha = _init_repo(tmp_path)
    fake = FakeBackend('```json\n{"architecture":"a","modules":"m","conventions":"c"}\n```')
    res = await WikiGenerator(fake).generate(tmp_path, since_sha=sha)
    assert res.skipped is True
    assert fake.calls == []          # zero cost when nothing changed


async def test_wiki_refresh_uses_diff_prompt_when_head_moved(tmp_path):
    sha = _init_repo(tmp_path)
    (tmp_path / "b.py").write_text("y = 2\n")
    _sp.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    _sp.run(["git", "commit", "-qm", "add b"], cwd=tmp_path, check=True)
    fake = FakeBackend('```json\n{"architecture":"a","modules":"m","conventions":"c"}\n```')
    res = await WikiGenerator(fake).generate(tmp_path, since_sha=sha)
    assert res.skipped is False
    assert len(fake.calls) == 1
    assert "UPDATE" in fake.calls[0]["prompt"]      # refresh prompt, not full
    assert "b.py" in fake.calls[0]["prompt"]         # the diff seeded it


async def test_wiki_full_generation_without_since_sha(tmp_path):
    _init_repo(tmp_path)
    fake = FakeBackend('```json\n{"architecture":"a","modules":"m","conventions":"c"}\n```')
    res = await WikiGenerator(fake).generate(tmp_path)
    assert res.skipped is False
    assert len(fake.calls) == 1
    assert "UPDATE" not in fake.calls[0]["prompt"]   # full-generation prompt


# --------------------------------------------------------------------------- #
# F2: structured-output-first, lenient fence/bare-object fallback, WHY errors  #
# --------------------------------------------------------------------------- #

from no_human import docs_gen as _docs_gen  # noqa: E402


def test_parse_accepts_uppercase_fence():
    assert _parse_wiki_json('```JSON\n{"architecture":"a"}\n```') == {"architecture": "a"}


def test_parse_accepts_bare_fence_and_prose():
    assert _parse_wiki_json('Here you go:\n```\n{"modules":"m"}\n```\nDone.') == {"modules": "m"}


def test_parse_accepts_bare_object():
    assert _parse_wiki_json('{"conventions":"c"}') == {"conventions": "c"}


def test_parse_repairs_bad_escapes():
    assert _parse_wiki_json('```json\n{"architecture":"path C:\\\\x \\d"}\n```')["architecture"].startswith("path")


async def test_generate_prefers_structured_output(fake_repo):
    fake_backend = FakeBackend(final_text="no json here",
                               structured_output={"architecture": "A", "modules": "M", "conventions": "C"})
    res = await WikiGenerator(fake_backend).generate(fake_repo)
    assert res.error is None and len(res.files_written) == 3
    assert fake_backend.calls[0]["output_format"]["schema"] == _docs_gen.WIKI_SCHEMA


async def test_generate_error_carries_output_excerpt(fake_repo):
    fake_backend = FakeBackend(final_text="I could not complete the analysis because the repo is huge",
                               structured_output=None)
    res = await WikiGenerator(fake_backend).generate(fake_repo)
    assert res.error.startswith("failed to parse wiki JSON from agent output: I could not complete")
