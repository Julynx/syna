"""Tests for syna's engine-error summary.

The stale-engine-socket self-heal and the pipe probe moved into
bollard 0.3.0 (its discovery runs the heal before every probe and
raises a structured EndpointDiscoveryError), so syna only shapes the
user-facing message: bollard's actionable bring-up detail leads, and
generic virtualization advice stays dropped. Non-bollard-shaped errors
pass through unchanged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from bollard import EndpointDiscoveryError

from syna import state

BOLLARD_LAST_FAILURE = (
    "`podman machine start` failed: win-sshproxy.exe failed to start, exit code: 0"
)


def test_engine_error_text_leads_with_the_cause():
    exc = EndpointDiscoveryError(
        "No Docker or Podman API endpoint is reachable. The Podman"
        " machine could not be started. Check that virtualization is"
        " enabled and try running `podman machine start` in a terminal"
        " for details.",
        last_failure=BOLLARD_LAST_FAILURE,
    )
    message = state.engine_error_text(exc)
    assert message.startswith("The container engine is not reachable."), message
    assert BOLLARD_LAST_FAILURE in message, message
    assert "virtualization" not in message, message


def test_engine_error_text_passes_other_messages_through():
    plain = "Docker API Error (500): boom"
    assert state.engine_error_text(RuntimeError(plain)) == plain


def test_engine_error_text_passes_failureless_discovery_through():
    """Without a last_failure, bollard's own message is shown as-is.

    Real failureless Windows errors carry the generic virtualization
    advice; there is no cause to lead with, so the text passes
    through unchanged.
    """
    exc = EndpointDiscoveryError(
        "No Docker or Podman API endpoint is reachable. The Podman"
        " machine could not be started. Check that virtualization is"
        " enabled and try running `podman machine start` in a terminal"
        " for details."
    )
    assert state.engine_error_text(exc) == str(exc)


if __name__ == "__main__":
    run_suite(
        [
            test_engine_error_text_leads_with_the_cause,
            test_engine_error_text_passes_other_messages_through,
            test_engine_error_text_passes_failureless_discovery_through,
        ]
    )
