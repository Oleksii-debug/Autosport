from __future__ import annotations

from types import SimpleNamespace

from autosport.causal_collector import SyncState
from autosport.product_paper_decision_cycle import ProductPaperDecisionCycle


def _healthy_tick() -> SimpleNamespace:
    return SimpleNamespace(
        source_provider_unavailable=False,
        invalidation_backlog=False,
    )


def _status(*, source_sync_state: str) -> SimpleNamespace:
    return SimpleNamespace(
        source_provider_unavailable=False,
        source_sync_state=source_sync_state,
        source_unresolved_gap_delta_ids=(),
        source_state_projection_backlog=False,
        invalidation_full_refresh_required=False,
        invalidation_pending_count=0,
    )


def test_product_paper_decision_skips_source_retry_required() -> None:
    reason = ProductPaperDecisionCycle._decision_skip_reason(
        _healthy_tick(),
        _status(source_sync_state=SyncState.RETRY_REQUIRED.value),
    )

    assert reason == "source_retry_required"


def test_product_paper_decision_does_not_skip_ready_source_projection() -> None:
    reason = ProductPaperDecisionCycle._decision_skip_reason(
        _healthy_tick(),
        _status(source_sync_state=SyncState.READY.value),
    )

    assert reason is None
