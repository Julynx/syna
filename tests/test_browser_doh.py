"""Tests for the stealth browser's DoH configuration.

The browser session maps `browser_doh_uri` and `browser_doh_bootstrap`
onto Firefox's TRR prefs: mode 2 keeps native DNS as a fallback when
the resolver is unreachable, and the HTTPS-record prefs let Firefox
upgrade navigation to HTTP/3 and apply ECH configs where published.
An empty `browser_doh_uri` disables DoH entirely.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities import browser as browser_module
from syna.capabilities.browser import BrowserSession
from syna.config import load_browser_doh_bootstrap, load_browser_doh_uri

QUAD9 = "https://dns.quad9.net/dns-query"


def _session(monkeypatch, uri=QUAD9, bootstrap="9.9.9.9"):
    """A session built with the DoH loaders pinned to known values."""
    monkeypatch.setattr(browser_module, "load_browser_doh_uri", lambda: uri)
    monkeypatch.setattr(browser_module, "load_browser_doh_bootstrap", lambda: bootstrap)
    return BrowserSession(headless=True)


def test_doh_enabled_maps_to_trr_prefs(monkeypatch):
    prefs = _session(monkeypatch)._kw["firefox_user_prefs"]
    assert prefs["network.trr.mode"] == 2
    assert prefs["network.trr.uri"] == QUAD9
    assert prefs["network.trr.bootstrapAddr"] == "9.9.9.9"
    assert prefs["network.dns.use_https_rr_as_altsvc"] is True
    assert prefs["network.dns.echconfig.enabled"] is True
    assert prefs["media.peerconnection.enabled"] is False


def test_empty_uri_disables_doh(monkeypatch):
    prefs = _session(monkeypatch, uri="", bootstrap="9.9.9.9")._kw["firefox_user_prefs"]
    assert not any(key.startswith("network.trr.") for key in prefs)
    assert not any(key.startswith("network.dns.") for key in prefs)
    assert prefs["media.peerconnection.enabled"] is False


def test_bootstrap_optional(monkeypatch):
    prefs = _session(
        monkeypatch, uri="https://dns.example/dns-query", bootstrap=""
    )._kw["firefox_user_prefs"]
    assert prefs["network.trr.uri"] == "https://dns.example/dns-query"
    assert "network.trr.bootstrapAddr" not in prefs


def test_bundled_default_is_quad9():
    assert load_browser_doh_uri() == QUAD9
    assert load_browser_doh_bootstrap() == "9.9.9.9"


def main():
    run_suite(
        [
            test_doh_enabled_maps_to_trr_prefs,
            test_empty_uri_disables_doh,
            test_bootstrap_optional,
            test_bundled_default_is_quad9,
        ]
    )


if __name__ == "__main__":
    main()
