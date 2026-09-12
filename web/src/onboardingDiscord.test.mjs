import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative, sep } from "node:path";

import { DISCORD_INVITE_URL } from "./community.js";
import { forwardDisabled, canJumpTo } from "./onboardingNav.js";

// The "Community" step (spec: "Onboarding offers the Discord once, reading
// the one invite we have"). Static source analysis, the same way
// onboardingIntegrations.test.mjs / onboardingNav.test.mjs read Onboarding.jsx
// — no jsdom/React renderer is wired into this project's `node --test`
// harness. Real-render coverage (clicking the step, opening the link,
// reaching Launch either way) lives in e2e/onboarding-discord-step.mjs.
//
// Measured AA contrast table (WCAG 2.1, see test 8 below) for the two
// tokens the step's text actually uses, against every opaque ambient surface
// in both themes — all clear 4.5:1, including --surface-3, the hard rule.
// (`.ob-faint`, i.e. `--accent-500`, is deliberately NOT used by this step:
// on light `--surface-2` — the top of `.ob-card`'s gradient, which is where
// this step's content actually sits — it measures 4.41:1, under AA. That
// pairing already ships elsewhere in the wizard, e.g. the integrations
// step's "(optional)" tag, but this step must not add a second instance of
// a failing pair, so "(optional)" and the echoed URL use `--text-muted`
// instead via the existing `ob-sub`/`ob-note` classes.)
//
//   token         | dark: --base/-1/-2/-3        | light: --base/-1/-2/-3
//   --text-hi     | 15.92 / 14.18 / 12.89 / 11.57 | 15.09 / 16.46 / 13.94 / 14.69
//   --text-muted  |  8.63 /  7.69 /  6.99 /  6.27 |  7.02 /  7.65 /  6.48 /  6.83

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "Onboarding.jsx"), "utf8");
const css = readFileSync(join(here, "styles.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

// ── isolate the step's own markup ────────────────────────────────────────
const STEP = (() => {
  const start = src.indexOf('{step.key === "discord" &&');
  assert.ok(start > 0, "the discord step must exist");
  const end = src.indexOf('{step.key === "summary" &&', start);
  assert.ok(end > start, "could not bound the discord step");
  return src.slice(start, end);
})();

// ── AC1: reachable ───────────────────────────────────────────────────────
test("BASE_STEPS carries a discord step directly before summary, which stays last", () => {
  const base = src.match(/const BASE_STEPS = \[([\s\S]*?)\n\];/);
  assert.ok(base, "the base-step list must still exist as its own array");
  const keys = [...base[1].matchAll(/key: "(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(keys, ["welcome", "repos", "projects", "integrations", "discord", "summary"]);
  const titles = Object.fromEntries(
    [...base[1].matchAll(/key: "(\w+)",\s+title: "([^"]+)"/g)].map((m) => [m[1], m[2]]),
  );
  assert.ok(titles.discord && titles.discord.trim().length > 0, "the discord step needs a non-empty title");
});

// ── AC1: openable ────────────────────────────────────────────────────────
test("the step renders a plain outbound anchor to the invite — no embed of any kind", () => {
  assert.match(STEP, /<a className="ob-btn-ghost"/);
  assert.match(STEP, /href=\{DISCORD_INVITE_URL\}/, "the href must come from the imported constant, never a literal");
  assert.match(STEP, /target="_blank"/);
  assert.match(STEP, /rel="[^"]*noreferrer[^"]*"/);
  assert.match(STEP, /rel="[^"]*noopener[^"]*"/);
  for (const forbidden of [/<iframe/i, /widget/i, /discord\.com/i, /<script/i, /fetch\(/, /useEffect/]) {
    assert.doesNotMatch(STEP, forbidden, `the step must not contain ${forbidden}`);
  }
});

test("the step is rendered inside the wizard's own <Stagger> entrance wrapper, like every other step", () => {
  const staggerStart = STEP.indexOf("<Stagger>");
  assert.ok(staggerStart > -1 && staggerStart < 60, "the step's first real element must be <Stagger>");
  assert.match(STEP, /<\/Stagger>\s*\)\}\s*$/, "the step must close its own <Stagger> immediately before the block ends");
});

test('the step tells the user skipping is safe, in its own rendered copy', () => {
  assert.match(STEP, /Skipping changes nothing/, "the step must say outright that ignoring it has no consequence");
});

test(".ob-note's own rule paints text --text-muted, never --accent-500 (the echoed URL sits in plain .ob-note, not .ob-note code)", () => {
  // Distinct from the vocabulary test (which forbids the class "ob-faint" on
  // the step's elements) — this pins the *rule itself* so repainting
  // `.ob-note { color: var(--accent-500) }` directly (never adding the class
  // "ob-faint") would also be caught.
  const rule = css.match(/\.ob-note\s*\{([^}]*)\}/);
  assert.ok(rule, ".ob-note base rule must exist");
  assert.match(rule[1], /color:\s*var\(--text-muted\)/, ".ob-note must paint with --text-muted");
  assert.doesNotMatch(rule[1], /--accent-500/, ".ob-note's own rule must never reference --accent-500");
});

// ── warm-editorial copy: no implementation breadcrumb ───────────────────
test("the step's rendered copy names no internal function or source file", () => {
  // A first-run user reads this text; it must never surface an internal
  // symbol/filename (e.g. "routeExternally in desktop/main.mjs") as if it
  // were product copy — that reads like an implementation comment that
  // leaked into the UI, not the warm-editorial voice DESIGN.md requires.
  for (const forbidden of [/routeExternally/i, /desktop\/main\.mjs/i, /\.mjs\b/i, /\.jsx\b/i]) {
    assert.doesNotMatch(STEP, forbidden, `the step's copy must not name an internal symbol/file: ${forbidden}`);
  }
});

// ── AC1: never blocks ────────────────────────────────────────────────────
test("the discord step never gates forward progress, behaviourally", () => {
  // discord is BASE_STEPS index 4, lastIndex is now 5 (6 steps).
  assert.equal(forwardDisabled({ index: 4, lastIndex: 5, busy: false }), false);
  assert.equal(canJumpTo({ from: 0, to: 4, busy: false }), true, "jumping to the discord step must be permitted");
});

test('"discord" appears in no gating expression, and the step has no disabled/required control', () => {
  for (const gate of ["continueBlocked", "projectsBlockContinue", "launchReadiness", "canStartMinimal", "forwardDisabled"]) {
    const re = new RegExp(`${gate}[^\\n]*discord`, "i");
    assert.doesNotMatch(src, re, `${gate} must never reference the discord step`);
  }
  const finishBody = src.slice(src.indexOf("async function finish()"), src.indexOf("\n  }\n", src.indexOf("async function finish()")));
  assert.doesNotMatch(finishBody, /discord/i, "finish() must not reference the discord step");
  assert.doesNotMatch(STEP, /disabled=/, "the discord step must not disable anything");
  assert.doesNotMatch(STEP, /required/, "the discord step must not require anything");
});

// ── AC2: single source, with a positive control ─────────────────────────
function walk(root, skip = new Set(["node_modules", "dist", ".git"])) {
  const out = [];
  for (const name of readdirSync(root)) {
    if (skip.has(name)) continue;
    const p = join(root, name);
    const st = statSync(p);
    if (st.isDirectory()) out.push(...walk(p, skip));
    else out.push(p);
  }
  return out;
}

const REPO_ROOT = join(here, "..", "..");
const SCAN_DIRS = ["web/src", "web/e2e", "desktop", "src/no_human"]
  .map((d) => join(REPO_ROOT, d))
  .filter((d) => { try { return statSync(d).isDirectory(); } catch { return false; } });

function filesMatching(pattern) {
  const hits = [];
  for (const dir of SCAN_DIRS) {
    for (const f of walk(dir)) {
      let body;
      try { body = readFileSync(f, "utf8"); } catch { continue; }
      if (pattern.test(body)) hits.push(relative(REPO_ROOT, f).split(sep).join("/"));
    }
  }
  return hits.sort();
}

test("the invite literal exists exactly once in source, at web/src/community.js — with controls", () => {
  // Derived from the constant, never re-typed. This line used to carry the
  // invite code as a literal — the one thing this whole file exists to stop —
  // so changing the invite turned the test red for a reason that had nothing
  // to do with the invite being wrong.
  const inviteRe = new RegExp(
    DISCORD_INVITE_URL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const hits = filesMatching(inviteRe);
  assert.deepEqual(hits, ["web/src/community.js"], "the raw invite URL must live in exactly one source file");

  // Positive control: the same scanner, over the same tree, must find the
  // exported symbol in more than one place (its definition, plus every
  // importer) — proving the walker actually finds matches rather than
  // silently returning nothing.
  const symbolHits = filesMatching(/DISCORD_INVITE_URL/);
  assert.ok(symbolHits.length >= 2, `expected >=2 files importing/defining DISCORD_INVITE_URL, got ${symbolHits.length}: ${symbolHits.join(", ")}`);

  // Negative control: an absent sentinel must return zero hits. Built by
  // concatenation, not written as one literal — this test file lives inside
  // the very tree being scanned, so a sentinel spelled out whole here would
  // trivially "find" itself and the control would prove nothing.
  const sentinel = ["ZZZ_SENTINEL", "_DOES_NOT", "_EXIST", "_ANYWHERE_ZZZ"].join("");
  const noneHits = filesMatching(new RegExp(sentinel));
  assert.deepEqual(noneHits, []);

  assert.doesNotMatch(src, /discord\.gg/, "Onboarding.jsx must not carry the literal URL");
  assert.match(src, /import \{ DISCORD_INVITE_URL \} from "\.\/community\.js";/, "Onboarding.jsx must import the constant");
});

// ── AC2: byte-identical to the published invite, wherever a doc publishes it ─
// The docs are DISCOVERED on every run, never listed. A hand-kept list of doc
// filenames is the defect this guards against: it can only ever describe the
// repo as it stood the day it was typed, so the first translated README added
// afterwards is scanned by nobody and a wrong or stale invite in it ships
// green. Both halves of the comparison come from the repo itself — the files
// from a walk, the URL from the DISCORD_INVITE_URL imported at the top of this
// file — so the invite is never re-typed here either.
const DOC_SKIP = new Set([
  // Vendored and generated trees only. An omission here can only widen what
  // gets scanned, never narrow it, so this set cannot hide a doc.
  "node_modules", "dist", "build", "coverage", ".git", ".venv", "venv",
  "__pycache__", ".pytest_cache", ".claude-worktrees",
]);
const DOC_EXT = /\.(md|ya?ml)$/i;

const docFiles = walk(REPO_ROOT, DOC_SKIP)
  .map((p) => relative(REPO_ROOT, p).split(sep).join("/"))
  .filter((p) => DOC_EXT.test(p))
  .sort();

// A second, independent discovery of the same ground: every translation of the
// README sits at the repo root as README.<lang>.md. readdirSync finds them, so
// a language added tomorrow is covered with no edit here — and it gives the
// walk above a floor the walk cannot satisfy by returning nothing.
const rootReadmes = readdirSync(REPO_ROOT)
  .filter((n) => /^README(\.[A-Za-z-]+)?\.md$/.test(n))
  .sort();

test("every doc occurrence of the invite is exactly the constant — docs discovered, never listed", () => {
  // Fail closed. An empty walk, a walk that lost the READMEs, or a root with
  // no README at all is a FAILURE here, not a vacuous pass over zero files.
  assert.ok(rootReadmes.length > 0, "no README.md at the repo root — README discovery is broken");
  for (const name of rootReadmes) {
    assert.ok(docFiles.includes(name), `the doc walk never reached ${name} — doc discovery is broken`);
  }

  // Bounded to the invite-code alphabet so trailing prose punctuation (a
  // markdown ")", a full-width Chinese "，", a Korean particle glued on with
  // no space) is never swept into the match — no stripping step needed.
  // `_` and `-` are legal in a Discord vanity code. Without them, the day
  // this project buys `discord.gg/no-human-ai` the regex truncates it to
  // `discord.gg/no` and the test goes permanently red against its own
  // constant — failing for a reason that has nothing to do with drift.
  const urlRe = /https:\/\/discord\.gg\/[A-Za-z0-9_-]+/g;
  const carriers = new Map();
  for (const f of docFiles) {
    const found = [...readFileSync(join(REPO_ROOT, f), "utf8").matchAll(urlRe)].map((m) => m[0]);
    if (found.length > 0) carriers.set(f, found);
  }

  // The non-zero floor, established by discovery rather than by a number typed
  // here: every README the repo actually has publishes the invite.
  for (const name of rootReadmes) {
    assert.ok(carriers.has(name), `${name} publishes no Discord invite, but every README translation does`);
  }
  const occurrences = [...carriers].flatMap(([f, found]) => found.map((u) => [f, u]));
  assert.ok(
    occurrences.length >= rootReadmes.length,
    `expected at least one invite per README (${rootReadmes.length}), found ${occurrences.length} across ${carriers.size} file(s)`,
  );

  for (const [f, u] of occurrences) {
    assert.equal(u, DISCORD_INVITE_URL, `${f} publishes ${u} — the one invite we have lives in web/src/community.js`);
  }
});

// ── AC3: vocabulary ──────────────────────────────────────────────────────
test("the step uses only the wizard's existing class vocabulary, each backed by a real rule", () => {
  const ALLOWED = new Set(["ob-h2", "ob-sub", "ob-note", "ob-row", "ob-btn-ghost"]);
  const classNames = [...STEP.matchAll(/className="([^"]+)"/g)].flatMap((m) => m[1].split(/\s+/));
  assert.ok(classNames.length > 0, "the step must render at least one classed element");
  for (const cls of classNames) {
    assert.ok(ALLOWED.has(cls), `class "${cls}" is not in the wizard's existing vocabulary`);
    assert.match(css, new RegExp(`\\.${cls}\\b`), `class "${cls}" has no rule in styles.css`);
  }
  assert.doesNotMatch(STEP, /style=\{\{/, "no inline style");
  assert.doesNotMatch(STEP, /#[0-9a-fA-F]{3,6}\b/, "no literal hex colour");
  assert.doesNotMatch(STEP, /\brgb\(|\bhsl\(/, "no literal rgb()/hsl() colour");
  assert.doesNotMatch(STEP, /font-family/, "no new font declared inline");
  assert.doesNotMatch(STEP, /@import|fonts\.googleapis/, "no external font fetch");
});

// ── AC3/AC4: no new colour ───────────────────────────────────────────────
test("no Discord-branded token or literal was added to styles.css", () => {
  assert.doesNotMatch(css, /#5865F2/i, "Discord's brand blurple must not appear as a literal");
  assert.doesNotMatch(css, /--discord/i, "no --discord* token was introduced");
  assert.match(css, /a\.ob-btn-ghost\s*\{/, "the anchor-specific ob-btn-ghost rule must exist");
  const rule = css.match(/a\.ob-btn-ghost\s*\{([^}]*)\}/);
  assert.ok(rule, "a.ob-btn-ghost rule must exist");
  assert.doesNotMatch(rule[1], /\bcolor\s*:/, "a.ob-btn-ghost must not declare its own color — no new colour pairing");
  assert.doesNotMatch(rule[1], /\bbackground\s*:/, "a.ob-btn-ghost must not declare its own background — no new colour pairing");
});

// ── AC4: AA contrast, including --surface-3 ─────────────────────────────
// Same WCAG 2.1 relative-luminance maths as contrast.test.mjs, reading real
// token values out of styles.css rather than hand-copying hex.
const parseColor = (value) => {
  const hex = value.match(/^#([0-9a-fA-F]{6})$/);
  if (hex) return [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16));
  return null;
};
const luminance = ([r, g, b]) => {
  const [R, G, B] = [r, g, b].map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * R + 0.7152 * G + 0.0722 * B;
};
const contrast = (fg, bg) => {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
};

const blockAfter = (re) => {
  const m = css.match(re);
  assert.ok(m, `expected to find the rule ${re}`);
  return m[1];
};
const declarations = (block) => {
  const out = new Map();
  for (const m of block.matchAll(/(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g)) out.set(m[1], m[2].trim());
  return out;
};
const THEME = {
  dark: declarations(blockAfter(/:root\s*\{([^}]*)\}/)),
  light: declarations(blockAfter(/\[data-theme="light"\]\s*\{([^}]*)\}/)),
};
function resolveToken(token, theme, seen = new Set()) {
  assert.ok(!seen.has(token), `circular token alias at ${token}`);
  seen.add(token);
  const raw = THEME[theme].get(token) ?? THEME.dark.get(token);
  if (!raw) return null;
  const alias = raw.match(/^var\(\s*(--[a-zA-Z0-9-]+)\s*\)$/);
  return alias ? resolveToken(alias[1], theme, seen) : parseColor(raw);
}

const AA_SMALL = 4.5;
const SURFACES = ["--base", "--surface-1", "--surface-2", "--surface-3"];

test("--text-hi and --text-muted clear AA against every surface (incl. --surface-3), both themes", () => {
  for (const theme of ["dark", "light"]) {
    for (const token of ["--text-hi", "--text-muted"]) {
      const fg = resolveToken(token, theme);
      assert.ok(fg, `${theme}: ${token} must resolve to a colour`);
      for (const surface of SURFACES) {
        const bg = resolveToken(surface, theme);
        assert.ok(bg, `${theme}: ${surface} must resolve to a colour`);
        const ratio = contrast(fg, bg);
        assert.ok(ratio >= AA_SMALL, `${theme}: ${token} on ${surface} is ${ratio.toFixed(2)}:1 < ${AA_SMALL}:1`);
      }
    }
  }
});

test("the step never uses .ob-faint (--accent-500), which fails AA on the card's own light --surface-2", () => {
  // .ob-card's background is a --surface-2 → --surface-1 gradient (styles.css),
  // so text near the top of the card sits on --surface-2 — not just --surface-3.
  // --accent-500 (what .ob-faint / .ob-note code render) falls under 4.5:1 there
  // in light theme, so this step must not put "(optional)" or the echoed URL in
  // that class, even though an older step (integrations) already does. Assert
  // both the fact that drives this rule and that the step never uses the class.
  const lightAccent = resolveToken("--accent-500", "light");
  const lightS2 = resolveToken("--surface-2", "light");
  const ratio = contrast(lightAccent, lightS2);
  assert.ok(ratio < AA_SMALL, `expected --accent-500 on light --surface-2 to still be a known AA failure (got ${ratio.toFixed(2)}:1) — if this token pair now passes, .ob-faint may be safe to use here again`);
  assert.doesNotMatch(STEP, /\bob-faint\b/, "the discord step must not render text in .ob-faint (--accent-500 fails AA on --surface-2 in light theme)");
});
