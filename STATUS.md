# vibelab-egress — статус работы

Обновляется после каждого завершённого шага. Источник правды по требованиям — `TECH_SPEC.md`.

## Установка host-клиента — 2026-10-19

Через `pkexec` установлен текущий клиент в `/usr/local` и профиль
`host-uid-1000` в `/etc/vibelab-egress`; `egressctl whoami` подтвердил ID и
режимы host, а `egress-run direct -- /usr/bin/true` завершился успешно.
Повторный live acceptance через установленный `/usr/local/bin` клиент дважды
дал **18 PASS / 3 FAIL / 7 SKIP**; оба отчёта независимо проверены как
`VERIFIED`. Два FAIL касаются TUN TCP/UDP, один — bounded benchmark; все три
выбрали один Hysteria2 маршрут, у которого не было публичного exit IP,
UDP-ответа и HTTP headers. До установки acceptance был **23 PASS / 0 FAIL /
5 SKIP**, поэтому текущие повторы указывают на доступность маршрута, не на
ошибку CLI установки. Закрытые отчёты находятся в
`~/.local/share/vibelab-egress-test/live-user-installed-client*.json`.

## Закрытие локального QA — 2026-10-19

Плановые четыре блока закоммичены отдельно: `2379d91`, `3734942`, `5a3aba2`,
`fd055f7`. Полный offline-набор: **376 passed**; install harness:
**75 passed, 0 failed**; Python/Bash syntax и `git diff --check` прошли.
Локальный backend пересобран из текущего дерева и healthy.

Полный user live acceptance: **23 PASS / 0 FAIL / 5 SKIP**. Независимый
`verify-live-report.py` подтвердил hash, все PASS и secret scan: **VERIFIED**.
Отчёт с правами 0600 хранится вне репозитория в
`~/.local/share/vibelab-egress-test/live-user-after-fixes.json`.
OpenScience отсутствует; root-only harness не удалось повторить, так как
`sudo -n` требует пароль. Эти причины указаны в handoff.

Повторный probe всех **401** маршрутов по двум независимым HTTPS целям,
ожидаемые статусы 200/204, parallel 8, партии до 50: **33** ответили по обеим
целям, **346** повторно дали `tls_failure`, **22** дали target-specific или
нестабильный результат. Подробная таблица и передача следующему инженеру —
`reports/local_subscriptions_2026-10-18.md` и `handoff.md`.

## Передача локального QA — 2026-10-18

- CLI/API клиента: `probe-domain` принимает и передаёт фильтры, список маршрутов,
  параллелизм, timeout, параметры HTTP, стадии и лимит согласно §24; `--json`
  работает после имени команды. Сохранены stdin URL, длинные запросы и
  `benchmark --bytes`. Целевая проверка `client/tests/`: 98 passed.
- Backend: параметры probe валидируются; явный список сверх текущего лимита
  отклоняется, каталог обрезается до лимита, parallel ограничен числом one-shot
  слотов. Probe выполняется в рабочем потоке; сохранены классификация TLS
  timeout и ограниченный benchmark. Целевые A3/A5: 61 passed.
- Установка: backend installer работает из чужого cwd, host client сохраняет
  проход к backend secrets для service UID при закрытых файлах. Сценарий
  установки с новым cwd и остальные offline install cases: 75 passed.
- Приёмочный harness: SOCKS-aware HTTP 204 для parallel one-shot, полный JSON
  больших ответов, допуск меняющегося публичного direct IP, явные SKIP при
  отсутствии OpenScience и усиленный независимый verifier. Целевые тесты
  verifier: 15 passed; сохранённый globe-отчёт: VERIFIED (19/0/9).

## Локальная проверка подписок — 2026-10-18

На этой машине установлен backend и host TUN helper. Четыре личных источника
дали 405 принятых proxy маршрутов и 12 пропусков валидатора; полный каталог
содержит 406 маршрутов с `direct`. Несколько SS маршрутов подтверждены
реальными HTTP 204 через `egress-run` и TUN. Root host harness: 18 pass,
0 fail, 4 skip; root live acceptance: 17 pass, 0 fail, 11 skip.
Пользовательский live acceptance: 23 pass, 0 fail, 5 skip; независимый
verifier подтвердил hash и отсутствие секретов. Подробности и журнал багов:
`reports/local_subscriptions_2026-10-18.md`.

Исправлены запуск installer вне репозитория, права каталога host клиента,
безопасный ввод URL из stdin, обрезание больших JSON в acceptance, неверные
proxy/IP ассерты, клиентский тайм-аут долгих операций и игнорирование
`benchmark --bytes`. Backend probe теперь сохраняет результаты других узлов
при TLS timeout одного маршрута; benchmark корректно разбирает HTTP URL и
считает только тело ответа. Локальный офлайн-набор: 373 passed, сценарии
установки: 75 passed. OpenScience отсутствует на этой машине; его checks
отмечаются skip. Лимит `probe-domain --all` — 50 маршрутов за запрос.

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
      `poc/mihomo` (CGO off), `poc/mihomo-gvisor` (tags with_gvisor, userspace WG);
      бинарники и vendor-исходники исключены из репо (коммит `fa65d70`) —
      рецепт сборки в `DEVELOPMENT.md`, fetch — `scripts/fetch-poc-vendor.sh`
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
 2026-10-16: третий live-отчёт с globe (09:32 UTC) — 15 pass / 1 fail /
 12 skip. OpenScience `whoami` и HARD DIRECT теперь проходят; Mihomo admin
 доступен. Оставшийся FAIL `tun_local_workload` снова `no_response`: запуск
 `docker compose ... up -d` оставил старый образ helper (`Running`, без
 build/recreate). Нужен явный `--build --force-recreate`; после него текущая
 A6-архитектура должна честно сообщить `unsupported_in_environment`, но
 полноценный host-native TUN пока не реализован. Direct probe всё ещё
 `auth rejected`: egressd не получал `EGRESSD_ONESHOT_SLOTS_FILE` и применял
 другие пароли слотов, чем bootstrap/Mihomo. Исправлено в Compose, а оба
 loader'а используют общий валидирующий reader. Чек §49.39 с `docker
 restart` ошибочно выдавал PASS за recreate; теперь это skip с причиной.
 `egress-verify` запущен с runtime `runc`, поэтому проверка его client
 package не подтверждает поведение OpenScience/gVisor §50.9; PASS для этого
 пункта теперь требует runtime `runsc`/gVisor.
 2026-10-16: повторный live-отчёт с globe — **14 pass / 2 fail / 12 skip**.
 `tun_local_workload` получил `no_response`, после чего helper socket исчез;
 `a8_internal_hard_direct` ложнопадал из-за `connect_ex(...) < 0` вместо
 `== 0`. Контейнерный `egress-verify` имеет правильный gVisor client.yaml,
 но не резолвит `host.docker.internal`; его надо запускать с
 `--add-host=host.docker.internal:host-gateway` (или `extra_hosts`).
 Исправления в текущем дереве: helper возвращает ошибку setup без падения
 процесса и честно показывает отсутствие CAP_SYS_ADMIN для `ip netns add`;
 TCP-чек сохраняет errno; повторная установка принудительно пересоздаёт
 backend-контейнеры для загрузки обновлённого generated config. TUN остаётся
 **не подтверждённым live**: текущая Compose-модель даёт только NET_ADMIN,
 а host-native command внутри helper-контейнера не имеет host filesystem.
 Не расширять права контейнера без отдельного пересмотра дизайна A6.
 `a8_probe_domain_all` при единственном direct теперь skip по §49.20.
 2026-10-15: репозиторий автономен (GitHub `VorontsovIE/egress`), проект =
 корень git. Тяжёлые third-party данные исключены из истории (GEO ~370 МБ +
 mihomo/wgserver vendor-исходники и бинарники ~127 МБ): перед оффлайн
 PoC-прогоном — `sh scripts/fetch-geo-data.sh` и `sh scripts/fetch-poc-vendor.sh`.
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
 - **Install logging (2026-10-14, ветка `install-logging`, 7 коммитов:
   handoff + 6 фиксов)**: структурный лог установки
   (`<root>/install-backend-host.log`, `step N/10` + run header + ERR trap),
   robust health gate (3 последовательных полла, redacted-дампы),
   self-explaining harness (5 новых live-чексов + блок `diagnosis`),
   оффлайн-свит `tests/install/` (stubbed docker, без root — возрождение
   утраченного `/tmp/a1_test.sh`), per-service health + лог
   `<root>/verify-backend-host.log`. Подробности — журнал 2026-10-14;
   merge в `main` — решение lead'а.
   - [x] **Установить бэкенд на хосте**: `bash scripts/install-backend-host.sh`
         (GEO опционален; 2026-10-12: стек поднят на globe — mihomo healthy,
         egressd up; harness-фиксы из этого же дня, см. журнал решений).

   - [x] **Live-проверка на globe (2026-10-15/16)**: бэкенд + host-клиент
         + контейнерный клиент (`openscience-main` в `egress-verify`),
         offline 314 passed в контейнере (python 3.11, root). Live
         acceptance 23:35 → 9 pass / 7 fail / 12 skip; ВСЕ 7 fail —
         найденные root causes, серия фиксов 2026-10-16 (журнал ниже).
         Дальнейший план для этого отчёта заменён host-native A6 ниже.
   - [ ] **Установить host-native TUN-helper на globe**:
         `sudo bash scripts/install-tun-host.sh --user vibelab`, обновить
         Compose backend, открыть новую login-сессию для группы socket,
         затем запустить §50 acceptance. Контейнерный helper удалить как orphan.
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
- 2026-10-14: Install logging + self-explaining tests — ветка
  `install-logging` (work order `HANDOFF-install-logging.md`; 6 фикс-коммитов
  после handoff-коммита; merge в `main` делает lead). Причина — прогон
  2026-10-12: health gate пропустил restarting-egressd (ОДИН docker inspect
  вернул устаревшие healthy/running, `RestartCount=1` никто не читал),
  установка «успешно» дошла до harness и упала на
  `host_whoami_identity: Connection refused` (exit 4) без объяснения, какой
  шаг сломался; лога установки не существовало. Сделано:
  (1) `scripts/install-backend-host.sh` — `log()`: `[<UTC ts>] [step N/10]`
  в stdout + append в `$ROOT/install-backend-host.log`; run header
  (hostname/kernel/user, версии docker/compose/python3, git rev, argv —
  только пути); `STEP`/`STEP_NAME` на всех 10 маркерах; `trap on_error ERR`
  (step + BASH_COMMAND + exit code + `full log:`; непредвиденные сбое —
  exit 3, реальный rc остаётся в строке лога); `run_logged` (tee в лог с
  PIPESTATUS[0]): compose build/up/ps, `sync-docker-cidrs`,
  `bootstrap_config`, harness (JSON-репорт целиком в лог); обязательные
  subprocess-сбои — `die_env` с `full log:` (compose up rc 1 → exit 3).
  (2) Health gate: для КАЖДОГО сервиса `State.Status=running` +
  `Health.Status=healthy` + `RestartCount=0`; переход — только после 3
  последовательных поллов (2 с, счётчик сбрасывается); timeout 90 с →
  поля inspect (State/Health/RestartCount/ExitCode/Error/OOMKilled) +
  `docker logs --tail 100` через `redact_stream()` (значения
  mihomo_api_secret, clients/*.token, *.proxy-creds → `[REDACTED]`) +
  `die_env` с `full log:`.
  (3) `host/egress-host-test`: 5 новых live-only чексов (`--static` —
  skipped): `egressd_container_healthy` (оба сервиса, баг 2026-10-12),
  `client_package_importable` (import из /usr/local/lib/vibelab-egress;
  skipped, если префикс не установлен — документировано в docstring),
  `egressctl_on_path`, `generated_config_present` (mixed-port/socks-port),
  `secrets_readable_by_container_user` (uid 1000; skipped, если uid нет).
  `collect_stack_diagnosis()`: при ЛЮБОМ упавшем live-чеке — ОДИН блок
  `diagnosis` в корне отчёта (compose ps, state/health/restarts, redacted
  `docker logs --tail 50`; выбор «общий блок» задокументирован в
  docstring) + actionable `next:`-строка в detail каждого упавшего
  live-чека. Существующие 17 чексов — без изменений (статусы и detail
  сопоставлены по --static-прогону).
  (4) `tests/install/` — оффлайн-свит (stubbed docker/id/sleep/ufw/
  systemctl/findmnt/df/egressctl, `VIBELAB_EGRESS_TEST_MODE=1`): success
  (rc 0, run header + маркеры всех 10 шагов, config.yaml, секреты 0600,
  harness 0 failed), compose up rc 1 → exit 3 + step 9 + `full log:`,
  never-healthy → exit 3 + дамп + RestartCount + redaction (вбитое
  значение секрета в лог возвращается только как `[REDACTED]`),
  restarting (усталый healthy) → exit 3, root-escape (--help rc 0; без
  TEST_MODE под non-root → exit 3), negative-harness из утраченного
  /tmp/a1_test.sh (0.0.0.0:1080, docker.sock, provider-секрет в бандле).
  `sleep`-стуб: 90-с таймаут прогоняется мгновенно, логика цикла — в
  полном объёме. В install-скрипте — test-escape (только root-check; в
  production не используется, в шапке задокументировано). Non-root
  success-путь: `--skip-client` + препроvisions бандла реальным
  provision-client.sh (install-client.sh пишет /usr/local + /etc —
  root-only; в root-контейнере свит ходит ровно по флагам work order).
  (5) `scripts/verify-backend-host.sh`: per-service health (старый
  grep '"healthy"' проходил, когда healthy был ХОТЬ ОДИН из двух сервисов)
  + весь вывод в `$ROOT/verify-backend-host.log` (run header как в
  install-логе); 7 секций, счётчик, exit 0/2/3 — без изменений.
  (6) INSTALL.md — «Логи и диагностика установки»: где логи, формат,
  таблица exit codes 0/2/3/4 + действия, идемпотентность, health gate.
  Проверено: bash -n/py_compile на каждом коммите; оффлайн-свит 56/56
  (`python3 -m pytest tests/install/ -q`); полный свит **302 passed**
  (базовые 301 + tests/install; 8 environment-зависимых тестов требуют
  root: PermissionError `/run/vibelab-egress`, `/var/lib/egressd` —
  (на root-контейнере: 309)); security gate 54/54. Инварианты:
  data plane (egressd/mihomo/compose/client) не тронут, новых
  зависимостей нет, host-инструменты stdlib-only без shell=True/eval/exec,
  секреты в логах — только `[REDACTED]`/пути, `poc/` и `reports/` нетронуты.
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
- 2026-10-15: Реструктуризация репозитория: проект = git root («внешний» слой
  упразднён — корневые метофайлы AGENTS.md/DEVELOPMENT.md/.gitignore перенесены
  в проект, побайтовые дубли TECH_SPEC.md и live-acceptance.py удалены).
  ~370 МБ GEO-данных исключены из истории (host/geo, poc/geo, poc/geo.tar.gz,
  poc/meta-rules-dat-release, копии в poc/run* и poc/diag_*/mihomo) —
  скачиваются по требованию `scripts/fetch-geo-data.sh` (--full/--release/
  --check, codeload-fallback для OpenScience). SHAs переписаны
  (git filter-repo); ветка `main`; force-push на GitHub.

- 2026-10-16: Live (globe) — live-acceptance 9/7/12: ВСЕ 7 fail получили
  root cause и фикс (серия 2026-10-16):
  (1) mihomo admin API был привязан к 127.0.0.1 ВНУТРИ контейнера mihomo —
  egressd (другой контейнер) не мог его достать: mihomo.reachable=false,
  все пробы proxy_failure, версия не отчитывалась, селекторы не
  верифицировались → `controller: 0.0.0.0:9090` (порт 9090 нигде не
  публикуется; доступ — только Bearer-secret в приватной compose-сети);
  (2) ProbeEngine/benchmark смотрели на 127.0.0.1:18081 ВНУТРИ egressd
  (там ничего не слушает) → `EGRESSD_PROBE_HOST=vibelab-egress-mihomo`
  (compose.yaml); capability-ответы клиентам не меняются (клиент ходит
  в свой published endpoint);
  (3) клиентский one-shot брал host/port из capability бэкенда (его
  viewpoint 127.0.0.1:18081 — из контейнерного клиента недоступен) →
  клиент берёт host/port из СВОЕГО конфига (published порт 1081),
  credentials — из capability; allowlist tun-helper тоже published порт
  (127.0.0.1:1081; прежние 18081 с хоста недостижимы);
  (4) `/v1/status` не имел версии egressd при недоступном mihomo (49.42)
  → топ-левое `version` (0.1.0, синхронно с client `__version__`);
  (5) `tun_helper_socket()`: `str(None)` == `"None"` — truthy — маскировал
  env и default (в репорте "socket not found: None", spec 50.9) →
  резолв config-key > env > default; host client.yaml при этом НИКДА не
  содержал ключа (default — контейнерный путь, на хосте нет) →
  `provision-client.sh --tun-socket` (host: `$ROOT/tun-helper/tun-helper.sock`),
  harness резолвит flag > env > host client.yaml > default;
  (6) secrets-скан ложно срабатывал: `clients/*.profile.json` —
  детерминированная НЕ-секретная метаданные (одинаковая строка в
  fixture/PoC) → исключены из множества секретов; `generated/config.yaml`
  легитимно содержит mihomo-secret + client/oneshot credentials → в
  generated/-скане исключаются значения этих origin'ов (значения
  подписаны origin'ом, в leaks видно, что именно);
  (7) litellm: `/health` отвечает 503, пока не настроен provider
  (контейнер при этом up) → `/health/liveliness`; parallel-oneshot и
  `best --domain` — skip без proxy-роутов (с одним direct-роутом чек
  вырождается); recreate-чек — self-диагностика в skip (installed
  client.yaml + `config show` + mounts: в прогоне 23:35 контейнер
  предъявлял HOST control URL — следующий репорт покажет, куда ушло
  контейнерное конфигование).
  Offline: новые тесты (capabilities 4, one-shot vantage 2, str(None) 4,
  backend version 1), security gate 54/54, install-свит без изменений.

- 2026-10-16: второй live-отчёт (08:48 UTC, globe): 14/2/12. Для
  `a8_internal_hard_direct` найдено условие `connect_ex < 0` (код успеха
 равен 0); producer/verifier теперь сохраняют и проверяют сырой errno.
  `tun_local_workload`: helper закрыл сокет без ответа; `ip netns add` бросает
  `CalledProcessError`, который прежний сервер не ловил. Теперь ошибка
  возвращается клиенту, а capability проверяет обязательный для этого
  backend CAP_SYS_ADMIN. Это диагностический фикс, не реализация TUN.
  `egress-verify` не имеет DNS-записи `host.docker.internal` (подтверждено
  `Name or service not known` и installed client.yaml), поэтому install-client
  не может исправить его без пересоздания контейнера с `--add-host`.
  Mihomo остался `reachable=false` и SOCKS probe вернул `auth rejected`:
  bind-mounted generated config не вызывает Compose recreate; install теперь
  использует `up -d --force-recreate`, затем нужен новый live-отчёт.
  `probe-domain --all` с одной direct-строкой больше не выдаёт PASS за
  сравнение нескольких routes (§49.20). Локально: py_compile и
  `git diff --check` чисты, 13 точечных pytest passed; offline install suite
  71/71. Полный live-прогон ожидает globe.

- 2026-10-16: третий live-отчёт (09:32 UTC, globe): 15/1/12. Исправлена
  связка one-shot credentials: в Compose добавлен путь к постоянному
  `oneshot-slots.json` для egressd; probe allocator и config generator
  читают его одним кодом и падают явно, если заданный файл отсутствует,
  вместо молчаливой подмены паролей. `probe-domain --all` теперь считает
  сплошной `proxy_failure` инфраструктурной ошибкой даже при одной direct
  route. `--recreate` фактически делал только `docker restart`, поэтому
  §49.39 больше не получает ложный PASS: результат skip до настоящей
  проверки воспроизводимого контейнера. Старый helper image на globe
  остался после `up -d`; нужен явный rebuild/recreate для диагностики A6.
  `egress-verify` работает под `runc`, так что §50.9 нельзя засчитывать
  как проверку OpenScience/gVisor.
  Локально: 12 целевых pytest passed, py_compile/bash -n/YAML и
  `git diff --check` чисты. Live-повтор с новым образом ожидается.

- 2026-10-16: четвёртый live-отчёт (11:57 UTC, globe): 14/6/8.
  Новый helper отвечает явно: `ip netns add requires CAP_SYS_ADMIN; helper
  has NET_ADMIN only` (exit 6). Это один архитектурный блокер §50.2, от
  которого зависели остальные пять TUN-падений; расширять привилегии
  helper без пересмотра модели безопасности нельзя. Наличие UNIX-сокета
  ошибочно выдавалось `egressctl capabilities` за `tun=supported`: теперь
  клиент запрашивает локальную capability у helper и отражает её реальный
  статус; live harness независимо проверяет ответ helper, §50.2 оставляет
  FAIL, зависимые TUN-проверки дают skip с причиной. §50.4 теперь требует
  две разные proxy-route плюс direct; одиночный direct больше не запускает
  «параллельный» тест. Verifier проверяет оба инварианта. SOCKS direct-probe
  дошёл до TLS/HTTP (301), Mihomo healthy; для §49.20 всё ещё нужны
  дополнительные маршруты. `egress-verify` запущен под runc, поэтому §50.9
  остаётся skip до проверки настоящего gVisor-контейнера.
  Локально: 76 целевых тестов и security gate 54/54 прошли вне песочницы;
  live-повтор ожидается.

- 2026-10-16: пятый live-отчёт (13:31 UTC, globe): 14/1/13,
  `verify-live-report.py` — VERIFIED, hash OK, 14 PASS подтверждены.
  Единственный FAIL — §50.2: host helper сообщает отсутствие CAP_SYS_ADMIN
  для `ip netns add`; связанные TUN-проверки теперь честно SKIP.
  `egressctl capabilities` совпадает с независимым ответом helper:
  `unsupported_in_environment`. Backend/Mihomo и проверки §49, доступные
  с одним direct route, проходят; provider-зависимые проверки SKIP.
  §50.9 остаётся SKIP, поскольку тестовый контейнер имеет runtime runc,
  а не gVisor. §49.39 остаётся SKIP: проверен restart, не recreate.
  При запуске verifier без root его отдельный secret scan был пропущен;
  producer-чек `a8_secrets_absent` завершился PASS.

- 2026-10-16: A6 host-native PoC начат. В одноразовом Ubuntu 24.04 контейнере
  с `CAP_SYS_ADMIN` + `CAP_NET_ADMIN` проверены named netns, veth, TUN,
  Mihomo v1.19.32 под непривилегированным UID, `direct + tun` через NAT и
  TCP через per-run Mihomo → фиктивный SOCKS5. Результаты и границы проверки
  записаны в `poc/A6_POC.md`. Это НЕ host-native live-приёмка globe; UDP,
  systemd, host filesystem и cleanup ещё не подтверждены. Для SOCKS5
  outbound обязательно явное `interface-name` veth, иначе Mihomo выбирает
  tun0 и зацикливает upstream.

- 2026-10-16: реализован кандидат host-native A6. Контейнерный helper и
  `compose.tun.yaml` заменены на host systemd helper с ограниченным набором
  capabilities и UNIX socket; протокол v2 передаёт stdio, cwd/env/umask,
  peer UID/GID/группы берутся из OS. Per-run netns/veth/nft/TUN, прямой NAT,
  отдельный Mihomo без capabilities для proxy, точечные host firewall rules,
  cleanup и pinned binary installer добавлены. §50.2 теперь проверяет запись
  в host файл и cwd; §50.11 проверяет ошибку отсутствующего helper. Offline
  suite: 332 PASS с `EGRESSD_METRICS_DB` в `/tmp`. Изолированный Docker PoC
  проверил direct create/exec/cleanup (UID 65534, cwd `/tmp`, RC 0) и proxy
  TUN → фиктивный SOCKS5 CONNECT (`VIA_PROXY`, RC 0). Результаты не заменяют
  live acceptance: sudo на globe без пароля недоступен, host unit там не
  установлен, provider/UDP/host firewall и фактический gVisor не проверены.
  A6 остаётся открытой. Handoff для агентов: `handoff.md`.

- 2026-10-16: источник A6 доставлен на globe: `31d16df`, затем installer
  исправлен в `9cddf57` для удаления только старого exact-name Docker helper.
  `git pull --ff-only` завершился, локально на globe Compose config и syntax
  host unit корректны; `reports/host_acceptance_report.json` сохранён как
  локально изменённый файл. Старый helper пока продолжает работать:
  host socket отсутствует, `sudo -n` для `vibelab` требует пароль. Запрошена
  root-установка у пользователя. Дополнительно cleanup теперь завершает
  фоновые PID в конкретном named netns; в изолированном PoC `sleep` после
  выхода parent завершён, named netns исчез. 97 целевых тестов PASS.

- 2026-10-16: независимая проверка сохранённого старого JSON на globe после
  доставки кода: VERIFIED, hash OK, 14 подтверждённых PASS / 1 FAIL / 13
  SKIP; secret scan verifier без root снова `skipped_no_secrets_on_verifier_host`.
  Это прежний отчёт, не новая приёмка. §50.5 исправлен: direct проверяет
  отсутствие `tun0` внутри собственного netns, а не отсутствие любого host
  TUN device; verifier проверяет сырые строки. 56 целевых тестов PASS.

- 2026-10-16: пользователь выполнил root bootstrap на globe. Backend install
  завершился: host harness 19 PASS / 0 FAIL / 3 SKIP, Mihomo/egressd healthy,
  1081 TCP+UDP опубликованы на loopback и Docker gateway; installer скопировал
  pinned binary и включил host unit, старый Docker helper удалён. Новый SSH
  вход видит группу `vibelab-egress-tun`, но unit уходит в auto-restart до
  создания socket. По коду unit не хватало CAP_CHOWN для worker job-файлов,
  CAP_KILL для cleanup чужого UID и CAP_SETPCAP для сброса bounding set;
  они добавлены в ограниченный набор. Запрошен root journal для подтверждения
  точной ошибки; после обновления unit нужна live-приёмка.

- 2026-10-16: root journal globe подтвердил отказ `os.chown` для
  `/run/vibelab-egress/jobs` при старте helper: CAP_CHOWN действительно
  отсутствовал в установленном unit. Исправление `d9c98ca` доставлено в
  checkout globe, но установленный unit пока старый. В одноразовом Docker
  PoC с точным новым набором capabilities helper стартовал, создал netns,
  запустил `id` под UID/GID 65534 и очистил ресурсы (RC 0). Ожидается
  повтор `sudo bash scripts/install-tun-host.sh --user vibelab` на globe.

- 2026-10-16: после повторной установки unit на globe socket доступен
  `root:vibelab-egress-tun` 0660, `egressctl capabilities` сообщает
  `tun=supported`. Первый host-native live-прогон от UID 1082: §50.2 PASS
  (host cwd/файл и caller UID/GID); direct isolation, no-root-capabilities,
  cleanup, HARD DIRECT, no fallback — PASS. Общий результат 11 PASS / 1 FAIL /
  16 SKIP: host CLI не мог читать `/etc/vibelab-egress/client.token`
  (`root:root` 0600 после root install), из-за чего preflight и зависимые
  проверки дали SKIP; host harness упал на root-only persistent dir и был
  отмечен FAIL. Исправлен `install-client.sh`: при sudo host-установке
  клиентские token/auth остаются 0600, владельцем становится вызывающий
  host user; `/etc/vibelab-egress` остаётся root-owned 0750 с его группой.
  Root-only harness из non-root live получает честный SKIP. Offline 332 PASS;
  изолированный Docker тест подтвердил чтение token только host user.

- 2026-10-16: первый новый JSON `reports/host_acceptance_report.json` на globe
  независимо VERIFIED: 11 PASS / 1 FAIL / 16 SKIP, checks hash
  `d6009ef6f87458d33cf5958dd75cd59026eaa27ecf85a39aaa02ec60d8b37049`.
  §50.2 доказал caller UID/GID 1082:1083, host cwd и запись в host файл;
  §50.5 direct без tun0, §50.6 CapEff=0, §50.7 cleanup netns/veth,
  §50.8 HARD DIRECT и §50.11 no fallback — PASS. Это direct-only результат,
  proxy/UDP требуют provider routes. Настоящий OpenScience container работает
  под `runsc`; ручной запуск того же client module из смонтированного repo
  внутри него дал exit 6 и явный no-fallback. Формальный §50.9 остаётся SKIP,
  потому что бинарник `egress-run` в контейнере не установлен. Root-only
  verifier secret scan без sudo пропущен. Исправление host client credential
  ownership доставлено (`3f4e6fa`), ожидается его root-установка.

- 2026-10-16: после root переустановки host client token/auth стали
  `vibelab:root` 0600, каталог остался `root:vibelab` 0750; host
  `egressctl status` видит Mihomo v1.19.32. Новый live JSON независимо
  VERIFIED: 16 PASS / 1 FAIL / 11 SKIP, hash
  `62cbc2c890395d99f03bbd4fd8db16672db6317d9ed9508b6ca295bf8541d43e`,
  verifier secret scan выполнен. Единственный FAIL — §50.12:
  `egressctl` отсутствует в настоящем OpenScience `runsc` container
  (exit 127); §50.9 по той же причине SKIP. Ручной source-module вызов
  ранее подтвердил exit 6/no fallback, но формальную установку не заменяет.
  `docker cp` в работающий runsc сообщил успех, однако `/tmp` в sandbox
  не увидел каталог; проверенная альтернатива — tar stream через
  `docker exec -i` (несекретный probe появился). Ожидается root-доступ
  к client bundle для передачи его через stdin и установки в контейнере.

- 2026-10-16: OpenScience client `openscience-main` установлен в настоящем
  `runsc` контейнере через `tar | docker exec -i`; затем текущий package и
  installer из host checkout переданы тем же способом, потому что
  `/srv/workspace/vibelab-egress` внутри контейнера отставал от host.
  `egressctl status` видит backend, `egressctl capabilities` сообщает
  `unsupported_in_environment`, а `egress-run --mode tun direct -- true`
  завершается с кодом 6 без fallback. Временные каталоги внутри runsc удалены.
  Начальный `docker cp` оставил невидимую для runsc копию bundle в Docker
  overlay; оба файла с credentials в ней перезаписаны нулевой длиной и
  проверены обратным копированием. Остаток каталога без секретов исчезнет
  при пересоздании контейнера.

- 2026-10-16: новый host live acceptance на globe (`2026-10-16T16:30:40Z`):
  **19 PASS / 0 FAIL / 9 SKIP**, checks hash
  `1d527999d8c280d28676711212d117652958bf7b162d28441f0d67cad68152ca`.
  Независимый verifier на globe дал `VERIFIED`, hash OK, 19 подтверждённых
  PASS; secret scan выполнен. §50.2 доказал host UID/GID 1082:1083,
  исходный cwd и host-файл; §50.5–§50.12, включая фактический gVisor §50.9,
  прошли в доступных сценариях. Provider-зависимые TCP/UDP и параллельные
  проверки пропущены: сейчас доступен только direct route. §49.39 ждёт
  реального пересоздания контейнера; root-only `a8_host_test_report`
  пропущен в non-root запуске (отдельный root harness установки backend
  ранее дал 19 PASS / 0 FAIL / 3 SKIP). **A6 остаётся открытой** до live
  PASS provider-зависимых §50.1/§50.3/§50.4 и проверки оставшихся SKIP.
  Актуальные задачи по ролям — `handoff.md`.

- 2026-10-17: реализован host-side импорт proxy-источников. Новый
  `egress-providerctl` добавляет URL (скрытый ввод) или локальный файл 0600,
  показывает redacted-статус, обновляет и удаляет provider ID. Метаданные URL
  и исходные URI хранятся в `/etc/vibelab-egress/secrets/providers/`,
  нормализованные YAML — в persistent `providers/` (egressd RW, Mihomo RO).
  Исправлен путь file-provider в runtime config. Добавлены URI VMess/Trojan/
  Shadowsocks/Hysteria2/TUIC, base64 URI, сохранена поддержка Clash YAML,
  VLESS и WireGuard. URL refresh каждые 600 с; ручной `--via` использует
  выделенный one-shot SOCKS slot, не общий selector. Ошибка обновления
  оставляет предыдущий provider YAML и каталог; ответ и журнал содержат
  только фиксированный код ошибки. Оффлайн-свит: **345 passed** (uv Python
  3.13, отдельный временный state). Live на globe ещё не выполнен: доступный
  Docker относится к `vorontsov-x13`, здесь нет целевого backend/secrets и
  URL реальной подписки. A6 остаётся открытой до реального импорта и live
  acceptance с независимым verifier.

- 2026-10-17: добавлены личные профили для host UID и изолированные providers.
  Локальная enrollment-служба создаёт профиль по `SO_PEERCRED` при первом
  обращении без sudo и без home. Backend работает под выделенным служебным
  UID; TUN helper socket доступен всем локальным UID с проверкой peer UID.
  Каталог, default, проверки, benchmark, one-shot и обновления providers
  ограничены владельцем. `egressctl source add/list/refresh/remove` поддерживает
  личные источники. Host хранит исходник в backend; контейнер хранит его только
  в собственном каталоге и передаёт нормализованный снимок. Добавлены
  регенерация, hot reload и rollback личного provider. ТЗ расширено §52.
  Оффлайн-свит после изменений: **353 passed** (uv Python 3.13, временный
  state, включая install harness, Mihomo ACL-группы и запрет redirect на
  приватный адрес). Live на целевом host ещё требуется после deploy.

- 2026-10-17: после переноса на globe офлайн-свит показал 352 PASS / 1 FAIL:
  stubbed install harness подхватывал реальный `/etc/vibelab-egress` и UID
  службы из host `compose.env`. Тест теперь указывает временный каталог
  client bundle и фиксированный тестовый UID; live harness принимает новый
  `host-uid-<UID>` наряду с legacy `host-main` и поддерживает установку без
  legacy client-файлов через enrollment socket. Локальный офлайн-свит после
  исправления — **353 passed**; повторный прогон на globe ожидается.

- 2026-10-18: host acceptance на globe от 2026-10-17 дал **17 PASS / 2 FAIL /
  3 SKIP**. `host_whoami_identity` и `host_default_route_mode` не прошли,
  потому что Mihomo отклонял hot reload (HTTP 400): egressd передавал путь
  `/var/lib/egressd/generated/config.yaml` из своего контейнера, а Mihomo
  разрешает только путь внутри своего home. Для `PUT /configs` теперь
  передаётся `/root/.config/mihomo/generated/config.yaml`, указывающий на тот
  же общий файл в контейнере Mihomo. Добавлен regression test с проверкой
  compose mounts и reload path. Локальный офлайн-свит: **354 passed**;
  повторная live-проверка на globe ещё требуется.

- 2026-10-18: повторная установка на globe с `e0dfef0` выявила следующий
  runtime-дефект: Mihomo стал unhealthy, поскольку egressd успел перезаписать
  bootstrap-конфиг перед его пересозданием. Runtime-генератор использовал
  `MIHOMO_API_URL` (адрес подключения из egressd) как bind адрес самого
  Mihomo и оставлял SOCKS listeners на `127.0.0.1` внутри контейнера.
  Разделены адрес API подключения и bind адрес контроллера, в compose явно
  заданы `0.0.0.0:9090` и `0.0.0.0` для SOCKS; installer останавливает старый
  egressd до bootstrap-регенерации. Добавлены regression tests конфига и
  порядка установки. Локальный офлайн-свит: **355 passed**; live-повтор на
  globe ожидается.

- 2026-10-18: на globe два непривилегированных UID (`egress-smoke-a/b`)
  успешно получили разные `host-uid-*` профили; `doctor` для первого показал
  рабочие config, control plane, identity и proxy endpoint. Проба
  `source add --file /path/to/subscription` указала несуществующий примерный
  путь и завершилась traceback. CLI теперь сообщает `source file error`
  с кодом 2 для отсутствующего или нечитаемого файла; INSTALL.md уточняет
  необходимость фактического пути, доступного вызывающему UID. Клиентские
  тесты: **29 passed**. Live-проверка импорта реальной подписки ещё требуется.

- 2026-10-18: globe live: `egress-smoke-a` добавил личную URL-подписку
  `blanc` (62 узла). `source list` возвращает provider ID
  `p-8eaa1876e31d86fa826ead70-blanc`, а `egressctl list` показывает
  62 маршрута этого provider. У `egress-smoke-b` `source list` пуст.
  Попытки `list --provider` с route ID или коротким alias закономерно
  вернули пустой список: фильтр ожидает точный provider ID. Проверка
  фильтра с правильным ID и фактического соединения через маршрут ожидается.

- 2026-10-18: Xeovo URL-ссылка на globe возвращает 64 450 байт, но личный
  импорт отклонялся: в URI-списке несколько разных узлов имели одинаковую
  подпись `US-SLC-Hysteria2`. Для URI-подписок нормализатор теперь добавляет
  к повторяющимся именам стабильный суффикс на основе несекретных параметров
  узла; строгий запрет коллизий в Clash YAML сохранён. Тест покрывает
  перестановку URI и отсутствие credentials в именах. Локальный офлайн-свит:
  **357 passed**; live-повтор импорта Xeovo на globe ещё требуется.

- 2026-10-18: установка на globe с `a2aa7a3` завершилась успешно: backend
  healthy, host acceptance **19 PASS / 0 FAIL / 3 SKIP**. Повторный импорт
  Xeovo всё ещё дал общий HTTP 400. Причина после пересборки пока не известна:
  формат подписки и этап отказа запрошены отдельной диагностикой без вывода
  URL и ключей. Нормализатор расширен: одинаковые подписи разных узлов теперь
  получают уникальные стабильные имена для всех форматов, в том числе Clash
  YAML; итоговый provider не содержит коллизий. Оффлайн-свит **357 passed**.
  Live-импорт Xeovo остаётся открытым до результата диагностики и повторной
  проверки на globe.

- 2026-10-18: диагностика обновлённого backend показала: Xeovo URL
  распознаётся как `uris`, нормализуется в **317 узлов** и отклоняется только
  на верхнем лимите 300 в `personal.validate_yaml`. По требованию владельца
  верхний предел числа узлов удалён; остаются проверка непустого списка,
  публичных адресов и ограничение исходника/снимка в 4 MiB. §52 ТЗ
  синхронизирован, добавлен тест на 317 узлов. Локальный офлайн-свит:
  **359 passed**. Live-импорт после обновления backend ещё требуется.

- 2026-10-18: следующая диагностика Xeovo после снятия лимита: из 317
  нормализованных узлов 20 имеют публичный IP, 285 — разрешимое публичное
  DNS-имя, 12 — два DNS-имени без пригодного публичного ответа. Метка
  `bad types` в диагностике обозначает типы этих 12 узлов, а не неподдерживаемые
  протоколы. Импорт теперь пропускает только узлы без проверенного публичного
  адреса, сохраняет остальные и возвращает `node_count` и `skipped_nodes` через
  API/CLI. При нуле пригодных узлов импорт отклоняется; закрепление публичного
  IP, ограничения размера и откат reload сохранены. Оффлайн-свит:
  **360 passed**. На globe нужно обновить backend и клиент, затем повторить
  импорт Xeovo; ожидается около 305 сохранённых узлов при прежнем DNS-ответе.

- 2026-10-18: на globe первый `source add xeovo` завершился клиентским
  таймаутом, а повторный вызов получил 409: backend уже сохранил provider.
  Диагностический скрипт ожидал два значения от `validate_yaml`, который
  теперь возвращает три (`yaml`, число сохранённых и число пропущенных).
  Валидация кэширует результат проверки каждого серверного имени на время
  одного импорта, поэтому повторяющиеся DNS-имена не проверяются сотни раз.
  Таймаут CLI для `source add/refresh` увеличен до 300 секунд; при таймауте
  CLI сообщает о неизвестном результате и предлагает проверить `source list`.
  Ответ 409 для существующего provider теперь предлагает `source refresh`.
  Локальный офлайн-свит: **361 passed**. После обновления на globe требуется
  проверить сохранённый `xeovo` через `source list`, без повторного `add`.

- 2026-10-18: повторный `source add` с теми же alias, видом и исходным URL
  или содержимым файла теперь идемпотентен. Host API возвращает успех с
  `skipped: true`, не загружает URL повторно и не регенерирует конфигурацию;
  другой исходник под занятым alias остаётся конфликтом 409. Контейнерный
  клиент пропускает повтор только если совпадает локальный исходник и provider
  уже присутствует в backend; при отсутствии backend-снимка он загружает его
  заново. CLI выводит `source skipped`. Обновлены §52 ТЗ и INSTALL.md;
  локальный офлайн-свит: **362 passed**. Live-проверка на globe ещё требуется.

- 2026-10-18: для одного прокси без файла добавлен `egressctl source add
  <alias> --uri '<proxy URI>'`; `--uri -` читает одну ссылку из stdin.
  Поддерживаются одиночные VLESS, VMess, Trojan, Shadowsocks, Hysteria2 и
  TUIC URI существующего адаптера. Host хранит URI как личный source;
  контейнер хранит его только локально и передаёт нормализованный снимок.
  Повтор идентичного URI идемпотентен. Многострочный вход с `--uri`
  отклоняется. Обновлены §52 ТЗ и INSTALL.md; локальный офлайн-свит:
  **363 passed**. Live-проверка на globe ещё требуется.

- 2026-10-18: на globe одиночный VLESS Reality URI успешно добавился, но
  `egressctl test` вернул `healthy: false`, а `egress-run ... curl` не смог
  установить TLS-соединение. В адаптере найдено расхождение с конфигурацией
  Mihomo: `fp` попадал внутрь `reality-opts`, `sid` терялся, SNI находился
  в неверном поле, а `spider-x` подставлялся фиксированным значением.
  Теперь `fp` становится верхнеуровневым `client-fingerprint`, SNI —
  `servername`, `pbk`/`sid` — `reality-opts.public-key`/`short-id`.
  Исправлены оба пути разбора VLESS (одиночный и смешанный список URI).
  Host `source refresh` теперь пересоздаёт и сохранённый URI, поэтому
  удалять личный provider не требуется. Тесты проверяют Reality-поля,
  закрепление публичного IP и refresh. Локальный офлайн-свит:
  **365 passed**. Реальную доступность узла нужно проверить на globe.

- 2026-10-18: globe live после `source refresh nochlezhka`: `egressctl test`
  остаётся `healthy: false`, `egress-run` через узел даёт TLS-ошибку,
  Mihomo сообщает `REALITY authentication failed`; прямой `egressctl test
  direct` успешен (84 мс). Безопасная сверка сохранённой URI и provider YAML
  показала совпадение UUID, `pbk`, `sid`, SNI и fingerprint (5/5), поэтому
  текущие данные не указывают на ошибку преобразования этих полей.
  Документация Mihomo предупреждает о несовместимости Reality с Xray-core
  26.7.11+; версия серверного Xray пока неизвестна. Следующий шаг —
  подтвердить её у провайдера или проверить ту же ссылку Xray-клиентом на
  той же машине. Если Xray-клиент работает, а Mihomo нет, потребуется
  совместимый серверный профиль или другой поддерживаемый endpoint.

- 2026-10-18: пользователь подтвердил, что та же URI работает через
  v2rayN с Xray-core 26.10.10. С учётом совпадения 5/5 параметров в
  provider YAML и ошибки `REALITY authentication failed` в Mihomo это
  подтверждает несовместимость данного endpoint с текущим движком, а не
  непригодность ссылки. Стек закреплён на Mihomo v1.19.32; нужен
  совместимый серверный профиль от провайдера или отдельная интеграция
  Xray с собственными ограничениями доступа и жизненного цикла.
  Повторный refresh здесь не поможет.

- 2026-10-18: массовая проверка личных подписок на globe ошибочно показала
  0 рабочих маршрутов: каталог egressd содержал 405 узлов, но запросы
  `/proxies/<узел>/delay` к Mihomo быстро возвращали 404. Узлы файловых
  провайдеров находятся в отдельном API `/providers/proxies`; `egressctl test`
  теперь обращается к `/providers/proxies/<провайдер>/<узел>/healthcheck`.
  В неуспешном результате добавлен HTTP-статус Mihomo для диагностики.
  Регрессионный тест проверяет endpoint и mapping route ID в provider/node.
  Локальный офлайн-свит: **366 passed**. Live-проверка рабочих узлов на globe
  требуется после обновления backend.

- 2026-10-18: на globe тест Xeovo Shadowsocks дал `healthy=true`, 545 мс;
  массовая повторная проверка Blanc показала рабочие узлы, но `egressctl list`
  выводил `delay=?ms` при `healthy=True`. Статус сохранялся; CLI ошибочно читал
  `delay_ms` вместо фактического поля API `latency_ms`. Исправлено отображение.
  Также исправлен расчёт свежести в `best`: каталог хранит `last_test_at` как
  ISO UTC-строку, тогда как ранжирование ожидало Unix timestamp и могло
  аварийно завершиться после проверки узла. Добавлены проверки отображения,
  строкового времени и вызова `best` после теста; офлайн-свит **366 passed**.
  Live-проверка обновлённого клиента и backend на globe требуется.

- 2026-10-18: host TUN helper на globe сообщил `tun=supported`, но запуск
  непрямого маршрута завершился `netns_setup_failed [Errno 1] Operation not
  permitted` для per-run `mihomo/config.yaml`. Причина: helper сначала передавал
  файл worker-пользователю, затем вызывал `chmod` без `CAP_FOWNER` в systemd
  unit. Порядок изменён: `chmod 0600` выполняется пока файл принадлежит root,
  затем `chown` передаёт его worker. Набор capabilities не расширялся.
  Регрессионный тест моделирует отсутствие `CAP_FOWNER`; офлайн-свит
  **367 passed**. На globe требуется обновить только TUN helper через
  `scripts/install-tun-host.sh` и повторить live TUN-команду.

- 2026-10-18: после запуска host TUN задания разрешение доменных имён в нём
  завершалось `curl: (6) Could not resolve host`. `resolv.conf` задания указывает
  на `1.1.1.1`, а его Mihomo перехватывал DNS-пакеты без включённого внутреннего
  DNS-резолвера. Per-run конфиг теперь включает DNS и отправляет DoH-запросы
  через выбранный one-shot прокси; перехватываются UDP и TCP на порту 53.
  Регрессионный тест проверяет эти параметры, офлайн-свит **367 passed**.
  Требуется live-проверка на globe после переустановки TUN helper.
