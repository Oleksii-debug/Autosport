from __future__ import annotations

import pytest

from autosport.strategy_model_factory import WalkForwardEvaluationConfig


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
