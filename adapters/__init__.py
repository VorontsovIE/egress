"""Provider adapters (spec section 11.2).

Each package exposes a ``ProviderAdapter`` subclass. See ``common.py`` for the
shared format detection, normalization, atomic write and last-known-good logic.
"""
from .common import (  # noqa: F401
    ProviderAdapter,
    detect_format,
    normalize_source,
    atomic_write,
    atomic_write_bytes,
    redact,
    run_adapter,
    get_adapter,
    stable_slug,
)
