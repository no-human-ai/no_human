// The confirm queue floods (live: 193 pending, mostly rules mined from ~89
// past conversations across many repos). A flat wall of 193 cards is
// untriageable. Group proposals by the repo the conversation happened in
// (memory.project — now carried through from Transcript.workspaces), so the
// human triages a handful of labeled, counted groups and can recognize the
// big unscoped conversation-mining backlog at a glance. Pure, node --test'd.
import { basename } from "./pathBasename.js";

const UNSCOPED = "__unscoped__";

// A short, human-readable label for a project path (its last path segment),
// or "Unscoped" when the proposal carries no project.
export function projectLabel(project) {
  const p = (project || "").trim();
  if (!p) return "Unscoped";
  const seg = basename(p);
  return seg || p;
}

// Group learning proposals by project. Returns groups sorted by size (largest
// first) so the biggest triage targets surface at the top; ties break
// alphabetically by label for a stable order. Unscoped always sorts last among
// equal sizes so real repos lead. Each group: { key, label, count, items }.
export function groupLearningsByProject(items) {
  const groups = new Map();
  for (const it of items || []) {
    const key = (it.project || "").trim() || UNSCOPED;
    let g = groups.get(key);
    if (!g) {
      g = { key, label: key === UNSCOPED ? "Unscoped" : projectLabel(key), items: [] };
      groups.set(key, g);
    }
    g.items.push(it);
  }
  const out = [...groups.values()].map((g) => ({ ...g, count: g.items.length }));
  out.sort((a, b) => {
    if (b.count !== a.count) return b.count - a.count;
    if (a.key === UNSCOPED) return 1;      // unscoped last on a tie
    if (b.key === UNSCOPED) return -1;
    return a.label.localeCompare(b.label);
  });
  return out;
}
