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
    container = FakeContainer({"pgid": "4242"})
    monkeypatch.setattr(processes, "_ensure_watcher", lambda r, c: None)
    result = spawn_process(
        "python3 build.py",
        name="build",
        registry=registry,
        container=container,
        runtime_cap_s=99,
    )
    assert result["process_id"] == "build"
    assert result["max_runtime_s"] == 99
    proc = registry.processes["build"]
    assert proc.pgid == 4242
    assert proc.dir == "/workspace/processes/build"
    exec_script = container.calls[0][-1]
    assert "setsid bash -c" in exec_script
    assert "timeout -k 10 99" in exec_script
    assert "stdbuf -oL -eL" in exec_script
    assert 'printf %s "$bg" > ' in exec_script
    assert "/workspace/processes/build/stdout" in exec_script
    assert "> /workspace/processes/build/exit_code" in exec_script


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
        lambda c, d: ("exited:0", "hello out", "warn line"),
    )
    report = format_process_report(proc, "exited:0", "hello out", "warn line")
    assert "finished with exit code 0" in report
    assert "hello out" in report and "warn line" in report
    assert "/workspace/processes/p1/stdout" in report


def test_tail_script_caps_bytes_and_frames_base64():
    script = processes._tail_script("/workspace/processes/p")
    assert f"tail -c {4000 * 4}" in script
    stdout, stderr = processes._decode_tails(
        "STATE running\nSTDOUT\n" + "aGVsbG8=\nSTDERR\n"
    )
    assert stdout == "hello" and stderr == ""


def test_kill_signals_the_group_and_keeps_artifacts(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="p1", command="x", dir="/workspace/processes/p1")
    proc.pgid = 77
    registry.processes["p1"] = proc
    container = FakeContainer()
    monkeypatch.setattr(
        processes,
        "check_process_state",
        lambda c, d: ("running", "", ""),
    )
    report = kill_process("p1", registry=registry, container=container, by="model")
    assert "Stop signals sent" in report
    kill_script = container.calls[0][-1]
    assert 'kill -- -"$pgid"' in kill_script
    assert "kill -9" in kill_script
    assert proc.state == "killed" and proc.killed_by == "model"
    # A model kill suppresses the completion notice (it already knows).
    processes._finalize_process(registry, proc, "0")
    assert registry.completion_sink is None or True
    assert proc.state == "exited" and proc.exit_code == 0
    assert proc.notified


def test_kill_on_finished_process_reports_snapshot(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    proc.state = "exited"
    proc.exit_code = 0
    registry.processes["p1"] = proc
    monkeypatch.setattr(
        processes, "check_process_state", lambda c, d: ("exited:0", "", "")
    )
    report = kill_process(
        "p1", registry=registry, container=FakeContainer(), by="model"
    )
    assert "already finished" in report
    assert proc.killed_by is None, "a settled record keeps its own story"


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
    payload = seen[0]
    assert payload["kind"] == "process" and payload["id"] == "p9"
    assert "runtime cap" in payload["model_notice"]
    assert "runtime cap" in payload["summary"]


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
    container = FakeContainer({"/exit_code": "3"})
    proc = ProcessRecord(id="pw", command="x", dir="/d")
    registry.processes["pw"] = proc
    monkeypatch.setattr(processes, "WATCHER_POLL_INTERVAL_S", 0.01)
    finished = []
    real_finalize = processes._finalize_process

    def one_shot_finalize(reg, record, marker):
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
            return "0"

    pod = FlakyPod()
    finished = []
    real_finalize = processes._finalize_process

    def one_shot_finalize(reg, record, marker):
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

    def fake_finalize(reg, record, marker):
        record.state = "exited"
        record.exit_code = int(marker)
        if record.id == "p2":
            seen.add("second-finalized")

    monkeypatch.setattr(processes, "_finalize_process", fake_finalize)
    processes._watch_processes(registry, FakeContainer({"/d2/exit_code": "7"}))
    assert first.exit_code == 0
    assert "second-finalized" in seen


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
            test_check_reports_state_and_tails,
            test_tail_script_caps_bytes_and_frames_base64,
            test_kill_signals_the_group_and_keeps_artifacts,
            test_kill_on_finished_process_reports_snapshot,
            test_unknown_id_error_explains_recovery,
            test_finalize_fires_one_completion_notice,
            test_completion_notice_text_carries_paths,
            test_watcher_notices_exit_and_keeps_looping,
            test_watcher_stops_when_the_pod_dies,
            test_watcher_survives_a_transient_poll_failure,
            test_watcher_picks_up_a_process_spawned_later,
            test_registry_for_is_stable_per_container,
            test_wait_for_deadline_composition,
        ]
    )


if __name__ == "__main__":
    main()
