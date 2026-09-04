"""v10+v11 two-run truncation class (ns-7ef821b2): the design_doc directive
forbade repository edits AND demanded the complete document in the final
message — mandating exactly the shape that truncates (v10 cut mid-Approach-C;
v11 cut mid-'### 4a. Exact steps', empty diff, no file ever written; both
delivered docs died in the report field)."""

from __future__ import annotations


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend


async def test_design_doc_deliverable_is_a_file_not_the_final_message(store, tmp_path):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("write a design doc", repo_path=str(tmp_path))
    t.kind = "design_doc"
    d = orch._kind_directive(t)
    low = d.lower()
    # The document must be WRITTEN TO A FILE...
    assert "write the document to a file" in low
    assert "docs/" in d
    # ...with the final report a summary + pointer, never the whole doc.
    assert "summary" in low
    assert "file path" in low
    # The truncation-mandating instruction is gone.
    assert "deliver the complete document as your final message" not in low
    # Grounding rules survive.
    assert "file:line" in low
    assert "assumption" in low


async def test_design_doc_still_forbids_code_edits(store, tmp_path):
    cfg = load_config(tmp_path / "c.yaml")
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("write a design doc", repo_path=str(tmp_path))
    t.kind = "design_doc"
    d = orch._kind_directive(t)
    # Writing the doc file is allowed; changing source code is not.
    assert "do not modify source code" in d.lower()
