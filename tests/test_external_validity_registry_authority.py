from __future__ import annotations

import pytest

from autosport.external_validity_baseline import PolicyEvaluation
from autosport.external_validity_registry import (
    ExternalValidityRegistryError,
    build_registered_external_validity_report,
)
from autosport.scientific_registry import ScientificRegistry

from test_external_validity_registry import (
    _evaluations,
    _protocol,
    _seed_happy_registry,
)


def test_wrapper_rejects_spoofed_registry_subclass_before_fake_get() -> None:
    calls: list[tuple[object, ...]] = []

    class SpoofedScientificRegistry(ScientificRegistry):
        def get(self, *args: object) -> object:
            calls.append(args)
            return object()

    spoofed = object.__new__(SpoofedScientificRegistry)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="exact ScientificRegistry authority",
    ):
        build_registered_external_validity_report(
            spoofed,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            (),
            candidate_evaluation_bundle_id="bundle-id:spoofed",
            baseline_evaluation_bundle_ids={},
        )

    assert calls == []


def test_exact_policy_evaluation_boundary_rejects_subclass(tmp_path) -> None:
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path,
        protocol,
        candidate,
        baselines,
    )

    class SpoofedPolicyEvaluation(PolicyEvaluation):
        pass

    spoofed = SpoofedPolicyEvaluation(
        policy_id=candidate.policy_id,
        policy_artifact_sha256=candidate.policy_artifact_sha256,
        protocol_sha256=candidate.protocol_sha256,
        evidence_scope_sha256=candidate.evidence_scope_sha256,
        cohort_sha256=candidate.cohort_sha256,
        primary_metric=candidate.primary_metric,
        evaluated_at=candidate.evaluated_at,
        metric_value=candidate.metric_value,
        uncertainty_low=candidate.uncertainty_low,
        uncertainty_high=candidate.uncertainty_high,
        observed_count=candidate.observed_count,
        scored_count=candidate.scored_count,
        abstention_count=candidate.abstention_count,
        total_cost=candidate.total_cost,
        evaluation_bundle_sha256=candidate.evaluation_bundle_sha256,
        baseline_definition_sha256=candidate.baseline_definition_sha256,
    )

    with pytest.raises(
        ExternalValidityRegistryError,
        match="candidate must be an exact PolicyEvaluation value",
    ):
        build_registered_external_validity_report(
            registry,
            protocol,
            spoofed,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids=baseline_ids,
        )


def test_exact_registry_instance_read_shadow_is_rejected_before_fake_read(
    tmp_path,
    monkeypatch,
) -> None:
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path,
        protocol,
        candidate,
        baselines,
    )
    calls: list[str] = []

    def forged_read():
        calls.append("forged-read")
        return {"schema_version": 1, "records": []}

    # ScientificRegistry has a mutable instance dictionary. Before this guard an
    # exact-type caller could shadow _read and ScientificRegistry.get(...) would
    # consume the forged state through normal self._read() dispatch.
    monkeypatch.setattr(registry, "_read", forged_read)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="instance read authority was rebound",
    ):
        build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids=baseline_ids,
        )

    assert calls == []


def test_runtime_registry_class_read_rebind_is_rejected_before_fake_read(
    tmp_path,
    monkeypatch,
) -> None:
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path,
        protocol,
        candidate,
        baselines,
    )
    calls: list[str] = []

    def forged_read(_self):
        calls.append("forged-class-read")
        return {"schema_version": 1, "records": []}

    monkeypatch.setattr(ScientificRegistry, "_read", forged_read)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="executable read authority was rebound",
    ):
        build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids=baseline_ids,
        )

    assert calls == []
