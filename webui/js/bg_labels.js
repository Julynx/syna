"use strict";

/* Pure label helpers for the background-work UI: the termination label
   on the viewer modal's footer button, and the per-row state and
   runtime labels of the background work list. Kept free of browser
   dependencies so the Node suite (tests/js/test_bg_labels.cjs) can
   exercise them directly. */

/* Short termination labels for the footer button, derived from the
   server summaries (live background_done events) or the structured
   record fields (reopened modals). */
function bgEndLabel(kind, meta) {
  const summary = meta.summary || "";
  if (kind === "process") {
    if (summary === "stopped by the user") return "Stopped by user";
    if (summary === "killed by its runtime cap") return "Stopped by runtime cap";
    const exit = summary.match(/^exited with code (-?\d+)$/);
    if (exit) return "Exited " + exit[1];
    if (meta.state === "exited") {
      const code =
        meta.exit_code === null || meta.exit_code === undefined
          ? "?"
          : meta.exit_code;
      return "Exited " + code;
    }
    if (meta.state === "killed") {
      return meta.killed_by === "model"
        ? "Stopped by the model"
        : "Stopped by user";
    }
    return summary || "Finished";
  }
  if (summary.startsWith("stopped by the ")) {
    const who = summary.slice("stopped by the ".length);
    return who === "user" ? "Stopped by user" : "Stopped by the " + who;
  }
  if (summary === "stopped by its runtime cap") return "Stopped by runtime cap";
  if (summary === "ended with an error") return "Ended with error";
  if (summary === "finished with a response") return "Finished";
  if (meta.state === "killed") {
    const who = meta.stopped_by || "user";
    return who === "user" ? "Stopped by user" : "Stopped by the " + who;
  }
  if (meta.end_reason === "runtime cap") return "Stopped by runtime cap";
  if (meta.end_reason === "error") return "Ended with error";
  return "Finished";
}

/* Short per-row state for the list cards. */
function bgListStateLabel(kind, row) {
  if (row.state === "running") return "running";
  if (kind === "process") {
    if (row.state === "killed") return "stopped";
    const code =
      row.exit_code === null || row.exit_code === undefined
        ? "?"
        : row.exit_code;
    return "exit " + code;
  }
  if (row.end_reason === "runtime cap") return "runtime cap";
  if (row.state === "killed") return "stopped";
  if (row.end_reason === "error") return "error";
  return "finished";
}

/* Compact elapsed time for the list cards: live runtime for running
   work, total runtime for finished work. */
function bgRuntimeLabel(started) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - started));
  if (seconds < 60) return seconds + "s";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + "m";
  const hours = Math.floor(minutes / 60);
  return hours + "h" + String(minutes % 60).padStart(2, "0") + "m";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { bgEndLabel, bgListStateLabel, bgRuntimeLabel };
}
