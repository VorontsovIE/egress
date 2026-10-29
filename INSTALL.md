# INSTALL — vibelab-egress

Два **независимых** шага (spec section 32):

- **A. Backend deployment** — один раз на Docker host (root).
- **B. Client installation** — в каждую execution environment (host,
  Docker container, другие контейнеры/VM) независимо.

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
- Для TUN в клиентских bundle заранее собрать static engine на доверенной
  build-машине: `scripts/build-tun-engine.sh` (стабильный Go 1.26.3 или новее; результат в
  `client/tun-engine/dist/`).

### Persistent paths

Persistent root (по умолчанию `/var/lib/vibelab-egress`; для уже существующей установки задайте её текущий root через `VIBELAB_EGRESS_ROOT`,
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

# container client
scripts/provision-client.sh --client container-main --environment container
```

Для каждого client генерируются: bundle (`client.yaml`, `client.token`,
`default-proxy.auth`) + backend-секреты (`clients/<id>.*`). Provider
secrets в bundle **не попадают**.

### Dev/test bridge (нужен, если агент работает из container)

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
root-owned (0755), чтобы enrollment-служба могла пройти к своему подкаталогу.
Backend secrets в подкаталоге `secrets/` сохраняют
собственные ограничения доступа.

### TUN prerequisites and per-run setup

TUN mode runs locally in the execution environment. Each `egress-run --mode
tun` creates a fresh user and network namespace; the workload remains
inside that environment. A small static packet engine outside the nested
namespace receives only the TUN file descriptor and forwards packets to the
selected one-shot SOCKS route. The helper service, helper socket, and
`CAP_NET_ADMIN` on the outer client container are not used.

For an already installed backend, this one command prepares and installs a host
client profile (and reuses its existing bundle without rotating credentials).
It builds the packet engine when missing; stable Go 1.26.3 or newer is needed
only for that build. For a fresh backend install, build the engine first as shown below, then
`install-backend-host.sh` provisions and installs `host-main` automatically.

```bash
sudo scripts/setup-egress-client.sh --client host-main --environment host
egressctl capabilities
```

Build the pinned packet engine before provisioning client bundles:

```bash
scripts/build-tun-engine.sh
```

Install on a Linux host with `/dev/net/tun`, `unshare` (util-linux), `ip`
(iproute2), and unprivileged user namespaces enabled. Install the host client
as in step B, then verify the local capability:

```bash
egressctl capabilities
egress-run --mode tun <route> -- curl -fsS https://example.com/
```

For a one-command check in an ordinary Docker test container, run this after
the backend is installed. It builds the engine if needed, provisions a bundle,
builds a plain test image, installs the client inside it, and checks the chosen
proxy route in proxy and TUN modes:

```bash
sudo scripts/setup-egress-client.sh --client egress-test-client \
  --environment container --docker-test --route blanc:FI-Helsinki-1
```

To also check two different TUN routes concurrently, pass two route IDs. The
harness starts both workloads before releasing them to fetch their public exit
IP, and checks that they have distinct network namespaces and distinct public
exit IPs:

```bash
sudo scripts/setup-egress-client.sh --client egress-test-client \
  --environment container --docker-test \
  --route 'blanc:FI-Helsinki-1' --route 'xeovo:US-New-York-1'
```

Choose routes known to use different exit IPs. With `--copy-personal-source`,
pass personal provider node IDs; each distinct provider snapshot is copied once
and its route IDs are mapped to the test-owned snapshot.

Для персонального маршрута host-профиля (ID начинается с `p-`) добавьте
`--copy-personal-source`. Host скопирует нормализованный snapshot в новый
персональный источник тестового клиента и заменит ID маршрута перед запуском
контейнера. URL исходной подписки и YAML с узлами не попадут в bundle или
контейнер. Snapshot независимый: повторный запуск обновит его с host-источника.
Например:

```bash
sudo scripts/setup-egress-client.sh --client egress-test-client \
  --environment container --docker-test \
  --route 'p-8970d7b87bee79eda2c25b78-blanc:vless-49' --copy-personal-source
```

The same test harness can be run directly with an existing bundle:

```bash
sudo bash tests/docker/run-client-container.sh \
  /var/lib/vibelab-egress/client-bundles/egress-test-client blanc:FI-Helsinki-1
```

The harness starts an ordinary Docker client with `/dev/net/tun` plus the repository
profile `client/seccomp-tun.json`. The Docker profile allows the exact nested
namespace tuple used by the client; do not add `--privileged` or
`CAP_NET_ADMIN`. Install `util-linux`, `iproute2`, and `ca-certificates` in the
image. TUN reads nameserver addresses from the container's `/etc/resolv.conf`
and makes those addresses local only inside each private network namespace.
Its DNS forwarder then uses configured resolvers over the selected route; the
default Cloudflare resolvers use DoH first, then UDP and TCP fallback. The
outer container's DNS and network remain unchanged. At least one configured
`tun_dns_servers` address must differ from all nameservers in `/etc/resolv.conf`
so the forwarder has a non-recursive upstream. The generic test image and
launch flags are in `tests/docker/`.

For removal of older privileged helper installations, first let all jobs drain,
then run `sudo scripts/remove-legacy-tun-helper.sh`. The script stops the host
helper before checking its job records, freezes a legacy helper container while
checking it, and removes only helper files. If host job records remain, it
refuses deletion and leaves the host helper stopped so no new job can race the
check; inspect/drain those records and rerun the script. Client credentials and
backend data are preserved.

### Логи и диагностика установки

**Где лежат логи.**
- Установка: `<root>/install-backend-host.log` (append; по умолчанию
  `/var/lib/vibelab-egress/install-backend-host.log`).
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

В работающем container под `runsc` проверен случай, когда `docker cp`
сообщил об успехе, но файлы в `/tmp` не появились внутри контейнера.
Передавайте bundle через stdin контейнера и проверяйте его там до установки:

```bash
sudo tar -C <root>/client-bundles/<id> -cf - . |
  docker exec -i <container> sh -c \
  'mkdir -p /tmp/vibelab-egress-client && tar -C /tmp/vibelab-egress-client -xf -'
docker exec <container> test -s /tmp/vibelab-egress-client/client.token
docker exec <container> bash /srv/workspace/vibelab-egress/scripts/install-client.sh \
  --client <id> --target container --bundle /tmp/vibelab-egress-client
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
   # или внутри container:
   scripts/install-client.sh --client <id> --target container \
       --bundle <bundle>
   ```

2. **Environment-specific** `/etc/vibelab-egress/client.yaml`
   (генерируется provisioning, не копится вручную; override
   `VIBELAB_EGRESS_CONFIG`).

3. **Отдельные read-only secrets**: API token + default-proxy auth в
   отдельных файлах (host: `/etc/vibelab-egress/*.token|*.auth` 0600;
   container: `/run/secrets/vibelab_egress_*` read-only mount; в plain-
   контейнере без такого маунта `install-client.sh` сам создаёт
   `/run/secrets` в writable rootfs).

4. **Skill** (`skills/egress/SKILL.md`) — устанавливается, если
   environment agent-capable (container).

Для TUN host и container требуют статический packet engine, собранный
командой `scripts/build-tun-engine.sh` (стабильный Go 1.26.3 или новее). Provisioning включает его
в bundle; установка проверяет checksum и выбирает сборку по архитектуре.

5–7. **Проверки**:

   ```bash
   egressctl config validate
   egressctl whoami
   egressctl doctor
   # или одним скриптом:
   scripts/verify-client.sh [--config <path>] [--offline]
   ```

### Reproducible container installation

Production container: bake client package + Skill в image либо
подключать детерминированным read-only artifact/mount (bundle из
`<root>/client-bundles/container-main`). Не полагаться на ручную
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

Для приватной URL-подписки используйте `--url -` и подайте одну строку через
stdin: так URL с токеном не окажется в аргументах процесса.

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

### Проверка из обычного Docker-контейнера

Для проверки без заранее подготовленной среды создайте bundle на host и
запустите одноразовый тестовый контейнер. В контейнер монтируется checkout и
bundle; `install-client.sh`, `egressctl` и дочерняя команда `egress-run`
выполняются внутри него. Контейнеру не выдаются Docker socket или
`CAP_NET_ADMIN`.

```bash
scripts/provision-client.sh --client egress-test-client --environment container
bash tests/docker/run-client-container.sh \
  /var/lib/vibelab-egress/client-bundles/egress-test-client blanc:FI-Helsinki-1
```

Сценарий собирает обычный `python:3.11-slim`-based image, добавляет только
host-gateway DNS alias и выполняет `whoami`, локальность дочернего процесса и
HTTPS-запросы в proxy и TUN mode через выбранный route. Для TUN Docker
получает `/dev/net/tun` и профиль `client/seccomp-tun.json`; профиль не выдаёт
контейнеру `CAP_NET_ADMIN` и не включает `--privileged`. Каждое выполнение
создаёт свой namespace, так что параллельные запуски могут выбирать разные
route ID. Пакетный engine получает только TUN fd и выбранную SOCKS-сессию;
команда и её дочерние процессы остаются в клиентском контейнере. Если передать
два `--route`, harness дополнительно запускает TUN workloads одновременно,
сверяет namespace и проверяет, что каждый получил публичный exit IP, отличный
от IP второго маршрута.
