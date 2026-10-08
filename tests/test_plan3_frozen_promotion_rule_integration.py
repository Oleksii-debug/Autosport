"""Plan 3 Section 1: persisted frozen research rules must fail closed after restart."""

from __future__ import annotations

import pytest

import test_scientific_registry_promotion_protocol_guards as protocol_guards
from autosport.scientific_registry import PromotionEvidenceError, ScientificRegistry


@pytest.mark.parametrize(
    ("bad_rule", "expected_error"),
    [
        (
            '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi",'
            '"primary_metric":"net_profit","minimum_improvement":0.05,'
            '"minimum_effective_sample_size":5}',
            "not canonical JSON",
        ),
        (
            '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi",'
            '"minimum_improvement":0.05,"minimum_effective_sample_size":5,'
            '"auxiliary":{"metric":"roi","metric":"net_profit"}}',
            "not canonical JSON",
        ),
        (
            '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi",'
            '"minimum_improvement":NaN,"minimum_effective_sample_size":5}',
            "not canonical JSON",
        ),
        (
            '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi",'
            '"minimum_improvement":' + "9" * 400 + ','
            '"minimum_effective_sample_size":5}',
            "minimum improvement is invalid",
        ),
    ],
)
def test_restarted_registry_rejects_ambiguous_pre_registered_promotion_without_mutation(
    tmp_path, monkeypatch, bad_rule: str, expected_error: str
) -> None:
    # Malformed rules are deliberately *pre-registered* rather than injected
    # after evaluation. The durable protocol hash must be computed normally.
    monkeypatch.setattr(
        protocol_guards, "_frozen_promotion_rule_text", lambda: bad_rule
    )
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    protocol, _, _, evidence_id = protocol_guards._seed_foundation(registry)
    original_bytes = path.read_bytes()

    restarted = ScientificRegistry(path)
    assert restarted.get("ResearchProtocol", "protocol-1") is not None
    assert restarted.get("Experiment", "experiment-1") is not None
    with pytest.raises(PromotionEvidenceError, match=expected_error):
        restarted.record_promotion(protocol_guards._promotion(protocol, evidence_id))

    # The rejected economic promotion must not alter earlier scientific evidence.
    assert path.read_bytes() == original_bytes
    assert ScientificRegistry(path).get("PromotionDecision", "promotion-1") is None


def test_restarted_registry_accepts_unambiguous_frozen_promotion_once(tmp_path) -> None:
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    protocol, _, _, evidence_id = protocol_guards._seed_foundation(registry)
    restarted = ScientificRegistry(path)
    decision = protocol_guards._promotion(protocol, evidence_id)

    first_digest = restarted.record_promotion(decision)
    accepted_bytes = path.read_bytes()
    assert ScientificRegistry(path).get("PromotionDecision", "promotion-1") is not None

    assert ScientificRegistry(path).record_promotion(decision) == first_digest
    assert path.read_bytes() == accepted_bytes
