// PostHog triage 2026-09-09: of the real-user dead clicks over the prior 3
// days (three installs on 0.2.1/0.2.2, Windows and Mac), 26 of ~40 were on
// `<select class="new-task-select">` in Settings → Models (Coder/Reviewer/
// Planner/Supervisor and the backend override), 4 more on `<input>`. Opening
// a native dropdown or focusing a field mutates nothing in the DOM, scrolls
// nothing and changes no selection, so posthog-js's dead-click heuristic
// flags every one of them — burying the real dead controls: a Continue
// button in onboarding's Repositories step, the theme toggle, the Settings
// "Updates" tab, the "How to find this" tooltip button, a close button, a
// status label.
//
// `DEAD_CLICK_IGNORE_SELECTORS` is handed to posthog-js's own
// `capture_dead_clicks.css_selector_ignorelist`, which posthog matches
// against the click target and every one of its ancestors. `isDeadClickIgnored`
// is the same rule expressed as a pure, unit-testable predicate — the
// executable spec for the list, not something posthog runs itself.
//
// posthog-js's DEFAULT ignorelist is `[".ph-no-capture", ".ph-no-deadclick"]`.
// Handing it a custom list REPLACES the default rather than extending it, so
// both are re-listed here — dropping them would start reporting dead clicks
// inside every ph-no-capture block (Board cards, TaskTable body, SlideOver).
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
        // Deliberate divergence from the flat CSS list (which has no early
        // exit): a <button> nested inside an associated <label> is ignored
        // by posthog's selector match but kept here — the app has no such
        // markup, and this only ever loses noise, never a real button.
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

export default isDeadClickIgnored;
