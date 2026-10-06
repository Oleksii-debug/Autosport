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
