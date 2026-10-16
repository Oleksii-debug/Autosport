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


def test_caller_cannot_mint_positive_execution_feasibility_result() -> None:
    """Positive feasibility must come from the canonical product resolver only."""

    decision_at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    forged = ExecutionFeasibilitySnapshot(
        state=FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY,
        evidence_digest="f" * 64,
        reasons=(),
        requested_stake=Decimal("12"),
        limit_price=Decimal("2"),
        displayed_acceptable_depth=Decimal("12"),
        snapshot_id="caller-forged-snapshot",
        snapshot_digest="e" * 64,
        provider_id="betfair",
        account_id="acct-1",
        market_id="1.234",
        selection_id="42",
        market_version=17,
        inplay=False,
        bet_delay_seconds=0,
        observed_at=decision_at - timedelta(milliseconds=100),
        received_at=decision_at - timedelta(milliseconds=50),
        decision_at=decision_at,
        source_mode=SourceMode.LIVE,
        projection_kind=ProjectionKind.EX_ALL_OFFERS,
        liquidity_overlap_key="d" * 64,
    )

    assert forged.state is FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY
    assert forged.sufficient is False

def test_result_authority_exposes_no_module_level_mint_or_registry() -> None:
    """Ordinary imports must not expose a writable canonical result mint."""

    assert not hasattr(
        feasibility_module,
        "_EXECUTION_FEASIBILITY_RESULT_ISSUED",
    )
    assert not hasattr(
        feasibility_module,
        "_issue_execution_feasibility_result",
    )
    assert (
        feasibility_module.EXECUTION_FEASIBILITY_RESULT_TRUST_BOUNDARY
        == "trusted-process-api-provenance-v1"
    )

