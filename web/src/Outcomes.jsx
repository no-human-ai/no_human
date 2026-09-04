import { useState, useRef } from "react";
import TaskTable from "./TaskTable.jsx";
import SlideOver from "./SlideOver.jsx";
import { routeTask, isRealFailure } from "./boardLanes.js";
import { groupFailedByTitle } from "./boardGroups.js";
import { pluralize } from "./pluralize.js";

// 5D: the Done and Failed screens. These are OUTCOMES, not gates — they left the board so the
// three lanes that need the human get the width, and an outcome list is read as a sortable table
// (date, duration, tokens, cost), not as a column of cards.
//
// Rows open the SAME drawer the board cards open, so nothing is lost by leaving the board.

export default function Outcomes({ tasks, lane, onFollowUp = null, authMode }) {
  const [selectedId, setSelectedId] = useState(null);
  const triggerRef = useRef(null);
  const isFailedLane = lane === "failed";

  const laneTasks = tasks.filter((t) => routeTask(t) === lane);
  // One stubborn task retried five ways is a graveyard, not five failures: same-title runs
  // collapse to ONE row (a real failure always heads its group — a cancel that merely happens
  // to be newer must not hide it) with a "+N older" note. This is the collapse that used to
  // protect the board's Failed lane; the failures live here now, so it does too.
  const rows = isFailedLane
    ? groupFailedByTitle(laneTasks).map((g) => ({ ...g.task, _collapsed: g.collapsedCount }))
    : laneTasks;
  // A cancelled task ends in `failed` status but is NOT a capability failure. It is shown here
  // (this is the outcome list) but counted separately — "11 failed" that was really 1 failure and
  // 10 cancels is exactly the alarm this product must not raise.
  // Counts are over the RAW tasks, not the collapsed rows — the summary must not under-report.
  const failures = laneTasks.filter(isRealFailure).length;
  const cancelled = laneTasks.length - failures;

  const title = isFailedLane ? "Failed" : "Done";
  const summary = isFailedLane
    ? `${failures} ${pluralize(failures, "failure")}${cancelled > 0 ? ` · ${cancelled} cancelled` : ""}`
    : `${laneTasks.length} ${pluralize(laneTasks.length, "task")} shipped`;

  function open(id, node) {
    triggerRef.current = node;
    setSelectedId(id);
  }
  function close() {
    setSelectedId(null);
    triggerRef.current?.focus();
    triggerRef.current = null;
  }

  return (
    <div className="outcome-page">
      <h2 className={`outcome-title outcome-title-${lane}`}>{title}</h2>
      <div className="outcome-sub">{summary}</div>
      <TaskTable
        tasks={rows}
        onSelect={open}
        emptyHint={isFailedLane ? "No failures — nothing has broken." : "Nothing shipped yet."}
        authMode={authMode}
      />
      {selectedId && (
        <SlideOver
          taskId={selectedId}
          key={selectedId}
          onClose={close}
          onJump={(id) => setSelectedId(id)}
          onFollowUp={onFollowUp}
        />
      )}
    </div>
  );
}
