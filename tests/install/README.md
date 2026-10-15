# Оффлайн-свит install-скриптов (tests/install)

Воспроизводимые тесты `scripts/install-backend-host.sh` и harness
`host/egress-host-test` БЕЗ Docker daemon и БЕЗ root. Возрождают утраченный
`/tmp/a1_test.sh` (см. HANDOFF-install-logging.md, контекст, п. 4).

## Запуск

```bash
python3 -m pytest tests/install/ -q        # через pytest-обёртку
bash tests/install/test_install_scripts.sh # или напрямую
```

Свит входит в общий оффлайн-прогон: `python3 -m pytest tests/ client/tests/ -q`.

## Как устроено

- `stubs/` — фиксированные bash-заглушки, ставятся в FRONT PATH только на
  время сценария:
  - `docker` — compose version/build/up/ps, inspect --format, logs --tail,
    network ls/inspect. Поведение управляется env:
    - `STUB_DOCKER_UP_RC` (default 0) — rc `compose up -d`;
    - `STUB_DOCKER_BUILD_RC` (default 0) — rc `compose build`;
    - `STUB_DOCKER_HEALTH` = `healthy` (default) | `restarting` | `never`:
      - `healthy` — running + healthy + RestartCount=0;
      - `restarting` — `State.Status=restarting` при **усталом**
        `Health.Status=healthy` и `RestartCount=1` (повтор бага 2026-10-12:
        однократный inspect мог показать healthy у restarting-контейнера);
      - `never` — `Health.Status=starting` навсегда (timeout-ветка).
    - `STUB_DOCKER_LOGS` — файл, содержимое которого отдаёт `docker logs`.
  - `egressctl` — `--help`, `--json whoami|route current|mode current`;
  - `id` — `id -u [target]`; `STUB_ID_UID` (default 0) — «текущий» uid
    (нужен для сценария root-escape в root-контейнере);
  - `sleep` — no-op (health gate 45×2с прогоняется мгновенно; сама логика
    цикла — 3 последовательных полла, счётчик, timeout — выполняется в
    полном объёме);
  - `ufw`, `systemctl`, `findmnt`, `df` — no-op с правдоподобным выводом.
- `test_install_scripts.sh` — сценарии:
  0. синтаксис: `bash -n` всех `scripts/*.sh`, `py_compile` host-утилит;
  1. success-путь: rc 0; лог-файл с run header (hostname/kernel/user,
     docker/compose версии, git rev, argv) и маркерами всех 10 шагов;
     `generated/config.yaml`; секреты 0600; harness-репорт 0 failed;
     JSON-репорт harness целиком в лог;
  2. fail A: `STUB_DOCKER_UP_RC=1` → rc 3; в логе `step 9`,
     `docker compose up -d`, `full log:`;
  3. fail B: `STUB_DOCKER_HEALTH=never` → rc 3; дамп `docker logs` egressd
     + `RestartCount` в лог; значение mihomo_api_secret в логе контейнера
     возвращается только как `[REDACTED]`;
  4. fail C: `STUB_DOCKER_HEALTH=restarting` → rc 3 (усталый healthy
     reading не проходит health gate — 3 последовательных полла +
     RestartCount==0);
  5. root-escape: `--help` работает без root; запуск без
     `VIBELAB_EGRESS_TEST_MODE` под non-root → exit 3 «must run as root»;
  6. negative-harness (`--static`, tmp-деревья): публичный бинд
     `0.0.0.0:1080` → `no_public_binds=fail`; маунт `docker.sock` →
     `docker_socket_not_mounted=fail`; provider-секрет в client-бандле →
     `no_backend_secrets_in_client_bundles=fail`.
  7. `verify-backend-host.sh`: live-allowlist биндов поверх stubbed docker —
     безопасные бинды (127.0.0.1 + gateway) → rc 0; публичный бинд
     `0.0.0.0` → rc 3 с указанием offending-маппинга.

## Ограничения (отличие от среды work order)

Work order предполагал root-контейнер. В non-root окружении (host) полный
client-шаг установки невозможен: `scripts/install-client.sh` пишет в
`/usr/local` и `/etc/vibelab-egress` (root-only). Поэтому:

- **root**: success-путь — ровно флаги work order
  (`--yes --skip-firewall`, без `--skip-client`) — клиент ставится по-
  настоящему;
- **non-root**: success-путь идёт с `--skip-client`, а бандл + client-
  секреты создаёт сам свит реальным `scripts/provision-client.sh` (состояние
  полностью то же, что оставил бы шаг 7). `client_package_importable` в
  harness при этом skipped (префикс `/usr/local/lib/vibelab-egress` не
  установлен) — это ожидаемо и задокументировано в docstring чекса.

## Расширение

Новый сценарий = новый блок в `test_install_scripts.sh`
(`make_env`/`scenario_install` + `check`); новая управляемая переменная
stubs — док-строка в этом README + комментарий в `stubs/docker`. Не
забывайте: stubs — фиксированное поведение, никакого free-form входа.
