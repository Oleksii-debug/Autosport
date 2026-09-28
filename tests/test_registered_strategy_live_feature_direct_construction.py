from __future__ import annotations

import pytest

from autosport.registered_strategy_live_feature import (
    LIVE_FEATURE_FRESHNESS_POLICY_ID,
    LiveFeatureObservation,
    RegisteredStrategyLiveFeatureError,
)


def test_direct_constructor_cannot_mint_live_feature_authority() -> None:
    with pytest.raises(
        RegisteredStrategyLiveFeatureError,
        match="must be issued by the canonical product observer",
    ):
        LiveFeatureObservation(
            input_id="caller-minted-input",
            quote_key="provider-a:market-a:selection-a",
            feature=0.5,
            available_at="2026-01-03T00:00:02Z",
            decision_at="2026-01-03T00:00:10Z",
            freshness_policy_id=LIVE_FEATURE_FRESHNESS_POLICY_ID,
            freshness_max_age_seconds=10,
            freshness_timestamp="2026-01-03T00:00:00Z",
            market_event_sha256="a" * 64,
            market_snapshot_sha256="b" * 64,
            authority_sha256="c" * 64,
            evidence_sha256="d" * 64,
        )
