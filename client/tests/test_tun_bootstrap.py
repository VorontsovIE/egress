"""TUN-local DNS forwarding uses the selected route and tolerates UDP limits."""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))

from egress_client import tun_bootstrap  # noqa: E402


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


def test_resolv_conf_collects_all_numeric_unicast_nameservers(tmp_path):
    resolv_conf = tmp_path / "resolv.conf"
    resolv_conf.write_text(
        "# Docker bridge and local stub resolvers\n"
        "nameserver 127.0.0.11\n"
        "nameserver 172.18.0.2\n"
        "nameserver 2606:4700:4700::1111\n"
        "nameserver fe80::1%eth0\n"
        "nameserver 172.18.0.2\n"
        "nameserver 0.0.0.0\n"
        "nameserver 224.0.0.1\n"
        "nameserver resolver.example\n"
    )

    assert tun_bootstrap._outer_dns_servers(str(resolv_conf)) == (
        "127.0.0.11", "172.18.0.2", "2606:4700:4700::1111", "fe80::1")


def test_configure_network_localizes_nonloopback_resolvers(monkeypatch):
    commands = []
    monkeypatch.setattr(tun_bootstrap, "_outer_dns_servers", lambda: (
        "127.0.0.11", "172.18.0.2", "2606:4700:4700::1111"))
    monkeypatch.setattr(tun_bootstrap, "_ip", lambda *args: commands.append(args))

    tun_bootstrap._configure_network(-1)

    assert ("addr", "add", "172.18.0.2/32", "dev", "lo") in commands
    assert ("-6", "addr", "add", "2606:4700:4700::1111/128", "dev", "lo", "nodad") in commands
    assert not any("127.0.0.11/32" in arg for command in commands for arg in command)


def test_dns_proxy_does_not_forward_to_its_intercepted_listener():
    assert tun_bootstrap._usable_dns_upstreams(
        ("172.18.0.2", "1.1.1.1"), ("1.1.1.1", "1.0.0.1")) == ("1.0.0.1",)
    assert tun_bootstrap._usable_dns_upstreams(
        ("2606:4700:4700::1111",),
        ("2606:4700:4700:0:0::1111", "2606:4700:4700::1001"),
    ) == ("2606:4700:4700::1001",)


def test_dns_proxy_rejects_only_recursive_upstreams():
    with pytest.raises(RuntimeError, match="overlap the caller's resolver"):
        tun_bootstrap._usable_dns_upstreams(("9.9.9.9",), ("9.9.9.9",))


def test_udp_dns_forwarder_replies_to_client_socket(monkeypatch):
    monkeypatch.setattr(tun_bootstrap, "_forward_dns",
                        lambda query, _upstreams: query + b"-answer")
    server = tun_bootstrap.socketserver.ThreadingUDPServer(
        ("127.0.0.1", 0), tun_bootstrap._ForwardDNSUDPHandler)
    server.upstreams = ("1.1.1.1",)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            client.sendto(b"query", server.server_address)
            assert client.recvfrom(1024)[0] == b"query-answer"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
