"""Unit tests for the server's local-origin and token enforcement."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite
from fastapi.testclient import TestClient

from syna import server

LOCAL_HOST = {"Host": "127.0.0.1:8000"}


def test_api_open_without_token(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", None)
    client = TestClient(server.app)

    response = client.get("/api/files/unknown/unknown")

    assert response.status_code == 404


def test_api_requires_token(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)

    denied = client.get("/api/files/unknown/unknown", headers=LOCAL_HOST)
    allowed = client.get(
        "/api/files/unknown/unknown",
        headers={**LOCAL_HOST, "X-Syna-Token": "secret123"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 404


def test_api_accepts_token_from_query(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)

    response = client.get(
        "/api/files/unknown/unknown?token=secret123", headers=LOCAL_HOST
    )

    assert response.status_code == 404


def test_foreign_host_header_is_rejected(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)

    response = client.get(
        "/api/files/unknown/unknown",
        headers={"Host": "rebind.evil.example", "X-Syna-Token": "secret123"},
    )

    assert response.status_code == 403


def test_localhost_host_header_is_accepted(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)

    response = client.get(
        "/api/files/unknown/unknown",
        headers={"Host": "localhost:8000", "X-Syna-Token": "secret123"},
    )

    assert response.status_code == 404


def test_shell_and_vendor_stay_open(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)

    shell = client.get("/", headers=LOCAL_HOST)
    vendor = client.get("/vendor/missing.js", headers=LOCAL_HOST)

    assert shell.status_code == 200
    assert "secret123" in shell.text
    assert vendor.status_code == 404


def _assert_ws_denied(client, path, headers=None, why=""):
    """Connect expecting the handshake to be denied (HTTP 403 / close 1008)."""
    try:
        with client.websocket_connect(path, headers=headers or {}):
            pass
    except Exception:
        return
    raise AssertionError(why or "the WebSocket handshake must have been denied")


def test_ws_rejects_foreign_origin(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)
    _assert_ws_denied(
        client,
        "/ws/authtest?token=secret123",
        headers={"Origin": "http://rebind.evil.example"},
        why="a foreign Origin must not open a WebSocket",
    )


def test_ws_rejects_other_local_ports(monkeypatch):
    """Another loopback port is a different application, not this UI.

    The old any-loopback-port regex let a page on localhost:9999 (a dev
    server, an XSS'd local app) pass the origin check.
    """
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)
    _assert_ws_denied(
        client,
        "/ws/authtest?token=secret123",
        headers={"Origin": "http://localhost:9999"},
        why="another local port's origin must not open a WebSocket",
    )


def test_ws_accepts_the_pinned_origin(monkeypatch):
    """The server's own UI origin still opens WebSockets."""
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)

    try:
        with client.websocket_connect(
            "/ws/authtest?token=secret123",
            headers={"Origin": server.TRUSTED_ORIGINS[0]},
        ):
            pass
    except Exception as exc:
        raise AssertionError(f"the server's own origin must open a WebSocket: {exc}")


def test_cors_headers_are_pinned_to_the_own_origin():
    """Cross-origin reads of the token-embedding shell are limited to the UI."""
    client = TestClient(server.app)
    pinned = server.TRUSTED_ORIGINS[0]

    allowed = client.get("/", headers={"Origin": pinned})
    assert allowed.headers.get("access-control-allow-origin") == pinned, (
        "the server's own UI origin must be able to read the HTML shell"
    )

    foreign = client.get("/", headers={"Origin": "http://localhost:9999"})
    assert "access-control-allow-origin" not in foreign.headers, (
        "another local port must not be able to read the HTML shell,"
        " which embeds the auth token"
    )


def test_ws_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret123")
    client = TestClient(server.app)
    _assert_ws_denied(
        client, "/ws/authtest", why="a missing token must not open a WebSocket"
    )


def main():
    """Run every server-auth test and report a summary."""
    run_suite(
        [
            test_api_open_without_token,
            test_api_requires_token,
            test_api_accepts_token_from_query,
            test_foreign_host_header_is_rejected,
            test_localhost_host_header_is_accepted,
            test_shell_and_vendor_stay_open,
            test_ws_rejects_foreign_origin,
            test_ws_rejects_other_local_ports,
            test_ws_accepts_the_pinned_origin,
            test_cors_headers_are_pinned_to_the_own_origin,
            test_ws_rejects_missing_token,
        ]
    )


if __name__ == "__main__":
    main()
