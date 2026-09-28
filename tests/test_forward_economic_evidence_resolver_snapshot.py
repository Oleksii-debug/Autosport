from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

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
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
UNIVERSE_SHA = "e" * 64
AUTHORITY_SHA = "f" * 64
EXECUTION_SHA = "3" * 64
SETTLEMENT_SHA = "4" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="resolver-snapshot-family",
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
        protocol_id="resolver-snapshot-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="resolver-snapshot-universe",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=2,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("3"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.1"),
        paired_lambda=Decimal("0.1"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(sequence: int, event_digit: str) -> ForwardDecisionObservation:
    challenger_digit = format(sequence + 10, "x")
    champion_digit = format(sequence + 12, "x")
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256=event_digit * 64,
        challenger_decision_sha256=challenger_digit * 64,
        champion_decision_sha256=champion_digit * 64,
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


class StaticResolver:
    authority_sha256 = AUTHORITY_SHA

    def __init__(
        self,
        challenger: ResolvedPolicyOutcome,
        champion: ResolvedPolicyOutcome,
    ) -> None:
        self.challenger = challenger
        self.champion = champion

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        del sequence, universe_event_sha256, decision_sha256
        if policy_id == "challenger":
            return self.challenger
        return self.champion


class OutcomeMutatingResolver(StaticResolver):
    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        if policy_id == "challenger":
            return self.challenger

        # Mutate the previously returned caller-owned object after it has
        # already passed _resolve_exact().  Without an internal snapshot, these
        # unvalidated authority fields leak into the canonical recorded step.
        object.__setattr__(
            self.challenger,
            "decision_committed_at",
            T0 - timedelta(minutes=1),
        )
        object.__setattr__(
            self.challenger,
            "settlement_evidence_sha256",
            "9" * 64,
        )
        return super().resolve(
            policy_id=policy_id,
            sequence=sequence,
            universe_event_sha256=universe_event_sha256,
            decision_sha256=decision_sha256,
        )


class ObservationMutatingResolver(StaticResolver):
    def __init__(
        self,
        *,
        external_observation: ForwardDecisionObservation,
        duplicate_event_sha256: str,
        challenger: ResolvedPolicyOutcome,
        champion: ResolvedPolicyOutcome,
    ) -> None:
        super().__init__(challenger, champion)
        self.external_observation = external_observation
        self.duplicate_event_sha256 = duplicate_event_sha256

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        del sequence, universe_event_sha256, decision_sha256
        if policy_id == "challenger":
            # The duplicate-member guard already ran before this callback.
            # A caller-owned observation must not be able to change the member
            # identity that record() is currently processing.
            object.__setattr__(
                self.external_observation,
                "universe_event_sha256",
                self.duplicate_event_sha256,
            )
            return self.challenger
        return self.champion


def test_later_resolve_cannot_mutate_already_validated_outcome() -> None:
    observation = _observation(0, "1")
    challenger = _challenger(observation)
    original_committed_at = challenger.decision_committed_at
    original_settlement_sha = challenger.settlement_evidence_sha256
    resolver = OutcomeMutatingResolver(
        challenger=challenger,
        champion=_champion(observation),
    )

    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    step = accumulator.record(observation, resolver)

    assert challenger.decision_committed_at < accumulator.protocol.frozen_at
    assert challenger.settlement_evidence_sha256 == "9" * 64
    assert step.challenger_decision_committed_at == original_committed_at
    assert (
        step.challenger_settlement_evidence_sha256
        == original_settlement_sha
    )
    assert accumulator.steps == (step,)
    summary = accumulator.summary()
    assert summary.positive_authority_verified is False
    assert summary.conditional_eprocess_verified is False
    assert summary.scientific_promotion_gate_passed is False


def test_resolver_cannot_relabel_observation_after_member_guard() -> None:
    first = _observation(0, "1")
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    accumulator.record(
        first,
        StaticResolver(
            challenger=_challenger(first),
            champion=_champion(first),
        ),
    )
    before = accumulator.summary()

    second = _observation(1, "2")
    duplicate_event_sha256 = first.universe_event_sha256
    relabelled_for_resolver = ForwardDecisionObservation(
        sequence=second.sequence,
        universe_sha256=second.universe_sha256,
        universe_event_sha256=duplicate_event_sha256,
        challenger_decision_sha256=second.challenger_decision_sha256,
        champion_decision_sha256=second.champion_decision_sha256,
    )
    resolver = ObservationMutatingResolver(
        external_observation=second,
        duplicate_event_sha256=duplicate_event_sha256,
        challenger=_challenger(relabelled_for_resolver),
        champion=_champion(relabelled_for_resolver),
    )

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="resolved universe event mismatch",
    ):
        accumulator.record(second, resolver)

    # The caller object was mutated, but the accumulator's snapshotted
    # observation and already-committed evidence remain unchanged.
    assert second.universe_event_sha256 == duplicate_event_sha256
    assert accumulator.steps[0].universe_event_sha256 == duplicate_event_sha256
    assert len(accumulator.steps) == 1
    assert accumulator.next_sequence == 1
    after = accumulator.summary()
    assert after.evidence_sha256 == before.evidence_sha256
    assert after.observed_events == 1
    assert after.scientific_promotion_gate_passed is False
