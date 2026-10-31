# Offline installer test suite

This suite exercises `scripts/install-backend-host.sh` and `host/egress-host-test` without a Docker daemon or root access.

## Run

```sh
python3 -m pytest tests/install/ -q
bash tests/install/test_install_scripts.sh
```

The suite is also included in `python3 -m pytest tests/ client/tests/ -q`.

## Test harness

Fixed command stubs are prepended to `PATH` only during each scenario. The Docker stub covers Compose version/build/up/ps, inspect, logs, and network queries. Its environment controls include `STUB_DOCKER_UP_RC`, `STUB_DOCKER_BUILD_RC`, `STUB_DOCKER_HEALTH` (`healthy`, `restarting`, or `never`), and `STUB_DOCKER_LOGS`. Other fixed stubs cover `egressctl`, `id`, `sleep`, `ufw`, `systemctl`, `findmnt`, and `df`.

The harness covers shell/Python syntax, successful installation and structured logs, deployment and health-gate failures, root enforcement, static security checks, and backend bind verification. Health polling runs without real delays while retaining the production decision logic.

Every scenario skips the host client installation, which writes to `/usr/local` and `/etc/egress`; it provisions a temporary bundle with the real provisioning script instead. The client wheel and its install layout are covered by focused tests. The host harness receives an isolated empty client prefix so the suite is independent of packages already installed on the runner.

## Add scenarios

Add a scenario to `test_install_scripts.sh` using its environment/setup helpers. Document new stub controls here and alongside the relevant stub. Stubs must remain fixed-operation; do not add free-form command input.
