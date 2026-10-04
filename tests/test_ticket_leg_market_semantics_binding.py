from decimal import Decimal

import pytest

from autosport.domain import TicketLeg


_S1 = "soccer:h2h:v1"
_S2 = "soccer:h2h:v2"


def _leg(semantics: str | None) -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("2.10"),
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def test_same_quote_different_market_semantics_do_not_alias_settlement_identity() -> None:
    first = _leg(_S1)
    second = _leg(_S2)

    assert first.quote_key == second.quote_key
    assert first.settlement_identity != second.settlement_identity


def test_legacy_quote_identity_is_unchanged_by_absent_semantics() -> None:
    legacy = TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("2.10"),
        sport="soccer",
        exchange_side="back",
    )
    explicit_none = _leg(None)

    assert legacy.quote_key == explicit_none.quote_key
    assert legacy.settlement_identity == (legacy.quote_key, None)


@pytest.mark.parametrize(
    "identity",
    [
        "",
        " Soccer:h2h:v1",
        "SOCCER:H2H:V1",
        "soccer|h2h",
        "unknown",
        "mixed",
        "unspecified",
    ],
)
def test_ticket_leg_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        _leg(identity)

