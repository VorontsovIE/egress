# INSTALL — vibelab-egress

Два **независимых** шага (spec section 32):

- **A. Backend deployment** — один раз на Docker host (root).
- **B. Client installation** — в каждую execution environment (host,
  OpenScience/gVisor, другие контейнеры/VM) независимо.

Один и тот же client package (`egressctl` + `egress-run`, stdlib-only)
устанавливается везде; различаются только `client.yaml` и secret files.

---

## A. Backend deployment (Docker host)

### Prerequisites

- Docker Engine + Compose v2 (`docker compose version`);
- python3 (для host-утилит, без сторонних пакетов);
- root на host;
- доступ в интернет для загрузки GEO-данных и образа `metacubex/mihomo`
  (если GEO-URL недоступны — до установки выполнить `sh scripts/fetch-geo-data.sh`:
  скрипт-установки seed-ит mihomo из `host/geo/`, иначе mihomo скачает сам
  через `geox-url`).

### Persistent paths

Persistent root (по умолчанию `/home/vibelab/openscience/egress`,
настраивается `--root` / `VIBELAB_EGRESS_ROOT`):

```text
<root>/
  mihomo/        # bbolt state (store-selected), logs
  providers/     # provider subscriptions (read-only mount)
  generated/     # generated mihomo config + docker-cidrs.yaml
  egressd/       # metrics.sqlite, session state
  geo/           # GeoIP.dat, GeoSite.dat, ASN.mmdb
```

Перед install скрипт показывает `findmnt -T` и `df -h` для выбранного
filesystem — убедитесь, что это достаточно большой диск (GEO ~30 MB,
state растёт со временем).

### Secrets

`/etc/vibelab-egress/secrets/` (0700/0600, генерируются скриптом,
нигде не печатаются):

```text
mihomo_api_secret      # Mihomo admin API (только egressd)
devbridge.token        # narrow dev/test bridge
providers/             # subscription-секреты (blanc, xeovo, custom VLESS)
clients/               # <id>.token, <id>.profile.json, <id>.proxy-creds
```

Provider subscriptions кладутся в `providers/` вручную (или через
adapters, A2) **до** первого deploy, либо добавляются позже — egressd
подхватит при update.

### Firewall

Публикуются только: `127.0.0.1:9091/1080/1081` (host clients) и
`<docker-gateway>:9091/1080/1081` (container clients). Mihomo admin API
(9090) **не публикуется вообще** — только private compose network.
Скрипт применяет базовые ufw-правила (если ufw есть): lo + gateway +
каждая docker-подсеть из артефакта `generated/docker-cidrs.yaml`
(контейнерные клиенты стучат в опубликованные порты со своих container
IP, а не с gateway-адреса — spec 36). Сети, созданные после установки,
нужно разрешить вручную: `ufw allow from <cidr> to any port
9091,1080:1081 proto tcp`. При nftables — сделайте эквивалент вручную:
разрешите 9091/1080/1081 только с lo и docker-подсетями, заблокируйте
9090 наружу.

### Compose deployment

```bash
scripts/install-backend-host.sh            # полный bootstrap (root)
```

Скрипт: создаёт persistent dirs → генерирует secrets → GEO-seed (из
`host/geo/`, если присутствует; иначе mihomo скачает сам) →
sync Docker CIDRs (+ systemd timer) → firewall → `docker compose build
egressd && docker compose up -d` → provision+install host client →
host acceptance harness. Переключатели: `--root`, `--secrets-dir`,
`--docker-gateway`, `--skip-client`, `--skip-firewall`, `--yes`.

Mihomo image pinned: `metacubex/mihomo:v1.19.32` (override `MIHOMO_IMAGE`).

### Client provisioning (после backend)

```bash
# host client (обычно делает install-backend-host.sh сам)
scripts/provision-client.sh --client host-main --environment host

# OpenScience client
scripts/provision-client.sh --client openscience-main --environment gvisor-container
```

Для каждого client генерируются: bundle (`client.yaml`, `client.token`,
`default-proxy.auth`) + backend-секреты (`clients/<id>.*`). Provider
secrets в bundle **не попадают**.

### Dev/test bridge (нужен, если агент работает из OpenScience)

```bash
scripts/bootstrap-dev-access.sh            # root, один раз
```

Поднимает `egress-devbridge` (только фиксированные операции:
deploy/redeploy/status/logs/run-acceptance/sync-docker-cidrs/
install-client; bearer-token auth; bind на docker gateway). Агенту
даётся endpoint + путь к token-файлу (значение токена нигде не
печатается).

### Verify / rollback / uninstall

```bash
scripts/verify-backend-host.sh             # compose, binds, socket, secrets, harness
scripts/uninstall-backend-host.sh          # down stack, remove units/secrets
                                           # (persistent data сохраняется,
                                           #  --remove-data — удалить)
```

### Логи и диагностика установки

**Где лежат логи.**
- Установка: `<root>/install-backend-host.log` (append; по умолчанию
  `/home/vibelab/openscience/egress/install-backend-host.log`).
- Верификация: `<root>/verify-backend-host.log` (append, тот же формат).

**Формат строк.** Каждая строка: `[<UTC timestamp>] [step N/10] <message>`,
где N — номер шага из 10 (1 = prerequisites, 2 = persistent root,
3 = backend secrets, 4 = GEO, 5 = CIDR sync, 6 = firewall, 7 = client,
8 = bootstrap config, 9 = compose deploy, 10 = acceptance harness).
Файл начинается с run header: hostname, kernel, пользователь, версии
docker/compose/python3, git-rev репозитория, все аргументы запуска
(только пути — значения секретов в лог не попадают никогда).

В лог также тупится вывод ключевых subprocess'ов: `docker compose
build/up/ps`, `egress-hostctl sync-docker-cidrs`,
`egressd.bootstrap_config` и **целиком** JSON-репорт harness
(`egress-host-test`). При timeout health gate в лог пишутся поля
`docker inspect` (State/Health/RestartCount/ExitCode/Error/OOMKilled) и
`docker logs --tail 100` каждого сервиса — значения известных backend
секретов в дампах замазаны как `[REDACTED]`.

**Exit codes и что делать.**

| exit | значение | что делать |
|------|----------|------------|
| 0 | установка завершена, acceptance PASS | — |
| 2 | ошибка аргументов | поправить флаги (`--help`), повторить |
| 3 | окружение/инфраструктура (docker, compose build/up, CIDR sync, bootstrap, health gate) | читать лог: строка `ERROR: step=N (...) command='...' exit=<rc>` + `full log: <путь>`; затем `docker logs <vibelab-egress-egressd\|vibelab-egress-mihomo>`, исправить причину, повторить |
| 4 | acceptance harness не прошёл | открыть `<root>/acceptance-report.json` (JSON есть и в лог целиком), по detail каждого failed-чека + блоку `diagnosis` (live-режим) |

Установка **идемпотентна**: повторный запуск после падения безопасен —
секреты сохраняются (генерируются один раз), каталоги переиспользуются,
compose пересобирает/перезапускает только то, что нужно.

**Health gate.** Считается успешным только после 3 последовательных
поллов (интервал 2 с), где каждый сервис `running + healthy +
RestartCount=0`; timeout 90 с. Один «усталый» healthy-reading у
restarting-контейнера (баг 2026-10-12) прохождения не даёт.

---

## B. Client installation in each execution environment

Контейнерные окружения резолвят backend через `host.docker.internal`. На
Linux Docker-демон эту запись **не** создаёт по умолчанию: контейнер нужно
поднимать с `--add-host=host.docker.internal:host-gateway` (или `extra_hosts`
в compose), иначе `egressctl whoami` упадёт с `Name or service not known`.

**Как перенести bundle в контейнер.** `install-client.sh` читает bundle из
файловой системы самого контейнера. Копируйте в **свежий** каталог (старый
сначала удалите):

```bash
docker exec -u root <container> rm -rf /root/egress-client-bundle
docker cp <root>/client-bundles/<id> <container>:/root/egress-client-bundle
```

Нюанс `docker cp`: копирование каталога в **уже существующий** каталог
контейнера вкладывает его внутрь (`/root/egress-client-bundle/<id>/`), а
старые файлы верхнего уровня остаются. После ротации токена клиент будет
авторизоваться старым токеном (`unknown token`), хотя на backend уже новый —
легко спутать с багом авторизации egressd.

Для каждого client profile:

1. **Одинаковый package** `egressctl` + `egress-run` (stdlib-only,
   устанавливается без package index):

   ```bash
   scripts/install-client.sh --client <id> --target host \
       --bundle <root>/client-bundles/<id>
   # или внутри OpenScience:
   scripts/install-client.sh --client <id> --target gvisor-container \
       --bundle <bundle>
   ```

2. **Environment-specific** `/etc/vibelab-egress/client.yaml`
   (генерируется provisioning, не копится вручную; override
   `VIBELAB_EGRESS_CONFIG`).

3. **Отдельные read-only secrets**: API token + default-proxy auth в
   отдельных файлах (host: `/etc/vibelab-egress/*.token|*.auth` 0600;
   OpenScience: `/run/secrets/vibelab_egress_*` read-only mount; в plain-
   контейнере без такого маунта `install-client.sh` сам создаёт
   `/run/secrets` в writable rootfs).

4. **Skill** (`skills/egress/SKILL.md`) — устанавливается, если
   environment agent-capable (OpenScience).

5–7. **Проверки**:

   ```bash
   egressctl config validate
   egressctl whoami
   egressctl doctor
   # или одним скриптом:
   scripts/verify-client.sh [--config <path>] [--offline]
   ```

### Reproducible OpenScience installation

Production OpenScience: bake client package + Skill в image либо
подключать детерминированным read-only artifact/mount (bundle из
`<root>/client-bundles/openscience-main`). Не полагаться на ручную
установку в writable layer — она исчезнет при recreate.
`verify-client.sh` проверяет, что install root — persistent путь.

### Exit codes (стабильные)

`egressctl`: 0 ok · 1 runtime/API · 2 usage · 3 config · 4 auth ·
5 identity mismatch. `egress-run`: 0 ok · 2 usage · 3 config · 4 auth ·
6 unsupported mode · (иначе — exit code child-процесса).
