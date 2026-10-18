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
  providers/     # normalized provider YAML; egressd writes, Mihomo reads
  generated/     # generated mihomo config + docker-cidrs.yaml
  egressd/       # metrics.sqlite, session state
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
providers/             # source metadata (.json), raw static sources (.source)
clients/               # <id>.token, <id>.profile.json, <id>.proxy-creds
```

Каждый источник добавляется через host-only `egress-providerctl`; один ID
соответствует одному набору маршрутов. Метаданные и исходный файл живут
только в secrets, а нормализованный YAML создаёт egressd в persistent root.

```bash
sudo egress-providerctl add --id myprovider --subscription-url
# URL вводится скрыто; схема по умолчанию HTTPS, явный HTTP допустим.
sudo egress-providerctl add --id custom --source-file /path/to/links.txt
# Исходный файл должен иметь режим 0600.
sudo egress-providerctl list
sudo egress-providerctl update --id myprovider
sudo egress-providerctl update --id custom --source-file /path/to/new-links.txt
sudo egress-providerctl remove --id custom
```

Поддерживаются Clash/Mihomo YAML, base64-подписки, URI VLESS, VMess,
Trojan, Shadowsocks, Hysteria2, TUIC и WireGuard `.conf`. URL-источники
обновляются каждые 10 минут. Ошибка загрузки или разбора сохраняет
предыдущие маршруты. Ручное обновление через выбранный маршрут:
`egressctl update --provider myprovider --via otherprovider:route`.
Проверка: `egressctl providers`, `egressctl list`, `egressctl list --healthy`,
`egressctl test myprovider:route`.

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

Host client, установленный через `sudo` от имени `vibelab`, получает
доступ к своим `client.token` и `default-proxy.auth`: файлы остаются 0600,
владельцем становится `vibelab`; каталог `/etc/vibelab-egress` остаётся
root-owned (0750). Backend secrets в подкаталоге `secrets/` сохраняют
собственные ограничения доступа.

### Установка и обновление host-native TUN-helper

TUN-helper запускается отдельным host systemd unit. Он сохраняет host mount
namespace для workload, передаёт текущий каталог и окружение клиента, а сетевой
доступ изолирует в per-run netns. Контейнерный helper удалён: файловая система
его контейнера не является файловой системой команды, вызванной на host.

На Docker host после установки backend:

```bash
sudo bash scripts/install-tun-host.sh --user vibelab
systemctl status vibelab-egress-tun-helper.service
```

Скрипт ставит pinned Mihomo v1.19.32 из Docker image с проверкой SHA256,
создаёт отдельного непривилегированного worker и локальную группу доступа к
`/run/vibelab-egress/tun-helper.sock`. После первого добавления пользователя в
группу нужна новая login-сессия. Повторный запуск скрипта обновляет код и unit.
`egress-run` внутри OpenScience/gVisor по-прежнему не получает этот сокет.

Перед включением helper нужен host с `/dev/net/tun`, `ip`, `nft`, `iptables`,
`setpriv` и systemd. Unit ограничен набором CAP_SYS_ADMIN, CAP_NET_ADMIN,
CAP_SETUID, CAP_SETGID: первый нужен для `ip netns add`; перед exec workload
все capabilities сбрасываются. Helper слушает только UNIX socket, не получает
Docker socket. Проверка после установки:

```bash
egressctl capabilities
bash scripts/run-live-acceptance.sh
sudo python3 scripts/verify-live-report.py reports/host_acceptance_report.json
```

Секреты и generated config в отчёт и git не переносить.

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
`docker cp` и повторная установка клиента эту запись не добавляют. Проверить
существующий контейнер можно командой
`docker exec <container> python3 -c "import socket; print(socket.gethostbyname('host.docker.internal'))"`; при отсутствии
записи пересоздать контейнер с указанным `--add-host`/`extra_hosts`. Если
пересоздание пока невозможно, можно заново подготовить bundle с явным
`--control-url http://<docker-gateway>:9091 --proxy-host <docker-gateway>`
и установить его; это ротирует клиентские секреты и требует обычной
регенерации backend-конфига.

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

В работающем OpenScience под `runsc` проверен случай, когда `docker cp`
сообщил об успехе, но файлы в `/tmp` не появились внутри контейнера.
Передавайте bundle через stdin контейнера и проверяйте его там до установки:

```bash
sudo tar -C <root>/client-bundles/<id> -cf - . |
  docker exec -i <container> sh -c \
  'mkdir -p /tmp/vibelab-egress-client && tar -C /tmp/vibelab-egress-client -xf -'
docker exec <container> test -s /tmp/vibelab-egress-client/client.token
docker exec <container> bash /srv/workspace/vibelab-egress/scripts/install-client.sh \
  --client <id> --target gvisor-container --bundle /tmp/vibelab-egress-client
docker exec <container> rm -rf /tmp/vibelab-egress-client
```

Путь к `install-client.sh` должен указывать на актуальную версию checkout
в контейнере: смонтированный `/srv/workspace` может отставать от host.

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

## Личные профили и источники (§52)

После установки backend на host локальному пользователю не требуется
`provision-client.sh` или sudo: первый вызов `egressctl whoami` обращается к
`/run/vibelab-egress-enroll/enroll.sock`. Служба определяет UID по Unix socket,
создаёт профиль `host-uid-<uid>` и возвращает учётные данные в память клиента.
Работает и для системных UID без home. Проверка от имени такого пользователя:

```bash
egressctl whoami
egressctl source add personal --url 'https://public.example/subscription'
egressctl source list
egressctl list --provider <provider_id>
egressctl source refresh personal
egressctl source remove personal
```

Один прокси добавляется напрямую по URI, без файла подписки:

```bash
egressctl source add single --uri 'vless://fake-uuid@proxy.example:443?security=tls#single'
egressctl source list
```

Для ввода URI из stdin без его записи в командной строке используйте
`egressctl source add single --uri -`, вставьте одну полную ссылку и нажмите
Enter. Поддерживаются `vless://`, `vmess://`, `trojan://`, `ss://`,
`hysteria2://`/`hy2://` и `tuic://`; один вызов `--uri` принимает один узел.
После обновления backend используйте `egressctl source refresh single`, чтобы
пересоздать уже добавленный URI по новой версии адаптера.

Повторный `source add` с тем же именем и тем же URL, URI или содержимым файла
завершается как `source skipped`: существующий источник не загружается и
конфигурация не меняется. Чтобы получить изменившееся содержимое URL,
используйте `egressctl source refresh personal`.

Для файла вместо `--url` используется `--file /фактический/путь/к/файлу`:
файл должен существовать и читаться тем пользователем, от имени которого
выполняется `egressctl`. Личные
маршруты видны только владельцу; глобальные providers доступны всем.

В контейнере `egressctl source add` хранит исходный URL или содержимое файла
внутри контейнера: `${VIBELAB_EGRESS_SOURCE_DIR}` либо
`${XDG_STATE_HOME}/vibelab-egress/sources` либо
`~/.local/state/vibelab-egress/sources`, 0700/0600. Для `source add/refresh`
нужен PyYAML в контейнере (`python3 -m pip install PyYAML` при сборке image).
Установщик копирует optional adapters рядом с stdlib-only клиентом.
Контейнер отправляет backend только нормализованный YAML, поэтому его сырой
URL/файл не доступен и не управляется со стороны host. Каталог источников
нужно включить в persistent volume или image, если они должны переживать
пересоздание контейнера. Обновление контейнерного URL запускается внутри
контейнера командой `egressctl source refresh <name>` (например, таймером).
