from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    BetSide,
    FamilywiseAlphaRegistry,
    ForwardDecisionObservation,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
    ResolvedPolicyOutcome,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
UNIVERSE_SHA = "e" * 64
AUTHORITY_SHA = "f" * 64
EXECUTION_SHA = "c" * 64
SETTLEMENT_SHA = "d" * 64
SUBSTITUTED_SETTLEMENT_SHA = "9" * 64


class Resolver:
    def __init__(self, mapping: dict[tuple[int, str], ResolvedPolicyOutcome]) -> None:
        self.mapping = mapping
        self.authority_sha256 = AUTHORITY_SHA

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self.mapping[(sequence, policy_id)]


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="forward-family-step-integrity",
        total_alpha=Decimal("0.2"),
        allocations=(
            AlphaAllocation(
                challenger_id="challenger",
                alpha=Decimal("0.2"),
            ),
        ),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="forward-protocol-step-integrity",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe-step-integrity",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=5,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(sequence: int) -> ForwardDecisionObservation:
    suffix = f"{sequence + 1:x}"
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256=suffix * 64,
        challenger_decision_sha256=("a" * 63) + suffix,
        champion_decision_sha256=("b" * 63) + suffix,
    )


def _challenger(
    observation: ForwardDecisionObservation,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.challenger_decision_sha256,
        decision_committed_at=T0
        + timedelta(minutes=2, seconds=observation.sequence),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal("10"),
        execution_evidence_sha256=EXECUTION_SHA,
        execution_accepted_at=T0
        + timedelta(minutes=3, seconds=observation.sequence),
        settlement_evidence_sha256=SETTLEMENT_SHA,
        settlement_available_at=T0
        + timedelta(hours=1, seconds=observation.sequence),
    )


def _champion(
    observation: ForwardDecisionObservation,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="champion",
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.champion_decision_sha256,
        decision_committed_at=T0
        + timedelta(minutes=2, seconds=observation.sequence),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("0"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
    )


def test_recorded_step_cannot_rebind_positive_evidence_after_publication() -> None:
    observations = [_observation(sequence) for sequence in range(5)]
    mapping: dict[tuple[int, str], ResolvedPolicyOutcome] = {}
    for observation in observations:
        mapping[(observation.sequence, "challenger")] = _challenger(observation)
        mapping[(observation.sequence, "champion")] = _champion(observation)

    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = Resolver(mapping)
    record_return = None
    for observation in observations:
        record_return = accumulator.record(observation, resolver)
    assert record_return is not None

    before = accumulator.summary()
    assert before.scientific_promotion_gate_passed is True
    assert (
        accumulator.steps[-1].challenger_settlement_evidence_sha256
        == SETTLEMENT_SHA
    )

    object.__setattr__(
        record_return,
        "challenger_settlement_evidence_sha256",
        SUBSTITUTED_SETTLEMENT_SHA,
    )
    assert (
        accumulator.steps[-1].challenger_settlement_evidence_sha256
        == SETTLEMENT_SHA
    )
    assert accumulator.summary().evidence_sha256 == before.evidence_sha256

    # Dataclass frozen=True is not an authority boundary.  If a caller-visible
    # step is the accumulator's stored object, object.__setattr__ can substitute
    # a different settlement lineage after the statistics were accumulated.
    exposed_step = accumulator.steps[-1]
    try:
        object.__setattr__(
            exposed_step,
            "challenger_settlement_evidence_sha256",
            SUBSTITUTED_SETTLEMENT_SHA,
        )
    except (AttributeError, TypeError):
        # A future implementation may expose a non-mutable value/copy.
        return

    try:
        after = accumulator.summary()
    except ForwardEconomicEvidenceError:
        # Detecting post-publication integrity drift and failing closed is safe.
        return

    # A safe copy-by-value/snapshot implementation keeps canonical published
    # evidence unchanged even if an exposed object can itself be mutated.
    assert (
        accumulator.steps[-1].challenger_settlement_evidence_sha256
        == SETTLEMENT_SHA
    )
    assert after.evidence_sha256 == before.evidence_sha256
    assert after.scientific_promotion_gate_passed is True
