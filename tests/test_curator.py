"""Learning-store curator (D2 #3): never deletes, pinned exempt, fail-closed."""

from __future__ import annotations


from no_human.core.db import SOURCE_PROPOSED
from no_human.learning import ORIGIN_CURATOR, LearningQueue
from no_human.learning.curator import build_curate_prompt, curate, parse_curation


async def _seed(store, n=3, *, title="never push to master", confirmed=False):
    ids = []
    for i in range(n):
        mid = await store.add_memory(
            mem_type="rule", title=title, content=f"{title} — noted",
            confirmed=confirmed, dedupe_key=f"k{title}{i}")
        ids.append(mid)
    return ids


async def test_dedupe_archives_copies_keeps_oldest(store):
    ids = await _seed(store, 3)
    report = await curate(store)
    assert report.duplicates_archived == 2
    live = await store.list_memories(confirmed=False)
    assert [m["id"] for m in live] == [ids[0]]
    # never deleted: recoverable with the audit trail
    all_rows = await store.list_memories(confirmed=False, include_archived=True)
    assert len(all_rows) == 3
    archived = [m for m in all_rows if m["id"] != ids[0]]
    assert all("[archived: duplicate of" in m["content"] for m in archived)


async def test_confirmed_memories_are_never_touched(store):
    await _seed(store, 2, title="the operator's pinned rule", confirmed=True)
    report = await curate(store)
    assert report.duplicates_archived == 0
    assert len(await store.list_memories(confirmed=True)) == 2


async def test_llm_pass_fail_closed_on_garbage(store):
    await _seed(store, 1, title="a")
    await _seed(store, 1, title="b")

    async def bad_llm(prompt):
        return "no json here"
    report = await curate(store, llm_call=bad_llm, apply=True)
    assert report.llm_archive_proposed == []
    assert len(await store.list_memories(confirmed=False)) == 2


async def test_llm_archive_and_consolidate_apply_with_flag(store):
    a = (await _seed(store, 1, title="grep before assuming paths"))[0]
    b = (await _seed(store, 1, title="verify paths with grep first"))[0]
    c = (await _seed(store, 1, title="transient: run 0x3f failed once"))[0]

    async def llm(prompt):
        assert "CURATE_JSON_START" in build_curate_prompt([])
        return (
            "CURATE_JSON_START\n"
            + '{"archive": [{"id": "' + c[:8] + '", "reason": "transient one-off"}],'
            + ' "consolidate": [{"ids": ["' + a[:8] + '", "' + b[:8] + '"],'
            + ' "title": "verify paths with grep before assuming",'
            + ' "content": "Always grep/verify paths before acting."}]}'
            + "\nCURATE_JSON_END")

    # Without apply: proposals only, nothing changes.
    r1 = await curate(store, llm_call=llm)
    assert len(r1.llm_archive_proposed) == 1 and not r1.llm_applied
    assert len(await store.list_memories(confirmed=False)) == 3

    # With apply: c archived; a+b consolidated into ONE NEW UNCONFIRMED
    # proposal (the human gate stands) and archived.
    r2 = await curate(store, llm_call=llm, apply=True)
    live = await store.list_memories(confirmed=False)
    assert len(live) == 1
    assert live[0]["confirmed"] == 0
    assert "consolidated from 2" in live[0]["content"]
    # THE HUMAN GATE ACTUALLY STANDS. This line used to read
    # `live[0]["source"] == "curator"` — it asserted, and so protected, the
    # bug: `source` is the queue-VISIBILITY contract and `pending()` selects
    # source="proposed", so a consolidation archived TWO rows the human could
    # confirm and produced one they could not see. `list_memories(confirmed=
    # False)` above could not catch it because it does not filter on source,
    # which is exactly why the assertion is on `pending()` now — the query the
    # human's confirm queue really runs. The curator names itself in `origin`.
    assert live[0]["source"] == SOURCE_PROPOSED
    assert live[0]["origin"] == ORIGIN_CURATOR
    pending_ids = {m["id"] for m in await LearningQueue(store).pending()}
    assert live[0]["id"] in pending_ids, (
        "a consolidated learning must reach the confirm queue, or the "
        "curator has destroyed two proposals to create an invisible one")


def test_parse_curation_fail_closed_shapes():
    assert parse_curation("") is None
    assert parse_curation("CURATE_JSON_START\nnot json\nCURATE_JSON_END") is None
    assert parse_curation('CURATE_JSON_START\n["list"]\nCURATE_JSON_END') is None
    ok = parse_curation('CURATE_JSON_START\n{"archive": []}\nCURATE_JSON_END')
    assert ok == {"archive": [], "consolidate": []}
