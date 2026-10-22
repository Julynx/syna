"""Unit tests for the browser network guard: public internet only."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite
from syna import net_guard
from syna.browser import BrowserSession, ToolError


class FakeRoute:
    """Records which of the guard's route outcomes fired."""

    def __init__(self, url):
        self.request = type("Request", (), {"url": url})()
        self.fulfilled = False
        self.aborted = False
        self.continued = False

    async def fulfill(self, **kwargs):
        self.fulfilled = True

    async def abort(self, code):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class FakeWebSocket:
    """Records whether the guard closed the socket."""

    def __init__(self, url):
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True


def test_literal_private_ips_are_blocked():
    for host in [
        "127.0.0.1",
        "[::1]",
        "::1",
        "::ffff:127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "fc00::1",
        "fe80::1",
    ]:
        decision = net_guard.check_destination(host)
        assert not decision.allowed, host
        assert decision.reason, host


def test_obfuscated_ipv4_forms_are_blocked():
    for host in [
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
        "127.1",
        "127.0.0.1.",
        "LOCALHOST",
    ]:
        decision = net_guard.check_destination(host)
        assert not decision.allowed, host


def test_url_spec_shorthand_bypasses_are_blocked():
    """Shortened dotted forms per the WHATWG URL IPv4 parser.

    The last label carries all remaining bits, so these are loopback or
    private addresses in the browser even though they look like ordinary
    hostnames. Reasons must name the real range — blocking via a
    mis-parse into 0.0.0.0/8 would be luck, not correctness.
    """
    expectations = {
        "127.65535": "loopback",
        "127.526344": "loopback",
        "0x7f.1": "loopback",
        "10.524288": "private",
        "172.16.257": "private",
        "10.0.1": "private",
    }
    for host, expected_reason in expectations.items():
        decision = net_guard.check_destination(host)
        assert not decision.allowed, host
        assert expected_reason in decision.reason, (host, decision.reason)


def test_public_short_forms_are_not_blocked():
    for host in ["8.8", "1.2.3", "6.6", "134744072"]:
        decision = net_guard.check_destination(host)
        assert decision.allowed, (host, decision.reason)


def test_public_ips_are_allowed():
    for host in ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700::1111"]:
        assert net_guard.check_destination(host).allowed, host


def test_hostname_resolving_private_is_blocked(monkeypatch):
    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)
    net_guard._dns_cache.clear()

    decision = net_guard.check_destination("rebind.evil.example")
    assert not decision.allowed
    assert "127.0.0.1" in decision.reason


def test_hostname_resolving_public_is_allowed(monkeypatch):
    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)
    net_guard._dns_cache.clear()

    assert net_guard.check_destination("example.com").allowed


def test_unresolvable_hostname_is_allowed(monkeypatch):
    def fake_getaddrinfo(host, port):
        raise OSError("name resolution failed")

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)
    net_guard._dns_cache.clear()

    assert net_guard.check_destination("no-such-host.invalid").allowed


def test_resolution_is_cached(monkeypatch):
    calls = []

    def fake_getaddrinfo(host, port):
        calls.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)
    net_guard._dns_cache.clear()

    net_guard.check_destination("cached.example")
    net_guard.check_destination("cached.example")

    assert len(calls) == 1


def test_allowed_hosts_override(monkeypatch):
    monkeypatch.setattr(
        net_guard,
        "load_config",
        lambda: {"browser_allowed_hosts": ["LocalHost"]},
    )

    assert net_guard.check_destination("localhost").allowed
    assert not net_guard.check_destination("127.0.0.1").allowed


def test_allow_private_networks_toggle(monkeypatch):
    monkeypatch.setattr(
        net_guard,
        "load_config",
        lambda: {"browser_allow_private_networks": True},
    )

    assert net_guard.check_destination("127.0.0.1").allowed


def test_check_url_scheme_handling():
    blocked = net_guard.check_url("http://127.0.0.1:8000/")
    assert not blocked.allowed

    ws_blocked = net_guard.check_url("ws://127.0.0.1:8000/ws")
    assert not ws_blocked.allowed

    assert net_guard.check_url("data:text/plain,hello").allowed
    assert net_guard.check_url("about:blank").allowed
    assert net_guard.check_url("https://example.com/page").allowed


def test_goto_blocks_private_urls_before_touching_the_page():
    session = BrowserSession()

    async def attempt():
        return await session.tool_goto("http://127.0.0.1:8000/")

    try:
        asyncio.run(attempt())
    except ToolError as exc:
        assert "blocked" in str(exc)
        assert "public internet" in str(exc)
    else:
        raise AssertionError("goto to loopback must be blocked")


def test_guard_route_neutralizes_public_script_hosts():
    session = BrowserSession()
    route = FakeRoute("https://fundingchoicesmessages.google.com/boot.js")

    asyncio.run(session._guard_route(route))

    assert route.fulfilled and not route.aborted and not route.continued


def test_guard_route_neutralizes_wildcard_host_and_scoped_path():
    session = BrowserSession()
    wildcard = FakeRoute("https://tracker.id5-sync.com/frame.js")
    scoped_hit = FakeRoute(
        "https://script.wikia.nocookie.net/fandom-ae-assets/identity-service/x.js"
    )
    scoped_miss = FakeRoute("https://script.wikia.nocookie.net/other.js")

    asyncio.run(session._guard_route(wildcard))
    asyncio.run(session._guard_route(scoped_hit))
    asyncio.run(session._guard_route(scoped_miss))

    assert wildcard.fulfilled
    assert scoped_hit.fulfilled
    assert scoped_miss.continued and not scoped_miss.fulfilled


def test_guard_route_leaves_lookalike_urls_alone():
    """A query-string lookalike must not neutralize an innocent page."""
    session = BrowserSession()
    route = FakeRoute("https://bank.com/?x=http://sub.id5-sync.com/y")

    asyncio.run(session._guard_route(route))

    assert route.continued and not route.fulfilled and not route.aborted


def test_guard_route_continues_public_and_aborts_private():
    session = BrowserSession()
    public = FakeRoute("https://example.com/style.css")
    private = FakeRoute("http://127.0.0.1:8000/api/files/x/y")

    asyncio.run(session._guard_route(public))
    asyncio.run(session._guard_route(private))

    assert public.continued and not public.aborted
    assert private.aborted and not private.continued


def test_websocket_observer_kills_private_connections():
    session = BrowserSession()

    async def scenario():
        fake = FakeWebSocket("ws://127.0.0.1:8000/ws")
        session._on_websocket(fake)
        await asyncio.sleep(0)
        return fake

    assert asyncio.run(scenario()).closed


def test_websocket_observer_leaves_public_connections():
    session = BrowserSession()

    async def scenario():
        fake = FakeWebSocket("wss://example.com/socket")
        session._on_websocket(fake)
        await asyncio.sleep(0)
        return fake

    assert not asyncio.run(scenario()).closed


def main():
    """Run every network-guard test and report a summary."""
    run_suite(
        [
            test_literal_private_ips_are_blocked,
            test_obfuscated_ipv4_forms_are_blocked,
            test_url_spec_shorthand_bypasses_are_blocked,
            test_public_short_forms_are_not_blocked,
            test_public_ips_are_allowed,
            test_hostname_resolving_private_is_blocked,
            test_hostname_resolving_public_is_allowed,
            test_unresolvable_hostname_is_allowed,
            test_resolution_is_cached,
            test_allowed_hosts_override,
            test_allow_private_networks_toggle,
            test_check_url_scheme_handling,
            test_goto_blocks_private_urls_before_touching_the_page,
            test_guard_route_neutralizes_public_script_hosts,
            test_guard_route_neutralizes_wildcard_host_and_scoped_path,
            test_guard_route_leaves_lookalike_urls_alone,
            test_guard_route_continues_public_and_aborts_private,
            test_websocket_observer_kills_private_connections,
            test_websocket_observer_leaves_public_connections,
        ]
    )


if __name__ == "__main__":
    main()
