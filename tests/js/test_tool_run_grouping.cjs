/**
 * Drift guard for the web UI's tool-run grouping.
 *
 * Consecutive tool calls collapse into one "See more" group. The event
 * dispatcher in chat.js ends a run only on events the user can see
 * (bubbles, file cards, errors, infos); invisible mid-turn accounting
 * must never split it — `usage` fires after every model request, so a
 * missing exclusion there renders one group per tool batch.
 *
 * Both sides are parsed textually: the exclusion list from chat.js,
 * the event types each source emits from the server modules. The test
 * asserts every event type the server can emit is classified, and that
 * the invisible ones are all excluded:
 *
 *   node tests/js/test_tool_run_grouping.cjs
 *
 * The reporting mirrors tests/_runner.py: one PASS/FAIL line per test
 * and a non-zero exit code when anything fails.
 */
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const chatSource = fs.readFileSync(
  path.join(__dirname, "..", "..", "webui", "js", "chat.js"),
  "utf8",
);
const serverSources = [
  "conversation/session.py",
  "server.py",
  "capabilities/subagents.py",
].map((relative) =>
  fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "syna", relative),
    "utf8",
  ),
);

function dispatcherExclusions() {
  const match = chatSource.match(
    /event\.type !== "tool_call"[\s\S]*?resetToolRun\(\);/,
  );
  assert.ok(match, "the resetToolRun dispatcher block was not found");
  return [...match[0].matchAll(/event\.type !== "(\w+)"/g)].map((m) => m[1]);
}

function serverEventTypes() {
  const types = new Set();
  for (const source of serverSources) {
    for (const match of source.matchAll(/"type": "(\w+)"/g)) {
      types.add(match[1]);
    }
  }
  /* Content-part types ride inside image history messages, not the
     event stream; everything else parsed here is a chat event. */
  types.delete("text");
  types.delete("image_url");
  assert.ok(types.size > 0, "no event types parsed from the server sources");
  return types;
}

/* Events with no visible rendering in the chat: they update chips and
   state or carry data for other viewers, so they cannot interrupt a run.
   Anything visible (bubbles, file cards, error/info lines) must stay out
   of this set and keep ending the group. */
const INVISIBLE_EVENT_TYPES = [
  "busy",
  "ready",
  "status",
  "usage",
  "compaction",
  "subagent_message",
  "background_done",
  "rewind_state",
  "history_rewind",
  "history_done",
  "injected_notice",
  "tool_call",
];

const VISIBLE_EVENT_TYPES = [
  "user_message",
  "model_message",
  "file",
  "file_sent",
  "error",
  "info",
];

const tests = [];

function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

test("every invisible event type is excluded from ending a tool run", () => {
  const excluded = dispatcherExclusions();
  const missing = INVISIBLE_EVENT_TYPES.filter(
    (type) => !excluded.includes(type),
  );
  assert.deepEqual(
    missing,
    [],
    "invisible event types missing from the exclusion list: " +
      missing.join(", "),
  );
});

test("every visible event type still ends a tool run", () => {
  const excluded = dispatcherExclusions();
  const wronglyExcluded = VISIBLE_EVENT_TYPES.filter((type) =>
    excluded.includes(type),
  );
  assert.deepEqual(
    wronglyExcluded,
    [],
    "visible events must reset the run, but are excluded: " +
      wronglyExcluded.join(", "),
  );
});

test("every server event type is classified as visible or invisible", () => {
  const excluded = dispatcherExclusions();
  const classified = new Set([...excluded, ...VISIBLE_EVENT_TYPES]);
  const serverTypes = serverEventTypes();
  const unclassified = [...serverTypes].filter(
    (type) => !classified.has(type) && type !== "tool_call",
  );
  assert.deepEqual(
    unclassified,
    [],
    "server event types with no visibility classification: " +
      unclassified.join(", "),
  );
});

function main() {
  let failed = 0;
  for (const { name, fn } of tests) {
    try {
      fn();
      console.log(`PASS ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`FAIL ${name}`);
      console.log(error.message);
    }
  }
  if (failed) {
    console.log(`${failed} TEST(S) FAILED`);
    process.exit(1);
  }
  console.log(`ALL ${tests.length} TESTS PASSED`);
}

main();
