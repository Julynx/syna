/**
 * Test suite for the background-work label helpers
 * (webui/js/bg_labels.js: bgEndLabel, bgListStateLabel, bgRuntimeLabel).
 *
 * Runs on plain Node.js with zero dependencies:
 *
 *   node tests/js/test_bg_labels.cjs
 *
 * The reporting mirrors tests/_runner.py: one PASS/FAIL line per test
 * and a non-zero exit code when anything fails.
 */
"use strict";

const assert = require("assert");
const {
  bgEndLabel,
  bgListStateLabel,
  bgRuntimeLabel,
  bgCallLabel,
} = require("../../webui/js/bg_labels.js");

const tests = [];

function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

test("process summaries map to short termination labels", () => {
  assert.strictEqual(
    bgEndLabel("process", { summary: "stopped by the user" }),
    "Stopped by user",
  );
  assert.strictEqual(
    bgEndLabel("process", { summary: "killed by its runtime cap" }),
    "Stopped by runtime cap",
  );
  assert.strictEqual(
    bgEndLabel("process", { summary: "exited with code 0" }),
    "Exited 0",
  );
  assert.strictEqual(
    bgEndLabel("process", { summary: "exited with code -1" }),
    "Exited -1",
  );
});

test("process structured fields map to short termination labels", () => {
  assert.strictEqual(bgEndLabel("process", { state: "exited", exit_code: 3 }), "Exited 3");
  assert.strictEqual(
    bgEndLabel("process", { state: "exited", exit_code: null }),
    "Exited ?",
  );
  assert.strictEqual(
    bgEndLabel("process", { state: "killed", killed_by: "user" }),
    "Stopped by user",
  );
  assert.strictEqual(
    bgEndLabel("process", { state: "killed", killed_by: "model" }),
    "Stopped by the model",
  );
});

test("subagent summaries map to short termination labels", () => {
  assert.strictEqual(
    bgEndLabel("subagent", { summary: "stopped by the user" }),
    "Stopped by user",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { summary: "stopped by the model" }),
    "Stopped by the model",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { summary: "stopped by its runtime cap" }),
    "Stopped by runtime cap",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { summary: "ended with an error" }),
    "Ended with error",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { summary: "finished with a response" }),
    "Finished",
  );
});

test("subagent structured fields map to short termination labels", () => {
  assert.strictEqual(
    bgEndLabel("subagent", { state: "killed", stopped_by: "user" }),
    "Stopped by user",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { state: "killed", stopped_by: "teardown" }),
    "Stopped by the teardown",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { end_reason: "runtime cap" }),
    "Stopped by runtime cap",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { end_reason: "error" }),
    "Ended with error",
  );
  assert.strictEqual(
    bgEndLabel("subagent", { state: "finished", end_reason: "response" }),
    "Finished",
  );
});

test("every live summary string maps off the summary alone", () => {
  /* The live paths pass only {summary}; no structured fallback may be
     required to reach the right label. */
  const cases = {
    process: {
      "stopped by the user": "Stopped by user",
      "killed by its runtime cap": "Stopped by runtime cap",
      "exited with code 2": "Exited 2",
    },
    subagent: {
      "stopped by the user": "Stopped by user",
      "stopped by its runtime cap": "Stopped by runtime cap",
      "ended with an error": "Ended with error",
      "finished with a response": "Finished",
    },
  };
  for (const [kind, summaries] of Object.entries(cases)) {
    for (const [summary, expected] of Object.entries(summaries)) {
      assert.strictEqual(bgEndLabel(kind, { summary }), expected);
    }
  }
});

test("list state labels are short and kind-aware", () => {
  assert.strictEqual(bgListStateLabel("process", { state: "running" }), "running");
  assert.strictEqual(
    bgListStateLabel("process", { state: "exited", exit_code: 0 }),
    "exit 0",
  );
  assert.strictEqual(
    bgListStateLabel("process", { state: "exited", exit_code: null }),
    "exit ?",
  );
  assert.strictEqual(bgListStateLabel("process", { state: "killed" }), "stopped");
  assert.strictEqual(bgListStateLabel("subagent", { state: "running" }), "running");
  assert.strictEqual(
    bgListStateLabel("subagent", { state: "finished", end_reason: "response" }),
    "finished",
  );
  assert.strictEqual(
    bgListStateLabel("subagent", { state: "killed" }),
    "stopped",
  );
  assert.strictEqual(
    bgListStateLabel("subagent", { end_reason: "runtime cap" }),
    "runtime cap",
  );
});

test("runtime labels format compact durations", () => {
  const now = Date.now() / 1000;
  assert.strictEqual(bgRuntimeLabel(now - 0), "0s");
  assert.strictEqual(bgRuntimeLabel(now - 34), "34s");
  assert.strictEqual(bgRuntimeLabel(now - 90), "1m");
  assert.strictEqual(bgRuntimeLabel(now - 3600), "1h00m");
  assert.strictEqual(bgRuntimeLabel(now - 3660), "1h01m");
  assert.strictEqual(bgRuntimeLabel(now + 30), "0s", "clock skew clamps to zero");
});

test("dispatch call labels name the action and the work", () => {
  assert.strictEqual(bgCallLabel("process", "spawn", "ticker"), 'Spawned process "ticker"');
  assert.strictEqual(bgCallLabel("process", "check", "ticker"), 'Checked process "ticker"');
  assert.strictEqual(bgCallLabel("process", "kill", "ticker"), 'Killed process "ticker"');
  assert.strictEqual(bgCallLabel("subagent", "spawn", "filedemo"), 'Spawned subagent "filedemo"');
  assert.strictEqual(bgCallLabel("subagent", "check", "filedemo"), 'Checked subagent "filedemo"');
  assert.strictEqual(bgCallLabel("subagent", "kill", "filedemo"), 'Killed subagent "filedemo"');
});

test("dispatch call labels survive missing pieces", () => {
  assert.strictEqual(bgCallLabel("process", "spawn", ""), "Spawned process");
  assert.strictEqual(bgCallLabel("subagent", "check", ""), "Checked subagent");
  assert.strictEqual(bgCallLabel("process", "", ""), "process");
  assert.strictEqual(bgCallLabel("subagent", "unknown", "x"), 'subagent "x"');
  assert.strictEqual(bgCallLabel("other", "spawn", ""), "other");
});

/* ------------------------------------------------------------------ */
/* Runner                                                               */
/* ------------------------------------------------------------------ */

let failed = [];
for (const { name, fn } of tests) {
  try {
    fn();
    console.log("PASS " + name);
  } catch (err) {
    failed.push(name);
    console.log("FAIL " + name);
    console.log(err && err.stack ? err.stack : String(err));
  }
}
if (failed.length) {
  console.log(failed.length + " TEST(S) FAILED: " + failed.join(", "));
  process.exit(1);
}
console.log("ALL " + tests.length + " TESTS PASSED");
