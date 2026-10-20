# DEVELOPMENT.md — vibelab-egress

Как работать с проектом из среды разработки. Окружение, команды, ограничения.

## Окружение: мы внутри докер-контейнера

Разработка и тестирование могут выполняться изнутри обычного Docker-контейнера.
Путь checkout и постоянного состояния зависит от настроек конкретного
контейнера; в примерах этого документа предполагается checkout в `/srv/workspace`.
Данные приложения и временное состояние контейнера не следует использовать
как место для артефактов проекта.
- Контейнер — недоверенная execution environment по threat model проекта
  (spec §7.1). Backend (mihomo + egressd) **не запускается здесь** — он живёт
  на хосте в отдельном compose stack.
- Доступ к backend из контейнера — через docker-bridge gateway
  `172.17.0.1` (eth0 контейнера `172.17.0.2/16`): control API `:9091`,
  SOCKS `:1080` (default) / `:1081` (one-shot).
- Dev/test bridge для host-операций: `host/egress-devbridge` на
  `172.17.0.1:9099` (bearer-token, фиксированные операции, spec §33.2).
  Поднимается администратором один раз: `scripts/bootstrap-dev-access.sh`.

### Чего НЕТ в этом контейнере (зафиксировано в STATUS.md, влияет на тесты)

- **Нет Docker/podman** (`/var/run/docker.sock` отсутствует) — compose stack
  нельзя поднять здесь; `compose.yaml` — артефакт для целевого хоста.
  Live-проверки, требующие Docker daemon, в контейнере считаются skipped.
- **Нет CAP_NET_ADMIN**: `/proc/net/wireguard` отсутствует, `unshare -n`
  запрещён — TUN/netns не проверяются в этой среде (capability matrix — A6).
- **Сетевые блокировки:** `objects.githubusercontent.com` и
  `raw.githubusercontent.com` недоступны; работает `codeload.github.com`.
  Поэтому POC-бинарники собираются из исходников
  (`scripts/fetch-poc-vendor.sh` + рецепт «Сборка (редко)»); на целевом
  хосте релизы GitHub доступны, но production-стек использует Docker-образ
  `metacubex/mihomo:v1.19.32` (`compose.yaml`) и POC-бинарники не требует.
  Скачивание GEO — `scripts/fetch-geo-data.sh` (только release-ассеты; при
  блокировке хоста — диагностика + процедура ручного seed в сообщении
  скрипта).
- **Публичные цели для e2e:** доступны `cloudflare.com/cdn-cgi/trace` (200),
  `gstatic.com/generate_204` (204); `api.ipify.org`, `example.com` — нет.
  Для локальных e2e-проверок использовать TEST-NET-3 (`203.0.113.0/24`) —
  он не попадает под правило `GEOIP,LAN,DIRECT` (127.0.0.1 попадает, и это
  уже ловило невалидные тесты — см. STATUS.md «Валидность тестов»).

### Инструменты, которые ЕСТЬ

| Инструмент | Версия / путь |
|---|---|
| Python | 3.11.17 (`python3`) |
| pytest | 9.1.1 (fastapi, httpx, pyyaml, aiosqlite, cryptography установлены) |
| git | 2.43.0 |
| Go | 1.27.2 (`/usr/local/go/bin/go`) — для сборки `poc/wgserver/` и mihomo |
| curl, wget, tar | есть |
| CPU / RAM | 24 CPU / 256 GiB |

## Команды

Все команды выполнять из корня репозитория (корня проекта; относительные
пути в harness'ах от него).

### Тесты (оффлайн, без mihomo, без сети)

```bash
python3 -m pytest tests/ client/tests/ -q        # 309 passed (2026-10-13)
python3 -m pytest tests/security/ -v             # A8 security gate отдельно
```

Пакеты не установлены как dist — тесты подставляют `egressd/src` и корень
репо в `sys.path` сами (как и `poc/run_*.py`).

### Live-оркестраторы PoC (нужен собранный mihomo, без Docker)

```bash
python3 poc/run_poc.py      # Phase 0: 22 проверки (idempotent, сам поднимает процессы)
python3 poc/run_a2.py       # A2: providers/GEO на реальном mihomo (8 live-проверок)
python3 poc/run_a3.py       # A3: /v1 API на реальном mihomo (7 live-проверок)
```

Используют бинарники `poc/mihomo` / `poc/mihomo-gvisor` (не в git — сборка
по рецепту ниже) и GEO из `poc/geo/`.
GEO в git не хранится (с коммита `1c9d3e3`): `sh scripts/fetch-geo-data.sh`
(только release-ассеты; при блокировке хоста — диагностика + ручное seed
сообщением скрипта).

### Host-side (выполняются на хосте, НЕ в контейнере)

```bash
# root на хосте:
scripts/install-backend-host.sh        # полный bootstrap backend
scripts/verify-backend-host.sh         # проверка задеплоенного backend
scripts/run-live-acceptance.sh         # live acceptance → reports/host_acceptance_report.json
scripts/uninstall-backend-host.sh      # rollback

# из контейнера — только через devbridge (172.17.0.1:9099):
#   deploy / redeploy / status / logs / run-acceptance / sync-docker-cidrs / install-client
```

### Клиент (в любой execution environment)

```bash
egressctl config validate && egressctl whoami && egressctl doctor
egressctl route current            # текущий default
egress-run <route> -- <command>    # one-shot, не меняет default
egress-run --mode tun <route> -- <command>   # отдельный user/net/mount namespace для каждого запуска
```

Правила использования — `skills/egress/SKILL.md` (обязательны для агентов).

### Сборка (редко)

POC-бинарники (`poc/mihomo`, `poc/mihomo-gvisor`, `poc/wgserver/wgserver`)
не в git (исключены в коммите `fa65d70`). Recipe:

```bash
# 0. vendor-исходники (mihomo v1.19.32 + wireguard-go @ a6cecdd7f57f +
#    1-строчный патч; идемпотентно, --check — верификация):
sh scripts/fetch-poc-vendor.sh

# 1. mihomo v1.19.32 (go.mod: go 1.20; в dev-контейнере /usr/local/go/bin/go 1.27.2):
cd poc/mihomo-src && CGO_ENABLED=0 go mod download
cd poc/mihomo-src && CGO_ENABLED=0 go build -o ../mihomo .
cd poc/mihomo-src && CGO_ENABLED=0 go build -tags with_gvisor -o ../mihomo-gvisor .

# 2. wgserver — userspace WireGuard-сервер (go.mod: go 1.23):
cd poc/wgserver && go mod download && go build -o wgserver .
```

Phase-0 сборка (2026-10-10): `poc/mihomo` — sha256 `a2b70600…`
(в `poc/mihomo.sha256`), `poc/mihomo-gvisor` — 61 МБ. Хэш пере-сборки
зависит от версии Go — не фиксируется.

## Layout и точки входа

```text
корень репозитория (/srv/workspace в dev-контейнере)
├── AGENTS.md            # опись + соглашения для агентов
├── DEVELOPMENT.md       # этот файл
├── README.md            # обзор + быстрый старт
├── TECH_SPEC.md         # требования (источник правды)
├── STATUS.md            # статус фаз + журнал решений
├── compose.yaml         # backend stack (host)
├── egressd/             # control plane (FastAPI /v1)
├── client/              # egressctl + egress-run (stdlib-only)
├── adapters/            # provider-адаптеры
├── host/                # host-инструменты (stdlib-only, fixed argv)
├── scripts/             # install/verify/provision (host, bash)
├── tests/               # offline pytest + A8 security gate
├── poc/                 # PoC: бинарники, исходники, GEO, harness'ы
└── reports/             # acceptance-артефакты
```

## Ключевые пути на host (persistent)

```text
/var/lib/vibelab-egress/  # persistent root (VIBELAB_EGRESS_ROOT)
  mihomo/        # bbolt state (store-selected), logs
  providers/     # подписки (ro-mount)
  generated/     # config.yaml + docker-cidrs.yaml
  egressd/       # metrics.sqlite, session state
  geo/           # GeoIP.dat, GeoSite.dat, ASN.mmdb
/etc/vibelab-egress/secrets/          # секреты backend (0600, mounted files)
/etc/vibelab-egress/                  # host client: client.yaml, token, proxy auth
```

## Ограничения и известные проблемы

- Live-часть (Docker + CAP_NET_ADMIN) не проверяется в контейнере — только на
  хосте; текущий acceptance-репорт: `reports/host_acceptance_report.json`
  (4 passed / 1 failed / 23 skipped, 2026-10-12) — проверить статус failed
  пункта в `STATUS.md` перед повторным прогоном.
- `poc/` содержит ~130 МБ бинарников/исходников — намеренно (см. AGENTS.md);
  GEO-данные в git не хранятся — `scripts/fetch-geo-data.sh`.
