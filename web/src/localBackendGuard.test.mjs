import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// [re-home] Board rejects an unrunnable LOCAL-backend config before submit.
//
// Re-home of private PR #747 against current public main. That branch's own
// ACs assumed a JS-side `localBackendUnconfigured` heuristic in
// TaskComposer.jsx — one that no longer exists on this tree (grep confirms
// zero `local_model`/`local_base_url` hits below). The composer already
// gates submit purely on the server-provided `coder_backend_availability`
// (core.backend_settings.describe_backend, wired through
// core.runtime.assert_task_backend_usable — see tests/
// test_local_model_preflight.py for the server-side fix this file has
// nothing to add to). This file exists only to PIN that:
//   1. no JS-side local-only rule is ever reintroduced here, and
//   2. the existing server-driven gate (selectedBackendUnavailable /
//      canSubmit) actually blocks/unblocks on that server data, for `local`
//      exactly as for any other backend id — no special-casing.
//
// Like taskComposerDraft.test.mjs and taskComposerCreateRepo.test.mjs, there
// is no React renderer in this harness: the gating expression is extracted
// verbatim from the real source and EXECUTED (via `new Function`) against
// controlled `config`/`backend` inputs, so this proves behaviour rather than
// merely pattern-matching text.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

// Extracts the `backendAvailability` -> `selectedBackendInfo` ->
// `selectedBackendUnavailable` chain verbatim from TaskComposer.jsx. If this
// shape ever changes (e.g. a regression back to a JS-side truthiness check
// on llm.local_base_url/llm.local_model), the anchor itself stops matching
// and this fails loudly rather than silently testing stale text.
function extractSelectedBackendUnavailable(src) {
  const m = src.match(/const backendAvailability = new Map\([\s\S]*?: false;/);
  assert.ok(
    m,
    "expected the backendAvailability -> selectedBackendInfo -> " +
      "selectedBackendUnavailable chain, gated purely on " +
      "config.coder_backend_availability",
  );
  return new Function("config", "backend", `${m[0]}\nreturn selectedBackendUnavailable;`);
}

// Drops `//`-prefixed comment lines so a prose reference (e.g. this file's
// own parity-tracking comment above `backendAvailability`, which names
// `llm.local_model`/`llm.local_base_url` to explain what the SERVER preflight
// now covers) doesn't false-positive as a reintroduced JS-side rule. Only
// live code is checked.
function stripLineComments(src) {
  return src
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
}

test("the composer keeps no local-backend rule of its own", () => {
  const src = read("TaskComposer.jsx");
  const code = stripLineComments(src);
  // The private PR's `localBackendUnconfigured` heuristic read
  // llm.local_base_url/llm.local_model directly off `config` in the
  // frontend, so it could disagree with the server (e.g. an unset OPENAI
  // credential for 'codex' was invisible to a rule that only ever looked at
  // 'local' keys). Public main already deleted that heuristic; this pins it
  // never comes back as actual CODE (a parity-tracking comment naming those
  // keys to explain the server's check is fine and expected).
  assert.doesNotMatch(code, /local_model/);
  assert.doesNotMatch(code, /local_base_url/);
  assert.doesNotMatch(code, /localBackendUnconfigured/);
});

test("submit is blocked when the server reports the selected backend unavailable, and open when it does not", () => {
  const src = read("TaskComposer.jsx");
  const selectedBackendUnavailable = extractSelectedBackendUnavailable(src);

  // The server refused 'local' for the exact reason the runtime preflight
  // now names (llm.local_model missing) -> blocked. No JS-side re-derivation
  // of *why*; the composer only reads `available`.
  assert.equal(
    selectedBackendUnavailable(
      {
        coder_backend_availability: [
          { id: "local", available: false, reason: "... llm.local_model is not set ..." },
        ],
      },
      "local",
    ),
    true,
  );

  // Both llm.local_base_url and llm.local_model are set server-side -> open.
  assert.equal(
    selectedBackendUnavailable(
      { coder_backend_availability: [{ id: "local", available: true, reason: "" }] },
      "local",
    ),
    false,
  );

  // Not special to 'local': the same gate blocks any backend id the server
  // marks unavailable (e.g. 'codex' with no CLI/credential on this install).
  assert.equal(
    selectedBackendUnavailable(
      {
        coder_backend_availability: [
          { id: "codex", available: false, reason: "the codex CLI was not found" },
        ],
      },
      "codex",
    ),
    true,
  );

  // Absent evidence never blocks: an older server without the field, or a
  // GET /api/config still in flight, must degrade to "no opinion" rather
  // than refusing everything.
  assert.equal(selectedBackendUnavailable({}, "local"), false);
  assert.equal(selectedBackendUnavailable({ coder_backend_availability: [] }, "local"), false);

  // A lookup miss for the CHOSEN id specifically (field present, but this
  // id isn't in it) is the same "no opinion", not a refusal.
  assert.equal(
    selectedBackendUnavailable(
      { coder_backend_availability: [{ id: "codex", available: true, reason: "" }] },
      "local",
    ),
    false,
  );

  // No backend explicitly chosen (the picker's config-default option, "")
  // never blocks on this gate, regardless of what other ids' availability
  // says — effectiveCoderBackend/the disclosure logic own that path, not
  // this guard.
  assert.equal(
    selectedBackendUnavailable(
      { coder_backend_availability: [{ id: "local", available: false, reason: "x" }] },
      "",
    ),
    false,
  );
});
