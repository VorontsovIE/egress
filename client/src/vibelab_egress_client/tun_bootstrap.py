"""Private bootstrap used by ``unshare`` for one local TUN workload.

This process creates the interface inside its own nested user/network
namespaces, passes only the packet fd to the parent, and starts the workload
after the dataplane reports ready.
"""
from __future__ import annotations

import array
import ctypes
import fcntl
import http.client
import ipaddress
import json
import os
import signal
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
PR_CAPBSET_DROP = 24
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_DATA = 0x0000FFFF
BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_JMP_JSET_K = 0x45
BPF_RET_K = 0x06


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort),
                ("filter", ctypes.POINTER(SockFilter))]


class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


def _control() -> socket.socket:
    fd = int(os.environ["VIBELAB_EGRESS_TUN_CONTROL_FD"])
    return socket.socket(fileno=fd)


def _ip(*args: str) -> None:
    proc = subprocess.run(["ip", *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, check=False)
    if proc.returncode:
        raise RuntimeError((proc.stderr or "ip command failed").strip())


def _outer_dns_servers(path: str = "/etc/resolv.conf") -> tuple[str, ...]:
    """Return numeric nameservers from the caller's resolver configuration."""
    servers = []
    try:
        for line in open(path, encoding="ascii"):
            fields = line.split()
            if len(fields) == 2 and fields[0] == "nameserver":
                try:
                    ip = ipaddress.ip_address(fields[1].split("%", 1)[0])
                except ValueError:
                    continue
                address = str(ip)
                if not ip.is_unspecified and not ip.is_multicast and address not in servers:
                    servers.append(address)
    except OSError:
        pass
    return tuple(servers)


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


_CLOUDFLARE_DOH_HOST = "cloudflare-dns.com"
_CLOUDFLARE_DOH_IPS = frozenset({
    "1.1.1.1", "1.0.0.1",
    "2606:4700:4700::1111", "2606:4700:4700::1001",
})


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a configured resolver IP while verifying Cloudflare SNI."""

    def __init__(self, address: str, timeout: float = 4.0):
        self.address = address
        super().__init__(_CLOUDFLARE_DOH_HOST, 443, timeout=timeout,
                         context=ssl.create_default_context())

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout,
                                        self.source_address)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _forward_dns_doh(query: bytes, upstream: str) -> bytes:
    connection = _PinnedHTTPSConnection(upstream)
    try:
        connection.request(
            "POST", "/dns-query", body=query,
            headers={"Accept": "application/dns-message",
                     "Content-Type": "application/dns-message"},
        )
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "").split(";", 1)[0]
        content_type = content_type.strip().lower()
        if response.status != 200 or content_type != "application/dns-message":
            raise OSError("DNS-over-HTTPS resolver rejected the query")
        answer = response.read(65535)
        if not answer:
            raise OSError("DNS-over-HTTPS resolver returned an empty answer")
        return answer
    finally:
        connection.close()


def _forward_dns_udp(query: bytes, upstream: str) -> bytes:
    family = socket.AF_INET6 if ":" in upstream else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        target = (upstream, 53, 0, 0) if family == socket.AF_INET6 else (upstream, 53)
        sock.sendto(query, target)
        return sock.recv(65535)


def _forward_dns_tcp(query: bytes, upstream: str) -> bytes:
    family = socket.AF_INET6 if ":" in upstream else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        target = (upstream, 53, 0, 0) if family == socket.AF_INET6 else (upstream, 53)
        sock.connect(target)
        sock.sendall(struct.pack("!H", len(query)) + query)
        header = _recv_exact(sock, 2)
        if header is None:
            raise OSError("DNS-over-TCP response is missing")
        size = struct.unpack("!H", header)[0]
        answer = _recv_exact(sock, size)
        if answer is None:
            raise OSError("DNS-over-TCP response is truncated")
        return answer


def _forward_dns(query: bytes, upstreams: tuple[str, ...]) -> bytes:
    """Forward DNS over the selected TUN route, with TCP fallbacks for UDP."""
    last_error: Exception | None = None
    cloudflare = [address for address in upstreams if address in _CLOUDFLARE_DOH_IPS]
    for address in cloudflare:
        try:
            return _forward_dns_doh(query, address)
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
    for forwarder in (_forward_dns_udp, _forward_dns_tcp):
        for address in upstreams:
            try:
                return forwarder(query, address)
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
    raise OSError(f"all configured DNS servers failed: {last_error}")


def _usable_dns_upstreams(listeners: tuple[str, ...],
                          configured: tuple[str, ...]) -> tuple[str, ...]:
    # The caller's resolver addresses are local aliases in this namespace.
    # Forwarding to one of those same addresses would recurse back into this
    # proxy, so only retain configured upstreams that are not intercepted.
    intercepted = {str(ipaddress.ip_address(address)) for address in listeners}
    upstreams = []
    for address in configured:
        canonical = str(ipaddress.ip_address(address.split("%", 1)[0]))
        if canonical not in intercepted:
            upstreams.append(canonical)
    upstreams = tuple(upstreams)
    if not upstreams:
        raise RuntimeError(
            "all configured TUN DNS servers overlap the caller's resolver addresses")
    return upstreams


class _ForwardDNSUDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        query, request_socket = self.request
        try:
            response = _forward_dns(query, self.server.upstreams)
            request_socket.sendto(response, self.client_address)
        except OSError:
            pass


def _start_local_dns_proxy(configured: tuple[str, ...] | None = None) -> int | None:
    """Forward the caller's DNS requests through this namespace's TUN route."""
    # The network namespace has its own loopback and no Docker bridge device.
    # Make every resolver from the caller's resolv.conf local in this namespace
    # so DNS requests are accepted here instead of sent as unsupported UDP via
    # the packet engine. _configure_network installs the corresponding local
    # routes before the workload starts.
    listeners = _outer_dns_servers()
    if configured is None:
        configured = tuple(x.strip() for x in
                           os.environ.get("VIBELAB_EGRESS_TUN_DNS", "").split(",")
                           if x.strip())
    if not listeners:
        return None
    if not configured:
        raise RuntimeError("no TUN DNS servers configured")
    upstreams = _usable_dns_upstreams(listeners, configured)
    servers = []

    class TCPHandler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                header = self.rfile.read(2)
                if len(header) != 2:
                    return
                size = struct.unpack("!H", header)[0]
                query = self.rfile.read(size)
                if len(query) != size:
                    return
                response = _forward_dns(query, upstreams)
                self.wfile.write(struct.pack("!H", len(response)) + response)
            except (OSError, http.client.HTTPException, struct.error):
                pass

    for address in listeners:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        udp = type("UDPServer", (socketserver.ThreadingUDPServer,),
                   {"address_family": family, "daemon_threads": True})
        tcp = type("TCPServer", (socketserver.ThreadingTCPServer,),
                   {"address_family": family, "daemon_threads": True})
        bind = (address, 53, 0, 0) if family == socket.AF_INET6 else (address, 53)
        for server_class in (udp, tcp):
            handler = _ForwardDNSUDPHandler if server_class is udp else TCPHandler
            server = server_class(bind, handler)
            if server_class is udp:
                server.upstreams = upstreams
            servers.append(server)
    pid = os.fork()
    if pid:
        for server in servers:
            server.server_close()
        return pid
    _drop_capabilities()
    _deny_namespace_syscalls()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        signal.pause()


def _new_tun() -> int:
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_CLOEXEC)
    try:
        request = struct.pack("16sH", b"egress0", IFF_TUN | IFF_NO_PI)
        fcntl.ioctl(fd, TUNSETIFF, request)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _configure_network(fd: int) -> None:
    _ip("link", "set", "lo", "up")
    for address in _outer_dns_servers():
        ip = ipaddress.ip_address(address)
        if ip.is_loopback:
            continue
        if ip.version == 6:
            _ip("-6", "addr", "add", f"{ip}/128", "dev", "lo", "nodad")
        else:
            _ip("addr", "add", f"{ip}/32", "dev", "lo")
    # The namespace has no physical interface. These are its only external
    # routes; if the TUN device stops, packets have nowhere else to go.
    _ip("addr", "add", "198.18.0.1/30", "dev", "egress0")
    _ip("-6", "addr", "add", "fd00::1/126", "dev", "egress0")
    _ip("link", "set", "dev", "egress0", "mtu", "1500", "up")
    _ip("route", "add", "default", "dev", "egress0")
    _ip("-6", "route", "add", "default", "dev", "egress0")


def _deny_namespace_syscalls() -> None:
    machine = os.uname().machine
    if machine in ("x86_64", "amd64"):
        nr_unshare, nr_setns, nr_clone, nr_clone3 = 272, 308, 56, 435
    elif machine in ("aarch64", "arm64"):
        nr_unshare, nr_setns, nr_clone, nr_clone3 = 97, 268, 220, 435
    else:
        raise RuntimeError(f"seccomp namespace guard unsupported on {machine}")
    namespace_flags = (0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 |
                       0x10000000 | 0x20000000 | 0x40000000 | 0x00000080)
    ins = [
        (BPF_LD_W_ABS, 0, 0, 0),
        (BPF_JMP_JEQ_K, 0, 1, nr_unshare), (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | 1),
        (BPF_JMP_JEQ_K, 0, 1, nr_setns), (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | 1),
        (BPF_JMP_JEQ_K, 0, 1, nr_clone3), (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | 38),
        (BPF_JMP_JEQ_K, 0, 3, nr_clone),
        (BPF_LD_W_ABS, 0, 0, 16),
        (BPF_JMP_JSET_K, 0, 1, namespace_flags),
        (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | 1),
        (BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
    ]
    filters = (SockFilter * len(ins))(*(SockFilter(*x) for x in ins))
    program = SockFprog(len(ins), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _drop_capabilities() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    # Drop the bounding set while CAP_SETPCAP is still available.
    for cap in range(0, 64):
        if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            if err != 22:
                raise OSError(err, os.strerror(err))
    header = CapHeader(0x20080522, 0)
    data = (CapData * 2)(CapData(0, 0, 0), CapData(0, 0, 0))
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "--" or len(args) < 2:
        print("tun bootstrap: internal invocation is malformed", file=sys.stderr)
        return 125
    control = _control()
    tun_fd = None
    dns_pid = None
    try:
        tun_fd = _new_tun()
        _configure_network(tun_fd)
        control.sendmsg([b'{"event":"tun-ready"}'], [
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [tun_fd]))
        ])
        if control.recv(16) != b"start":
            raise RuntimeError("parent did not start the local TUN engine")
        control.close()
        os.close(tun_fd)
        tun_fd = None
        dns_upstreams = tuple(x.strip() for x in
                              os.environ.get("VIBELAB_EGRESS_TUN_DNS", "").split(",")
                              if x.strip())
        os.environ.pop("VIBELAB_EGRESS_TUN_CONTROL_FD", None)
        os.environ.pop("VIBELAB_EGRESS_TUN_DNS", None)
        os.environ.pop("VIBELAB_EGRESS_TUN_ENGINE", None)
        module_root = os.environ.pop("VIBELAB_EGRESS_TUN_MODULE_ROOT", None)
        if module_root:
            os.environ["PYTHONPATH"] = os.pathsep.join(
                item for item in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                if item and item != module_root)
            if not os.environ["PYTHONPATH"]:
                os.environ.pop("PYTHONPATH", None)
        dns_pid = _start_local_dns_proxy(dns_upstreams)
        workload = None
        signal.signal(signal.SIGINT, lambda _signum, _frame: None)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
        _drop_capabilities()
        _deny_namespace_syscalls()
        # Keep the workload in this namespace process group. The outer client
        # forwards signals to the group, so timeout cleanup also kills any
        # grandchildren instead of orphaning them.
        workload = subprocess.Popen(args[1:], stdin=None, stdout=None, stderr=None,
                                    close_fds=True, env=os.environ)
        rc = workload.wait()
        if dns_pid is not None:
            try:
                os.kill(dns_pid, signal.SIGTERM)
                os.waitpid(dns_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        return 128 + -rc if rc < 0 else rc
    except BaseException as exc:
        try:
            control.send(json.dumps({"event": "error", "error": str(exc)}).encode())
        except OSError:
            pass
        print(f"egress-run TUN setup failed: {exc}", file=sys.stderr)
        return 125
    finally:
        if dns_pid is not None:
            try:
                os.kill(dns_pid, signal.SIGTERM)
                os.waitpid(dns_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        if tun_fd is not None:
            os.close(tun_fd)
        try:
            control.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
