"""TUN-local DNS forwarding uses the selected route and tolerates UDP limits."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))

from vibelab_egress_client import tun_bootstrap  # noqa: E402


class _DohResponse:
    status = 200

    def getheader(self, name, default=""):
        assert name == "Content-Type"
        return "application/dns-message; charset=binary"

    def read(self, _limit):
        return b"dns-answer"


class _DohConnection:
    def __init__(self, address):
        self.address = address
        self.request_args = None
        self.closed = False

    def request(self, *args, **kwargs):
        self.request_args = (args, kwargs)

    def getresponse(self):
        return _DohResponse()

    def close(self):
        self.closed = True


def test_cloudflare_dns_uses_pinned_doh_endpoint(monkeypatch):
    created = []

    def connection(address):
        result = _DohConnection(address)
        created.append(result)
        return result

    monkeypatch.setattr(tun_bootstrap, "_PinnedHTTPSConnection", connection)
    assert tun_bootstrap._forward_dns_doh(b"dns-query", "1.1.1.1") == b"dns-answer"
    assert created[0].address == "1.1.1.1"
    args, kwargs = created[0].request_args
    assert args == ("POST", "/dns-query")
    assert kwargs["body"] == b"dns-query"
    assert kwargs["headers"] == {
        "Accept": "application/dns-message",
        "Content-Type": "application/dns-message",
    }
    assert created[0].closed


def test_cloudflare_doh_falls_back_to_udp_then_tcp(monkeypatch):
    calls = []

    def no_doh(_query, address):
        calls.append(("doh", address))
        raise OSError("DoH unavailable")

    def no_udp(_query, address):
        calls.append(("udp", address))
        raise OSError("UDP unavailable")

    def tcp_answer(_query, address):
        calls.append(("tcp", address))
        return b"tcp-answer"

    monkeypatch.setattr(tun_bootstrap, "_forward_dns_doh", no_doh)
    monkeypatch.setattr(tun_bootstrap, "_forward_dns_udp", no_udp)
    monkeypatch.setattr(tun_bootstrap, "_forward_dns_tcp", tcp_answer)

    assert tun_bootstrap._forward_dns(
        b"dns-query", ("1.1.1.1", "1.0.0.1")) == b"tcp-answer"
    assert calls == [
        ("doh", "1.1.1.1"), ("doh", "1.0.0.1"),
        ("udp", "1.1.1.1"), ("udp", "1.0.0.1"),
        ("tcp", "1.1.1.1"),
    ]


def test_custom_resolver_keeps_configured_dns_policy(monkeypatch):
    def unexpected_doh(*_args):
        pytest.fail("Cloudflare DoH must not be used for a custom resolver")

    def udp_unavailable(_query, _address):
        raise OSError("UDP unavailable")

    monkeypatch.setattr(tun_bootstrap, "_forward_dns_doh", unexpected_doh)
    monkeypatch.setattr(tun_bootstrap, "_forward_dns_udp", udp_unavailable)
    monkeypatch.setattr(tun_bootstrap, "_forward_dns_tcp",
                        lambda _query, address: address.encode())

    assert tun_bootstrap._forward_dns(b"dns-query", ("9.9.9.9",)) == b"9.9.9.9"
