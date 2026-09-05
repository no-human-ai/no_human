import { taskBurn, taskCost } from "./cost.js";

// Cost rolled up per project. The per-task cost is already on screen and the lifetime total
// is already a tile, but nothing sat between them: "what has this repo cost me" took manual
// addition across the task table. That is the question a freelancer bills from.
//
// Grouped by repo_name because that is the only client-ish key the task carries. Tasks with
// no repo fall into one "Unattributed" bucket rather than being dropped: every task the page
// lists must land in exactly one row, or the rows stop summing to the total.

export const UNATTRIBUTED = "Unattributed";

/**
 * [{ project, tasks, cost }], largest cost first; ties broken by name so the order is stable.
 * Zero-cost projects are kept: "0 tasks priced" is information, and dropping them would make
 * the rows sum to less than the page's own total.
 *
 * Keyed case-insensitively — `no_human` and `No_Human` are one project, not two — while the
 * first spelling seen is what gets displayed. Whitespace was already normalised, so leaving
 * case alone made the normalisation half-done.
 */
export function costByProject(tasks) {
  const byProject = new Map();
  for (const t of tasks || []) {
    const name = (t.repo_name || "").trim() || UNATTRIBUTED;
    const g = byProject.get(name.toLowerCase())
      || { project: name, tasks: 0, cost: 0, tokens: 0 };
    g.tasks += 1;
    g.cost += taskCost(t);
    // Same nine buckets `cost` prices (SCRUM re-home) — cost and burn move
    // together closely enough that sorting stays keyed on `cost` alone (see
    // below), so subscription mode can read this column without a second sort.
    g.tokens += taskBurn(t);
    byProject.set(name.toLowerCase(), g);
  }
  // Sort key stays `cost`, not `tokens`: changing it would reorder rows out
  // from under the existing (dollar-mode) ordering assertions.
  return [...byProject.values()].sort(
    (a, b) => b.cost - a.cost || a.project.localeCompare(b.project),
  );
}

/** The sum of the rows above — so the header total and the rows cannot disagree. */
export function totalCost(groups) {
  return (groups || []).reduce((sum, g) => sum + g.cost, 0);
}

/** The token-basis sibling of `totalCost`, for the subscription-mode table. */
export function totalTokens(groups) {
  return (groups || []).reduce((sum, g) => sum + g.tokens, 0);
}
