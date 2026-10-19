# HANDOFF — install logging + self-explaining tests (work order)

Статус: work order для developer-агента. Реализовать фиксы 1–6 строго по
порядку, каждый фикс — отдельный git-коммит. Критерии приёмки — в каждом
фиксе и в разделе «Definition of done».

## Контекст (почему это нужно)

Установка backend на хосте (`scripts/install-backend-host.sh`) несколько раз
падала с ошибками, которые было трудно диагностировать:

1. **Нет лога установки.** Скрипт печатает короткие `say()`-строки в stdout,
   не пишет файл, нет timestamps, нет номеров шагов, нет ERR-trap: при падении
   `docker compose build|up` (через `set -e`) видно только голый stderr docker
   без указания, какой именно шаг упал.
2. **Health gate ненадёжен.** Факт (реальный прогон 2026-10-12, см.
   `reports/install-backend-host-report.log`): `docker compose ps` показывал
   `vibelab-egress-egressd … Restarting (1) 19 seconds ago`, но health gate
   (`install-backend-host.sh`, функция `all_healthy`) пропустил установку —
   однократное чтение `.State.Health.Status`/`.State.Status` у
   restarting-контейнера может вернуть устаревшее `healthy`/`running`.
   Установка «успешно» дошла до harness, который упал на
   `host_whoami_identity: Connection refused` (exit 4) — без объяснения,
   что egressd crash-loop'ит.
3. **Harness не объясняет причины.** `host/egress-host-test` при
   `Connection refused` пишет только `rc=1: … Connection refused` — без
   состояния контейнеров, без `docker logs`, без «что делать дальше».
   Отдельные классы сбоев не проверяются вовсе: установленный ли client
   package импортируется, есть ли `egressctl` в PATH, сгенерирован ли
   bootstrap-конфиг, кому принадлежат backend-секреты.
4. **Оффлайн-тесты install-скриптов не закоммичены.** A1-suite
   (`/tmp/a1_test.sh`, 47/47) жила в /tmp и потерялась; install-скрипты
   сейчас не покрыты воспроизводимыми тестами.

## Цель и non-goals

**Цель:** после установки (или её падения) администратор и AI-агент по одному
файлу лога + JSON-репорту harness понимают, какой шаг/чек сломался, почему и
какую команду выполнить дальше. Оффлайн-свит покрывает install-скрипты
stubbed-прогоном (без Docker, без root).

**Non-goals:** никаких изменений data plane (egressd, mihomo, compose-файлы,
client package), никаких новых зависимостей (host-инструменты — stdlib-only),
никаких изменений live-acceptance (`host/live-acceptance.py`), никакого
re-architecture install-потока (те же 10 шагов, те же flags + 2 новых
testability-флага).

## Обязательные инварианты (AGENTS.md, TECH_SPEC §33–38)

- Секреты: в логах и отчётах — только пути + sha256[:8], никогда значения.
  Если в evidence попадают `docker logs` — значения известных секретов
  (mihomo_api_secret, client tokens) должны быть замазаны.
- Host-инструменты: Python stdlib-only, фиксированный argv, без
  `shell=True`/`os.system`/`eval`/`exec` (A8 security gate это проверяет —
  54/54 должно остаться 54/54).
- Client package — stdlib-only (не трогаем).
- `poc/` и `reports/` не удалять. Язык документации — русский; код/комментарии
  — английский.
- STATUS.md обновляется после каждого завершённого шага (журнал в конце).

## Git workflow

- Репозиторий: корень `/srv/workspace` (git root), ветка `main`,
  единственный коммит `0640911`. Remote нет — push не нужен.
- Создать ветку `install-logging` от `main`. Каждый фикс — отдельный коммит
  (7 коммитов: handoff + 6 фиксов). Merge в `main` делает lead, НЕ агент.
- В рабочем дереве есть чужое изменение
  `vibelab-egress/reports/host_acceptance_report.json` — его НЕ трогать и НЕ
  коммитить (оставить dirty).
- Commit message: `<scope>: <что> (<почему/доказательство>)`, например
  `install: structured step logging with ERR trap (log file + step markers)`.
  Не коммитить ничего, кроме файлов своего фикса.

## Окружение (где выполняется работа)

- Контейнер (container runtime), работаем **root** (`id -u` = 0).
- **Нет** Docker daemon, `systemctl`, `ufw`. Есть `findmnt`, `df`,
  `python3` 3.11.17, `pytest` 9.1.1, git 2.43.
- `/etc/vibelab-egress` и `/usr/local/lib/vibelab-egress` в контейнере
  отсутствуют.
- Базовый оффлайн-свит: `cd vibelab-egress && python3 -m pytest tests/
  client/tests/ -q` → **309 passed** (2026-10-13). После каждого коммита
  прогонять; регрессий быть не должно.

## Фикс 1 — Structured install logging
Файл: `vibelab-egress/scripts/install-backend-host.sh`

1. Хелпер `log()`: строка вида
   `[YYYY-mm-ddTHH:MM:SSZ] [step N/10] <message>` (UTC, `date -u +%FT%TZ`),
   печатается в stdout И дописывается (append, `>>`) в
   `$LOG_FILE="$ROOT/install-backend-host.log"`.
2. `LOG_FILE` определяется после парсинга аргументов, но лог-файл создаётся
   ДО шага 2 (persistent root): mkdir -p "$(dirname "$LOG_FILE")" (в
   test-режиме это tmpdir — см. фикс 4). До создания лог-файла `log()`
   печатает только в stdout (graceful).
3. Run header в начало лога: hostname, kernel (`uname -r`), user, docker
   version, docker compose version, python3 version, git rev репо
   (`git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown`), все
   аргументы запуска (без значений секретов — аргументы скрипта их не
   содержат, но `--secrets-dir` печатать как путь).
4. Заменить все `say` на `log` с номером текущего шага (шаги 1–10 как в
   шапке скрипта; шаг 1 = prerequisites). Текущий шаг держать в переменной
   `STEP`/`STEP_NAME` и обновлять на каждом маркере.
5. `trap 'on_error' ERR`: `on_error` логирует (в лог и stderr)
   `step=$STEP ($STEP_NAME)`, `BASH_COMMAND`, exit code (`$?`), и строку
   `full log: $LOG_FILE`. Задокументированные exit codes (0/2/3/4)
   сохраняются: `die_usage`/`die_env` вызывают `exit` явно — trap на них не
   влияет (или влияет, но не меняет код: проверить).
6. Вывод subprocess'ов, которые сейчас теряются или идут мимо лога,
   тупить в лог: `docker compose build`, `docker compose up -d`,
   `docker compose ps`, `python3 "$HOSTCTL" sync-docker-cidrs`,
   `python3 -m egressd.bootstrap_config`, harness `egress-host-test`
   (его JSON-репорт целиком в лог). Реализация: `run_logged <label> <cmd...>`
   — `"$@" 2>&1 | tee -a "$LOG_FILE"` с сохранением exit code
   (`PIPESTATUS[0]`); при ненулевом коде — `on_error`-подобная запись и
   прерывание (для обязательных команд).
7. Запрещено: печатать значения секретов; менять порядок шагов; менять
   flags/exit codes.

**Критерии приёмки фикса 1:**
- `bash -n` OK.
- Стуб-прогон (см. фикс 4, но уже сейчас вручную): success-путь создаёт
  `$ROOT/install-backend-host.log` с run header и маркерами всех 10 шагов.
- Инжект-сбой (stub `docker compose up` → rc 1): exit 3, в логе есть
  `step 9`, команда `docker compose up -d`, `full log:` строка.
- 309 passed.

## Фикс 2 — Robust health gate
Файл: `vibelab-egress/scripts/install-backend-host.sh` (блок шага 9)

1. `all_healthy` ужесточить: для КАЖДОГО сервиса из `SERVICES` требовать
   `.State.Status == running` И `.State.Health.Status == healthy` И
   `.RestartCount == 0` (docker inspect --format).
2. Устойчивость к устаревшему чтению (реальный баг 2026-10-12): переход в
   «healthy» разрешить только после **3 последовательных** успешных
   `all_healthy` (poll каждые 2 с, т.е. ~6 с settle). Счётчик сбрасывается
   при любом провале.
3. Timeout (90 с): для каждого не-healthy сервиса в лог выгружать
   `docker logs --tail 100 <svc>` (с redaction — см. ниже) и ключевые поля
   `docker inspect` (State.Status, Health.Status, RestartCount,
   State.ExitCode, State.Error, OOMKilled), затем `die_env` с указанием
   `full log: $LOG_FILE`.
4. Redaction: значения секретов (`$SECRETS_DIR/mihomo_api_secret`,
   `$SECRETS_DIR/clients/*.token`, `*.proxy-creds`) не должны попасть в лог.
   Реализовать `redact_stream()`: прочитать значения (root может),
   `sed -e "s/$val/[REDACTED]/g"` по каждому значению (значения —
   urlsafe-токены без спецсимволов sed; если значение пустое — пропустить).
   Применять ко всем `docker logs`-дампам.

**Критерии приёмки фикса 2:**
- `bash -n` OK.
- Стуб-прогон «restarting» (stub `docker inspect` возвращает
  `RestartCount=1` для egressd, health=healthy): установка НЕ проходит
  health gate, exit 3, в логе — дамп логов egressd.
- Стуб-прогон «все healthy c первого полла»: gate проходит (3 полла).
- 309 passed.

## Фикс 3 — Self-explaining harness
Файл: `vibelab-egress/host/egress-host-test`

1. Новые чексы (stdlib-only, фиксированный argv, секреты в отчёт не
   попадают; все — live-only, в `--static` режиме → skipped):
   - `egressd_container_healthy`: `docker inspect` обоих сервисов:
     running + healthy + RestartCount == 0. Fail → detail с состоянием
     каждого сервиса.
   - `client_package_importable`: `python3 -c "import
     vibelab_egress_client.egressctl"` с `sys.path`-вставкой
     `/usr/local/lib/vibelab-egress` (проверяет ровно тот баг 2026-10-12,
     когда package копировался сплющенным). Fail → detail: «installed
     package layout broken; re-run scripts/install-client.sh --target
     host».
   - `egressctl_on_path`: `egressctl --help` rc==0. Fail → detail:
     «egressctl not on PATH; check /usr/local/bin wrappers».
   - `generated_config_present`: `$ROOT/generated/config.yaml` существует,
     непуст, парсится встроенным YAML-парсером harness'а, содержит
     `mixed-port`/`socks-port` (top-level ключи). Fail → detail:
     «bootstrap config missing; re-run install step 8 or check
     egressd logs».
   - `secrets_readable_by_container_user`: owner uid всех файлов
     `$SECRETS_DIR` (рекурсивно) == 1000 (uid egressd). Skip, если uid 1000
     не существует на хосте (`id -u 1000` rc!=0). Fail → detail: «egressd
     (uid 1000) cannot read backend secrets; re-run install (step 8b)».
2. Diagnosis-хелпер `collect_stack_diagnosis()`: при live-режиме, если
   ЛЮБОЙ из live-чексов упал, один раз собрать: `docker compose ps`
   (текст), `docker logs --tail 50` каждого сервиса (redact по значениям
   секретов из `$SECRETS_DIR`, если читаются), `docker inspect`
   State/Health/RestartCount каждого сервиса. Результат кладётся в
   `evidence` упавших live-чексов (или в общий `diagnosis`-блок отчёта —
   выбрать одно, задокументировать в docstring) и в `detail` каждого
   упавшего live-чека добавляется actionable-строка вида
   `next: docker logs vibelab-egress-egressd`.
3. Существующие 17 чексов — без изменений в семантике (pass/fail/skip
   условия не менять). Схема отчёта расширяется только новыми чексами
   (additive).
4. Инварианты: stdlib-only, фиксированный argv, без `shell=True` (A8 gate),
   секреты не в отчёт (значения — только redact-маркер/путь+sha8).

**Критерии приёмки фикса 3:**
- `py_compile` OK; `bash -n` не нужен (python).
- `--static` прогон в этом контейнере: все новые чексы → skipped,
   существующие — как раньше (сравнить с текущим выводом: те же
   pass/fail/skip по старым id).
- Stubbed live-прогон (stub `docker` в PATH, см. фикс 4): при
  «egressd restarting» новый чек `egressd_container_healthy` → fail с
  состоянием в detail + diagnosis в evidence; при «всё healthy» → pass.
- 309 passed (включая security gate).

## Фикс 4 — Committed offline install test suite
Файлы: `vibelab-egress/tests/install/` (новый каталог),
`vibelab-egress/scripts/install-backend-host.sh` (test-mode escape)

1. `tests/install/stubs/` — фиксированные stub-скрипты (bash, фиксированное
   поведение, управляемое env-переменными):
   - `docker`: поддерживает `compose version`, `compose build`,
     `compose up -d`, `compose ps`, `inspect --format`, `logs --tail`,
     `network ls --format`, `network inspect --format`. Поведение:
     `STUB_DOCKER_UP_RC` (default 0), `STUB_DOCKER_HEALTH`
     (`healthy|restarting|never`), `STUB_DOCKER_LOGS` (файл с логами).
     `inspect --format` возвращает последовательность значений по
     `{{...}}` в формате: Status, Health.Status, RestartCount (в порядке,
     как запрашивает install-скрипт).
   - `ufw`, `systemctl`, `findmnt`, `df` — no-op с правдоподобным выводом.
   - `egressctl` — `--help` → rc 0; `--json whoami` → JSON
     `{"client_id": "host-main"}`; `--json route current` /
     `--json mode current` → JSON.
2. `tests/install/test_install_scripts.sh` (bash, запускается pytest'ом
   через `tests/install/test_install_scripts.py` — тонкая pytest-обёртка,
   чтобы свит входил в `python3 -m pytest tests/ -q`):
   - `bash -n` всех `scripts/*.sh`; `py_compile` всех `host/*.py`-утилит.
   - **Success-путь:** tmpdir-`--root`, tmpdir-`--secrets-dir`,
     `VIBELAB_EGRESS_TEST_MODE=1`, `PATH=stubs:$PATH`, `--yes
     --skip-firewall`. Assert: rc 0; лог-файл существует; в логе run header
     (hostname, docker version) и маркеры всех 10 шагов;
     `generated/config.yaml` создан; секреты созданы (600).
   - **Fail-путь A (up падает):** `STUB_DOCKER_UP_RC=1` → rc 3; в логе
     `step 9`, `docker compose up -d`, `full log:`.
   - **Fail-путь B (never healthy):** `STUB_DOCKER_HEALTH=never` → rc 3;
     в логе дамп `docker logs` egressd + `RestartCount`.
   - **Fail-путь C (restarting):** `STUB_DOCKER_HEALTH=restarting` →
     rc 3 (health gate не проходит).
   - **Negative-harness (портировать из утраченного /tmp/a1_test.sh):**
     tmp compose.yaml с `0.0.0.0:1080` → `no_public_binds=fail`;
     compose с docker.sock mount → `docker_socket_not_mounted=fail`;
     provider-секрет в client bundle →
     `no_backend_secrets_in_client_bundles=fail`. Запускать
     `host/egress-host-test --static` с `VIBELAB_EGRESS_REPO`/`VIBELAB_EGRESS_ROOT`
     на tmp-дерево.
3. Test-mode escape в `install-backend-host.sh`:
   `[ "${VIBELAB_EGRESS_TEST_MODE:-0}" = "1" ] || [ "$(id -u)" = "0" ] ||
   die_env "must run as root (host bootstrap operation)"`. Документировать
   в шапке скрипта: `VIBELAB_EGRESS_TEST_MODE=1` — только для
   stubbed-тестов, в production не используется.
4. `tests/install/README.md` (русский): как запустить, что покрывается,
   как расширять.

**Критерии приёмки фикса 4:**
- `python3 -m pytest tests/install/ -q` → все pass в этом контейнере
  (без Docker, root).
- Полный свит `python3 -m pytest tests/ client/tests/ -q` → 309 + новые
  passed, 0 failed.
- Без `VIBELAB_EGRESS_TEST_MODE` и без root скрипт по-прежнему требует
  root (проверить: `setpriv --reuid 65534 --regid 65534 --clear-groups
  bash scripts/install-backend-host.sh --help` — help работает, а
  запуск без --help без root → exit 3).

## Фикс 5 — verify-backend-host.sh parity
Файл: `vibelab-egress/scripts/verify-backend-host.sh`

1. Health-проверка: вместо `grep -q '"healthy"'` (любой сервис) —
   per-service: для `vibelab-egress-mihomo` и `vibelab-egress-egressd`
   отдельно `docker inspect` Status==running, Health.Status==healthy,
   RestartCount==0; каждый — отдельный pass/fail.
2. Лог: весь вывод скрипта дублировать в
   `$ROOT/verify-backend-host.log` (append, те же `pass/fail/skip`
   хелперы пишут в оба места). Run header как в install-логе.
3. Semantics не менять: exit 0/2/3, счётчик FAILURES, те же 7 секций.

**Критерии приёмки фикса 5:** `bash -n` OK; 309 passed. (Live-проверка —
только на хосте, здесь недоступна.)

## Фикс 6 — Docs
Файлы: `vibelab-egress/INSTALL.md`, `vibelab-egress/STATUS.md`

1. INSTALL.md: новый раздел «Логи и диагностика установки» (русский):
   - где лежит лог (`<root>/install-backend-host.log`), формат строк
     (timestamp, step N/10), что в run header;
   - таблица exit codes (0/2/3/4) + что делать при каждом (3 → читать
     лог, `docker logs <svc>`, исправить, повторить — установка
     идемпотентна: секреты сохраняются, каталоги переиспользуются);
   - где лог verify (`<root>/verify-backend-host.log`).
2. STATUS.md: в «Текущий шаг / Next» добавить строку про install-logging
   (ветка, 7 коммитов, оффлайн-свит), в «Журнал решений» — запись
   2026-10-14: что сделано, почему (баг health gate 2026-10-12 —
   restarting-контейнер прошёл gate), как проверено (stubbed-свит,
   309+N passed).

**Критерии приёмки фикса 6:** файлы читаются, ссылки на пути точные;
309 passed.

## Definition of done (для developer-агента)

- 7 коммитов на `install-logging` (handoff-коммит делает lead ДО запуска
  агента — агенту 6 фиксов).
- После каждого коммита: `bash -n` затронутых .sh, `py_compile` затронутых
  .py, `python3 -m pytest tests/ client/tests/ -q` без регрессий.
- Финально: `python3 -m pytest tests/ -q` (включая новый tests/install/)
  все pass; `python3 -m pytest tests/security/ -q` → 54 passed.
- Рабочее дерево чистое, кроме чужого dirty
  `vibelab-egress/reports/host_acceptance_report.json` (не трогать).
- Если критерий приёмки какого-то фикса недостижим — НЕ импровизировать:
  остановить работу, записать в `vibelab-egress/HANDOFF-issues.md` (новый
  файл) что именно не получилось и почему, и завершить с отчётом.

## Команды проверки (копипаст)

```bash
cd /srv/workspace/vibelab-egress
python3 -m pytest tests/ client/tests/ -q          # базовый свит
python3 -m pytest tests/install/ -q                # новый install-свит
python3 -m pytest tests/security/ -q               # A8 gate (54)
bash -n scripts/*.sh
python3 -m py_compile host/egress-host-test host/egress-hostctl
```
