# vibelab-egress — статус работы

Обновляется после каждого завершённого шага. Источник правды по требованиям — `TECH_SPEC.md`.

## Фаза A0 — PoC (ЗАКРЫТА, 22/22)

**Цель:** доказать все 30 пунктов Phase 0 (раздел 45 спеки) на Mihomo v1.19.32,
зафиксировать результаты в `POC_REPORT.md`.

**Результат: 22/22 проверок пройдено** (прогон 2026-10-10, `poc/results.json`).
Архитектурные пункты Phase 0 (3,5,6,7,8,9,10,11,12,13,14,15,16,17,18,20)
доказаны на реальном бинарнике; остальное зафиксировано как deferred A2–A8
(таблица в `POC_REPORT.md`, раздел 5).

### Готово
- [x] Структура репо (раздел 44 спеки)
- [x] Сборка Mihomo v1.19.32 из исходников (релизы GitHub недоступны):
      `poc/mihomo` (CGO off), `poc/mihomo-gvisor` (tags with_gvisor, userspace WG)
- [x] Userspace WireGuard-сервер `poc/wgserver/` (wireguard-go + sing-wireguard,
      forward TCP на 127.0.0.1)
- [x] GEO-данные через codeload: `poc/geo/{GeoIP.dat,GeoSite.dat,asn.mmdb}`
- [x] Production-генератор конфига `egressd/src/egressd/configgen.py`
      (валидирован реальным бинарником: geodata-mode bool, rules — строки,
      DIRECT built-in, provider-ноды через `use:`)
- [x] Фейковые SOCKS5-ноды с JSONL-логом (`poc/fake_node.py`)
- [x] PoC-оркестратор `poc/run_poc.py` (22 проверки)
- [x] Подтверждено: `profile.store-selected: true` переживает hot-reload
      (patchSelectGroup в executor.go восстанавливает selections из bbolt-cache)
- [x] VLESS e2e (P20) и WireGuard e2e (P21) — полный data plane через tunnel
- [x] `POC_REPORT.md` (версии, configs, measured outcomes, решения,
      маппинг на 30 пунктов Phase 0)

### Ключевые ограничения среды (зафиксированы, влияют на A0/A6)
- Нет Docker/podman — mihomo запускается бинарником напрямую; compose.yaml —
  артефакт для целевого хоста
- `objects.githubusercontent.com` и `raw.githubusercontent.com` заблокированы;
  работает `codeload.github.com`
- `/proc/net/wireguard` отсутствует, `unshare -n` запрещён — TUN/netns не
  проверяются в этой среде (capability matrix в A6)
- Публичные цели для e2e: `cloudflare.com/cdn-cgi/trace` (200),
  `gstatic.com/generate_204` (204); api.ipify.org/example.com — недоступны

### Валидность тестов (критическая находка, закрыта)
Первый прогон дал 19/22, но **P07–P11, P20, P21 были невалидны**: цель
127.0.0.1 попадала под правило `GEOIP,LAN,DIRECT` → трафик шёл DIRECT, а не
через ноду (лог ноды пуст при 200). Доказано диагностикой. Фикс: non-LAN цель
`203.0.113.1` (TEST-NET-3) + ассерты по canned-телу `NODE:<name>` и nodes.jsonl.
Итог: 22/22 валидных.

### Корневая причина P21 (закрыта)
В контейнере без NET_ADMIN wireguard-go `upLocked()` → netlink route socket
падает → device остаётся Down → tunneled-пакеты не доходят до gVisor-стека.
Фикс: vendored `wireguard-go` с `StdNetSupportsStickySockets=false`
(`poc/wgserver/third_party/wireguard-go/conn/sticky_linux.go:112`) + `replace`
в `go.mod`. После фикса: `Interface state ... now Up` немедленно, FWD-лог
показывает полный путь tunnel → gVisor → echo → tunnel.

## Текущий шаг / Next
 Все фазы A0–A8 закрыты в части, реализуемой в этой среде. Остаётся только
 live-часть, требующая целевого хоста с Docker + CAP_NET_ADMIN. Для неё
 подготовлен **готовый к запуску harness + перепроверка** (2026-10-11):
 - `host/live-acceptance.py` — producer: на хосте прогоняет ВСЕ live-чексы
   (A6 §50: 12 чексов TUN; A8 §49: host-only live) и пишет **один** JSON-отчёт
   `reports/host_acceptance_report.json`. Stdlib-only, фиксированный allowlist
   команд, секреты из отчёта вычищаются.
 - `scripts/verify-live-report.py` — verifier: **недоверчиво** пересчитывает
   каждый pass/fail из сырых evidence + проверяет tamper-hash `checks_hash`.
   Отчёт самодостаточен и tamper-evident: подделанный/багованный отчёт не
   пройдёт. Тесты: `tests/test_verify_live_report.py` (8/8).
 - `scripts/run-live-acceptance.sh` — одна команда на хосте (авто-детект
   контейнера OpenScience); `host/LIVE_ACCEPTANCE.md` — инструкция.
   - [x] **Установить бэкенд на хосте**: `bash scripts/install-backend-host.sh`
         (GEO опционален; 2026-10-12: стек поднят на globe — mihomo healthy,
         egressd up; harness-фиксы из этого же дня, см. журнал решений).
   - [ ] **Поднять TUN-стек**: `docker compose -f compose.yaml -f compose.tun.yaml up -d`
         (иначе 10 TUN-чексов останутся skip).
   - [ ] **Клиент в OpenScience-контейнере** (бандл из `INSTALL.md`, шаг B) —
         иначе `openscience_tun_unsupported` / `a8_client_same_release` skip.
   - [ ] **Запустить на хосте**: `bash scripts/run-live-acceptance.sh`
        (добавить `--recreate` для деструктивного recreate-чека) → отдать ОДИН
        файл `reports/host_acceptance_report.json`.
 - [ ] **Перепроверить**: `python3 scripts/verify-live-report.py
       reports/host_acceptance_report.json` → VERIFIED / REJECTED.
 - A6 live (acceptance §50): host-native `egress-run --mode tun` e2e,
       concurrent host TUN jobs, `direct+tun` e2e, UDP/QUIC, stale netns
       cleanup, OpenScience `unsupported_in_environment`, no silent fallback.
 - A8 live (acceptance §49): публичные NIC закрыты, recreate-персистентность
       OpenScience client, LiteLLM, parallel one-shot, local exec,
       internal/HARD DIRECT, secrets-скан, host acceptance report.
- A6 закрыта (realizable-in-sandbox): helper + security invariant'ы +
  capability reporting + compose.tun.yaml + клиентский путь; 43+5+7 тестов.
- A4 закрыта: 101/101 (73 unit + 28 integration, live-egressd).
- A5 закрыта: 38/38 (ranking + probe + benchmark + best).
- A7 закрыта: `skills/egress/SKILL.md` (decision tree + 10 примеров).
- A8 закрыта (static/pytest): 54/54 (+7 helper privilege review); найдены и
  закрыты 2 реальных дефекта (автосгенерация /docs|/openapi.json; 500 на
  oversized domain).
- A3 закрыта: 18/18 offline (pytest) + 7/7 live (реальный мihomo), стабильно x2.
- A2 закрыта: 42 offline (pytest) + 8/8 live (реальный мihomo), стабильно x2.
- A1 закрыта: 47/47 suite, артефакты на месте.
- A0 закрыта: 22/22, `POC_REPORT.md` написан.
- Полный прогон: **309 passed** (`tests/` + `client/tests/`, 2026-10-11,
  после GEO-правки; до этого 301 + 8 test_verify_live_report).

## Фаза A1 — Deployment/Bootstrap (ЗАКРЫТА, 47/47, 2026-10-10)

**Результат: 47/47 проверок пройдено** (suite `/tmp/a1_test.sh`, 2 прогона,
стабильно). Live-ветки harness/verify без Docker daemon — skipped (6), не
defect: Docker в этой среде отсутствует.

### Готово
- [x] `compose.yaml` — pinned `metacubex/mihomo:v1.19.32`
      (digest `sha256:bac1a74de365ea59270ba134016762c6be05654d55246518de256113bd1c225e`)
      + `egressd` (build из репо); binds только `127.0.0.1` и
      `${DOCKER_GATEWAY:-172.17.0.1}`; 9090 не публикуется; docker.sock не
      монтируется; секреты — ro-mounts 0600; healthchecks (mihomo: wget
      /version; egressd: python /v1/health); `no-new-privileges`.
- [x] `egressd/Dockerfile`, `egressd/pyproject.toml`,
      `egressd/src/egressd/main.py` — A1-скелет: JSON-логирование,
      ClientRegistry (token→profile, mtime-reload), MihomoAdmin, endpoints
      `/v1/health|ready|self|status|capabilities` + Bearer-auth; identity по
      токену, не по client_id.
- [x] `egressd/src/egressd/bootstrap_config.py` — bootstrap-генератор
      (secrets + providers + docker-cidrs → `generated/config.yaml`, 0600);
      stdlib YAML-эммиттер; в A3 egressd забирает генерацию на себя.
- [x] `configgen.py` — добавлено `listener_listen` (default `127.0.0.1`,
      контейнер `0.0.0.0` — публикация на host-адрес в compose и есть
      enforcement «no public binds»).
- [x] Клиентский пакет `client/` (stdlib-only, без PyYAML/requests — ставится
      в bare host и gVisor без package index):
      - `config.py` — discovery: --config → `VIBELAB_EGRESS_CONFIG` →
        `/etc/vibelab-egress/client.yaml`; flat-YAML parser; секреты из файлов.
      - `api.py` — все /v1-эндпоинты.
      - `egressctl.py` — полный CLI 30.1; exit codes 0/1/2/3/4/5.
      - `egress_run.py` — 30.2: local exec, argv без shell, exit status,
        SIGINT/SIGTERM forward, one-shot cleanup, `--mode tun` → exit 6.
      - `examples/client.{host,openscience}.yaml`.
- [x] `scripts/` (все +x): `provision-client.sh` (host/gvisor-container
      bundles + backend secrets, shlex-quoted), `install-client.sh`,
      `verify-client.sh` (offline/live), `install-backend-host.sh` (10 шагов:
      prereq → persistent dirs+findmnt/df → secrets → GEO → CIDR sync+timer →
      firewall → provision host client → bootstrap config → compose deploy →
      harness), `uninstall-backend-host.sh` (data сохраняется без
      `--remove-data`), `verify-backend-host.sh`, `bootstrap-dev-access.sh`.
- [x] `host/egress-hostctl` (от worker, верифицирован: compile, offline CIDR
      sync, bogus subcommand → exit 2), `host/egress-host-test` (17 checks,
      static/live, JSON report, AST-проверка no-arbitrary-command),
      `host/egress-devbridge` (fixed ops, bearer auth, отказ public bind),
      `host/units/{vibelab-egress-cidr-sync.service,.timer,
      vibelab-egress-devbridge.service}`.
- [x] `mihomo/config.template.yaml` — reference-скелет с аннотациями.
- [x] `INSTALL.md` — два независимых шага (A. Backend deployment,
      B. Client installation) + reproducible OpenScience install + exit codes.
- [x] Тесты: `/tmp/a1_test.sh` → **47/47, 0 failed** (2 прогона); тестовый
      фикс `/etc/vibelab-egress` удалён.

### Тестовое покрытие (разделы suite)
1. compile/syntax: py_compile всех 10 .py + bash -n всех 7 .sh.
2. provisioning: host-main + openscience-main; token perms 600; токены
   раздельны; bundles без mihomo-секрета; openscience bundle =
   `host.docker.internal`/`tun_backend: none`, host = `127.0.0.1`/`host-helper`.
3. bootstrap config: PyYAML-parse (12 rules; listeners 18080/18081 on
   0.0.0.0; users host-main+openscience-main; os_0..os_3; groups =
   clients+slots; `GEOIP,LAN,DIRECT,no-resolve` первый, `MATCH,DIRECT`
   последний; IN-USER после hard-direct; `store-selected: true`;
   secret подставлен).
4. harness static negative: compose с `0.0.0.0:1080` + docker.sock →
   `no_public_binds=fail`, `docker_socket_not_mounted=fail`; provider-секрет
   в bundle → `no_backend_secrets_in_client_bundles=fail`; секреты в
   JSON-репорте отсутствуют.
5. harness static positive: 11 pass / 6 skipped / 0 fail (skipped =
   live-daemon checks).
6. devbridge: healthz 200; 401 wrong token; 400 unknown op; 400 unknown
   field `cmd`; sync-docker-cidrs → чистый JSON; отказ bind на 8.8.8.8 без
   `--allow-public` (exit 2).
7. egressd smoke: 401 без токена; `/v1/self` → `client_id=host-main`
   (environment=host); чужой токен → `openscience-main` (identity bound to
   token); mihomo down → `reachable: false`; ни один response не содержит
   значение секрета.
8. client package: `config validate`/`show` (токен маскируется); `whoami`
   live против запущенного egressd (exit 0); `egress-run direct` сохраняет
   exit 7; `--mode tun` → exit 6; `--default` инжектит
   `socks5h://host-main:...`; concurrent invocations независимы; SIGTERM →
   wrapper exit 143, child terminated.

### Баги, найденные тестами и закрытые
- `provision-client.sh`: `eval` python-вывода ломался на неэкранированном
  JSON → фикс `shlex.quote` в python-эммиттере.
- Скрипты были не исполняемыми (rc=126) → `chmod +x scripts/*.sh host/egress-*`.
- `egress-host-test` `no_arbitrary_host_command`: substring-поиск
  `"shell=True"` ловил docstring → заменён на AST-анализ (только реальные
  вызовы `eval(`/`exec(`/`os.system(`/`subprocess.* shell=True`).
- `egress-host-test` `is_local_bind`: `ipaddress` считает `0.0.0.0` private →
  явная проверка `is_unspecified`/`0.0.0.0`/`::` → False.
- `egress_run.py`: `--mode tun direct` — direct-кратчайший путь обходил
  tun-проверку → tun-проверка перенесена ПЕРЕД direct.
- `egress_run.py`: смерть child сигналом давала `-15` → нормализовано в
  `128+signum` (143).
- `_die` аннотирован `NoReturn` (гасит LSP «possibly unbound»).

### Ограничения среды (не дефекты)
- Docker/podman в этой среде нет — `compose.yaml` и host-скрипты — артефакты
  для целевого хоста; live-ветки harness/verify → skipped.
- Worker `ses_edab839cfffeed3ns4r6K7rGFf` (host-tools, background) упал на
  provider error после создания только `host/egress-hostctl`; остальные
  host-файлы (egress-host-test, egress-devbridge, units) дописаны lead'ом.

## Фаза A2 — Mihomo/Providers/GEO (ЗАКРЫТА, 2026-10-10)

**Результат: 42/42 offline (pytest) + 8/8 live (реальный Mihomo v1.19.32),
стабильно (2 прогона live).**

### Готово
- [x] `adapters/` — provider-адаптеры (spec 11.2):
  - `common.py` — detection формата (Clash YAML / base64 Clash / WireGuard
    `.conf` / `vless://`), нормализация, **отказ на дубликате node-name**
    (Mihomo молча грузит дупы — routing hazard), atomic write +
    last-known-good (`.lkg`), redaction секретов (`private-key`/`uuid`/
    `password` → `***REDACTED***`, public-key не трогаем), `stable_slug`
    (display name → stable id, spec 13).
  - `blanc/`, `xeovo/` — адаптеры; **чистые** node-names (без префикса),
    тег провайдера — через `override.additional-prefix` в конфиге (spec 11.1),
    единый путь для native (http) и adapter (file) провайдеров.
  - `__main__.py` — CLI `python -m adapters --provider blanc --source in --out out`.
- [x] `tests/fixtures/` — blanc/xeovo fixtures (clash yaml, base64, wg-conf,
  vless) с fake-секретами (`198.51.100.0/24`, `fake-*-do-not-use`), README.
- [x] `egressd/src/egressd/geo_update.py` — GEO-обновление (spec 14/41):
  download + валидация (non-empty, size) + atomic write (binary-safe) +
  metadata (`geo-metadata.json`: source, sha256, size, updated_at) + status
  (staleness по interval). Injectable fetcher (offline-тесты). Неудачный
  update не трогает предыдущий файл.
- [x] `egressd/src/egressd/main.py` — `/v1/geo/status` + `/v1/geo/update`
  (auth, datasets subset, force, 400 на unknown dataset, 401 без токена).
- [x] `configgen.py` — `geo-auto-update: true` + `geo-update-interval: 24`
  (spec 14.1, нативный путь Mihomo).
- [x] `egressd/Dockerfile` — `COPY adapters` + `PYTHONPATH=/app` (adapters
  импортируются egressd).
- [x] `tests/integration/test_a2.py` — 42 теста (adapters: detection/
  normalize/redaction/atomic/lkg/dup; geo: update/idempotency/force/
  failed-fetch/tiny/status/stale/missing/lkg; configgen: geo-auto-update +
  node-collision validation; egressd: /v1/geo/* endpoints).
- [x] `poc/run_a2.py` — live-харнес (реальный мihomo + fake nodes):
  1. config validation (mihomo up, 6 groups, 13 rules, 3 provider nodes)
  2. node collisions (adapter отклоняет дуп; live-конфиг без дупов)
  3. client selector isolation (host→blanc, os→xeovo, независимые defaults)
  4. direct route (DIRECT → no node response)
  5. concurrent one-shot (3 параллельных слота → 3 разные ноды)
  6. mapping scale (300 нод → 13 правил, как и 3 ноды — IN-USER фиксирован)
  7. GEO update (geo-auto-update в конфиге, update переписывает файл+meta+lkg,
     mihomo жив)
  8. Docker bypass precedence (Docker-CIDR правило до IN-USER; Docker-цель
     остаётся DIRECT даже через proxy)

### Ключевые решения / находки (A2)
- **Двойной префикс (баг, найден live-тестом):** адаптер печатал `blanc-` в
  имя, а `ProviderDef.prefix` добавлял `additional-prefix: blanc-` →
  `blanc-blanc-...`, PUT промахивался, группы стояли на DIRECT. Фикс:
  адаптер — чистые имена, тег только через `additional-prefix` (spec 11.1).
- **Mihomo НЕ отказывается стартовать на дубликате node-name** (проверено
  live: грузит молча). Защита — в адаптере (ValueError на дупе) +
  collision-free live-конфиг.
- **API-формы Mihomo v1.19.32:** `/proxies` → `{"proxies": {...}}`;
  `/rules` → `{"rules": [{index,type,payload,proxy}]}`; provider-ноды — в
  `all` поля select-группы, НЕ top-level ключи `/proxies`; file-providers
  грузятся асинхронно после старта (нужен wait).
- **Mihomo требует provider path = subpath home dir** (`-d`); в харнесе
  файлы пишутся в `<home>/providers/` (как в проде — mount в config dir).
- **GEO update пишет в отдельный dir** (не в home mihomo), чтобы не
  повредить датасеты работающего mihomo; fetcher отдаёт реальные байты
  GeoIP + маркер (иначе hash совпадёт → "unchanged").
- **`atomic_write_bytes`** (binary-safe) — GEO-датасеты бинарные;
  text-версия ломала 0x00/0xFF.

## Фаза A3 — egressd (ЗАКРЫТА, 2026-10-11)

**Результат: 18/18 offline (pytest) + 7/7 live (реальный Mihomo v1.19.32),
стабильно (2 прогона live). Совместный прогон A2+A3 offline: 60/60.**

### Готово
- [x] `egressd/src/egressd/catalog.py` — RouteCatalog: stable route ID
      `<provider>:<clean>` ↔ mihomo-name `<prefix><clean>` (явные карты в обе
      стороны), synthetic `direct`, geo-эвристика из stable-name, snapshot.
- [x] `egressd/src/egressd/metrics.py` — MetricsDB (sqlite3, WAL, schema v1,
      10 таблиц + meta, `_scrub` маскирует secret-значения перед persist).
- [x] `egressd/src/egressd/state.py` — ClientState: per-client default
      route/mode, atomic JSON persist, `reconcile()` fallback-to-direct только
      для затронутых клиентов.
- [x] `egressd/src/egressd/probe.py` — SlotAllocator (thread-safe, blocking
      allocate), ProbeEngine (slot + point_group + socks_probe), `socks_probe`
      (DNS/TCP/TLS/HTTP через authenticated SOCKS5), `classify_probe` (чистая,
      data-plane-driven, все классы spec 24).
- [x] `egressd/src/egressd/configctl.py` — ConfigController (regenerate atomic
      + hot-reload `PUT /configs` + poll `/version`), `build_egress_config` из
      env; egressd забирает генерацию конфига на себя (bootstrap-путь
      заменён).
- [x] `egressd/src/egressd/main.py` — полный публичный `/v1` API (spec 27):
      self/status/capabilities/routes/routes/{id}/default/default-mode/
      providers/providers/{id}/update/providers/update-all/test/probe-domain/
      benchmark/benchmarks/proxy/default-capability/oneshot/session(+active)/
      reconcile/doctor/geo/* + health/ready; Bearer-auth (identity по токену),
      audit-события в JSON-лог, metrics DB.
- [x] `tests/integration/test_a3.py` — 18 offline-тестов (включая 9
      обязательных A3: impersonation denied, cross-client isolation,
      concurrent one-shot, route-deletion-during-job, provider-update race,
      secret redaction, per-client fallback, no remote-exec/passthrough,
      probe error classification).
- [x] `poc/run_a3.py` — live-харнес (реальный мihomo + fake nodes), 7 чексов:
      config_catalog, per_client_defaults, concurrent_oneshot,
      provider_hot_reload, per_client_fallback, domain_probe, doctor.

### Ключевые решения / находки (A3)
- **Route ID = `<provider>:<clean>`**; mihomo-name = `<prefix><clean>`;
  `direct` — synthetic (mihomo-name `DIRECT`). Catalog строится из
  provider-файлов (детерминированно, без async-load mihomo).
- **`catalog.mihomo_name()` — явная карта route-ID → mihomo-name.** Критический
  баг (нашёл live-харнес): реализация делала
  `self._mihomo_to_id.get(route_id, route_id)`, а `_mihomo_to_id` ключится по
  mihomo-name — lookup по route-ID не попадал и возвращал route-ID С ДВОЙНИКОМ
  (`blanc:FI-Helsinki-1`) → mihomo `400 Selector update error: proxy not
  exist`, селекторы стояли на DIRECT. Offline-тесты не ловили (mihomo down →
  `st==0` → `_select` возвращает fast, проверяется только state). Фикс:
  отдельная карта `_id_to_mihomo`.
- **`_select()` — verify-and-retry селектора.** PUT `PUT /proxies/<group>`,
  затем GET `now` == ожидаемое; ретраи через окно async-load file-provider
  (mihomo грузит file-providers асинхронно после initial config и после
  hot-reload; до загрузки нода PUT → 400). `st==0` (mihomo down/offline) →
  fast return (selection персистится в state, reapplies на reconcile);
  timeout → warning + False (oneshot/benchmark пробрасывают 503/
  `selector_not_applied`).
- **`rebuild_catalog` обновляет ВСЕ ссылки на catalog** (`app.state.catalog`,
  `app.state.state.catalog` И `app.state.engine.catalog`). Критический баг
  (нашёл live-харнес, чек 6): ProbeEngine создавался с начальным ПУСТЫМ
  catalog (line 427), rebuild подменял `app.state.catalog` новым объектом, но
  не engine → probe видел КАЖДЫЙ route как unknown → `proxy_failure` +
  `latency_ms=None` (подпись early-return unknown-route). Offline-тест не
  ловил (использует `socks_probe`/`classify_probe` напрямую, не engine).
- **JsonLogHandler печатает `extra=`-поля** (audit-события, PUT-failure
  detail: path/status/resp). Было: emit собирал только ts/level/logger/msg →
  audit-строка была просто "audit" без payload (скрыло реальный status 400
  при отладке).
- **Probe-классификация data-plane-driven**: client-side DNS-сбой — только
  контекст (в live домен резолвится на exit через fake-ip); он лишь
  довершает host-unreachable (SOCKS code 4) до `dns_failure`.
- **One-shot session**: SlotAllocator (thread-safe), `PUT /proxies/os_N` через
  `_select`, capability `{host, port, username, password, scheme: socks5h}`;
  клиент закрывает через `DELETE /v1/oneshot/session/{id}`; слоты освобождаются
  при close и при route-removal.
- **Provider update**: re-scan manifest (source через env
  `EGRESSD_PROVIDER_SOURCE_<id>`), controlled fetch + adapter + atomic write,
  `rebuild_catalog`, hot-reload через `configctl`, reconcile fallback,
  terminate affected sessions, audit. В offline-режиме `configctl=None`.
- **Env egressd**: `EGRESSD_CLIENTS_DIR`, `EGRESSD_PROVIDERS_DIR`,
  `EGRESSD_STATE_DIR`, `EGRESSD_METRICS_DB`, `EGRESSD_GEO_DIR`,
  `EGRESSD_ONESHOT_SLOTS`/`_FILE`, `EGRESSD_CONFIG_OUT`, `EGRESSD_PROXY_HOST`,
  `EGRESSD_DEFAULT_PROXY_PORT` (18080), `EGRESSD_ONESHOT_PROXY_PORT` (18081),
  `EGRESSD_LISTENER_LISTEN`, `EGRESSD_DNS_LISTEN`, `EGRESSD_HEALTH_URL`,
  `EGRESSD_TEST_MAX_ROUTES` (50), `EGRESSD_BENCH_URL`,
  `EGRESSD_SELECT_TIMEOUT` (15), `MIHOMO_API_URL`, `MIHOMO_SECRET_FILE`.

### Harness-баги, закрыты (A3 live)
- **`EGRESSD_PROVIDER_SOURCE_blanc` в env харнеса не виден egressd** (отдельный
  uvicorn-процесс) → update шёл "native" path, новая нода не добавлялась.
  Фикс: source-файл + env пишутся ДО старта egressd (`write_blanc_source`).
- **Порт-коллизия fake-node новой ноды**: `start_nodes([FI-Helsinki-3])`
  ставил её на `NODE_BASE+0` (занято FI-Helsinki-1), а source декларировал
  `NODE_BASE+4` → data plane не работал. Фикс: `start_nodes(names, offset,
  logname)`, новая нода на offset 4.
- **`wait_providers_loaded` не assert'ился** (мог таймаутнуть молча) — теперь
  результат используется в ассерте чек-а.

## Фаза A4 — клиентский пакет (ЗАКРЫТА, 101/101, 2026-10-11)

Ядро (egressctl + egress-run, stdlib-only) было из A1; A4 добавила полное
тестовое покрытие по разделам 30/48 спеки.

### Готово
- [x] `client/tests/test_config.py` (23): discovery order (--config → env →
      /etc), flat-YAML parser, валидация (client_id/environment/ports/url/
      tun_backend), секреты из отдельных файлов (token, user:password),
      ошибки (missing/empty/bad format).
- [x] `client/tests/test_egressctl.py` (24): все команды 30.1 со stubbed API —
      human + `--json` output, exit codes 0/1/2/3/4/5, identity-mismatch guard
      (local client_id vs /v1/self), secret masking в `config show`
      (по умолчанию токены не печатаются), usage errors.
- [x] `client/tests/test_egress_run.py` (26): argv без shell-reparse (спейсы в
      аргументе), exit status (включая 128+signum), SIGTERM forwarding (child
      реально получает сигнал), one-shot cleanup в finally (в т.ч. при
      падении child), explicit route НЕ меняет default, `direct` без API,
      `--mode tun` → exit 6 (проверяется до direct short-circuit),
      concurrent invocations.
- [x] `tests/integration/test_a4.py` (28): **реальный egressd процесс** на
      loopback + реальный клиентский пакет (subprocess, on-disk config/token):
      whoami/identity, API parity (/v1/self == egressctl whoami), spoofed
      client_id игнорируется, per-client isolation (2 живых клиента),
      route set/current, mode set tun → unsupported, list/test, egress-run
      local execution (marker file + pid/uid), 3 concurrent one-shots
      (2 VPN + direct), signal forwarding через реальный бинарный путь,
      doctor (без утечки credentials).
- [x] Фикс parser-бага `egress_run.py`: `egress-run -- cmd` (без route)
      теперь usage error (exit 2) вместо silent "no route".

### Тестовое покрытие
- 73 unit (client/tests) + 28 integration (tests/integration/test_a4.py) =
  **101/101 passed**.
- Покрытие пунктов 48 спеки: 1,2,3,4,5,6,7,8,11,12,13,16,17,23,25 (host-side
  9,10,14,15,18,19,20,21,22,24,26 — host harness / live-стек).

## Фаза A5 — probe/benchmark/ranking (ЗАКРЫТА, 38/38, 2026-10-11)

### Готово
- [x] `egressd/src/egressd/ranking.py` — прозрачный ranking (spec 26):
      weighted score (health 40 + freshness 20 + latency 30 + geo 10;
      speed-aware: 35/15/25/15/10), все компоненты в [0,1], детерминированный
      tie-break по route id, per-route `explanation` + `components` +
      `formula` в ответе.
- [x] `GET /v1/best` (main.py): фильтры country/provider/domain/max_latency/
      speed_aware. `--domain`: кандидат только при свежем успешном probe
      (хранящиеся probe_samples, окно 900с) ИЛИ on-demand probe shortlist
      (bounded, через route-specific one-shot path) — GEO-эвристики НЕ
      квалифицируют route.
- [x] `GET /v1/routes?healthy=true|false` (spec 30.1 `list --healthy`).
- [x] Benchmark enrichment: `rebuild_catalog` подтягивает последний ok
      benchmark per route в catalog (throughput score для `--speed-aware`).
- [x] Клиент: `api.best()`, `api.benchmark_shortlist()`; `egressctl best`
      (полные флаги spec 26 + human output с explanation) и
      `egressctl benchmark [ROUTE]` (one route ИЛИ shortlist pipeline
      `--country/--provider/--top-latency/--bytes` по spec 25).
- [x] `tests/integration/test_a5.py` (38): ranking unit (weights=100,
      health/freshness/latency/throughput/geo tiers, ordering, freshness vs
      latency, speed-aware, max_latency, domain gate, deterministic tie-break,
      explanation), classifier (все 9 классов spec 24), /v1/best API
      (transparency, фильтры, unhealthy exclusion, stored probe, on-demand
      probe, speed-aware, auth), /v1/benchmark (bounded bytes, shortlist +
      direct baseline, unknown route, per-client history), probe parallel cap.
- [x] Фикс latency_score: ratio best/latency (relative speed) вместо
      (lat-best)/lat.

### Тестовое покрытие
- 38/38 passed. Обязательные checks A5: direct baseline ✓, parallel probes
  with cap ✓, DNS/TCP/TLS/HTTP classification ✓, bounded bytes/time ✓,
  `best --domain` uses actual probe ✓.

## Фаза A6 — Strict/TUN Phase 2 (ЗАКРЫТА в части, реализуемой в этой среде, 2026-10-11)

В этой среде TUN недоступен (`unshare -n` запрещён, нет CAP_NET_ADMIN,
`/dev/net/tun` отсутствует для netns). Реализованы ВСЕ артефакты A6, которые
можно реализовать и проверить здесь: local privileged helper, его security
invariant'ы, capability reporting, compose.tun.yaml (explicit privilege
grant), клиентский путь `--mode tun`. Live-проверки TUN (per-run netns,
concurrent host TUN jobs, UDP/QUIC) — deferred до целевого хоста с
CAP_NET_ADMIN (acceptance §50, пункты 1-8).

### Готово
- [x] `host/strict-helper/egress-tun-helper.py` — local privileged helper
      (spec 21): UNIX-socket only (no TCP), fixed protocol
      (`capability`/`run`/`cleanup`), caller identity из SO_PEERCRED (не из
      body), route-id format validation + upstream allowlist (helper нельзя
      направить на произвольный proxy/route), per-run netns + TUN +
      route-specific upstream (tun2socks), `direct+tun` (isolated direct
      path, spec 21.3), privilege drop до caller UID/GID (setpriv
      --reuid/--regid) после network setup, bounded lifetime (MAX_RUN_S),
      stale netns cleanup, audit events без command payload.
- [x] `host/strict-helper/Dockerfile` (iproute2 + tun2socks, stdlib helper).
- [x] `host/units/vibelab-egress-tun-helper.service` — systemd unit с
      minimal reviewed grant: `AmbientCapabilities=CAP_NET_ADMIN`,
      `CapabilityBoundingSet=CAP_NET_ADMIN`, `NoNewPrivileges`,
      `ProtectSystem=strict`, `DevicePolicy=closed` + `/dev/net/tun rw`.
- [x] `compose.tun.yaml` — EXPLICIT reviewed privilege grant (override, не
      часть default stack): `cap_drop: ALL` + `cap_add: [NET_ADMIN]`, no
      privileged, no docker.sock, no TCP ports (UNIX socket only),
      no-new-privileges, `/dev/net/tun` mount.
- [x] Клиент: `egress-run --mode tun` теперь вызывает helper (host-helper
      backend): получает route-specific one-shot upstream из egressd,
      передаёт его helper'у; `direct+tun` без upstream; session закрывается
      в finally; helper reports unsupported_in_environment → exit 6 (no
      silent proxy fallback); helper not running → exit 6; netns error →
      exit 1. `--timeout` флаг. `tun_helper_socket` в config (default
      `/run/vibelab-egress/tun-helper.sock`).
- [x] egressd `/v1/capabilities` — per-client tun capability
      (`supported`/`unsupported_in_environment` + backend), консистентно с
      `mode set tun` (400) и one-shot `mode=tun` (400).
- [x] Capability matrix (spec 21.5): host-main → `tun: supported`
      (host-helper); openscience-main → `tun: unsupported_in_environment`
      (gVisor: no /dev/net/tun, no CAP_NET_ADMIN). `whoami`/`capabilities`
      accurately reports mode availability (acceptance §50.12).

### Тесты
- [x] `tests/integration/test_a6.py` (43): capability probe (реальная
      среда: unsupported_in_environment + reason), fixed protocol validation
      (unknown op/field, malformed route, arbitrary upstream, direct+upstream,
      empty/oversized command, bad timeout), `run` unsupported → explicit
      (no proxy fallback), stale netns cleanup (только stale, только наш
      prefix), UNIX-socket protocol (frames roundtrip, EOF, SO_PEERCRED,
      end-to-end capability + unknown_op через реальный сервер в thread),
      compose.tun.yaml static audit, egressd per-client tun capability.
- [x] `client/tests/test_egress_run.py` +5: tun direct via helper (no
      upstream), tun route obtains one-shot upstream + session closed,
      helper unsupported → exit 6, helper error → exit 1, --default
      resolves route.
- [x] `tests/security/test_security_gate.py` +7 (helper privilege review):
      local-only (no AF_INET), SO_PEERCRED identity, fixed protocol (no
      free-form command), privilege drop (setpriv), no command in audit,
      compose.tun minimal capability, openscience client cannot reach helper.

### Deferred до целевого хоста (acceptance §50, live)
- [ ] Host-native `egress-run --mode tun ROUTE -- command` e2e (proxy-unaware
      TCP client) — нужен CAP_NET_ADMIN.
- [ ] Concurrent host TUN one-shot jobs через разные routes (per-run netns).
- [ ] `direct + tun` isolated direct path e2e.
- [ ] UDP/QUIC test (если route/protocol/backend поддерживает).
- [ ] Stale netns cleanup e2e (live).
- [ ] OpenScience local strict backend (gVisor-preserving) — исследовать;
      пока `unsupported_in_environment` (зафиксировано).

## Фаза A7 — Skill/docs (ЗАКРЫТА, 2026-10-11)

### Готово
- [x] `skills/egress/SKILL.md` (обязательный deliverable spec 31):
      - 14 hard rules (whoami first, per-client defaults, one-shot preference,
        direct as normal route, parallel invocations, fresh health before
        choosing, probe-domain for domain questions, provider update + --via,
        no full-fleet benchmark, tun only with capabilities, no silent
        fallback, HARD DIRECT/LiteLLM never proxied, no backend internals,
        verify after route set, verify exit country);
      - decision tree (one-off vs persistent, domain access, best route,
        stale inventory, parallel tasks, proxy→tun);
      - 10 примеров: whoami/current, set+verify default, direct one-shot,
        VPN one-shot, 2 concurrent one-shots, probe-domain --all, provider
        update (direct + --via), proxy→tun decision, unsupported_in_
        environment диагностика, best --domain.
- [x] README/INSTALL уже есть из A1 (backend + client install).

## Фаза A8 — Security/Integration review (static/pytest ЗАКРЫТА, 54/54, 2026-10-11)

### Готово
- [x] `tests/security/test_security_gate.py` (54):
      - **API surface audit**: public API == ровно задокументированный /v1
        набор (spec 27); нет raw Mihomo passthrough (/proxies, /rules,
        /configs, /version → 404), нет arbitrary config patch, нет remote
        exec, нет client provisioning;
      - **auth**: все authenticated endpoints → 401 без токена; unknown/
        malformed token → 401; health/ready без auth;
      - **impersonation**: body/query client_id игнорируется; cross-client
        endpoints отсутствуют; one-shot session isolation (чужой session не
        виден/не закрывается); default-capability только свой;
      - **injection**: 12 bad route IDs (traversal, null, newline, spaces,
        shell metachars, 5000 chars) → 400/404, default не меняется; bad
        modes → 400; oversized/malformed domain → 400; non-JSON body → 400;
      - **secret scan**: WG private key отсутствует в API responses,
        metrics DB (все таблицы), structured logs; provider file не
        отдаётся API;
      - **malformed provider**: broken subscription → 502 + last-known-good
        сохранён; update-all отчитывает per-provider;
      - **compose static audit**: no public binds (только 127.0.0.1 /
        ${DOCKER_GATEWAY}), no docker.sock, no privileged, mihomo admin 9090
        не опубликован, image pinned, секреты не в environment;
      - **client package audit**: клиент не содержит mihomo admin client
        (port 9090 / /proxies/ / secret), example configs без секретов,
        клиент вызывает только задокументированные endpoints;
      - **CLI/API parity**: каждая egressctl-команда мапится на endpoint;
        egress-run использует только data-plane endpoints;
      - **helper privilege review** (A6, spec 21.4): TUN helper local-only
        (no AF_INET), identity из SO_PEERCRED, fixed protocol (no free-form
        command), privilege drop (setpriv --reuid/--regid), audit без
        command payload, compose.tun minimal capability (cap_drop ALL +
        cap_add NET_ADMIN only), OpenScience client не может достичь helper.
- [x] **Найдены и закрыты 2 реальных дефекта**:
      1. FastAPI автосгенерировал `/docs`, `/redoc`, `/openapi.json`
         (unauthenticated schema exposure) → отключено
         (`docs_url=None, redoc_url=None, openapi_url=None`);
      2. oversized domain в /v1/probe-domain → 500 (UnicodeError из idna) →
         добавлена bounded domain validation (400).
- [ ] Live-часть (deferred до целевого хоста с Docker): публичные NIC
      закрыты, recreate-персистентность OpenScience client, host acceptance
      harness report — через `host/egress-host-test` + administrator loop.

## Журнал решений
- 2026-10-12: Повторная установка на globe — стек поднялся (mihomo healthy,
  egressd up; healthcheck-фикс сработал). Встроенный harness: 4 pass / 4 fail /
  9 skip. Все 4 fail и 5 «compose.yaml not found» — баги harness/установщика,
  не стека; закрыты:
  (1) `host_whoami_identity`/`host_default_route_mode`: harness вызывал
  `egressctl whoami --json`, но `--json` — глобальный флаг argparse и должен
  стоять ДО подкоманды (`egressctl --json whoami`) → rc=2 usage error.
  (2) `no_backend_secrets_in_client_bundles`: harness сканировал
  `/etc/vibelab-egress` рекурсивно и заходил в `secrets/` (там секреты лежат
  по дизайну) → ложный «leak» самого файла `mihomo_api_secret`. Фикс: prune
  `SECRETS` в os.walk.
  (3) `persistent_dirs_permissions`: harness требовал 0700 для всех
  подкаталогов ROOT, но `egressd` намеренно 0755 (uid 1000 пишет state/
  metrics/geo), а `client-bundles` создавался `mkdir -p` с umask 0755.
  Фикс: harness ожидает `egressd=0755`, остальное 0700; `provision-client.sh`
  теперь `chmod 700 "$OUT"` (bundle root).
  (4) 5× «compose.yaml not found»: harness брал REPO из дефолтного песочного
  пути `/srv/workspace/vibelab-egress`; фикс — дефолт = parent host/ dir
  (реальный checkout) + `export VIBELAB_EGRESS_REPO` в install-скрипте.
  Проверено: harness --static 9/0/8 (0 fail); `--json` до подкоманды парсится
  (после — rc=2, как в отчёте); bash -n + py_compile; 309 passed (без
  регрессий).
- 2026-10-12: Live-отчёт с globe (4 pass / 1 fail / 23 skip) — «prerequisites not met»,
  но вскрыл 2 реальных дефекта, блокировавших запуск бэкенда:
  (1) `compose.yaml`/`compose.tun.yaml` healthcheck.test в raw exec-list форме
  (`[sh,-c,...]` / `[python,-c,...]`) — Docker 28 compose отклоняет:
  `healthcheck.test must start either by "CMD", "CMD-SHELL" or "NONE"` →
  `docker compose up -d` падал, стек НЕ поднимался (отсюда все "no live backend").
  Фикс: явная форма `[CMD-SHELL, <shell-string>]` (стабильна во всех версиях
  compose). Проверено: yaml-parse → test[0]==CMD-SHELL valid=True для всех 3
  сервисов; security gate 54/54; host-test static — все compose-чексы pass.
  (2) `install-client.sh` копировал СОДЕРЖИМОЕ пакета в `$libdir`
  (`cp -r $PKG_SRC/. $libdir/`), а wrapper импортирует `vibelab_egress_client.*`
  из `$libdir` → нужен `$libdir/vibelab_egress_client/` → `ModuleNotFoundError`
  (preflight в отчёте). Фикс: `cp -r $PKG_SRC $libdir/` (копируем ДИРЕКТОРИЮ
  пакета). Проверено: симуляция install → `import vibelab_egress_client.egressctl`
  OK.
  Остальные skips в отчёте — ожидаемые при отсутствии: TUN-сокета
  (compose.tun.yaml не поднят) и клиента в OpenScience-контейнере (не установлен).
- 2026-10-12: `egress-hostctl sync-docker-cidrs` — фикс под Docker 28. Корень
  проблемы: Docker 28 убрал `.Subnet`/`.Gateway` из template-контекста
  `docker network ls --format` (у сети может быть несколько IPAM-конфигов,
  они больше не уплощаются) → `template parsing error: can't evaluate field
  Subnet in type *network.networkContext` → установка падала на шаге 5.
  Фикс: `ls` отдаёт только name/id/driver (`DOCKER_LS_FORMAT`), а subnet/gateway
  читаются по-имени через `docker network inspect --format
  "{{range .IPAM.Config}}{{.Subnet}}\t{{.Gateway}}\n{{end}}"` (стабильно во
  всех версиях Docker; берётся первый IPAM-конфиг). Оба вызова — фиксированный
  argv `docker` (без shell) — security-инвариант `no_arbitrary_host_command`
  сохранён (AST-проверка pass). Offline-путь (`--offline`, 5 полей) не тронут.
  Проверено: py_compile OK; stubbed `docker` (ls + inspect) → корректный
  docker-cidrs.yaml (2 сети, subnet+gateway); offline-фикстура → тот же вывод;
  security gate 54/54.
- 2026-10-11: GEO — сделан ОПЦИОНАЛЬНЫМ (решение пользователя). Корень
  проблемы: `install-backend-host.sh` hard-fail'ил на `GEO download failed:
  GeoIP.dat` (404) и прерывал установку. Две причины: (1) мёртвый release-тег
  `release` (upstream перевёз данные на движущийся тег `latest` — старый 404);
  (2) GEO был обязательным шагом. Исправления: (a) все URL GEO (configgen.py,
  geo_update.py, mihomo/config.template.yaml) — `release` → `latest`;
  (b) GEO больше не предзагружается установщиком и не монтируется per-file —
  mihomo сам скачивает/обновляет GeoIP/GeoSite/ASN в свой persistent home dir
  через `geox-url` + `geo-auto-update` (spec 14.1; §41 допускает нативный
  auto-update). Убраны 3 `:ro` file-маунта из compose.yaml (при отсутствии
  файла Docker создавал каталог на host-пути — ломало mihomo и повторные
  загрузки) и `$ROOT/geo` + флаг `--geo-dir`. Mihomo работает без GEO (spec
  14.3: GEO — context, не evidence); egressd enrichment store
  (`${ROOT}/egressd/geo`) и так best-effort (fetch-failure → continue).
  309 passed (без регрессий).
- 2026-10-11: A6 TUN helper — local-only UNIX-socket privileged helper
  (spec 21.4). Ключевые решения: (1) route catalog принадлежит egressd
  (валидация route при создании one-shot session); helper enforcing route-id
  FORMAT (safe charset) + upstream allowlist (только configured one-shot
  listener), поэтому его нельзя направить на произвольный proxy/route —
  без static routes file (он бы устаревал при provider updates). (2)
  Privilege grant минимальный и reviewed: cap_drop ALL + cap_add NET_ADMIN
  (compose.tun.yaml / systemd unit), no privileged, no docker.sock, no TCP
  port, no-new-privileges; /dev/net/tun rw. (3) Caller identity из
  SO_PEERCRED, не из body; target запускается через setpriv --reuid/--regid
  (privilege drop после network setup). (4) Capability matrix per-client:
  host-main tun=supported (host-helper), openscience-main
  tun=unsupported_in_environment (gVisor: no /dev/net/tun, no CAP_NET_ADMIN);
  /v1/capabilities отчитывает консистентно с mode-set/one-shot checks.
  (5) No silent proxy fallback: helper reports unsupported_in_environment →
  client exit 6. Live-проверки TUN (per-run netns e2e, concurrent jobs,
  UDP/QUIC) deferred до целевого хоста с CAP_NET_ADMIN (acceptance §50).
- 2026-10-11: A5 ranking — прозрачная weighted formula (health/freshness/
  latency/geo; speed-aware добавляет throughput), все компоненты в [0,1],
  детерминированный tie-break по route id; ответ несёт per-route
  explanation+components+formula (spec 26: "Формула должна быть прозрачной и
  возвращаться в JSON explanation").
- 2026-10-11: `best --domain` квалифицирует route только по реальному probe
  (хранящийся свежий sample ИЛИ on-demand probe shortlist через one-shot
  path); GEO-эвристика (country из display name) даёт только geo-компонент
  score, а не candidacy (spec 26: "route должен считаться кандидатом только
  при успешном свежем domain probe либо после on-demand probe shortlist").
- 2026-10-11: A8 surface audit — public API зафиксирован как ровно
  задокументированный /v1 набор; FastAPI /docs|/redoc|/openapi.json
  отключены (unauthenticated schema exposure). Oversized/malformed domain →
  400 bounded validation (ранее 500 UnicodeError из idna).
- 2026-10-11: A4 — egress-run parser: `--` без route/--default → usage error
  (exit 2); ранее молча пропускался как "no route".
- 2026-10-09: Mihomo pinned v1.19.32 (latest на момент проверки).
- 2026-10-09: Архитектура роутинга подтверждена PoC: IN-USER → per-client
  selector (default) и IN-USER → per-slot selector (one-shot). Число правил
  не зависит от числа нод (P18: 300 нод → 39 правил).
- 2026-10-09: Обнаружена невалидность proxy-тестов (LAN-DIRECT перехват);
  фикс через non-LAN публичную цель.
- 2026-10-10: Mihomo image pin `metacubex/mihomo:v1.19.32` + digest
  `sha256:bac1a74...c225e`; образ alpine-based (есть `wget` для healthcheck),
  config dir `/root/.config/mihomo/`, entrypoint `/mihomo`.
- 2026-10-10: `configgen.py` — поле `listener_listen` (host/PoC:
  `127.0.0.1`; контейнер: `0.0.0.0`); публикация на host-адрес в compose и
  есть enforcement «no public binds».
- 2026-10-10: Bootstrap-генерация конфига — `bootstrap_config.py` (stdlib
  YAML-эмиттер) создаёт `generated/config.yaml` из secrets до первого старта
  стека; в A3 egressd забирает генерацию на себя.
- 2026-10-10: Клиентский пакет — stdlib-only (без PyYAML/requests), чтобы
  ставиться в bare host и gVisor без package index.
- 2026-10-10: Host-утилиты — Python3 stdlib-only, фиксированные операции, без
  arbitrary shell; `egress-host-test` имеет `--static` режим (без Docker
  daemon).
- 2026-10-10: Секреты — только mounted files (0600), никогда env-переменные;
  в репортах — только path + sha256[:8].
- 2026-10-10: `egress-host-test` no-arbitrary-command — AST-анализ вместо
  substring-поиска (substring ловил docstring).
- 2026-10-10: `is_local_bind` — явная проверка `is_unspecified`/`0.0.0.0`/`::`
  → False (`ipaddress` считает `0.0.0.0` private).
- 2026-10-10: `egress_run.py` — tun-проверка ПЕРЕД direct-кратчайшим путём
  (`--mode tun direct` не обходит exit 6).
- 2026-10-10: `egress_run.py` — смерть child сигналом нормализована в
  `128+signum` (SIGTERM → 143).
- 2026-10-10: A1 закрыта — 47/47 suite `/tmp/a1_test.sh` (2 прогона);
  live-ветки без Docker daemon → skipped, не дефект.
- 2026-10-10: A2 — provider-адаптеры (`adapters/`): detection
  (Clash/base64/wg-conf/vless), нормализация, redaction, atomic write +
  last-known-good, отказ на дубликате node-name. Тег провайдера — через
  `override.additional-prefix` (spec 11.1), адаптер даёт чистые имена.
- 2026-10-10: A2 — найден и закрыт баг двойного префикса
  (`blanc-blanc-...`): адаптер больше не печатает префикс, тег — только
  `additional-prefix`.
- 2026-10-10: A2 — Mihomo молча грузит дубликат node-name (не отказывается
  стартовать); защита — `ValueError` в адаптере + collision-free конфиг.
- 2026-10-10: A2 — GEO: `geo-auto-update: true` + `geo-update-interval: 24`
  в конфиге (нативный путь) + `egressd/geo_update.py` (явный update через
  API: download + atomic write + metadata + status/staleness).
  `atomic_write_bytes` — binary-safe (GEO-датасеты бинарные).
- 2026-10-10: A2 — egressd `/v1/geo/status` + `/v1/geo/update` (auth,
  datasets subset, force); `egressd/Dockerfile` копирует `adapters/`.
- 2026-10-10: A2 закрыта — 42/42 offline (pytest) + 8/8 live
  (`poc/run_a2.py`, реальный мihomo), стабильно x2.
- 2026-10-11: A3 — полный `/v1` API egressd: catalog (stable IDs), metrics DB
  (sqlite WAL, secret scrub), ClientState (per-client defaults + reconcile),
  ProbeEngine (slot + socks_probe + data-plane-driven классификация),
  ConfigController (regenerate + hot-reload), один `/v1`-роутинг (spec 27).
- 2026-10-11: A3 — найден и закрыт критический баг `catalog.mihomo_name()`:
  lookup по route-ID в мihomo-name карте не попадал → возвращал route-ID с
  двоеточием → mihomo 400 «proxy not exist», селекторы на DIRECT. Offline не
  ловил (mihomo down). Фикс: явная карта `_id_to_mihomo`.
- 2026-10-11: A3 — найден и закрыт баг stale-catalog: ProbeEngine держал
  начальную пустую ссылку на catalog, rebuild её не обновлял → probe видел все
  routes как unknown (`proxy_failure`, `latency_ms=None`). Фикс:
  `rebuild_catalog` обновляет `engine.catalog`.
- 2026-10-11: A3 — `_select()` verify-and-retry: PUT + GET `now` (mihomo
  грузит file-providers асинхронно; до загрузки нода PUT → 400). `st==0` →
  fast return (offline), timeout → warning + False.
- 2026-10-11: A3 — JsonLogHandler печатает `extra=`-поля (audit + PUT-failure
  detail); раньше payload терялся (скрыл status 400 при отладке).
- 2026-10-11: A3 — harness: provider source + env ДО старта egressd (отдельный
  процесс); fake-node offset для новой ноды (порт-коллизия).
- 2026-10-11: A3 закрыта — 18/18 offline (pytest) + 7/7 live
  (`poc/run_a3.py`, реальный мihomo), стабильно x2; A2+A3 offline 60/60.
