// The mutating calls in api.js, exercised for real against a stubbed `fetch`.
//
// These assert on the Error a CALLER receives, not on the text of api.js. That
// distinction is the whole point: a regex-over-source guard ("does the file
// mention detailMessage?") passes while the call is wired to the wrong body,
// and this repo has already paid nine review rounds for exactly that pattern.
// Here the module is imported, its real function is called, and the thrown
// message is read — the same string that lands in the drawer's flash banner.
//
// Guarded defect: five mutating POSTs threw a BARE status code and discarded
// the server's `detail`. `sendBack` was the worst of them — api/app.py's
// send_back answers 409 with "task is already done" / "task is cancelled",
// the only explanation of why the operator's click did nothing, and the UI
// showed "POST send-back → 409".
import test from "node:test";
import assert from "node:assert/strict";
import {
  uploadAttachment, finishReview, replyTask, chooseBlockerOption, sendBack,
  approveTask, createTask, fetchIntegrations, fetchModels, saveModels,
  fetchWindowSpend,
} from "./api.js";

/** Make the next fetch answer with `status` and the given JSON body. */
function stubFetch({ status = 409, body = {}, json = true } = {}) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts?.method });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        if (!json) throw new SyntaxError("Unexpected token < in JSON");
        return body;
      },
    };
  };
  return calls;
}

const original = globalThis.fetch;
test.after(() => { globalThis.fetch = original; });

// One row per mutating call: [name, invoke, the server's 409 reason].
const MUTATORS = [
  ["sendBack", () => sendBack("t1", "redo it"), "task is already done"],
  ["replyTask", () => replyTask("t1", "an answer"), "task is not awaiting input"],
  ["chooseBlockerOption", () => chooseBlockerOption("t1", 2), "no such option"],
  ["finishReview", () => finishReview("t1"), "task is not under review"],
  ["uploadAttachment", () => uploadAttachment("t1", { name: "x.png" }), "attachment too large"],
  // Already correct before this sweep — pinned so the group cannot regress
  // back to a bare status code one function at a time.
  ["approveTask", () => approveTask("t1"), "task is 'done', not awaiting_approval"],
  ["createTask", () => createTask({ title: "t" }), "repo_path does not exist"],
];

for (const [name, invoke, reason] of MUTATORS) {
  test(`${name} surfaces the server's reason, not just the status code`, async () => {
    stubFetch({ status: 409, body: { detail: reason } });
    const err = await invoke().then(
      () => null,
      (e) => e,
    );
    assert.ok(err instanceof Error, `${name} must reject on a non-2xx`);
    assert.ok(
      err.message.includes(reason),
      `${name} threw "${err.message}" — the operator never sees "${reason}"`,
    );
  });

  test(`${name} names the offending field on a 422, never [object Object]`, async () => {
    stubFetch({
      status: 422,
      body: { detail: [{ type: "missing", loc: ["body", "message"], msg: "Field required" }] },
    });
    const err = await invoke().then(() => null, (e) => e);
    assert.doesNotMatch(err.message, /\[object Object\]/,
      `${name} stringified the 422 list: "${err.message}"`);
    assert.match(err.message, /message/);
    assert.match(err.message, /Field required/);
  });

  test(`${name} still reports something when the body is not JSON at all`, async () => {
    // The SPA catch-all can answer HTML; `.json()` throws and the fallback —
    // which is where the status code still belongs — has to survive.
    stubFetch({ status: 502, json: false });
    const err = await invoke().then(() => null, (e) => e);
    assert.ok(err instanceof Error);
    assert.match(err.message, /502/, `no status in the fallback: "${err.message}"`);
  });
}

test("a successful mutating call resolves with the server's JSON, not an error", async () => {
  stubFetch({ status: 200, body: { ok: true, message: "Feedback stored." } });
  assert.deepEqual(await sendBack("t1", "redo"), { ok: true, message: "Feedback stored." });
});


// ── fetchIntegrations: "I could not ask" is not "the answer is none" ───────
//
// It used to fold EVERY non-ok response into `{integrations: []}`, which is
// byte-identical to a healthy server reporting nothing configured. The Backlog
// page reads that registry to choose between "not connected, here is how" and
// "the tracker is down", so a 500 rendered "Jira is not configured" and sent
// the operator to fix a token that was never the problem.
//
// Asserted on the value a CALLER receives, not on the text of api.js — the
// source-text version of this guard passes against the old swallowing code as
// long as the word `throw` appears anywhere in the function.

test("fetchIntegrations REJECTS on a failed request rather than reporting an empty registry", async () => {
  stubFetch({ status: 500, body: { detail: "database is locked" } });
  const err = await fetchIntegrations().then((v) => v, (e) => e);
  assert.ok(err instanceof Error,
    `a 500 must not resolve — it resolved with ${JSON.stringify(err)}`);
  assert.match(err.message, /database is locked/, "the server's own reason must survive");
});

test("fetchIntegrations rejects on 503 too, with the status when there is no detail", async () => {
  stubFetch({ status: 503, body: {} });
  const err = await fetchIntegrations().then((v) => v, (e) => e);
  assert.ok(err instanceof Error);
  assert.match(err.message, /503/);
});

test("fetchIntegrations rejects when the request never got an answer at all", async () => {
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  const err = await fetchIntegrations().then((v) => v, (e) => e);
  assert.ok(err instanceof Error);
  assert.match(err.message, /did not answer/);
});

test("fetchIntegrations resolves with the registry on success", async () => {
  const registry = { integrations: [{ name: "jira", configured: true }] };
  // The success path runs through _jsonSafe, which reads the content type (the
  // SPA catch-all can answer HTML at 200) — so this stub carries headers.
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    headers: { get: () => "application/json" },
    json: async () => registry,
  });
  assert.deepEqual(await fetchIntegrations(), registry);
});

test("fetchIntegrations does not mistake an HTML 200 from the SPA catch-all for a registry", async () => {
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    headers: { get: () => "text/html" },
    json: async () => { throw new SyntaxError("Unexpected token <"); },
  });
  assert.deepEqual(await fetchIntegrations(), { integrations: [] });
});

// ── fetchModels / saveModels (Settings → Models pane) ───────────────────────

test("fetchModels resolves with the payload on a healthy 200", async () => {
  const payload = { roles: [{ role: "coder", key: "primary_model" }], restart_required: false };
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => payload });
  assert.deepEqual(await fetchModels(), payload);
});

test("fetchModels returns null on a non-ok response, never throws", async () => {
  globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({}) });
  assert.equal(await fetchModels(), null);
});

test("fetchModels returns null when the request never got an answer at all", async () => {
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  assert.equal(await fetchModels(), null);
});

test("fetchModels returns null on an HTML 200 from the SPA catch-all (older server build)", async () => {
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    json: async () => { throw new SyntaxError("Unexpected token < in JSON"); },
  });
  assert.equal(await fetchModels(), null);
});

test("saveModels PUTs exactly the given body to /api/config/models", async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts?.method, body: JSON.parse(opts.body) });
    return { ok: true, status: 200, json: async () => ({ roles: [], restart_required: true }) };
  };
  await saveModels({ review_model: "claude-opus-5" });
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/api\/config\/models$/);
  assert.equal(calls[0].method, "PUT");
  assert.deepEqual(calls[0].body, { review_model: "claude-opus-5" });
});

test("saveModels throws the server's 422 detail verbatim, and nothing else", async () => {
  const detail = "'gpt-5.4' is not a Claude model. This role always runs on the Claude backend.";
  stubFetch({ status: 422, body: { detail } });
  const err = await saveModels({ review_model: "gpt-5.4" }).then(() => null, (e) => e);
  assert.ok(err instanceof Error);
  assert.equal(err.message, detail);
});


// Feature #1: the split's two calls, exercised for real against a stubbed fetch.
import { fetchSplitDrafts, splitTask } from "./api.js";

test("fetchSplitDrafts GETs the drafts endpoint and returns the drafts", async () => {
  const calls = stubFetch({ status: 200, body: { drafts: [{ title: "A" }, { title: "B" }] } });
  const out = await fetchSplitDrafts("t9");
  assert.equal(calls[0].url, "/api/tasks/t9/split-drafts");
  assert.deepEqual(out.drafts.map((d) => d.title), ["A", "B"]);
});

test("splitTask POSTs the confirmed drafts and returns the children", async () => {
  let sentBody;
  globalThis.fetch = async (url, opts) => {
    sentBody = JSON.parse(opts.body);
    assert.equal(String(url), "/api/tasks/t9/split");
    assert.equal(opts.method, "POST");
    return { ok: true, status: 201, json: async () => [{ id: "c1" }, { id: "c2" }] };
  };
  const children = await splitTask("t9", [{ title: "A" }, { title: "B" }]);
  assert.deepEqual(sentBody, { drafts: [{ title: "A" }, { title: "B" }] });
  assert.deepEqual(children.map((c) => c.id), ["c1", "c2"]);
});

// Onboarding-funnel telemetry calls, exercised for real against a stubbed
// fetch — previously ZERO coverage. Both are fire-and-forget from the
// wizard's point of view (Onboarding.jsx catches and swallows any
// rejection), but the binding itself — the URL, method, and body it sends —
// is exactly what determines whether a step is ever recorded at all, so it
// is worth pinning like every other mutating call above.
import { recordOnboardingStep, verifyAuthLive } from "./api.js";

test("recordOnboardingStep POSTs the step key to /api/onboarding/step-viewed", async () => {
  let sentBody;
  globalThis.fetch = async (url, opts) => {
    sentBody = JSON.parse(opts.body);
    assert.equal(String(url), "/api/onboarding/step-viewed");
    assert.equal(opts.method, "POST");
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  await recordOnboardingStep("repos");
  assert.deepEqual(sentBody, { step: "repos" });
});

test("recordOnboardingStep surfaces the server's detail on failure, like every other mutator", async () => {
  stubFetch({ status: 422, body: { detail: "'repos' is not a valid step" } });
  const err = await recordOnboardingStep("bogus").then(() => null, (e) => e);
  assert.ok(err instanceof Error);
  assert.equal(err.message, "'repos' is not a valid step");
});

test("verifyAuthLive POSTs an empty body to /api/auth/verify and returns the closed result", async () => {
  let sentBody, sentUrl, sentMethod;
  globalThis.fetch = async (url, opts) => {
    sentUrl = String(url);
    sentMethod = opts?.method;
    sentBody = JSON.parse(opts.body);
    return { ok: true, status: 200, json: async () => ({ result: "valid" }) };
  };
  const out = await verifyAuthLive();
  assert.equal(sentUrl, "/api/auth/verify");
  assert.equal(sentMethod, "POST");
  assert.deepEqual(sentBody, {});
  assert.deepEqual(out, { result: "valid" });
});

test("verifyAuthLive rejects on a non-2xx rather than reporting a fabricated result", async () => {
  stubFetch({ status: 403, body: { detail: "local origin required" } });
  const err = await verifyAuthLive().then(() => null, (e) => e);
  assert.ok(err instanceof Error);
  assert.equal(err.message, "local origin required");
});

test("splitTask surfaces the server's 409 reason, not a bare status", async () => {
  stubFetch({ status: 409, body: { detail: "task is no longer pending — it started running or was already split" } });
  await assert.rejects(splitTask("t9", [{ title: "A" }, { title: "B" }]),
    /no longer pending/);
});


// P1 (running-task page slow-open): review_checklist/verifier_results/
// test_results moved off the inline detail payload onto a per-attempt lazy
// endpoint. `fetchTask` is the one call site (SlideOver.jsx) that has to
// paper over the split so the drawer's existing summary code — which reads
// `task.attempts[i].review_checklist` etc. across MULTIPLE attempts, not
// just the last one — keeps working unchanged.
import {
  fetchAttemptDetails, fetchTask,
  _attemptDetailsCacheSizeForTests, _clearAttemptDetailsCacheForTests,
} from "./api.js";

/** Route a stubbed fetch by exact URL rather than answering every call the
 * same way — `fetchTask` now issues N+1 requests (the task, plus one per
 * attempt), each of which must get its OWN body. */
function stubFetchByUrl(routes) {
  const calls = [];
  globalThis.fetch = async (url) => {
    const u = String(url);
    calls.push(u);
    const route = routes[u];
    if (!route) throw new Error(`unstubbed fetch: ${u}`);
    return {
      ok: route.status === undefined || (route.status >= 200 && route.status < 300),
      status: route.status ?? 200,
      json: async () => route.body,
    };
  };
  return calls;
}

test("fetchAttemptDetails GETs the per-attempt lazy endpoint", async () => {
  const calls = stubFetchByUrl({
    "/api/tasks/t1/attempts/2/details": {
      body: { attempt_number: 2, review_checklist: { passed: true }, verifier_results: [], test_results: null },
    },
  });
  const out = await fetchAttemptDetails("t1", 2);
  assert.deepEqual(calls, ["/api/tasks/t1/attempts/2/details"]);
  assert.equal(out.review_checklist.passed, true);
});

test("fetchAttemptDetails rejects on a non-2xx", async () => {
  stubFetchByUrl({ "/api/tasks/t1/attempts/9/details": { status: 404, body: {} } });
  await assert.rejects(fetchAttemptDetails("t1", 9), /404/);
});

test("fetchTask hydrates every attempt's heavy fields from the lazy endpoint", async () => {
  stubFetchByUrl({
    "/api/tasks/t1": {
      body: {
        id: "t1",
        attempts: [{ id: "a1", attempt_number: 1 }, { id: "a2", attempt_number: 2 }],
      },
    },
    "/api/tasks/t1/attempts/1/details": {
      body: { attempt_number: 1, review_checklist: { passed: false }, verifier_results: [{ name: "v1" }], test_results: { test_count: 1 } },
    },
    "/api/tasks/t1/attempts/2/details": {
      body: { attempt_number: 2, review_checklist: { passed: true }, verifier_results: [{ name: "v2" }], test_results: { test_count: 2 } },
    },
  });
  const task = await fetchTask("t1");
  assert.equal(task.attempts[0].review_checklist.passed, false);
  assert.equal(task.attempts[1].review_checklist.passed, true);
  assert.deepEqual(task.attempts[0].verifier_results, [{ name: "v1" }]);
  assert.equal(task.attempts[1].test_results.test_count, 2);
});

test("fetchTask degrades gracefully when one attempt's details 404 — the task still resolves", async () => {
  stubFetchByUrl({
    "/api/tasks/t1": {
      body: { id: "t1", attempts: [{ id: "a1", attempt_number: 1 }] },
    },
    "/api/tasks/t1/attempts/1/details": { status: 404, body: {} },
  });
  const task = await fetchTask("t1");
  assert.equal(task.id, "t1");
  assert.equal(task.attempts[0].review_checklist, undefined);
});

test("fetchTask with no attempts makes no lazy-detail calls at all", async () => {
  const calls = stubFetchByUrl({
    "/api/tasks/t1": { body: { id: "t1", attempts: [] } },
  });
  await fetchTask("t1");
  assert.deepEqual(calls, ["/api/tasks/t1"]);
});

// ── Fix A: the attempt-details fan-out cache ─────────────────────────────────
// A SlideOver poll calls fetchTask repeatedly for the same task; before this,
// EVERY poll re-fetched EVERY attempt's heavy blobs even though a TERMINAL
// attempt's review_checklist/verifier_results/test_results never change
// again. Cache eligibility is `attempt.status` in ("failed", "succeeded") —
// NOT `completed_at` presence: only the successful-delivery paths stamp
// `completed_at`, while a failed attempt (the ordinary, far more common
// shape) leaves it null. `completed_at` still rides in the cache key as an
// extra invalidation signal when a resumed attempt (same number) reaches a
// new terminal state.

test.beforeEach(() => { _clearAttemptDetailsCacheForTests(); });

test("a succeeded attempt's details are fetched once across two fetchTask calls", async () => {
  const calls = stubFetchByUrl({
    "/api/tasks/cache-done": {
      body: {
        id: "cache-done",
        attempts: [{ id: "a1", attempt_number: 1, status: "succeeded", completed_at: "2026-08-01T00:00:00Z" }],
      },
    },
    "/api/tasks/cache-done/attempts/1/details": {
      body: { attempt_number: 1, review_checklist: { passed: true }, verifier_results: [], test_results: null },
    },
  });
  await fetchTask("cache-done");
  const second = await fetchTask("cache-done");
  const detailHits = calls.filter((u) => u === "/api/tasks/cache-done/attempts/1/details");
  assert.equal(detailHits.length, 1, "the second call must reuse the cached blobs");
  assert.equal(second.attempts[0].review_checklist.passed, true,
    "the cached blobs must still be merged onto the SECOND call's task");
});

test("a FAILED attempt with completed_at NULL is still cached — status, not completed_at, gates eligibility", async () => {
  // This is the real, ordinary shape: `update_attempt(..., status="failed")`
  // never stamps `completed_at` (only the success-delivery paths in the
  // orchestrator's finalizer do). A cache gated on completed_at presence
  // would refetch every failed attempt forever — this pins that it does not.
  const calls = stubFetchByUrl({
    "/api/tasks/cache-failed": {
      body: {
        id: "cache-failed",
        attempts: [{ id: "a1", attempt_number: 1, status: "failed", completed_at: null }],
      },
    },
    "/api/tasks/cache-failed/attempts/1/details": {
      body: { attempt_number: 1, review_checklist: { passed: false }, verifier_results: [], test_results: null },
    },
  });
  await fetchTask("cache-failed");
  await fetchTask("cache-failed");
  const detailHits = calls.filter((u) => u === "/api/tasks/cache-failed/attempts/1/details");
  assert.equal(detailHits.length, 1,
    "a failed attempt's blobs are frozen just like a succeeded one's — the second call must reuse the cache");
});

test("an INTERRUPTED attempt is never cached — it can resume and its blobs can change again", async () => {
  const calls = stubFetchByUrl({
    "/api/tasks/cache-interrupted": {
      body: {
        id: "cache-interrupted",
        attempts: [{ id: "a1", attempt_number: 1, status: "interrupted", completed_at: null }],
      },
    },
    "/api/tasks/cache-interrupted/attempts/1/details": {
      body: { attempt_number: 1, review_checklist: null, verifier_results: [], test_results: null },
    },
  });
  await fetchTask("cache-interrupted");
  await fetchTask("cache-interrupted");
  const detailHits = calls.filter((u) => u === "/api/tasks/cache-interrupted/attempts/1/details");
  assert.equal(detailHits.length, 2,
    "interrupted looks terminal but is resumable — a resume can write NEW blobs onto the same row");
});

test("a RUNNING attempt (in-progress status, completed_at null) is refetched on every fetchTask call", async () => {
  const calls = stubFetchByUrl({
    "/api/tasks/cache-inflight": {
      body: {
        id: "cache-inflight",
        attempts: [{ id: "a1", attempt_number: 1, status: "in_progress", completed_at: null }],
      },
    },
    "/api/tasks/cache-inflight/attempts/1/details": {
      body: { attempt_number: 1, review_checklist: null, verifier_results: [{ name: "v1" }], test_results: null },
    },
  });
  await fetchTask("cache-inflight");
  await fetchTask("cache-inflight");
  const detailHits = calls.filter((u) => u === "/api/tasks/cache-inflight/attempts/1/details");
  assert.equal(detailHits.length, 2, "a still-running attempt's blobs genuinely change and must never be cached");
});

test("a changed completed_at (a resumed attempt reaching a new terminal state) busts the cached entry", async () => {
  const detailHits = [];
  let taskCalls = 0;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u === "/api/tasks/cache-bust") {
      taskCalls += 1;
      const completed_at = taskCalls === 1 ? "2026-08-01T00:00:00Z" : "2026-08-02T00:00:00Z";
      return {
        ok: true, status: 200,
        json: async () => ({
          id: "cache-bust",
          attempts: [{ id: "a1", attempt_number: 1, status: "succeeded", completed_at }],
        }),
      };
    }
    if (u === "/api/tasks/cache-bust/attempts/1/details") {
      detailHits.push(u);
      return {
        ok: true, status: 200,
        json: async () => ({ attempt_number: 1, review_checklist: { round: detailHits.length }, verifier_results: [], test_results: null }),
      };
    }
    throw new Error(`unstubbed fetch: ${u}`);
  };
  const first = await fetchTask("cache-bust");
  const secondFetch = await fetchTask("cache-bust");
  assert.equal(detailHits.length, 2,
    "a different completed_at is a different attempt outcome and must not reuse the old blobs");
  assert.equal(first.attempts[0].review_checklist.round, 1);
  assert.equal(secondFetch.attempts[0].review_checklist.round, 2);
});

test("a failed details fetch for a terminal attempt degrades to absent fields and is never cached", async () => {
  const calls = stubFetchByUrl({
    "/api/tasks/cache-fetch-fail": {
      body: {
        id: "cache-fetch-fail",
        attempts: [{ id: "a1", attempt_number: 1, status: "succeeded", completed_at: "2026-08-01T00:00:00Z" }],
      },
    },
    "/api/tasks/cache-fetch-fail/attempts/1/details": { status: 404, body: {} },
  });
  const task = await fetchTask("cache-fetch-fail");
  assert.equal(task.id, "cache-fetch-fail");
  assert.equal(task.attempts[0].review_checklist, undefined);
  assert.equal(task.attempts[0].verifier_results, undefined);
  assert.equal(task.attempts[0].test_results, undefined);
  const detailHits = calls.filter((u) => u === "/api/tasks/cache-fetch-fail/attempts/1/details");
  assert.equal(detailHits.length, 1);
});

test("the attempt-details cache is bounded — inserting more than the cap evicts the oldest", async () => {
  assert.equal(_attemptDetailsCacheSizeForTests(), 0);
  const CAP_PROBE = 60; // > the documented 50-entry cap
  for (let i = 0; i < CAP_PROBE; i++) {
    const taskId = `cache-bound-${i}`;
    stubFetchByUrl({
      [`/api/tasks/${taskId}`]: {
        body: { id: taskId, attempts: [{ id: "a1", attempt_number: 1, status: "succeeded", completed_at: "2026-08-01T00:00:00Z" }] },
      },
      [`/api/tasks/${taskId}/attempts/1/details`]: {
        body: { attempt_number: 1, review_checklist: { passed: true }, verifier_results: [], test_results: null },
      },
    });
    await fetchTask(taskId);
    assert.ok(_attemptDetailsCacheSizeForTests() <= 50,
      `cache grew past the cap after inserting entry ${i}`);
  }
  assert.equal(_attemptDetailsCacheSizeForTests(), 50,
    "a long board session must not grow the cache unbounded");
});

// fetchWindowSpend is `makeEndpointGate` over GET /api/metrics/window — the
// attempt-attributed "last 24h" figure the board's ledger must consume
// verbatim (core/metrics.py:window_spend). Exercised here as a real call
// against a stubbed fetch, same convention as every other endpoint above;
// makeEndpointGate's own latch mechanics are unit-tested in
// queueHealthGate.test.mjs. fetchWindowSpend is a module-level singleton
// (built once at import), so a 404 permanently latches it for the REST of
// this test file's run — the success case must run first.
test("fetchWindowSpend GETs the window endpoint and returns the parsed body", async () => {
  const calls = stubFetch({
    status: 200,
    body: { hours: 24, since: "2026-07-16T06:00:00+00:00", cost_usd: 1.25, cost_model: "claude-sonnet-5", tokens: 40, attempts: 2 },
  });
  const out = await fetchWindowSpend();
  assert.equal(calls[0].url, "/api/metrics/window?hours=24");
  assert.deepEqual(out, { hours: 24, since: "2026-07-16T06:00:00+00:00", cost_usd: 1.25, cost_model: "claude-sonnet-5", tokens: 40, attempts: 2 });
});

test("fetchWindowSpend latches to null on 404 (old daemon) and stops fetching", async () => {
  const calls = stubFetch({ status: 404, body: {} });
  const first = await fetchWindowSpend();
  const second = await fetchWindowSpend();
  assert.equal(first, null);
  assert.equal(second, null);
  // The gate short-circuits once latched — a second call must not issue a
  // second network request.
  assert.equal(calls.length, 1);
});
