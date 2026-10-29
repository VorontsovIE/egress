---
title: "Техническое задание: управляемый egress-слой на базе Mihomo"
status: "Architecture review / client-local CLI model"
date: "2026-10-09"
lang: ru-RU
---

# 0. Назначение документа

Этот документ является **полным самостоятельным техническим заданием** на разработку, установку и тестирование управляемого egress-слоя для исследовательских и coding-агентов.

Документ должен быть достаточен для передачи coding-агенту без доступа к истории обсуждения.

Базовая инфраструктура:

- Linux host `globe`, Ubuntu 24.04;
- Docker Engine, cgroup v2;
- одним из пользователей egress будет container внутри контейнера (он работает в Docker + container runtime; считается недоверенным execution environment)
- LiteLLM работает на host и доступен container через host-side relay;
- основной networking/proxy engine нового egress-слоя — **Mihomo**;
- production deployment должен использовать pinned stable Mihomo version/image digest, а не `latest`.

Везде далее термин «клиентский контейнер» означает произвольный Docker-контейнер, пользующийся egress. Вместо LiteLLM — другой сервис в докер-контейнере в той же локальной сети или сервис в сети хоста, к которому клиентский контейнер подключается напрямую.

# 1. Краткое резюме решения

Нужно развернуть отдельный Docker Compose stack `vibelab-egress` на базе Mihomo.

`vibelab-egress` является отдельным deployment от container и других execution environments:

- Mihomo и `egressd` работают в отдельном Docker Compose stack;
- они НЕ запускаются внутри клиентского контейнера;
- они НЕ устанавливаются как основной data/control plane непосредственно на host;
- один backend обслуживает несколько execution environments: host, Docker container и при необходимости другие контейнеры/VM;
- **`egressctl` и `egress-run` устанавливаются локально в каждую среду, в которой требуется выполнять код**;
- локальные clients используют один и тот же protocol/API, но разные client profiles/configs и разные адреса подключения к backend;
- `egress-run` всегда является тонким локальным execution wrapper: команда запускается в той же execution environment, где был запущен `egress-run`, а не удалённо на host/backend;
- agent Skill устанавливается в agent environment рядом с локальными client tools;
- TUN выполняется клиентом в локальном per-run namespace; privileged host service не используется.

Mihomo должен выполнять максимально возможную часть работы штатными механизмами:

- proxy providers/subscriptions;
- WireGuard/VLESS и другие поддерживаемые outbounds;
- обновление providers;
- health/latency tests;
- per-client selector/default routing;
- SOCKS/listener data plane;
- GeoIP/GeoSite/ASN routing data;
- routing rules;
- DIRECT bypass.

Собственный код должен быть минимальным и состоять из:

1. `egressd` — узкого public control/security adapter поверх Mihomo API;
2. общего client package с `egressctl` и `egress-run`;
3. provider adapters — **только если** BlancVPN/Xeovo/другой провайдер не дают Mihomo-compatible subscription;
4. bounded throughput benchmark;
5. domain availability probe across routes;
6. client-local strict/TUN namespace bootstrap and packet engine;
7. обязательного agent Skill `skills/egress/SKILL.md`;
8. install/bootstrap scripts и тестового harness;
9. `README.md` для пользователей, включающего отдельный раздел для агентов.

## 1.1. Deployment topology

Deployment topology является нормативной частью архитектуры.

### Backend Docker Compose stack `vibelab-egress`

Основной egress stack MUST работать в Docker и MUST быть отдельным от container deployment.

```text
Docker host
|
+-- container stack
|   `-- container
|
`-- vibelab-egress stack
    |-- mihomo
    `-- egressd
```

Предпочтительно запускать Mihomo и `egressd` как два отдельных services одного Compose project:

```text
vibelab-egress-mihomo
vibelab-egress-egressd
```

Объединение их в один container допускается только при отдельном обосновании и не должно автоматически расширять privileges `egressd` до privileges Mihomo/TUN.

### Что живёт внутри egress stack

В Docker stack должны находиться:

- Mihomo;
- `egressd`;
- provider subscriptions/configuration;
- generated Mihomo configuration;
- persistent Mihomo state;
- per-client selector state;
- health/latency state;
- throughput/domain-probe state, если он хранится;
- authenticated default и one-shot SOCKS/proxy endpoints;
- public narrow `egressd` API;
- TUN endpoint самого Mihomo, если он нужен backend-side функциям.

VPN credentials и subscription secrets доступны egress stack, но не client environments.

### Client package: устанавливается там, где исполняется workload

Одинаковый client package устанавливается независимо:

```text
host environment
├── egressctl
├── egress-run
└── client.yaml

Docker container
├── egressctl
├── egress-run
├── client.yaml
└── skills/egress/SKILL.md

other container / VM
├── egressctl
├── egress-run
└── client.yaml
```

Ключевой invariant:

> `egress-run ROUTE -- command` запускает `command` локально в той execution environment, где запущен `egress-run`.

Следовательно:

- host `egress-run` запускает host process с host filesystem/environment;
- container `egress-run` запускает process внутри Docker container с его filesystem/environment;
- backend никогда не получает arbitrary command payload для обычного proxy execution;
- `egressd` не является remote-exec service.

### Environment-specific client config

Один и тот же client binary MUST работать с environment-specific config.

Рекомендуемый canonical path:

```text
/etc/vibelab-egress/client.yaml
```

и override:

```text
VIBELAB_EGRESS_CONFIG=/path/to/client.yaml
```

Минимальная схема:

```yaml
client_id: container-main
control_url: http://host.docker.internal:9091
proxy_host: host.docker.internal
default_proxy_port: 1080
oneshot_proxy_port: 1081
api_token_file: /run/secrets/vibelab_egress_client_token
default_proxy_auth_file: /run/secrets/vibelab_egress_default_proxy_auth
environment: container
tun_backend: local-userns
tun_dns_servers: 1.1.1.1,1.0.0.1
```

Для host:

```yaml
client_id: host-main
control_url: http://127.0.0.1:9091
proxy_host: 127.0.0.1
default_proxy_port: 1080
oneshot_proxy_port: 1081
api_token_file: /etc/vibelab-egress/client.token
default_proxy_auth_file: /etc/vibelab-egress/default-proxy.auth
environment: host
tun_backend: local-userns
tun_dns_servers: 1.1.1.1,1.0.0.1
```

`client_id` в локальном config нужен для диагностики, но server MUST определять реальную client identity по credential/token. Нельзя доверять произвольному `client_id`, присланному клиентом.

Client config не содержит provider/VPN secrets или Mihomo admin secret. API token/default proxy auth SHOULD храниться в отдельных secret files, а не inline в YAML.

### Per-client default state

Default route и default execution mode являются **per-client**, а не глобальными для всего backend.

Пример:

```text
host-main:
  default route = direct
  default mode  = proxy

container-main:
  default route = xeovo:fi-helsinki-03
  default mode  = proxy
```

Изменение default в container не должно менять host default и наоборот.

Для каждого provisioned client Mihomo/config generator SHOULD иметь отдельную selector group, например:

```text
CLIENT::host-main
CLIENT::container-main
```

`egressd` переключает только selector, относящийся к authenticated client.

Provisioning/removal client profiles является administrative/bootstrap operation и не входит в public agent API.

### Что живёт непосредственно на host

На host могут находиться:

1. обычная копия client package (`egressctl`, `egress-run`, host `client.yaml`);
2. install/bootstrap scripts;
3. firewall rules;
4. fixed acceptance/test harness;
5. narrow deployment helper;
6. Docker-CIDR synchronization helper;
7. client-local strict/TUN namespace and static packet engine.

Host client package является обычным пользователем backend и не получает Mihomo admin API/provider secrets.

На host НЕ должны постоянно работать второй экземпляр Mihomo или второй полноценный egress control plane без отдельного архитектурного решения.

### Доступ к backend

Один backend обслуживает несколько local clients:

```text
HOST workload
    |
    | local egressctl / egress-run
    | config -> 127.0.0.1
    v
+----------------------------+
|                            |
|      vibelab-egress        |----> Internet / VPN
|      egressd + Mihomo      |
|                            |
+----------------------------+
    ^
    | config -> host.docker.internal
    | local egressctl / egress-run
    |
Docker container workload
```

Agent-facing endpoints публикуются:

- на host loopback для host-native clients;
- на разрешённом Docker gateway/interface для container clients;
- никогда на public NIC.

Mihomo raw/admin API не публикуется ни на public interface, ни напрямую в client networks. К нему обращается только `egressd` и строго ограниченные administrative procedures.

### Privilege separation

container MUST NOT получать ради egress:

- Docker socket;
- raw Mihomo admin API;
- provider subscription secrets/private keys;
- host root shell;
- возможность изменять host nftables/routes;
- TUN получает только через `/dev/net/tun` и отдельный per-run user/network namespace; outer container не получает `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, host networking или privileged mode.

Клиент сам создаёт nested user/network namespaces; dataplane engine получает только открытый TUN file descriptor и selected route credentials. Engine не запускает workload и не является generic exec API.

# 2. Термины

## 2.1. Route

`route` — логическое направление egress.

Route ID может быть:

- `direct`;
- конкретной локацией BlancVPN/Xeovo/другого провайдера;
- конкретной локацией без провайдера, включая VLESS-ноды и по возможности VMESS, Trojan, Hysteria, Shadowsocks и другие поддерживаемые Mihomo protocols.

Примеры:

```text
direct
blanc:fi-helsinki-03
xeovo:nl-amsterdam-01
vless:personal-01
```

`direct` является **полноценным route ID**, а не специальным отсутствием маршрута.

Он доступен:

- как persistent default route;
- как explicit one-shot route;
- в domain probing;
- в benchmark/health tests там, где это осмысленно.

## 2.2. Client profile

`client profile` — identity одной execution environment, которая пользуется backend.

Примеры:

```text
host-main
container-main
research-container-02
```

Client identity определяется backend по credential/token. Она задаёт:

- отдельный persistent default route;
- отдельный persistent default execution mode;
- разрешённые capabilities/modes;
- audit identity;
- default proxy authentication identity/selector;
- rate/resource limits при необходимости.

Одна execution environment обычно имеет один client profile, но архитектура не должна запрещать несколько профилей.

## 2.3. Default route

Persistent route **конкретного client profile**, используемый `egress-run --default` и default proxy identity этого client.

Default route НЕ означает global default route host OS и НЕ является единым глобальным selector для всех clients.

Обычные host services (`sshd`, Docker daemon, LiteLLM и т. п.), не запущенные через egress-интерфейс, не должны автоматически попадать в egress.

## 2.4. One-shot route

Route, указанный для одной операции/команды. One-shot не меняет default route клиента.

One-shot operations должны поддерживать **параллельное выполнение через разные routes**, включая `direct`.

## 2.5. Execution mode

Независимая от route сущность:

- `proxy` — userspace SOCKS/proxy path;
- `tun` — strict/network-enforced path, где приложение не обязано знать о proxy.

Route и execution mode независимы:

```text
route=direct, mode=proxy
route=blanc:fi-helsinki-03, mode=proxy
route=xeovo:nl-amsterdam-01, mode=tun
route=direct, mode=tun
```

Для `direct + tun` смысл `tun` — strict/network-enforced execution без proxy/VPN egress; реализация может использовать isolated netns + direct NAT/path.

Поддержка `tun` является capability конкретной execution environment. Нельзя silently fallback из `tun` в `proxy`.

## 2.6. Local client

`egressctl` и `egress-run` являются local clients.

- `egressctl` управляет backend через public `egressd` API;
- `egress-run` получает у backend нужную network capability/session и запускает локальную команду;
- ни один из них не содержит provider secrets;
- ни один из них не должен требовать Mihomo admin secret;
- один и тот же client package должен работать на host и внутри container с разными configs.

# 3. Архитектурные принципы

1. **Mihomo-first.** Не писать собственные protocol engines, provider updater, selector или latency engine, если это уже штатно делает Mihomo.
2. **Local execution.** `egress-run` запускает command в той же execution environment, где запущен сам wrapper. Backend не исполняет command удалённо.
3. **Один backend, много clients.** Host/container/другие среды используют один `vibelab-egress`, но имеют отдельные client profiles/configs/defaults.
4. **container остаётся недоверенным.** Не выдавать ему Docker socket, host root shell, provider secrets или Mihomo admin secret.
5. **Public API parity.** Public `egressd` API и control capabilities `egressctl` должны быть изоморфны: `egressctl` — удобный клиент API, а не security boundary.
6. **No hidden privileged CLI.** `egressctl` не получает скрытых административных endpoints/capabilities, недоступных прямому public API client с тем же credential.
7. **Data plane и control plane разделены.** Control идёт через `egressd`; сетевой трафик — через Mihomo/data-plane endpoints.
8. **Все внутренние Docker destinations для container clients всегда DIRECT.** Это hard security/routing invariant.
9. **LiteLLM всегда DIRECT** как частный случай local/operator control plane; дополнительные direct destinations задаются оператором.
10. **Per-client default.** Default route/mode одного client не влияет на defaults других clients.
11. **Default route и one-shot routes независимы.**
12. **One-shot concurrency обязательна.** Нельзя использовать один mutable global `ONESHOT` selector с lock как финальную архитектуру.
13. **`direct` доступен везде**, где семантически доступна конкретная proxy/VPN location.
14. **Proxy first, TUN when needed.** Proxy mode обязателен для v1; TUN capability может отличаться по execution environment.
15. **Нет глобального host VPN.** Egress применяется только к явно подключённым clients/commands.
16. **Provider subscriptions/keys остаются в backend.**
17. **Fail safe / last-known-good.** Failed provider update не уничтожает рабочую конфигурацию.
18. **Фактическая доступность домена измеряется запросом**, а не выводится только из GeoIP/GeoSite.
19. **No remote arbitrary exec.** Public backend API никогда не принимает произвольную shell command для исполнения на host/backend.
20. **Capabilities explicit.** Unsupported `tun`/UDP/etc. возвращаются явно и диагностируемо.

# 4. Цели

Система должна позволять пользователю/агенту из host или container execution environment:

- установить одинаковый `egressctl`/`egress-run` client package;
- подключиться к одному backend через environment-specific config;
- видеть собственный client identity/capabilities;
- видеть все доступные routes;
- видеть **свой** текущий default route и mode;
- менять свой default route/mode, не влияя на другие clients;
- запускать локальную команду через любой route, включая `direct`;
- запускать несколько one-shot команд одновременно через разные routes;
- обновлять subscriptions/providers;
- при необходимости инициировать provider update через заданный working route;
- проверять health/latency routes;
- проверять доступность конкретного domain через набор или все routes;
- выполнять bounded throughput benchmark shortlist routes;
- выбирать route по стране/provider/latency/speed/health/domain availability;
- использовать GeoIP/GeoSite/ASN metadata как дополнительный контекст;
- диагностировать deployment через `egressctl doctor`;
- использовать Skill для agent workflows;
- устанавливать/настраивать backend и отдельные clients по воспроизводимой документации;
- при наличии environment capability запускать local command в strict/TUN режиме.

# 5. Не-цели первой версии

Не входят автоматически:

- глобальный VPN для всего host;
- изменение host default route;
- public proxy/API service;
- прямой agent access к полному Mihomo API;
- собственная реализация WireGuard/VLESS;
- scraping web cabinets провайдеров;
- full-fleet speed-test при каждом выборе;
- отключение container runtime ради strict mode;
- выдача container Docker socket;
- generic privileged RPC / arbitrary host shell.

# 6. Исходная инфраструктура и DIRECT control plane

Текущая логика container → LiteLLM (напомню: вместо container и litellm могут быть другие пользователи сети в докер-контейнерах, обращающиеся друг к другу через внутреннюю докер-сеть):

```text
container (container runtime)
   |
   | Docker default bridge
   v
host.docker.internal / docker0 gateway
   |
   +--> host relay :4001
          |
          +--> 127.0.0.1:4000 LiteLLM
```

Egress не должен нарушать этот path.

Hard DIRECT должен покрывать не только LiteLLM, а **все локальные Docker networks** и явно заданный operator control plane.

Если ресурс, к которому мы обращаемся напрямую (типа LiteLLM) находится на другой машине, должна быть возможность явно задать, что это подключение тоже HARD DIRECT.

# 7. Threat model

## 7.1. Недоверенные субъекты

- container agent;
- команды/код, запущенные агентом;
- любой client, имеющий только ordinary public API credential;
- внешние websites/repositories;
- provider subscription content;
- другие containers на Docker network без корректного client credential.

## 7.2. Доверенные компоненты

- host administrator;
- pinned Mihomo image/binary;
- `egressd`;
- install/bootstrap scripts;
- local per-invocation TUN namespace bootstrap;
- provider secret store.

`egressctl` и `egress-run` не являются security boundary: недоверенный code может повторить их public API calls самостоятельно.

## 7.3. Security invariants

1. container не видит provider subscription URLs/private keys/VLESS secrets.
2. container не видит Mihomo admin secret.
3. Mihomo admin API не публикуется на host/docker gateway как client-facing service.
4. Public `egressd` API безопасен при прямом вызове без `egressctl`.
5. Public API не предоставляет generic `exec`, arbitrary file access, arbitrary config patch или raw Mihomo API passthrough.
6. `egressctl` использует только тот же public API и не имеет скрытого privileged transport.
7. Client identity определяется credential/token, а не доверенным полем `client_id` запроса.
8. Agent-facing ports не слушают public NIC.
9. Internal Docker CIDRs всегда DIRECT в default, one-shot, probe и strict modes для container clients.
10. Operator-defined HARD DIRECT destinations имеют приоритет над выбранным route.
11. Provider subscription update по умолчанию выполняется DIRECT; update via route не должен требовать раскрытия provider secret client-side.
12. Local TUN bootstrap не превращается в root-shell API; workload capabilities сбрасываются до exec.
13. Никакой автоматический fallback `container runtime -> runc` недопустим.
14. Provider config/subscription parser считается input-validation boundary.
15. One client не может менять default route/mode другого client через public API.
16. Provider/API credentials разных trust domains должны быть разделены.
17. `egress-run` command argv/environment не отправляются backend в ordinary proxy mode.

# 8. Итоговая схема

```text
                              HOST

   host workload                              Docker container workload
        |                                              |
   egressctl / egress-run                         egressctl / egress-run
        |                                              |
   client.yaml:                                  client.yaml:
   127.0.0.1                                    host.docker.internal
        |                                              |
        +----------------------+-----------------------+
                               |
                               v
                  +-----------------------------+
                  | vibelab-egress Docker stack |
                  |                             |
                  | +---------+      private    |
                  | | egressd |-----------------+----+
                  | +----+----+                      |
                  |      |                           v
                  |      |                    +-------------+
                  |      |                    |   Mihomo    |
                  |      |                    | providers   |
                  |      |                    | selectors   |
                  |      |                    | rules/GEO   |
                  |      |                    +------+------+ 
                  +------+---------------------------+-------+
                         |                           |
                 metrics/catalog/state         Internet/VPN

client-local per-run optional:
  nested user/network namespace + TUN packet engine

Agent/client-facing:
  :1080  authenticated per-client default proxy listener
  :1081  authenticated route-addressable one-shot listener
  :9091  public narrow egressd API

Never client-facing:
  Mihomo admin API
  provider secrets
  generic host exec
```

Host и container используют один backend, но разные client configs и credentials.

Default state различается по client profile; one-shot sessions дополнительно различаются по route/session и могут выполняться параллельно.

# 9. Mihomo responsibilities

Mihomo должен отвечать за:

- proxy providers/subscriptions;
- provider health-check;
- per-node delay tests;
- WireGuard/VLESS и другие поддерживаемые protocols;
- per-client selector groups (`CLIENT::<client_id>` или эквивалент);
- authenticated default SOCKS listener;
- route-addressable authenticated one-shot listener;
- routing rules/`IN-USER` или подтверждённый эквивалент;
- GeoIP/GeoSite/ASN rules;
- HARD DIRECT bypass;
- provider refresh API;
- direct route.

Наличие общего aggregate group допустимо как implementation detail, но **не должно превращать default route в один глобальный selector для всех clients**.

Production version должна быть pinned по tag + digest после PoC; точная syntax/features проверяются против этой pinned версии.

# 10. egressd responsibilities

`egressd` — тонкий public control/security adapter.

Он должен:

- ходить во внутренний Mihomo API;
- предоставлять authenticated public API, соответствующий возможностям `egressctl`;
- аутентифицировать client profile по credential/token;
- возвращать `/self` identity/capabilities;
- нормализовать location catalog и stable route IDs;
- provision-time связывать client profiles с Mihomo per-client selector groups;
- хранить per-client default route/mode;
- переключать только selector authenticated client;
- обновлять providers и reconciliation state;
- при исчезновении route переводить только затронутые client defaults на `direct`;
- собирать/хранить health metadata;
- выполнять domain probes;
- выполнять bounded benchmarks;
- управлять one-shot route credentials/mapping/session metadata;
- выдавать network capability для `egress-run`, но не запускать пользовательские commands;
- хранить audit log с client identity;
- не раскрывать provider secrets.

`egressd` НЕ должен:

- принимать arbitrary shell command для исполнения;
- исполнять workload клиента;
- быть generic proxy к Mihomo admin API;
- предоставлять arbitrary netns/firewall operations;
- доверять client-provided `client_id` вместо authenticated identity.

## 10.1. Public API parity rule

Все обычные control-plane возможности `egressctl` должны быть выражены через documented public `/v1` API.

`egressctl` не имеет скрытых privileged endpoints. Прямой HTTP client с тем же token должен иметь ровно те же permissions, что `egressctl`.

Исключение: local execution mechanics `egress-run` (например local `exec()` или per-run TUN namespace) не являются server control-plane capabilities и поэтому не обязаны быть REST endpoints.

# 11. Providers/subscriptions

## 11.1. Native provider — предпочтительный вариант

Если BlancVPN/Xeovo выдают Mihomo/Clash-compatible YAML/URI/base64 subscription, использовать `proxy-providers` напрямую.

Mihomo поддерживает `http`, `file`, `inline`, interval updates, health-check, filter/exclude-filter и override.

Пример концепции:

```yaml
proxy-providers:
  blanc:
    type: http
    url: <secret>
    path: ./providers/blanc.yaml
    interval: 21600
    proxy: DIRECT
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      timeout: 5000
    override:
      additional-prefix: "[blanc] "
```

## 11.2. Adapter path

Если provider выдаёт WireGuard `.conf` или иной несовместимый формат:

```text
provider source -> provider-specific adapter -> Mihomo provider YAML
```

Adapter обязан:

- иметь fixtures без secrets;
- делать atomic replace;
- сохранять last-known-good;
- не логировать private keys;
- иметь отдельные tests.

## 11.3. Custom VLESS

Custom VLESS node(s) должны быть static proxies или file provider.

# 12. Обновление подписок

Обязательные команды:

```bash
egressctl update
egressctl update --provider blanc
egressctl update --provider xeovo
egressctl update --if-older-than 6h
```

Дополнительно должна быть возможность запросить transport route для provider refresh, если direct update блокируется:

```bash
egressctl update --provider blanc --via xeovo:fi-helsinki-03
egressctl update --provider blanc --via direct
```

`--via` задаёт egress route backend-side download/update и не раскрывает subscription URL/token client-side.

Если реализация native Mihomo `proxy-providers` не позволяет безопасно выполнить конкретный refresh через произвольный route без global selector race, `egressd` должен выполнить controlled fetch через route-specific data plane, проверить/атомарно записать provider artifact и затем выполнить reconcile. Нельзя временно менять shared default selector ради provider update.

TUN transport для provider refresh может быть добавлен в Phase 2, если он реально нужен; proxy-capable HTTP fetch является предпочтительным механизмом.

После update система должна:

1. обновить provider через Mihomo либо adapter/controlled fetch;
2. перечитать inventory;
3. обновить normalized catalog;
4. обновить one-shot route mappings;
5. проверить defaults всех client profiles;
6. каждый default, чей route исчез, переключить именно для этого client на `direct`;
7. запустить быстрый health-check новых/изменённых nodes;
8. обновить GEO assets при необходимости/по отдельной политике freshness;
9. записать audit event с caller client identity и способом update.

Failed update не должен уничтожать previous working provider state.

# 13. Route catalog

Agent-facing route object:

```json
{
  "id": "blanc:fi-helsinki-03",
  "provider": "blanc",
  "display_name": "Helsinki 03",
  "country": "FI",
  "city": "Helsinki",
  "protocol": "wireguard",
  "healthy": true,
  "latency_ms": 43,
  "exit_ip": "...",
  "exit_country": "FI",
  "asn": 12345,
  "last_test_at": "...",
  "benchmark_mbps": 210,
  "benchmark_at": "..."
}
```

`direct` должен присутствовать в catalog как synthetic route:

```json
{
  "id": "direct",
  "provider": "builtin",
  "display_name": "Direct",
  "protocol": "direct"
}
```

Stable route ID не должен зависеть только от изменяемого display name.

# 14. GeoIP / GeoSite / ASN

## 14.1. Использование Mihomo GEO data

Mihomo должен быть настроен на использование и автоматическое обновление GEO data.

Рекомендуемый upstream:

```text
https://github.com/MetaCubeX/meta-rules-dat
```

Использовать поддерживаемые Mihomo assets:

- `geoip.dat` или `geoip.metadb`/`country.mmdb`;
- `geosite.dat`;
- `GeoLite2-ASN.mmdb`.

В конфигурации должны использоваться штатные параметры Mihomo:

```yaml
geo-auto-update: true
geo-update-interval: 24
geox-url:
  geoip: ...
  geosite: ...
  mmdb: ...
  asn: ...
```

## 14.2. Назначение GEO data

GEO data используются для:

- верификации фактической страны exit IP;
- ASN enrichment;
- классификации маршрутов;
- GEOIP/GEOSITE routing rules;
- heuristics при выборе кандидатов;
- объяснения агенту, почему route подходит/не подходит.

## 14.3. Ограничение

GeoIP/GeoSite **не считаются достоверной базой блокировок**.

Если задача звучит как «доступен ли `example.com` из страны X», окончательный ответ должен основываться на реальном probe через route.

GEO data дают context, probe даёт evidence.

# 15. Default route

Default route является per-client.

Для каждого provisioned client Mihomo SHOULD иметь отдельную selector group:

```text
CLIENT::host-main = select(direct + all allowed provider nodes)
CLIENT::container-main = select(direct + all allowed provider nodes)
```

`egressd` public API действует относительно authenticated caller.

CLI:

```bash
egressctl route current
egressctl route set direct
egressctl route set blanc:fi-helsinki-03
```

Допускаются aliases:

```bash
egressctl status
egressctl use direct
egressctl use ROUTE
```

`route current` возвращает минимум:

- authenticated `client_id`;
- route ID;
- provider/display name;
- current health;
- default execution mode;
- selector/backend reconciliation status.

Изменение route одним client не влияет на selectors/defaults других clients.

Если selected route исчез после provider update, только затронутые client profiles получают fallback `direct` + warning/audit event.

# 16. Default execution mode

Система хранит default execution mode **per-client**:

```text
proxy | tun
```

CLI:

```bash
egressctl mode current
egressctl mode set proxy
egressctl mode set tun
```

Это не переключает global host networking и не меняет mode других clients.

Default mode используется `egress-run --default` локально в конкретной execution environment.

Backend `/self`/capabilities должен сообщать, какие modes разрешены для client profile. Локальный client также проверяет наличие локальной namespace/TUN capability.

Если `tun` недоступен, операция должна вернуть явный `unsupported`/`unsupported_in_environment`, без silent fallback в `proxy`.

# 17. Proxy data plane

## 17.1. Per-client default listener

`1080/tcp` — authenticated default SOCKS endpoint.

Каждый client получает собственную default proxy identity/credential. По этой identity Mihomo направляет трафик в selector данного client profile:

```text
client-default:host-main       -> CLIENT::host-main
client-default:container-main -> CLIENT::container-main
```

Фактические usernames/passwords не обязаны буквально содержать client ID; предпочтительны opaque credentials.

Порядок routing:

1. hard DIRECT bypass rules;
2. client identity -> client selector;
3. selector -> current per-client default route.

Это позволяет generic proxy-aware applications пользоваться persistent client default без `egress-run`.

## 17.2. One-shot listener

`1081/tcp` — authenticated route-addressable SOCKS endpoint.

Он НЕ использует один mutable `ONESHOT` selector.

Маршрут определяется на уровне SOCKS authentication identity / `IN-USER` rule либо другого Mihomo-native route-addressable mechanism, подтверждённого PoC.

Концептуально:

```text
(client A, route direct)               -> DIRECT
(client A, route blanc:fi-helsinki-03) -> [blanc] Helsinki 03
(client B, route xeovo:nl-01)          -> [xeovo] Amsterdam 01
```

Hard DIRECT bypass имеет более высокий приоритет, чем выбранный one-shot route.

## 17.3. Credentials/capabilities

Default proxy credential является client-scoped secret.

One-shot capability может быть:

- client+route credential, созданным при reconcile;
- short-lived session credential;
- иным mechanism, если он обеспечивает concurrency, audit identity и не требует mutable global selector.

Provider private credentials никогда не выдаются client.

# 18. One-shot concurrency

Обязательное требование:

```bash
egress-run blanc:fi-helsinki-03 -- cmd-A
egress-run xeovo:us-new-york-02 -- cmd-B
egress-run direct -- cmd-C
```

могут выполняться одновременно в одной или разных execution environments.

Они не должны:

- менять default route ни одного client;
- менять route друг друга;
- блокировать друг друга глобальным mutex;
- зависеть от единственного mutable `ONESHOT.now`.

`egressd` хранит active one-shot sessions/jobs только как observability state:

```bash
egressctl oneshot active
```

По умолчанию команда показывает sessions authenticated client; cross-client observability не является public capability.

Пример:

```json
{
  "id": "job-...",
  "client_id": "container-main",
  "route": "xeovo:us-new-york-02",
  "mode": "proxy",
  "started_at": "..."
}
```

Глобального «current one-shot route» не существует.

# 19. Генерация one-shot routing map

При provider/client reconciliation config generator строит mapping:

```text
(client identity, stable route ID)
    -> Mihomo proxy/group
    -> authenticated one-shot data-plane identity
```

Требования:

- `direct` всегда присутствует;
- mapping обновляется атомарно;
- name collisions между providers исключаются prefix-ами/stable internal names;
- capability не раскрывает provider keys;
- исчезнувший route перестаёт выдаваться новым sessions;
- существующая session получает детерминированную ошибку/завершение, если upstream route исчез;
- количество generated identities/rules должно быть измерено на реальном catalog из нескольких сотен routes и признано приемлемым в PoC;
- если client×route cardinality окажется слишком большой, разрешается заменить mapping на другой Mihomo-native concurrency-safe mechanism, но не на shared mutable selector.

# 20. `egress-run` proxy mode

`egress-run` является локальным thin execution client.

Интерфейс:

```bash
egress-run --default -- command ...
egress-run ROUTE -- command ...
egress-run direct -- command ...
egress-run --mode proxy ROUTE -- command ...
```

Алгоритм proxy mode:

1. загрузить local `client.yaml`;
2. authenticated call к public `egressd` API;
3. для `--default` получить текущий per-client default route/mode;
4. запросить/получить route-specific proxy capability/session;
5. настроить proxy path для **локально запускаемого** child process;
6. локально `exec`/spawn target command;
7. корректно проксировать exit code/signals;
8. закрыть session/обновить observability state.

Command/argv не отправляется backend для исполнения.

Для максимальной совместимости wrapper должен поддерживать:

1. native proxy env/config для известных clients, когда это надёжно;
2. `proxychains-ng`/эквивалент для generic dynamically linked TCP clients.

Документировать ограничения proxy mode:

- static binaries/direct syscalls могут обходить `LD_PRELOAD`;
- UDP/QUIC поддерживаются неполно;
- setuid binaries несовместимы;
- некоторые runtimes игнорируют env proxy;
- environment variables alone не являются network-enforcement mechanism.

Если приложение требует гарантированного перехвата, использовать `--mode tun` только при наличии соответствующей environment capability.

# 21. Strict/TUN mode

`egress-run --mode tun ROUTE -- command ...` выполняет command локально в
вызывающей среде и направляет его сеть через отдельный TUN на каждый запуск.
Это одинаково работает на Linux host и в произвольном Docker-контейнере.
`proxy` и `tun` являются независимыми per-invocation режимами; каждый вызов
может выбирать собственный route. При ошибке setup TUN команда не запускается
и переключения на proxy нет.

Клиент создаёт вложенные user и network namespaces, поднимает TUN и default
routes внутри нового network namespace, проксирует loopback DNS через TUN и
запускает workload после передачи TUN fd packet engine.
Workload, его argv, environment, cwd и stdio остаются в клиентской среде.
Engine получает только TUN fd и одноразовый SOCKS endpoint/credential для
выбранного route; он не получает argv и не запускает workload. Для route
direct используется direct packet forwarding. Закрытие команды останавливает
её engine и освобождает namespace.

При стандартных resolver IP Cloudflare (`1.1.1.1`, `1.0.0.1`) клиент сначала
отправляет DNS query через DNS-over-HTTPS на закреплённый IP с проверкой TLS
для `cloudflare-dns.com`. Если DoH недоступен, клиент пробует настроенные
resolver по UDP, затем по TCP. Для пользовательских resolver используются
только их адреса: сначала UDP, затем TCP. Все эти соединения идут внутри
выбранного TUN route; DNS не отправляется через outer Docker resolver.

На host требуются Linux, доступ к /dev/net/tun, util-linux unshare, iproute2 и
разрешённые unprivileged user namespaces. Для Docker-контейнера дополнительно
передаётся только /dev/net/tun и точечный seccomp profile, допускающий нужный
набор флагов unshare. Не выдавать outer container CAP_NET_ADMIN,
CAP_SYS_ADMIN, host networking, privileged mode или Docker socket. После
сетевой настройки workload теряет capabilities, получает no_new_privs и
запрет на создание/переход в другие namespaces.

Docker acceptance использует обычный временный контейнер и проверяет proxy,
TUN, локальность workload и реальный HTTPS-запрос. Общий helper и передача
workload/его control-процесса вне client container не используются.

# 22. Hard DIRECT bypass

Через egress никогда не должны идти:

- `127.0.0.0/8`;
- `::1/128`;
- все актуальные local Docker network CIDRs;
- Docker bridge gateways;
- `host.docker.internal`;
- operator-defined `EGRESS_BYPASS_CIDRS`;
- operator-defined `EGRESS_BYPASS_DOMAINS`;
- служебные endpoints egress control plane.

Не следует безусловно bypass-ить все RFC1918 ranges: будущая private remote network может быть доступна именно через VPN.

Docker CIDRs должны генерироваться на host helper-ом из `docker network inspect`; egress containers не получают Docker socket.

# 23. Проверка health/latency

Mihomo provider health-check и per-node delay test являются основой.

CLI:

```bash
egressctl test
egressctl test ROUTE
egressctl test --provider blanc
egressctl test --country FI
```

Основная метрика — HTTP/HTTPS delay к configurable 204 endpoint.

Не называть её ICMP ping.

`direct` также должен тестироваться как baseline.

# 24. Проверка доступности конкретного домена по routes

Обязательная feature.

CLI:

```bash
egressctl probe-domain example.com
egressctl probe-domain example.com --all
egressctl probe-domain example.com --country FI
egressctl probe-domain example.com --provider blanc
egressctl probe-domain example.com --routes direct,blanc:fi-01,xeovo:fi-02
egressctl probe-domain example.com --parallel 16 --json
```

Дополнительные параметры:

```text
--scheme http|https
--port
--path /
--method HEAD|GET
--timeout
--expected-status 200-399
--check dns,tcp,tls,http
--max-routes N
```

Для каждого route возвращать минимум:

```json
{
  "route": "blanc:fi-helsinki-03",
  "reachable": true,
  "dns_ok": true,
  "tcp_ok": true,
  "tls_ok": true,
  "http_ok": true,
  "status": 200,
  "latency_ms": 182,
  "resolved_ip": "...",
  "exit_ip": "...",
  "exit_country": "FI",
  "asn": 12345,
  "error": null
}
```

Probe должен выполняться через route-specific one-shot path и потому безопасно parallelize-иться.

`direct` участвует наравне с proxy routes.

Важно различать:

- DNS failure;
- TCP timeout/refused;
- TLS/SNI/certificate failure;
- HTTP status denial;
- redirect;
- connection reset;
- generic proxy failure.

Это даст агенту полезную информацию о характере блокировки.

# 25. Throughput benchmark

Mihomo delay test не является speed test.

CLI:

```bash
egressctl benchmark ROUTE
egressctl benchmark --country FI --top-latency 5
egressctl benchmark --provider xeovo --top-latency 10 --bytes 8M
```

Pipeline:

```text
all routes -> cheap health/latency -> shortlist -> bounded download benchmark
```

Не benchmark-ить сотни routes автоматически.

Benchmark должен использовать route-specific one-shot proxy path и поддерживать parallel jobs с configurable cap.

`direct` допустим как baseline.

# 26. `egressctl best`

Команды:

```bash
egressctl best
egressctl best --country NL
egressctl best --provider xeovo
egressctl best --country US --max-latency 150
egressctl best --country FI --domain example.com
egressctl best --speed-aware
```

Если указан `--domain`, route должен считаться кандидатом только при успешном свежем domain probe либо после on-demand probe shortlist.

Ranking по умолчанию:

1. фильтры country/provider;
2. health;
3. domain reachability, если domain задан;
4. freshness;
5. latency;
6. optional throughput score;
7. GEO verification/confidence.

Формула должна быть прозрачной и возвращаться в JSON explanation.

# 27. API egressd

Version prefix `/v1`.

Public API является API ordinary clients и соответствует control capabilities `egressctl`.

Минимум:

```text
GET  /v1/self
GET  /v1/status
GET  /v1/capabilities

GET  /v1/routes
GET  /v1/routes/{id}

GET  /v1/default
PUT  /v1/default
GET  /v1/default/mode
PUT  /v1/default/mode

GET  /v1/providers
POST /v1/providers/{id}/update
POST /v1/providers/update-all

POST /v1/test
POST /v1/routes/{id}/test
POST /v1/probe-domain
POST /v1/benchmark
GET  /v1/benchmarks

GET  /v1/proxy/default-capability

POST /v1/oneshot/session
DELETE /v1/oneshot/session/{id}
GET  /v1/oneshot/active

POST /v1/reconcile
GET  /v1/health
GET  /v1/ready
GET  /v1/doctor

GET  /v1/geo/status
POST /v1/geo/update
```

Semantics:

- authenticated token определяет client profile;
- `/v1/self` возвращает `client_id`, environment label, allowed modes/capabilities и API compatibility metadata;
- `/v1/default*` всегда относится к authenticated client;
- public API не принимает произвольный target `client_id` для изменения чужих defaults;
- `/v1/proxy/default-capability` выдаёт authenticated client его default proxy endpoint/credential metadata без provider secrets;
- `/v1/oneshot/session` не меняет selector/default и выдаёт route-specific data-plane capability;
- API никогда не возвращает provider private credentials;
- public API никогда не выполняет arbitrary user command.

Administrative provisioning clients/secrets/config generation должно идти через host-side bootstrap/admin tooling, не через ordinary public API.

## 27.1. Public API parity

Для каждого public control action `egressctl` должен существовать документированный API endpoint/operation.

Интеграционные тесты должны сравнивать CLI behaviour с прямыми API calls.

Нельзя считать отсутствие команды в CLI security control: public API сам обязан enforce permissions.

# 28. Authentication / authorization

Каждая execution environment получает отдельный client credential.

Примеры:

```text
host-main token
container-main token
```

Token связывается server-side с client profile и его permissions/capabilities.

container token может разрешать:

- self/status/list;
- read/set **своего** default route/mode;
- provider update trigger;
- health/probe/benchmark;
- create own one-shot sessions;
- read own active sessions;
- GEO status/update trigger, если разрешено policy.

Не разрешено:

- impersonate другой client;
- менять defaults другого client;
- raw Mihomo API;
- read provider secrets;
- arbitrary config patch;
- arbitrary host command;
- firewall/network namespace operations напрямую;
- client provisioning/removal.

Mihomo admin secret доступен только `egressd`/backend administration.

Client token считается доступным всему code внутри соответствующей execution environment. Поэтому token permissions должны быть безопасны для недоверенного code этой среды.

# 29. Client configuration and discovery

Один client package должен поддерживать config discovery в порядке:

1. explicit `--config PATH` (если команда поддерживает global option);
2. `VIBELAB_EGRESS_CONFIG`;
3. `/etc/vibelab-egress/client.yaml`;
4. optional user config path только если это явно документировано.

CLI должен иметь:

```bash
egressctl config show
egressctl config validate
egressctl whoami
```

`config show` по умолчанию редактирует/маскирует token/credentials.

При startup client SHOULD сравнить локальный expected `client_id` с `/v1/self`; mismatch является ошибкой конфигурации.

Host и container configs должны генерироваться provisioning/install tooling, а не копироваться вручную с заменой секретов.

# 30. CLI и `egress-run` contract

## 30.1. `egressctl`

Обязательные команды:

```bash
egressctl status
egressctl whoami

egressctl route current
egressctl route set ROUTE

egressctl mode current
egressctl mode set proxy|tun

egressctl providers
egressctl update
egressctl update --provider PROVIDER
egressctl update --if-older-than 6h
egressctl update --provider PROVIDER --via ROUTE

egressctl list
egressctl list --country FI
egressctl list --provider blanc
egressctl list --healthy

egressctl test
egressctl test ROUTE

egressctl probe-domain DOMAIN --all
egressctl benchmark ROUTE

egressctl best [filters]

egressctl oneshot active

egressctl geo status
egressctl geo update

egressctl config show
egressctl config validate
egressctl doctor
```

Все read-команды поддерживают `--json` там, где это применимо.

Exit codes стабильны и документированы.

`egressctl` использует только documented public `egressd` API.

## 30.2. `egress-run`

```bash
egress-run --default -- command ...
egress-run ROUTE -- command ...
egress-run direct -- command ...
egress-run --mode proxy ROUTE -- command ...
egress-run --mode tun ROUTE -- command ...
```

`--default` использует persistent default route + default mode authenticated client profile.

Explicit route никогда не меняет default route.

Explicit mode никогда не меняет persistent default mode.

Target command запускается локально; обычный backend API не получает command для исполнения.

`egress-run` обязан:

- использовать тот же config/token discovery, что `egressctl`;
- корректно передавать argv без shell-reparse по умолчанию;
- сохранять exit status;
- проксировать SIGINT/SIGTERM;
- cleanup session capability;
- выводить понятную ошибку при unsupported mode;
- поддерживать concurrent local invocations.

# 31. Skill для агента

Deliverable **обязателен**:

```text
skills/egress/SKILL.md
```

Skill устанавливается в agent execution environment вместе/рядом с local client package и учит использовать локальные `egressctl`/`egress-run`.

Skill должен обучить агента:

1. Начинать с `egressctl whoami`/`status`, чтобы знать client profile и capabilities.
2. Проверять `route current` перед изменением persistent default.
3. Помнить, что default per-client: изменение container default не меняет host default.
4. Предпочитать one-shot для единичной geo-specific задачи.
5. Использовать `direct` как обычный route, в том числе для baseline comparison.
6. Не менять default route ради одной команды.
7. Перед выбором routes использовать свежие health metrics.
8. Для вопроса о доступности конкретного сайта использовать `probe-domain`, а не только GeoIP/GeoSite.
9. При repeated failures/stale inventory выполнять provider update, при необходимости через working `--via ROUTE`.
10. Не benchmark-ить весь fleet; сначала shortlist.
11. Выбирать `tun` только если proxy mode недостаточен и `whoami/capabilities` подтверждает поддержку.
12. Не ожидать silent fallback `tun -> proxy`.
13. Никогда не пытаться направить HARD DIRECT/internal Docker destinations через VPN.
14. Никогда не обращаться напрямую к Mihomo admin API/provider secrets.
15. После `route set` проверять `route current`.
16. Для country-specific задачи при необходимости подтверждать exit country через GEO lookup/exit-IP check.
17. При параллельных задачах использовать независимые `egress-run ROUTE` invocations, не менять default.
18. Понимать ограничения proxy mode (`proxychains`, UDP/QUIC, static/setuid binaries).
19. Не пытаться получить host root/Docker socket для TUN из container.

Skill должен содержать decision tree и примеры минимум для:

- current/set default;
- direct one-shot;
- VPN one-shot;
- 2+ concurrent one-shot;
- domain probe across routes;
- provider update через direct и через рабочий route;
- proxy->tun decision;
- диагностику `unsupported_in_environment`.

# 32. Установка и bootstrap

Проект обязан включать backend installer и client installer отдельно.

Минимум:

```text
INSTALL.md
scripts/install-backend-host.sh
scripts/uninstall-backend-host.sh
scripts/verify-backend-host.sh
scripts/provision-client.sh
scripts/install-client.sh
scripts/verify-client.sh
scripts/bootstrap-dev-access.sh   # если используется narrow dev/test bridge
```

`INSTALL.md` должен описывать два независимых шага:

### A. Backend deployment

- prerequisites;
- persistent paths и `findmnt`/`df`;
- Docker/Compose;
- secrets/provider subscriptions;
- firewall;
- Compose deployment;
- pinned Mihomo image/version;
- GEO data;
- loopback/docker-gateway bindings;
- client provisioning;
- host acceptance harness;
- rollback/uninstall.

### B. Client installation in each execution environment

Для каждого client profile:

1. установить одинаковые `egressctl` + `egress-run` binaries/package;
2. положить environment-specific `/etc/vibelab-egress/client.yaml`;
3. предоставить отдельный API token и default-proxy auth как read-only secrets;
4. установить Skill, если environment agent-capable;
5. выполнить `egressctl config validate`;
6. выполнить `egressctl whoami`;
7. выполнить `egressctl doctor`/smoke tests.

Production container installation должна быть reproducible: предпочтительно bake client package + Skill в image либо подключать их детерминированным read-only artifact/mount. Не полагаться только на ручную установку в writable container layer, которая исчезнет при recreate.

Provisioning tool должен уметь генерировать отдельные client bundles/config templates для host и container без provider secrets.

# 33. Особое ограничение: разработчик находится внутри container

Coding/test agent предполагается запущенным **изнутри уже работающего Docker container**.

Он должен разрабатывать и тестировать local client именно из этой среды.

Implementation plan не может предполагать, что агент имеет:

- host root;
- host `systemctl`;
- Docker socket;
- host network namespace;
- доступ к `/etc` host;
- Mihomo admin API.

## 33.1. Что агент должен уметь делать самостоятельно из container

После первоначального bootstrap/provisioning агент должен уметь:

- редактировать repository в shared workspace;
- запускать unit/parser/config-generation tests;
- устанавливать/обновлять dev version local client package внутри container;
- валидировать container `client.yaml`;
- выполнять `egressctl whoami/status`;
- обращаться к public `egressd` API через `egressctl` и напрямую в API parity tests;
- обращаться к authenticated SOCKS endpoints;
- читать/менять только свой default route/mode;
- запускать local `egress-run` и убеждаться, что process остаётся внутри container runtime;
- запускать concurrent one-shot proxy tests;
- обновлять providers через public API;
- запускать domain probes/benchmarks;
- проверять HARD DIRECT/LiteLLM connectivity;
- проверять, что host client default не меняется его действиями (через host acceptance report или dedicated cross-client assertion);
- читать безопасные host-test reports.

## 33.2. Host-side operations

Основной runtime egress-системы находится в Docker Compose stack `vibelab-egress`.

Host privilege требуется для:

- первоначального backend bootstrap;
- Docker Compose deploy/redeploy;
- firewall/persistent dirs;
- публикации loopback/docker-gateway ports;
- Docker-CIDR synchronization;
- host client provisioning/install;
- host acceptance harness;
- local client TUN namespace and static packet engine.

Mihomo и `egressd` не устанавливаются как постоянные host daemons.

Один раз администратор вручную выполняет bootstrap либо использует заранее установленный narrow deployment/test interface.

После этого container agent может:

- validate staged backend/client bundle;
- request deploy/redeploy только `vibelab-egress` из фиксированного path;
- получить filtered status/logs;
- запустить фиксированный host acceptance harness;
- sync Docker CIDRs;
- request deterministic install/update client artifact в заранее разрешённую target environment, если такой helper реализован.

Запрещено:

- arbitrary shell command на host;
- arbitrary compose path/container management;
- arbitrary file copy в `/etc`;
- Docker socket passthrough;
- generic host exec ради выполнения container workload.

Для PoC допустим ручной administrator loop: агент подготавливает artifacts/instructions, пользователь выполняет host-only commands и возвращает structured test report. Production acceptance требует воспроизводимого installer/harness.

# 34. Host acceptance harness

Нужен root-owned фиксированный test harness, например:

```text
egress-host-test
```

Он выполняет только заранее определённые проверки и выдаёт JSON report без secrets:

- socket binds/public exposure/firewall;
- Compose health;
- raw Mihomo API reachability policy;
- Docker CIDR bypass artifact freshness;
- persistent directories/permissions;
- image versions/digests;
- restart/recreate checks;
- host client config/token presence and permissions;
- host `egressctl whoami` identity;
- host current default route/mode;
- per-client isolation assertion: тестовое изменение container default не меняет host default;
- client-local namespace, TUN fd handoff, capability drop и seccomp policy.

container agent может запросить только фиксированный harness/report через narrow dev/test mechanism; harness не принимает arbitrary shell command.

# 35. Docker/internal CIDR synchronization

Egress stack не получает Docker socket.

Host helper:

```text
egress-hostctl sync-docker-cidrs
```

собирает Docker networks через host Docker CLI/API и пишет только generated CIDR artifact.

Trigger:

- install;
- Docker restart;
- systemd timer;
- explicit narrow admin action.

# 36. Network exposure

Public client-facing services доступны только через trusted local paths:

```text
host clients:      127.0.0.1:<ports>
container clients: docker gateway / explicitly allowed local interface:<ports>
```

Нельзя bind на public IP / unrestricted `0.0.0.0` без explicit firewall/interface restriction.

Минимум:

- authenticated default SOCKS;
- authenticated one-shot SOCKS;
- public narrow `egressd` API.

Mihomo admin API — только private Compose network.

UFW/nftables должны разрешать container clients только с нужных local subnets/interfaces.

Network reachability сама по себе не заменяет application auth: control API и proxy endpoints должны иметь client credentials.

# 37. Secrets

Рекомендуемый layout backend:

```text
/etc/vibelab-egress/secrets/
  mihomo_api_secret
  providers/
    blanc_subscription
    xeovo_subscription
    custom_vless
  clients/
    host-main.token
    container-main.token
```

Client environment получает только свой API token и отдельный default-proxy credential; one-shot credentials/capabilities выдаются через public API и не являются provider secrets.

Примеры client-side:

```text
host:
  /etc/vibelab-egress/client.yaml
  /etc/vibelab-egress/client.token
  /etc/vibelab-egress/default-proxy.auth

container:
  /etc/vibelab-egress/client.yaml
  /run/secrets/vibelab_egress_client_token
  /run/secrets/vibelab_egress_default_proxy_auth
```

Permissions `0700/0600` или эквивалентные read-only secret mounts.

Provider/Mihomo secrets не должны попадать:

- в client configs;
- в Git;
- в tar/zip artifacts;
- в обычные logs;
- в public API responses;
- в `docker inspect` environment, если можно использовать mounted files.

Client token внутри container не считается тайной от code самого container; его permissions должны быть безопасны для всего trust domain этого container.

# 38. Persistence

Persistent root configurable и должен находиться на достаточно большом filesystem.

Пример:

```text
/var/lib/vibelab-egress/
  mihomo/
  providers/
  generated/
  egressd/
    metrics.sqlite
  geo/
```

Перед install проверять `findmnt -T` и `df -h`.

# 39. Metrics DB

SQLite достаточно.

Таблицы/сущности:

- client profile metadata без raw secrets;
- route metadata;
- health/latency samples;
- domain probe samples;
- benchmark samples;
- provider update events;
- per-client default selection events;
- one-shot jobs/sessions с `client_id`;
- GEO verification cache;
- schema version.

Provider/client raw secrets в DB не копировать, если для работы достаточно references/hashes.

# 40. Observability

`egressd` logs — structured JSON.

Audit events должны включать authenticated `client_id`, когда операция client-initiated:

- provider update;
- default route/mode change;
- probe-domain;
- benchmark;
- one-shot start/end;
- GEO update;
- reconciliation fallback to direct.

`egressctl doctor` проверяет относительно текущего client profile:

1. local config parse/permissions;
2. API/auth;
3. `/v1/self` identity совпадает с expected `client_id`;
4. API/client version compatibility;
5. Mihomo/backend readiness;
6. current default route/mode;
7. default proxy data plane;
8. one-shot direct;
9. one-shot proxy route;
10. Docker/HARD DIRECT bypass where applicable;
11. provider inventory;
12. GEO data freshness;
13. optional domain probe;
14. local strict/TUN capability;
15. отсутствие silent fallback.

Doctor не должен печатать API/proxy credentials.

# 41. GEO data update

Команды:

```bash
egressctl geo status
egressctl geo update
```

Допускается нативный Mihomo auto-update + explicit API-triggered update.

`geo status` должен показывать:

- source URLs;
- last update time;
- file/version/hash, если доступно;
- staleness;
- наличие GeoIP/GeoSite/ASN datasets.

# 42. Admin UI

MetaCubeXD/Zashboard допустим только как admin convenience.

Он не является частью agent API.

Если установлен:

- bind только на host loopback;
- full Mihomo secret не доступен container;
- UI может быть отключён без влияния на agent workflows.

# 43. MCP

MCP — optional Phase 3 и не заменяет local client package.

Если реализуется, он является thin wrapper над тем же public `egressd` API и действует от имени отдельного client profile/credential либо явно выбранного local profile:

```text
egress_status
egress_routes
egress_get_default
egress_set_default
egress_update_provider
egress_test
egress_probe_domain
egress_best
egress_benchmark
```

MCP:

- не реализует networking engine;
- не получает Mihomo admin secret/provider secrets;
- не предоставляет возможностей шире public API/`egressctl`;
- не запускает arbitrary shell command;
- не заменяет `egress-run` для local workload execution.

Если агент имеет local shell и Skill, preferred path остаётся `egressctl`/`egress-run`.

# 44. Repository layout

```text
vibelab-egress/
  TECH_SPEC.md
  README.md
  INSTALL.md
  compose.yaml
  scripts/build-tun-engine.sh
  scripts/remove-legacy-tun-helper.sh

  mihomo/
    config.template.yaml
    generated/
    providers/

  egressd/
    pyproject.toml
    src/egressd/
    tests/

  client/
    pyproject.toml
    src/vibelab_egress_client/
      egressctl.py
      egress_run.py
      config.py
      api.py
    tests/
    examples/
      client.host.yaml
      client.container.yaml

  adapters/
    blanc/
    xeovo/

  host/
    egress-hostctl
    egress-host-test
    client/tun-engine/
    units/

  skills/
    egress/
      SKILL.md

  scripts/
    install-backend-host.sh
    uninstall-backend-host.sh
    verify-backend-host.sh
    provision-client.sh
    install-client.sh
    verify-client.sh
    bootstrap-dev-access.sh

  tests/
    fixtures/
    integration/
    security/
```

Не должно быть двух независимых реализаций CLI для host и container: это один client package с разными configs/capabilities.

# 45. Phase 0 — обязательный PoC

До production implementation проверить:

1. реальный subscription format BlancVPN;
2. реальный subscription format Xeovo;
3. native Mihomo provider compatibility;
4. минимальный adapter, если нужен;
5. custom VLESS end-to-end;
6. WireGuard route через Mihomo;
7. provider manual update API;
8. provider health/delay API;
9. создание минимум двух per-client selector groups (`host-main`, `container-main`);
10. независимое query/set selector для каждого client;
11. `direct` в каждом client selector;
12. authenticated default proxy identity -> correct client selector;
13. one-shot `IN-USER`/equivalent route to concrete proxy;
14. **три concurrent one-shot через разные routes**, включая `direct`;
15. one-shot не меняет ни один client default;
16. generated client×route mapping масштабируется на фактические несколько сотен routes либо найден лучший concurrency-safe native mechanism;
17. hard Docker CIDR bypass на default и one-shot listeners;
18. GEO data download/update and GEOIP/ASN matching;
19. domain probe через direct + минимум две locations;
20. Mihomo admin API остаётся private;
21. provider route disappearance: only affected client fallback to direct;
22. один и тот же client package работает на host и container с разными configs;
23. `/v1/self` correctly binds token -> client identity;
24. попытка подменить `client_id` не позволяет управлять другим profile;
25. `egressctl` public operations воспроизводимы direct API calls с теми же permissions;
26. `egress-run` внутри container запускает child внутри Docker container, а не на backend/host;
27. proxy mode end-to-end из container;
28. proxy mode end-to-end с host client;
29. provider update через explicit working `--via ROUTE` без shared selector race;
30. проверить local strict/TUN backend на host и generic Docker container; зафиксировать обязательные runtime capabilities и их fail-closed поведение.

Результат: `POC_REPORT.md` с версиями, configs, measured outcomes и решениями.

# 46. План coding-агентов

## A0 — Architecture/PoC Agent

**Стартует первым и блокирует production implementation.**

Задачи:

- выполнить Phase 0;
- проверить pinned/current Mihomo docs/version;
- проверить provider/protocol formats;
- доказать per-client selectors/default isolation;
- доказать concurrency-safe route-addressable one-shot;
- проверить реальный масштаб client×route mappings;
- проверить GEO assets/domain probe;
- проверить host/container client configs;
- доказать local-execution semantics `egress-run`;
- verify client-local nested user/network namespace support on host and ordinary Docker containers.

Skills/компетенции:

- Linux/Docker/container runtime networking;
- Mihomo/Clash Meta;
- WireGuard/VLESS/SOCKS;
- HTTP/TLS diagnostics;
- process/network namespace semantics.

A0 работает преимущественно из container; host-only assertions выполняются через administrator/harness.

Deliverable: `POC_REPORT.md` + предложенные corrections TECH_SPEC, если PoC опровергнет предположения.

---

## A1 — Deployment/Bootstrap Agent

После A0.

Задачи:

- Compose backend stack;
- persistent dirs/secrets/firewall;
- host backend installer;
- client provisioning;
- install одного и того же client package на host/container;
- генерация разных `client.yaml`/tokens;
- uninstall/rollback;
- Docker CIDR sync;
- narrow dev/test bridge;
- host acceptance harness.

Обязательные проверки:

- никаких public binds;
- Docker socket не смонтирован;
- client tokens раздельны;
- backend secrets отсутствуют в clients;
- container не получает arbitrary host command;
- recreate container не теряет production client installation/Skill.

---

## A2 — Mihomo/Providers/GEO Agent

После A0, параллельно A1.

Задачи:

- config template;
- native providers/adapters;
- per-client selector groups;
- default listener + client identities;
- route-addressable one-shot listener;
- `IN-USER`/equivalent rules;
- DIRECT bypass;
- GeoIP/GeoSite/ASN;
- GEO auto-update.

Проверить:

- config validation;
- node collisions;
- client selector isolation;
- direct route;
- concurrent one-shot;
- mapping scale;
- GEO update;
- Docker bypass precedence.

---

## A3 — egressd Agent

После A0; contracts согласуются с A1/A2.

Задачи:

- public narrow API;
- token -> client profile auth;
- `/self`/capabilities;
- Mihomo API client;
- stable route IDs/catalog;
- per-client default route/mode;
- provider reconcile;
- one-shot sessions/capabilities;
- update-via-route;
- domain probe/benchmark metadata;
- metrics DB/audit/GEO enrichment.

Обязательные tests:

- client impersonation denied;
- one client cannot change another default;
- concurrent sessions;
- route deletion during job;
- provider update races;
- secret redaction;
- per-client fallback to direct;
- API has no remote exec/raw Mihomo passthrough;
- domain probe error classification.

---

## A4 — Local Client Package Agent

После A3 API stabilization.

Задачи:

- единый package с `egressctl` и `egress-run`;
- config discovery/validation;
- host/container endpoint differences только через config;
- `/self` identity verification;
- human output + JSON;
- stable exit codes;
- local proxy execution;
- signal/exit-code handling;
- doctor;
- public API parity tests.

Проверить и на host, и изнутри container:

- один и тот же release/package;
- разные configs;
- correct `whoami`;
- independent defaults;
- local child process remains in expected execution environment;
- `direct`/VPN one-shot;
- concurrent invocations;
- LiteLLM/internal connectivity.

---

## A5 — Probe / Benchmark / Ranking Agent

После A2/A3, может идти параллельно A4 после freeze contracts.

Задачи:

- domain probe engine;
- bounded benchmark;
- `best` ranking;
- route update-via health workflows;
- GEO explanation metadata.

Обязательные checks:

- direct baseline;
- parallel probes with cap;
- DNS/TCP/TLS/HTTP classification;
- bounded bytes/time;
- `best --domain` uses actual probe.

---

## A6 — Strict/TUN Agent

Выполнено в client-local per-invocation backend.

Задачи/контракт:

- nested user/network namespace и новый TUN на каждый вызов;
- отдельный packet engine получает TUN fd и route-specific SOCKS credentials;
- `direct + tun` и разные route в параллельных запусках;
- workload остаётся в host или исходном Docker-контейнере;
- очистка engine, DNS forwarder и namespace после завершения/timeout;
- ordinary Docker profile разрешает только фиксированную пару namespace flags;
- fail-closed без fallback в proxy.

Критические запреты:

- no container runtime disable/runc fallback;
- no Docker socket to container;
- no generic remote host exec;
- no shared mutable one-shot selector;
- no silent proxy fallback.

---

## A7 — Agent Skill / Documentation Agent

После A4/A5; Skill обязателен для v1.

Задачи:

- `skills/egress/SKILL.md`;
- README user + agent sections;
- host/container client install/config documentation;
- usage examples/decision tree;
- optional MCP only after CLI.

Проверить на реальном container agent:

- `whoami`/capabilities;
- current/set own default;
- direct/VPN one-shot;
- concurrent one-shot;
- domain probe across routes;
- provider update + `--via` recovery;
- proxy/TUN choice;
- agent не пытается использовать raw Mihomo API/host root.

---

## A8 — Independent Security/Integration Review Agent

Последний mandatory gate. Не должен быть автором ключевых компонентов.

Задачи:

- adversarial API review;
- direct API calls bypassing CLI;
- client impersonation/default isolation;
- port exposure/auth;
- secret scan;
- routing bypass verification;
- concurrency races;
- malformed provider input;
- bootstrap/helper privilege review;
- host acceptance report review.

Особенно проверить:

- public API не мощнее documented egressctl control surface;
- container может обойти CLI, но от этого не получает дополнительных privileges;
- container не получает host shell/Docker socket/provider/Mihomo secrets;
- host/container defaults независимы;
- internal Docker/HARD DIRECT остаются DIRECT;
- parallel one-shot не влияют друг на друга;
- `egress-run` исполняет command локально;
- LiteLLM остаётся доступен;
- public NIC ports закрыты;
- real secrets отсутствуют в Git/artifacts/logs.

# 47. Порядок работ

```text
A0 Architecture/PoC
 |
 +--> architecture review / TECH_SPEC corrections
 |
 +--> A1 deployment + client provisioning -------+
 |                                               |
 +--> A2 Mihomo/providers/per-client routing ----+--> A3 egressd/API
                                                       |
                                                       +--> A4 local client package
                                                       |
                                                       +--> A5 probe/benchmark/ranking
                                                              |
                                                              +--> A7 Skill/docs

Proxy-mode acceptance (host + container)
 |
 +--> A6 strict/TUN implementation
 |
 +--> A8 independent security/integration gate
```

A1/A2 могут идти параллельно после A0.
A4/A5 могут частично идти параллельно после freeze public API/session contracts.
A7 Skill является частью v1 release, не optional documentation.
A8 проверяет proxy и client-local TUN boundaries.

# 48. Testing strategy изнутри container

Поскольку основной coding/test agent находится внутри container, обязательны реальные integration tests из этой environment.

Минимальный набор:

1. local client package installed and version reported;
2. `egressctl config validate`;
3. `egressctl whoami` -> `container-main`;
4. direct API `/v1/self` returns same identity/capabilities;
5. попытка spoof другого `client_id` не меняет identity;
6. `egressctl route current`;
7. `egressctl route set direct`;
8. `egressctl route set <VPN>`;
9. host client default остаётся неизменным (через host harness assertion);
10. default proxy exit IP соответствует container default;
11. `egress-run direct -- ...` выполняет child внутри Docker container;
12. `egress-run <VPN> -- ...` выполняет child внутри Docker container;
13. 3+ concurrent one-shot routes, один `direct`;
14. provider update;
15. update blocked provider via working `--via ROUTE` where fixture/environment permits;
16. route list/reconciliation;
17. health test;
18. `probe-domain` direct + multiple routes;
19. bounded benchmark shortlist;
20. Docker internal endpoint remains reachable DIRECT;
21. LiteLLM continues to work during default switch/provider update/concurrent one-shot;
22. raw Mihomo API fails from container;
23. direct public API gives no capability beyond egressctl;
24. container cannot operate on host-main default;
25. Generic Docker `--mode tun` acceptance proves child processes stay local and per-invocation namespace routing works through the TUN fd;
26. host acceptance harness report passes.

# 49. Acceptance criteria — v1 proxy mode

Release принимается только если:

1. Mihomo version/image pinned.
2. BlancVPN loads natively or via documented adapter.
3. Xeovo loads natively or via documented adapter.
4. Custom VLESS works end-to-end.
5. At least one WireGuard route works through Mihomo.
6. `direct` exists in route catalog and works as default/one-shot.
7. Host и container используют один и тот же client package release.
8. Host и container имеют разные configs/tokens/endpoints where needed.
9. `egressctl whoami` returns authenticated profile, not blindly local `client_id`.
10. Defaults are per-client; changing container default does not change host default.
11. Each client can query actual current route/mode.
12. Disappearing selected route falls back to direct only for affected clients.
13. `egressctl` control operations use documented public API only.
14. Direct API calls with same token have same privileges as egressctl.
15. Public API has no raw Mihomo passthrough, arbitrary config patch, host exec or client provisioning.
16. Provider update works and failed update preserves last-known-good.
17. Update via explicit working route works without changing shared/client defaults.
18. Health/delay tests work.
19. GEO data update/status and exit country/ASN enrichment work where available.
20. `probe-domain DOMAIN --all` compares multiple routes including direct and distinguishes DNS/TCP/TLS/HTTP failures.
21. Default proxy uses authenticated client's selected default route.
22. One-shot explicit route does not change any default.
23. At least three simultaneous one-shot jobs through different routes work without interference.
24. One concurrent job may use `direct`.
25. `egress-run` target command executes locally in caller environment, not backend/host remote exec.
26. Exit status/signals are preserved adequately.
27. Docker-internal destination is DIRECT via default and one-shot proxy for container client.
28. Operator-defined HARD DIRECT destination remains direct.
29. LiteLLM remains reachable during route changes/provider updates/concurrency.
30. Raw Mihomo API unavailable from container/ordinary clients.
31. Agent API and proxy endpoints authenticated.
32. Public NIC cannot reach client-facing ports.
33. Docker socket absent from egress stack/container.
34. Provider secrets absent from client configs/Git/API/logs/docker inspect env/artifacts.
35. Benchmark bounded by bytes/time.
36. `best --domain` uses actual domain probe, not GEO heuristics only.
37. Skill installed and real agent completes documented workflows using local CLI.
38. Backend + separate client install instructions reproduce deployment.
39. container client install survives/reproduces container recreate by documented mechanism.
40. Coding agent inside container can run integration suite without host root.
41. Host-only assertions exposed through fixed harness, not generic host shell.
42. API/client version mismatch is detected with actionable error/capability negotiation.

# 50. Acceptance criteria — Phase 2 TUN

Дополнительно:

1. `egress-run --mode tun ROUTE -- command` works for proxy-unaware TCP client on host and generic Docker container.
2. Command remains in the calling environment with caller filesystem/environment and mapped caller identity.
3. UDP/QUIC test works when route/protocol/backend supports it.
4. Concurrent host TUN one-shot jobs through different routes work independently.
5. `direct + tun` works as isolated direct path.
6. No generic root escalation/remote exec.
7. stale netns/TUN resources cleaned.
8. HARD DIRECT rules remain enforced where applicable.
9. Client container processes remain in their isolated container for every invocation.
10. The packet engine receives only a TUN fd and bounded selected-route credentials; it never receives argv/env/cwd/stdio or launches a workload.
11. No Docker socket or host root is granted to the client container.
12. No silent fallback from `tun` to `proxy`.
13. `egressctl whoami/capabilities` accurately reports mode availability.

# 51. Upstream references to use during implementation

Coding agents должны сверяться с актуальной upstream документацией перед реализацией:

- Mihomo docs: `https://wiki.metacubex.one/`
- Mihomo API: `https://wiki.metacubex.one/en/api/`
- Proxy providers: `https://wiki.metacubex.one/en/config/proxy-providers/`
- Proxy groups: `https://wiki.metacubex.one/en/config/proxy-groups/`
- Routing rules (`IN-USER`, `IN-NAME`, `GEOIP`, ASN): `https://wiki.metacubex.one/en/config/rules/`
- Inbounds/listeners/TUN: `https://wiki.metacubex.one/en/config/inbound/`
- General authentication/listeners: `https://wiki.metacubex.one/en/config/general/`
- TUN details: `https://wiki.metacubex.one/en/config/inbound/tun/`
- Rule providers: `https://wiki.metacubex.one/en/config/rule-providers/`
- GEO data source: `https://github.com/MetaCubeX/meta-rules-dat`

ТЗ фиксирует requirements и invariants; точная syntax Mihomo должна проверяться по pinned version и integration tests.

# 52. Личные профили и источники

1. На host каждый локальный UID, включая системный аккаунт без домашнего каталога, получает профиль `host-uid-<uid>` при первом вызове клиента. Доступ к локальному enrollment socket разрешён всем UID; служба определяет UID через `SO_PEERCRED` и не принимает UID или client ID из запроса. Клиент получает учётные данные в памяти, без записи в home пользователя. Все вызовы `egressctl` и `egress-run` работают от исходного UID без sudo.
2. Глобальные providers и установка остаются под управлением администратора. Обычный профиль может создать личный provider из URL подписки, локального файла или одной proxy URI через `egressctl source add` (`--url`, `--file`, `--uri`). Он видит и использует глобальные и свои маршруты, не видит маршруты и источники других профилей. Server-side ACL применяется к спискам, выбору default, тестам, probes, benchmark, обновлению providers и one-shot сеансам. Группа Mihomo для постоянного default содержит только глобальные и собственные providers.
3. Для host-профиля исходная подписка хранится в backend, под правами служебного пользователя, и обновляется там. Для container-профиля исходный URL или файл хранится **внутри контейнера** в личном каталоге пользователя с режимом 0700/0600. Контейнер сам получает и нормализует подписку и передаёт backend только YAML-снимок узлов. Backend не читает, не обновляет и не удаляет исходные файлы/URL контейнера; `egressctl source refresh` выполняется внутри контейнера. Нормализация в контейнере использует optional adapter и PyYAML; базовый клиент остаётся stdlib-only.
4. API личных providers: `GET/POST /v1/personal/providers`, `PUT /v1/personal/providers/{alias}/snapshot`, `POST /v1/personal/providers/{alias}/refresh`, `DELETE /v1/personal/providers/{alias}`. Host вызывает POST/refresh; контейнер вызывает snapshot. Повторный `source add` с теми же alias, типом и исходным URL, URI или содержимым файла возвращает успех с `skipped: true`, без повторной загрузки и регенерации. Host `source refresh` пересоздаёт provider из сохранённого URL или URI. Alias и owner выводятся из аутентифицированного профиля. Секреты исходника не выдаются через API.
5. Личный proxy endpoint и URL подписки должны разрешаться только в публичные адреса. При импорте узлы с непубличным или неразрешимым адресом сервера пропускаются; API и CLI сообщают их число. Если пригодных узлов не осталось, импорт отклоняется. Для DNS-имён проверенный публичный IP закрепляется в конфигурации узла, чтобы Mihomo не разрешал имя повторно при соединении. Размер исходника и нормализованного снимка ограничен 4 MiB; отдельного верхнего предела числа узлов нет. Число личных providers — 32 на профиль. Удаление и изменение выполняются с регенерацией конфигурации и откатом при ошибке reload.
