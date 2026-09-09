// PostHog triage 2026-09-09: of the real-user dead clicks over the prior 3
// days (three installs on 0.2.1/0.2.2, Windows and Mac), 26 of ~40 were on
// `<select class="new-task-select">` in Settings → Models (Coder/Reviewer/
// Planner/Supervisor and the backend override), 4 more on `<input>`. Opening
// a native dropdown or focusing a field mutates nothing in the DOM, scrolls
// nothing and changes no selection, so posthog-js's dead-click heuristic
// flags every one of them.
//
// `DEAD_CLICK_IGNORE_SELECTORS` is handed to posthog-js's own
// `capture_dead_clicks.css_selector_ignorelist`, which posthog matches
// against the click target and every one of its ancestors — this removes
// native form controls AT THE SOURCE: posthog never even queues them as dead-
// click candidates. `isDeadClickIgnored` is the same rule expressed as a
// pure, unit-testable predicate — the executable spec for the list, not
// something posthog runs itself. It deliberately diverges from the flat CSS
// list on one point: posthog's selector match has no early exit, so a
// `<button>` nested inside an associated `<label for=...>` is matched by
// `label[for]` and ignored by posthog; this predicate gives the `<button>`
// itself priority and reports it. That is posthog's own behaviour to know
// about, not a gap this file closes — the app has no such markup today.
//
// posthog-js's DEFAULT ignorelist is `[".ph-no-capture", ".ph-no-deadclick"]`.
// Handing it a custom list REPLACES the default rather than extending it, so
// both are re-listed here — dropping them would start reporting dead clicks
// inside every ph-no-capture block (Board cards, TaskTable body, SlideOver).
//
// The ignorelist above only ever covered NATIVE CONTROLS. The button dead
// clicks it deliberately leaves alone (a Continue button, the theme toggle, a
// tab) turned out to be a SEPARATE problem: detector artefacts of an ordering
// race in posthog-js 1.417.1 itself, measured 2026-09-09 in real Chromium
// with session replay on: 9 of 9 clicks on React buttons that visibly
// re-render were reported `$dead_click` (1 of 9 with replay off), and on the
// real project (event 560622) 38 of the last 40 `$dead_click` events carried
// `$dead_click_last_mutation_timestamp − $dead_click_event_timestamp` in
// `[-12, 0] ms` with no `$dead_click_mutation_delay_ms` at all — versus
// genuinely dead controls in the harness at -3.6 s / -10.8 s / -14.4 s or no
// mutation stamp. The module's own source (dist/dead-clicks-autocapture.js)
// explains the mechanism: `_lastMutation` is stamped when the
// MutationObserver CALLBACK runs — the microtask right after React flushes
// the click's own update — while `click.timestamp` is stamped later, in a
// window bubble-phase click listener, and `mutationDelayMs` is only computed
// when `click.timestamp <= _lastMutation`. rrweb's per-click recording puts
// 1-12 ms between the two stamps, so on a genuinely synchronous React
// re-render the click's own mutation is already in the past by the time
// `click.timestamp` is stamped, the delay is never computed, and the
// candidate times out dead at the absolute timeout
// (`mutation_threshold_ms * 1.1` = 2750 ms).
//
// Falsy-zero read (established, not assumed): the only falsy-zero guard in
// that path is `this._lastMutation && click.timestamp <= this._lastMutation`
// — it only discards a literal epoch-0 `_lastMutation`, unreachable here. The
// *alive* check itself, `isNumber(mutationDelayMs) && mutationDelayMs <
// mutation_threshold_ms`, treats a computed 0 ms delay as alive correctly, so
// that path does not explain the 15-of-38 measured events sitting at exactly
// 0 ms. `deadClickBeforeSend` below (in `[0, 100]`, inclusive of 0 for that
// reason) reports this as a harness observation, not a proven mechanism for
// the exact-0 case.
//
// Fix: `deadClickBeforeSend`, wired as posthog-js's own `before_send` init
// option in telemetry.js, drops a `$dead_click` client-side when it has no
// `$dead_click_mutation_delay_ms` and its own event-minus-last-mutation gap
// falls in that race window — genuine timeouts (a large gap, or a present
// mutation delay) are untouched. This does NOT cover `capture_heatmaps`'s own
// dead-click layer — see telemetry.js for that disclosure.
export const DEAD_CLICK_IGNORE_SELECTORS = [
  ".ph-no-capture",
  ".ph-no-deadclick",
  // Native form controls: a click that opens a dropdown, focuses a field or
  // picks an <option> produces no mutation/scroll/selection change and is
  // not a dead control. Covers every `<input>` type (checkbox, radio, text,
  // email, ...) as one family — they are all native controls with inherent
  // interactivity regardless of type.
  "select",
  "input",
  "textarea",
  "option",
  // A <label> only counts when it actually labels something (intake Q&A):
  // an unassociated label is text content, not a control, and a click on it
  // may legitimately be a real dead click.
  "label[for]",
  "label:has(input, select, textarea)",
];

const FORM_CONTROL_TAGS = new Set(["SELECT", "INPUT", "TEXTAREA", "OPTION"]);

// Intake Q&A: traverse ancestors to the root but cap iteration to protect
// against pathologically deep or malformed (e.g. self-referential) DOM trees.
const MAX_ANCESTORS = 20;

function tagOf(el) {
  return String(el?.tagName || "").toUpperCase();
}

function roleOf(el) {
  if (typeof el?.getAttribute === "function") {
    const r = el.getAttribute("role");
    if (r) return r;
  }
  return el?.role || "";
}

function labelIsAssociated(el) {
  // Per the intake Q&A: a label counts as associated "either via for
  // attribute or by wrapping/containing a form control element" — the mere
  // PRESENCE of a `for` attribute is enough, even if it is dangling (points
  // at an id that does not exist). `control` (the DOM's resolved answer) is
  // only consulted when there is no `for` attribute at all.
  if (el.htmlFor || (typeof el.getAttribute === "function" && el.getAttribute("for"))) {
    return true;
  }
  if ("control" in el) return Boolean(el.control);
  return Boolean(
    typeof el.querySelector === "function" && el.querySelector("input, select, textarea"),
  );
}

/**
 * Pure predicate mirroring DEAD_CLICK_IGNORE_SELECTORS: should a dead click
 * on `node` (or its nearest interactive ancestor) be ignored?
 *
 * Fails open (returns false → click is still captured as dead) on anything
 * malformed, so a hostile/unexpected node can never throw into the caller.
 */
export function isDeadClickIgnored(node) {
  try {
    let el = node;
    let hops = 0;
    while (el && typeof el === "object" && hops < MAX_ANCESTORS) {
      const tag = tagOf(el);
      if (tag && FORM_CONTROL_TAGS.has(tag)) return true;
      if (tag === "LABEL") {
        if (labelIsAssociated(el)) return true;
        // An unassociated label does not short-circuit; it may itself sit
        // inside a form control's subtree (kept walking below).
      } else if (tag === "BUTTON" || tag === "A" || roleOf(el) === "button") {
        // Divergence from posthog's own behaviour: posthog's flat
        // css_selector_ignorelist match has no early exit, so its own scan
        // would match `label[for]` on an ancestor and ignore a <button>
        // nested inside an associated <label> too. This predicate instead
        // gives the nearest interactive element (the <button> itself)
        // priority and reports it — the app has no such markup today, so
        // this only ever documents where the two disagree, never removes a
        // real button from production traffic.
        return false;
      }
      const parent = el.parentElement;
      if (!parent || parent === el) break;
      el = parent;
      hops += 1;
    }
    return false;
  } catch {
    return false;
  }
}

// The measured race window (see the header comment above): a $dead_click
// with no computed mutation delay and an event-minus-last-mutation gap in
// [0, DEAD_CLICK_RACE_WINDOW_MS] is the ordering artefact, not a real dead
// control. Inclusive of 0 — 15 of the 38 measured artefacts sit exactly
// there and the source read does not explain why; the harness observes it.
export const DEAD_CLICK_RACE_WINDOW_MS = 100;

/**
 * Pure predicate: is this captured $dead_click event the posthog-js
 * mutation-vs-click stamp ordering race (see the header), rather than a
 * genuinely dead control?
 *
 * Fails open (returns false → the event is kept) on anything malformed, so a
 * hostile/unexpected event can never throw into the caller.
 */
export function isDeadClickRaceArtifact(event) {
  try {
    if (!event || event.event !== "$dead_click") return false;
    const props = event.properties ?? {};
    if (props.$dead_click_mutation_delay_ms != null) return false;
    const eventTs = props.$dead_click_event_timestamp;
    const mutationTs = props.$dead_click_last_mutation_timestamp;
    if (!Number.isFinite(eventTs) || !Number.isFinite(mutationTs)) return false;
    const gap = eventTs - mutationTs;
    return gap >= 0 && gap <= DEAD_CLICK_RACE_WINDOW_MS;
  } catch {
    return false;
  }
}

/**
 * posthog-js `before_send` option (see telemetry.js): drops the ordering-race
 * artefact, returns every other event unchanged — same object reference,
 * including a non-$dead_click event, `null` or `undefined` (a `before_send`
 * chain may already be passing those through).
 */
export function deadClickBeforeSend(event) {
  return isDeadClickRaceArtifact(event) ? null : event;
}

export default isDeadClickIgnored;
