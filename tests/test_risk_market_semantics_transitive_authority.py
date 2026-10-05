from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.domain as domain_module
from autosport import _risk_market_semantics_identity as semantics_identity
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


_OBSERVED = "2026-10-05T15:00:00+00:00"
_SOURCE = "2026-10-05T14:59:59+00:00"
_INGEST = "2026-10-05T15:00:01+00:00"
_PROPOSAL = "2026-10-05T15:00:02+00:00"


def _leg(semantics: str) -> TicketLeg:
    return TicketLeg(
        "event-transitive-risk",
        "market-transitive-risk",
        "selection-transitive-risk",
        Decimal("2"),
        sport="football",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def _quote(semantics: str) -> MarketEvent:
    leg = _leg(semantics)
    return MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts=_OBSERVED,
        source_id="provider-transitive-risk",
        sequence=1,
        source_ts=_SOURCE,
        ingest_ts=_INGEST,
        sport=leg.sport,
        exchange_side=leg.exchange_side,
        market_semantics_id=semantics,
    )


def _context(semantics: str) -> ProposedTicketRiskContext:
    return ProposedTicketRiskContext(
        legs=(_leg(semantics),),
        quotes=(_quote(semantics),),
        bankroll_id="paper-main",
        currency="USD",
        proposal_ts=_PROPOSAL,
    )


def _policy() -> PaperRiskPolicy:
    goal = EconomicGoalContract(
        goal_id="goal-transitive-risk",
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
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )


def test_candidate_digest_rejects_captured_payload_code_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("rules:s1")
    assert PaperRiskPolicy.risk_of_ruin_candidate_sha256(context) is not None

    def forged_payload(*args, **kwargs):
        del args, kwargs
        return {"schema": "forged-risk-candidate"}

    monkeypatch.setattr(
        semantics_identity._candidate_payload,
        "__code__",
        forged_payload.__code__,
    )

    assert PaperRiskPolicy.risk_of_ruin_candidate_sha256(context) is None


def test_candidate_digest_rejects_canonical_domain_global_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("rules:s1")
    assert PaperRiskPolicy.risk_of_ruin_candidate_sha256(context) is not None

    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_STRING_VALUE",
        lambda value, field_name: value,
    )

    assert PaperRiskPolicy.risk_of_ruin_candidate_sha256(context) is None


def test_portfolio_digest_rejects_captured_payload_code_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg("rules:s1")], Decimal("1"), placed_at=_PROPOSAL)
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is not None

    def forged_payload(*args, **kwargs):
        del args, kwargs
        return {"schema": "forged-risk-portfolio"}

    monkeypatch.setattr(
        semantics_identity._portfolio_payload,
        "__code__",
        forged_payload.__code__,
    )

    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is None


def test_portfolio_digest_rejects_canonical_domain_global_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg("rules:s1")], Decimal("1"), placed_at=_PROPOSAL)
    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is not None

    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_RESERVED_SEMANTIC_IDENTITIES",
        frozenset(),
    )

    assert PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book) is None


def test_vector_digest_rejects_captured_scalar_digest_code_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context("rules:s1")
    assert PaperRiskPolicy.risk_of_ruin_candidate_vector_sha256((context,)) is not None

    def forged_digest(*args, **kwargs):
        del args, kwargs
        return "f" * 64

    monkeypatch.setattr(
        semantics_identity._risk_of_ruin_candidate_sha256,
        "__code__",
        forged_digest.__code__,
    )

    assert PaperRiskPolicy.risk_of_ruin_candidate_vector_sha256((context,)) is None


def test_evaluate_rejects_captured_market_semantics_validator_code_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context("rules:s1")
    object.__setattr__(context.quotes[0], "market_semantics_id", "rules:s2")

    def forged_validator(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(
        semantics_identity._validate_context_market_semantics,
        "__code__",
        forged_validator.__code__,
    )

    decision = policy.evaluate(book, Decimal("1"), context=context)

    assert decision.allowed is False
    assert decision.reason == "proposed ticket quote risk evidence is invalid"
