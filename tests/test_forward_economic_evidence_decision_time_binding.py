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
T0 = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


class Resolver:
    authority_sha256 = SHA_E

    def __init__(
        self,
        observation: ForwardDecisionObservation,
        *,
        challenger_committed_at: datetime,
        champion_committed_at: datetime,
    ) -> None:
        self._rows = {
            "challenger": self._outcome(
                "challenger",
                observation,
                challenger_committed_at,
            ),
            "champion": self._outcome(
                "champion",
                observation,
                champion_committed_at,
            ),
        }

    @staticmethod
    def _outcome(
        policy_id: str,
        observation: ForwardDecisionObservation,
        committed_at: datetime,
    ) -> ResolvedPolicyOutcome:
        decision_sha256 = (
            observation.challenger_decision_sha256
            if policy_id == "challenger"
            else observation.champion_decision_sha256
        )
        return ResolvedPolicyOutcome(
            policy_id=policy_id,
            sequence=observation.sequence,
            universe_event_sha256=observation.universe_event_sha256,
            decision_sha256=decision_sha256,
            decision_committed_at=committed_at,
            side=BetSide.NONE,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal("0"),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
        )

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self._rows[policy_id]


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="decision-time-family",
        total_alpha=Decimal("0.05"),
        allocations=(AlphaAllocation("challenger", Decimal("0.05")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="decision-time-protocol",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="decision-time-universe",
        universe_sha256=SHA_C,
        authority_binding_sha256=SHA_E,
        alpha_registry=registry,
        minimum_events=1,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("10"),
        absolute_lambda=Decimal("0.1"),
        paired_lambda=Decimal("0.1"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation() -> ForwardDecisionObservation:
    return ForwardDecisionObservation(
        sequence=0,
        universe_sha256=SHA_C,
        universe_event_sha256=SHA_D,
        challenger_decision_sha256=SHA_A,
        champion_decision_sha256=SHA_B,
    )


def _record(
    *,
    challenger_committed_at: datetime,
    champion_committed_at: datetime,
):
    observation = _observation()
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    step = accumulator.record(
        observation,
        Resolver(
            observation,
            challenger_committed_at=challenger_committed_at,
            champion_committed_at=champion_committed_at,
        ),
    )
    return step, accumulator.summary()


def test_step_payload_binds_exact_causal_decision_commit_instants() -> None:
    challenger_committed_at = T0 + timedelta(minutes=2, seconds=3)
    champion_committed_at = T0 + timedelta(minutes=2, seconds=7)

    step, _summary = _record(
        challenger_committed_at=challenger_committed_at,
        champion_committed_at=champion_committed_at,
    )

    assert step.challenger_decision_committed_at == challenger_committed_at
    assert step.champion_decision_committed_at == champion_committed_at
    assert step.to_payload()["challenger_decision_committed_at"] == (
        "2026-09-23T06:02:03Z"
    )
    assert step.to_payload()["champion_decision_committed_at"] == (
        "2026-09-23T06:02:07Z"
    )


def test_evidence_sha_changes_when_only_admitted_decision_time_changes() -> None:
    _step_a, summary_a = _record(
        challenger_committed_at=T0 + timedelta(minutes=2, seconds=3),
        champion_committed_at=T0 + timedelta(minutes=2, seconds=7),
    )
    _step_b, summary_b = _record(
        challenger_committed_at=T0 + timedelta(minutes=2, seconds=13),
        champion_committed_at=T0 + timedelta(minutes=2, seconds=17),
    )

    assert summary_a.evidence_sha256 != summary_b.evidence_sha256
