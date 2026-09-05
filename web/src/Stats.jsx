import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fetchMetrics, fetchRepos, fetchRepoUnderstanding, searchEvents } from "./api.js";
import { fmtCost, fmtTokens, lifetimeCost, taskBurn, pricingIsReal } from "./cost.js";
import { northStarTiles } from "./northStar.js";
import TaskTable from "./TaskTable.jsx";
import { isRealFailure } from "./boardLanes.js";
import { profileRows, profileStatus } from "./repoView.js";
import { kindLabel, groupByTask } from "./searchView.js";
import { pluralize } from "./pluralize.js";
import { costByProject, totalCost, totalTokens as totalProjectTokens } from "./costGroups.js";
import { parseTimestamp } from "./parseTimestamp.js";

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtDuration(seconds) {
  if (seconds < 60) return "<1m";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.round((seconds % 86400) / 3600);
  return h > 0 ? `${d}d ${h}h` : `${d}d`;
}

function parseTS(ts) {
  // Delegates to the shared UTC-aware helper (web/src/parseTimestamp.js) —
  // a naive DB timestamp must parse as UTC, not local.
  return parseTimestamp(ts);
}

// Compute all stats from the task list
function computeStats(tasks) {
  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 86400 * 1000);
  const sevenDaysAgo = new Date(now.getTime() - 7 * 86400 * 1000);

  const doneTasks = tasks.filter(t => t.status === "done");
  // An operator-cancelled task ends in FAILED status but is not a capability
  // failure — keep it out of the failure count and the success-rate denominator.
  const cancelledTasks = tasks.filter(t => t.status === "failed" && t.cancelled);
  const failedTasks = tasks.filter(isRealFailure);

  // Tasks with valid timestamps
  const doneWithTimes = doneTasks
    .map(t => ({
      created: parseTS(t.created_at),
      finished: parseTS(t.updated_at),
      kind: t.kind || "feature",
    }))
    .filter(t => t.created && t.finished);

  // Completion durations in seconds
  const durations = doneWithTimes
    .map(t => (t.finished.getTime() - t.created.getTime()) / 1000)
    .filter(d => d > 0);

  // Average duration
  const avgDuration = durations.length > 0
    ? durations.reduce((a, b) => a + b, 0) / durations.length
    : 0;

  // Median duration
  const sortedDurations = [...durations].sort((a, b) => a - b);
  const medianDuration = sortedDurations.length > 0
    ? sortedDurations[Math.floor(sortedDurations.length / 2)]
    : 0;

  // Completed in last 30 days
  const last30 = doneWithTimes.filter(t => t.finished >= thirtyDaysAgo);

  // Completed in last 7 days
  const last7 = doneWithTimes.filter(t => t.finished >= sevenDaysAgo);

  // Average tasks per day
  let tasksPerDay = 0;
  if (doneWithTimes.length > 0) {
    const earliest = new Date(Math.min(...doneWithTimes.map(t => t.finished.getTime())));
    const spanDays = Math.max(1, (now.getTime() - earliest.getTime()) / 86400000);
    tasksPerDay = doneWithTimes.length / spanDays;
  }

  // Daily histogram for last 30 days (for sparkline)
  const dailyCounts = [];
  for (let i = 29; i >= 0; i--) {
    const dayStart = new Date(now.getTime() - (i + 1) * 86400000);
    const dayEnd = new Date(now.getTime() - i * 86400000);
    const count = doneWithTimes.filter(t => t.finished >= dayStart && t.finished < dayEnd).length;
    const label = dayStart.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    dailyCounts.push({ label, count });
  }

  // Kind breakdown
  const kindMap = {};
  for (const t of doneWithTimes) {
    kindMap[t.kind] = (kindMap[t.kind] || 0) + 1;
  }
  const kindBreakdown = Object.entries(kindMap)
    .map(([kind, count]) => ({ kind, count }))
    .sort((a, b) => b.count - a.count);

  // Success rate
  const totalTerminal = doneTasks.length + failedTasks.length;
  const successRate = totalTerminal > 0 ? (doneTasks.length / totalTerminal) * 100 : 0;

  // Token tracking across all tasks
  // Burn = fresh + cache-read tokens (the same definition as the drawer and
  // board — tokens_used alone hides 90%+ of real spend, C1).
  // Nine buckets, same as taskCost — the tile shows this count beside that price.
  const burnOf = (t) => taskBurn(t);
  const totalTokens = tasks.reduce((s, t) => s + burnOf(t), 0);
  const avgTokensPerTask = doneTasks.length > 0
    ? tasks.filter(t => burnOf(t) > 0).reduce((s, t) => s + burnOf(t), 0)
      / Math.max(1, tasks.filter(t => burnOf(t) > 0).length)
    : 0;

  // SDLC metrics — backward-compatible (handles missing fields)
  const withAttempts = doneTasks.filter(t => t.attempts?.length > 0);
  const firstAttemptRate = withAttempts.length > 0
    ? (withAttempts.filter(t => t.attempts.length === 1 && t.attempts[0].review_passed).length / withAttempts.length) * 100
    : 0;
  const avgAttempts = withAttempts.length > 0
    ? withAttempts.reduce((s, t) => s + t.attempts.length, 0) / withAttempts.length
    : 0;
  const specCoverage = tasks.length > 0
    ? (tasks.filter(t => t.context?.spec).length / tasks.length) * 100
    : 0;
  let stagnationCount = 0;
  for (const t of tasks) {
    if (t.attempts?.length >= 2) {
      const a1 = t.attempts[t.attempts.length - 2];
      const a2 = t.attempts[t.attempts.length - 1];
      const r1 = a1.review_checklist?.items;
      const r2 = a2.review_checklist?.items;
      if (r1?.length && r2?.length) {
        const p1 = r1.filter(it => it.passed).length;
        const p2 = r2.filter(it => it.passed).length;
        if (p1 === p2 && p2 < r2.length) stagnationCount++;
      }
    }
  }

  return {
    totalCompleted: doneTasks.length,
    totalFailed: failedTasks.length,
    totalCancelled: cancelledTasks.length,
    totalTasks: tasks.length,
    tasksPerDay,
    avgDuration,
    medianDuration,
    last30Count: last30.length,
    last7Count: last7.length,
    dailyCounts,
    kindBreakdown,
    successRate,
    totalTokens,
    avgTokensPerTask,
    firstAttemptRate,
    avgAttempts,
    specCoverage,
    stagnationCount,
  };
}

// ── Sparkline (pure SVG) ─────────────────────────────────────────────────────

// Daily completions. Rebuilt against the dataviz anti-patterns (UI_AUDIT M8): the old one
// stretched every bar (`preserveAspectRatio="none"`), drew a ZERO day as a 1px mark — so an
// empty month read as a broken dotted baseline rather than "nothing shipped" — and put the
// dates only in a hover <title>, i.e. the tooltip was the only way to read a value.
//
// One series, so no legend (the section title names it). Zero days are a recessive baseline
// rule, not marks. The busiest day is direct-labelled and the axis carries first/mid/last
// dates, so every value is reachable without hovering.
// The SVG must render 1:1 at its container's width. A fixed 640-wide viewBox scales
// uniformly, so at 390px the whole chart — axis dates included — shrank to ~5px type,
// illegible on exactly the viewport that has no hover tooltip either.
function useContainerWidth(fallback = 640) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    // Seed from the real box BEFORE the browser paints: starting at the 640 fallback made the
    // chart paint upscaled and then snap, a visible layout jump on every mount.
    const el = ref.current;
    if (el) {
      const w = Math.round(el.getBoundingClientRect().width);
      if (w > 0) setWidth(Math.max(240, w));
    }
  }, []);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([entry]) => {
      const w = Math.round(entry.contentRect.width);
      if (w > 0) setWidth(Math.max(240, w));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

const CHART_H = 88;
const AXIS_H = 18;
// The max hairline and a full-height bar must land on the SAME line, or the annotation lies.
const GRID_Y = 6;

function Sparkline({ data, width = 640 }) {
  if (!data || data.length === 0) return null;
  const max = Math.max(...data.map((d) => d.count), 1);
  const gap = 2;
  const barW = Math.max(1, (width - (data.length - 1) * gap) / data.length);
  const plotH = CHART_H - AXIS_H;
  const peakIdx = data.reduce((best, d, i) => (d.count > data[best].count ? i : best), 0);
  const hasAny = data.some((d) => d.count > 0);

  return (
    <svg
      className="stats-sparkline"
      width={width}
      height={CHART_H}
      viewBox={`0 0 ${width} ${CHART_H}`}
      role="img"
      aria-label={
        hasAny
          ? `Daily completions, last ${data.length} days. Busiest day ${data[peakIdx].label} with ${data[peakIdx].count}.`
          : `Daily completions, last ${data.length} days. Nothing completed.`
      }
    >
      {/* A hairline at the max, one shade off the surface — the scale, without a grid. */}
      {hasAny && (
        <>
          <line x1={0} y1={GRID_Y} x2={width} y2={GRID_Y} className="stats-spark-grid" />
          <text x={width} y={GRID_Y + 11} textAnchor="end" className="stats-spark-gridlabel">{max}</text>
        </>
      )}
      {data.map((d, i) => {
        const x = i * (barW + gap);
        // A zero day is the ABSENCE of a bar. It needs no mark: the axis rule below already
        // draws the baseline across the full width, and a 1px rect in the axis colour added no
        // pixels — only a hover-only tooltip on something invisible.
        if (d.count === 0) return null;
        const barH = Math.max(2, (d.count / max) * (plotH - GRID_Y));
        return (
          <rect
            key={i}
            x={x}
            y={plotH - barH}
            width={barW}
            height={barH}
            rx={2}
            className="stats-sparkline-bar has-data"
          >
            <title>{`${d.label}: ${d.count} task${d.count !== 1 ? "s" : ""}`}</title>
          </rect>
        );
      })}
      <line x1={0} y1={plotH} x2={width} y2={plotH} className="stats-spark-axis" />
      {/* Dates on the axis: the chart said "last 30 days" but never said WHICH days. */}
      <text x={0} y={CHART_H - 4} className="stats-spark-tick">{data[0].label}</text>
      <text x={width / 2} y={CHART_H - 4} textAnchor="middle" className="stats-spark-tick">
        {data[Math.floor(data.length / 2)].label}
      </text>
      <text x={width} y={CHART_H - 4} textAnchor="end" className="stats-spark-tick">
        {data[data.length - 1].label}
      </text>
    </svg>
  );
}

// ── Kind breakdown bar ───────────────────────────────────────────────────────

const KIND_COLORS = {
  feature: "var(--accent-500)",
  bugfix: "var(--red)",
  ci_fix: "var(--amber)",
  test_gap: "var(--purple)",
  investigation: "var(--cyan)",
  code_review: "var(--green)",
};

// Per-task cost is a table column and lifetime cost is a tile; nothing sat between them, so
// "what has this repo cost me" meant adding the column up by hand. Same cost model as both.
//
// Subscription mode (realDollars = false) never renders a dollar figure here — the same rule
// as everywhere else on this page. The testid, section shape and `cost-project-*` CSS classes
// stay identical either way; only the title, sub-line, last column and bar basis switch to
// tokens (deadCss.test.mjs pins the classes; no new class names are introduced).
function CostByProject({ tasks, realDollars }) {
  const rows = useMemo(() => costByProject(tasks), [tasks]);
  if (rows.length === 0) return null;
  const total = totalCost(rows);
  const tokens = totalProjectTokens(rows);
  const max = Math.max(...rows.map((r) => (realDollars ? r.cost : r.tokens)), 0);

  return (
    <div className="stats-section ph-no-capture" data-testid="cost-by-project">
      <h3 className="stats-section-title">
        {realDollars ? "Cost by Project" : "Token Usage by Project"}
      </h3>
      <div className="stats-section-sub">
        {realDollars
          ? `${fmtCost(total)} across ${rows.length} ${pluralize(rows.length, "project")}`
          : `${fmtTokens(tokens)} across ${rows.length} ${pluralize(rows.length, "project")}`}
      </div>
      <div className="cost-project-wrap">
        <table className="cost-project-table">
        <thead>
          <tr>
            <th className="cost-project-th" scope="col">Project</th>
            <th className="cost-project-th cost-project-th-right" scope="col">Tasks</th>
            <th className="cost-project-th cost-project-th-right" scope="col">
              {realDollars ? "Est. Cost" : "Tokens"}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.project} className="cost-project-row">
              <th className="cost-project-name" scope="row">
                {/* The bar is decoration on top of the number, not the only way to read it. */}
                <span
                  className="cost-project-bar"
                  style={{
                    width: max > 0 ? `${((realDollars ? r.cost : r.tokens) / max) * 100}%` : 0,
                  }}
                  aria-hidden="true"
                />
                <span className="cost-project-label">{r.project}</span>
              </th>
              <td className="cost-project-val">{r.tasks}</td>
              <td className="cost-project-val cost-project-cost">
                {realDollars ? fmtCost(r.cost) : fmtTokens(r.tokens)}
              </td>
            </tr>
          ))}
        </tbody>
        </table>
      </div>
    </div>
  );
}

function KindBar({ breakdown, total }) {
  if (breakdown.length === 0) return null;
  return (
    <div className="stats-kind-bar-wrap">
      <div className="stats-kind-bar">
        {breakdown.map(({ kind, count }) => (
          <div
            key={kind}
            className="stats-kind-segment"
            style={{
              width: `${(count / total) * 100}%`,
              background: KIND_COLORS[kind] || "var(--text-dim)",
            }}
            title={`${kind}: ${count}`}
          />
        ))}
      </div>
      <div className="stats-kind-legend">
        {breakdown.map(({ kind, count }) => (
          <div key={kind} className="stats-kind-legend-item">
            <span
              className="stats-kind-dot"
              style={{ background: KIND_COLORS[kind] || "var(--text-dim)" }}
            />
            <span className="stats-kind-label">{kind}</span>
            <span className="stats-kind-count">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Token cost estimate ──────────────────────────────────────────────────────

// fmtTokens/estimateCost come from cost.js — the ONE home (a local copy here
// silently shadowed the blended cache-read pricing and kept est. fresh-only).

// ── Task table ───────────────────────────────────────────────────────────────


// ── Main Stats Component ─────────────────────────────────────────────────────

// C3-G3: what no_human understands about a repo — its onboarded profile, the
// cached repo map, and matched playbooks. Read-only; NO indexing, NO RAG. Lets
// the operator see what the coder sees, without opening a task.
function RepoUnderstanding() {
  const [repos, setRepos] = useState([]);
  const [selected, setSelected] = useState("");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { fetchRepos().then(setRepos); }, []);
  useEffect(() => {
    if (!selected) { setData(null); setLoading(false); return; }
    let ignore = false;   // drop a stale response when the repo is switched fast
    setLoading(true);
    fetchRepoUnderstanding(selected).then((d) => {
      if (ignore) return;
      setData(d);
      setLoading(false);
    });
    return () => { ignore = true; };
  }, [selected]);

  if (repos.length === 0) return null;   // nothing onboarded yet — no empty widget

  return (
    <div className="stats-section ph-no-capture" data-testid="repo-understanding">
      <h3 className="stats-section-title">Repository Understanding</h3>
      <div className="stats-section-sub">
        What no_human knows about a repo — profile, structure map, and playbooks
      </div>
      {/* Chromium's AX tree reported role=combobox name="" — no accessible name at all,
          so it announced as a bare "combo box". The <h3> above is not a label. */}
      <select
        className="repo-understanding-select"
        data-testid="repo-understanding-select"
        aria-label="Repository"
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
      >
        <option value="">Select a repo&hellip;</option>
        {repos.map((r) => (
          <option key={r.repo_path} value={r.repo_path}>{r.name}</option>
        ))}
      </select>

      {loading && <div className="stats-section-sub">Loading&hellip;</div>}

      {data && (
        <div className="repo-understanding-body">
          <div className="repo-understanding-status">{profileStatus(data.profile)}</div>

          {profileRows(data.profile).length > 0 && (
            <table className="repo-understanding-profile">
              <tbody>
                {profileRows(data.profile).map((row) => (
                  <tr key={row.label}>
                    <td className="repo-understanding-key">{row.label}</td>
                    <td className="repo-understanding-val">{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {data.playbooks.length > 0 && (
            <div className="repo-understanding-playbooks">
              <div className="repo-understanding-subhead">
                Playbooks ({data.playbooks.length})
              </div>
              <ul>
                {data.playbooks.map((p, i) => (
                  <li key={i}>{p.title}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="repo-understanding-subhead">Structure map</div>
          {data.repo_map
            ? <pre className="repo-understanding-map">{data.repo_map}</pre>
            : <div className="stats-section-sub">
                No cached map (the repo path may be unavailable on this host).
              </div>}
        </div>
      )}
    </div>
  );
}

// C3-G4: cross-task search over the failure/fix record (events_fts). Debounced;
// results grouped by task. Answers "how was a failure like this handled before?"
function SessionSearch() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const [searched, setSearched] = useState(false);

  useEffect(() => {
    const term = q.trim();
    if (!term) { setResults([]); setSearched(false); return; }
    let ignore = false;
    const h = setTimeout(() => {
      searchEvents(term).then((r) => {
        if (ignore) return;
        setResults(r);
        setSearched(true);
      });
    }, 250);   // debounce so each keystroke isn't a request
    return () => { ignore = true; clearTimeout(h); };
  }, [q]);

  const groups = groupByTask(results);

  return (
    <div className="stats-section ph-no-capture" data-testid="session-search">
      <h3 className="stats-section-title">Search history</h3>
      <div className="stats-section-sub">
        Full-text search across every task's failures, reviews, and blockers
      </div>
      {/* The placeholder was doing the labelling — Chromium computed the name as
          nameFrom=["placeholder"]. The name itself persists once you type (the attribute
          stays), but a placeholder is a hint, not a label: it vanishes VISUALLY the
          moment anything is typed, so the field's purpose is only discoverable while it
          is empty, and placeholder-as-name is inconsistently supported across AT. */}
      <input
        type="search"
        className="session-search-input"
        data-testid="session-search-input"
        aria-label="Search history"
        placeholder="e.g. ImportError, timeout, tamper…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      {/* Results swap in silently after the 250ms debounce — sighted users see the list
          appear, screen readers got nothing. Rendered unconditionally so the live region
          exists before its content changes, which is what makes it announce. */}
      <div className="sr-only" role="status">
        {!searched
          ? ""
          : groups.length === 0
            ? "No matches."
            : `${groups.length} ${pluralize(groups.length, "task")} matched.`}
      </div>
      {/* aria-hidden: the live region above already carries this sentence, and without it
          "No matches." appears twice in a screen reader's reading order. */}
      {searched && groups.length === 0 && (
        <div className="stats-section-sub" aria-hidden="true">No matches.</div>
      )}
      {groups.length > 0 && (
        <ul className="session-search-results">
          {groups.map((g) => (
            <li key={g.task_id} className="session-search-group">
              <div className="session-search-task">
                <span className="session-search-taskid">{g.task_id.slice(0, 8)}</span>
                {g.task_title}
              </div>
              {g.hits.map((h, i) => (
                <div key={i} className="session-search-hit">
                  <span className="session-search-kind">{kindLabel(h.kind)}</span>
                  <span className="session-search-snippet">{h.snippet}</span>
                </div>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Stats({ tasks, authMode }) {
  const stats = useMemo(() => computeStats(tasks), [tasks]);
  // api_key (BYO) is the only mode that pays Anthropic per token for real; every
  // other/absent value falls to token-led display (pricingIsReal, SCRUM re-home).
  const realDollars = pricingIsReal(authMode);
  // undefined = fetch in flight (loader) · null = fetch FAILED (honest
  // 'unavailable') · object = loaded. A failed refetch keeps the loaded value
  // — a dollar figure must never be replaced by a fake loader (PR #108
  // review, medium: a 500 rendered a permanent lying 'est. loading…').
  const [metrics, setMetrics] = useState(undefined);
  const [chartRef, chartWidth] = useContainerWidth();
  useEffect(() => {
    fetchMetrics().then((m) =>
      setMetrics((prev) => (m === null && prev ? prev : m)));
  }, [tasks.length]);
  const northStar = northStarTiles(metrics, authMode);

  if (tasks.length === 0) {
    return (
      <div className="stats-page">
        <div className="stats-empty">
          <div className="stats-empty-icon">&#9776;</div>
          <div className="stats-empty-title">No tasks yet</div>
          <div className="stats-empty-desc">
            Create your first task to start seeing stats here.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="stats-page">
      <div className="stats-header">
        <h2 className="stats-title">Performance</h2>
        <div className="stats-subtitle">
          {stats.totalTasks} total tasks &middot; {stats.totalCompleted} completed
        </div>
      </div>

      {/* North-star row (D1): the real delivery numbers from /api/metrics —
          "completed" above counts 0-token code reviews, these do not. */}
      {northStar.length > 0 && (
        <div className="stats-northstar">
          {northStar.map((t) => (
            <div key={t.label} className={`ns-tile ns-${t.tone}`}>
              <div className="ns-label">{t.label}</div>
              <div className="ns-value">{t.value}</div>
              <div className="ns-sub">{t.sub}</div>
            </div>
          ))}
        </div>
      )}

      {/* Primary metric cards */}
      <div className="stats-cards">
        <div className="stats-card stats-card-accent">
          <div className="stats-card-label">Avg. Tasks / Day</div>
          <div className="stats-card-value">
            {stats.totalCompleted > 0 ? stats.tasksPerDay.toFixed(1) : "\u2014"}
          </div>
          <div className="stats-card-sub">
            {stats.last7Count} in the last 7 days
          </div>
        </div>

        <div className="stats-card">
          <div className="stats-card-label">Avg. Completion Time</div>
          <div className="stats-card-value">
            {stats.avgDuration > 0 ? fmtDuration(stats.avgDuration) : "\u2014"}
          </div>
          <div className="stats-card-sub">
            median: {stats.medianDuration > 0 ? fmtDuration(stats.medianDuration) : "\u2014"}
          </div>
        </div>

        <div className="stats-card">
          <div className="stats-card-label">Last 30 Days</div>
          <div className="stats-card-value">{stats.last30Count}</div>
          <div className="stats-card-sub">
            tasks completed
          </div>
        </div>

        <div className={`stats-card${
          stats.totalCompleted + stats.totalFailed > 0
            ? (stats.successRate < 50 ? " stats-card-bad"
               : stats.successRate >= 80 ? " stats-card-good" : "")
            : ""}`}>
          <div className="stats-card-label">Success Rate</div>
          <div className="stats-card-value">
            {stats.totalCompleted + stats.totalFailed > 0
              ? `${stats.successRate.toFixed(0)}%`
              : "\u2014"}
          </div>
          <div className="stats-card-sub">
            {stats.totalCompleted} done &middot; {stats.totalFailed} failed
            {stats.totalCancelled > 0 && <> &middot; {stats.totalCancelled} cancelled</>}
          </div>
        </div>

        <div className="stats-card">
          <div className="stats-card-label">Token Usage</div>
          <div className="stats-card-value">{fmtTokens(stats.totalTokens)}</div>
          <div className="stats-card-sub">
            {/* Subscription mode pays a flat fee: an "est. $X" here would be an API-rate
                estimate, not money that changed hands, so it is dropped entirely rather
                than shown alongside the token count (pricingIsReal, SCRUM re-home). */}
            {realDollars &&
              (metrics === undefined
                ? <span className="stats-loading">est. loading…</span>
                : metrics === null
                  ? <>est. unavailable</>
                  : <>est. {fmtCost(lifetimeCost(metrics))}</>)}
            {stats.avgTokensPerTask > 0 &&
              `${realDollars ? " · " : ""}avg ${fmtTokens(stats.avgTokensPerTask)}/task`}
          </div>
        </div>

        {/* First-attempt-pass and spec-coverage only when they have data \u2014
            an empty "\u2014" tile is clutter, not information (D3 declutter). */}
        {stats.avgAttempts > 0 && (
          <div className="stats-card" data-testid="stat-first-attempt">
            <div className="stats-card-label">First-Attempt Pass</div>
            <div className="stats-card-value">{stats.firstAttemptRate.toFixed(0)}%</div>
            <div className="stats-card-sub">
              avg {stats.avgAttempts.toFixed(1)} attempts/task
            </div>
          </div>
        )}

        {stats.specCoverage > 0 && (
          <div className="stats-card" data-testid="stat-spec-coverage">
            <div className="stats-card-label">Spec Coverage</div>
            <div className="stats-card-value">{stats.specCoverage.toFixed(0)}%</div>
            <div className="stats-card-sub">
              {tasks.filter(t => t.context?.spec).length} of {tasks.length} tasks
            </div>
          </div>
        )}

        <div className="stats-card" data-testid="stat-stagnation">
          <div className="stats-card-label">Stagnation</div>
          <div className="stats-card-value">{stats.stagnationCount}</div>
          <div className="stats-card-sub">
            tasks with unchanged pass rate
          </div>
        </div>
      </div>

      {/* Daily completions chart */}
      <div className="stats-section">
        <h3 className="stats-section-title">Daily Completions</h3>
        <div className="stats-section-sub">Last 30 days</div>
        <div className="stats-chart-wrap" ref={chartRef}>
          <Sparkline data={stats.dailyCounts} width={chartWidth} />
        </div>
      </div>

      {/* Kind breakdown */}
      {stats.kindBreakdown.length > 0 && (
        <div className="stats-section">
          <h3 className="stats-section-title">By Type</h3>
          <KindBar breakdown={stats.kindBreakdown} total={stats.totalCompleted} />
        </div>
      )}

      {/* Cost by project — the rollup between the per-task column and the lifetime tile. */}
      <CostByProject tasks={tasks} realDollars={realDollars} />

      {/* Repository Understanding (C3-G3) */}
      <RepoUnderstanding />

      {/* Cross-task session search (C3-G4) */}
      <SessionSearch />

      {/* Task table */}
      <div className="stats-section">
        <h3 className="stats-section-title">All Tasks</h3>
        <div className="stats-section-sub">{tasks.length} tasks</div>
        <TaskTable tasks={tasks} authMode={authMode} />
      </div>
    </div>
  );
}
