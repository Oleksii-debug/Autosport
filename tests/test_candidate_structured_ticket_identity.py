from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.candidate_optimizer import (
    PortfolioAwareCandidateOptimizer,
    _candidate_ticket,
    _ticket_leg_from_candidate,
)
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.domain import PaperTicket, TicketLeg, _quote_identity
from autosport.research_strategy import _candidate_leg_from_dict
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


def _structured_leg(
    *,
    event_id: str = "event|2026",
    market_id: str = "market|spread",
    selection_id: str = "player|a",
) -> CandidateLeg:
    quote_key = _quote_identity(event_id, market_id, selection_id)
    return CandidateLeg(
        quote_key,
        event_id,
        Decimal("2"),
        Decimal("0.5"),
        market_id,
        selection_id,
    )


def _single_candidate(leg: CandidateLeg) -> ParlayCandidate:
    return ParlayCandidate(
        (leg,),
        leg.decimal_odds,
        leg.probability,
        leg.probability * leg.decimal_odds - Decimal("1"),
    )


def test_optimizer_ticket_conversion_accepts_delimiter_bearing_structured_identity() -> None:
    leg = _structured_leg()

    ticket_leg = _ticket_leg_from_candidate(leg)

    assert ticket_leg.event_id == leg.event_id
    assert ticket_leg.market_id == leg.market_id
    assert ticket_leg.selection_id == leg.selection_id
    assert ticket_leg.quote_key == leg.quote_key
    assert ticket_leg.quote_key.startswith("component-boundary-v1-")


def test_optimizer_ticket_conversion_accepts_injective_structured_identity() -> None:
    leg = CandidateLeg(
        "event-2026|market-spread|player-a",
        "event-2026",
        Decimal("2"),
        Decimal("0.5"),
        "market-spread",
        "player-a",
    )

    ticket_leg = _ticket_leg_from_candidate(leg)

    assert ticket_leg.event_id == "event-2026"
    assert ticket_leg.market_id == "market-spread"
    assert ticket_leg.selection_id == "player-a"
    assert ticket_leg.quote_key == leg.quote_key


def test_ambiguous_legacy_quote_key_fails_closed_without_structured_suffix_identity() -> None:
    leg = CandidateLeg(
        "event|market|with|delimiter|selection",
        "event",
        Decimal("2"),
        Decimal("0.5"),
    )

    with pytest.raises(
        ValueError,
        match="requires structured market_id and selection_id",
    ):
        leg.ticket_identity()

    with pytest.raises(
        ValueError,
        match="requires structured market_id and selection_id",
    ):
        _ticket_leg_from_candidate(leg)


def test_research_plan_loader_accepts_explicit_delimiter_bearing_identity_as_data() -> None:
    raw = {
        "quote_key": "event|2026|market|spread|player|a",
        "event_id": "event|2026",
        "market_id": "market|spread",
        "selection_id": "player|a",
        "decimal_odds": "2",
        "probability": "0.5",
    }

    leg = _candidate_leg_from_dict(raw)

    assert leg.ticket_identity() == (
        "event|2026",
        "market|spread",
        "player|a",
    )
    assert leg.quote_key.startswith("component-boundary-v1-")
    ticket_leg = _ticket_leg_from_candidate(leg)
    assert ticket_leg.quote_key == leg.quote_key


def test_research_plan_loader_rejects_ambiguous_legacy_quote_key() -> None:
    raw = {
        "quote_key": "event|market|with|delimiter|selection",
        "decimal_odds": "2",
        "probability": "0.5",
    }

    with pytest.raises(
        ValueError,
        match="ambiguous quote_key requires structured",
    ):
        _candidate_leg_from_dict(raw)


def test_research_plan_loader_rejects_partial_structured_identity() -> None:
    raw = {
        "quote_key": "event|market|selection",
        "event_id": "event",
        "market_id": "market",
        "decimal_odds": "2",
        "probability": "0.5",
    }

    with pytest.raises(
        ValueError,
        match="must be provided together",
    ):
        _candidate_leg_from_dict(raw)


def test_research_plan_loader_rejects_mismatched_structured_identity() -> None:
    raw = {
        "quote_key": "event|market|selection",
        "event_id": "event",
        "market_id": "different-market",
        "selection_id": "selection",
        "decimal_odds": "2",
        "probability": "0.5",
    }

    with pytest.raises(
        ValueError,
        match="does not match quote_key",
    ):
        _candidate_leg_from_dict(raw)


def test_previously_colliding_structured_identities_are_injective_for_candidate_conversion() -> None:
    first = _structured_leg(event_id="e", market_id="m|x", selection_id="s")
    second = _structured_leg(event_id="e", market_id="m", selection_id="x|s")

    assert first.quote_key != second.quote_key
    assert _ticket_leg_from_candidate(first).quote_key == first.quote_key
    assert _ticket_leg_from_candidate(second).quote_key == second.quote_key


def test_direct_legacy_structured_alias_fails_closed_without_loader_migration() -> None:
    legacy = CandidateLeg(
        "e|m|x|s",
        "e",
        Decimal("2"),
        Decimal("0.5"),
        "m|x",
        "s",
    )

    with pytest.raises(ValueError, match="does not match quote_key"):
        _ticket_leg_from_candidate(legacy)


def test_optimizer_ticket_id_remains_structurally_bound_for_injective_identity() -> None:
    first = CandidateLeg(
        "e1|m|s",
        "e1",
        Decimal("2"),
        Decimal("0.5"),
        "m",
        "s",
    )
    second = CandidateLeg(
        "e2|m|s",
        "e2",
        Decimal("2"),
        Decimal("0.5"),
        "m",
        "s",
    )
    quote_to_group = {"e1|m|s": 0, "e2|m|s": 1}

    first_ticket = _candidate_ticket(
        _single_candidate(first),
        Decimal("10"),
        quote_to_group,
    )
    second_ticket = _candidate_ticket(
        _single_candidate(second),
        Decimal("10"),
        quote_to_group,
    )

    assert first_ticket.ticket_id != second_ticket.ticket_id
    assert first_ticket.legs[0].event_id == "e1"
    assert second_ticket.legs[0].event_id == "e2"