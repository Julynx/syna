"""Unit tests for the background process capability."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities import processes
from syna.capabilities.processes import (
    CAP_EXIT_CODE,
    ProcessRecord,
    PodRegistry,
    format_process_report,
    kill_process,
    process_completion_notice,
    process_status_summary,
    reserve_id,
    spawn_process,
    unknown_id_error,
)
from syna.capabilities.subagents import SubagentRecord


class FakeContainer:
    """Exec recorder standing in for the pod."""

    def __init__(self, outputs: dict[str, str] | None = None):
        self.calls: list[list] = []
        self.outputs = outputs or {}

    def exec(self, argv):
        self.calls.append(argv)
        script = argv[-1] if len(argv) > 2 else ""
        for marker, output in self.outputs.items():
            if marker in script:
                return output
        return ""

    def copy_to(self, source, dest):
        pass


def fresh_registry():
    return PodRegistry()


def test_id_validation_accepts_and_rejects():
    registry = fresh_registry()
    accepted, error = reserve_id(registry, "process", "my-job.1_x")
    assert accepted == "my-job.1_x" and error is None
    for bad in [
        "",
        "-lead",
        "has space",
        "slash/ed",
        "x" * 65,
        "café",
        "..",
        ".",
        "...",
        ".hidden",
        "a..b",
        "trailing.",
    ]:
        _, error = reserve_id(registry, "process", bad)
        assert error, bad


def test_ids_are_unique_across_kinds_and_never_released():
    registry = fresh_registry()
    first, error = reserve_id(registry, "process", "worker")
    assert error is None
    _, error = reserve_id(registry, "subagent", "worker")
    assert error and "already taken" in error
    auto, error = reserve_id(registry, "process", None)
    assert error is None and auto == "process_0001"
    auto_sub, error = reserve_id(registry, "subagent", None)
    assert error is None and auto_sub == "subagent_0001"


def test_auto_ids_skip_taken_and_count_per_kind():
    registry = fresh_registry()
    registry.taken_ids.add("process_0001")
    assert reserve_id(registry, "process", None)[0] == "process_0002"
    assert reserve_id(registry, "subagent", None)[0] == "subagent_0001"


def test_spawn_registers_and_starts_watcher(monkeypatch):
    registry = fresh_registry()
    container = FakeContainer({"PGID": "PGID 4242\nSTARTED 1794185400.250"})
    monkeypatch.setattr(processes, "_ensure_watcher", lambda r, c: None)
    result = spawn_process(
        "python3 build.py",
        name="build",
        registry=registry,
        container=container,
        runtime_cap_s=99,
    )
    assert result["name"] == "build"
    assert result["max_runtime_s"] == 99
    proc = registry.processes["build"]
    assert proc.pgid == 4242
    assert proc.started_at == 1794185400.250
    assert proc.dir == "/workspace/processes/build"
    exec_script = container.calls[0][-1]
    assert "setsid bash -c" in exec_script
    assert "timeout -k 10 99" in exec_script
    assert "stdbuf -oL -eL" in exec_script
    assert 'printf %s "$bg" > ' in exec_script
    assert "/workspace/processes/build/stdout" in exec_script
    assert "> /workspace/processes/build/exit_code" in exec_script
    assert "date +%s.%N > " in exec_script and "finished_at" in exec_script
    started_line = next(
        line for line in exec_script.splitlines() if "started_at" in line
    )
    timeout_line = next(
        line for line in exec_script.splitlines() if "timeout -k" in line
    )
    assert exec_script.index(started_line) < exec_script.index(timeout_line), (
        "the start stamp must be written before timeout begins counting"
    )


def test_spawn_failure_releases_the_id(monkeypatch):
    class Exploding(FakeContainer):
        def exec(self, argv):
            raise RuntimeError("pod dead")

    registry = fresh_registry()
    monkeypatch.setattr(processes, "_ensure_watcher", lambda r, c: None)
    result = spawn_process("x", name="doomed", registry=registry, container=Exploding())
    assert "could not start" in result["error"]
    assert "doomed" not in registry.taken_ids
    assert "doomed" not in registry.processes


def test_spawn_stamps_the_parent_record(monkeypatch):
    """Work spawned inside a subagent's turn records the spawner's name."""
    from syna.state import get_background_record, set_background_record

    registry = fresh_registry()
    monkeypatch.setattr(processes, "_ensure_watcher", lambda r, c: None)
    set_background_record(None)
    top = spawn_process(
        "x",
        name="top",
        registry=registry,
        container=FakeContainer({"PGID": "PGID 1\nSTARTED 1794185400.1"}),
    )
    assert registry.processes[top["name"]].parent_id is None
    set_background_record(SubagentRecord(id="par", goal="g", level=2))
    try:
        nested = spawn_process(
            "x",
            name="nested",
            registry=registry,
            container=FakeContainer({"PGID": "PGID 2\nSTARTED 1794185400.2"}),
        )
        assert registry.processes[nested["name"]].parent_id == "par"
    finally:
        set_background_record(None)
    assert get_background_record() is None


def test_reports_use_the_record_runtime_cap(monkeypatch):
    monkeypatch.setattr(processes, "load_process_max_runtime_s", lambda: 1200)
    capped = ProcessRecord(
        id="p1", command="x", dir="/workspace/processes/p1", runtime_cap_s=5
    )
    assert "5s cap" in format_process_report(capped, "running", "", "")
    capped.exit_code = CAP_EXIT_CODE
    capped.state = "exited"
    assert "the cap is 5 seconds" in process_completion_notice(capped)
    default = ProcessRecord(id="p2", command="x", dir="/workspace/processes/p2")
    assert "1200s cap" in format_process_report(default, "running", "", "")


def test_check_reports_state_and_tails(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="p1", command="x", dir="/workspace/processes/p1")
    registry.processes["p1"] = proc
    container = FakeContainer()
    view = processes._tail_script("/workspace/processes/p1")
    assert "base64" in view and "exit_code" in view
    monkeypatch.setattr(
        processes,
        "check_process_state",
        lambda c, d: ("exited:0", "hello out", "warn line", None),
    )
    report = format_process_report(proc, "exited:0", "hello out", "warn line")
    assert "finished with exit code 0" in report
    assert "hello out" in report and "warn line" in report
    assert "/workspace/processes/p1/stdout" in report


def test_tail_script_caps_bytes_and_frames_base64():
    script = processes._tail_script("/workspace/processes/p")
    assert f"tail -c {4000 * 4}" in script
    assert "finished_at" in script
    stdout, stderr = processes._decode_tails(
        "STATE running\nFINISHED_AT \nSTDOUT\n" + "aGVsbG8=\nSTDERR\n"
    )
    assert stdout == "hello" and stderr == ""


def test_check_process_state_parses_the_end_stamp():
    container = FakeContainer(
        {
            "STATE ": "STATE exited:0\nFINISHED_AT 1794185405\nSTDOUT\n\nSTDERR\n\n",
        }
    )
    view = container.outputs["STATE "]
    lines = view.splitlines()
    assert lines[1] == "FINISHED_AT 1794185405"
    state, stdout, stderr, ended = processes.check_process_state(container, "/d")
    assert state == "exited:0" and ended == 1794185405.0
    missing_stamp = FakeContainer(
        {"STATE ": "STATE exited:0\nFINISHED_AT \nSTDOUT\n\nSTDERR\n\n"}
    )
    state, stdout, stderr, ended = processes.check_process_state(missing_stamp, "/d")
    assert ended is None


def test_spawn_markers_parse_pgid_and_started_stamp():
    container = FakeContainer({"PGID": "PGID 4242\nSTARTED 1794185400.250"})
    pgid, started_at = processes._read_spawn_markers(container, "/d")
    assert pgid == 4242 and started_at == 1794185400.25


def test_spawn_markers_survive_a_date_without_nanoseconds():
    """A `date` without %N support must not poison the runtime.

    The stamp parse fails, started_at stays None, and reports fall
    back to the host-clock measurement instead of crashing or using a
    garbage epoch.
    """
    container = FakeContainer({"PGID": "PGID 7\nSTARTED 1794185400.N"})
    pgid, started_at = processes._read_spawn_markers(container, "/d")
    assert pgid == 7 and started_at is None
    assert processes._parse_epoch("") is None
    assert processes._parse_epoch("not-a-number") is None


def test_ended_runtime_prefers_the_pod_clock_stamps():
    """Runtime from the launcher's stamps beats the host-clock mix.

    The live log caught this: a 5s cap was reported as 4s because
    finished_at (pod clock) was compared against started (host clock,
    stamped before the spawn exec). With both pod stamps present the
    host values are irrelevant, skew included.
    """
    proc = ProcessRecord(id="p9", command="x", dir="/d")
    proc.started = 1000.0
    proc.started_at = 1002.0
    proc.finished_at = 1007.0
    assert processes._ended_runtime_s(proc) == 5.0
    only_end = ProcessRecord(id="p10", command="x", dir="/d")
    only_end.started = 1000.0
    only_end.finished_at = 1005.0
    assert processes._ended_runtime_s(only_end) == 5.0
    nothing = ProcessRecord(id="p11", command="x", dir="/d")
    nothing.started = time.time() - 3.0
    assert abs(processes._ended_runtime_s(nothing) - 3.0) < 0.1


def test_stamp_parsing_rejects_non_finite_values():
    """The stamp files are pod-writable; nan must not become a runtime."""
    assert processes._parse_epoch("nan") is None
    assert processes._parse_epoch("inf") is None
    assert processes._parse_epoch("-inf") is None
    container = FakeContainer(
        {"STATE ": "STATE exited:0\nFINISHED_AT nan\nSTDOUT\n\nSTDERR\n\n"}
    )
    state, stdout, stderr, ended = processes.check_process_state(container, "/d")
    assert ended is None


def test_kill_signals_the_group_and_keeps_artifacts(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="p1", command="x", dir="/workspace/processes/p1")
    proc.pgid = 77
    registry.processes["p1"] = proc
    container = FakeContainer()
    monkeypatch.setattr(
        processes,
        "check_process_state",
        lambda c, d: ("running", "", "", None),
    )
    monkeypatch.setattr(processes, "KILL_WAIT_S", 0.05)
    monkeypatch.setattr(processes, "KILL_POLL_INTERVAL_S", 0.01)
    report = kill_process("p1", registry=registry, container=container, by="model")
    assert "still terminating" in report
    assert "notice arrives here then" in report
    kill_script = container.calls[0][-1]
    assert 'kill -- -"$pgid"' in kill_script
    assert "kill -9" in kill_script
    assert proc.state == "killed" and proc.killed_by == "model"
    # The timeout path promised the notice, so it must still fire.
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    processes._finalize_process(registry, proc, "0")
    assert proc.state == "exited" and proc.exit_code == 0
    assert len(seen) == 1, "the timeout fallback must not swallow the notice"


def test_kill_blocks_until_the_exit_lands(monkeypatch):
    """A kill waits for the end and reports the final exit code.

    The response then delivered the outcome: a model kill marks the
    record collected (no follow-up notice), a user kill leaves the
    flags so the notice still tells the model who stopped the work.
    """
    registry = fresh_registry()
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    proc = ProcessRecord(id="pk", command="x", dir="/workspace/processes/pk")
    proc.pgid = 7
    registry.processes["pk"] = proc
    container = FakeContainer()
    probes = [0]

    def state_after_kill(c, d):
        probes[0] += 1
        return (
            ("running", "", "", None)
            if probes[0] == 1
            else ("exited:143", "", "", None)
        )

    monkeypatch.setattr(processes, "check_process_state", state_after_kill)
    monkeypatch.setattr(processes, "KILL_POLL_INTERVAL_S", 0.01)

    report = kill_process("pk", registry=registry, container=container, by="model")
    assert "exit code 143" in report
    assert "still terminating" not in report
    assert proc.outcome_collected, "the response delivered the outcome"
    processes._finalize_process(registry, proc, "143")
    assert seen == [], "no notice may follow an outcome the kill delivered"

    proc2 = ProcessRecord(id="pu", command="x", dir="/workspace/processes/pu")
    proc2.pgid = 8
    registry.processes["pu"] = proc2
    probes[0] = 0
    report = kill_process("pu", registry=registry, container=container, by="user")
    assert "exit code 143" in report
    assert not proc2.outcome_collected, (
        "the model did not see this response; the notice must tell it"
    )
    processes._finalize_process(registry, proc2, "143")
    assert len(seen) == 1
    assert "stopped by the user" in seen[0]["model_notice"]


def test_kill_on_finished_process_reports_snapshot(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    proc.state = "exited"
    proc.exit_code = 0
    registry.processes["p1"] = proc
    monkeypatch.setattr(
        processes, "check_process_state", lambda c, d: ("exited:0", "", "", None)
    )
    report = kill_process(
        "p1", registry=registry, container=FakeContainer(), by="model"
    )
    assert "already finished" in report
    assert proc.killed_by is None, "a settled record keeps its own story"
    assert proc.outcome_collected, "the kill report carried the outcome"
    assert proc.finished_at is not None


def test_finished_reports_use_the_recorded_end_time(monkeypatch):
    """A finished process's runtime comes from its end time, not report time."""
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    proc.started = time.time() - 60
    proc.finished_at = proc.started + 5
    report = format_process_report(proc, "exited:0", "", "")
    assert "after 5s" in report, report
    assert "after 60s" not in report
    still_running = ProcessRecord(id="p2", command="x", dir="/d")
    still_running.started = time.time() - 7
    report = format_process_report(still_running, "running", "", "")
    assert "runtime 7s" in report, report


def test_observe_end_time_adopts_the_first_finished_sighting():
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    processes.observe_end_time(proc, "running")
    assert proc.finished_at is None
    before = time.time()
    processes.observe_end_time(proc, "exited:0")
    assert proc.finished_at is not None and proc.finished_at >= before
    marked = proc.finished_at
    processes.observe_end_time(proc, "exited:1")
    assert proc.finished_at == marked, "the first sighting wins"


def test_adopt_end_time_prefers_the_pod_stamp():
    """The launcher's stamp is the truth; a late fallback cannot win."""
    true_end = time.time() - 30
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    proc.started = true_end - 5
    processes.adopt_end_time(proc, "exited:0", true_end)
    assert proc.finished_at == true_end
    later_fallback = true_end + 20
    processes.adopt_end_time(proc, "exited:0", None)
    assert proc.finished_at == true_end, "a stamp already set is kept"
    processes.observe_end_time(proc, "exited:0")
    assert proc.finished_at == true_end
    late_poller = ProcessRecord(id="p2", command="x", dir="/d")
    late_poller.started = true_end - 5
    processes.observe_end_time(late_poller, "exited:0")
    late_poller.finished_at = later_fallback
    processes.adopt_end_time(late_poller, "exited:0", true_end)
    assert late_poller.finished_at == true_end, (
        "the pod stamp replaces a later fallback observation"
    )


def test_finalize_uses_the_launcher_stamp():
    registry = fresh_registry()
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    proc.started = 1794185400.0
    registry.processes["p1"] = proc
    processes._finalize_process(registry, proc, "0", "1794185405")
    assert proc.finished_at == 1794185405.0, "the true end time, not poll time"
    no_stamp = ProcessRecord(id="p2", command="x", dir="/d")
    registry.processes["p2"] = no_stamp
    processes._finalize_process(registry, no_stamp, "0", "")
    assert no_stamp.finished_at is not None, "the fallback still marks the end"


def test_unknown_id_error_explains_recovery():
    message = unknown_id_error("ghost")
    assert "ghost" in message
    assert "per-session" in message and "ps" in message


def test_finalize_fires_one_completion_notice():
    registry = fresh_registry()
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    proc = ProcessRecord(id="p9", command="x", dir="/d")
    registry.processes["p9"] = proc
    processes._finalize_process(registry, proc, str(CAP_EXIT_CODE))
    processes._finalize_process(registry, proc, str(CAP_EXIT_CODE))
    assert len(seen) == 1
    assert proc.finished_at is not None, "finalize records the end time"
    payload = seen[0]
    assert payload["kind"] == "process" and payload["id"] == "p9"
    assert "runtime cap" in payload["model_notice"]
    assert "runtime cap" in payload["summary"]


def test_completion_payload_carries_the_parent_id():
    registry = fresh_registry()
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    proc = ProcessRecord(id="p9", command="x", dir="/d", parent_id="par")
    registry.processes["p9"] = proc
    processes._finalize_process(registry, proc, "0")
    assert len(seen) == 1
    assert seen[0]["parent_id"] == "par"
    assert seen[0]["id"] == "p9"


def test_wait_for_collection_suppresses_the_notice():
    registry = fresh_registry()
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    proc = ProcessRecord(id="p10", command="x", dir="/d")
    registry.processes["p10"] = proc
    proc.outcome_collected = True
    processes._finalize_process(registry, proc, "0")
    assert seen == [], "wait_for already reported this outcome"
    assert proc.state == "exited" and proc.exit_code == 0


def test_completion_notice_text_carries_paths():
    proc = ProcessRecord(id="p2", command="x", dir="/workspace/processes/p2")
    proc.state = "exited"
    proc.exit_code = 0
    notice = process_completion_notice(proc)
    assert "/workspace/processes/p2/stdout" in notice
    assert "/workspace/processes/p2/stderr" in notice
    assert "wait_for" in notice
    assert process_status_summary(proc) == "exited with code 0"


def test_watcher_notices_exit_and_keeps_looping(monkeypatch):
    registry = fresh_registry()
    container = FakeContainer({"/exit_code": "EXIT 3\nSTAMP "})
    proc = ProcessRecord(id="pw", command="x", dir="/d")
    registry.processes["pw"] = proc
    monkeypatch.setattr(processes, "WATCHER_POLL_INTERVAL_S", 0.01)
    finished = []
    real_finalize = processes._finalize_process

    def one_shot_finalize(reg, record, marker, stamp=""):
        real_finalize(reg, record, marker)
        finished.append(True)
        registry.active = False

    monkeypatch.setattr(processes, "_finalize_process", one_shot_finalize)
    processes._watch_processes(registry, container)
    assert proc.state == "exited" and proc.exit_code == 3
    assert not registry.watcher_started, "a dropped registry resets the flag"


def test_watcher_stops_when_the_pod_dies(monkeypatch):
    registry = fresh_registry()

    class DeadPod:
        def exec(self, argv):
            raise RuntimeError("engine gone")

    proc = ProcessRecord(id="pw", command="x", dir="/d")
    registry.processes["pw"] = proc
    monkeypatch.setattr(time, "sleep", lambda s: None)
    processes._watch_processes(registry, DeadPod())
    assert proc.state == "running"
    assert registry.watcher_started, "a dead pod ends the watcher for good"


def test_watcher_survives_a_transient_poll_failure(monkeypatch):
    """One failed probe must not silence notices for the session.

    Live-observed in a real session: a poll that raced other pod work
    raised, the watcher declared the pod gone and stopped, and a
    finished process never delivered its completion notice.
    """
    registry = fresh_registry()
    proc = ProcessRecord(id="pw", command="x", dir="/d")
    registry.processes["pw"] = proc
    monkeypatch.setattr(processes, "WATCHER_POLL_INTERVAL_S", 0.01)

    class FlakyPod:
        def __init__(self):
            self.calls = 0

        def exec(self, argv):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("CannotSendRequest on a busy connection")
            return "EXIT 0\nSTAMP "

    pod = FlakyPod()
    finished = []
    real_finalize = processes._finalize_process

    def one_shot_finalize(reg, record, marker, stamp=""):
        real_finalize(reg, record, marker)
        finished.append(True)
        registry.active = False

    monkeypatch.setattr(processes, "_finalize_process", one_shot_finalize)
    processes._watch_processes(registry, pod)
    assert finished and proc.state == "exited" and proc.exit_code == 0
    assert pod.calls >= 2, "the watcher retried after the failure"


def test_watcher_picks_up_a_process_spawned_later(monkeypatch):
    """The race the live test caught: a spawn after an exit must be seen.

    The watcher keeps looping for the registry's lifetime, so a second
    spawn is always picked up even when it lands right after a previous
    process finished.
    """
    registry = fresh_registry()
    first = ProcessRecord(id="p1", command="x", dir="/d1")
    registry.processes["p1"] = first
    monkeypatch.setattr(processes, "WATCHER_POLL_INTERVAL_S", 0.0)
    poll = [0]
    seen = set()

    def fake_sleep(seconds):
        poll[0] += 1
        if poll[0] == 1:
            first.state = "exited"
            first.exit_code = 0
        elif poll[0] == 2:
            second = ProcessRecord(id="p2", command="x", dir="/d2")
            registry.processes["p2"] = second
        elif poll[0] == 4:
            registry.active = False

    monkeypatch.setattr(time, "sleep", fake_sleep)

    def fake_finalize(reg, record, marker, stamp=""):
        record.state = "exited"
        record.exit_code = int(marker)
        if record.id == "p2":
            seen.add("second-finalized")

    monkeypatch.setattr(processes, "_finalize_process", fake_finalize)
    processes._watch_processes(
        registry, FakeContainer({"/d2/exit_code": "EXIT 7\nSTAMP "})
    )
    assert first.exit_code == 0
    assert "second-finalized" in seen


def test_watcher_never_misreads_the_stamp_as_the_exit_code(monkeypatch):
    """A poll shape without an exit marker finalizes nothing.

    The space-partition parse this replaces would have read a bare
    stamp as the exit code when exit_code was missing; the marker
    parse finalizes only on a real EXIT line.
    """
    registry = fresh_registry()
    proc = ProcessRecord(id="pw", command="x", dir="/d")
    registry.processes["pw"] = proc
    monkeypatch.setattr(processes, "WATCHER_POLL_INTERVAL_S", 0.01)
    finalized = []
    monkeypatch.setattr(processes, "_finalize_process", lambda *a: finalized.append(a))
    polls = [0]

    def fake_sleep(seconds):
        polls[0] += 1
        if polls[0] >= 3:
            registry.active = False

    monkeypatch.setattr(time, "sleep", fake_sleep)

    class StampOnlyPod:
        def exec(self, argv):
            return "STAMP 1794185405"

    processes._watch_processes(registry, StampOnlyPod())
    assert finalized == [], "a stamp without an exit marker is not an outcome"
    assert proc.state == "running"


def test_registry_for_is_stable_per_container():
    container = FakeContainer()
    registry = processes.registry_for(container)
    assert processes.registry_for(container) is registry
    processes.drop_registry(container)
    assert processes.registry_for(container) is not registry


def test_wait_for_deadline_composition(monkeypatch):
    from syna.capabilities import tools as _  # noqa: F401  (registry import side effects)

    monkeypatch.setattr(processes, "load_wait_for_timeout", lambda: 120)
    assert processes.wait_for_deadline_s(None) == 130.0
    assert processes.wait_for_deadline_s({"max_wait_s": 30}) == 40.0
    assert processes.wait_for_deadline_s({"max_wait_s": 99999}) == 3610.0
    # Invalid and hostile values fall back to the default budget; the
    # tool body rejects them, the deadline never amplifies them.
    assert processes.wait_for_deadline_s({"max_wait_s": "bogus"}) == 130.0
    assert processes.wait_for_deadline_s({"max_wait_s": -5}) == 130.0
    assert processes.wait_for_deadline_s({"max_wait_s": 0}) == 130.0
    assert processes.wait_for_deadline_s({"max_wait_s": True}) == 130.0
    assert processes.requested_wait_budget_s({"max_wait_s": 2.0}) == 2


def main():
    run_suite(
        [
            test_id_validation_accepts_and_rejects,
            test_ids_are_unique_across_kinds_and_never_released,
            test_auto_ids_skip_taken_and_count_per_kind,
            test_spawn_registers_and_starts_watcher,
            test_spawn_failure_releases_the_id,
            test_spawn_stamps_the_parent_record,
            test_check_reports_state_and_tails,
            test_tail_script_caps_bytes_and_frames_base64,
            test_kill_signals_the_group_and_keeps_artifacts,
            test_kill_on_finished_process_reports_snapshot,
            test_unknown_id_error_explains_recovery,
            test_finalize_fires_one_completion_notice,
            test_completion_payload_carries_the_parent_id,
            test_completion_notice_text_carries_paths,
            test_finished_reports_use_the_recorded_end_time,
            test_observe_end_time_adopts_the_first_finished_sighting,
            test_adopt_end_time_prefers_the_pod_stamp,
            test_finalize_uses_the_launcher_stamp,
            test_check_process_state_parses_the_end_stamp,
            test_spawn_markers_parse_pgid_and_started_stamp,
            test_spawn_markers_survive_a_date_without_nanoseconds,
            test_stamp_parsing_rejects_non_finite_values,
            test_ended_runtime_prefers_the_pod_clock_stamps,
            test_watcher_notices_exit_and_keeps_looping,
            test_watcher_stops_when_the_pod_dies,
            test_watcher_survives_a_transient_poll_failure,
            test_watcher_picks_up_a_process_spawned_later,
            test_watcher_never_misreads_the_stamp_as_the_exit_code,
            test_registry_for_is_stable_per_container,
            test_wait_for_deadline_composition,
        ]
    )


if __name__ == "__main__":
    main()
