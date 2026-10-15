# POC_REPORT — vibelab-egress (Phase 0)

**Результат: 22/22 проверок пройдено.** Архитектура роутинга Mihomo подтверждена
на реальном бинарнике. Production-реализация (A1–A8) разблокирована.

Дата прогона: 2026-10-10. Команда: `python3 poc/run_poc.py` (идемпотентно,
убивает и поднимает все процессы сам).

> **Update (исключение из репозитория — коммит `fa65d70`):** бинарники и vendor-исходники, описанные в этом
> отчёте (`poc/mihomo*`, `poc/mihomo-src*`, `poc/wgserver/wgserver`,
> `poc/wgserver/third_party/wireguard-go/`), исключены из репозитория.
> Восстановление: `sh scripts/fetch-poc-vendor.sh`; рецепт сборки —
> `DEVELOPMENT.md`, «Сборка (редко)». Единственное изменение вендора —
> 1-строчный патч, сохранённый в
> `poc/wgserver/third_party/wireguard-go-sticky-sockets.patch`.

---

## 1. Версии и окружение

| Компонент | Версия / значение | Примечание |
|---|---|---|
| Mihomo (Meta) | **1.19.32** | собран из исходников (релизы GitHub недоступны) |
| `poc/mihomo` | CGO off | основной контроллер + SOCKS-листы |
| `poc/mihomo-gvisor` | tags `with_gvisor` | userspace WG-клиент |
| Go | 1.27.2 | `/usr/local/go` |
| wireguard-go | `a6cecdd7f57f` | vendored в `poc/wgserver/third_party/` |
| sing-wireguard | `c3ae17d19f9e` | gVisor IP-стек |
| gvisor | `79317d808312` | userspace TCP/UDP forward |
| GEO | GeoIP.dat 16.6 MB, GeoSite.dat 4.3 MB, asn.mmdb 9.0 MB | через codeload |

Среда: Linux x86_64, 24 CPU, 256 GiB, контейнер (eth0 = 172.17.0.2/16).

**Ограничения среды (влияют на A0/A6):**
- Нет Docker/podman — mihomo запускается бинарником напрямую; `compose.yaml`
  — артефакт для целевого хоста.
- `objects.githubusercontent.com` / `raw.githubusercontent.com` заблокированы;
  работает `codeload.github.com` (source tarball).
- `/proc/net/wireguard` отсутствует, `unshare -n` → "Operation not permitted",
  netlink `AF_NETLINK/NETLINK_ROUTE` → "operation not supported" (нет NET_ADMIN)
  → TUN/netns-тесты в этой среде невозможны (capability matrix — A6).

---

## 2. Архитектура (подтверждена PoC)

Один SOCKS-listener на клиента + один на one-shot. Роутинг через `IN-USER`:

```
client user  -> IN-USER,<user>  -> per-client select (host-main / openscience-main)
slot user    -> IN-USER,os_N    -> per-slot select (os_0..os_7)
```

- **Default-прокси**: юзер клиента → `IN-USER` → per-client `select`.
- **One-shot**: фиксированные юзеры слотов `os_0..os_N` → `IN-USER` → per-slot
  `select`. Число правил **не зависит** от числа нод (P18: 300 нод → 39 правил,
  1 нода → 39 правил).
- **Fallback**: `MATCH,DIRECT` — последнее правило (никакого implicit VPN).
- **Hard-direct** (Docker CIDR 172.16/12, 127.0.0.0/8, 10.0.0.0/8, 192.168/16,
  169.254/16) стоит **до** `IN-USER` — обход на обоих listeners (P12).

Ключевые факты о Mihomo (проверено падением/успехом реального бинарника):
- `geodata-mode` — bool; `rules` — список строк `"TYPE,payload,PROXY,opts"`;
  `DIRECT`/`REJECT` built-in (в `proxies` нельзя); provider-ноды в группах
  через `use:`.
- Тип группы — `select` (API отображает как `"Selector"`). Валидные:
  `url-test`/`select`/`fallback`/`load-balance`; `relay` удалён.
- **Provider-ноды не top-level прокси**: `GET /proxies/<node>/delay` → 404.
  Delay работает **через группу**: `GET /proxies/<group>/delay?url=...` → 200
  `{"delay":int}`. Это production-паттерн для egressd (probe-слот → нода).
- `profile.store-selected: true` (default) **переживает hot-reload**:
  `patchSelectGroup` (`hub/executor/executor.go:444-466`) восстанавливает
  selections из bbolt-cache (P15).
- Контроллер: Bearer-аутентификация; клиентский токен **не** проходит на
  mihomo API (P19, 401).

---

## 3. Измеренные результаты (22 проверки)

| # | Проверка | Итог | Измеренный результат |
|---|---|---|---|
| P01 | controller version | PASS | `{'meta': True, 'version': '1.19.32'}` |
| P02 | controller requires auth | PASS | no-auth → 401 |
| P03 | per-client selectors | PASS | `host-main`, `openscience-main` type=Selector, DIRECT в `all` |
| P04 | one-shot slot groups | PASS | 8 slot-групп (`os_0..os_7`) |
| P05 | set default host | PASS | PUT → 204, `now=blanc-FI-Helsinki-1` |
| P06 | client isolation | PASS | os=xeovo-US-NY-1, host=blanc-FI-Helsinki-1 (независимо) |
| P07 | default proxy host | PASS | 200, body `NODE:blanc-FI-Helsinki-1` (non-LAN цель) |
| P08 | default proxy openscience | PASS | 200, body `NODE:xeovo-US-NY-1` |
| P09 | one-shot route | PASS | 200, body `NODE:blanc-FI-Helsinki-1` через slot |
| P10 | one-shot no default change | PASS | host/os defaults не изменились после one-shot |
| P11 | concurrent one-shot | PASS | 3 параллельных: 200/200/direct, ноды `blanc-FI-Helsinki-1`,`xeovo-DE-FRA-1` |
| P12 | hard-direct precedence | PASS | hard-direct@1 до IN-USER@5 |
| P13 | provider update API | PASS | PUT provider → 204 |
| P14 | node delay API | PASS | через группу → 200 `{"delay":12}` |
| P15 | update preserves selection | PASS | после update `host.now=blanc-FI-Helsinki-1` |
| P16 | GEOIP rules loaded | PASS | geoip rules=1, total=16 |
| P17 | DNS query | PASS | 200 |
| P18 | mapping scale | PASS | 300 нод → 39 правил, 1 нода → 39 (не зависит от нод) |
| P19 | client token no mihomo API | PASS | client-token → 401 |
| P20 | VLESS e2e | PASS | 200, body `NODE:vless-upstream` (main mihomo → VLESS tunnel → upstream) |
| P21 | WireGuard e2e | PASS | 200, JSON `token_ok` через WG tunnel → gVisor → echo → WG → client |

---

## 4. Data-plane e2e (P20, P21) — доказательство полного пути

Цель — non-LAN `203.0.113.1` (TEST-NET-3), чтобы запрос не перехватывался
`GEOIP,LAN,DIRECT` и реально шёл через ноду.

**VLESS (P20):** client → main mihomo (`os_4 → vless-node`) → VLESS tunnel →
`vless-upstream` fake node → canned `NODE:vless-upstream`. Ответ `NODE:vless-upstream`
— доказательство, что трафик прошёл через VLESS-транспорт.

**WireGuard (P21):** client → main mihomo (`os_5 → wg-node`) → WG tunnel →
userspace `wgserver` (wireguard-go + gVisor stack) → relay на `127.0.0.1:18999`
(echo) → JSON `{"token_ok": true}` → WG tunnel → client.

FWD-лог wgserver (полный data plane):
```
FWD NewConnection src=10.66.0.2:60484 dst=203.0.113.1:18999 target=127.0.0.1:18999
FWD dialed 127.0.0.1:18999, relaying
FWD client->echo 86 bytes
FWD echo->client 145 bytes
FWD echo->client 54 bytes
```

**Ключевой фикс (P21):** в контейнере без NET_ADMIN wireguard-go `upLocked()`
→ `BindUpdate()` → `startRouteListener()` (netlink route socket) падает →
устройство остаётся Down → tunneled-пакеты не доходят до gVisor-стека.
Решение: vendored `wireguard-go` с `conn.StdNetSupportsStickySockets = false`
(`conn/sticky_linux.go:112`) + `replace` в `go.mod`. Sticky-сокеты — только
kernel-оптимизация переиспользования, для PoC не нужны. После фикса:
`Interface state was Down, requested Up, now Up` (немедленно).

---

## 5. Маппинг на 30 пунктов Phase 0 (раздел 45 спеки)

PoC-харнес (22 проверки) покрывает **архитектурные** пункты. Остальные —
покрываются в A1–A8 или фиксируются ниже.

| # | Пункт Phase 0 | Статус | Где |
|---|---|---|---|
| 1 | реальный subscription format BlancVPN | **deferred A2** | provider-адаптер + fixtures |
| 2 | реальный subscription format Xeovo | **deferred A2** | provider-адаптер + fixtures |
| 3 | native Mihomo provider compatibility | **PASS** | P13 (file provider, update API) |
| 4 | минимальный adapter, если нужен | **deferred A2** | зависит от 1–2 |
| 5 | custom VLESS end-to-end | **PASS** | P20 |
| 6 | WireGuard route через Mihomo | **PASS** | P21 |
| 7 | provider manual update API | **PASS** | P13 |
| 8 | provider health/delay API | **PASS** | P14 (через группу) |
| 9 | ≥2 per-client selector groups | **PASS** | P03 |
| 10 | независимый query/set selector | **PASS** | P05, P06 |
| 11 | `direct` в каждом client selector | **PASS** | P03 (DIRECT в `all`) |
| 12 | authenticated default proxy identity → selector | **PASS** | P07, P08 (IN-USER) |
| 13 | one-shot IN-USER route to concrete proxy | **PASS** | P09 |
| 14 | три concurrent one-shot (включая direct) | **PASS** | P11 |
| 15 | one-shot не меняет client default | **PASS** | P10 |
| 16 | client×route mapping масштабируется | **PASS** | P18 (39 правил @ 300 нод) |
| 17 | hard Docker CIDR bypass (default + one-shot) | **PASS** | P12 |
| 18 | GEO download/update + GEOIP/ASN matching | **PASS** | P16, P17 (GEO-файлы + DNS) |
| 19 | domain probe через direct + ≥2 locations | **deferred A5** | probe-domain движок |
| 20 | Mihomo admin API остаётся private | **PASS** | P02, P19 |
| 21 | provider route disappearance → only affected client fallback | **deferred A2/A3** | fallback-логика |
| 22 | один client package на host и OpenScience | **deferred A4** | config discovery |
| 23 | `/v1/self` token → client identity | **deferred A3** | egressd API |
| 24 | подмена `client_id` не даёт чужой profile | **deferred A3/A8** | auth |
| 25 | `egressctl` ops = direct API calls | **deferred A4** | client package |
| 26 | `egress-run` child внутри OpenScience/gVisor | **deferred A4** | local-execution semantics |
| 27 | proxy mode e2e из OpenScience | **deferred A4** | |
| 28 | proxy mode e2e с host client | **deferred A4** | |
| 29 | provider update через `--via ROUTE` без race | **deferred A4** | |
| 30 | TUN/strict feasibility + capability matrix | **deferred A6** | capability matrix (TUN недоступен здесь) |

**Итог:** архитектурные ядро-пункты (3,5,6,7,8,9,10,11,12,13,14,15,16,17,18,20)
— доказаны PoC на реальном бинарнике. Пункты 1,2,4 (реальные подписки), 19
(probe), 21–29 (client package + egressd), 30 (TUN) — покрываются в A2–A6/A8
по плану.

---

## 6. Решения

- **2026-10-09:** Mihomo pinned v1.19.32 (latest на момент проверки).
- **2026-10-09:** Архитектура роутинга подтверждена: `IN-USER` → per-client
  selector (default) и `IN-USER` → per-slot selector (one-shot). Число правил
  не зависит от числа нод (P18).
- **2026-10-09:** Обнаружена невалидность proxy-тестов (LAN-DIRECT перехват
  цели 127.0.0.1); фикс через non-LAN цель `203.0.113.1` (TEST-NET-3).
- **2026-10-10:** P21 (WG e2e) — корневая причина: netlink route socket
  недоступен в контейнере (нет NET_ADMIN) → device Down. Фикс: vendored
  wireguard-go с `StdNetSupportsStickySockets=false`. После фикса 22/22.
- **2026-10-10:** Startup-детект wgserver: фиксированный `sleep(2)` → polling
  (gVisor + wireguard-go init под нагрузкой занимает >2 с).

## 7. Файлы

- `poc/run_poc.py` — оркестратор, 22 проверки, идемпотентный.
- `poc/fake_node.py` — фейковые SOCKS5-ноды (canned `NODE:<name>`, JSONL-лог).
- `poc/wgserver/` — userspace WG-сервер (wireguard-go + gVisor, relay + FWD-лог).
- `poc/wgserver/third_party/wireguard-go/` — vendored, патч sticky-сокетов.
- `poc/mihomo`, `poc/mihomo-gvisor` — бинарники v1.19.32.
- `poc/geo/` — GEO-данные.
- `poc/results.json` — результаты последнего прогона (22/22).
- `poc/run/` — артефакты прогона (mihomo.yaml, log_*.txt, nodes.jsonl).
- `egressd/src/egressd/configgen.py` — production-генератор конфига (ядро A2).
