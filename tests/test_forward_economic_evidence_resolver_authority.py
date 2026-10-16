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
T0 = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="resolver-forgery-family",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="resolver-forgery-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="resolver-forgery-universe",
        universe_sha256=SHA_E,
        authority_binding_sha256=SHA_F,
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
    suffix = f"{sequence:x}"
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=SHA_E,
        universe_event_sha256=suffix * 64,
        challenger_decision_sha256=SHA_A[:-1] + suffix,
        champion_decision_sha256=SHA_B[:-1] + suffix,
    )


class CallerMintedResolver:
    """Deliberately has no product-issued execution/settlement authority."""

    authority_sha256 = SHA_F

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
        return ResolvedPolicyOutcome(
            policy_id=policy_id,
            sequence=sequence,
            universe_event_sha256=universe_event_sha256,
            decision_sha256=decision_sha256,
            decision_committed_at=committed,
            side=BetSide.BACK,
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("10"),
            net_pnl_currency=Decimal("10"),
            execution_evidence_sha256=SHA_C,
            execution_accepted_at=T0 + timedelta(minutes=3, seconds=sequence),
            settlement_evidence_sha256=SHA_D,
            settlement_available_at=T0 + timedelta(hours=1, seconds=sequence),
        )


def test_caller_minted_resolver_cannot_mint_positive_scientific_gate() -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = CallerMintedResolver()

    blocked = False
    try:
        for sequence in range(5):
            accumulator.record(_observation(sequence), resolver)
    except ForwardEconomicEvidenceError:
        blocked = True

    if blocked:
        return

    # If the implementation chooses to retain caller assertions for audit,
    # they still must never become positive scientific evidence.
    summary = accumulator.summary()
    assert summary.positive_authority_verified is False
    assert summary.scientific_promotion_gate_passed is False
    assert summary.promotion_authority is False
