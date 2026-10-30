"""Per-invocation, client-local TUN runtime.

The Python client stays dependency-free. Linux namespace and TUN setup uses
the system ``unshare``/``ip`` tools; the packet engine is a separately
installed, pinned static binary. The engine receives a TUN fd and a private
control pipe only. It never receives or starts the workload command.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

ENGINE_ENV = "EGRESS_TUN_ENGINE"
DEFAULT_ENGINE = "/usr/local/libexec/egress/egress-tun-engine"
BOOTSTRAP_MODULE = "egress_client.tun_bootstrap"


def engine_path() -> str | None:
    user_engine = Path.home() / ".local" / "share" / "egress" / "current" / "egress-tun-engine"
    candidate = os.environ.get(ENGINE_ENV, DEFAULT_ENGINE)
    if candidate == DEFAULT_ENGINE and user_engine.is_file():
        candidate = str(user_engine)
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None


def _probe_program() -> str:
    # Fixed, local capability probe. It creates a disposable user+network
    # namespace and a TUN device, then exits without touching the caller's
    # network namespace.
    return (
        "import fcntl, os, struct\n"
        "fd=os.open('/dev/net/tun', os.O_RDWR|os.O_CLOEXEC)\n"
        "fcntl.ioctl(fd, 0x400454ca, struct.pack('16sH', b'egress-probe', 0x1001))\n"
        "os.close(fd)\n"
    )


def probe() -> dict[str, Any]:
    """Report whether local TUN prerequisites are present."""
    if sys.platform != "linux":
        return {"status": "unsupported_in_environment", "reason": "Linux is required"}
    if not Path("/dev/net/tun").exists():
        return {"status": "unsupported_in_environment", "reason": "/dev/net/tun is missing"}
    if not os.access("/dev/net/tun", os.R_OK | os.W_OK):
        return {"status": "unsupported_in_environment", "reason": "/dev/net/tun is not accessible"}
    if engine_path() is None:
        return {"status": "unsupported_in_environment", "reason": f"TUN engine is missing (set {ENGINE_ENV})"}
    unshare = shutil.which("unshare")
    ip = shutil.which("ip")
    if not unshare or not ip:
        missing = "unshare" if not unshare else "iproute2"
        return {"status": "unsupported_in_environment", "reason": f"{missing} is required"}
    try:
        proc = subprocess.run(
            [unshare, "--user", "--map-root-user", "--net", "--fork",
             sys.executable, "-c", _probe_program()],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unsupported_in_environment", "reason": f"namespace probe failed: {exc}"}
    if proc.returncode:
        detail = (proc.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"namespace probe exited {proc.returncode}"
        return {"status": "unsupported_in_environment", "reason": reason}
    return {"status": "supported", "reason": "local user/network namespaces and TUN are available"}


def _proxy_url(upstream: dict[str, Any] | None) -> str:
    if upstream is None:
        return "direct://"
    from urllib.parse import quote

    host = str(upstream["host"])
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    username = quote(str(upstream.get("username", "")), safe="")
    password = quote(str(upstream.get("password", "")), safe="")
    auth = f"{username}:{password}@" if username or password else ""
    return f"socks5://{auth}{host}:{int(upstream['port'])}"


def _recv_with_fd(sock: socket.socket) -> tuple[dict[str, Any], int | None]:
    data, ancillary, _flags, _addr = sock.recvmsg(4096, socket.CMSG_SPACE(struct.calcsize("i")))
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("invalid TUN bootstrap response") from exc
    received_fd = None
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            received_fd = struct.unpack("i", payload[:struct.calcsize("i")])[0]
            break
    return message, received_fd


def _start_engine(engine: str, tun_fd: int, upstream: dict[str, Any] | None) -> subprocess.Popen:
    config_r, config_w = os.pipe()
    ready_r, ready_w = os.pipe()
    config = json.dumps({"proxy": _proxy_url(upstream)}, separators=(",", ":")).encode() + b"\n"
    try:
        os.write(config_w, config)
    finally:
        os.close(config_w)
    for fd in (tun_fd, config_r, ready_w):
        os.set_inheritable(fd, True)
    try:
        proc = subprocess.Popen(
            [engine, "--device-fd", str(tun_fd), "--config-fd", str(config_r),
             "--ready-fd", str(ready_w)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, cwd="/", env={"LANG": "C"},
            pass_fds=(tun_fd, config_r, ready_w),
        )
    finally:
        os.close(config_r)
        os.close(ready_w)
    try:
        ready = os.read(ready_r, 1)
    finally:
        os.close(ready_r)
    if ready != b"R":
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise RuntimeError("TUN engine did not initialize")
    return proc


def run(command: list[str], *, env: dict[str, str], cwd: str,
        dns_servers: tuple[str, ...], upstream: dict[str, Any] | None,
        timeout: float | None = None) -> int:
    """Run a workload in a new client-local user and network namespace."""
    capability = probe()
    if capability["status"] != "supported":
        raise RuntimeError(capability["reason"])
    engine = engine_path()
    unshare = shutil.which("unshare")
    assert engine and unshare

    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    child_env = dict(env)
    module_root = str(Path(__file__).resolve().parent.parent)
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (module_root, existing_pythonpath) if item)
    child_env["EGRESS_TUN_MODULE_ROOT"] = module_root
    child_env["EGRESS_TUN_CONTROL_FD"] = str(child_sock.fileno())
    child_env["EGRESS_TUN_DNS"] = ",".join(dns_servers)
    bootstrap = [sys.executable, "-m", BOOTSTRAP_MODULE, "--", *command]
    proc = None
    engine_proc = None
    tun_fd = None
    try:
        proc = subprocess.Popen(
            [unshare, "--user", "--map-root-user", "--net", "--fork", *bootstrap],
            cwd=cwd, env=child_env, close_fds=True, pass_fds=(child_sock.fileno(),),
            start_new_session=True,
        )
        child_sock.close()
        parent_sock.settimeout(20)
        message, tun_fd = _recv_with_fd(parent_sock)
        if message.get("event") != "tun-ready" or tun_fd is None:
            raise RuntimeError(str(message.get("error") or "namespace bootstrap failed"))

        engine_proc = _start_engine(engine, tun_fd, upstream)
        os.close(tun_fd)
        tun_fd = None
        parent_sock.sendall(b"start")
        parent_sock.settimeout(None)
        old_handlers = {}
        def forward(signum, _frame):
            try:
                os.killpg(proc.pid, signum)
            except ProcessLookupError:
                pass
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, forward)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            return 124
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
        return 128 + -rc if rc < 0 else rc
    finally:
        parent_sock.close()
        child_sock.close()
        if tun_fd is not None:
            os.close(tun_fd)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if engine_proc is not None:
            engine_proc.terminate()
            try:
                engine_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                engine_proc.kill()
                engine_proc.wait()
