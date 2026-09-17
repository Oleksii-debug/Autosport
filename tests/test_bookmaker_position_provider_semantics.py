from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapabilityError,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


_TS = "2026-09-17T16:00:00+00:00"
_HASH = "a" * 64


def _position(**overrides: object) -> BookmakerPositionObservation:
    values: dict[str, object] = {
        "venue_id": "betfair-exchange",
        "account_id": "acct-a",
        "adapter_id": "betfair-readonly",
        "observation_id": "order-1-observation",
        "external_position_id": "bet-1",
        "state": BookmakerPositionState.OPEN,
        "provider_amount": Decimal("10.00"),
        "provider_amount_semantics": "backer_stake",
        "provider_side": "BACK",
        "currency": "EUR",
        "observed_at": _TS,
        "source_payload_sha256": _HASH,
        "decimal_odds": Decimal("3.00"),
    }
    values.update(overrides)
    return BookmakerPositionObservation(**values)


def test_position_preserves_back_and_lay_provider_amount_without_inventing_liability() -> None:
    back = _position(
        observation_id="back-observation",
        external_position_id="back-bet",
        provider_side="BACK",
        provider_amount=Decimal("10.00"),
    )
    lay = _position(
        observation_id="lay-observation",
        external_position_id="lay-bet",
        provider_side="LAY",
        provider_amount=Decimal("10.00"),
    )

    assert back.provider_side == "BACK"
    assert lay.provider_side == "LAY"
    assert back.provider_amount == lay.provider_amount == Decimal("10.00")
    assert back.provider_amount_semantics == "backer_stake"
    assert lay.provider_amount_semantics == "backer_stake"


def test_position_amount_semantics_and_optional_side_fail_closed() -> None:
    with pytest.raises(BookmakerCapabilityError, match="provider_amount_semantics"):
        _position(provider_amount_semantics="")

    with pytest.raises(BookmakerCapabilityError, match="provider_amount"):
        _position(provider_amount=Decimal("NaN"))

    with pytest.raises(BookmakerCapabilityError, match="provider_side"):
        _position(provider_side=" LAY")

    side_not_applicable = _position(provider_side=None)
    assert side_not_applicable.provider_side is None


def test_legacy_stake_input_is_migrated_to_explicit_semantics() -> None:
    legacy = BookmakerPositionObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id="legacy-observation",
        external_position_id="legacy-position",
        state=BookmakerPositionState.OPEN,
        stake=Decimal("4.25"),
        currency="EUR",
        observed_at=_TS,
        source_payload_sha256=_HASH,
    )

    assert legacy.provider_amount == Decimal("4.25")
    assert legacy.provider_amount_semantics == "legacy_stake"
    assert legacy.provider_side is None

    with pytest.raises(BookmakerCapabilityError, match="cannot be combined"):
        BookmakerPositionObservation(
            venue_id="book-a",
            account_id="acct-a",
            adapter_id="adapter-a",
            observation_id="ambiguous-observation",
            external_position_id="ambiguous-position",
            state=BookmakerPositionState.OPEN,
            stake=Decimal("4.25"),
            provider_amount=Decimal("4.25"),
            provider_amount_semantics="backer_stake",
            currency="EUR",
            observed_at=_TS,
            source_payload_sha256=_HASH,
        )
