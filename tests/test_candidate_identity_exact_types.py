from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass virtual method must not execute")


class _CandidateLegSubclass(CandidateLeg):
    pass


def _leg() -> CandidateLeg:
    return CandidateLeg(
        quote_key="event-1|market-1|selection-1",
        event_id="event-1",
        decimal_odds=Decimal("2"),
        probability=Decimal("0.5"),
        market_id="market-1",
        selection_id="selection-1",
    )


@pytest.mark.parametrize(
    "field",
    ("event_id", "market_id", "selection_id"),
)
def test_ticket_identity_rejects_str_subclass_before_virtual_method(field: str) -> None:
    leg = _leg()
    object.__setattr__(leg, field, _TrapStr(getattr(leg, field)))

    with pytest.raises(ValueError, match="canonical string"):
        leg.ticket_identity()


def test_ticket_identity_rejects_quote_key_str_subclass_before_virtual_method() -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", _TrapStr(leg.quote_key))

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        leg.ticket_identity()


def test_search_rejects_quote_key_str_subclass_before_virtual_method() -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", _TrapStr(leg.quote_key))

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        BeamParlayCandidateSearch().search([leg], minimum_legs=1)


def test_search_rejects_event_id_str_subclass_before_virtual_method() -> None:
    leg = _leg()
    object.__setattr__(leg, "event_id", _TrapStr(leg.event_id))

    with pytest.raises(ValueError, match="event_id must be a non-empty canonical string"):
        BeamParlayCandidateSearch().search([leg], minimum_legs=1)


def test_search_rejects_candidate_leg_subclass_before_member_dispatch() -> None:
    hostile = _CandidateLegSubclass(
        quote_key="event-1|market-1|selection-1",
        event_id="event-1",
        decimal_odds=Decimal("2"),
        probability=Decimal("0.5"),
        market_id="market-1",
        selection_id="selection-1",
    )

    with pytest.raises(ValueError, match="must be an exact CandidateLeg"):
        BeamParlayCandidateSearch().search([hostile], minimum_legs=1)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("quote_key", "event-1|market-1|selection-1\x00"),
        ("event_id", "event-1\x00"),
        ("market_id", "market-1\x00"),
        ("selection_id", "selection-1\x00"),
        ("event_id", "event-\ud800"),
    ),
)
def test_ticket_identity_rejects_noncanonical_identity_bytes(
    field: str,
    value: str,
) -> None:
    leg = _leg()
    object.__setattr__(leg, field, value)

    with pytest.raises(ValueError, match="canonical string"):
        leg.ticket_identity()


@pytest.mark.parametrize(
    "quote_key",
    (
        "event-1| market-1|selection-1",
        "event-1|market-1|selection-1 ",
        "event-1|market-1\x00|selection-1",
    ),
)
def test_legacy_ticket_suffix_cannot_mint_noncanonical_structured_identity(
    quote_key: str,
) -> None:
    leg = CandidateLeg(
        quote_key=quote_key,
        event_id="event-1",
        decimal_odds=Decimal("2"),
        probability=Decimal("0.5"),
    )

    with pytest.raises(ValueError, match="canonical string"):
        leg.ticket_identity()


def test_search_rejects_non_utf8_quote_key_before_identity_use() -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", "event-1|market-1|selection-\ud800")

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        BeamParlayCandidateSearch().search([leg], minimum_legs=1)
