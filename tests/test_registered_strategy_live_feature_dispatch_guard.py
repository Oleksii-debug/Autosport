from __future__ import annotations

import pytest

import autosport.registered_strategy_live_feature as feature_module


def test_observer_rejects_authority_resolver_rebind_before_forged_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False

    def forged_resolver(*args, **kwargs):
        nonlocal forged_called
        forged_called = True
        raise AssertionError("forged authority resolver must not run")

    monkeypatch.setattr(
        feature_module,
        "resolve_registered_live_feature_authority",
        forged_resolver,
    )

    with pytest.raises(
        feature_module.RegisteredStrategyLiveFeatureError,
        match="dependency 'resolve_registered_live_feature_authority' changed",
    ):
        feature_module.observe_registered_strategy_live_features(
            "input-v1",
            None,
            decision_at="2026-09-25T12:00:00Z",
            freshness_max_age_seconds=5,
            registry=None,
            model_version_id="model-v1",
        )

    assert forged_called is False


def test_observer_rejects_evidence_hasher_rebind_before_forged_hasher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged_called = False

    def forged_hasher(*args, **kwargs):
        nonlocal forged_called
        forged_called = True
        return "0" * 64

    monkeypatch.setattr(
        feature_module,
        "_canonical_json_sha256",
        forged_hasher,
    )

    with pytest.raises(
        feature_module.RegisteredStrategyLiveFeatureError,
        match="dependency '_canonical_json_sha256' changed",
    ):
        feature_module.observe_registered_strategy_live_features(
            "input-v1",
            None,
            decision_at="2026-09-25T12:00:00Z",
            freshness_max_age_seconds=5,
            registry=None,
            model_version_id="model-v1",
        )

    assert forged_called is False


def test_observer_rejects_contract_rebind_before_observation() -> None:
    original = feature_module.LIVE_FEATURE_DEFINITION_SHA256
    feature_module.LIVE_FEATURE_DEFINITION_SHA256 = "0" * 64
    try:
        with pytest.raises(
            feature_module.RegisteredStrategyLiveFeatureError,
            match="contract 'LIVE_FEATURE_DEFINITION_SHA256' changed",
        ):
            feature_module.observe_registered_strategy_live_features(
                "input-v1",
                None,
                decision_at="2026-09-25T12:00:00Z",
                freshness_max_age_seconds=5,
                registry=None,
                model_version_id="model-v1",
            )
    finally:
        feature_module.LIVE_FEATURE_DEFINITION_SHA256 = original
