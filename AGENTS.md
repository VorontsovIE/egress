# AGENTS.md — vibelab-egress

Инструкция для coding-агентов, работающих в этом репозитории.

## Что это

**vibelab-egress** — управляемый egress-слой на базе Mihomo для исследовательских
и coding-агентов. Один backend (Docker Compose stack: `mihomo` + `egressd`)
обслуживает несколько execution environments (host, OpenScience/gVisor, другие
контейнеры). Клиенты (`egressctl` + `egress-run`) ставятся локально в каждую
среду.

- **Источник правды по требованиям:** `TECH_SPEC.md`
  (51 раздел; layout — раздел 44, план фаз — раздел 46, acceptance — 49/50).
- **Статус работ:** `STATUS.md` — обновлять после каждого
  завершённого шага (это правило самого файла).
- **Результат PoC (фаза A0):** `POC_REPORT.md` — 22/22.
- **Установка:** `INSTALL.md`.
- **Окружение разработки:** `DEVELOPMENT.md` (корень репозитория).

## Опись репозитория

Корень git-репозитория — корень проекта: репозиторий автономен (GitHub
`VorontsovIE/egress`), «внешнего» окружения нет. В dev-контейнере корень
репозитория смонтирован в `/srv/workspace`, на globe —
`/home/vibelab/openscience/workspace`. Отслеживается 1299 файлов, ~127 МБ.

```text
.
├── .gitignore
├── AGENTS.md
├── DEVELOPMENT.md
├── README.md
├── TECH_SPEC.md                        # ТЗ, 51 раздел (ru)
├── STATUS.md                           # статус фаз A0–A8, журнал решений
├── POC_REPORT.md                       # отчёт Phase 0 PoC (22/22)
├── INSTALL.md                          # установка: A. backend (host), B. client (каждая среда)
├── compose.yaml                        # backend stack: mihomo (pinned v1.19.32) + egressd
├── compose.tun.yaml                    # override: strict/TUN helper (NET_ADMIN, UNIX-socket only)
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
│   └── examples/                       # client.host.yaml, client.openscience.yaml
│
├── adapters/                           # provider-адаптеры (spec §11.2): common.py + blanc/, xeovo/
│                                       # detection/normalization/redaction/atomic-write+LKG
│
├── host/                               # host-side инструменты (Python stdlib-only, fixed argv)
│   ├── egress-hostctl                  # узкий CLI: deploy/status/logs/sync-docker-cidrs/run-acceptance
│   ├── egress-host-test                # host acceptance harness (--static без Docker daemon)
│   ├── egress-devbridge                # dev/test bridge: фиксированные ops, bearer-token, 172.17.0.1:9099
│   ├── live-acceptance.py              # live acceptance (spec §50, §49) → один JSON-репорт
│   ├── LIVE_ACCEPTANCE.md
│   ├── strict-helper/                  # TUN helper (Dockerfile + egress-tun-helper.py, NET_ADMIN)
│   ├── units/                          # systemd: devbridge, tun-helper, cidr-sync (service+timer)
│   └── geo/                            # качается по требованию (не в git; scripts/fetch-geo-data.sh)
│
├── scripts/                            # bash-скрипты (host, root где указано)
│   ├── install-backend-host.sh         # полный bootstrap backend (secrets, CIDR sync, ufw, compose)
│   ├── uninstall-backend-host.sh       # rollback (--remove-data — удалить persistent root)
│   ├── verify-backend-host.sh          # compose, binds, socket, secrets, harness
│   ├── provision-client.sh             # client bundle: client.yaml + token + proxy.auth
│   ├── install-client.sh               # установка client package (host | gvisor-container)
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
    ├── mihomo, mihomo-gvisor           # собранные бинарники v1.19.32 (49 МБ / 59 МБ)
    ├── mihomo.sha256
    ├── mihomo-src/                     # vendored исходники v1.19.32 (~6 МБ, .git удалён)
    ├── mihomo-src.tar.gz
    ├── wgserver/                       # userspace WireGuard-сервер (Go, wireguard-go + sing-wireguard)
    ├── geo/                            # не в git (scripts/fetch-geo-data.sh)
    ├── meta-rules-dat-release/         # не в git (scripts/fetch-geo-data.sh --full)
    ├── fake_node.py                    # фейковые SOCKS5-ноды с JSONL-логом
    ├── run_poc.py, run_a2.py, run_a3.py# live-оркестраторы (реальный mihomo)
    ├── run/, run_a2/, run_a3/          # результаты прогонов (logs, configs, state, clients/)
    ├── diag_load/, diag_put/, diag_timing/, diag_state/  # диагностические прогоны
    └── results.json                    # итог PoC: 22/22
```

Состав по типам: 39 `.py`, 1047 `.go` (vendored mihomo-src), 16 `.sh`, 39 `.yaml`,
12 `.md`.

### Зачем в репо такие крупные бинарники и данные

- `poc/mihomo*`, `poc/wgserver/wgserver` — собраны из исходников, т.к.
  `objects.githubusercontent.com` / `raw.githubusercontent.com` заблокированы
  в этой среде (релизы GitHub недоступны). Коммитятся намеренно.
- GEO-данные (GeoIP/GeoSite/ASN) **не хранятся в git** (исключены из истории
  2026-10-15): скачиваются по требованию `scripts/fetch-geo-data.sh` (источник
  MetaCubeX/meta-rules-dat, те же URL, что `geox-url`; в OpenScience работает
  fallback на `codeload.github.com`).
- `poc/mihomo-src/` — исходники, чтобы бинарники воспроизводились.

## Соглашения (обязательные)

1. **Секреты.** В репозитории только фиктивные значения (PoC: `tok-host-main`,
   `a3-mihomo-secret`, TEST-NET-2 адреса, `fake-*-do-not-use` ключи). Реальные
   секреты живут на host: `/etc/vibelab-egress/secrets/` (0600, mounted files,
   никогда env-переменные). Никогда не коммитить реальные токены/ключи/подписки.
2. **Client package — stdlib-only.** `client/` должен устанавливаться в bare host
   и gVisor-контейнер без package index. Не добавлять зависимости.
3. **Host-инструменты — fixed argv.** `host/egress-hostctl`, `egress-host-test`,
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
   (309 passed на 2026-10-13). Live-проверки требуют host с Docker +
   CAP_NET_ADMIN и не проходят в этом контейнере.
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
