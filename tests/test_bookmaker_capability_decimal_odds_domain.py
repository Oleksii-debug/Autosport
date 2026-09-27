from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapabilityError,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


_TS = "2026-09-23T11:00:00+00:00"
_HASH = "a" * 64


def _position(decimal_odds: Decimal) -> BookmakerPositionObservation:
    return BookmakerPositionObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id="position-open-1",
        external_position_id="external-1",
        state=BookmakerPositionState.OPEN,
        currency="EUR",
        observed_at=_TS,
        source_payload_sha256=_HASH,
        provider_amount=Decimal("10"),
        provider_amount_semantics="backer_stake",
        decimal_odds=decimal_odds,
    )


@pytest.mark.parametrize("decimal_odds", [Decimal("0.5"), Decimal("1")])
def test_position_rejects_impossible_decimal_odds(decimal_odds: Decimal) -> None:
    with pytest.raises(BookmakerCapabilityError, match="greater than 1"):
        _position(decimal_odds)


def test_position_accepts_decimal_odds_just_above_one() -> None:
    position = _position(Decimal("1.01"))

    assert position.decimal_odds == Decimal("1.01")
