# A6: промежуточный PoC host-native strict/TUN

Дата: 2026-10-16. Среда PoC: одноразовый контейнер Ubuntu 24.04 на локальном
Docker host с `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `/dev/net/tun`; без Docker socket
в тестируемом helper и без host mount. Этот прогон подтверждает сетевые
механизмы, но **не** является live-приёмкой на globe.

## Подтверждено

- `ip netns add` создаёт отдельный netns при наличии `CAP_SYS_ADMIN` и
  `CAP_NET_ADMIN`; `ip netns exec` поднимает `lo`.
- Бинарник Mihomo v1.19.32 из закреплённого образа с digest
  `sha256:bac1a74de365ea59270ba134016762c6be05654d55246518de256113bd1c225e`
  имеет SHA256 `6de241191ca39f9ac1cd6e3194fc9303c19055d3549c8bdb4ff1349c89173819`.
  Mihomo запустился с заранее созданным TUN, принадлежащим отдельному UID, без
  `CAP_NET_ADMIN` у его процесса.
- `direct + tun`: отдельный netns, veth, локальный NAT и `nft` filter;
  процесс с UID/GID 65534 выполнил `id`, затем получил внешний IPv4 через
  HTTPS; оба процесса завершились с кодом 0.
- Proxy path: Mihomo в netns (отдельный UID) направил TCP клиента без proxy
  environment через TUN и SOCKS5 к фиктивному upstream; upstream подтвердил
  CONNECT к публичному адресу, клиент получил `VIA_PROXY`, код 0. Для
  предотвращения петли пришлось явно задать `interface-name` для SOCKS5
  outbound — без него Mihomo выбирал `tun0` как исходящий интерфейс.

## Не подтверждено

- Запуск из настоящей host-службы systemd с файловой системой, cwd,
  окружением, UID/GID и группами вызывающего пользователя.
- Реальный опубликованный one-shot listener, SOCKS5 UDP associate, QUIC,
  DNS, параллельные задания и cleanup после аварийного завершения.
- Совместимость host firewall/UFW на globe и фактический набор capabilities
  systemd. Пока эти проверки не пройдены, capability и A6 нельзя считать
  закрытыми.

## Дополнительный прогон реализации

После сборки `native_netns.py` повторён изолированный Docker PoC с настоящими
методами backend. `create` → `pin_direct` → `exec_in` → `destroy` запустил
процесс с UID/GID 65534 в `/tmp`, RC 0. Proxy-прогон запустил Mihomo под
worker UID со сброшенными capabilities; fake SOCKS5 получил CONNECT к
`1.1.1.1:80`, HTTP-клиент без proxy env получил `VIA_PROXY`, RC 0. Оба прогона
сделаны в одноразовом контейнере, поэтому host systemd/one-shot/DNS/UDP и
продуктовая приёмка остаются непроверенными.
