import { test } from "node:test";
import assert from "node:assert/strict";
import { optionValue } from "./pathSuggest.js";

// The bug this guards: a native datalist only matches options whose value
// starts with the input. optionValue rebuilds each option in the input's own
// shape so a ~/-relative input still completes.

test("tilde-relative input keeps the tilde prefix", () => {
  assert.equal(optionValue("~/Dow", "Downloads"), "~/Downloads");
  assert.equal(optionValue("~/git/", "svc"), "~/git/svc");
});

test("absolute input keeps the absolute prefix", () => {
  assert.equal(optionValue("/Users/x/Dow", "Downloads"), "/Users/x/Downloads");
  assert.equal(optionValue("/Users/dev/git/", "alpha"), "/Users/dev/git/alpha");
});

test("no slash yet -> bare name (still starts-with the typed segment)", () => {
  assert.equal(optionValue("proj", "projects"), "projects");
  assert.equal(optionValue("", "git"), "git");
});

test("the result always starts with the text up to the last slash", () => {
  // The datalist contract: option.value must start with the input for it to
  // show. Everything through the last slash the user typed is preserved verbatim.
  for (const input of ["~/Dow", "/Users/x/Dow", "~/git/", "proj", ""]) {
    const cut = input.lastIndexOf("/");
    const prefix = cut >= 0 ? input.slice(0, cut + 1) : "";
    assert.ok(optionValue(input, "anything").startsWith(prefix));
  }
});

test("null/undefined inputs do not throw", () => {
  assert.equal(optionValue(undefined, "git"), "git");
  assert.equal(optionValue("~/g", undefined), "~/");
});

test("a Windows backslash input cuts at the last backslash", () => {
  assert.equal(optionValue("C:\\Users\\me\\Doc", "Documents"), "C:\\Users\\me\\Documents");
  assert.equal(optionValue("C:/Users/me/Doc", "Documents"), "C:/Users/me/Documents");
  assert.equal(optionValue("D:\\", "work"), "D:\\work");
});

test("children mode appends the separator matched to the input", () => {
  assert.equal(optionValue("~/work", "svc", true), "~/work/svc");
  assert.equal(optionValue("C:\\Users\\me", "work", true), "C:\\Users\\me\\work");
  // Already has a trailing separator: falls through to the cut behaviour,
  // which produces the identical result here.
  assert.equal(optionValue("~/work/", "svc", true), "~/work/svc");
  assert.equal(optionValue("", "git", true), "git");
});

test("children mode is opt-in", () => {
  // Every pre-existing 2-arg call site must keep the splice behaviour.
  assert.equal(optionValue("~/work", "svc"), "~/svc");
  assert.equal(optionValue("C:\\Users\\me", "work"), "C:\\Users\\work");
});
