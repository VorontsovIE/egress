from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))
from egress_client.api import EgressApi


def test_personal_route_shorthand_resolves_for_current_token():
    api = EgressApi.__new__(EgressApi)
    api.list_personal = lambda: {"providers": [
        {"alias": "blanc", "provider_id": "p-host-egress-blanc"}
    ]}
    assert api.resolve_route("@blanc:vless-49") == "p-host-egress-blanc:vless-49"
    assert api.resolve_route("direct") == "direct"


@pytest.mark.parametrize("route", ("@blanc", "@_bad:node", "@blanc:node:extra"))
def test_personal_route_shorthand_rejects_invalid_or_unknown_names(route):
    api = EgressApi.__new__(EgressApi)
    api.list_personal = lambda: {"providers": []}
    with pytest.raises(ValueError):
        api.resolve_route(route)
