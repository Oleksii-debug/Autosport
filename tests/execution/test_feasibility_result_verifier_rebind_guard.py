from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import autosport.execution.feasibility as feasibility_module
from autosport.execution.feasibility import (
    ExecutionFeasibilitySnapshot,
    FeasibilityState,
    ProjectionKind,
    SourceMode,
)


def _forged_positive() -> ExecutionFeasibilitySnapshot:
    decision_at = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    return ExecutionFeasibilitySnapshot(
        state=FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY,
        evidence_digest="f" * 64,
        reasons=(),
        requested_stake=Decimal("10"),
        limit_price=Decimal("2"),
        displayed_acceptable_depth=Decimal("10"),
        snapshot_id="caller-forged-snapshot",
        snapshot_digest="e" * 64,
        provider_id="betfair",
        account_id="acct-1",
        market_id="1.234",
        selection_id="42",
        market_version=1,
        inplay=False,
        bet_delay_seconds=0,
        observed_at=decision_at - timedelta(milliseconds=100),
        received_at=decision_at - timedelta(milliseconds=50),
        decision_at=decision_at,
        source_mode=SourceMode.LIVE,
        projection_kind=ProjectionKind.EX_ALL_OFFERS,
        liquidity_overlap_key="d" * 64,
    )


def test_module_verifier_rebind_cannot_mint_positive_feasibility() -> None:
    """The sufficient property must not resolve authority through a writable global."""

    forged = _forged_positive()
    assert forged.sufficient is False
    assert not hasattr(
        feasibility_module,
        "_is_execution_feasibility_result_authoritative",
    )
    assert not hasattr(
        feasibility_module,
        "_install_execution_feasibility_result_authority",
    )

    feasibility_module._is_execution_feasibility_result_authoritative = lambda _: True
    try:
        assert forged.sufficient is False
    finally:
        del feasibility_module._is_execution_feasibility_result_authoritative
