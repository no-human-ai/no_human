import { useMemo } from "react";
import { fmtCost, fmtTokens, taskBurn, taskCost, pricingIsReal } from "./cost.js";
import { httpPrUrl } from "./prUrl.js";
import { failedReasonLine } from "./cardBlockerLine.js";
import { parseTimestamp } from "./parseTimestamp.js";

// The task table, extracted from Stats.jsx so the outcome screens (5D: Done / Failed) show the
// SAME table instead of a second one that would drift out of step with it.
//
// `onSelect` is optional: on Stats the table is a read-out, and on an outcome screen every row
// opens the task drawer — the same drawer the board cards open, with the same keyboard contract
// (Enter must open it, and preventDefault is load-bearing — see Board.jsx).

const STATUS_DOT = {
  done: "var(--green)",
  failed: "var(--red)",
  implementing: "var(--accent-500)",
  reviewing: "var(--amber)",
  testing: "var(--purple)",
};

function parseTS(ts) {
  // Delegates to the shared UTC-aware helper (web/src/parseTimestamp.js) —
  // a naive DB timestamp must parse as UTC, not local.
  return parseTimestamp(ts);
}

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

export default function TaskTable({ tasks, onSelect = null, emptyHint = null, authMode }) {
  // Same rule as Stats.jsx: only api_key (BYO) pays per token for real, so only that mode
  // gets the "Est. Cost" column at all — the header and its cells share this ONE boolean so
  // the <th> count and <td> count can never drift apart (tableA11y.test.mjs).
  const realDollars = pricingIsReal(authMode);
  const rows = useMemo(() => {
    return tasks
      .map((t) => {
        const created = parseTS(t.created_at);
        const updated = parseTS(t.updated_at);
        const duration =
          created && updated ? (updated.getTime() - created.getTime()) / 1000 : null;
        return { ...t, duration, _sortKey: updated ? updated.getTime() : 0 };
      })
      .sort((a, b) => b._sortKey - a._sortKey);
  }, [tasks]);

  if (rows.length === 0) {
    return emptyHint ? <div className="outcome-empty">{emptyHint}</div> : null;
  }

  return (
    <div className="stats-table-wrap">
      <table className="stats-table">
        <thead>
          <tr>
            <th className="stats-th stats-th-name">Task</th>
            <th className="stats-th">Status</th>
            <th className="stats-th">Duration</th>
            <th className="stats-th">Project</th>
            <th className="stats-th stats-th-right">Tokens</th>
            {realDollars && <th className="stats-th stats-th-right">Est. Cost</th>}
          </tr>
        </thead>
        {/* ph-no-capture: every row renders operator content (task titles,
            PR URLs, repo names) — the whole body is masked from replay. */}
        <tbody className="ph-no-capture">
          {rows.map((t) => (
            <tr
              key={t.id}
              // Cancelled tasks end in `failed` status but aren't capability
              // failures — recede them so the eye lands on real failures. See
              // Outcomes.jsx, which counts the two apart in the header.
              className={`stats-tr${onSelect ? " stats-tr-clickable" : ""}${t.cancelled ? " stats-tr-cancelled" : ""}`}
              {...(onSelect
                ? {
                    role: "button",
                    tabIndex: 0,
                    "aria-label": `Open ${t.title}`,
                    onClick: (e) => onSelect(t.id, e.currentTarget),
                    onKeyDown: (e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        // Without preventDefault the drawer opens, autofocuses its close button
                        // in the same event flush, and Enter's default activation clicks it —
                        // the drawer opens and shuts within one keypress (Board.jsx, B2).
                        e.preventDefault();
                        onSelect(t.id, e.currentTarget);
                      }
                    },
                  }
                : {})}
            >
              <td className="stats-td stats-td-name" title={t.title}>
                {/* The title lives in its OWN ellipsis span inside a flex row:
                    when the cell was the ellipsis container, a real-length
                    title (~80 chars mean) pushed the View PR anchor past the
                    260px clip — focusable but invisible at every viewport
                    (review D1). The badge is a flex:0 0 auto sibling that can
                    never be clipped away. The flex row is an inner div, not
                    the td — see styles.css .stats-td-name-row for why. */}
                <div className="stats-td-name-row">
                <span className="stats-td-name-title">{t.title}</span>
                {t._collapsed > 0 && (
                  <span
                    className="stats-collapsed-note"
                    title="Older runs with this title — open the row to see its attempts"
                  >
                    +{t._collapsed} older
                  </span>
                )}
                {/* The way BACK to a shipped ticket's PR (operator demo finding):
                    same affordance as the board card - a real anchor for http(s),
                    a text-only badge for demo-DB local-pr:// URLs. stopPropagation
                    so the link never also opens the row's drawer; the row's
                    keydown guard already bails on descendant events, so Enter on
                    the focused link activates the link, not the row. */}
                {t.pr_url && (
                  httpPrUrl(t.pr_url) ? (
                    <a
                      className="card-pr-badge stats-pr-link"
                      href={t.pr_url}
                      target="_blank"
                      rel="noreferrer noopener"
                      title="open the pull request in a new tab"
                      onClick={(e) => e.stopPropagation()}
                    >View PR ↗</a>
                  ) : (
                    <span className="card-pr-badge stats-pr-link">PR</span>
                  )
                )}
                </div>
                {/* A budget-terminated task ends in `failed` with no question
                    attached — nobody was asked, by design. Without this line the
                    row says "failed" and nothing else, which is less than the
                    escalation it replaced told you. Copy lives in
                    cardBlockerLine.js beside the parked-card copy. */}
                {failedReasonLine(t) && (
                  <div className="stats-td-reason">{failedReasonLine(t)}</div>
                )}
              </td>
              <td className="stats-td stats-td-status">
                <span
                  className="stats-status-dot"
                  style={{ background: STATUS_DOT[t.status] || "var(--text-dim)" }}
                />
                {t.cancelled ? "cancelled" : t.status.replace(/_/g, " ")}
              </td>
              <td className="stats-td stats-td-mono">
                {t.duration != null && t.duration > 0 ? fmtDuration(t.duration) : "—"}
              </td>
              <td className="stats-td stats-td-project" title={t.repo_name || undefined}>
                {t.repo_name || "—"}
              </td>
              <td className="stats-td stats-td-mono stats-td-right">
                {fmtTokens(taskBurn(t) || null)}
              </td>
              {realDollars && (
                <td className="stats-td stats-td-mono stats-td-right">
                  {fmtCost(taskCost(t))}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
