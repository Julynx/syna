"""Pod initialization steps run on container creation.

Each step is a small asset script executed once, right after the pod
starts and before it is handed to a session, so the model never sees a
half-initialized pod. A step is one asset file plus one entry in
`POD_INIT_STEPS`: `create_container()` runs every enabled step through
`run_pod_init_steps()` in order.

Steps fail open. A failing step is logged with its output tail and the
pod is handed over anyway: a degraded pod is more useful than no pod,
and every script is written so partial completion leaves the pod's
previous behavior intact.
"""

import base64
import struct
import time
from urllib.parse import urlparse

from bollard import Container

from .config import (
    get_logger,
    load_pod_doh_bootstrap,
    load_pod_doh_bootstrap_alt,
    load_pod_doh_uri,
)
from .paths import asset_path

SUCCESS_MARKER = "pod-init: step OK"


class PodInitStep:
    """One named initialization script with its configuration inputs.

    `asset` is the file name inside the assets folder; `env` maps to the
    environment variables the script reads, and `enabled` decides whether
    the step runs at all, so a config value can switch a step off without
    removing it.
    """

    def __init__(self, name: str, asset: str, env: dict[str, str], enabled: bool):
        self.name = name
        self.asset = asset
        self.env = env
        self.enabled = enabled


def doh_stamp(doh_uri: str, bootstrap: str) -> str:
    """Encode the dnscrypt-proxy stamp for the configured DoH resolver.

    Layout per go-dnsstamps' DoH stamp: protocol byte (0x02), an 8-byte
    little-endian properties word (DNSSEC, NoLog, NoFilter), a
    length-prefixed bootstrap IP that permanently pins the connection
    away from the poisoned resolver path, an empty hash list (the
    certificate validates through the system CA store, so resolver
    certificate rotations cannot break the pod), the length-prefixed
    provider name with its port, and the query path.
    """
    parsed = urlparse(doh_uri)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("the DoH resolver URI must be an https URL")
    provider = f"{parsed.hostname}:{parsed.port or 443}"
    path = parsed.path or "/dns-query"
    stamp = bytearray()
    stamp.append(0x02)
    stamp += struct.pack("<Q", 0b111)
    if bootstrap:
        stamp.append(len(bootstrap))
        stamp += bootstrap.encode()
    else:
        stamp.append(0)
    stamp.append(0)
    stamp.append(len(provider))
    stamp += provider.encode()
    stamp.append(len(path))
    stamp += path.encode()
    return "sdns://" + base64.urlsafe_b64encode(bytes(stamp)).decode().rstrip("=")


def _doh_step() -> PodInitStep:
    """The DoH step, configured from the pod_doh_* configuration keys.

    The primary stamp pins `pod_doh_bootstrap`; `pod_doh_bootstrap_alt`
    registers a second entry on the same provider for resolver
    failover. Both pins must belong to the provider in `pod_doh_uri`.
    When no bootstrap IP is configured, the primary stamp carries no
    address pin and the script gets a bootstrap_resolvers line so
    dnscrypt-proxy can still resolve the provider hostname without the
    poisoned system resolver.
    """
    doh_uri = load_pod_doh_uri()
    bootstrap = load_pod_doh_bootstrap()
    bootstrap_alt = load_pod_doh_bootstrap_alt()
    env = {}
    if doh_uri:
        env["DOH_STAMP"] = doh_stamp(doh_uri, bootstrap)
        if bootstrap_alt and bootstrap_alt != bootstrap:
            env["DOH_STAMP_ALT"] = doh_stamp(doh_uri, bootstrap_alt)
        if not bootstrap:
            env["DOH_NEEDS_BOOTSTRAP"] = "1"
    return PodInitStep(
        name="doh",
        asset="pod_doh_setup.sh",
        env=env,
        enabled=bool(doh_uri),
    )


def pod_init_steps() -> list[PodInitStep]:
    """Every initialization step, in execution order."""
    return [_doh_step()]


def step_command(step: PodInitStep) -> list[str]:
    """The exec command that runs one step's script inside the pod.

    The script travels base64-encoded so the container's shell never
    sees the host's quoting.
    """
    script = asset_path(step.asset).read_text(encoding="utf-8")
    script = base64.b64encode(script.encode()).decode()
    env_prefix = " ".join(f"{key}='{value}'" for key, value in step.env.items())
    inner = (
        f"echo {script} | base64 -d > /tmp/syna_pod_init_{step.name}.sh"
        f" && {env_prefix} bash /tmp/syna_pod_init_{step.name}.sh"
    )
    return ["bash", "-c", inner]


def run_pod_init_steps(container: Container) -> dict[str, bool]:
    """Run every enabled step on a fresh pod; report which went active.

    A step is active when its output carries the success marker; steps
    that error or finish without the marker are logged with the tail of
    their output and skipped.
    """
    results: dict[str, bool] = {}
    for step in pod_init_steps():
        if not step.enabled:
            results[step.name] = False
            continue
        started = time.monotonic()
        try:
            output = container.exec(step_command(step))
        except Exception as exc:
            get_logger().warning(
                "Pod init step '%s' errored; the pod continues without it (%s).",
                step.name,
                exc,
            )
            results[step.name] = False
            continue
        elapsed = time.monotonic() - started
        if SUCCESS_MARKER not in output:
            tail = " ".join(output.split())[-300:]
            get_logger().warning(
                "Pod init step '%s' did not activate after %.1fs; the pod"
                " continues without it (%s).",
                step.name,
                elapsed,
                tail,
            )
            results[step.name] = False
            continue
        get_logger().info("Pod init step '%s' active in %.1fs.", step.name, elapsed)
        results[step.name] = True
    return results
