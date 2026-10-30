"""The shell installer accepts client-only mode without sudo escalation."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "egress-install"


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def test_client_only_installer_parses_and_never_calls_sudo(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "sudo-called"
    executable(bin_dir / "sudo", f"touch '{marker}'\nexit 97\n")
    executable(
        bin_dir / "curl",
        """
out=
previous=
for arg in "$@"; do
  if [ "$previous" = -o ]; then out="$arg"; fi
  previous="$arg"
done
case "$out" in
  */release.json) printf '{"tag_name":"v1.2.3"}' > "$out" ;;
  *) printf 'test release payload' > "$out" ;;
esac
""",
    )
    executable(
        bin_dir / "python3",
        """
if [ "${1:-}" = - ]; then
  shift
  cat >/dev/null
  case "$#" in
    1) echo v1.2.3 ;;
    2) echo "https://assets.invalid/$2" ;;
    *) exit 81 ;;
  esac
  exit 0
fi
if [ "${1:-}" = -m ] && [ "${2:-}" = venv ]; then
  env="$3"
  mkdir -p "$env/bin"
  printf '#!/bin/sh\\nexit 0\\n' > "$env/bin/python"
  chmod 0755 "$env/bin/python"
  exit 0
fi
exit 82
""",
    )
    for command in ("gh", "sha256sum"):
        executable(bin_dir / command, "exit 0\n")

    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "USER": "alice",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    })
    completed = subprocess.run(
        ["bash", str(INSTALLER), "--client-only"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "client v1.2.3 installed" in completed.stdout
    assert not marker.exists()
    base = home / ".local" / "share" / "egress"
    assert (base / "current").is_symlink()
    assert os.readlink(base / "current") == "versions/1.2.3"
    assert (home / ".local" / "bin" / "egressctl").is_file()
    assert (home / ".local" / "bin" / "egress-run").is_file()
