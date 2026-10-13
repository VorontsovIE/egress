"""Xeovo provider adapter (spec section 11.2).

Xeovo's exact subscription format is not available in this environment
(deferred to A2, spec section 45 item 2). The adapter is therefore the
generic normalizer (Clash YAML / base64 Clash / WireGuard .conf / vless://)
that produces **clean, stable node names** (e.g. ``US-New-York-1``). The
``xeovo-`` provider tag is applied by Mihomo's ``override.additional-prefix``
in the provider config (spec 11.1), not baked into the file.

Once the real Xeovo format is confirmed, only :meth:`name_map` (and, if the
format is non-standard, the parser dispatch in ``common.detect_format``) needs
to change -- the atomic-write / last-known-good / no-secret-logging guarantees
are inherited from :class:`adapters.common.ProviderAdapter`.
"""
from __future__ import annotations

from ..common import ProviderAdapter, stable_slug


class XeovoAdapter(ProviderAdapter):
    provider = "xeovo"
    default_prefix = "xeovo-"

    # Display name -> stable id (spec section 13). "US - New York 2" ->
    # "US-New-York-2".
    def name_map(self, display: str) -> str:
        return stable_slug(display)
