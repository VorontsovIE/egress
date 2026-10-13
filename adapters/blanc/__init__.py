"""BlancVPN provider adapter (spec section 11.2).

BlancVPN's exact subscription format is not available in this environment
(deferred to A2, spec section 45 item 1). The adapter is therefore the
generic normalizer (Clash YAML / base64 Clash / WireGuard .conf / vless://)
that produces **clean, stable node names** (e.g. ``FI-Helsinki-1``). The
``blanc-`` provider tag is applied by Mihomo's ``override.additional-prefix``
in the provider config (spec 11.1), not baked into the file -- so the same
tagging works for a native (http) subscription and for the adapter output.

Once the real BlancVPN format is confirmed, only :meth:`name_map` (and, if the
format is non-standard, the parser dispatch in ``common.detect_format``) needs
to change -- the atomic-write / last-known-good / no-secret-logging guarantees
are inherited from :class:`adapters.common.ProviderAdapter`.
"""
from __future__ import annotations

from ..common import ProviderAdapter, stable_slug


class BlancAdapter(ProviderAdapter):
    provider = "blanc"
    # Recommended override.additional-prefix for the provider config.
    default_prefix = "blanc-"

    # Display name -> stable id (spec section 13: the stable route ID must not
    # depend only on the mutable display name). "FI - Helsinki 1" ->
    # "FI-Helsinki-1".
    def name_map(self, display: str) -> str:
        return stable_slug(display)
