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
