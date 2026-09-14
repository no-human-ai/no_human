import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  REPLAY_EXCLUDED_PATHS,
  REPLAY_BODY_ALLOWLIST,
  API_BODY_CLASSIFICATION,
  maskCapturedNetworkRequest,
} from "./replayScrub.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");

test("REPLAY_EXCLUDED_PATHS names the address-carrying endpoints", () => {
  assert.ok(REPLAY_EXCLUDED_PATHS.includes("/api/onboarding/email"));
  assert.ok(REPLAY_EXCLUDED_PATHS.includes("/api/onboarding/status"));
  assert.ok(REPLAY_EXCLUDED_PATHS.includes("/api/onboarding/reset"));
});

test("drops the onboarding email request in relative/absolute/query/trailing-slash forms", () => {
  const shapes = [
    { name: "/api/onboarding/email" },
    { url: "/api/onboarding/email" },
    { name: "http://localhost:8420/api/onboarding/email" },
    { name: "https://board.local/api/onboarding/email?x=1" },
    { name: "/api/onboarding/email/" },
    { name: "/api/onboarding/email?foo=bar&baz=qux" },
  ];
  for (const data of shapes) {
    assert.equal(maskCapturedNetworkRequest(data), null, JSON.stringify(data));
  }
});

test("drops the onboarding status request/response in the same forms", () => {
  const shapes = [
    { name: "/api/onboarding/status" },
    { name: "http://localhost:8420/api/onboarding/status" },
    { name: "/api/onboarding/status/" },
    { name: "/api/onboarding/status?ts=123" },
  ];
  for (const data of shapes) {
    assert.equal(maskCapturedNetworkRequest(data), null, JSON.stringify(data));
  }
});

test("drops the onboarding reset request/response in the same forms", () => {
  const shapes = [
    { name: "/api/onboarding/reset" },
    { name: "http://localhost:8420/api/onboarding/reset" },
    { name: "/api/onboarding/reset/" },
    { name: "/api/onboarding/reset?ts=123" },
  ];
  for (const data of shapes) {
    assert.equal(maskCapturedNetworkRequest(data), null, JSON.stringify(data));
  }
});

// Was "passes unrelated endpoints through by identity" — that was the fail-OPEN
// bug: everything not explicitly excluded passed straight through, including
// bodies nobody had ever looked at. The fix is default-deny, so the very same
// endpoints now keep their request line (so replay is still useful for
// debugging network activity) but lose their bodies/headers unless they are
// individually allowlisted.
test("unrelated endpoints keep their request line but lose their bodies", () => {
  for (const raw of ["/api/tasks", "/api/config", "/api/onboarding/complete", "/api/onboarding/repos/onboard"]) {
    const data = { name: raw, method: "GET", status: 200, requestBody: "secret-in", responseBody: "secret-out", requestHeaders: { Cookie: "x" }, responseHeaders: { "Set-Cookie": "y" } };
    const out = maskCapturedNetworkRequest(data);
    assert.notEqual(out, data, raw); // not identity anymore
    assert.equal(out.name, raw, raw); // request line preserved
    assert.equal(out.method, "GET", raw);
    assert.equal(out.status, 200, raw);
    assert.notEqual(out.requestBody, "secret-in", raw);
    assert.notEqual(out.responseBody, "secret-out", raw);
    assert.deepEqual(out.requestHeaders, {}, raw);
    assert.deepEqual(out.responseHeaders, {}, raw);
    // original object must not be mutated
    assert.equal(data.requestBody, "secret-in", raw);
    assert.equal(data.responseBody, "secret-out", raw);
  }
});

test("the three allowlisted endpoints pass through completely unchanged", () => {
  for (const raw of REPLAY_BODY_ALLOWLIST) {
    const data = { name: raw, requestBody: "in", responseBody: "out", requestHeaders: { a: 1 }, responseHeaders: { b: 2 } };
    assert.equal(maskCapturedNetworkRequest(data), data, raw);
  }
});

test("default-deny: an endpoint nobody has classified yet is redacted, not passed through", () => {
  const data = { name: "/api/zzqq-future-endpoint-nobody-has-seen", responseBody: "some-repo-path-maybe" };
  const out = maskCapturedNetworkRequest(data);
  assert.notEqual(out, data);
  assert.notEqual(out.responseBody, "some-repo-path-maybe");
  assert.equal(out.name, "/api/zzqq-future-endpoint-nobody-has-seen");
});

test("regression: /api/profiles (name + absolute repo_path for every repo) is redacted", () => {
  const data = {
    name: "http://127.0.0.1:8420/api/profiles",
    responseBody: JSON.stringify([{ name: "my-secret-repo", repo_path: "/Users/eyal/code/my-secret-repo" }]),
  };
  const out = maskCapturedNetworkRequest(data);
  assert.notEqual(out, data);
  assert.ok(!String(out.responseBody).includes("my-secret-repo"));
  assert.ok(!String(out.responseBody).includes("/Users/eyal"));
});

test("query strings carrying paths are stripped from the request line, not just the body", () => {
  const data = { name: "/api/repo?path=%2FUsers%2Feyal%2Fcode%2Fmy-secret-repo", responseBody: "irrelevant" };
  const out = maskCapturedNetworkRequest(data);
  assert.ok(!String(out.name).includes("my-secret-repo"));
  assert.ok(!String(out.name).includes("path="));
  assert.equal(out.name, "/api/repo");
});

test("an unparseable URL never qualifies for the Tier-2 allowlist", () => {
  // Crafted so a naive substring/prefix check against an allowlisted path
  // would wrongly pass it through unchanged.
  const data = { name: "::not a url:: /api/version but not really", responseBody: "secret" };
  const out = maskCapturedNetworkRequest(data);
  assert.notEqual(out, data);
  assert.notEqual(out.responseBody, "secret");
});

test("a frozen/sealed data object is never mutated and fails closed or redacts safely", () => {
  const frozen = Object.freeze({ name: "/api/tasks", responseBody: "secret", requestHeaders: Object.freeze({}) });
  assert.doesNotThrow(() => maskCapturedNetworkRequest(frozen));
  const out = maskCapturedNetworkRequest(frozen);
  // Either dropped (null) or redacted into a NEW object — either way the
  // frozen input itself must be untouched (freezing would throw on mutation
  // if the implementation ever tried).
  assert.equal(frozen.responseBody, "secret");
  if (out !== null) {
    assert.notEqual(out.responseBody, "secret");
  }
});

test("never throws on undefined/null/unparseable input, and fails CLOSED on anything matching-but-odd", () => {
  assert.doesNotThrow(() => maskCapturedNetworkRequest(undefined));
  assert.doesNotThrow(() => maskCapturedNetworkRequest(null));
  assert.doesNotThrow(() => maskCapturedNetworkRequest({}));
  assert.equal(maskCapturedNetworkRequest(undefined), undefined);
  assert.equal(maskCapturedNetworkRequest(null), null);
  // Malformed but still matching by substring: not a parseable URL at all,
  // yet contains the excluded path — must fail closed (excluded), not throw
  // and not pass through.
  const weird = { name: "::not a url:: /api/onboarding/email" };
  assert.equal(maskCapturedNetworkRequest(weird), null);
  // A hostile getter that throws must still fail closed.
  const hostile = {
    get name() {
      throw new Error("boom");
    },
  };
  assert.equal(maskCapturedNetworkRequest(hostile), null);
});

// ---------------------------------------------------------------------------
// Source sweep: every `/api/*` pathname api.js actually calls must have a
// classification entry, so adding a new endpoint without deciding where it
// sits fails this test instead of silently defaulting to "captured in full"
// the way the old fail-open version did. This is a "keep the map honest as
// code evolves" test in the same spirit as telemetry.test.mjs's capture-event
// disclosure sweep — it re-derives the endpoint list from source on every run
// rather than trusting a hand-maintained copy.
// ---------------------------------------------------------------------------

// Finds the raw contents of a template literal that starts right after the
// backtick at `start`, correctly skipping any backtick-delimited template
// nested inside a `${...}` interpolation (e.g. `${q ? `?${q}` : ""}`) so it
// doesn't mistake the nested literal's closing backtick for the outer one.
function findTemplateBody(src, start) {
  let i = start;
  let depth = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "\\") {
      i += 2;
      continue;
    }
    if (c === "`" && depth === 0) return src.slice(start, i);
    if (c === "$" && src[i + 1] === "{") {
      depth++;
      i += 2;
      continue;
    }
    if (c === "}" && depth > 0) {
      depth--;
      i++;
      continue;
    }
    if (c === "`" && depth > 0) {
      i++;
      while (i < src.length && src[i] !== "`") {
        if (src[i] === "\\") i += 2;
        else i++;
      }
      i++;
      continue;
    }
    i++;
  }
  return src.slice(start);
}

// Normalizes a raw literal body (already stripped of a leading `${BASE}`) to
// a stable pathname key: interpolated path segments become `:param`, and a
// trailing interpolation that only ever builds a query string (contains a
// literal "?" and has nothing static after it) is dropped along with any
// literal "?..." suffix, matching how the server sees the path.
function normalizeApiPath(raw) {
  const body = raw.replace(/^\$\{BASE\}/, "");
  let out = "";
  let i = 0;
  while (i < body.length) {
    const ch = body[i];
    if (ch === "$" && body[i + 1] === "{") {
      let depth = 1;
      let j = i + 2;
      while (j < body.length && depth > 0) {
        const c = body[j];
        if (c === "`") {
          j++;
          while (j < body.length && body[j] !== "`") j++;
          j++;
          continue;
        }
        if (c === "{") depth++;
        else if (c === "}") depth--;
        j++;
      }
      const inner = body.slice(i + 2, j - 1);
      if (j === body.length && inner.includes("?")) {
        i = j;
        break;
      }
      out += ":param";
      i = j;
      continue;
    }
    if (ch === "?") break;
    out += ch;
    i++;
  }
  return out;
}

function sweepApiEndpoints() {
  const src = fs.readFileSync(path.join(REPO_ROOT, "web", "src", "api.js"), "utf8");
  const found = new Set();
  const openRe = /`(\$\{BASE\})?\/api\//g;
  let m;
  while ((m = openRe.exec(src))) {
    const contentStart = m.index + 1; // right after the opening backtick
    const body = findTemplateBody(src, contentStart);
    found.add(normalizeApiPath(body));
  }
  const quotedRe = /"(\/api\/[^"]*)"/g;
  while ((m = quotedRe.exec(src))) found.add(normalizeApiPath(m[1]));
  return found;
}

test("every /api/* endpoint api.js calls has a classification entry", () => {
  const endpoints = sweepApiEndpoints();
  assert.ok(endpoints.size > 0, "sweep found nothing — regex is broken, not that api.js has no endpoints");
  const missing = [...endpoints].filter((p) => !(p in API_BODY_CLASSIFICATION));
  assert.deepEqual(missing, [], `unclassified endpoint(s) found in api.js — add each to API_BODY_CLASSIFICATION in replayScrub.js: ${missing.join(", ")}`);
});

test("no stale classification entries for endpoints api.js no longer calls (drop-tier entries exempt: defence-in-depth)", () => {
  const endpoints = sweepApiEndpoints();
  const stale = Object.entries(API_BODY_CLASSIFICATION)
    .filter(([p, v]) => v.tier !== "drop" && !endpoints.has(p))
    .map(([p]) => p);
  assert.deepEqual(stale, [], `classification entries with no live call site in api.js: ${stale.join(", ")}`);
});

test("every classification entry has a non-empty why, and a valid tier", () => {
  for (const [p, v] of Object.entries(API_BODY_CLASSIFICATION)) {
    assert.ok(["drop", "allow", "redact"].includes(v.tier), `${p}: bad tier ${v.tier}`);
    assert.ok(typeof v.why === "string" && v.why.length > 10, `${p}: missing/short why`);
  }
});

test("REPLAY_BODY_ALLOWLIST and REPLAY_EXCLUDED_PATHS agree with API_BODY_CLASSIFICATION's tiers", () => {
  for (const p of REPLAY_BODY_ALLOWLIST) {
    assert.equal(API_BODY_CLASSIFICATION[p]?.tier, "allow", `${p} is on REPLAY_BODY_ALLOWLIST but not tier "allow" in the classification map`);
  }
  for (const p of REPLAY_EXCLUDED_PATHS) {
    assert.equal(API_BODY_CLASSIFICATION[p]?.tier, "drop", `${p} is on REPLAY_EXCLUDED_PATHS but not tier "drop" in the classification map`);
  }
  const allowTierPaths = Object.entries(API_BODY_CLASSIFICATION).filter(([, v]) => v.tier === "allow").map(([p]) => p);
  assert.deepEqual([...allowTierPaths].sort(), [...REPLAY_BODY_ALLOWLIST].sort());
  const dropTierPaths = Object.entries(API_BODY_CLASSIFICATION).filter(([, v]) => v.tier === "drop").map(([p]) => p);
  assert.deepEqual([...dropTierPaths].sort(), [...REPLAY_EXCLUDED_PATHS].sort());
});

test("docs/configuration.md documents the historical-recordings gap as open follow-up work", () => {
  const doc = fs.readFileSync(path.join(REPO_ROOT, "docs", "configuration.md"), "utf8");
  assert.match(doc, /Historical recordings/i);
});
