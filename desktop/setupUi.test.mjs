// Guards the regression that Escape/secondary must never quit the app when a
// board is live behind the credential screen.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { CLI_INSTALL_LINE, codexKeyToSend, dismissTarget, existingSignInCopy, labels,
         parseCanReturn, requirementLine, restartFailedMessage, saveDisabled,
         saveProgress } from "./setupUi.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));

test("parseCanReturn: only the explicit flag counts", () => {
  assert.equal(parseCanReturn("?canReturn=1"), true);
  assert.equal(parseCanReturn("?origin=x&canReturn=1"), true);
  assert.equal(parseCanReturn("?canReturn=0"), false);
  assert.equal(parseCanReturn("?canReturn=true"), false, "only \"1\"");
  assert.equal(parseCanReturn(""), false);
  assert.equal(parseCanReturn(undefined), false, "first run has no query");
});

test("dismissTarget: NEVER quit when a board is behind the screen", () => {
  assert.equal(dismissTarget(true), "dismiss",
    "quitting here would kill the app and stop a spawned server");
  assert.equal(dismissTarget(false), "quit",
    "genuine first run has nothing to dismiss to");
});

test("labels: the buttons describe what will actually happen", () => {
  const over = labels(true);
  assert.equal(over.secondary, "Back to board");
  assert.match(over.primary, /restart/, "saving restarts a running server");
  assert.match(over.hint, /interrupted/, "the interruption must be stated");

  const first = labels(false);
  assert.equal(first.secondary, "Quit");
  assert.equal(first.primary, "Save and start");
  assert.match(first.hint, /API key/);
});

test("labels: api_key mode states where the key lives and who it bills", () => {
  const first = labels(false, "api_key");
  assert.equal(first.secondary, "Quit");
  assert.equal(first.primary, "Save and start");
  assert.match(first.hint, /\.env/, "storage location is stated");
  assert.match(first.hint, /Anthropic account/i, "billing destination is stated");
  // The same screen ships on macOS, Windows AND Linux (measured on a real
  // Ubuntu 24.04 desktop 2026-08-18: it said "this Mac"). Platform-neutral
  // copy, or the first thing a Linux/Windows user reads is wrong about them.
  assert.doesNotMatch(first.hint, /\bMac\b/, "credential copy must not name macOS");

  const over = labels(true, "api_key");
  assert.match(over.hint, /interrupted/,
    "re-entry over a live board still warns about the restart in every mode");
});

test("labels: no mode's copy disparages the other (claims discipline, D4)", () => {
  for (const canReturn of [true, false]) {
    for (const mode of ["subscription", "api_key"]) {
      const { hint } = labels(canReturn, mode);
      assert.doesNotMatch(hint, /will not work|would bill the metered/i,
        `labels(${canReturn}, ${mode}) must describe, not disparage`);
    }
  }
  // Same claims discipline applies to the existing-sign-in row's copy — it
  // must describe the manual-paste path it points at, not disparage it as a
  // fallback.
  const copy = existingSignInCopy();
  for (const text of [copy.button, copy.note, copy.guidance]) {
    assert.doesNotMatch(text, /will not work|would bill the metered/i,
      "existingSignInCopy() must describe, not disparage");
  }
});

test("saveProgress: never claims to stop an old server on first run", () => {
  // labels(false) already says "Save and START" — there is nothing to stop.
  for (const ms of [0, 5000, 20000, 40000]) {
    assert.doesNotMatch(saveProgress(ms, false), /old server/i,
      `first-run copy at ${ms}ms must not mention an old server`);
  }
  // Re-entry over a live board DOES stop one, and should say so.
  assert.match(saveProgress(5000, true), /old server/i);
});

test("saveProgress: the message advances so a 43s save cannot look like a hang", () => {
  const at = (ms) => saveProgress(ms, true);
  assert.equal(at(0), at(2999), "no churn in the first seconds");
  // Each stage must actually differ from the previous one, or the copy is inert.
  const stages = [at(0), at(5000), at(20000), at(40000)];
  assert.equal(new Set(stages).size, 4,
    `every stage must say something new, got ${JSON.stringify(stages)}`);
  assert.ok(stages.every((m) => typeof m === "string" && m.length > 0));
});

test("restartFailedMessage: names the action that actually helps, in both cases", () => {
  const ours = restartFailedMessage("http://127.0.0.1:8420", true);
  assert.match(ours, /quit no_human/i,
    "our own server: the only fix is to quit the app");
  assert.doesNotMatch(ours, /nh start|restart that server/i,
    "a friend on a packaged DMG has no terminal and never ran `nh start`");

  const theirs = restartFailedMessage("http://127.0.0.1:8420", false);
  assert.match(theirs, /another server/i);
  assert.doesNotMatch(theirs, /quit no_human/i,
    "quitting the app cannot free a port another process holds");
});

test("the first-run screen's copy is platform-neutral — it ships on macOS, Windows and Linux", () => {
  const html = fs.readFileSync(path.join(here, "token.html"), "utf8");
  assert.doesNotMatch(html, /this Mac\b/,
    "token.html says 'this Mac' — a Linux/Windows user reads that on first run (seen on Ubuntu 24.04, 2026-08-18)");
  // The existing-sign-in row's copy ships on the same three platforms — no
  // OS-specific terminal app name or path shape.
  const copy = existingSignInCopy();
  for (const text of [copy.button, copy.note, copy.guidance]) {
    assert.doesNotMatch(text, /\bMac\b|Terminal\.app|PowerShell|cmd\.exe/,
      `existingSignInCopy() must stay platform-neutral, got: ${text}`);
  }
});

test("the existing-sign-in copy promises only what happens locally", () => {
  const copy = existingSignInCopy();
  // It must name the actual command so the instruction is copy-pasteable...
  assert.match(copy.note, /claude setup-token/);
  assert.match(copy.guidance, /claude setup-token/);
  // ...tell the user to paste the result themselves...
  assert.match(copy.guidance, /paste/i);
  // ...and never claim no_human does anything automatically — the row only
  // ever reveals the same manual-paste field every other path uses.
  for (const text of [copy.button, copy.note, copy.guidance]) {
    assert.doesNotMatch(text, /automatic|import|we'll open|opening it for you/i,
      `existingSignInCopy() overpromises automation: ${text}`);
  }

  // Source-level guardrails on token.html itself: the deleted IPC-driven
  // click handler and its "couldn't finish automatically" copy must not
  // reappear, #steps (the manual-paste instructions the click reveals) must
  // still exist, and "use-existing" must only ever be built inside
  // renderExistingSignin — never as static markup that could render before
  // detection completes.
  const html = fs.readFileSync(path.join(here, "token.html"), "utf8");
  assert.doesNotMatch(html, /importClaudeSignIn/,
    "token.html must not call the deleted nh:claude-import-token bridge method");
  assert.doesNotMatch(html, /Couldn't finish that automatically/,
    "the old IPC-failure copy must be gone along with the IPC call it described");
  assert.match(html, /id="steps"/,
    "the manual-paste instructions the click reveals must still exist");
  const useExistingOccurrences = html.split('id = "use-existing"').length - 1;
  assert.equal(useExistingOccurrences, 1,
    "\"use-existing\" must be built exactly once, inside renderExistingSignin");
});

test("requirementLine: an OK claude names its version and where it was found", () => {
  const line = requirementLine("claude", { ok: true, path: "/opt/homebrew/bin/claude", version: "2.1.3" });
  assert.equal(line, "✓ claude 2.1.3 at /opt/homebrew/bin/claude");
});

test("requirementLine: node's OK row has no version field and must not print 'undefined'", () => {
  // The nh:requirements contract carries no version for node — a naive
  // `${i.version}` interpolation would print the literal word "undefined"
  // into the row shown to the operator.
  const line = requirementLine("node", { ok: true, path: "/usr/local/bin/node" });
  assert.equal(line, "✓ node at /usr/local/bin/node");
  assert.doesNotMatch(line, /undefined/);
});

test("requirementLine: a missing tool shows the exact install line from the paragraph it replaced", () => {
  assert.equal(requirementLine("claude", { ok: false, path: "", version: "" }),
    `✗ claude not found — ${CLI_INSTALL_LINE}`);
  assert.equal(requirementLine("node", { ok: false, path: "" }),
    `✗ node not found — ${CLI_INSTALL_LINE}`);
  // The exact copy the old static #prereq paragraph used to show, so a friend
  // who has seen it once still recognises it in the checklist.
  assert.equal(CLI_INSTALL_LINE,
    "Install Node.js (nodejs.org), then run npm install -g @anthropic-ai/claude-code");
});

test("requirementLine: never throws on a missing/undefined info (broken IPC round-trip)", () => {
  assert.equal(requirementLine("claude", undefined), `✗ claude not found — ${CLI_INSTALL_LINE}`);
  assert.equal(requirementLine("claude", null), `✗ claude not found — ${CLI_INSTALL_LINE}`);
});

test("saveDisabled: locked until claude resolves, unless the operator chose to skip", () => {
  assert.equal(saveDisabled(null, false), true, "nothing checked yet must not allow Save");
  assert.equal(saveDisabled({ claude: { ok: false }, node: { ok: true } }, false), true);
  assert.equal(saveDisabled({ claude: { ok: true }, node: { ok: false } }, false), false,
    "only claude gates Save — a missing node must not also block it");
  // The escape hatch: "I'll install it later" must free Save even with
  // nothing found, or a friend without claude installed is locked out for good.
  assert.equal(saveDisabled(null, true), false);
  assert.equal(saveDisabled({ claude: { ok: false } }, true), false);
});

test("codexKeyToSend: sends a key ONLY for api_key; subscription/skip send nothing (#6b)", () => {
  // The whole of constraint #6b at the UI layer: subscription and a skipped
  // section (no radio chosen) must never produce an OpenAI credential to write.
  assert.equal(codexKeyToSend("subscription", "sk-proj-typedanyway"), "",
    "subscription must send nothing even if a value is somehow present");
  assert.equal(codexKeyToSend("", "sk-proj-typedanyway"), "",
    "a skipped section sends nothing");
  assert.equal(codexKeyToSend("api_key", ""), "", "api_key with an empty field sends nothing");
  assert.equal(codexKeyToSend("api_key", "   "), "", "a whitespace-only field sends nothing");
  assert.equal(codexKeyToSend("api_key", "  sk-proj-real  "), "sk-proj-real",
    "api_key with a value sends the trimmed key");
});
