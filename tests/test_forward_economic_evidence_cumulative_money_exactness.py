from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    BetSide,
    FamilywiseAlphaRegistry,
    ForwardDecisionObservation,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicProtocol,
    ResolvedPolicyOutcome,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
UNIVERSE_SHA = "a" * 64
AUTHORITY_SHA = "b" * 64
EXECUTION_SHA = "c" * 64
SETTLEMENT_SHA = "d" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="cumulative-money-exactness-family",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="cumulative-money-exactness-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="cumulative-money-exactness-universe",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=2,
        risk_unit_currency=Decimal("1E100"),
        maximum_accepted_odds=Decimal("2"),
        maximum_drawdown_currency=Decimal("0"),
        absolute_lambda=Decimal("0.1"),
        paired_lambda=Decimal("0.1"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(sequence: int) -> ForwardDecisionObservation:
    event_digit = f"{sequence + 1:x}"
    challenger_digit = f"{sequence + 3:x}"
    champion_digit = f"{sequence + 5:x}"
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256=event_digit * 64,
        challenger_decision_sha256=challenger_digit * 64,
        champion_decision_sha256=champion_digit * 64,
    )


def _none_outcome(
    observation: ForwardDecisionObservation,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="champion",
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.champion_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2 + observation.sequence),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal(0),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
    )


def _challenger_outcome(
    observation: ForwardDecisionObservation,
    *,
    pnl: str,
    stake: str,
    settlement_hour: int,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.challenger_decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2 + observation.sequence),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal(stake),
        net_pnl_currency=Decimal(pnl),
        execution_evidence_sha256=EXECUTION_SHA,
        execution_accepted_at=T0 + timedelta(minutes=5 + observation.sequence),
        settlement_evidence_sha256=SETTLEMENT_SHA,
        settlement_available_at=T0 + timedelta(hours=settlement_hour),
    )


class _Resolver:
    authority_sha256 = AUTHORITY_SHA

    def __init__(
        self,
        rows: dict[tuple[str, int], ResolvedPolicyOutcome],
    ) -> None:
        self._rows = rows

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self._rows[(policy_id, sequence)]


def _summary_for(challenger_rows: tuple[tuple[str, str, int], ...]):
    observations = tuple(_observation(i) for i in range(len(challenger_rows)))
    rows: dict[tuple[str, int], ResolvedPolicyOutcome] = {}
    for observation, (pnl, stake, settlement_hour) in zip(
        observations,
        challenger_rows,
        strict=True,
    ):
        rows[("challenger", observation.sequence)] = _challenger_outcome(
            observation,
            pnl=pnl,
            stake=stake,
            settlement_hour=settlement_hour,
        )
        rows[("champion", observation.sequence)] = _none_outcome(observation)

    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = _Resolver(rows)
    for observation in observations:
        accumulator.record(observation, resolver)
    return accumulator.summary()


def test_subcontext_loss_after_large_gain_remains_exact_drawdown() -> None:
    summary = _summary_for(
        (
            ("1E100", "1E100", 1),
            ("-1E-50", "1E-50", 2),
        )
    )

    exact_total = Fraction(Decimal("1E100")) - Fraction(Decimal("1E-50"))
    exact_drawdown = Fraction(Decimal("1E-50"))

    assert Fraction(summary.challenger_total_pnl_currency) == exact_total
    assert Fraction(summary.challenger_peak_pnl_currency) == Fraction(
        Decimal("1E100")
    )
    assert Fraction(summary.challenger_max_drawdown_currency) == exact_drawdown
    assert summary.drawdown_guard_passed is False
    assert summary.positive_authority_verified is False
    assert summary.conditional_eprocess_verified is False
    assert summary.scientific_promotion_gate_passed is False


def test_subcontext_gain_after_large_loss_remains_in_exact_total() -> None:
    summary = _summary_for(
        (
            ("-1E100", "1E100", 1),
            ("1E-50", "1E-50", 2),
        )
    )

    exact_total = -Fraction(Decimal("1E100")) + Fraction(Decimal("1E-50"))

    assert Fraction(summary.challenger_total_pnl_currency) == exact_total
    assert summary.positive_authority_verified is False
    assert summary.conditional_eprocess_verified is False
    assert summary.scientific_promotion_gate_passed is False
