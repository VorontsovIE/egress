# Provider fixtures

These fixtures exercise provider adapters in `adapters/`. Every address, key, UUID, and token is fake (`198.51.100.0/24` TEST-NET-2, `fake-*-do-not-use` keys, and zero UUIDs). They are test data, not usable subscriptions.

| File | Format | Coverage |
|---|---|---|
| `blanc_clash.yaml` | Clash YAML | SOCKS5, WireGuard, VLESS, display name to stable ID |
| `blanc_wireguard.conf` | WireGuard `.conf` | Non-Clash adapter path |
| `xeovo_base64.txt` | Base64 Clash YAML | Base64 subscription decoding |
| `xeovo_vless.txt` | `vless://` URIs | TLS and Reality parsing |

Run the adapter tests with `python3 -m pytest tests/integration/test_adapters.py` or run the A2 suite in `tests/integration/test_a2.py`.
