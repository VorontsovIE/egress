# Установка host-клиента после локального QA

Дата: 2026-10-19. Следующий инженер: host-клиент обновлён от имени root
через `pkexec` из текущего дерева. В `/usr/local` установлены
`egressctl`/`egress-run`, конфигурация `/etc/vibelab-egress/client.yaml`
указывает на проверенный профиль `host-uid-1000`. `egressctl --json whoami`
подтвердил профиль; `egress-run direct -- /usr/bin/true` прошёл. Каталог
конфигурации root-owned с группой пользователя, token и proxy auth имеют
права 0600 и владельца пользователя.

Полный acceptance установленного клиента выполнен дважды. Оба результата:
**18 PASS / 3 FAIL / 7 SKIP**; оба отчёта независимо дали `VERIFIED` с
успешной проверкой secret scan. FAIL повторились на одном маршруте
Hysteria2: TUN TCP не получил публичный exit IP, TUN UDP не получил ответ,
bounded benchmark не получил HTTP headers. До установки полный acceptance
был **23 PASS / 0 FAIL / 5 SKIP**. Это воспроизводится как состояние
конкретного маршрута; установленный CLI и backend аутентификация работают.
Не объявлять live acceptance полностью зелёным, пока узел не восстановится
или не будет выбран другой пригодный маршрут.

## Сравнение proxy и TUN

На одном и том же HTTPS URL в proxy и TUN проверены исходный и ещё два
Hysteria2-маршрута: **3/3** дали TLS EOF в обоих режимах (всего 6 запросов).
`egressctl test` для исходного маршрута вернул `mihomo_status=504`. Для
контроля маршрут Shadowsocks из предыдущего зелёного acceptance вернул
HTTP 200 как в proxy, так и в TUN. Значит, TUN работает на контрольном узле;
сбой локализуется в проверенных Hysteria2-маршрутах или их доступности.
Данные пока не позволяют разделить неисправность конкретных узлов и
несовместимость Hysteria2-конфигурации с серверной стороной.

## Текущая доступность Hysteria2 — 2026-10-19

`egressctl list --json` показал 21 Hysteria2-маршрут. Все 21 проверены
ограниченным `probe-domain` (parallel 8, timeout 10 секунд) на двух
независимых HTTPS целях: `www.cloudflare.com/cdn-cgi/trace` (ожидался 200)
и `www.google.com/generate_204` (ожидался 204). Обе проверки дали
**0/21 успехов, 21/21 tls_failure**. На момент прогона доступных
Hysteria2-маршрутов не обнаружено. Это результат HTTPS-проверок из текущей
среды, а не доказательство постоянной недоступности узлов.

Закрытые отчёты (0600), verifier `VERIFIED`, hashes:

- `~/.local/share/vibelab-egress-test/live-user-installed-client.json` —
  `886b27818e3fbeb2eedd06ec2f153845156dc7668647501a3e7266dd6474afce`
- `~/.local/share/vibelab-egress-test/live-user-installed-client-recheck.json` —
  `e99fbb4f25531733381b509b6320cad5625992491a19b5857de4abb58d5ca298`

Дальше повторить live acceptance после смены состояния маршрута; при
необходимости проверить выбранный маршрут через `egressctl test` и
`egressctl probe-domain`. Другие SKIP остаются обусловлены отсутствующим
OpenScience и root-only проверками.

# Handoff: локальные QA фиксы и повторная приёмка

Дата: 2026-10-19. Получатель — следующий инженер, продолжающий локальную
проверку. Четыре кодовых блока из исходного плана закоммичены отдельно:

1. `2379d91 fix(client): complete probe and benchmark CLI contracts`
2. `3734942 fix(egressd): bound probes and handle route timeouts`
3. `5a3aba2 fix(install): handle external cwd and host permissions`
4. `fd055f7 fix(acceptance): verify complete proxy results`

## Что сделано

- `probe-domain` клиента покрывает параметры §24: country/provider, явный
  список routes, parallel, timeout, scheme/port/path/method, expected status,
  stages и max-routes. Сохранены ACL на каждый выбранный маршрут, stdin ввод
  приватной URL подписки, увеличенный timeout сетевых запросов и передача
  `benchmark --bytes`.
- Backend валидирует опции, ограничивает parallel числом one-shot слотов,
  ограничивает каталог текущим лимитом и отклоняет явный список сверх лимита.
  Сетевая работа `probe-domain` выполняется вне event loop. Сохранены TLS
  timeout classification и ограниченная загрузка benchmark.
- Installer фиксирует корень репозитория независимо от cwd; каталог host
  клиента доступен для прохода service UID, а файлы credentials остаются
  закрытыми.
- Acceptance использует SOCKS-aware проверки, сохраняет большие JSON,
  допускает изменение публичного direct IP, явно пропускает проверки при
  отсутствии OpenScience. Независимый verifier перепроверяет HTTP статусы и
  размер benchmark.

## Проверка после фиксов

- Полный offline-набор:
  `EGRESSD_METRICS_DB=/tmp/egressd-local-qa-tests.sqlite3 uv run --with pytest --with fastapi --with uvicorn --with httpx --with pyyaml --with aiosqlite --with cryptography python -m pytest tests/ client/tests/ -q`
  — **376 passed**.
- `bash tests/install/test_install_scripts.sh` — **75 passed, 0 failed**.
- Python compile, Bash syntax checks для `scripts/` и shell-файлов `host/`,
  `git diff --check` — успешно.
- Backend пересобран и перезапущен с `/etc/vibelab-egress/compose.env`;
  оба Compose сервиса healthy.
- Host acceptance после фиксов: **23 PASS / 0 FAIL / 5 SKIP**.
  Проверки TUN TCP, UDP/QUIC, параллельных one-shot и ограниченного
  benchmark прошли. SKIP: два OpenScience capability/TUN пункта и проверка
  установленного client release связаны с отсутствующим контейнером;
  recreate check выключен по умолчанию; host harness требует root.
- Для этого user acceptance CLI запускался из текущего `client/src` через
  приватные локальные wrappers и профиль UID 1000. `/usr/local/bin` client
  отдельно не переустанавливался: для этого требуется root.
- `verify-live-report.py` запущен внутри backend контейнера, чтобы проверить
  отчёт против полного набора секретов: **VERIFIED**, hash OK, 23 PASS
  подтверждены, secret scan выполнен; checks hash:
  `5149d64dd01ea67645d9cf1bb6d89d1ee22251a34e1e2db42e42a3cdd3005417`.
  JSON отчёта закрыт правами 0600:
  `~/.local/share/vibelab-egress-test/live-user-after-fixes.json`.
- Повторный TLS probe охватил 401 маршрут по источникам и протоколам,
  партиями до 50 и parallel 8 (8 слотов), на двух HTTPS целях с ожидаемыми
  200/204. Сводка: **33** маршрута прошли обе цели, **346** получили
  TLS failure на обеих целях, **22** дали target-specific/нестабильный
  результат. Разбивка по batch и протоколу записана в
  `reports/local_subscriptions_2026-10-18.md`; агрегированный JSON без
  route ID хранится приватно в
  `~/.local/share/vibelab-egress-test/probe-domain-groups.json`.

Повторные TLS ошибки совместимы с блокировкой или несовместимостью отдельных
узлов, но не доказывают причину у провайдера. 33 Shadowsocks маршрута ответили
ожидаемыми статусами на обе цели; полный acceptance прошёл TCP, UDP/QUIC,
parallel checks и benchmark на отобранных здоровых маршрутах. Приватные URL,
credentials и сырые конфиги в git и tracked отчёты не добавлялись.

### Следующий шаг

Для обновления acceptance на машине с root-доступом запустить
`scripts/run-live-acceptance.sh`, затем
`sudo python3 scripts/verify-live-report.py reports/host_acceptance_report.json`.
`sudo -n true` в текущей сессии запросил пароль, поэтому повторить root-only
host harness здесь не удалось; соответствующий SKIP отражён в user report.
После получения root доступа обновить host client скриптом
`scripts/install-client.sh` для используемого профиля и проверить файлы token
и auth, затем повторить harness с установленным `/usr/local/bin` client.
OpenScience контейнер в локальном Docker отсутствует, поэтому его recreate и
проверка установленной версии требуют среды с этим контейнером.

# Handoff: A6 host-native strict/TUN

Дата: 2026-10-16. Последний live-отчёт globe от 16:30:40 UTC:
**19 PASS / 0 FAIL / 9 SKIP**. Независимая проверка на globe: `VERIFIED`,
checks hash `1d527999d8c280d28676711212d117652958bf7b162d28441f0d67cad68152ca`,
все 19 PASS подтверждены, secret scan выполнен. Отчёт:
`reports/host_acceptance_report.json`. A6 **открыта**: proxy routes ещё нет.

## Реализовано и проверено

- Host systemd helper заменил контейнерный. UNIX socket 0660
  `root:vibelab-egress-tun`, `SO_PEERCRED` и проверка групп, фиксированный
  протокол v2 и `SCM_RIGHTS` для stdio. Запущенная с host команда сохраняет
  host filesystem, cwd, env, umask и UID/GID вызывающего; capabilities команды
  сброшены. Helper имеет только указанные unit capabilities.
- Для каждой команды создаются netns/veth, ограниченные firewall rules и TUN;
  direct использует NAT, proxy — отдельный непривилегированный Mihomo и
  опубликованный gateway `:1081` с one-shot credentials. Startup и завершение
  очищают ресурсы, включая фоновые процессы в netns. Изолированный PoC
  проверил direct и proxy TCP через фиктивный SOCKS5 (`poc/A6_POC.md`).
- На globe §50.2 прошёл: UID/GID 1082:1083, host cwd и запись файла на host.
  §50.5 direct без tun0, §50.6 `CapEff=0`, §50.7 cleanup, §50.8 HARD DIRECT,
  §50.9 настоящий OpenScience `runsc`, §50.11 отказ без helper и §50.12
  capability accuracy прошли. Прочие доступные A8 проверки прошли.
- OpenScience client установлен внутри `runsc`. `egressctl status` видит
  backend; `egressctl capabilities` сообщает `unsupported_in_environment`,
  `egress-run --mode tun direct -- true` завершается кодом 6 без fallback.
  Socket helper и host namespace туда не предоставлены. Bundle был передан
  через `tar | docker exec -i`: `docker cp` в этом окружении дал невидимый
  внутри контейнера каталог `/tmp`. Временные файлы внутри runsc удалены;
  оставшиеся в Docker overlay token/auth от первоначального `docker cp`
  обнулены и проверены обратным копированием.
- Offline suite: 332 PASS с `EGRESSD_METRICS_DB` в `/tmp`. На globe
  backend installer ранее дал host harness 19 PASS / 0 FAIL / 3 SKIP.

## Имплементирующему агенту

1. Сверь последние записи `STATUS.md`, `TECH_SPEC.md` §21/§50, `INSTALL.md`,
   `poc/A6_POC.md` и текущий JSON до изменений. Закрой доступность реальных
   provider proxy routes штатным provisioning, без секретов в git/логах.
2. На host проверь proxy TCP и UDP/QUIC через one-shot listener, Docker DNAT,
   UFW и FORWARD для veth. Правила должны оставаться привязаны к конкретному
   interface и destination gateway `:1081`; не открывай общий FORWARD.
   Если обнаружится дефект, добавь точечный regression test.
3. Проверь concurrent proxy jobs, signal/cancel, timeout и restart helper:
   не должны оставаться netns/veth/firewall rules, `/etc/netns` и `/run` job
   state. Не ослабляй gVisor и не добавляй Docker socket.
4. Установка OpenScience client сейчас сделана в работающем контейнере.
   Обеспечь воспроизводимую установку при его пересоздании; смонтированный
   `/srv/workspace/vibelab-egress` может содержать старую версию клиента.
   Проверь §49.39 на полном recreate и зафиксируй результат.

## Тестирующему агенту

1. Запусти `EGRESSD_METRICS_DB=/tmp/egressd-a6-test.sqlite3 python3 -m pytest tests/ client/tests/ -q`,
   syntax checks для Python/Bash, `systemd-analyze verify` и `git diff --check`
   после кодовых изменений.
2. На globe запусти `bash scripts/run-live-acceptance.sh`, затем
   `sudo python3 scripts/verify-live-report.py reports/host_acceptance_report.json`.
   Сохрани итоговый checks hash и разбор каждого FAIL/SKIP. Не смешивай
   `egress-host-test` (22 checks) с live acceptance (28 checks).
3. Добейся PASS §50.1 proxy TCP, §50.3 UDP/QUIC и §50.4 concurrent proxy
   на настоящих provider routes. Повтори §50.2 и §50.9 как regression.
   §49.39 требует recreate, `docker restart` недостаточен. Root-only
   `a8_host_test_report` в non-root live получает честный SKIP; запусти
   соответствующую проверку с root, если требуется закрыть этот пункт.
4. Обнови `STATUS.md` после каждого завершённого шага. A6 не закрывать,
   пока зависимые live-проверки не пройдут на host.

## Операционному и безопасностному агенту

- Проверь unit capabilities: `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `CAP_SETUID`,
  `CAP_SETGID`, `CAP_CHOWN`, `CAP_KILL`, `CAP_SETPCAP`; socket только локальный,
  клиентская команда с `CapEff=0`. Проверяй отсутствие секретов/команд/env
  в audit, Docker socket в Compose и доступа OpenScience к host helper.
- Текущий helper установлен и работает на globe. При обновлении используй
  `scripts/install-tun-host.sh`; старый контейнерный helper больше не должен
  работать. Не сбрасывай общие таблицы Docker/UFW при cleanup.
- Не передавай реальные secrets, bundle, generated config и `/run` state.
  В git сохраняй только проверенный JSON и документацию. Исторические копии
  отчётов на globe не включать в коммит без отдельной проверки.
