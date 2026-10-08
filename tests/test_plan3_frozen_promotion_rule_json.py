"""Plan-3 scientific negative evidence: frozen promotion rules reject ambiguous JSON."""

from __future__ import annotations

import json

import pytest

from autosport.scientific_registry import (
    PromotionEvidenceError,
    _frozen_promotion_rule_payload,
)


def _rule_text(*, metric: str = "roi") -> str:
    return json.dumps({
        "kind": "autosport-promotion-rule-v1",
        "primary_metric": metric,
        "minimum_improvement": 0.05,
        "minimum_effective_sample_size": 5,
    }, sort_keys=True, separators=(",", ":"))


def test_frozen_promotion_rule_accepts_unambiguous_canonical_evidence() -> None:
    assert _frozen_promotion_rule_payload(_rule_text())["primary_metric"] == "roi"


@pytest.mark.parametrize("malformed", [
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","primary_metric":"net_profit","minimum_improvement":0.05,"minimum_effective_sample_size":5}',
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","minimum_improvement":0.05,"minimum_improvement":0.99,"minimum_effective_sample_size":5}',
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","minimum_improvement":0.05,"minimum_effective_sample_size":5,"minimum_effective_sample_size":1}',
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","minimum_improvement":NaN,"minimum_effective_sample_size":5}',
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","minimum_improvement":Infinity,"minimum_effective_sample_size":5}',
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","minimum_improvement":-Infinity,"minimum_effective_sample_size":5}',
    '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi","minimum_improvement":0.05,"minimum_effective_sample_size":5,"auxiliary":{"key":1,"key":2}}',
])
def test_frozen_promotion_rule_rejects_duplicate_or_nonfinite_json(
    malformed: str,
) -> None:
    with pytest.raises(PromotionEvidenceError, match="not canonical JSON"):
        _frozen_promotion_rule_payload(malformed)
