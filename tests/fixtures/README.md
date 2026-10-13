# Provider fixtures

Test fixtures for the provider adapters (`adapters/`). **Every address, key,
UUID and token here is a fake placeholder** (`198.51.100.0/24` TEST-NET-2,
`fake-*-do-not-use` keys, `00000000-...` UUIDs). They exist to exercise the
adapter's format detection, parsing, normalization, redaction and
atomic-write/last-known-good behavior -- not to provide working subscriptions.

| file | format | exercises |
| --- | --- | --- |
| `blanc_clash.yaml` | Clash YAML | socks5 + wireguard + vless nodes, display-name -> stable id |
| `blanc_wireguard.conf` | WireGuard `.conf` | adapter path (spec 11.2): non-Clash format |
| `xeovo_base64.txt` | base64 Clash YAML | base64-encoded subscription |
| `xeovo_vless.txt` | `vless://` URIs | VLESS URI parsing (tls + reality) |

Run the adapter tests: `python -m pytest tests/integration/test_adapters.py`
(or the A2 suite `tests/integration/test_a2.py`).
