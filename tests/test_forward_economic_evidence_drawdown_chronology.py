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
T0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="drawdown-chronology-family",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="drawdown-chronology-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="drawdown-chronology-universe",
        universe_sha256=SHA_E,
        authority_binding_sha256=SHA_F,
        alpha_registry=registry,
        minimum_events=3,
        risk_unit_currency=Decimal("100"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("70"),
        absolute_lambda=Decimal("0.1"),
        paired_lambda=Decimal("0.1"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(sequence: int) -> ForwardDecisionObservation:
    suffix = f"{sequence:x}"
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=SHA_E,
        universe_event_sha256=suffix * 64,
        challenger_decision_sha256=SHA_A[:-1] + suffix,
        champion_decision_sha256=SHA_B[:-1] + suffix,
    )


class ChronologyResolver:
    authority_sha256 = SHA_F

    # Candidate sequence deliberately differs from provider settlement chronology:
    #   sequence order:   -60, +100, -60  -> max drawdown 60
    #   settlement order: +100, -60, -60  -> max drawdown 120
    _challenger = {
        0: (Decimal("-60"), Decimal("60"), timedelta(hours=2)),
        1: (Decimal("100"), Decimal("100"), timedelta(hours=1)),
        2: (Decimal("-60"), Decimal("60"), timedelta(hours=3)),
    }

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        committed = T0 + timedelta(minutes=2, seconds=sequence)
        if policy_id == "champion":
            return ResolvedPolicyOutcome(
                policy_id=policy_id,
                sequence=sequence,
                universe_event_sha256=universe_event_sha256,
                decision_sha256=decision_sha256,
                decision_committed_at=committed,
                side=BetSide.NONE,
                accepted_odds=None,
                accepted_stake=None,
                net_pnl_currency=Decimal("0"),
                execution_evidence_sha256=None,
                execution_accepted_at=None,
                settlement_evidence_sha256=None,
                settlement_available_at=None,
            )

        pnl, stake, settlement_delay = self._challenger[sequence]
        return ResolvedPolicyOutcome(
            policy_id=policy_id,
            sequence=sequence,
            universe_event_sha256=universe_event_sha256,
            decision_sha256=decision_sha256,
            decision_committed_at=committed,
            side=BetSide.BACK,
            accepted_odds=Decimal("2"),
            accepted_stake=stake,
            net_pnl_currency=pnl,
            execution_evidence_sha256=SHA_C,
            execution_accepted_at=T0 + timedelta(minutes=3, seconds=sequence),
            settlement_evidence_sha256=SHA_D,
            settlement_available_at=T0 + settlement_delay,
        )


def test_drawdown_guard_uses_economic_chronology_not_candidate_sequence() -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = ChronologyResolver()

    for sequence in range(3):
        accumulator.record(_observation(sequence), resolver)

    summary = accumulator.summary()

    # Candidate-order accumulation reports only 60, but the provider-settlement
    # chronology realizes +100 -> +40 -> -20, a 120 peak-to-trough drawdown.
    # A 70-unit guard therefore must fail closed.
    assert summary.drawdown_guard_passed is False


class AmbiguousChronologyResolver(ChronologyResolver):
    _challenger = {
        0: (Decimal("50"), Decimal("50"), timedelta(hours=1)),
        1: (Decimal("-50"), Decimal("50"), timedelta(hours=1)),
        2: (Decimal("0"), Decimal("10"), timedelta(hours=2)),
    }


def test_drawdown_guard_fails_closed_when_same_instant_order_matters() -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = AmbiguousChronologyResolver()

    for sequence in range(3):
        accumulator.record(_observation(sequence), resolver)

    summary = accumulator.summary()

    # +50 and -50 become available at the exact same authoritative instant.
    # Their intra-instant order can change the drawdown path, so the product
    # must not choose candidate/refId ordering to manufacture a passing gate.
    assert summary.challenger_peak_pnl_currency == Decimal("50")
    assert summary.challenger_max_drawdown_currency == Decimal("50")
    assert summary.drawdown_guard_passed is False
