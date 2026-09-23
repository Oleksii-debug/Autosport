from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
UNIVERSE_SHA = "e" * 64
AUTHORITY_SHA = "f" * 64


def _sha(n: int) -> str:
    return f"{n:x}" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="cost-incidence-family",
        total_alpha=Decimal("0.1"),
        allocations=(AlphaAllocation("challenger", Decimal("0.1")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="cost-incidence-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="cost-incidence-universe",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=4,
        risk_unit_currency=Decimal("200"),
        maximum_accepted_odds=Decimal("3"),
        maximum_drawdown_currency=Decimal("70"),
        maximum_economic_cost_currency=Decimal("100"),
        absolute_lambda=Decimal("0.1"),
        paired_lambda=Decimal("0.1"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(sequence: int) -> ForwardDecisionObservation:
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256=_sha(sequence + 1),
        challenger_decision_sha256=_sha(sequence + 5),
        champion_decision_sha256=_sha(sequence + 9),
    )


def _none(
    policy_id: str,
    obs: ForwardDecisionObservation,
    *,
    net: str = "0",
    cost: str = "0",
    cost_incurred_at: datetime | None = None,
    cost_available_at: datetime | None = None,
) -> ResolvedPolicyOutcome:
    kwargs: dict[str, object] = {}
    if cost_available_at is not None:
        kwargs.update(
            economic_cost_currency=Decimal(cost),
            economic_cost_evidence_sha256=_sha(13 + obs.sequence),
            economic_cost_incurred_at=cost_incurred_at,
            economic_cost_available_at=cost_available_at,
        )
    return ResolvedPolicyOutcome(
        policy_id=policy_id,
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=(
            obs.challenger_decision_sha256
            if policy_id == "challenger"
            else obs.champion_decision_sha256
        ),
        decision_committed_at=T0 + timedelta(minutes=10 + obs.sequence),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal(net),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        **kwargs,
    )


def _executed(
    policy_id: str,
    obs: ForwardDecisionObservation,
    *,
    pnl: str,
    stake: str,
    settlement_at: datetime,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id=policy_id,
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=(
            obs.challenger_decision_sha256
            if policy_id == "challenger"
            else obs.champion_decision_sha256
        ),
        decision_committed_at=T0 + timedelta(minutes=10 + obs.sequence),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal(stake),
        net_pnl_currency=Decimal(pnl),
        execution_evidence_sha256=_sha(2 + obs.sequence),
        execution_accepted_at=T0 + timedelta(minutes=20 + obs.sequence),
        settlement_evidence_sha256=_sha(10 + obs.sequence),
        settlement_available_at=settlement_at,
    )


class _Resolver:
    authority_sha256 = AUTHORITY_SHA

    def __init__(self, mapping: dict[tuple[int, str], ResolvedPolicyOutcome]) -> None:
        self._mapping = mapping

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self._mapping[(sequence, policy_id)]


def test_predecision_incurred_cost_can_be_proven_later_without_retimestamping() -> None:
    obs = _observation(0)
    incurred_at = T0 + timedelta(minutes=5)
    available_at = T0 + timedelta(hours=5)

    outcome = _none(
        "challenger",
        obs,
        net="-60",
        cost="60",
        cost_incurred_at=incurred_at,
        cost_available_at=available_at,
    )

    # The economic debit belongs to the frozen campaign/member at its canonical
    # incurred/effective instant.  A later source verification controls when the
    # evidence may be consumed, but must not rewrite when the money was incurred.
    assert outcome.economic_cost_incurred_at == incurred_at
    assert outcome.economic_cost_available_at == available_at
    assert outcome.economic_cost_incurred_at < outcome.decision_committed_at


def test_realized_drawdown_uses_cost_incidence_not_late_evidence_availability() -> None:
    observations = [_observation(i) for i in range(4)]
    mapping: dict[tuple[int, str], ResolvedPolicyOutcome] = {}

    # True capital chronology:
    # t1 +100 -> t2 -60 cost -> t3 -50 -> t4 +110 = max drawdown 110.
    # The cost receipt is only reverified/available at t5.  Booking the debit at
    # t5 instead would fabricate 0 -> 100 -> 50 -> 160 -> 100, max drawdown 60.
    mapping[(0, "challenger")] = _executed(
        "challenger",
        observations[0],
        pnl="100",
        stake="100",
        settlement_at=T0 + timedelta(hours=1),
    )
    mapping[(1, "challenger")] = _none(
        "challenger",
        observations[1],
        net="-60",
        cost="60",
        cost_incurred_at=T0 + timedelta(hours=2),
        cost_available_at=T0 + timedelta(hours=5),
    )
    mapping[(2, "challenger")] = _executed(
        "challenger",
        observations[2],
        pnl="-50",
        stake="50",
        settlement_at=T0 + timedelta(hours=3),
    )
    mapping[(3, "challenger")] = _executed(
        "challenger",
        observations[3],
        pnl="110",
        stake="110",
        settlement_at=T0 + timedelta(hours=4),
    )

    for obs in observations:
        mapping[(obs.sequence, "champion")] = _none("champion", obs)

    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = _Resolver(mapping)
    for obs in observations:
        accumulator.record(obs, resolver)

    summary = accumulator.summary()
    assert summary.challenger_total_pnl_currency == Decimal("100")
    assert summary.challenger_peak_pnl_currency == Decimal("100")
    assert summary.challenger_max_drawdown_currency == Decimal("110")
    assert summary.drawdown_guard_passed is False
    assert summary.positive_authority_verified is False
    assert summary.conditional_eprocess_verified is False
    assert summary.scientific_promotion_gate_passed is False
    assert summary.promotion_authority is False
