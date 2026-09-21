from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

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
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def registry(*, alpha: str = "0.2", total: str = "0.2") -> FamilywiseAlphaRegistry:
    return FamilywiseAlphaRegistry(
        family_id="forward-family-1",
        total_alpha=Decimal(total),
        allocations=(AlphaAllocation("challenger", Decimal(alpha)),),
        sealed_at=T0,
    )


def protocol(**overrides) -> ForwardEconomicProtocol:
    values = dict(
        protocol_id="forward-protocol-1",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe-1",
        universe_sha256=SHA_E,
        authority_binding_sha256=SHA_F,
        alpha_registry=registry(),
        minimum_events=5,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )
    values.update(overrides)
    return ForwardEconomicProtocol(**values)


def observation(seq: int) -> ForwardDecisionObservation:
    return ForwardDecisionObservation(
        sequence=seq,
        universe_sha256=SHA_E,
        universe_event_sha256=f"{seq % 16:x}" * 64,
        challenger_decision_sha256=SHA_A[:-1] + f"{seq % 16:x}",
        champion_decision_sha256=SHA_B[:-1] + f"{seq % 16:x}",
    )


def outcome(
    policy_id: str,
    obs: ForwardDecisionObservation,
    *,
    side: BetSide,
    pnl: str,
    odds: str | None = "2",
    stake: str | None = "10",
    decision_sha: str | None = None,
) -> ResolvedPolicyOutcome:
    if side is BetSide.NONE:
        return ResolvedPolicyOutcome(
            policy_id=policy_id,
            sequence=obs.sequence,
            universe_event_sha256=obs.universe_event_sha256,
            decision_sha256=decision_sha or (
                obs.challenger_decision_sha256 if policy_id == "challenger" else obs.champion_decision_sha256
            ),
            decision_committed_at=T0 + timedelta(minutes=2, seconds=obs.sequence),
            side=side,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal(pnl),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
        )
    return ResolvedPolicyOutcome(
        policy_id=policy_id,
        sequence=obs.sequence,
        universe_event_sha256=obs.universe_event_sha256,
        decision_sha256=decision_sha or (
            obs.challenger_decision_sha256 if policy_id == "challenger" else obs.champion_decision_sha256
        ),
        decision_committed_at=T0 + timedelta(minutes=2, seconds=obs.sequence),
        side=side,
        accepted_odds=Decimal(odds),
        accepted_stake=Decimal(stake),
        net_pnl_currency=Decimal(pnl),
        execution_evidence_sha256=SHA_C,
        execution_accepted_at=T0 + timedelta(minutes=3, seconds=obs.sequence),
        settlement_evidence_sha256=SHA_D,
        settlement_available_at=T0 + timedelta(hours=1, seconds=obs.sequence),
    )


class Resol