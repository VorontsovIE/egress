# vibelab-egress

Управляемый egress-слой на базе Mihomo для исследовательских и coding-агентов.
Один backend (Docker Compose stack: `mihomo` + `egressd`) обслуживает несколько
execution environments (host, OpenScience/gVisor, другие контейнеры/VM);
клиентский пакет (`egressctl` + `egress-run`, stdlib-only) ставится локально
в каждую среду.

```text
Docker host
├── OpenScience stack (workload)
└── vibelab-egress stack
    ├── mihomo            # pinned v1.19.32: providers, rules, GEO, listeners, TUN
    └── egressd           # authenticated /v1 control API + data-plane capabilities
        ▲
        │ (client token, per environment)
host / OpenScience / other container
    ├── egressctl         # control plane: routes, defaults, providers, probes
    └── egress-run        # local exec: `egress-run ROUTE -- command`
```

Ключевой invariant: `egress-run` запускает команду **локально** в среде
вызова; backend никогда не получает arbitrary command payload и не является
remote-exec service (TECH_SPEC §1.1, §10).

## Документация

| Файл | Назначение |
|---|---|
| `TECH_SPEC.md` | Источник правды по требованиям (51 раздел; layout §44, acceptance §49/50) |
| `INSTALL.md` | Установка: A. backend (Docker host), B. client (каждая среда) |
| `STATUS.md` | Статус фаз A0–A8, журнал решений — обновляется после каждого шага |
| `POC_REPORT.md` | Отчёт Phase 0 PoC (22/22) |
| `host/LIVE_ACCEPTANCE.md` | Live-acceptance на целевом хосте (один JSON-отчёт) |

## Быстрый старт

**Backend** (один раз на Docker host, root) — `INSTALL.md`, шаг A:

```sh
bash scripts/install-backend-host.sh      # 10 шагов, лог <root>/install-backend-host.log
bash scripts/verify-backend-host.sh       # pass/fail по каждому сервису
```

**Client** (в каждую execution environment) — `INSTALL.md`, шаг B:

```sh
bash scripts/provision-client.sh --client host-main --environment host  # backend-side: bundle
bash scripts/install-client.sh --client host-main --target host   # host: package + /etc
egressctl config validate && egressctl whoami && egressctl doctor
```

Дальше — обычный workflow: `egress-run <route> -- command`,
`egressctl route set/list`, `egressctl best`, skill для агентов —
`skills/egress/SKILL.md`.

## Разработка и тесты

Оффлайн (без Docker, без root, stdlib-only):

```sh
python3 -m pytest tests/ client/tests/ -q    # полный свит (в root-контейнере: 309 passed)
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

Фазы A0–A8 закрыты в части, реализуемой в среде разработки (OpenScience,
без Docker); live-acceptance на целевом хосте — см. «Next» в `STATUS.md`
и `host/LIVE_ACCEPTANCE.md`. Свежая ветка install-logging: структурный
лог установки, robust health gate, self-explaining harness, оффлайн-свит
install-скриптов.
