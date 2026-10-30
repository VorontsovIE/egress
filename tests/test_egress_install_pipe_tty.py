"""Full-install bootstrap must use the controlling TTY when stdin is a pipe."""
from __future__ import annotations

import json
import os
import pty
import select
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "egress-install"


def _run_pipe_under_pty(command: str, env: dict[str, str], answer: bytes) -> tuple[int, bytes]:
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(env["TEST_CWD"])
        os.execve("/bin/bash", ["bash", "-c", command], env)

    output = bytearray()
    sent = False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if not sent and b"sudo credential prompt" in output:
                    os.write(master, answer)
                    sent = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return os.waitstatus_to_exitcode(status), bytes(output)
        else:
            os.kill(pid, 9)
            _, status = os.waitpid(pid, 0)
            raise AssertionError(f"installer timed out; output: {bytes(output)!r}")
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), bytes(output)
    finally:
        os.close(master)


def test_full_bootstrap_accepts_piped_script_with_controlling_tty(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    release_json = tmp_path / "release.json"
    archive_path = tmp_path / "release.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        body = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo("egress-1.2.3/scripts/install-backend-host.sh")
        info.mode = 0o755
        info.size = len(body)
        import io
        archive.addfile(info, io.BytesIO(body))
    release_json.write_text(json.dumps({
        "tag_name": "v1.2.3",
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": "egress-1.2.3.tar.gz",
            "browser_download_url": "https://assets.invalid/egress-1.2.3.tar.gz",
        }],
    }))

    curl = stub_dir / "curl"
    curl.write_text("""#!/bin/sh
set -eu
out=
url=
previous=
for arg in "$@"; do
  if [ "$previous" = -o ]; then out="$arg"; fi
  case "$arg" in https://*) url="$arg" ;; esac
  previous="$arg"
done
case "$url" in
  *api.github.com*) cp "$TEST_RELEASE_JSON" "$out" ;;
  *) cp "$TEST_RELEASE_ARCHIVE" "$out" ;;
esac
""")
    curl.chmod(0o755)

    gh = stub_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)

    sudo_log = tmp_path / "sudo.log"
    root_tmp = tmp_path / "privileged"
    root_tmp.mkdir()
    deployment = tmp_path / "backend-deploy-reached"
    sudo = stub_dir / "sudo"
    sudo.write_text("""#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$SUDO_LOG"
case "$1" in
  -v)
    echo 'sudo credential prompt' >&2
    [ -t 0 ] || { echo 'sudo stdin was not a tty' >&2; exit 91; }
    IFS= read -r answer
    [ "$answer" = test-password ] || exit 92
    echo 'SUDO_VALIDATED' >&2
    ;;
  mktemp)
    mkdir -p "$SUDO_ROOT_WORK/release"
    echo "$SUDO_ROOT_WORK/release"
    ;;
  install|gh)
    exit 0
    ;;
  tar)
    dest=
    while [ "$#" -gt 0 ]; do
      if [ "$1" = -C ]; then shift; dest="$1"; fi
      shift
    done
    mkdir -p "$dest/egress-1.2.3/scripts"
    printf '#!/bin/sh\\nexit 0\\n' > "$dest/egress-1.2.3/scripts/install-backend-host.sh"
    ;;
  test)
    shift
    /usr/bin/test "$@"
    ;;
  env)
    case "$*" in
      *install-backend-host.sh*) touch "$DEPLOYMENT_MARKER" ;;
      *) exit 93 ;;
    esac
    ;;
  rm)
    target=
    for arg in "$@"; do target="$arg"; done
    case "$target" in "$SUDO_ROOT_WORK"/*) rm -rf -- "$target" ;; *) exit 94 ;; esac
    ;;
  *) echo "unexpected sudo command: $*" >&2; exit 95 ;;
esac
""")
    sudo.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "USER": "alice",
        "TMPDIR": str(tmp_path),
        "TEST_CWD": str(tmp_path),
        "TEST_RELEASE_JSON": str(release_json),
        "TEST_RELEASE_ARCHIVE": str(archive_path),
        "SUDO_LOG": str(sudo_log),
        "SUDO_ROOT_WORK": str(root_tmp),
        "DEPLOYMENT_MARKER": str(deployment),
    })
    code, output = _run_pipe_under_pty(f"cat '{INSTALLER}' | bash", env,
                                      b"test-password\n")

    decoded = output.decode(errors="replace")
    assert code == 0, decoded
    assert "This installs the egress backend and system client" in decoded
    assert "SUDO_VALIDATED" in decoded
    assert decoded.index("Administrator access is") < decoded.index("sudo credential prompt")
    assert deployment.exists(), decoded
    calls = sudo_log.read_text().splitlines()
    assert calls[0] == "-v"
    assert any(call.startswith("mktemp -d /var/tmp/egress-install.") for call in calls)
    assert not root_tmp.joinpath("release").exists()
