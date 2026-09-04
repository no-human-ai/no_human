import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { modelsPanelView, pendingBody, resetBody, applyError, reviewerBackendView } from "./modelsPanelView.js";

// 6d part 3: reviewer-backend visual proof is delivered via the hermetic UI
// walk (.no_human/ui_evidence.json) driving web/e2e/models-pane.mjs's
// --evidence-server fixture; this file's own coverage is unchanged.
// One fixture payload shared by every test below — the shape GET /api/models
// actually returns (see core/model_settings.py::models_payload). Nothing in
// this file invents an id, price, default or rule string of its own; every
// expectation is read out of this fixture.
const VENDOR_PIN_NOTE =
  "only Claude ids may run this role — the coder's backend (Claude/Codex/local) is a separate control.";
const DISABLED_REASON =
  "'gpt-5-codex' cannot be set as llm.primary_model: only the Claude backend reads that key.";
const COST_NOTE =
  "The operator's 2026-08-11 A/B reverted this role from claude-opus-5 back to claude-opus-4-8.";

function payload(overrides = {}) {
  return {
    roles: [
      {
        role: "coder",
        key: "primary_model",
        current: "claude-sonnet-5",
        default: "claude-sonnet-5",
        note: "",
        cost_note: "",
        options: [
          {
            id: "claude-sonnet-5",
            price_class: { label: "medium", input_rate: 3, output_rate: 15 },
            is_default: true,
            note: "",
            requires_backend: false,
          },
          {
            id: "gpt-5-codex",
            price_class: { label: "medium", input_rate: 3, output_rate: 15 },
            is_default: false,
            note: "",
            requires_backend: true,
            disabled_reason: DISABLED_REASON,
          },
        ],
      },
      {
        role: "reviewer",
        key: "review_model",
        current: "claude-opus-4-8",
        default: "claude-opus-4-8",
        note: VENDOR_PIN_NOTE,
        cost_note: COST_NOTE,
        options: [
          {
            id: "claude-opus-4-8",
            price_class: { label: "high", input_rate: 15, output_rate: 75 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
          {
            id: "claude-opus-5",
            price_class: { label: "high", input_rate: 18, output_rate: 90 },
            is_default: false,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
      {
        role: "planner",
        key: "planner_model",
        current: "claude-opus-5",
        default: "claude-opus-5",
        note: VENDOR_PIN_NOTE,
        cost_note: "",
        options: [
          {
            id: "claude-opus-5",
            price_class: { label: "high", input_rate: 18, output_rate: 90 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
      {
        role: "supervisor",
        key: "supervisor_model",
        current: "claude-sonnet-5",
        default: "claude-sonnet-5",
        note: VENDOR_PIN_NOTE,
        cost_note: "",
        options: [
          {
            id: "claude-sonnet-5",
            price_class: { label: "medium", input_rate: 3, output_rate: 15 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
      {
        role: "utility",
        key: "utility_model",
        current: "claude-haiku-4-5",
        default: "claude-haiku-4-5",
        note: VENDOR_PIN_NOTE,
        cost_note: "",
        options: [
          {
            id: "claude-haiku-4-5",
            price_class: { label: "low", input_rate: 0.8, output_rate: 4 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
    ],
    restart_required: false,
    ...overrides,
  };
}

// 1. Five rows in payload order, each field copied verbatim from the
// payload — plus a source-text guard that neither view-model file spells out
// a model id or a config key of its own. This is the "no literals in JS" AC
// made executable, not just asserted in prose.
test("modelsPanelView returns five rows in payload order, fields copied from the payload", () => {
  const p = payload();
  const view = modelsPanelView(p);
  assert.equal(view.unavailable, false);
  assert.equal(view.rows.length, 5);
  assert.deepEqual(
    view.rows.map((r) => r.role),
    ["coder", "reviewer", "planner", "supervisor", "utility"],
  );
  view.rows.forEach((row, i) => {
    const src = p.roles[i];
    assert.equal(row.role, src.role);
    assert.equal(row.key, src.key);
    assert.equal(row.current, src.current);
    assert.equal(row.default, src.default);
    assert.deepEqual(
      row.options.map((o) => o.id),
      src.options.map((o) => o.id),
    );
  });
});

test("neither view-model file hardcodes a model id, vendor prefix, or config key", () => {
  const files = {
    "modelsPanelView.js": readFileSync(new URL("./modelsPanelView.js", import.meta.url), "utf8"),
    "ModelsPanel.jsx": readFileSync(new URL("./ModelsPanel.jsx", import.meta.url), "utf8"),
  };
  for (const [name, src] of Object.entries(files)) {
    assert.doesNotMatch(src, /claude-/, `${name} names a claude- id`);
    assert.doesNotMatch(src, /gpt-/, `${name} names a gpt- id`);
    assert.doesNotMatch(src, /primary_model|review_model|planner_model|supervisor_model|utility_model/,
      `${name} spells out a config key`);
    // Constraint §6d: the reviewer backend picker's options must come from
    // the server's own GET /api/coder-backend payload (backendOptions), not
    // a second hand-rolled `["claude", ...]`-shaped SUPPORTED_BACKENDS copy.
    assert.doesNotMatch(src, /\[\s*["']claude["']\s*,/, `${name} hardcodes a backend id list`);
  }
});

// 2. requires_backend flips disabled + reason; false -> enabled, empty reason.
test("options carry disabled/reason straight from requires_backend and disabled_reason", () => {
  const view = modelsPanelView(payload());
  const coder = view.rows.find((r) => r.role === "coder");
  const claudeOpt = coder.options.find((o) => o.id === "claude-sonnet-5");
  const gptOpt = coder.options.find((o) => o.id === "gpt-5-codex");
  assert.equal(claudeOpt.disabled, false);
  assert.equal(claudeOpt.reason, "");
  assert.equal(gptOpt.disabled, true);
  assert.equal(gptOpt.reason, DISABLED_REASON);
});

// 3. Pinned-role rows surface VENDOR_PIN_NOTE verbatim as row.note; coder "".
test("pinned-role rows carry the server's note verbatim; the coder row does not", () => {
  const view = modelsPanelView(payload());
  const byRole = Object.fromEntries(view.rows.map((r) => [r.role, r]));
  assert.equal(byRole.coder.note, "");
  for (const role of ["reviewer", "planner", "supervisor", "utility"]) {
    assert.equal(byRole[role].note, VENDOR_PIN_NOTE);
  }
});

// 4. pendingBody returns only edited keys; a selection equal to current is
// absent even if present in `pending`.
test("pendingBody sends only keys whose pending value differs from current", () => {
  const p = payload();
  const pending = {
    review_model: "claude-opus-5", // changed
    primary_model: "claude-sonnet-5", // same as current -> must be dropped
  };
  assert.deepEqual(pendingBody(p, pending), { review_model: "claude-opus-5" });
});

test("pendingBody is empty for an empty or missing pending map", () => {
  const p = payload();
  assert.deepEqual(pendingBody(p, {}), {});
  assert.deepEqual(pendingBody(p, undefined), {});
  assert.deepEqual(pendingBody(null, { review_model: "x" }), {});
});

// 5. resetBody equals the fixture's default values, read from the fixture.
test("resetBody equals the payload's own defaults for every role that has drifted", () => {
  const p = payload();
  p.roles[1].current = "claude-opus-5"; // reviewer drifted from its default
  const expected = {};
  for (const r of p.roles) {
    if (r.default !== r.current) expected[r.key] = r.default;
  }
  assert.deepEqual(resetBody(p), expected);
  assert.deepEqual(resetBody(p), { review_model: p.roles[1].default });
});

test("resetBody is empty when every role already sits at its default", () => {
  assert.deepEqual(resetBody(payload()), {});
});

// 6. applyError reverts everything and surfaces the server text verbatim.
test("applyError clears all pending edits and returns the server detail verbatim", () => {
  const detail = "'gpt-5.4' has no published price; refusing to run an unpriced model.";
  const result = applyError({ review_model: "gpt-5.4", planner_model: "claude-opus-5" }, detail);
  assert.deepEqual(result.pending, {});
  assert.equal(result.error, detail);
});

// 7. showRestartBanner follows payload.restart_required both directions, and
// is never inferred from current !== default.
test("showRestartBanner follows restart_required, not any current/default mismatch", () => {
  const p1 = payload({ restart_required: true });
  assert.equal(modelsPanelView(p1).showRestartBanner, true);

  const p2 = payload({ restart_required: false });
  assert.equal(modelsPanelView(p2).showRestartBanner, false);

  // Drift present, restart_required false -> still no banner.
  const p3 = payload({ restart_required: false });
  p3.roles[1].current = "claude-opus-5";
  assert.equal(modelsPanelView(p3).showRestartBanner, false);
});

// 8. Reviewer row costNote equals roles[i].cost_note; absent -> ""; no other
// row carries one.
test("only the reviewer row carries a costNote, copied verbatim from cost_note", () => {
  const view = modelsPanelView(payload());
  const byRole = Object.fromEntries(view.rows.map((r) => [r.role, r]));
  assert.equal(byRole.reviewer.costNote, COST_NOTE);
  for (const role of ["coder", "planner", "supervisor", "utility"]) {
    assert.equal(byRole[role].costNote, "");
  }
});

test("costNote is empty when the payload omits cost_note entirely", () => {
  const p = payload();
  delete p.roles[1].cost_note;
  const view = modelsPanelView(p);
  assert.equal(view.rows.find((r) => r.role === "reviewer").costNote, "");
});

// 9. modelsPanelView(null) / {} -> {rows: [], unavailable: true}.
test("a missing or empty payload is reported unavailable with no rows", () => {
  assert.deepEqual(modelsPanelView(null), { unavailable: true, showRestartBanner: false, rows: [] });
  assert.deepEqual(modelsPanelView({}), { unavailable: true, showRestartBanner: false, rows: [] });
  assert.deepEqual(modelsPanelView({ roles: [] }), { unavailable: true, showRestartBanner: false, rows: [] });
});

// --- Constraint §6d: role_backends (reviewer only) --- //

// 10. Every non-reviewer row's `backend` is null (the fixture never sets it);
// the reviewer row is also null when the payload omits the field entirely —
// an older server, or a role outside ROLE_BACKEND_ROLES, must render exactly
// as it did before this constraint landed.
test("row.backend is null for every role when the payload carries no backend block", () => {
  const view = modelsPanelView(payload());
  for (const row of view.rows) {
    assert.equal(row.backend, null, `${row.role} row.backend should be null`);
  }
});

// 11. When the server DOES send a reviewer backend block, it is copied
// verbatim onto row.backend (camelCased isDefault, everything else as-is) —
// never re-derived, and no other row is affected by its presence.
test("the reviewer row copies a present backend block verbatim onto row.backend", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  const view = modelsPanelView(p);
  const byRole = Object.fromEntries(view.rows.map((r) => [r.role, r]));
  assert.deepEqual(byRole.reviewer.backend, { backend: "codex", model: "gpt-5-codex", isDefault: false });
  for (const role of ["coder", "planner", "supervisor", "utility"]) {
    assert.equal(byRole[role].backend, null);
  }
});

test("a reviewer backend block on the server's own default is copied with isDefault true", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const view = modelsPanelView(p);
  const reviewer = view.rows.find((r) => r.role === "reviewer");
  assert.deepEqual(reviewer.backend, { backend: "claude", model: "claude-opus-4-8", isDefault: true });
});

// 12. pendingBody's third argument, pendingRoleBackend, is a three-state
// contract independent of the five model-id keys handled by `pending`.
test("pendingBody omits role_backends entirely when pendingRoleBackend is omitted (undefined)", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  // No third argument at all -> old 2-arg call shape, byte-identical result.
  assert.deepEqual(pendingBody(p, {}), {});
  assert.equal("role_backends" in pendingBody(p, {}), false);
});

test("pendingBody sends a clearing role_backends when pendingRoleBackend is null and the reviewer is not already default", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  assert.deepEqual(pendingBody(p, {}, null), { role_backends: { reviewer: null } });
});

test("pendingBody sends nothing for pendingRoleBackend null when the reviewer is already default (no-op)", () => {
  const p = payload();
  // No backend block at all (never explicitly chosen) -> already default.
  assert.deepEqual(pendingBody(p, {}, null), {});

  const p2 = payload();
  p2.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  assert.deepEqual(pendingBody(p2, {}, null), {});
});

test("pendingBody sends an explicit role_backends choice when it differs from the current effective backend", () => {
  const p = payload();
  // No explicit choice yet -> currently default.
  assert.deepEqual(
    pendingBody(p, {}, { backend: "codex", model: "gpt-5-codex" }),
    { role_backends: { reviewer: { backend: "codex", model: "gpt-5-codex" } } },
  );
});

test("pendingBody sends nothing when the explicit choice matches the current explicit choice exactly (no-op)", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  assert.deepEqual(pendingBody(p, {}, { backend: "codex", model: "gpt-5-codex" }), {});
});

test("pendingBody combines a role_backends edit with ordinary model-id edits in one body", () => {
  const p = payload();
  const pending = { planner_model: "claude-opus-5" }; // same as current -> dropped
  const pending2 = { primary_model: "gpt-5-codex" }; // changed
  assert.deepEqual(
    pendingBody(p, pending, { backend: "codex", model: "gpt-5-codex" }),
    { role_backends: { reviewer: { backend: "codex", model: "gpt-5-codex" } } },
  );
  assert.deepEqual(
    pendingBody(p, pending2, { backend: "codex", model: "gpt-5-codex" }),
    { primary_model: "gpt-5-codex", role_backends: { reviewer: { backend: "codex", model: "gpt-5-codex" } } },
  );
});

// 13. resetBody only clears the reviewer's explicit backend when one is
// actually set (is_default === false); a payload with no backend block, or
// one already at is_default true, must not add the key at all.
test("resetBody adds a clearing role_backends only when the reviewer is on an explicit non-default backend", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  assert.deepEqual(resetBody(p), { role_backends: { reviewer: null } });
});

test("resetBody adds no role_backends key when the reviewer has no backend block at all", () => {
  assert.deepEqual(resetBody(payload()), {});
});

test("resetBody adds no role_backends key when the reviewer's backend block is already the default", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  assert.deepEqual(resetBody(p), {});
});

test("resetBody combines a role_backends clear with an ordinary drifted model-id reset in one body", () => {
  const p = payload();
  p.roles[1].current = "claude-opus-5"; // review_model drifted from its default
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  assert.deepEqual(resetBody(p), {
    review_model: p.roles[1].default,
    role_backends: { reviewer: null },
  });
});

// 14. B6 end-to-end shape: the exact GET /api/models response the server
// sends right after a role_backends PUT succeeds — `is_default: false` on
// the reviewer row's backend block (the write already landed, read back
// from disk per B6) alongside `restart_required: true` (the choice will not
// take effect until the orchestrator's next task) in the SAME payload. Two
// independently-tested fields (row.backend.isDefault, showRestartBanner)
// must both read correctly off one real post-save payload, and the Reset
// control born from that exact payload must clear it — proving the UI's
// clear path is live without a restart, not just each field in isolation.
test("a post-save payload (saved backend, restart pending) shows the choice and clears it correctly", () => {
  const p = payload({ restart_required: true });
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };

  const view = modelsPanelView(p);
  assert.equal(view.showRestartBanner, true);
  const reviewer = view.rows.find((r) => r.role === "reviewer");
  assert.deepEqual(reviewer.backend, { backend: "codex", model: "gpt-5-codex", isDefault: false });

  // The clear control (Reset) built from this exact payload must send a
  // clearing role_backends — dead until a restart would mean this stays
  // `{}` even though the row plainly shows a non-default backend.
  assert.deepEqual(resetBody(p), { role_backends: { reviewer: null } });

  // Same proof for the picker's own "back to default" pick (pendingBody),
  // independent of Reset's whole-panel path.
  assert.deepEqual(pendingBody(p, {}, null), { role_backends: { reviewer: null } });
});

// 15. pendingBody's null-clear path is idempotent: sending the same
// already-cleared (is_default: true) payload back through it again must
// keep producing an empty body, not re-emit the clear forever.
test("pendingBody(payload, {}, null) is idempotent once the reviewer is already back at the default", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const first = pendingBody(p, {}, null);
  assert.deepEqual(first, {});
  const second = pendingBody(p, {}, null);
  assert.deepEqual(second, first);
});

// --- 6d part 3: reviewerBackendView (saved/pending/selected derivation) --- //

test("reviewerBackendView returns null when the reviewer row carries no backend block", () => {
  assert.equal(reviewerBackendView(payload(), undefined, []), null);
  assert.equal(reviewerBackendView(payload(), null, []), null);
  assert.equal(reviewerBackendView(null, undefined, []), null);
});

test("reviewerBackendView reports saved.isDefault and defaultModel when the reviewer is unset (on default)", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const view = reviewerBackendView(p, undefined, []);
  assert.deepEqual(view.saved, { backend: "claude", model: "claude-opus-4-8", isDefault: true });
  assert.equal(view.defaultModel, p.roles[1].default);
  assert.deepEqual(view.selected, view.saved);
  assert.equal(view.unsaved, false);
});

test("reviewerBackendView reports the chosen backend/model when the reviewer has an explicit choice", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  const view = reviewerBackendView(p, undefined, []);
  assert.deepEqual(view.saved, { backend: "codex", model: "gpt-5-codex", isDefault: false });
  assert.deepEqual(view.selected, view.saved);
});

test("reviewerBackendView.unsaved is false for an omitted (undefined) pending pick", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  assert.equal(reviewerBackendView(p, undefined, []).unsaved, false);
});

test("reviewerBackendView.unsaved is true for a pending pick that differs from the saved state", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const view = reviewerBackendView(p, { backend: "codex", model: "gpt-5-codex" }, []);
  assert.equal(view.unsaved, true);
  assert.deepEqual(view.selected, { backend: "codex", model: "gpt-5-codex", isDefault: false });
});

test("reviewerBackendView.unsaved is false for a pending pick identical to the saved one", () => {
  const p = payload();
  p.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  const view = reviewerBackendView(p, { backend: "codex", model: "gpt-5-codex" }, []);
  assert.equal(view.unsaved, false);
});

test("reviewerBackendView.unsaved is true for a pending null (clear) only when currently non-default", () => {
  const nonDefault = payload();
  nonDefault.roles[1].backend = { backend: "codex", model: "gpt-5-codex", is_default: false };
  assert.equal(reviewerBackendView(nonDefault, null, []).unsaved, true);

  const alreadyDefault = payload();
  alreadyDefault.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  assert.equal(reviewerBackendView(alreadyDefault, null, []).unsaved, false);
});

test("reviewerBackendView.submittable is false for a blank (or whitespace-only) model", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const view = reviewerBackendView(p, { backend: "codex", model: "   " }, []);
  assert.equal(view.submittable, false);
});

test("reviewerBackendView.submittable is false when the selected backend option is disabled", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const options = [{ id: "codex", disabled: true, reason: "not configured" }];
  const view = reviewerBackendView(p, { backend: "codex", model: "gpt-5-codex" }, options);
  assert.equal(view.submittable, false);
});

test("reviewerBackendView.submittable is true for an available backend with a non-blank model", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  const options = [{ id: "codex", disabled: false }];
  const view = reviewerBackendView(p, { backend: "codex", model: "gpt-5-codex" }, options);
  assert.equal(view.submittable, true);
});

test("reviewerBackendView.submittable is always true when selected is on default, regardless of options", () => {
  const p = payload();
  p.roles[1].backend = { backend: "claude", model: "claude-opus-4-8", is_default: true };
  assert.equal(reviewerBackendView(p, undefined, []).submittable, true);
  assert.equal(reviewerBackendView(p, null, []).submittable, true);
});

// --- 6d part 3: ModelsPanel.jsx source pins (wiring, not behaviour) --- //

test("ModelsPanel.jsx imports and calls reviewerBackendView, server-derived options only", () => {
  const src = readFileSync(new URL("./ModelsPanel.jsx", import.meta.url), "utf8");
  assert.match(src, /import\s*\{[^}]*reviewerBackendView[^}]*\}\s*from\s*["']\.\/modelsPanelView\.js["']/,
    "must import reviewerBackendView from modelsPanelView.js");
  assert.match(src, /reviewerBackendView\(/, "must call reviewerBackendView");
  assert.match(src, /backendOptions\.map\(/, "the option list must be rendered from backendOptions");
  assert.match(src, /<option value="">/, "a clear-to-default option must be present");
  assert.match(src, /data-testid="models-save"/, "the panel's Save button needs an unambiguous selector");
  assert.match(src, /data-testid="reviewer-backend-pending"/, "the unsaved-pick line needs its own hook");
});

test("the reviewer backend override option shows '(unavailable)', not the raw reason paragraph", () => {
  const src = readFileSync(new URL("./ModelsPanel.jsx", import.meta.url), "utf8");
  const start = src.indexOf("Reviewer backend override");
  const end = src.indexOf("Reviewer model");
  assert.ok(start >= 0 && end > start, "must find the reviewer-backend-override select block");
  const block = src.slice(start, end);
  assert.ok(!block.includes("${o.reason}"), "the raw multi-sentence reason must not be interpolated into the option text");
  assert.match(block, /\{o\.label\}\{o\.disabled \? " \(unavailable\)" : ""\}/);
  assert.match(block, /title=\{o\.reason \|\| undefined\}/);
  assert.match(block, /className="ntm-hint"[\s\S]*\{o\.short\}/);
  assert.match(block, /\.filter\(\(o\) => o\.disabled && o\.short\)/);
});

test("ModelsPanel.jsx's Save button still goes through pendingBody, not a second hand-rolled body", () => {
  const src = readFileSync(new URL("./ModelsPanel.jsx", import.meta.url), "utf8");
  assert.match(src, /const dirty = pendingBody\(payload, pending, pendingRoleBackend\)/);
  assert.match(src, /commit\(dirty\)/, "Save must submit the pendingBody-derived body");
});
