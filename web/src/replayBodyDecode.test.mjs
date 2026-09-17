import test from "node:test";
import assert from "node:assert/strict";
import zlib from "node:zlib";
import { createDecodeStats, decode, decompressionHealth } from "../e2e/replayBodyDecode.mjs";

// Mirrors what posthog-js's recorder chunk actually sends: a top-level
// PostHog event carrying `properties.$snapshot_data`, an array of rrweb
// events. A `cv: "2024-10"`-marked FullSnapshot's whole `data` field is
// gzip-compressed and encoded as a JS "binary string" (one UTF-16 code unit
// per raw byte) — see replayBodyDecode.mjs's header comment for why that's
// `Buffer.from(s, "binary")`, not base64.
function gzipBinaryString(text) {
  return zlib.gzipSync(Buffer.from(text, "utf8")).toString("binary");
}

function makeSnapshotBody({ cv = "2024-10", data } = {}) {
  const payload = {
    event: "$snapshot",
    properties: {
      $snapshot_data: [{ type: 2, cv, data }],
    },
  };
  return { raw: Buffer.from(JSON.stringify(payload), "utf8"), req: { headers: {} } };
}

test("decompressionHealth is not ok when the cv compression marker no longer matches", () => {
  // Simulates mutation 1 from the PR: `ev.cv !== "2024-10"` drifting to a
  // different literal (e.g. "2024-11") — decompressRrwebEvent's guard clause
  // never even attempts to inflate the field, so cvMarkedEvents stays 0.
  const domOnlyText = "control-string-only-in-dom-channel";
  const { raw, req } = makeSnapshotBody({ cv: "2024-11", data: gzipBinaryString(domOnlyText) });
  const stats = createDecodeStats();
  const { text, expandedText } = decode(raw, req, stats);

  assert.equal(stats.cvMarkedEvents, 0);
  const health = decompressionHealth(stats, {
    netBytes: Buffer.byteLength(text, "utf8"),
    fullBytes: Buffer.byteLength(expandedText, "utf8"),
  });
  assert.equal(health.ok, false);
  assert.match(health.reason, /cv:"2024-10"/);
  // The DOM-only content never got decompressed at all, so it's invisible
  // to expandedText, too — this is exactly the fail-open shape being closed.
  assert.ok(!expandedText.includes(domOnlyText));
});

test("decompressionHealth is not ok when the expanded haystack is no larger than the network-only text", () => {
  // Simulates mutation 2 from the PR: decode() degraded so expandedText
  // ends up equal to (or no larger than) the raw network-only text — e.g. an
  // accidental `expandedText: text` regression. Constructed directly against
  // decompressionHealth (rather than via a broken decode()) since this
  // branch is about the byte-count relationship, independent of how it came
  // to be true.
  const stats = createDecodeStats();
  stats.cvMarkedEvents = 1;
  stats.compressedFieldsSeen = 1;
  stats.inflateOk = 1;
  stats.inflateFailed = 0;

  const health = decompressionHealth(stats, { netBytes: 500, fullBytes: 500 });
  assert.equal(health.ok, false);
  assert.match(health.reason, /not strictly larger/);
});

test("decompressionHealth is not ok when a cv-marked field fails to inflate", () => {
  // A cv-marked event whose `data` is not actually gzip data (posthog-js's
  // per-field encoding drifted) — inflateBinaryGzipString's catch counts it
  // as inflateFailed rather than silently doing nothing.
  const { raw, req } = makeSnapshotBody({ cv: "2024-10", data: "not actually gzip data" });
  const stats = createDecodeStats();
  const { text, expandedText } = decode(raw, req, stats);

  assert.equal(stats.cvMarkedEvents, 1);
  assert.equal(stats.inflateFailed, 1);
  assert.equal(stats.inflateOk, 0);

  const health = decompressionHealth(stats, {
    netBytes: Buffer.byteLength(text, "utf8"),
    fullBytes: Buffer.byteLength(expandedText, "utf8"),
  });
  assert.equal(health.ok, false);
  // Falls into the "zero inflated successfully" branch since inflateOk is 0.
  assert.match(health.reason, /zero compressed field\(s\) inflated/);
});

test("decompressionHealth is ok on a well-formed compressed FullSnapshot, and the decompressed DOM text is in expandedText but not in text", () => {
  const domOnlyText = "control-string-only-in-dom-channel-4e8b";
  const { raw, req } = makeSnapshotBody({ cv: "2024-10", data: gzipBinaryString(domOnlyText) });
  const stats = createDecodeStats();
  const { text, expandedText, events } = decode(raw, req, stats);

  assert.equal(events.length, 1);
  assert.equal(stats.cvMarkedEvents, 1);
  assert.equal(stats.inflateOk, 1);
  assert.equal(stats.inflateFailed, 0);

  // The DOM-only content is only reachable via the decompressed field, never
  // via a naive substring scan of the outer JSON `text` — this is the exact
  // distinction checks 2/3/4/6/7 in replay-body-leak.mjs rely on.
  assert.ok(!text.includes(domOnlyText));
  assert.ok(expandedText.includes(domOnlyText));

  const health = decompressionHealth(stats, {
    netBytes: Buffer.byteLength(text, "utf8"),
    fullBytes: Buffer.byteLength(expandedText, "utf8"),
  });
  assert.equal(health.ok, true);
  assert.match(health.reason, /0 failures/);
});
