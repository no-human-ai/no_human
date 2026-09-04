# Assumptions

_Harness-captured record for task `e881ecd7`, commit `807f673c9a9e5138eced319f9c2abf9b04b0ebe2` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 5 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the Evaluator class (full module path and class name), and does it already have a from_dict method? **A:** Full module path and class name likely: src/no_human/core/evaluator.py::Evaluator (or possibly inline in orchestrator.py::Evaluator). The from_dict method likely does not exist yet, based on the task spec saying 'add a small from_dict if none exists', implying current absence. _(assumption)_
- **Q:** What is the exact context key name where _act_on_eval stores the split proposal on DECOMPOSE verdict? **A:** The split proposal is most likely stored in the context key 'split_proposal', following the naming pattern established by 'original_criteria' and 'assumptions'. The task explicitly references DECOMPOSE attaching 'a split proposal' without specifying an alternative key name. _(assumption)_
- **Q:** What is the signature of update_task? How does it accept the modified task/context for writing? **A:** The update_task signature is most likely: update_task(task: Task) or update_task(task_id: str, context: dict) based on typical store patterns. The task description states '_act_on_eval does whole-Task update_task writes', suggesting the full Task object is passed or task_id with a context dict is the conventional call pattern for this codebase. _(assumption)_
- **Q:** Does _act_on_eval modify any context keys beyond original_criteria, assumptions, and split_proposal? **A:** Based on the task description, _act_on_eval modifies exactly three context keys: original_criteria, assumptions, and split_proposal (the decomposition key). No other keys are mentioned as owned by _act_on_eval. Any additional verdict-related fields (e.g., eval_acted marker) would be set separately, not inside _act_on_eval's merge loop. _(assumption)_
- **Q:** Do existing tests for _act_on_eval and dispatch eval require real model API credentials to run, or can they be fully mocked? **A:** HUMAN-GATED: not self-answerable

</details>

