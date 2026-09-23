from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from autosport.supervised_provider_evidence import (
    ProviderEvidenceError,
    verify_betfair_provider_state,
)


_HELPERS = runpy.run_path(
    str(
        Path(__file__).with_name(
            "test_betfair_readback_requested_price_authority.py"
        )
    )
)
_action = _HELPERS["_action"]
_profile = _HELPERS["_profile"]
_capture = _HELPERS["_capture"]
PROVIDER_REF = _HELPERS["PROVIDER_REF"]


def test_injected_readback_transport_cannot_mint_provider_effect_authority() -> None:
    action = _action()
    profile = _profile()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )

    with pytest.raises(ProviderEvidenceError):
        verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=PROVIDER_REF,
        )
