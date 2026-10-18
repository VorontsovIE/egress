"""Host-native per-run network namespace backend.

This module is deliberately usable only by the local, privileged UNIX-socket
helper. All subprocess arguments are constructed here; requests never supply
an executable, namespace name, interface name, or firewall command.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import pwd
import re
import select
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path("/run/vibelab-egress/jobs")
NETNS_PREFIX = "egress-tun-"
MIHOMO_PATH = "/usr/local/libexec/vibelab-egress/mihomo"
SERVICE_USER = "vibelab-egress-worker"
MAX_JOBS = 8
STALE_TTL_S = 300
_NAME_RE = re.compile(r"^egress-tun-[0-9a-f]{12}$")


class NetworkSetupError(RuntimeError):
    """A setup step failed before the workload was started."""


def _run(argv: list[str], *, input_text: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, input=input_text, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False)
    if check and result.returncode:
        # Never include argv: it must remain free of secrets, and avoiding it
        # here protects against future accidental additions.
        raise NetworkSetupError(
            f"network setup failed ({os.path.basename(argv[0])}, rc={result.returncode}): "
            f"{result.stderr.strip()[:160]}")
    return result


def _service_identity() -> tuple[int, int]:
    entry = pwd.getpwnam(SERVICE_USER)
    return entry.pw_uid, entry.pw_gid


def _job_name() -> str:
    return NETNS_PREFIX + os.urandom(6).hex()


def _interface_names(name: str) -> tuple[str, str]:
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    return "eh" + digest, "en" + digest


def _address_pair(name: str, used: set[str]) -> tuple[str, str, str]:
    """Allocate a /30 in the reserved link-local pool, avoiding live jobs."""
    base = int(ipaddress.IPv4Address("169.254.240.0"))
    first = int(hashlib.sha256(name.encode()).hexdigest()[:4], 16) % 1024
    for offset in range(1024):
        idx = (first + offset) % 1024
        net = ipaddress.IPv4Network((base + 4 * idx, 30))
        if str(net) not in used:
            hosts = list(net.hosts())
            return str(net), str(hosts[0]), str(hosts[1])
    raise NetworkSetupError("no free per-run veth subnet")


class NativeNetnsBackend:
    """Owns only resources bearing our random per-run identifier."""

    def __init__(self, *, state_dir: Path = STATE_DIR,
                 mihomo_path: str = MIHOMO_PATH,
                 gateway: str = "172.17.0.1") -> None:
        self.state_dir = Path(state_dir)
        self.mihomo_path = mihomo_path
        self.gateway = str(ipaddress.IPv4Address(gateway))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _, service_gid = _service_identity()
        os.chown(self.state_dir, 0, service_gid)
        os.chmod(self.state_dir, 0o710)
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def prerequisites(mihomo_path: str = MIHOMO_PATH) -> list[str]:
        missing = [name for name in ("ip", "nft", "iptables", "setpriv")
                   if shutil.which(name) is None]
        if not os.path.isfile(mihomo_path) or not os.access(mihomo_path, os.X_OK):
            missing.append("pinned host mihomo")
        try:
            _service_identity()
        except KeyError:
            missing.append("service account")
        return missing

    def _used_subnets(self) -> set[str]:
        used: set[str] = set()
        for entry in self.state_dir.glob("*/state.json"):
            try:
                used.add(json.loads(entry.read_text())["subnet"])
            except (OSError, ValueError, KeyError):
                continue
        return used

    def create(self, name: str) -> None:
        if not _NAME_RE.fullmatch(name):
            raise NetworkSetupError("invalid generated namespace name")
        with self._lock:
            if len(self.jobs) >= MAX_JOBS:
                raise NetworkSetupError("too many concurrent TUN jobs")
            host_if, ns_if = _interface_names(name)
            subnet, host_ip, ns_ip = _address_pair(name, self._used_subnets())
            directory = self.state_dir / name
            directory.mkdir(mode=0o700)
            state = {"name": name, "host_if": host_if, "ns_if": ns_if,
                     "subnet": subnet, "host_ip": host_ip, "ns_ip": ns_ip,
                     "created": time.time(), "mihomo_pid": None}
            self.jobs[name] = state
            (directory / "state.json").write_text(json.dumps(state))
            os.chmod(directory / "state.json", 0o600)
        try:
            # ip netns exec bind-mounts this resolver into the child's mount
            # namespace. A host systemd stub (127.0.0.53) is unreachable from
            # this network namespace. DNS packets then follow the selected
            # direct or TUN route under the namespace firewall.
            resolver_dir = Path("/etc/netns") / name
            resolver_dir.mkdir(mode=0o700, parents=True)
            (resolver_dir / "resolv.conf").write_text("nameserver 1.1.1.1\noptions timeout:2 attempts:2\n")
            _run(["ip", "netns", "add", name])
            _run(["ip", "link", "add", host_if, "type", "veth", "peer", "name", ns_if])
            _run(["ip", "link", "set", ns_if, "netns", name])
            _run(["ip", "addr", "add", f"{host_ip}/30", "dev", host_if])
            _run(["ip", "link", "set", host_if, "up"])
            for argv in (
                ["ip", "link", "set", "lo", "up"],
                ["ip", "addr", "add", f"{ns_ip}/30", "dev", ns_if],
                ["ip", "link", "set", ns_if, "up"],
                ["ip", "route", "add", f"{self.gateway}/32", "via", host_ip],
            ):
                _run(["ip", "netns", "exec", name, *argv])
        except Exception:
            self.destroy(name)
            raise

    def _install_host_rules(self, name: str, *, direct: bool) -> None:
        state = self.jobs[name]
        iface = state["host_if"]
        # The one-shot listener remains bound to the Docker gateway only.
        # Rules are scoped to this veth and removed with the job.
        for proto in ("tcp", "udp"):
            _run(["iptables", "-w", "-I", "INPUT", "1", "-i", iface,
                  "-d", self.gateway, "-p", proto, "--dport", "1081", "-j", "ACCEPT"])
            if not direct:
                # Docker may DNAT the gateway publication before INPUT. In
                # that case it traverses FORWARD with container port 18081;
                # match the original destination so no other container port
                # becomes reachable from the strict namespace.
                _run(["iptables", "-w", "-I", "FORWARD", "1", "-i", iface,
                      "-p", proto, "-m", "conntrack", "--ctorigdst", self.gateway,
                      "--ctorigdstport", "1081", "-j", "ACCEPT"])
        if direct:
            _run(["iptables", "-w", "-I", "FORWARD", "1", "-i", iface,
                  "-j", "ACCEPT"])
            _run(["iptables", "-w", "-I", "FORWARD", "1", "-o", iface,
                  "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
                  "-j", "ACCEPT"])
            _run(["iptables", "-w", "-t", "nat", "-A", "POSTROUTING",
                  "-s", state["subnet"], "-j", "MASQUERADE"])
        else:
            _run(["iptables", "-w", "-I", "FORWARD", "1", "-o", iface,
                  "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
                  "-j", "ACCEPT"])
        state["host_rules"] = True

    def _install_ns_filter(self, name: str, *, direct: bool) -> None:
        state = self.jobs[name]
        ns_if = state["ns_if"]
        uid, _ = _service_identity()
        if direct:
            output = f'oifname "lo" accept\n oifname "{ns_if}" accept'
        else:
            output = (f'oifname "lo" accept\n oifname "tun0" accept\n'
                      f'oifname "{ns_if}" meta skuid {uid} ip daddr {self.gateway} '
                      'tcp dport 1081 accept\n'
                      f'oifname "{ns_if}" meta skuid {uid} ip daddr {self.gateway} '
                      'udp dport 1081 accept\n'
                      f'oifname "{ns_if}" ip daddr {self.gateway} '
                      'tcp dport 9091 accept')
        rules = ("table inet egress_tun {\n"
                 " chain output { type filter hook output priority 0; policy drop;\n"
                 f" {output}\n }}\n"
                 " chain input { type filter hook input priority 0; policy drop;\n"
                 " iifname \"lo\" accept\n ct state established,related accept\n"
                 " iifname \"tun0\" accept\n }\n}\n")
        _run(["ip", "netns", "exec", name, "nft", "-f", "-"], input_text=rules)

    def pin_direct(self, name: str) -> None:
        state = self.jobs[name]
        self._install_ns_filter(name, direct=True)
        self._install_host_rules(name, direct=True)
        _run(["ip", "netns", "exec", name, "ip", "route", "add", "default",
              "via", state["host_ip"]])

    def pin_route(self, name: str, upstream: dict[str, Any]) -> None:
        state = self.jobs[name]
        uid, gid = _service_identity()
        # The caller can name only the configured host listener. Inside the
        # namespace, its host-loopback address is unreachable; use the
        # separately configured Docker gateway publication instead.
        if (upstream.get("host"), upstream.get("port")) != ("127.0.0.1", 1081):
            raise NetworkSetupError("upstream is not the configured host one-shot listener")
        directory = self.state_dir / name
        config = {
            "mode": "rule", "log-level": "warning", "ipv6": False,
            "allow-lan": False,
            # DNS packets from the workload enter tun0 and are handled by
            # Mihomo's internal resolver. The resolver's DoH connection is
            # explicitly sent through the selected one-shot proxy, so the
            # strict namespace never needs direct access to a DNS server.
            "dns": {"enable": True, "ipv6": False,
                    "default-nameserver": ["1.1.1.1"],
                    "nameserver": ["https://1.1.1.1/dns-query#oneshot"]},
            "tun": {"enable": True, "stack": "mips", "device": "tun0",
                    "auto-route": False, "auto-detect-interface": False,
                    "dns-hijack": ["any:53", "tcp://any:53"]},
            "proxies": [{"name": "oneshot", "type": "socks5",
                         "server": self.gateway, "port": 1081,
                         "interface-name": state["ns_if"],
                         "username": upstream.get("username", ""),
                         "password": upstream.get("password", ""),
                         "udp": True}],
            "rules": ["MATCH,oneshot"],
        }
        home = directory / "mihomo"
        home.mkdir(mode=0o770)
        os.chown(home, 0, gid)
        os.chmod(home, 0o770)
        os.chown(directory, 0, gid)
        os.chmod(directory, 0o710)
        cfg_path = home / "config.yaml"
        cfg_path.write_text(json.dumps(config))  # JSON is valid YAML.
        os.chmod(cfg_path, 0o600)
        # The helper's capability set omits CAP_FOWNER. Set the mode while
        # root still owns the file, then transfer ownership to the worker.
        os.chown(cfg_path, uid, gid)
        _run(["ip", "netns", "exec", name, "ip", "tuntap", "add",
              "dev", "tun0", "mode", "tun", "user", str(uid)])
        _run(["ip", "netns", "exec", name, "ip", "addr", "add",
              "198.18.0.1/30", "dev", "tun0"])
        _run(["ip", "netns", "exec", name, "ip", "link", "set", "tun0", "up"])
        self._install_ns_filter(name, direct=False)
        self._install_host_rules(name, direct=False)

        process = subprocess.Popen(
            ["ip", "netns", "exec", name, "setpriv",
             f"--reuid={uid}", f"--regid={gid}", "--clear-groups",
             "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all",
             self.mihomo_path, "-d", str(home), "-f", str(cfg_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        state["mihomo_pid"] = process.pid
        state["mihomo_process"] = process
        (directory / "state.json").write_text(json.dumps(
            {k: v for k, v in state.items() if k != "mihomo_process"}))
        _run(["ip", "netns", "exec", name, "ip", "route", "add", "default",
              "dev", "tun0"])
        # A socket check validates startup without logging the per-run config.
        time.sleep(0.2)
        if process.poll() is not None:
            raise NetworkSetupError("per-run Mihomo exited during startup")

    def exec_in(self, name: str, command: list[str], uid: int, gid: int,
                timeout: float, *, groups: list[int], env: dict[str, str],
                cwd: str, umask: int, stdio: tuple[int, int, int],
                cancel_sock: socket.socket | None = None) -> int:
        group_flag = ["--groups=" + ",".join(map(str, groups))] if groups else ["--clear-groups"]
        launcher = str(Path(__file__).with_name("exec_as_caller.py"))
        process = subprocess.Popen(
            ["ip", "netns", "exec", name, "setpriv",
             f"--reuid={uid}", f"--regid={gid}", *group_flag,
             "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all",
             "/usr/bin/python3", launcher, cwd, str(umask), *command],
            stdin=stdio[0], stdout=stdio[1], stderr=stdio[2],
            env=env, close_fds=True, start_new_session=True)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if time.monotonic() >= deadline:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return 124
            if cancel_sock and select.select([cancel_sock], [], [], 0)[0]:
                if not cancel_sock.recv(1, socket.MSG_PEEK):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    return 143
            time.sleep(0.1)
        rc = process.returncode
        return 128 - rc if rc < 0 else rc

    def destroy(self, name: str) -> None:
        if not _NAME_RE.fullmatch(name):
            return
        with self._lock:
            state = self.jobs.pop(name, None)
        directory = self.state_dir / name
        if state is None:
            try:
                state = json.loads((directory / "state.json").read_text())
            except (OSError, ValueError):
                state = {}
        process = state.get("mihomo_process")
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        # Background children of the workload may outlive its initial PID.
        # A restarted helper also must not trust a saved PID (PID reuse).
        # Kill only processes still inside OUR randomly named namespace.
        pids = _run(["ip", "netns", "pids", name], check=False)
        for item in pids.stdout.split():
            if item.isdecimal():
                try:
                    os.kill(int(item), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if pids.stdout.strip():
            time.sleep(0.2)
            remaining = _run(["ip", "netns", "pids", name], check=False)
            for item in remaining.stdout.split():
                if item.isdecimal():
                    try:
                        os.kill(int(item), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        host_if = state.get("host_if")
        subnet = state.get("subnet")
        if host_if and re.fullmatch(r"eh[0-9a-f]{10}", host_if):
            # Remove exact rules only; never flush operator or Docker tables.
            for proto in ("tcp", "udp"):
                _run(["iptables", "-w", "-D", "INPUT", "-i", host_if,
                      "-d", self.gateway, "-p", proto, "--dport", "1081", "-j", "ACCEPT"],
                     check=False)
                _run(["iptables", "-w", "-D", "FORWARD", "-i", host_if,
                      "-p", proto, "-m", "conntrack", "--ctorigdst", self.gateway,
                      "--ctorigdstport", "1081", "-j", "ACCEPT"], check=False)
            for argv in (
                ["iptables", "-w", "-D", "FORWARD", "-i", host_if, "-j", "ACCEPT"],
                ["iptables", "-w", "-D", "FORWARD", "-o", host_if,
                 "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
                ["ip", "link", "del", host_if],
            ):
                _run(argv, check=False)
        if subnet:
            _run(["iptables", "-w", "-t", "nat", "-D", "POSTROUTING",
                  "-s", subnet, "-j", "MASQUERADE"], check=False)
        _run(["ip", "netns", "del", name], check=False)
        shutil.rmtree(directory, ignore_errors=True)
        shutil.rmtree(Path("/etc/netns") / name, ignore_errors=True)

    def cleanup_stale(self, *, now: float | None = None,
                      ttl: float = STALE_TTL_S) -> list[str]:
        now = time.time() if now is None else now
        removed = []
        for directory in self.state_dir.iterdir():
            if not directory.is_dir() or not _NAME_RE.fullmatch(directory.name):
                continue
            try:
                state = json.loads((directory / "state.json").read_text())
                age = now - float(state["created"])
            except (OSError, ValueError, KeyError):
                age = ttl + 1
            if age > ttl and directory.name not in self.jobs:
                self.destroy(directory.name)
                removed.append(directory.name)
        return removed
