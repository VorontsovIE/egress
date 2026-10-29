# AGENTS.md — vibelab-egress

Инструкция для coding-агентов, работающих в этом репозитории.

## Что это

**vibelab-egress** — управляемый egress-слой на базе Mihomo для исследовательских
и coding-агентов. Один backend (Docker Compose stack: `mihomo` + `egressd`)
обслуживает несколько execution environments (host, Docker container, другие
контейнеры). Клиенты (`egressctl` + `egress-run`) ставятся локально в каждую
среду.

- **Источник правды по требованиям:** `TECH_SPEC.md`
  (52 раздела; layout — раздел 44, план фаз — раздел 46, acceptance — 49/50).
- **Статус работ:** `STATUS.md` — обновлять после каждого
  завершённого шага (это правило самого файла).
- **Результат PoC (фаза A0):** `POC_REPORT.md` — 22/22.
- **Установка:** `INSTALL.md`.
- **Окружение разработки:** `DEVELOPMENT.md` (корень репозитория).

## Опись репозитория

Корень git-репозитория — корень проекта: репозиторий автономен (GitHub
`VorontsovIE/egress`), «внешнего» окружения нет. В dev-контейнере корень
репозитория смонтирован в `/srv/workspace`, на globe —
`/srv/workspace`. Отслеживается 180 файлов, ~1.4 МБ
(vendor-данные и бинарники — не в git, см. ниже).

```text
.
├── .gitignore
├── AGENTS.md
├── DEVELOPMENT.md
├── README.md
├── TECH_SPEC.md                        # ТЗ, 52 раздела (ru)
├── STATUS.md                           # статус фаз A0–A8, журнал решений
├── POC_REPORT.md                       # отчёт Phase 0 PoC (22/22)
├── INSTALL.md                          # установка: A. backend (host), B. client (каждая среда)
├── compose.yaml                        # backend stack: mihomo (pinned v1.19.32) + egressd
│
├── mihomo/
│   ├── config.template.yaml            # референс-шаблон сгенерированного конфига
│   ├── generated/                      # пусто в репо (заполняется egressd на host)
│   └── providers/                      # пусто в репо (подписки, ro-mount на host)
│
├── egressd/                            # control plane (FastAPI, узкий public /v1 API)
│   ├── Dockerfile                      # python:3.11-slim, non-root, копирует adapters/
│   ├── pyproject.toml                  # deps: fastapi, uvicorn, httpx, pyyaml, aiosqlite, cryptography
│   └── src/egressd/                    # ~3850 строк: main, configgen, configctl, catalog,
│                                       # probe, ranking, geo_update, state, metrics, bootstrap_config
│
├── client/                             # client package: egressctl + egress-run (stdlib-only, 0 deps)
│   ├── pyproject.toml
│   ├── src/vibelab_egress_client/      # api, config, egressctl, egress_run (~1100 строк)
│   ├── tests/                          # unit-тесты клиента
│   └── examples/                       # client.host.yaml, client.container.yaml
│
├── adapters/                           # provider-адаптеры (spec §11.2): common.py + blanc/, xeovo/
│                                       # detection/normalization/redaction/atomic-write+LKG
│
├── host/                               # host-side инструменты (Python stdlib-only, fixed argv)
│   ├── egress-hostctl                  # узкий CLI: deploy/status/logs/CIDRs/acceptance/client provisioning
│   ├── egress-host-test                # host acceptance harness (--static без Docker daemon)
│   ├── egress-devbridge                # dev/test bridge: фиксированные ops, bearer-token, 172.17.0.1:9099
│   ├── client_identity.py               # host/container default ID from Linux username
│   ├── live-acceptance.py              # live acceptance (spec §50, §49) → один JSON-репорт
│   ├── LIVE_ACCEPTANCE.md
│   ├── units/                          # systemd: devbridge, cidr-sync (service+timer)
│   └── geo/                            # качается по требованию (не в git; scripts/fetch-geo-data.sh)
│
├── scripts/                            # bash-скрипты (host, root где указано)
│   ├── install-backend-host.sh         # полный bootstrap backend (secrets, CIDR sync, ufw, compose)
│   ├── uninstall-backend-host.sh       # rollback (--remove-data — удалить persistent root)
│   ├── verify-backend-host.sh          # compose, binds, socket, secrets, harness
│   ├── provision-client.sh             # client bundle: client.yaml + token + proxy.auth
│   ├── install-client.sh               # установка client package (host | container)
│   ├── migrate-egress-identities.py    # rollback-safe UID/provider ID migration
│   ├── verify-client.sh                # config validate/whoami/doctor + права на секреты
│   ├── bootstrap-dev-access.sh         # один раз: devbridge token + unit + start
│   ├── fetch-geo-data.sh               # GEO-скачивание по требованию (не в git; spec 14/41)
│   ├── run-live-acceptance.sh          # live acceptance на host → reports/host_acceptance_report.json
│   └── verify-live-report.py           # независимая re-verification репорта (tamper-evident)
│
├── skills/egress/SKILL.md              # agent skill: правила работы с egressctl/egress-run
│
├── tests/                              # offline pytest (без mihomo, без сети)
│   ├── integration/                    # test_a2..test_a6 (providers/GEO, API, probes, TUN-логика)
│   ├── security/test_security_gate.py  # A8: API surface, impersonation, secret scan, compose audit
│   └── fixtures/                       # фиктивные подписки (TEST-NET-2, fake-ключи) + README
│
├── reports/                            # артефакты приёмки
│   ├── host_acceptance_report.json     # live acceptance: 4 passed / 1 failed / 23 skipped (2026-10-12)
│   └── install-backend-host-report.log
│
└── poc/                                # Phase 0 PoC (A0): бинарники, исходники, harness'ы
    ├── mihomo.sha256                   # hash бинарника Phase-0 (сам бинарник — не в git)
    ├── wgserver/                       # userspace WireGuard-сервер (Go, wireguard-go + sing-wireguard)
    │   └── third_party/
    │       └── wireguard-go-sticky-sockets.patch   # 1-строчный vendor-патч (в git)
    ├── geo/                            # не в git (scripts/fetch-geo-data.sh)
    ├── meta-rules-dat-release/         # не в git (scripts/fetch-geo-data.sh --full)
    ├── fake_node.py                    # фейковые SOCKS5-ноды с JSONL-логом
    ├── run_poc.py, run_a2.py, run_a3.py# live-оркестраторы (реальный mihomo)
    ├── run/, run_a2/, run_a3/          # результаты прогонов (logs, configs, state, clients/)
    ├── diag_load/, diag_put/, diag_timing/, diag_state/  # диагностические прогоны
    └── results.json                    # итог PoC: 22/22
```

Состав по типам: 39 `.py`, 1 `.go` (poc/wgserver), 17 `.sh`, 39 `.yaml`,
12 `.md`, 1 `.patch`.

### Vendor-данные и бинарники — не в git

- `poc/mihomo`, `poc/mihomo-gvisor` (v1.19.32, кастомные сборки: CGO off /
  `-tags with_gvisor`) и `poc/wgserver/wgserver` **исключены из истории**
  (коммит `fa65d70`). Сборка из исходников — рецепт в `DEVELOPMENT.md`,
  «Сборка (редко)». `poc/mihomo.sha256` хранит hash Phase-0 сборки
  (`a2b70600…`, референс).
- Vendor-исходники **не хранятся в git**: `sh scripts/fetch-poc-vendor.sh`
  (codeload, pinned sha256: mihomo v1.19.32, MetaCubeX/wireguard-go
  @ a6cecdd7f57f). Единственное изменение вендора — 1-строчный патч
  `poc/wgserver/third_party/wireguard-go-sticky-sockets.patch`
  (`StdNetSupportsStickySockets=false` для контейнера без NET_ADMIN) — в git,
  накладывается скриптом при fetch.
- GEO-данные (GeoIP/GeoSite/ASN) **не хранятся в git** (исключены в коммите
  `1c9d3e3`): скачиваются по требованию `scripts/fetch-geo-data.sh` (источник —
  release-ассеты MetaCubeX/meta-rules-dat, те же URL, что `geox-url`).
  codeload эти файлы подавать не может (upstream не коммитит `.dat` в git):
  при блокировке release-хоста скрипт печатает диагностику и процедуру
  ручного seed в `host/geo/` (или seed можно пропустить — mihomo скачает GEO
  сам в рантайме по geox-url).

## Соглашения (обязательные)

1. **Секреты.** В репозитории только фиктивные значения (PoC: `tok-host-main`,
   `a3-mihomo-secret`, TEST-NET-2 адреса, `fake-*-do-not-use` ключи). Реальные
   секреты живут на host: `/etc/vibelab-egress/secrets/` (0600, mounted files,
   никогда env-переменные). Никогда не коммитить реальные токены/ключи/подписки.
2. **Python client package — stdlib-only.** `client/src/` устанавливается на
   host и произвольный Docker-контейнер без package index. Отдельный pinned
   static TUN engine собирается по `client/tun-engine/go.mod` и включается в
   bundle; не добавлять Python dependencies.
3. **Host-инструменты — fixed argv.** `host/egress-hostctl`, включая
   `copy-personal-source` для копирования нормализованного snapshot в явно
   указанный container test profile, `egress-host-test`,
   `egress-devbridge`, `live-acceptance.py`: Python stdlib-only, фиксированный
   allowlist команд, без `shell=True`/`os.system`, без free-form command input.
   Это security-инварианты (spec §33–38); A8 security gate их проверяет.
4. **Сгенерированный конфиг не редактировать руками.** `generated/config.yaml`
   создаётся `egressd/configgen.py` из secrets + providers; менять входы и
   регенерировать.
5. **Security invariants compose** (spec §36/37): no public binds (только
   127.0.0.1 / docker gateway), no Docker socket, mihomo admin API (9090)
   не публикуется, image pinned по tag + digest. Проверять через
   `scripts/verify-backend-host.sh` и `tests/security/`.
6. **Тесты.** Оффлайн-свит: `python3 -m pytest tests/ client/tests/ -q`
   (текущий результат зафиксирован в `STATUS.md`). TUN smoke проверен в
   обычном Docker-контейнере с `/dev/net/tun` и seccomp profile; нужен доступ
   к backend для проверки реальных provider routes.
7. **STATUS.md** обновляется после каждого завершённого шага; решения и
   найденные баги — в журнал в конце файла.
8. **Корень репозитория = корень проекта.** Репозиторий автономен (GitHub
   `VorontsovIE/egress`); все пути в доках и скриптах — относительно корня
   репозитория. «Внешнего» слоя и корневых дублей проектных файлов не
   создавать (2026-10-14: исторические корневые дубли удалены).
9. **Не удалять** `poc/` (бинарники, исходники, результаты прогонов) и
   `reports/` — это доказательства приёмки (POC_REPORT.md, acceptance-репорт).
   GEO-данные в git не хранятся — `scripts/fetch-geo-data.sh`.
10. **Язык документации** — русский (как в существующих документах проекта);
    код и комментарии — английский.

## Быстрая навигация

| Задача | Куда смотреть |
|---|---|
| Требования/архитектура | `TECH_SPEC.md` |
| Что сделано, что дальше | `STATUS.md` |
| Public API egressd | spec §27, `egressd/src/egressd/main.py` |
| Клиентский CLI | spec §30, `client/src/vibelab_egress_client/` |
| Provider-адаптеры | spec §11/12, `adapters/common.py` |
| Установка на host | `INSTALL.md`, `scripts/install-backend-host.sh` |
| Acceptance | spec §49/50, `host/live-acceptance.py`, `scripts/run-live-acceptance.sh` |
| Безопасность | spec §7, §33–38, `tests/security/test_security_gate.py` |
| Окружение разработки | `DEVELOPMENT.md` |
