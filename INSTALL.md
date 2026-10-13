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
- доступ в интернет для загрузки GEO-данных и образа `metacubex/mihomo`.

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
Скрипт применяет базовые ufw-правила (если ufw есть); при nftables —
сделайте эквивалент вручную: разрешите 9091/1080/1081 только с lo и
docker-bridge, заблокируйте 9090 наружу.

### Compose deployment

```bash
scripts/install-backend-host.sh            # полный bootstrap (root)
```

Скрипт: создаёт persistent dirs → генерирует secrets → скачивает GEO →
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

---

## B. Client installation in each execution environment

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
   OpenScience: `/run/secrets/vibelab_egress_*` read-only mount).

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
