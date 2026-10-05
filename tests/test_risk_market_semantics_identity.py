from __future__ import annotations

from decimal import Decimal

import pytest

from autosport import _risk_market_semantics_identity as semantics_identity
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
import autosport.risk as risk_module
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


_OBSERVED = "2026-10-05T15:00:00+00:00"
_SOURCE = "2026-10-05T14:59:59+00:00"
_INGEST = "2026-10-05T15:00:01+00:00"
_PROPOSAL = "2026-10-05T15:00:02+00:00"


def _leg(semantics: str | None) -> TicketLeg:
    return TicketLeg(
        "event-risk-semantics",
        "market-risk-semantics",
        "selection-risk-semantics",
        Decimal("2"),
        sport="football",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def _quote(semantics: str | None) -> MarketEvent:
    leg = _leg(semantics)
    return MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts=_OBSERVED,
        source_id="provider-risk-semantics",
        sequence=1,
        source_ts=_SOURCE,
        ingest_ts=_INGEST,
        sport=leg.sport,
        exchange_side=leg.exchange_side,
        market_semantics_id=semantics,
    )


def _context(semantics: str | None) -> ProposedTicketRiskContext:
    return ProposedTicketRiskContext(
        legs=(_leg(semantics),),
        quotes=(_quote(semantics),),
        bankroll_id="paper-main",
        currency="USD",
        proposal_ts=_PROPOSAL,
    )


def test_candidate_digest_distinguishes_market_semantics() -> None:
    legacy = PaperRiskPolicy.risk_of_ruin_candidate_sha256(_context(None))
    first = PaperRiskPolicy.risk_of_ruin_candidate_sha256(_context("rules:s1"))
    second = PaperRiskPolicy.risk_of_ruin_candidate_sha256(_context("rules:s2"))

    assert legacy is not None
    assert first is not None
    assert second is not None
    assert len({legacy, first, second}) == 3


def test_candidate_vector_digest_inherits_market_semantics_identity() -> None:
    first = PaperRiskPolicy.risk_of_ruin_candidate_vector_sha256(
        (_context("rules:s1"),)
    )
    second = PaperRiskPolicy.risk_of_ruin_candidate_vector_sha256(
        (_context("rules:s2"),)
    )

    assert first is not None
    assert second is not None
    assert first != second


def test_candidate_vector_digest_is_in_stake_vector_executable_witness() -> None:
    descriptor = vars(PaperRiskPolicy)["risk_of_ruin_candidate_vector_sha256"]
    assert type(descriptor) is classmethod

    witnesses = risk_module._PAPER_RISK_DERIVE_GOAL_STAKE_VECTOR_HELPER_WITNESSES
    matches = [
        witness
        for witness in witnesses
        if witness[0] == "risk_of_ruin_candidate_vector_sha256"
    ]

    assert len(matches) == 1
    witness = matches[0]
    assert witness[1] is descriptor
    assert witness[2] is descriptor.__func__
    assert witness[3] is descriptor.__func__.__code__
    assert witness[4] is True


def test_candidate_payload_schema_and_leg_bind_market_semantics() -> None:
    payload = semantics_identity._candidate_payload(_context("rules:s1"))

    assert payload["schema"] == "autosport.risk-candidate.v4"
    legs = payload["legs"]
    assert type(legs) is list
    assert legs[0]["market_semantics_id"] == "rules:s1"


def test_portfolio_payload_schema_and_leg_bind_market_semantics() -> None:
    book = PaperBook("100")
    book.open_ticket(
        [_leg("rules:s1")],
        Decimal("1"),
        placed_at=_PROPOSAL,
    )

    payload = semantics_identity._portfolio_payload(book)
    digest = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)

    assert payload["schema"] == "autosport.paper-risk-state.v5"
    tickets = payload["tickets"]
    assert type(tickets) is list
    assert tickets[0]["legs"][0]["market_semantics_id"] == "rules:s1"
    assert digest is not None


def test_context_rejects_quote_market_semantics_mismatch() -> None:
    with pytest.raises(
        ValueError,
        match="market semantics must match proposed legs exactly",
    ):
        ProposedTicketRiskContext(
            legs=(_leg("rules:s1"),),
            quotes=(_quote("rules:s2"),),
            bankroll_id="paper-main",
            currency="USD",
            proposal_ts=_PROPOSAL,
        )


def test_candidate_digest_fails_closed_after_quote_semantics_mutation() -> None:
    context = _context("rules:s1")
    object.__setattr__(context.quotes[0], "market_semantics_id", "rules:s2")

    assert PaperRiskPolicy.risk_of_ruin_candidate_sha256(context) is None


def test_evaluate_fails_closed_after_quote_semantics_mutation() -> None:
    goal = EconomicGoalContract(
        goal_id="goal-risk-semantics",
        revision=1,
        bankroll_id="paper-main",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    book = PaperBook("100")
    context = _context("rules:s1")
    object.__setattr__(context.quotes[0], "market_semantics_id", "rules:s2")

    decision = policy.evaluate(book, Decimal("1"), context=context)

    assert decision.allowed is False
    assert decision.reason == "proposed ticket quote risk evidence is invalid"
