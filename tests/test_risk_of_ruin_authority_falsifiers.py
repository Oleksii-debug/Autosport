from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import (
    PaperRiskPolicy,
    ProposedTicketRiskContext,
    RiskOfRuinEvidence,
    RiskOfRuinVectorEvidence,
)


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-ror-authority-falsifier",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("0.01"),
        max_concurrent_positions=10,
    )


def _policy() -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(),
    )


def _context(index: int) -> ProposedTicketRiskContext:
    leg = TicketLeg(
        f"event-{index}",
        f"market-{index}",
        f"selection-{index}",
        Decimal("2"),
    )
    quote = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts="2026-09-16T15:00:00+00:00",
        source_id="provider-1",
        sequence=index,
        source_ts="2026-09-16T14:59:59+00:00",
        ingest_ts="2026-09-16T15:00:01+00:00",
    )
    return ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts="2026-09-16T15:00:02+00:00",
    )


def test_caller_cannot_mint_single_candidate_risk_of_ruin_authority() -> None:
    """Public hashes plus a caller-authored upper bound must not grant risk authority."""

    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    portfolio_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
    candidate_sha256 = policy.risk_of_ruin_candidate_sha256(context)
    assert portfolio_sha256 is not None
    assert candidate_sha256 is not None

    caller_minted = RiskOfRuinEvidence(
        evidence_id="caller-minted-ror",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="caller-claims-to-be-risk-model",
        causal_cutoff="2026-09-16T14:59:58+00:00",
        evaluated_at="2026-09-16T15:00:01+00:00",
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio_sha256,
        candidate_sha256=candidate_sha256,
        evaluated_stake=Decimal("1"),
        upper_bound=Decimal("0"),
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=caller_minted),
    )

    assert not decision.allowed, (
        "caller-created RiskOfRuinEvidence currently mints positive risk authority; "
        "acceptance must require product-issued durable scientific provenance"
    )


def test_caller_cannot_mint_vector_risk_of_ruin_authority() -> None:
    """A fabricated whole-vector witness must not authorize a positive stake vector."""

    policy = _policy()
    book = PaperBook("100")
    contexts = (_context(1), _context(2))
    portfolio_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
    candidate_vector_sha256 = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio_sha256 is not None
    assert candidate_vector_sha256 is not None

    caller_minted = RiskOfRuinVectorEvidence(
        evidence_id="caller-minted-vector-ror",
        research_protocol_sha256="c" * 64,
        reproducibility_bundle_sha256="d" * 64,
        producer_identity="caller-claims-to-be-vector-risk-model",
        causal_cutoff="2026-09-16T14:59:58+00:00",
        evaluated_at="2026-09-16T15:00:01+00:00",
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio_sha256,
        candidate_vector_sha256=candidate_vector_sha256,
        evaluated_stakes=(Decimal("1"), Decimal("1")),
        upper_bound=Decimal("0"),
    )

    decision = policy.derive_goal_stake_vector(
        book,
        (Decimal("0.01"), Decimal("0.01")),
        contexts=contexts,
        risk_of_ruin_vector_evidence=caller_minted,
    )

    assert decision.action != "STAKE_VECTOR", (
        "caller-created RiskOfRuinVectorEvidence currently mints positive vector "
        "risk authority; acceptance must require product-issued durable scientific provenance"
    )
