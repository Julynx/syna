"""Network policy for the agent's browser: public internet only.

The browser runs on the host, so every request it makes, agent-driven
navigation or a page's own fetches, originates from the host's network.
This module decides whether a destination is reachable under the policy:
public internet targets only. Loopback, private, link-local (including
the cloud metadata service), and other non-routable ranges are blocked.

Hostname checks resolve DNS at decision time (with a short-lived cache)
so names that currently resolve into those ranges are caught. When
`net_guard_doh_uri` is configured, the resolution goes through the
guard's own DoH endpoint first, sharing the browser's poison-free view:
a resolver that poisons a name into a private range cannot make the
guard block a site the browser would reach, and a name poisoned into
NXDOMAIN cannot smuggle a private address past a guard that never
resolved it. When the DoH lookup fails, the system resolver answers,
which is also the view the browser's TRR mode 2 falls back to, so the
two stay consistent. That closes casual rebinding; a determined
rebinder can still race the browser's own second resolution, which is
why the Syna server independently validates the Host header, Origin,
and a per-process token; the browser guard is one layer, not the whole
defense. Resolution failures allow the request through: there is no
reachable target, and the browser surfaces the connection error it
would show anyway.
"""

import ipaddress
import json
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import load_config

DNS_CACHE_TTL_S = 60
DNS_CACHE_MAX = 4096

# One JSON DoH round-trip; a slow endpoint must not stall navigation
# decisions, and any failure falls back to the system resolver.
GUARD_DOH_TIMEOUT_S = 3

HTTP_SCHEMES = ("http", "https")
WEBSOCKET_SCHEMES = ("ws", "wss")
GUARDED_SCHEMES = HTTP_SCHEMES + WEBSOCKET_SCHEMES

# Every range that is not global unicast space, with the reason a blocked
# request reports. Explicit and version-independent: ipaddress.is_global
# semantics have shifted across Python releases, this list does not.
BLOCKED_NETWORKS: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]] = [
    (ipaddress.ip_network("0.0.0.0/8"), "the unspecified range"),
    (ipaddress.ip_network("10.0.0.0/8"), "a private network"),
    (ipaddress.ip_network("100.64.0.0/10"), "carrier-grade NAT"),
    (ipaddress.ip_network("127.0.0.0/8"), "the loopback interface"),
    (
        ipaddress.ip_network("169.254.0.0/16"),
        "link-local (includes the cloud metadata service)",
    ),
    (ipaddress.ip_network("172.16.0.0/12"), "a private network"),
    (ipaddress.ip_network("192.0.0.0/24"), "an IETF protocol range"),
    (ipaddress.ip_network("192.0.2.0/24"), "a documentation range"),
    (ipaddress.ip_network("192.88.99.0/24"), "an IETF protocol range"),
    (ipaddress.ip_network("192.168.0.0/16"), "a private network"),
    (ipaddress.ip_network("198.18.0.0/15"), "a benchmarking range"),
    (ipaddress.ip_network("198.51.100.0/24"), "a documentation range"),
    (ipaddress.ip_network("203.0.113.0/24"), "a documentation range"),
    (ipaddress.ip_network("224.0.0.0/4"), "multicast"),
    (ipaddress.ip_network("240.0.0.0/4"), "a reserved range"),
    (ipaddress.ip_network("::/128"), "the unspecified address"),
    (ipaddress.ip_network("::1/128"), "the loopback interface"),
    (ipaddress.ip_network("64:ff9b:1::/48"), "a local-use NAT64 range"),
    (ipaddress.ip_network("100::/64"), "a discard-only range"),
    (ipaddress.ip_network("2001:db8::/32"), "a documentation range"),
    (ipaddress.ip_network("2001::/23"), "an IETF protocol range"),
    (ipaddress.ip_network("fc00::/7"), "a private network"),
    (ipaddress.ip_network("fe80::/10"), "link-local"),
    (ipaddress.ip_network("ff00::/8"), "multicast"),
]

_dns_cache: dict[
    str, tuple[float, list[ipaddress.IPv4Address | ipaddress.IPv6Address]]
] = {}
_dns_lock = threading.Lock()


@dataclass(frozen=True)
class GuardDecision:
    """Outcome of a destination check; reason is empty when allowed."""

    allowed: bool
    reason: str = ""


def _private_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The reason an IP is blocked, or empty when it is public."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for network, reason in BLOCKED_NETWORKS:
        if ip in network:
            return reason
    return ""


def _normalize_host(host: str) -> str:
    """Lowercase and strip the syntax noise browsers tolerate in hosts."""
    host = host.strip().strip("[]").rstrip(".").lower()
    return host


def _parse_literal_ip(host: str):
    """Parse a host string as an IP, including URL-spec short forms.

    Browsers parse IPv4 per the WHATWG URL standard: decimal (2130706433),
    hex (0x7f000001), and octal (0177.0.0.1) spellings, and shortened
    dotted forms where the last label carries all remaining bits
    ("127.65535" is 127.0.255.255). All of these must be classified as
    addresses, never passed on as 'just a hostname'. Returns an address
    object or None when the host is not an IP literal.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.isdigit():
        value = int(host)
        if value <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(value)
        return None
    if host.startswith("0x") and all(c in "0123456789abcdef" for c in host[2:]):
        value = int(host, 16)
        if value <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(value)
        return None
    labels = host.split(".")
    if 2 <= len(labels) <= 4:
        values = []
        for label in labels:
            try:
                if label.startswith("0x"):
                    values.append(int(label[2:], 16))
                elif label.startswith("0") and label != "0" and label.isdigit():
                    values.append(int(label, 8))
                elif label.isdigit():
                    values.append(int(label))
                else:
                    return None
            except ValueError:
                return None
        last_bits = 8 * (5 - len(values))
        if any(v > 255 for v in values[:-1]) or values[-1] >= (1 << last_bits):
            return None
        packed = values[-1]
        for index, value in enumerate(values[:-1]):
            packed |= value << (24 - 8 * index)
        return ipaddress.IPv4Address(packed)
    return None


def _doh_ips(host: str, endpoint: str) -> list | None:
    """One JSON DoH lookup; None when the lookup itself failed.

    A definitive negative answer (NXDOMAIN, or NOERROR with no records)
    is an empty list: the name genuinely does not resolve, which is what
    the browser sees through the same view.
    """
    separator = "&" if "?" in endpoint else "?"
    request = urllib.request.Request(
        f"{endpoint}{separator}name={host}&type=A",
        headers={"accept": "application/dns-json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=GUARD_DOH_TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except Exception:
        return None
    status = payload.get("Status", 0)
    if status != 0:
        return [] if status == 3 else None
    try:
        return [
            ipaddress.ip_address(answer["data"])
            for answer in payload.get("Answer", [])
            if answer.get("type") in (1, 28)
        ]
    except (ValueError, TypeError, KeyError):
        return None


def _resolves_natively(host: str) -> bool:
    """Whether the browser resolves this name natively, never through TRR.

    Firefox excludes these from TRR (the default excluded domains plus
    its single-label rule), so the guard must classify them with the
    system resolver too: asking public DNS about 'localhost' answers
    NXDOMAIN, which would let loopback destinations slip through.
    """
    if "." not in host:
        return True
    return (
        host == "localhost"
        or host.endswith(".localhost")
        or host == "local"
        or host.endswith(".local")
    )


def _resolve(host: str) -> list:
    """Resolve a hostname the way the browser will see it.

    The guard's DoH endpoint (`net_guard_doh_uri`) answers first when
    configured; the system resolver is the fallback, which is also the
    view the browser's TRR mode 2 falls back to when its own resolver
    is unreachable, so guard and browser stay consistent. Names the
    browser resolves natively (localhost, single-label) skip the DoH
    path for the same reason.
    """
    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(host)
        if cached and cached[0] > now:
            return cached[1]
    endpoint = load_config().get("net_guard_doh_uri") or ""
    if _resolves_natively(host):
        endpoint = ""
    ips = _doh_ips(host, endpoint) if endpoint else None
    if ips is None:
        try:
            answers = socket.getaddrinfo(host, None)
            ips = [ipaddress.ip_address(answer[4][0]) for answer in answers]
            ips = list(dict.fromkeys(ips))
        except OSError:
            ips = []
    with _dns_lock:
        if len(_dns_cache) >= DNS_CACHE_MAX:
            _dns_cache.clear()
        _dns_cache[host] = (now + DNS_CACHE_TTL_S, ips)
    return ips


def _config():
    config = load_config()
    allow_private = bool(config.get("browser_allow_private_networks", False))
    allowed_hosts = {
        _normalize_host(entry)
        for entry in config.get("browser_allowed_hosts", [])
        if isinstance(entry, str)
    }
    return allow_private, allowed_hosts


def check_destination(host: str) -> GuardDecision:
    """Decide whether the browser may connect to this host at all.

    Port is irrelevant to the policy: a blocked range is blocked on every
    port. Hostnames listed in the browser_allowed_hosts config pass
    without resolution; otherwise the name must resolve to public
    addresses only.
    """
    host = _normalize_host(host)
    if not host:
        return GuardDecision(False, "an empty host")
    allow_private, allowed_hosts = _config()
    if allow_private or host in allowed_hosts:
        return GuardDecision(True)

    literal = _parse_literal_ip(host)
    if literal is not None:
        reason = _private_reason(literal)
        if reason:
            return GuardDecision(False, f"{host} is {reason}")
        return GuardDecision(True)

    for ip in _resolve(host):
        reason = _private_reason(ip)
        if reason:
            return GuardDecision(
                False,
                f"{host} resolves to {ip}, which is {reason}",
            )
    return GuardDecision(True)


def check_url(url: str, schemes=GUARDED_SCHEMES) -> GuardDecision:
    """Decide whether the browser may request this absolute URL.

    Schemes outside `schemes` (data:, blob:, about:, ...) have no host
    to connect to and pass through; ws/wss are checked the same way as
    http/https so a page cannot open a WebSocket around the policy.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return GuardDecision(False, "a malformed URL")
    if parsed.scheme.lower() not in schemes:
        return GuardDecision(True)
    host = parsed.hostname
    if not host:
        return GuardDecision(False, "a URL without a host")
    return check_destination(host)
