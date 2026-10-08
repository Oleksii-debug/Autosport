from __future__ import annotations

import pytest

from autosport.strategy_model_factory import (
    PromotionRule,
    WalkForwardEvaluationConfig,
    WalkForwardResult,
)


_SHA = "a" * 64


@pytest.mark.parametrize(
    "field",
    ("feature_definition_sha256", "feature_source_sha256"),
)
def test_walk_forward_feature_digest_rejects_uppercase_alias(field: str) -> None:
    values = {
        "feature_set_id": "features-v1",
        "feature_definition_sha256": _SHA,
        "feature_source_sha256": _SHA,
    }
    values[field] = _SHA.upper()

    with pytest.raises(ValueError, match="canonical lowercase SHA-256"):
        WalkForwardEvaluationConfig(**values)


def test_walk_forward_feature_digest_preserves_exact_lowercase_identity() -> None:
    config = WalkForwardEvaluationConfig(
        feature_set_id="features-v1",
        feature_definition_sha256=_SHA,
        feature_source_sha256=_SHA,
    )

    payload = config.canonical_payload()
    assert payload["feature_definition_sha256"] == _SHA
    assert payload["feature_source_sha256"] == _SHA


def test_walk_forward_config_identity_rejects_subclass_virtual_serialization() -> None:
    class HostileConfig(WalkForwardEvaluationConfig):
        __slots__ = ()

        def canonical_payload(self) -> dict[str, object]:
            raise AssertionError("config subclass serialization must not execute")

    config = HostileConfig()
    with pytest.raises(ValueError, match="exact WalkForwardEvaluationConfig"):
        _ = config.frozen_text
    with pytest.raises(ValueError, match="exact WalkForwardEvaluationConfig"):
        _ = config.config_sha256


def test_walk_forward_result_identity_rejects_subclass_virtual_serialization() -> None:
    class HostileResult(WalkForwardResult):
        __slots__ = ()

        def to_payload(self) -> dict[str, object]:
            raise AssertionError("result subclass serialization must not execute")

    result = HostileResult("mean-baseline-v1", (), "mse", 0.0)
    with pytest.raises(ValueError, match="exact WalkForwardResult"):
        _ = result.result_sha256


def test_promotion_rule_identity_rejects_subclass_virtual_serialization() -> None:
    class HostileRule(PromotionRule):
        __slots__ = ()

        def canonical_payload(self) -> dict[str, object]:
            raise AssertionError("promotion rule subclass serialization must not execute")

    rule = HostileRule(primary_metric="mse", minimum_improvement=0.0)
    with pytest.raises(ValueError, match="exact PromotionRule"):
        _ = rule.frozen_text
    with pytest.raises(ValueError, match="exact PromotionRule"):
        _ = rule.rule_sha256

