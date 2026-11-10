"""Tests for the pod's initialization steps and the DoH stamp builder.

The stamp builder must match go-dnsstamps' DoH layout exactly
(protocol byte, 8-byte little-endian properties, length-prefixed
bootstrap IP, a variable-length-prefixed hash list that ends at a zero
length, the provider name with its port, and the path); an official
Quad9 stamp decodes under the same reader. The pod creation path gates
each step on its configuration keys and fails open.
"""

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import pod_init, state
from syna.capabilities import rewind
from syna.config import (
    load_pod_doh_bootstrap,
    load_pod_doh_bootstrap_alt,
    load_pod_doh_uri,
)

OFFICIAL_DNS10_STAMP = (
    "sdns://AgcAAAAAAAAACDkuOS45LjEwILAZIHRLu3bJqwU-AeB7fgUORz0g95976kNfr-Q8nSQ"
    "vE2RuczEwLnF1YWQ5Lm5ldDo0NDMKL2Rucy1xdWVyeQ"
)


def decode_doh_stamp(stamp: str):
    """Decode a DoH stamp back into its fields, mirroring go-dnsstamps."""
    body = stamp.removeprefix("sdns://")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    assert raw[0] == 0x02
    props = int.from_bytes(raw[1:9], "little")
    pos = 9
    addr_len = raw[pos]
    addr = raw[pos + 1 : pos + 1 + addr_len]
    pos += 1 + addr_len
    hashes = []
    while True:
        vlen = raw[pos]
        pos += 1
        length = vlen & 0x7F
        if length:
            hashes.append(raw[pos : pos + length])
        pos += length
        if not vlen & 0x80:
            break
    name_len = raw[pos]
    provider = raw[pos + 1 : pos + 1 + name_len]
    pos += 1 + name_len
    path_len = raw[pos]
    path = raw[pos + 1 : pos + 1 + path_len]
    pos += 1 + path_len
    return props, addr, hashes, provider, path, raw[pos:]


def test_stamp_layout_matches_godnsstamps():
    stamp = pod_init.doh_stamp("https://dns10.quad9.net/dns-query", "9.9.9.10")
    props, addr, hashes, provider, path, rest = decode_doh_stamp(stamp)
    assert props == 0b111
    assert addr == b"9.9.9.10"
    assert hashes == []
    assert provider == b"dns10.quad9.net:443"
    assert path == b"/dns-query"
    assert rest == b""


def test_stamp_decodes_the_official_quad9_stamp():
    props, addr, hashes, provider, path, rest = decode_doh_stamp(OFFICIAL_DNS10_STAMP)
    assert props == 0b111
    assert addr == b"9.9.9.10"
    assert len(hashes) == 1 and len(hashes[0]) == 32
    assert provider == b"dns10.quad9.net:443"
    assert path == b"/dns-query"
    assert rest == b""


def test_stamp_without_bootstrap_pin():
    stamp = pod_init.doh_stamp("https://dns.example.com:5353/query", "")
    props, addr, hashes, provider, path, rest = decode_doh_stamp(stamp)
    assert props == 0b111
    assert addr == b""
    assert hashes == []
    assert provider == b"dns.example.com:5353"
    assert path == b"/query"
    assert rest == b""


def test_non_https_uri_is_rejected():
    try:
        pod_init.doh_stamp("http://dns.example.com/dns-query", "9.9.9.10")
    except ValueError:
        return
    raise AssertionError("http:// must be rejected")


def test_bundled_pod_defaults():
    assert load_pod_doh_uri() == "https://dns10.quad9.net/dns-query"
    assert load_pod_doh_bootstrap() == "9.9.9.10"
    assert load_pod_doh_bootstrap_alt() == "149.112.112.10"


def test_doh_step_configured_from_keys(monkeypatch):
    monkeypatch.setattr(pod_init, "load_pod_doh_uri", lambda: "")
    assert pod_init.pod_init_steps()[0].enabled is False
    monkeypatch.setattr(
        pod_init, "load_pod_doh_uri", lambda: "https://dns10.quad9.net/dns-query"
    )
    monkeypatch.setattr(pod_init, "load_pod_doh_bootstrap", lambda: "9.9.9.10")
    monkeypatch.setattr(
        pod_init, "load_pod_doh_bootstrap_alt", lambda: "149.112.112.10"
    )
    step = pod_init.pod_init_steps()[0]
    assert step.enabled is True
    assert step.env["DOH_STAMP"].startswith("sdns://")
    props, addr, _, provider, _, _ = decode_doh_stamp(step.env["DOH_STAMP_ALT"])
    assert addr == b"149.112.112.10"
    assert provider == b"dns10.quad9.net:443"
    assert "DOH_NEEDS_BOOTSTRAP" not in step.env


def test_doh_step_without_bootstrap_pin_requests_bootstrap_resolvers(monkeypatch):
    monkeypatch.setattr(
        pod_init, "load_pod_doh_uri", lambda: "https://dns.example.com:5353/query"
    )
    monkeypatch.setattr(pod_init, "load_pod_doh_bootstrap", lambda: "")
    monkeypatch.setattr(pod_init, "load_pod_doh_bootstrap_alt", lambda: "")
    step = pod_init.pod_init_steps()[0]
    assert step.env["DOH_NEEDS_BOOTSTRAP"] == "1"
    assert "DOH_STAMP_ALT" not in step.env


def test_step_command_carries_script_and_stamp():
    step = pod_init.PodInitStep(
        name="doh",
        asset="pod_doh_setup.sh",
        env={"DOH_STAMP": "sdns://abc", "DOH_STAMP_ALT": "sdns://def"},
        enabled=True,
    )
    command = pod_init.step_command(step)
    assert command[0] == "bash" and command[1] == "-c"
    inner = command[2]
    assert (
        "base64 -d" in inner
        and "DOH_STAMP='sdns://abc'" in inner
        and "DOH_STAMP_ALT='sdns://def'" in inner
    )
    b64 = inner.split("echo ")[1].split(" |")[0]
    script = base64.b64decode(b64).decode()
    assert script == pod_init.asset_path(step.asset).read_text(encoding="utf-8")
    assert "dnscrypt-proxy" in script
    assert "/root/.curlrc" in script and "http3" in script
    assert "syna-doh-alt" in script and "bootstrap_resolvers" in script


class _SentinelContainer:
    """A container stand-in that records exec calls or raises if touched."""

    def __init__(self, outputs=None):
        self.outputs = outputs
        self.calls = 0

    def exec(self, argv):
        if self.outputs is None:
            raise AssertionError("the pod was touched")
        self.calls += 1
        return self.outputs.pop(0)


def test_disabled_step_never_touches_the_pod(monkeypatch):
    monkeypatch.setattr(pod_init, "load_pod_doh_uri", lambda: "")
    results = pod_init.run_pod_init_steps(_SentinelContainer())
    assert results["doh"] is False
    assert results["rewind"] is False


def test_steps_fail_open(monkeypatch):
    monkeypatch.setattr(
        pod_init, "load_pod_doh_uri", lambda: "https://dns10.quad9.net/dns-query"
    )
    monkeypatch.setattr(pod_init, "load_pod_doh_bootstrap", lambda: "9.9.9.10")
    ok = _SentinelContainer(
        [f"noise\n{pod_init.SUCCESS_MARKER}\n", f"noise\n{pod_init.SUCCESS_MARKER}\n"]
    )
    assert pod_init.run_pod_init_steps(ok) == {"doh": True, "rewind": True}
    assert ok.calls == 2
    missing = _SentinelContainer(["apt-get failed"])
    assert pod_init.run_pod_init_steps(missing) == {"doh": False, "rewind": False}
    exploding = _SentinelContainer(None)
    assert pod_init.run_pod_init_steps(exploding) == {"doh": False, "rewind": False}


def test_create_container_runs_init_steps(monkeypatch):
    seen = {}

    class FakeContainer:
        def exec(self, argv):
            script = argv[-1]
            seen.setdefault("argvs", []).append(script)
            if "GIT_DIR" in script:
                return f"outer {'a' * 40}"
            seen["argv"] = argv
            return pod_init.SUCCESS_MARKER

    class FakeClient:
        def run_container(self, *args, **kwargs):
            seen["args"] = args
            return FakeContainer()

    monkeypatch.setattr(state, "client", None)
    monkeypatch.setattr(state, "init_docker_client", lambda: None)
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(pod_init, "get_data_home", lambda: Path(tempfile.mkdtemp()))
    container = state.create_container()
    assert seen["args"][0] == state.CONTAINER_IMAGE
    assert "base64 -d" in seen["argv"][2]
    assert container.exec(["true"]) == pod_init.SUCCESS_MARKER
    rewind.forget_pod(container)
    pod_init.forget_init_results(container)


def test_shared_dir_report_creates_the_directory(monkeypatch):
    home = Path(tempfile.mkdtemp())
    monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
    report = pod_init.shared_dir_report()
    assert (home / "shared").is_dir()
    assert report["entries"] == []
    assert report["pod_path"] == pod_init.POD_SHARED_DIR
    (home / "shared" / "notes").mkdir()
    (home / "shared" / "keys.txt").write_text("k", encoding="utf-8")
    report = pod_init.shared_dir_report()
    assert report["entries"] == ["keys.txt", "notes"]


class RecordingPod:
    """Exec and copy recorder standing in for the pod."""

    def __init__(self, fail_names=()):
        self.copy_calls = []
        self.exec_calls = []
        self.fail_names = fail_names

    def exec(self, argv):
        self.exec_calls.append(argv[-1])
        return ""

    def copy_to(self, source, dest):
        self.copy_calls.append((source, dest))
        if Path(source).name in self.fail_names:
            raise RuntimeError("pod gone")


def test_copy_shared_dir_copies_files_and_dirs(monkeypatch):
    home = Path(tempfile.mkdtemp())
    shared = home / "shared"
    shared.mkdir()
    (shared / "keys.txt").write_text("k", encoding="utf-8")
    (shared / "bundle").mkdir()
    (shared / "bundle" / "inner.txt").write_text("i", encoding="utf-8")
    monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
    pod = RecordingPod()
    report = pod_init.copy_shared_dir(pod)
    assert report["copied"] == ["bundle", "keys.txt"]
    assert report["host_path"] == shared
    assert report["pod_path"] == "/workspace/shared"
    assert any("mkdir -p /workspace/shared" in call for call in pod.exec_calls)
    assert [Path(src).name for src, _ in pod.copy_calls] == ["bundle", "keys.txt"]
    assert all(dest == "/workspace/shared" for _, dest in pod.copy_calls)


def test_copy_shared_dir_skips_empty_and_fails_open(monkeypatch):
    home = Path(tempfile.mkdtemp())
    (home / "shared").mkdir()
    monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
    pod = RecordingPod()
    assert pod_init.copy_shared_dir(pod) is None
    assert pod.copy_calls == []

    (home / "shared" / "good.txt").write_text("g", encoding="utf-8")
    (home / "shared" / "bad.txt").write_text("b", encoding="utf-8")
    report = pod_init.copy_shared_dir(RecordingPod(fail_names={"bad.txt"}))
    assert report["copied"] == ["good.txt"]

    class DeadPod:
        def exec(self, argv):
            raise RuntimeError("pod gone")

    assert pod_init.copy_shared_dir(DeadPod()) is None


def test_shared_copy_notice_counts_dirs_files_and_items(monkeypatch):
    home = Path(tempfile.mkdtemp())
    monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
    case = 0

    def report(names):
        nonlocal case
        case += 1
        shared = home / f"shared-{case}"
        shared.mkdir()
        for name in names:
            if name.endswith(".txt"):
                (shared / name).write_text("x", encoding="utf-8")
            else:
                (shared / name).mkdir()
        return {
            "host_path": shared,
            "pod_path": "/workspace/shared",
            "copied": list(names),
        }

    first = report(["notes"])
    assert pod_init.shared_copy_notice(first) == (
        f"Copied 1 dir from {first['host_path']} into /workspace/shared."
    )
    two_dirs = report(["a", "b"])
    assert pod_init.shared_copy_notice(two_dirs) == (
        f"Copied 2 dirs from {two_dirs['host_path']} into /workspace/shared."
    )
    two_files = report(["a.txt", "b.txt"])
    assert pod_init.shared_copy_notice(two_files) == (
        f"Copied 2 files from {two_files['host_path']} into /workspace/shared."
    )
    mixed = report(["a", "b.txt"])
    assert pod_init.shared_copy_notice(mixed) == (
        f"Copied 2 items from {mixed['host_path']} into /workspace/shared."
    )


def test_system_prompt_names_the_shared_directory(monkeypatch):
    import syna.conversation.session as session_module

    home = Path(tempfile.mkdtemp())
    shared = home / "shared"
    shared.mkdir()
    (shared / "keys.txt").write_text("k", encoding="utf-8")
    monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
    prompt = session_module.build_prompt()
    assert f"- {pod_init.POD_SHARED_DIR} contains files the user chose" in prompt
    assert "{{POD_SHARED_PATH}}" not in prompt
    assert "never authorizes it" in prompt


def test_system_prompt_drops_the_shared_bullet_when_empty(monkeypatch):
    import syna.conversation.session as session_module

    for with_empty_dir in (True, False):
        home = Path(tempfile.mkdtemp())
        if with_empty_dir:
            (home / "shared").mkdir()
        monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
        prompt = session_module.build_prompt()
        assert "contains files the user chose" not in prompt, prompt
        assert "{{POD_SHARED_PATH}}" not in prompt
        assert pod_init.POD_SHARED_DIR not in prompt


def test_create_container_delivers_the_shared_copy_sink(monkeypatch):
    seen = {}

    class FakeContainer:
        def exec(self, argv):
            if "GIT_DIR" in argv[-1]:
                return "outer " + "b" * 40
            return pod_init.SUCCESS_MARKER

        def copy_to(self, source, dest):
            seen.setdefault("copied", []).append((source, dest))

    class FakeClient:
        def run_container(self, *args, **kwargs):
            return FakeContainer()

    home = Path(tempfile.mkdtemp())
    shared = home / "shared"
    shared.mkdir()
    (shared / "keys.txt").write_text("k", encoding="utf-8")
    monkeypatch.setattr(state, "client", None)
    monkeypatch.setattr(state, "init_docker_client", lambda: None)
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(pod_init, "get_data_home", lambda: home)
    sink_reports = []
    state.create_container(shared_copy_sink=sink_reports.append)
    assert len(sink_reports) == 1
    assert sink_reports[0]["copied"] == ["keys.txt"]
    assert "Copied 1 file" in pod_init.shared_copy_notice(sink_reports[0])


def test_create_container_skips_the_sink_when_shared_is_empty(monkeypatch):
    class FakeContainer:
        def exec(self, argv):
            if "GIT_DIR" in argv[-1]:
                return "outer " + "c" * 40
            return pod_init.SUCCESS_MARKER

        def copy_to(self, source, dest):
            raise AssertionError("nothing may be copied from an empty dir")

    class FakeClient:
        def run_container(self, *args, **kwargs):
            return FakeContainer()

    monkeypatch.setattr(state, "client", None)
    monkeypatch.setattr(state, "init_docker_client", lambda: None)
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(pod_init, "get_data_home", lambda: Path(tempfile.mkdtemp()))
    sink_reports = []
    state.create_container(shared_copy_sink=sink_reports.append)
    assert sink_reports == []


def main():
    run_suite(
        [
            test_stamp_layout_matches_godnsstamps,
            test_stamp_decodes_the_official_quad9_stamp,
            test_stamp_without_bootstrap_pin,
            test_non_https_uri_is_rejected,
            test_bundled_pod_defaults,
            test_doh_step_configured_from_keys,
            test_doh_step_without_bootstrap_pin_requests_bootstrap_resolvers,
            test_step_command_carries_script_and_stamp,
            test_disabled_step_never_touches_the_pod,
            test_steps_fail_open,
            test_create_container_runs_init_steps,
            test_shared_dir_report_creates_the_directory,
            test_copy_shared_dir_copies_files_and_dirs,
            test_copy_shared_dir_skips_empty_and_fails_open,
            test_shared_copy_notice_counts_dirs_files_and_items,
            test_create_container_delivers_the_shared_copy_sink,
            test_create_container_skips_the_sink_when_shared_is_empty,
            test_system_prompt_names_the_shared_directory,
            test_system_prompt_drops_the_shared_bullet_when_empty,
        ]
    )


if __name__ == "__main__":
    main()
