# Tests — the orchestrator's own run

_Harness-captured record for task `4d409824`, commit `ee58870d05aaa94fd061cd5694c34a8ea9abfd3b` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 1,
  "failing_tests": [
    "tests/test_codex_oversized_jsonl_line.py::test_an_event_over_64_kib_is_parsed_and_does_not_kill_the_stream"
  ],
  "failure_blocks": [
    "FAILED tests/test_codex_oversized_jsonl_line.py::test_an_event_over_64_kib_is_parsed_and_does_not_kill_the_stream",
    "\u2014\u2014\u2014 tests/test_codex_oversized_jsonl_line.py::test_an_event_over_64_kib_is_parsed_and_does_not_kill_the_stream \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/4d409824f57a4ef69c97064cd62eafb9.56167.d4084907/.venv/bin/python3\n\noversized_event_cli = '/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-9614/popen-gw3/test_an_event_over_64_kib_is_p0/fake-codex'\ntmp_path = PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-9614/popen-gw3/test_an_event_over_64_kib_is_p0')\nmonkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x117084290>\n\n    async def test_an_event_over_64_kib_is_parsed_and_does_not_kill_the_stream(\n            oversized_event_cli, tmp_path, monkeypatch):\n        \"\"\"RED before the fix: raises\n        ``ValueError(\"Separator is not found, and chunk exceed the limit\")``\n        from inside stream()'s read loop, uncaught \u2014 the exact shape of task\n        78be079a attempt 36 (2026-08-25). GREEN after: both the oversized event\n        and the following normal event parse, and the session completes\n        normally with no exception escaping.\"\"\"\n        _stub_cli(monkeypatch, cli=oversized_event_cli)\n        seen: list[AgentEvent] = []\n    \n        result = await asyncio.wait_for(\n            cx.CodexBackend(env=FAKE_ENV).run(\n                \"p\", cwd=tmp_path, max_turns=9, on_event=seen.append),\n            30)\n    \n>       assert result.is_error is False, (result.fin\n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_codex_oversized_jsonl_line.py::test_an_event_over_64_kib_is_parsed_and_does_not_kill_the_stream"
  ],
  "ok": false,
  "passed": 13558,
  "ran": true,
  "tamper_flag": false
}
```
