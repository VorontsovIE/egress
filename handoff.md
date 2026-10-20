# Handoff: client-local TUN

Обновлено: 2026-10-20. Предыдущая версия документа описывала systemd TUN service и больше не является инструкцией для текущей реализации.

## Текущая архитектура

- `egress-run --mode tun ROUTE -- command` создаёт отдельный user/network namespace на каждый запуск.
- Workload и его дочерние процессы остаются внутри вызывающей среды, включая обычный Docker-контейнер.
- Отдельный статический packet engine получает только TUN fd и SOCKS credentials выбранной one-shot сессии; argv, cwd, environment и stdio workload ему не передаются.
- Local loopback DNS proxy внутри нового namespace отправляет DNS через TUN и тот же выбранный route.
- В Docker передаются `/dev/net/tun` и `client/seccomp-tun.json`; outer container не получает `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, `--privileged`, host network или Docker socket.
- Миграция старых host/container TUN service находится в `scripts/remove-legacy-tun-helper.sh`. Скрипт проверяет job state и отказывается останавливать среду, если не может подтвердить отсутствие активных запусков.

## Сборка и установка

Собрать static engine Go 1.26.3 на доверенной build-машине:

```bash
scripts/build-tun-engine.sh
```

Затем provisioning включает amd64/arm64 engines в client bundle. Установка клиента на host или в container проверяет checksum и выбирает engine по архитектуре. Подробные инструкции: `INSTALL.md`.

## Проверено в рабочем дереве

- Python/Bash syntax checks.
- `tests/` и `client/tests/` offline suite.
- Install-script harness.
- Static engine cross-build для Linux amd64 и arm64.
- Обычный Docker-контейнер: создание user/network namespace и TUN без `CAP_NET_ADMIN`.
- Реальный TUN direct HTTPS и DNS из контейнера; два вызова получили разные network namespace IDs.
- SOCKS5 auth/TCP forwarding через TUN проверены с локальным тестовым SOCKS endpoint; SOCKS UDP DNS проверен отдельным тестовым endpoint.

Полный acceptance через настоящие backend provider routes требует настроенного backend и действительного client bundle; live harness и generic Docker acceptance описаны в `host/LIVE_ACCEPTANCE.md` и `INSTALL.md`.
