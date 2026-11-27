/**
 * Drift guard for the web slash commands.
 *
 * The web UI's client-side registry (SLASH_COMMANDS in webui/js/chat.js)
 * is the UX; the server's WEB_COMMANDS frozenset is the authoritative
 * gate. Every non-local client command must exist in the server
 * whitelist, and /help must stay local (it renders without a server
 * round trip). Both sources are parsed textually, so the test runs on
 * plain Node.js with zero dependencies:
 *
 *   node tests/js/test_slash_commands.cjs
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
const serverSource = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "syna", "server.py"),
  "utf8",
);

function clientRegistry() {
  const match = chatSource.match(/const SLASH_COMMANDS = \{[\s\S]*?\n\};/);
  assert.ok(match, "SLASH_COMMANDS registry not found in webui/js/chat.js");
  const names = [...match[0].matchAll(/^\s{2}(\w+): \{/gm)].map((m) => m[1]);
  assert.ok(names.length > 0, "no commands parsed from the client registry");
  return names;
}

function serverWhitelist() {
  const match = serverSource.match(/WEB_COMMANDS = frozenset\(\{[^}]*\}\)/);
  assert.ok(match, "WEB_COMMANDS whitelist not found in server.py");
  return [...match[0].matchAll(/"(\w+)"/g)].map((m) => m[1]);
}

const tests = [];

function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

test("every server-side command is offered by the web client", () => {
  const client = new Set(clientRegistry());
  for (const name of serverWhitelist()) {
    assert.ok(client.has(name), `WEB_COMMANDS has '/${name}' but the web registry does not`);
  }
});

test("/help is client-local and never sent to the server", () => {
  const source = chatSource.match(/const SLASH_COMMANDS = \{[\s\S]*?\n\};/)[0];
  const helpEntry = source.match(/help: \{[\s\S]*?\n {2}\},/);
  assert.ok(helpEntry, "the /help entry is missing from the client registry");
  assert.ok(/local: true/.test(helpEntry[0]), "/help must be marked local");
  assert.ok(!serverWhitelist().includes("help"), "/help must not be whitelisted server-side");
});

test("/compact is whitelisted on both sides", () => {
  assert.ok(clientRegistry().includes("compact"), "the client registry lost /compact");
  assert.ok(serverWhitelist().includes("compact"), "WEB_COMMANDS lost /compact");
});

async function main() {
  let failed = [];
  for (const { name, fn } of tests) {
    try {
      await fn();
      console.log(`PASS ${name}`);
    } catch (err) {
      failed.push(name);
      console.log(`FAIL ${name}`);
      console.error(err && err.message ? err.message : err);
    }
  }
  if (failed.length) {
    console.log(`${failed.length} TEST(S) FAILED: ${failed.join(", ")}`);
    process.exit(1);
  }
  console.log(`ALL ${tests.length} TESTS PASSED`);
}

main();
