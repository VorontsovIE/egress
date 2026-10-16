# Handoff: A6 host-native strict/TUN

Дата: 2026-10-16. Исходный live-отчёт globe (13:31 UTC): 14 PASS / 1 FAIL / 13 SKIP. Этот отчёт подтверждён verifier, но §50.2 ещё не принят. После изменений ниже нового live-отчёта пока нет.

## Что реализовано

- Вместо контейнерного helper — host systemd unit `vibelab-egress-tun-helper.service`, локальный UNIX socket с проверкой `SO_PEERCRED`, фиксированный протокол v2 с передачей stdin/stdout/stderr через `SCM_RIGHTS`. Вызов из host сохраняет host mount namespace, cwd, env, umask, UID/GID и дополнительные группы. Программа запускается без capabilities.
- Per-run named netns, veth, отдельные адреса, nft filter, точечные host firewall rules, TUN и непривилегированный Mihomo для proxy route; direct использует NAT. One-shot endpoint доступен через опубликованный Docker gateway `:1081`; client передаёт credentials только в закрытом request, helper пишет закрытый config в `/run`. Данные работы и сетевые объекты очищаются при завершении, startup чистит остатки.
- OpenScience/gVisor не получает socket, `/dev/net/tun`, Docker socket или host namespace. Отсутствие helper/возможностей даёт явный `unsupported_in_environment`, без перехода в proxy.
- Offline: 332 PASS (`EGRESSD_METRICS_DB=/tmp/... pytest tests/ client/tests/`); systemd unit syntax `systemd-analyze verify` без ошибок. Изолированный Docker PoC: direct create/exec/cleanup RC 0 с UID 65534 и cwd `/tmp`; proxy TUN через Mihomo → фиктивный SOCKS5 подтвердил CONNECT, клиент получил `VIA_PROXY`, RC 0. Это не host acceptance.

## Имплементирующему агенту

1. Перед включением на globe проверь `git diff`, `TECH_SPEC.md` §21/§50, `poc/A6_POC.md`, `INSTALL.md` и последние записи `STATUS.md`. Не передавай secrets, bundles, generated config, `/run` state. Обнови backend и host client через `sudo bash scripts/install-backend-host.sh --yes`, затем код и unit через `sudo bash scripts/install-tun-host.sh --user vibelab` из проверенного checkout. Не добавляй Docker socket и не меняй runtime gVisor.
2. На globe сначала проверь, что старый `vibelab-egress-tun-helper` контейнер остановлен/удалён как orphan; новый helper работает только через systemd. Обнови backend Compose для публикации `1081/udp` на loopback и Docker gateway. После установки нужна новая login-сессия пользователя для группы `vibelab-egress-tun`.
3. Если в live-проверке proxy path не подключается, исследуй порядок Docker DNAT, UFW и host FORWARD для veth. Правила должны оставаться привязаны к случайному interface и original destination `gateway:1081`; не открывай общий FORWARD и не ослабляй namespace nft filter. Проверь DNS и SOCKS5 UDP ASSOCIATE на настоящем one-shot listener.
4. Проверь signal/cancel, timeout, параллельные job, restart helper и остатки netns/veth/iptables/`/etc/netns`/`/run`. Если найдёшь дефект, исправь и добавь конкретный тест, обнови `STATUS.md`.

## Тестирующему агенту

1. Запусти `EGRESSD_METRICS_DB=/tmp/egressd-a6-test.sqlite3 python3 -m pytest tests/ client/tests/ -q`; `python3 -m py_compile host/strict-helper/*.py host/live-acceptance.py scripts/verify-live-report.py`; `bash -n scripts/*.sh`; `systemd-analyze verify host/units/vibelab-egress-tun-helper.service`; `git diff --check`.
2. На globe после установки: `egressctl capabilities`; `bash scripts/run-live-acceptance.sh`; `sudo python3 scripts/verify-live-report.py reports/host_acceptance_report.json`. Сохрани report hash и разбор каждого FAIL/SKIP. §50.2 обязан показать host UID/GID, cwd и запись в host `/tmp`. Не объявляй A6 закрытой до PASS §50.2 и зависимых проверок.
3. Для §50.9 нужен контейнер с фактическим runtime `runsc`/gVisor. Для provider-зависимых §50.3/4/5 и остальных требуются рабочие proxy routes; с одним direct корректен SKIP. §49.39 требует настоящее пересоздание контейнера, `docker restart` не засчитывается.

## Операционному/безопасностному агенту

- Аудируй scope capabilities unit: `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `CAP_SETUID`, `CAP_SETGID`; helper слушает только socket root:`vibelab-egress-tun` mode 0660. Целевая команда не должна наследовать capabilities. Проверь отсутствие command/env/secrets в audit, отсутствие Docker socket и отсутствие mount helper в OpenScience.
- Перед откатом останови host unit, затем удали только его netns/точечные правила через штатную cleanup; не сбрасывай таблицы Docker/UFW. Backend и пользовательские данные сохраняй.
- Секреты из `/etc/vibelab-egress/secrets/`, клиентские bundle и generated config не копировать в репозиторий или отчёт.

## Ограничение текущей сессии

SSH `vibelab@globe.autosome.ru` работает, но `sudo -n` требует пароль. Root install и live acceptance на globe в этой сессии ещё не выполнены. Команда установки приведена выше; после неё нужно продолжить live-приёмку и независимую проверку JSON.
