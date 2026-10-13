"""Domain probe / benchmark engine (spec sections 23, 24, 25).

The probe runs through a **route-specific one-shot path** (spec 24: "Probe
должен выполняться через route-specific one-shot path и потому безопасно
parallelize-иться"): for each route the engine allocates a dedicated one-shot
slot, points the slot's select group at the route, runs a DNS -> TCP -> TLS ->
HTTP request through that slot's authenticated SOCKS listener, classifies the
outcome, and releases the slot. Because every probe owns its slot, probes are
safe to run in parallel and never race on a shared selector (spec 18/19).

Error classification (spec 24) distinguishes the *nature* of a block so the
agent gets useful information:

  dns_failure, tcp_timeout, tcp_refused, tls_failure, http_status_denied,
  redirect, connection_reset, http_timeout, proxy_failure

The classifier (:func:`classify_probe`) is a pure function over a
:class:`ProbeOutcome`, so it is unit-testable without any network; the full
data-plane path is exercised by the live harness against a controllable fake
node.
"""
from __future__ import annotations

import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# error classes (spec 24)
OK = "ok"
DNS_FAILURE = "dns_failure"
TCP_TIMEOUT = "tcp_timeout"
TCP_REFUSED = "tcp_refused"
TLS_FAILURE = "tls_failure"
HTTP_STATUS_DENIED = "http_status_denied"
REDIRECT = "redirect"
CONNECTION_RESET = "connection_reset"
HTTP_TIMEOUT = "http_timeout"
PROXY_FAILURE = "proxy_failure"
SLOT_EXHAUSTED = "slot_exhausted"

ALL_ERROR_CLASSES = {
    OK, DNS_FAILURE, TCP_TIMEOUT, TCP_REFUSED, TLS_FAILURE,
    HTTP_STATUS_DENIED, REDIRECT, CONNECTION_RESET, HTTP_TIMEOUT,
    PROXY_FAILURE, SLOT_EXHAUSTED,
}


# ---------------------------------------------------------------------------
# one-shot slot allocator
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    index: int
    user: str
    password: str

    @property
    def group(self) -> str:
        return f"os_{self.index}"


class SlotAllocator:
    """Thread-safe allocation of the pre-allocated one-shot slots.

    A slot is a (user, password) identity on the one-shot SOCKS listener plus
    its select group. Allocating a slot and pointing its group at a route gives
    a caller a private, concurrency-safe data-plane path to that route.
    """

    def __init__(self, slots: list[Slot]):
        self._slots = {s.index: s for s in slots}
        self._free = set(self._slots.keys())
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def __len__(self) -> int:
        return len(self._slots)

    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    def allocate(self, timeout: float = 5.0) -> Slot | None:
        """Block up to ``timeout`` seconds for a free slot; None if none."""
        deadline = time.time() + timeout
        with self._cv:
            while not self._free:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            idx = min(self._free)
            self._free.discard(idx)
            return self._slots[idx]

    def release(self, slot: Slot) -> None:
        with self._cv:
            self._free.add(slot.index)
            self._cv.notify_all()

    def context(self, timeout: float = 5.0):
        """Context manager: allocate on enter, release on exit."""
        return _SlotCtx(self, timeout)


class _SlotCtx:
    def __init__(self, alloc: SlotAllocator, timeout: float):
        self._alloc = alloc
        self._timeout = timeout
        self.slot: Slot | None = None

    def __enter__(self) -> Slot:
        self.slot = self._alloc.allocate(self._timeout)
        if self.slot is None:
            raise SlotExhausted("no free one-shot slot")
        return self.slot

    def __exit__(self, *exc) -> None:
        if self.slot is not None:
            self._alloc.release(self.slot)
            self.slot = None


class SlotExhausted(Exception):
    pass


def load_slots(count: int, slots_file: str | None) -> list[Slot]:
    """Build the slot list from the oneshot-slots.json password map.

    Passwords come from the backend secrets dir (created by bootstrap). If the
    file is missing (e.g. a test), deterministic placeholder passwords are used
    so the allocator is still constructible; the live deployment always has the
    real file.
    """
    import json
    import os
    from pathlib import Path

    passwords: dict[str, str] = {}
    if slots_file and Path(slots_file).is_file():
        try:
            passwords = json.loads(Path(slots_file).read_text())
        except (json.JSONDecodeError, OSError):
            passwords = {}
    slots: list[Slot] = []
    for i in range(count):
        user = f"os_{i}"
        pw = passwords.get(user) or os.environ.get(f"EGRESSD_ONESHOT_PW_{i}") or f"pw-{i}"
        slots.append(Slot(index=i, user=user, password=pw))
    return slots


# ---------------------------------------------------------------------------
# probe outcome + pure classifier
# ---------------------------------------------------------------------------
@dataclass
class ProbeOutcome:
    route_id: str
    domain: str
    scheme: str = "https"
    port: int = 443
    path: str = "/"
    method: str = "GET"
    # stage signals (None = stage not performed)
    dns_ok: bool | None = None
    tcp_ok: bool | None = None
    tls_ok: bool | None = None
    http_ok: bool | None = None
    status: int | None = None
    latency_ms: int | None = None
    resolved_ip: str | None = None
    exit_ip: str | None = None
    exit_country: str | None = None
    asn: int | None = None
    socks_code: int | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def reachable(self) -> bool:
        return self.error == OK and self.http_ok is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route_id,
            "reachable": self.reachable,
            "dns_ok": self.dns_ok,
            "tcp_ok": self.tcp_ok,
            "tls_ok": self.tls_ok,
            "http_ok": self.http_ok,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "resolved_ip": self.resolved_ip,
            "exit_ip": self.exit_ip,
            "exit_country": self.exit_country,
            "asn": self.asn,
            "error": self.error,
            "detail": self.detail,
        }


def classify_probe(o: ProbeOutcome, expected: tuple[int, int] = (200, 399)) -> str:
    """Pure classification of a probe outcome into an error class (spec 24).

    The classification is **data-plane-driven**: the authoritative signal is
    what happened through the route (TCP -> TLS -> HTTP), not the client's own
    resolver. In the live stack the domain is resolved at the exit (mihomo
    fake-ip), so a client-side NXDOMAIN must not override a successful
    TCP/HTTP through the route. The client-side ``dns_ok`` is reported as
    context and only tips a host-unreachable TCP failure toward ``dns_failure``
    (the exit could not resolve it either).
    """
    # TCP: connect through the route failed.
    if o.tcp_ok is False:
        if o.detail.get("tcp_timeout"):
            return TCP_TIMEOUT
        # SOCKS reply code 4 = host unreachable. If the client could not
        # resolve the domain either, the block is at DNS; otherwise it is a
        # TCP-level refusal/unreachability.
        if o.socks_code == 4 or o.detail.get("tcp_refused"):
            return DNS_FAILURE if o.dns_ok is False else TCP_REFUSED
        return TCP_REFUSED
    # TLS: handshake failed.
    if o.tls_ok is False:
        return TLS_FAILURE
    # HTTP: we got a response (or a transport error while reading it).
    if o.detail.get("http_reset"):
        return CONNECTION_RESET
    if o.detail.get("http_timeout"):
        return HTTP_TIMEOUT
    if o.status is None:
        return PROXY_FAILURE
    if o.status in (300, 301, 302, 303, 307, 308):
        return REDIRECT
    if expected[0] <= o.status <= expected[1]:
        return OK
    return HTTP_STATUS_DENIED


# ---------------------------------------------------------------------------
# SOCKS5 probe (DNS -> TCP -> TLS -> HTTP) through the one-shot listener
# ---------------------------------------------------------------------------
def socks_probe(
    socks_host: str,
    socks_port: int,
    user: str,
    password: str,
    domain: str,
    scheme: str = "https",
    port: int | None = None,
    path: str = "/",
    method: str = "GET",
    timeout: float = 15.0,
    check: tuple[str, ...] = ("dns", "tcp", "tls", "http"),
    expected: tuple[int, int] = (200, 399),
) -> ProbeOutcome:
    """Run a full DNS/TCP/TLS/HTTP probe through an authenticated SOCKS5 listener.

    ``check`` selects which stages to perform/report. The DNS stage uses the
    system resolver as an independent signal (does the domain resolve at all?);
    the TCP stage is the SOCKS5 CONNECT through the route (does the exit reach
    it?); this is what lets the classifier separate a DNS block from a TCP
    block (spec 24).
    """
    port = port or (443 if scheme == "https" else 80)
    o = ProbeOutcome(route_id="", domain=domain, scheme=scheme, port=port,
                     path=path, method=method)
    t0 = time.time()

    # ---- DNS (system resolver, independent signal) ----
    if "dns" in check:
        try:
            infos = socket.getaddrinfo(domain, port, 0, socket.SOCK_STREAM)
            o.dns_ok = True
            o.resolved_ip = str(infos[0][4][0])
        except socket.gaierror as e:
            o.dns_ok = False
            o.detail["dns_error"] = str(e)

    # ---- connect to the SOCKS listener ----
    try:
        s = socket.create_connection((socks_host, socks_port), timeout)
    except socket.timeout:
        o.tcp_ok = False
        o.detail["tcp_timeout"] = True
        o.detail["socks_connect_error"] = "timeout connecting to listener"
        o.latency_ms = int((time.time() - t0) * 1000)
        o.error = classify_probe(o, expected)
        return o
    except OSError as e:
        o.tcp_ok = False
        o.detail["socks_connect_error"] = str(e)
        o.latency_ms = int((time.time() - t0) * 1000)
        o.error = PROXY_FAILURE
        return o

    try:
        # ---- SOCKS5 greeting + auth ----
        s.sendall(b"\x05\x02\x00\x02")
        g = s.recv(2)
        if len(g) < 2 or g[0] != 0x05:
            o.tcp_ok = False
            o.detail["socks_error"] = f"bad greeting {g!r}"
            o.error = PROXY_FAILURE
            return o
        if g[1] == 0x02:  # username/password
            u, p = user.encode(), password.encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            r = s.recv(2)
            if len(r) < 2 or r[1] != 0x00:
                o.tcp_ok = False
                o.detail["socks_error"] = f"auth rejected {r!r}"
                o.error = PROXY_FAILURE
                return o
        elif g[1] != 0x00:
            o.tcp_ok = False
            o.detail["socks_error"] = f"no acceptable method {g!r}"
            o.error = PROXY_FAILURE
            return o

        # ---- SOCKS5 CONNECT to domain:port (type 3 = domain) ----
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain.encode()
                  + port.to_bytes(2, "big"))
        resp = s.recv(4)
        if len(resp) < 4 or resp[1] != 0x00:
            o.tcp_ok = False
            o.socks_code = resp[1] if len(resp) >= 2 else None
            o.detail["socks_reply_code"] = o.socks_code
            # disambiguate: domain doesn't resolve at all -> dns_failure;
            # resolves but exit can't reach it -> tcp failure
            if o.dns_ok is False:
                o.error = DNS_FAILURE
            else:
                o.detail["tcp_refused"] = True
                o.error = classify_probe(o, expected)
            o.latency_ms = int((time.time() - t0) * 1000)
            return o
        # consume the bound-address part of the reply
        atyp = resp[3]
        if atyp == 1:
            s.recv(4)
        elif atyp == 3:
            ln = s.recv(1)[0]
            s.recv(ln)
        elif atyp == 4:
            s.recv(16)
        s.recv(2)
        o.tcp_ok = True

        # ---- TLS ----
        if scheme == "https" and "tls" in check:
            try:
                ctx = ssl.create_default_context()
                s = ctx.wrap_socket(s, server_hostname=domain)
                o.tls_ok = True
            except ssl.SSLError as e:
                o.tls_ok = False
                o.detail["tls_error"] = str(e)
                o.latency_ms = int((time.time() - t0) * 1000)
                o.error = TLS_FAILURE
                return o

        # ---- HTTP ----
        if "http" in check:
            host_hdr = domain if (scheme == "https" and port == 443) or (scheme == "http" and port == 80) \
                else f"{domain}:{port}"
            req = (f"{method} {path} HTTP/1.1\r\nHost: {host_hdr}\r\n"
                   f"Connection: close\r\n\r\n")
            try:
                s.sendall(req.encode())
                data = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                o.detail["http_timeout"] = True
                o.latency_ms = int((time.time() - t0) * 1000)
                o.error = HTTP_TIMEOUT
                return o
            except (ConnectionResetError, BrokenPipeError) as e:
                o.detail["http_reset"] = True
                o.detail["http_error"] = str(e)
                o.latency_ms = int((time.time() - t0) * 1000)
                o.error = CONNECTION_RESET
                return o
            text = data.decode(errors="replace")
            head = text.split("\r\n\r\n", 1)[0]
            first = head.split("\r\n", 1)[0] if head else ""
            parts = first.split(" ", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                o.status = int(parts[1])
                o.http_ok = expected[0] <= o.status <= expected[1]
            else:
                o.detail["http_parse_error"] = first[:120]
                o.http_ok = False
        o.latency_ms = int((time.time() - t0) * 1000)
        o.error = classify_probe(o, expected)
        return o
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# probe engine (orchestrates slot + mihomo + probe)
# ---------------------------------------------------------------------------
class ProbeEngine:
    """Runs probes/benchmarks through the one-shot path.

    ``point_group`` is a callable ``mihomo_group_name -> route_mihomo_name``
    that egressd supplies (it issues ``PUT /proxies/<group>``). Keeping it
    injectable makes the engine testable without a live mihomo.
    """

    def __init__(
        self,
        allocator: SlotAllocator,
        socks_host: str,
        socks_port: int,
        point_group,
        catalog,
        timeout: float = 15.0,
    ):
        self.allocator = allocator
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.point_group = point_group
        self.catalog = catalog
        self.timeout = timeout

    def probe_route(
        self,
        route_id: str,
        domain: str,
        scheme: str = "https",
        port: int | None = None,
        path: str = "/",
        method: str = "GET",
        check: tuple[str, ...] = ("dns", "tcp", "tls", "http"),
        expected: tuple[int, int] = (200, 399),
    ) -> ProbeOutcome:
        route = self.catalog.get(route_id)
        if route is None:
            o = ProbeOutcome(route_id=route_id, domain=domain, scheme=scheme)
            o.error = PROXY_FAILURE
            o.detail["reason"] = f"unknown route {route_id!r}"
            return o
        slot = self.allocator.allocate(self.timeout)
        if slot is None:
            o = ProbeOutcome(route_id=route_id, domain=domain, scheme=scheme)
            o.error = SLOT_EXHAUSTED
            o.detail["reason"] = "no free one-shot slot"
            return o
        try:
            # point the slot's group at the route (private path for this probe)
            self.point_group(slot.group, self.catalog.mihomo_name(route_id))
            o = socks_probe(
                self.socks_host, self.socks_port, slot.user, slot.password,
                domain, scheme=scheme, port=port, path=path, method=method,
                timeout=self.timeout, check=check, expected=expected,
            )
            o.route_id = route_id
            # enrich with catalog geo (context only; real verification is the
            # probe itself, spec 14.3)
            if o.exit_country is None:
                o.exit_country = route.country
            return o
        finally:
            self.allocator.release(slot)

    def probe_routes(
        self,
        route_ids: list[str],
        domain: str,
        parallel: int = 8,
        **kw: Any,
    ) -> list[ProbeOutcome]:
        """Probe many routes in parallel (each on its own slot)."""
        import concurrent.futures

        results: list[ProbeOutcome] = [None] * len(route_ids)  # type: ignore[list-item]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
            futs = {
                ex.submit(self.probe_route, rid, domain, **kw): i
                for i, rid in enumerate(route_ids)
            }
            for fut in concurrent.futures.as_completed(futs):
                results[futs[fut]] = fut.result()
        return results
