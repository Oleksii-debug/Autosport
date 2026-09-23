from autosport.scientific_registry import ResearchProtocol, ScientificRegistry
from autosport.scientific_registry_index import ScientificRegistryIndex
from autosport.strategy_experiment import ScientificProtocolBinding


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"


def _binding(
    protocol_id: str,
    *,
    frozen_at: str,
    feature_set_version: str = "features-v1",
) -> ScientificProtocolBinding:
    return ScientificProtocolBinding(
        research_protocol_id=protocol_id,
        research_question_id="question-1",
        research_question_sha256=SHA_A,
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=SHA_B,
        inclusion_criteria="predeclared events",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="lawful source evidence retained",
        causal_cutoff=T1,
        evaluation_design="sealed walk-forward holdout",
        feature_set_version=feature_set_version,
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule="promote only if primary improves and guardrails pass",
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_C,
        frozen_at_utc=frozen_at,
    )


def _protocol(
    protocol_id: str,
    *,
    frozen_at: str,
    feature_set_version: str = "features-v1",
) -> ResearchProtocol:
    return ResearchProtocol(
        _binding(
            protocol_id,
            frozen_at=frozen_at,
            feature_set_version=feature_set_version,
        ),
        SHA_C,
        SHA_D,
        SHA_A,
        T0,
    )


def test_protocol_fingerprint_discovers_semantic_duplicate_across_ids_and_restart(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    first = _protocol("protocol-a", frozen_at=T0)
    duplicate = _protocol("protocol-b", frozen_at="2026-01-01T00:05:00+00:00")
    changed_science = _protocol(
        "protocol-c",
        frozen_at="2026-01-01T00:10:00+00:00",
        feature_set_version="features-v2",
    )

    registry.append(first)
    registry.append(duplicate)
    index = ScientificRegistryIndex(registry)

    assert first.protocol_sha256 != duplicate.protocol_sha256
    fingerprint = index.protocol_fingerprint(first)
    assert index.protocol_fingerprint(duplicate) == fingerprint
    assert index.protocol_fingerprint(changed_science) != fingerprint
    assert [
        entry.record_id
        for entry in index.find_protocol_fingerprint(fingerprint, as_of=T1)
    ] == ["protocol-a", "protocol-b"]

    reopened = ScientificRegistryIndex(ScientificRegistry(path))
    assert [
        entry.record_id
        for entry in reopened.find_protocol_fingerprint(fingerprint, as_of=T1)
    ] == ["protocol-a", "protocol-b"]
