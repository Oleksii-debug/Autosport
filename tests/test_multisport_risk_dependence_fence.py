from __future__ import annotations

from decimal import Decimal

from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


def _goal(**overrides: object) -> EconomicGoalContract:
    values: dict[str, object] = {
        "goal_id": "goal-cross-sport-dependence",
        "revision": 1,
        "bankroll_id": "paper-bankroll",
        "currency": "USD",
        "max_stake_fraction": Decimal("1"),
        "max_session_loss_fraction": Decimal("1"),
        "max_day_loss_fraction": Decimal("1"),
        "max_drawdown_fraction": Decimal("1"),
        "max_capital_at_risk_fraction": Decimal("1"),
        "max_turnover_fraction": Decimal("1000"),
        "max_risk_of_ruin": Decimal("0.20"),
        "max_concurrent_positions": 20,
        "max_execution_slippage_fraction": Decimal("1"),
        "max_quote_age_seconds": Decimal("3600"),
    }
    values.update(overrides)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


def _context(sport: str, sequence: int) -> ProposedTicketRiskContext:
    leg = TicketLeg(
        "same-event",
        "same-market",
        "same-selection",
        Decimal("2"),
        sport=sport,
    )
    quote = MarketEvent(
        event_id="same-event",
        market_id="same-market",
        selection_id="same-selection",
        decimal_odds=Decimal("2.10"),
        observed_ts="2026-09-22T02:00:00Z",
        source_id=f"test-only:{sport}",
        sequence=sequence,
        market_type=MarketType.WINNER,
        source_ts="2026-09-22T01:59:59Z",
        ingest_ts="2026-09-22T02:00:01Z",
        sport=sport,
        competition_id="test-only-competition",
        market_semantics_id="test-only-winner-v1",
        provider_source_class="test-only-engineering-conformance",
        metadata={"evidence_grade": "TEST_ONLY_ENGINEERING_CONFORMANCE"},
    )
    return ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts="2026-09-22T02:00:02Z",
    )


def test_cross_sport_candidates_require_joint_risk_authority_not_independence_assumption() -> None:
    table_tennis = _context("table_tennis", 1)
    second_sport = _context("test-only-second-sport", 2)

    assert table_tennis.legs[0].quote_key != second_sport.legs[0].quote_key

    first_digest = PaperRiskPolicy.risk_of_ruin_candidate_sha256(table_tennis)
    second_digest = PaperRiskPolicy.risk_of_ruin_candidate_sha256(second_sport)
    assert first_digest is not None
    assert second_digest is not None
    assert first_digest != second_digest

    decision = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(),
    ).derive_goal_stake_vector(
        PaperBook("100"),
        (Decimal("0.25"), Decimal("0.25")),
        contexts=(table_tennis, second_sport),
    )

    assert decision.action == "WAIT"
    assert decision.stakes == (Decimal("0"), Decimal("0"))
    assert (
        decision.reason
        == "multi-candidate portfolio risk-of-ruin requires vector-bound evidence"
    )


def test_cross_sport_identity_never_collapses_to_one_candidate() -> None:
    table_tennis = _context("table_tennis", 1)
    second_sport = _context("test-only-second-sport", 1)

    decision = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("0.30"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(max_risk_of_ruin=Decimal("1")),
    ).derive_goal_stake_vector(
        PaperBook("100"),
        (Decimal("0.25"), Decimal("0.25")),
        contexts=(table_tennis, second_sport),
    )

    # With probabilistic ruin evidence disabled, exact arithmetic capital limits
    # may still aggregate exposures. Different sports do not create extra room.
    assert decision.action == "STAKE_VECTOR"
    assert sum(decision.stakes, Decimal("0")) == Decimal("30")
