# vibelab-egress

Управляемый egress-слой на базе Mihomo для исследовательских и coding-агентов.
Один backend (Docker Compose stack: `mihomo` + `egressd`) обслуживает несколько
execution environments (host, Docker container, другие контейнеры/VM);
клиентский пакет (`egressctl` + `egress-run`, stdlib-only) ставится локально
в каждую среду.

На host каждый локальный UID получает личный профиль автоматически при первом
вызове клиента, без sudo и без домашнего каталога. Пользователь может добавить
личную подписку или proxy через `egressctl source add`; в контейнере исходник
остаётся внутри контейнера, а backend получает только нормализованные узлы
(подробности: `INSTALL.md`, § «Личные профили и источники»).

```text
Docker host
├── client containers (isolated workloads)
└── vibelab-egress stack
    ├── mihomo            # pinned v1.19.32: providers, rules, GEO, listeners, TUN
    └── egressd           # authenticated /v1 control API + data-plane capabilities
        ▲
        │ (client token, per environment)
host / container / other container
    ├── egressctl         # control plane: routes, defaults, providers, probes
    └── egress-run        # local exec: `egress-run ROUTE -- command`
```

Ключевой invariant: `egress-run` запускает команду **локально** в среде
вызова; отдельный запуск выбирает собственные route и mode, backend никогда
не получает arbitrary command payload и не является remote-exec service
(TECH_SPEC §1.1, §10). Для Docker-клиента установка package/config выполняется
внутри его image или контейнера; подробные host/container инструкции — в
`INSTALL.md`.

## Документация

| Файл | Назначение |
|---|---|
| `TECH_SPEC.md` | Источник правды по требованиям (52 раздела; layout §44, acceptance §49/50) |
| `INSTALL.md` | Установка: A. backend (Docker host), B. client (каждая среда) |
| `STATUS.md` | Статус фаз A0–A8, журнал решений — обновляется после каждого шага |
| `POC_REPORT.md` | Отчёт Phase 0 PoC (22/22) |
| `host/LIVE_ACCEPTANCE.md` | Live-acceptance на целевом хосте (один JSON-отчёт) |

## Быстрый старт

**Backend** (один раз на Docker host, root) — `INSTALL.md`, шаг A:

```sh
scripts/build-tun-engine.sh             # once; requires Go 1.26.3
bash scripts/install-backend-host.sh      # 10 шагов, лог <root>/install-backend-host.log
bash scripts/verify-backend-host.sh       # pass/fail по каждому сервису
```

**Client** (в каждую execution environment) — `INSTALL.md`, шаг B:

```sh
sudo scripts/setup-egress-client.sh --client host-main --environment host
egressctl config validate && egressctl whoami && egressctl doctor
```

Одноразовый тест из обычного Docker-контейнера (после установки backend):

```sh
sudo scripts/setup-egress-client.sh --client egress-test-client \
  --environment container --docker-test \
  --route 'p-8970d7b87bee79eda2c25b78-blanc:vless-49' --copy-personal-source
```

`--copy-personal-source` нужен только для host-owned personal route (`p-…`).
Он создаёт отдельный snapshot тестового клиента, не открывая ему host-маршрут.

Дальше — обычный workflow: `egress-run <route> -- command`,
`egressctl route set/list`, `egressctl best`, skill для агентов —
`skills/egress/SKILL.md`.

## Разработка и тесты

Оффлайн (без Docker, без root, stdlib-only):

```sh
python3 -m pytest tests/ client/tests/ -q    # полный свит (проверен: 380 passed)
python3 -m pytest tests/security/ -q         # A8 security gate (54)
python3 -m pytest tests/install/ -q          # оффлайн-свит install-скриптов, stubbed docker (56 checks)
```

`tests/install/README.md` — как добавлять сценарии. Правила коммитов, стиля
и инвариантов — в `AGENTS.md` (корень репозитория) и `DEVELOPMENT.md`.

## Базовые security-инварианты

- Defaults и one-shot сессии — per-client; действия одного клиента не
  меняют состояние другого.
- Секреты в логах/отчётах — только путь + sha256[:8], никогда значения.
- Public API — narrow `/v1`; raw Mihomo API недоступен клиентам, client
  provisioning — только host-side bootstrap.
- Host-инструменты — Python stdlib, фиксированный argv, без
  `shell=True`/`eval`/`exec` (контролируется `tests/security/`).
- Docker socket egress stack и клиентам не выдаётся; CIDR-синхронизация —
  через `egress-hostctl sync-docker-cidrs` (сгенерированный артефакт).

## Текущий статус

Фазы A0–A8 закрыты в части, реализуемой в среде разработки (container,
без Docker); live-acceptance на целевом хосте — см. «Next» в `STATUS.md`
и `host/LIVE_ACCEPTANCE.md`. Свежая ветка install-logging: структурный
лог установки, robust health gate, self-explaining harness, оффлайн-свит
install-скриптов.
