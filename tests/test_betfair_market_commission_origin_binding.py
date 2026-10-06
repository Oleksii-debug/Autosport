from __future__ import annotations

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionAuthorityError,
)


def test_commission_source_client_origin_is_immutable_after_construction(tmp_path) -> None:
    source = BetfairMarketCommissionAuthority(
        tmp_path / "commission",
        BetfairSessionCredentials(
            application_key="test-application-key",
            session_token="test-session-token",
        ),
        authority_root=tmp_path / "authority",
    )
    origin = source._client
    replacement = object.__new__(BetfairReadOnlyClient)

    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="client origin is immutable after construction",
    ):
        source._client = replacement

    assert source._client is origin


def test_commission_source_cannot_temporarily_swap_and_restore_client(tmp_path) -> None:
    source = BetfairMarketCommissionAuthority(
        tmp_path / "commission",
        BetfairSessionCredentials(
            application_key="test-application-key",
            session_token="test-session-token",
        ),
        authority_root=tmp_path / "authority",
    )
    origin = source._client
    attacker = object.__new__(BetfairReadOnlyClient)

    for candidate in (attacker, attacker, origin):
        if candidate is origin:
            source._client = candidate
            continue
        with pytest.raises(BetfairMarketCommissionAuthorityError):
            source._client = candidate
        assert source._client is origin

    assert source._client is origin
