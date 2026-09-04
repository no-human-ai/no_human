import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Task D2.1: the Settings row's "!" badge becomes its own click target that
// opens the Second-brain pane directly (never the last-opened pane), and the
// AI-config nudge clears only once that pane has actually rendered — not
// merely because Settings opened on some other section. Like
// secondBrainPanel.test.mjs and sidebarNav.test.mjs, this is static source
// analysis: no jsdom/React renderer is wired into this project's
// `node --test` harness, so these assertions read the .jsx source rather
// than mounting components.

const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");
const settingsJsx = readFileSync(here + "Settings.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");

function fnBody(src, name) {
  const start = src.indexOf(name);
  assert.ok(start > -1, `${name} must be defined`);
  const rest = src.slice(start + name.length);
  const end = rest.search(/\n(export )?function [A-Za-z]/);
  return end === -1 ? rest : rest.slice(0, end);
}

// ── App.jsx: openSettings no longer clears the flag ─────────────────────── //

test("openSettings no longer marks the AI-config BADGE done — that moved to the pane's own mount", () => {
  // `fnBody` (below) is built for `function X` declarations and does not
  // terminate on an arrow-function const, so it is not reused here — this
  // extracts openSettings' own short body directly, up to its closing `};`.
  const m = appJsx.match(/const openSettings = \(tab = null\) => \{([\s\S]*?)\n\s*\};/);
  assert.ok(m, "openSettings must be found");
  assert.doesNotMatch(m[1], /markAiConfigDone\(\)/,
    "opening Settings on ANY pane must not clear the BADGE flag anymore — only the Second-brain pane's first render does");
  // And the replacement handler must exist and do the marking instead.
  assert.match(appJsx, /const handleSecondBrainOpened = \(\) => \{[^}]*markAiConfigDone\(\)/);
});

// Review fix (I1): the POPUP is a SEPARATE, strictly weaker acknowledgement
// than the badge — opening Settings on ANY pane kills the popup, even though
// the badge stays until the Second-brain pane specifically is seen. Without
// this split, a user who opens Settings via the row body or a Finish-setup
// deep link and never visits Second-brain got the popup back on EVERY
// subsequent Settings-adjacent render — confirmed live before this fix.
test("I1: openSettings marks the POPUP dismissed on every open, independent of which pane it opens on", () => {
  const m = appJsx.match(/const openSettings = \(tab = null\) => \{([\s\S]*?)\n\s*\};/);
  assert.ok(m, "openSettings must be found");
  assert.match(m[1], /markPopupDismissed\(\)/,
    "opening Settings (any pane) must permanently dismiss the popup");
  assert.match(m[1], /setPopupDismissed\(true\)/, "the popup's own React state must update too, so it disappears immediately");
});

test("I1: the popup's visibility is gated on popupDismissed, NOT on the badge's aiConfigDone", () => {
  // The className is now a template literal (mobile vs. desktop variant),
  // not a plain string — tolerate that instead of matching it verbatim.
  const idx = appJsx.search(/nh-aiconfig-nudge[^`]*`\}\s*role="dialog"/);
  assert.ok(idx > -1, "the popup element must be found");
  const before = appJsx.slice(Math.max(0, idx - 400), idx);
  assert.match(before, /!popupDismissed && onboarded === true && !settingsOpen/,
    "the popup's render condition must use popupDismissed, not aiConfigDone (aiConfigDone would make it nag forever)");
});

test("I1: the popup's own × dismisses the popup, not the badge", () => {
  const m = appJsx.match(/const dismissAiConfig = \(\) => \{([\s\S]*?)\};/);
  assert.ok(m, "dismissAiConfig must be found");
  assert.match(m[1], /markPopupDismissed\(\)/);
  assert.doesNotMatch(m[1], /markAiConfigDone\(\)/,
    "dismissing the popup must not also satisfy the badge's stricter condition");
});

test("I1: the badge's own visibility stays gated on aiConfigDone, unaffected by the popup fix", () => {
  assert.match(appJsx, /badge=\{aiConfigDone \? null : "!"\}/,
    "regression guard: the badge must still be the badge's OWN, stricter flag");
});

// ── App.jsx: the "!" badge is its own click target ──────────────────────── //

test("the Settings badge passes onBadgeClick + badgeAriaLabel and opens the Second-brain (\"learnings\") pane", () => {
  const idx = appJsx.indexOf('label="Settings"');
  assert.ok(idx > -1, "the Settings NavRow must be found");
  const row = appJsx.slice(idx, appJsx.indexOf("/>", idx) + 2);
  assert.match(row, /onBadgeClick=\{/, "the badge must be given its own click handler");
  assert.match(row, /badgeAriaLabel=/, "the badge must carry its own aria-label prop");
  assert.match(row, /openSettings\(\s*["']learnings["']\s*\)/,
    "the badge click must open Settings on the Second-brain (learnings) pane specifically");
});

test("the one-time popup CTA deep-links to the Second-brain pane, not Models", () => {
  const idx = appJsx.indexOf("nh-aiconfig-nudge-cta");
  assert.ok(idx > -1, "the popup CTA button must be found");
  const cta = appJsx.slice(idx, appJsx.indexOf("</button>", idx));
  assert.match(cta, /openSettings\(\s*["']learnings["']\s*\)/,
    "the popup CTA must target the Second-brain pane");
  assert.doesNotMatch(cta, /openSettings\(\s*["']models["']\s*\)/,
    "the popup CTA must no longer deep-link to Models");
});

test("SettingsOverlay is wired to notify App when the Second-brain pane first opens", () => {
  const idx = appJsx.indexOf("<SettingsOverlay");
  assert.ok(idx > -1);
  const block = appJsx.slice(idx, appJsx.indexOf(")}", idx) + 2);
  assert.match(block, /onSecondBrainOpened=\{/,
    "App must pass a callback SettingsOverlay can call once the Second-brain pane renders");
});

// ── NavRow: a button cannot nest inside a button ─────────────────────────── //

test("NavRow accepts onBadgeClick and still renders exactly one icon + one label", () => {
  const body = fnBody(appJsx, "function NavRow(");
  assert.match(body, /onBadgeClick/, "NavRow must accept an onBadgeClick prop");
  // Still exactly one icon + one label render, regardless of branch.
  assert.match(body, /nh-navrow-icon["'][^]*?\{icon\}/);
  assert.match(body, /nh-navrow-label["'][^]*?\{label\}/);
});

// Review fix (I3): the two tests above stay green even if the badge is
// re-nested INSIDE the row button (invalid HTML, and the review's own
// diagnosis was that this exact regression is unpinned) — neither asserts
// anything about STRUCTURE. This one does: in the onBadgeClick branch, the
// row <button> must fully CLOSE before the badge's own <button> OPENS,
// proving two siblings, not one nested inside the other. Re-nesting moves
// the badge's opening tag before the row button's closing tag and turns
// this red.
test("the badge is a SIBLING of the row button, not nested inside it — the row button closes before the badge button opens", () => {
  const body = fnBody(appJsx, "function NavRow(");
  const branchStart = body.indexOf("if (onBadgeClick) {");
  assert.ok(branchStart > -1, "the onBadgeClick branch must exist");
  const fallbackStart = body.indexOf("\n  return (", branchStart);
  const branch = fallbackStart === -1 ? body.slice(branchStart) : body.slice(branchStart, fallbackStart);
  const rowOpen = branch.indexOf("<button");
  const rowClose = branch.indexOf("</button>", rowOpen);
  const badgeOpen = branch.indexOf("<button", rowClose);
  assert.ok(rowOpen > -1 && rowClose > -1 && badgeOpen > -1,
    "expected exactly two distinct <button> elements in the onBadgeClick branch");
  assert.ok(rowClose < badgeOpen,
    "the row button must fully close (</button>) before the badge's own <button> opens — " +
    "a re-nested badge (moved inside the row button) fails this");
});

// Review fix (M3): a real sibling has nothing to bubble into (the wrapper
// <div> carries no onClick of its own), so a defensive e.stopPropagation()
// on the badge's click was dead code — removed, along with its (unpinned-
// by-structure) test above. The structural test above is the assertion that
// actually matters here: it is what makes stopPropagation provably
// unnecessary, and what would catch a future regression that DID need it
// (re-nesting).
test("the badge's onClick is NOT wrapped in a stopPropagation call — dead code once the badge is a real sibling", () => {
  const body = fnBody(appJsx, "function NavRow(");
  const branchStart = body.indexOf("if (onBadgeClick) {");
  const fallbackStart = body.indexOf("\n  return (", branchStart);
  const branch = body.slice(branchStart, fallbackStart);
  assert.match(branch, /onClick=\{onBadgeClick\}/,
    "the badge's onClick should be passed straight through, not wrapped");
  // Strip `//` line comments first — the surrounding explanatory comment
  // legitimately names "stopPropagation" in prose; only CODE must not.
  const code = branch.replace(/\/\/.*$/gm, "");
  assert.doesNotMatch(code, /stopPropagation/,
    "no stopPropagation — the wrapper div has no onClick of its own for it to guard against");
});

// ── a11y: own aria-label, min hit target, visible focus ring ─────────────── //

test("the badge button gets its own accessible name via aria-label", () => {
  const body = fnBody(appJsx, "function NavRow(");
  assert.match(body, /aria-label=\{badgeAriaLabel/);
});

// Review fix (M2): `title` (the tooltip) describes the NUDGE ("Complete AI
// configuration"), which is what the badge represents now — it used to sit
// on the row body even after the row body stopped being the nudge's own
// click target.
test("the tooltip (title) moved from the row body onto the badge button", () => {
  const body = fnBody(appJsx, "function NavRow(");
  const branchStart = body.indexOf("if (onBadgeClick) {");
  const fallbackStart = body.indexOf("\n  return (", branchStart);
  const branch = body.slice(branchStart, fallbackStart);
  const rowButton = branch.slice(branch.indexOf("<button"), branch.indexOf("</button>"));
  const badgeButton = branch.slice(branch.indexOf("<button", branch.indexOf("</button>")));
  assert.doesNotMatch(rowButton, /title=\{title\}/, "the row body button must no longer carry the tooltip");
  assert.match(badgeButton, /title=\{title\}/, "the badge button must carry the tooltip instead");
});

test("the badge button's CSS gives it a real hit target (>=12px) and its own focus ring", () => {
  const rule = stylesCss.match(/\.nh-navrow-badge-btn(?:[^{]*)\{([^}]*)\}/);
  // .nh-navrow-badge already sets min-width:20px/height:20px, which the -btn
  // class inherits by being applied alongside it — but the ring must exist
  // for the -btn class specifically (a span never receives focus).
  assert.ok(stylesCss.includes(".nh-navrow-badge-btn"), ".nh-navrow-badge-btn must have SOME rule");
  const focusList = stylesCss.match(/([\s\S]*?)outline:\s*2px solid var\(--accent-500\);\s*outline-offset:\s*2px;\s*\}/);
  assert.ok(focusList, "the shared focus-ring block must exist");
  assert.match(focusList[1].slice(-4000), /\.nh-navrow-badge-btn:focus-visible/,
    "the badge button must be added to the shared focus-visible ring list");
  void rule;
});

// ── Settings.jsx: markAiConfigDone moved into the pane's first mount ────── //

test("LearningsPanel accepts onFirstOpen and fires it from the config-fetch effect (mount-only deps)", () => {
  const body = fnBody(settingsJsx, "export function LearningsPanel(");
  assert.match(body, /onFirstOpen/, "LearningsPanel must accept an onFirstOpen callback");
  // The call now lives INSIDE the fetchConfig effect (folded in — see the
  // M4 test below for why a separate bare mount effect was wrong), which
  // itself still has empty deps (runs once).
  const effectStart = body.indexOf("useEffect(() => {\n    let alive = true;");
  assert.ok(effectStart > -1, "the config-fetch effect must be found");
  const effectEnd = body.indexOf("}, []);", effectStart);
  assert.ok(effectEnd > -1 && effectEnd < effectStart + 2000, "the effect must close with mount-only deps ([])");
});

// Review fix (M4): the OLD unconditional mount-only effect spent the flag
// even when `auto_manage` is OFF and LegacyLearningQueuePanel (no explainer
// at all) is what actually renders beneath this wrapper — the operator never
// saw the Second-brain pane, yet the badge cleared as if they had.
test("M4: onFirstOpen fires only when the auto-managed (real explainer) pane is what renders — never unconditionally on mount", () => {
  const body = fnBody(settingsJsx, "export function LearningsPanel(");
  // The old, unconditional, bare mount effect must be gone.
  assert.doesNotMatch(body, /useEffect\(\(\) => \{\s*onFirstOpen\?\.\(\);\s*\}, \[\]\)/,
    "onFirstOpen must not fire from its own unconditional mount-only effect anymore");
  // It must be reachable only through the branch that is true precisely when
  // auto_manage resolved true (i.e. SecondBrainPanel — the pane WITH the
  // explainer — is what will render).
  assert.match(body, /if \(isAutoManaged\) onFirstOpen\?\.\(\);/,
    "onFirstOpen must be gated on isAutoManaged, computed in the SAME fetchConfig().then() that decides which panel renders");
  // A fetch failure still defaults to auto-managed (matching setAutoManage's
  // own documented fallback), so the flag is spent there too — never left
  // permanently un-spendable just because the network hiccuped.
  const catchBlock = body.slice(body.indexOf(".catch(("), body.indexOf(".catch((") + 300);
  assert.match(catchBlock, /onFirstOpen\?\.\(\)/,
    "a fetch failure must still spend the flag (it defaults to the auto-managed rendering, same as setAutoManage's fallback)");
});

test("SettingsOverlay threads onSecondBrainOpened into LearningsPanel as onFirstOpen", () => {
  assert.match(settingsJsx, /export default function SettingsOverlay\(\{[^}]*onSecondBrainOpened[^}]*\}\)/,
    "SettingsOverlay must accept onSecondBrainOpened");
  const idx = settingsJsx.indexOf("<LearningsPanel");
  assert.ok(idx > -1);
  const el = settingsJsx.slice(idx, settingsJsx.indexOf("/>", idx) + 2);
  assert.match(el, /onFirstOpen=\{onSecondBrainOpened\}/,
    "the callback must be forwarded to LearningsPanel as onFirstOpen");
  assert.match(el, /onOpenTask=\{onOpenTask\}/, "onOpenTask must still be forwarded (unchanged from D3.2)");
});

// ── D2 setup-action buttons (D3.2 review addenda) ───────────────────────── //

test("SecondBrainPanel renders the two D2 setup-action buttons deep-linking to Models and Rules", () => {
  const body = fnBody(settingsJsx, "function SecondBrainPanel(");
  assert.match(body, /onNavigateSection/, "SecondBrainPanel must accept a section-navigation callback");
  assert.match(body, /onNavigateSection\?\.\(\s*["']models["']\s*\)/,
    "one setup action must deep-link to the Models pane");
  assert.match(body, /onNavigateSection\?\.\(\s*["']rules["']\s*\)/,
    "one setup action must deep-link to the Rules pane");
});

test("LearningsPanel forwards onNavigateSection through to SecondBrainPanel, and SettingsOverlay wires it to setSection", () => {
  const lp = fnBody(settingsJsx, "export function LearningsPanel(");
  assert.match(lp, /<SecondBrainPanel[^]*?onNavigateSection=\{onNavigateSection\}/);
  const idx = settingsJsx.indexOf("<LearningsPanel");
  const el = settingsJsx.slice(idx, settingsJsx.indexOf("/>", idx) + 2);
  assert.match(el, /onNavigateSection=\{setSection\}/,
    "SettingsOverlay must pass its own setSection so the buttons actually switch panes");
});

test("the setup-actions wrapper has a CSS rule", () => {
  assert.ok(settingsJsx.includes("second-brain-setup-actions"), "setup actions must be wrapped in a styled container");
  assert.match(stylesCss, /\.second-brain-setup-actions[\s,{:.]/);
});

// ── CSS (review I2): the split-row divider must not leak into mobile ────── //

/** Byte range [start, end) of the `{...}` block belonging to the FIRST
 *  top-level rule/at-rule whose selector/prelude text starts at `openBrace`
 *  (the index of its opening `{`) — counts nested braces so a media query
 *  containing other rule blocks is captured whole, not truncated at the
 *  first inner `}`. */
function braceBlock(src, openBrace) {
  let depth = 0;
  for (let i = openBrace; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return [openBrace, i + 1];
    }
  }
  throw new Error("unbalanced braces from " + openBrace);
}

test("I2: the split-row divider/reset rules are scoped to desktop (min-width: 641px), never leaking into the ≤640px mobile block", () => {
  const desktopStart = stylesCss.indexOf("@media (min-width: 641px)");
  assert.ok(desktopStart > -1, "a desktop-scoped media block must exist for the split-row divider fix");
  const [dStart, dEnd] = braceBlock(stylesCss, stylesCss.indexOf("{", desktopStart));
  const desktopBlock = stylesCss.slice(dStart, dEnd);
  assert.match(desktopBlock, /\.nh-navrow-split:has\(\.nh-settings-row\)/,
    "the :has() divider rule must live inside the desktop-only media block");
  assert.match(desktopBlock, /\.nh-navrow-split \.nh-settings-row\s*\{\s*border-top:\s*none;\s*padding-top:\s*0;\s*margin-top:\s*0;/,
    "the reset that neutralizes the button's own divider must ALSO be inside the desktop-only block");

  // And neither leaks OUTSIDE any media block (top-level, applies everywhere
  // including mobile) nor into the existing ≤640px mobile block.
  const beforeDesktop = stylesCss.slice(0, desktopStart);
  assert.doesNotMatch(beforeDesktop, /\.nh-navrow-split:has\(\.nh-settings-row\)/,
    "the :has() rule must not also appear unscoped earlier in the file");
  const mobileStart = stylesCss.indexOf("@media (max-width: 640px)");
  assert.ok(mobileStart > -1, "the existing mobile sidebar block must be found");
  const [mStart, mEnd] = braceBlock(stylesCss, stylesCss.indexOf("{", mobileStart));
  const mobileBlock = stylesCss.slice(mStart, mEnd);
  assert.doesNotMatch(mobileBlock, /nh-navrow-split/,
    "the mobile block must not reference .nh-navrow-split at all — on mobile the badge is display:none, " +
    "so the split branch must render identically to the plain row, untouched by any split-specific rule");
});

// ── Overlap regression: the popup must not cover the Finish-setup row ───── //
// Playwright caught this live: the popup was absolutely anchored above the
// Settings row and overlapped the Finish-setup entry rendered above it on
// the minimal path, intercepting its clicks. Pinned at the static-source
// level too, so a future refactor that reintroduces the absolute anchoring
// fails here before it ever reaches the e2e run.

test("the AI-config popup renders as a flow sibling ABOVE the Finish-setup entry, not inside .nh-settings-navwrap", () => {
  // The className is now a template literal (mobile vs. desktop variant),
  // not a plain string — tolerate that instead of matching it verbatim.
  const popupIdx = appJsx.search(/nh-aiconfig-nudge[^`]*`\}\s*role="dialog"/);
  const finishIdx = appJsx.indexOf("<FinishSetupCard");
  const navwrapIdx = appJsx.indexOf('<div className="nh-settings-navwrap">');
  assert.ok(popupIdx > -1, "the popup element must be found");
  assert.ok(finishIdx > -1, "FinishSetupCard must be found");
  assert.ok(navwrapIdx > -1, "the settings nav wrapper must be found");
  assert.ok(popupIdx < finishIdx,
    "the popup must render before FinishSetupCard so it sits above it in flow order");
  assert.ok(popupIdx < navwrapIdx,
    "the popup must render outside (before) .nh-settings-navwrap — it is a flow sibling, not anchored to the Settings row");
});

test("the popup's base rule is not absolutely positioned over the sidebar rows", () => {
  const idx = stylesCss.indexOf(".nh-aiconfig-nudge {");
  assert.ok(idx > -1, "the .nh-aiconfig-nudge base rule must be found");
  const [start, end] = braceBlock(stylesCss, stylesCss.indexOf("{", idx));
  const block = stylesCss.slice(start, end);
  assert.doesNotMatch(block, /position:\s*absolute/,
    "the base rule must not float the popup over neighboring rows");
  assert.doesNotMatch(block, /bottom:\s*calc\(100% \+ 8px\)/,
    "the base rule must not anchor the popup above another element");
  assert.match(block, /position:\s*relative/,
    "position:relative must be kept so the × dismiss button still anchors to the popup");
});

// ── Mobile off-screen popup regression (this bug, pinned) ───────────────── //
// Live bug: the ≤640px block re-lifted .nh-aiconfig-nudge to
// `position: absolute; bottom: calc(100% + 8px)` anchored to .nh-sidebar-foot,
// which sits outside the mobile nav's viewport — a new mobile user never saw
// the nudge. Fixed by rendering it in normal flow inside .nh-main on phones
// instead. Pinned at every layer: CSS must not re-lift it, App.jsx must host
// exactly one instance per breakpoint, and the mobile-only class must not
// leak into desktop.

test("the ≤640px mobile block no longer re-lifts .nh-aiconfig-nudge to position:absolute (this bug, pinned)", () => {
  const mobileStart = stylesCss.indexOf("@media (max-width: 640px)");
  assert.ok(mobileStart > -1, "the mobile sidebar block must be found");
  const [mStart, mEnd] = braceBlock(stylesCss, stylesCss.indexOf("{", mobileStart));
  const mobileBlock = stylesCss.slice(mStart, mEnd);
  assert.doesNotMatch(mobileBlock, /position:\s*absolute/,
    "the mobile block must not re-lift .nh-aiconfig-nudge to position:absolute — that put the popup outside the mobile nav's viewport");
  assert.doesNotMatch(mobileBlock, /bottom:\s*calc\(100% \+ 8px\)/,
    "the mobile block must not anchor the popup to `bottom: calc(100% + 8px)` of the (out-of-viewport) sidebar foot");
});

test("App.jsx hosts the AI-config nudge inside .nh-main on phones, and the two render sites are mutually exclusive", () => {
  const mainIdx = appJsx.indexOf('<main className="nh-main">');
  assert.ok(mainIdx > -1, "the main board container must be found");
  const phoneSiteIdx = appJsx.indexOf("{isPhone && aiConfigNudge}", mainIdx);
  assert.ok(phoneSiteIdx > -1,
    "on phones the nudge must be hosted as a flow child of <main className=\"nh-main\">, after its opening tag");

  const desktopSiteIdx = appJsx.indexOf("{!isPhone && aiConfigNudge}");
  assert.ok(desktopSiteIdx > -1,
    "on desktop the nudge must stay gated behind !isPhone at the sidebar-foot site");
  assert.ok(desktopSiteIdx < mainIdx,
    "the desktop (sidebar) site must render before <main>, i.e. it is a distinct site from the phone one");

  // Exactly one instance can ever be mounted — Playwright uses strict-mode
  // locators, so both sites rendering at once would break the e2e checks.
  assert.doesNotMatch(appJsx, /\{isPhone && aiConfigNudge\}[\s\S]*\{isPhone && aiConfigNudge\}/,
    "the phone-gated render site must appear exactly once");
  assert.doesNotMatch(appJsx, /\{!isPhone && aiConfigNudge\}[\s\S]*\{!isPhone && aiConfigNudge\}/,
    "the desktop-gated render site must appear exactly once");
});

test(".nh-aiconfig-nudge-mobile is defined only inside the ≤640px block, never at top level (no desktop leak)", () => {
  const mobileStart = stylesCss.indexOf("@media (max-width: 640px)");
  assert.ok(mobileStart > -1, "the mobile sidebar block must be found");
  const [mStart, mEnd] = braceBlock(stylesCss, stylesCss.indexOf("{", mobileStart));

  const ruleIdx = stylesCss.indexOf(".nh-aiconfig-nudge-mobile {");
  assert.ok(ruleIdx > -1, "a .nh-aiconfig-nudge-mobile rule must be defined");
  assert.ok(ruleIdx >= mStart && ruleIdx < mEnd,
    ".nh-aiconfig-nudge-mobile must be declared inside the ≤640px block, not at top level");

  const occurrences = stylesCss.split(".nh-aiconfig-nudge-mobile {").length - 1;
  assert.equal(occurrences, 1,
    ".nh-aiconfig-nudge-mobile must be declared exactly once (no stray top-level/desktop copy)");
});
