from __future__ import annotations

import hashlib

import pytest

from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    ExternalValidityError,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    PolicyEvaluation,
    REQUIRED_BASELINE_KINDS,
    build_external_validity_report,
)


T0 = "2026-01-01T00:00:00+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope(
    *,
    dataset_sha256: str = SHA_A,
    keys: tuple[str, ...] = ("a", "b", "c"),
) -> FrozenEvidenceScope:
    return FrozenEvidenceScope(
        dataset_sha256=dataset_sha256,
        dataset_cutoff=T0,
        cohort_keys=keys,
        market_evidence_sha256=SHA_B,
        outcome_evidence_sha256=SHA_C,
        cost_model_sha256=SHA_D,
        execution_model_sha256=SHA_E,
    )


def _definitions(
    *,
    mutate_market_config: bool = False,
) -> tuple[BaselineDefinition, ...]:
    supported = {
        BaselineKind.NO_BET_WAIT,
        BaselineKind.MARKET_IMPLIED_DEVIG,
        BaselineKind.PARTICIPANT_STRENGTH,
    }
    items = []
    for kind in REQUIRED_BASELINE_KINDS:
        is_supported = kind in supported
        config_seed = kind.value + (
            "-mutated"
            if mutate_market_config and kind is BaselineKind.MARKET_IMPLIED_DEVIG
            else ""
        )
        items.append(
            BaselineDefinition(
                kind=kind,
                baseline_id=f"baseline:{kind.value}",
                implementation_sha256=_hash("impl:" + kind.value),
                config_sha256=_hash("config:" + config_seed),
                supported=is_supported,
                unsupported_reason=(
                    None if is_supported else f"no causal support for {kind.value}"
                ),
            )
        )
    return tuple(items)


def _protocol(
    *,
    scope: FrozenEvidenceScope | None = None,
    mutate_market_config: bool = False,
) -> FrozenBaselineProtocol:
    return FrozenBaselineProtocol(
        protocol_id="extval:test:v1",
        frozen_at="2026-01-02T00:00:00+00:00",
        evidence_scope=scope or _scope(),
        candidate_id="candidate:complex",
        candidate_artifact_sha256=SHA_F,
        evaluation_semantics="paper-net-utility",
        primary_metric="net_utility",
        uncertainty_method="frozen-bootstrap-v1",
        baselines=_definitions(mutate_market_config=mutate_market_config),
    )


def _result(
    protocol: FrozenBaselineProtocol,
    policy_id: str,
    *,
    artifact_sha256: str,
    baseline_definition_sha256: str | None = None,
    metric: str = "0.05",
    low: str = "0.02",
    high: str = "0.08",
    scored: int = 2,
    abstained: int = 1,
    total_cost: str = "0.01",
    scope: FrozenEvidenceScope | None = None,
) -> PolicyEvaluation:
    actual_scope = scope or protocol.evidence_scope
    return PolicyEvaluation(
        policy_id=policy_id,
        policy_artifact_sha256=artifact_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=actual_scope.identity_sha256,
        cohort_sha256=actual_scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        metric_value=metric,
        uncertainty_low=low,
        uncertainty_high=high,
        observed_count=actual_scope.sample_count,
        scored_count=scored,
        abstention_count=abstained,
        total_cost=total_cost,
        evaluation_bundle_sha256=_hash("bundle:" + policy_id),
        baseline_definition_sha256=baseline_definition_sha256,
    )


def _supported_results(
    protocol: FrozenBaselineProtocol,
) -> tuple[PolicyEvaluation, ...]:
    values = {
        BaselineKind.NO_BET_WAIT: ("0", "0", "0", 0, 3, "0"),
        BaselineKind.MARKET_IMPLIED_DEVIG: (
            "0.03",
            "0.01",
            "0.05",
            3,
            0,
            "0.001",
        ),
        BaselineKind.PARTICIPANT_STRENGTH: (
            "0.04",
            "0.02",
            "0.06",
            2,
            1,
            "0.002",
        ),
    }
    results = []
    for definition in protocol.baselines:
        if not definition.supported:
            continue
        metric, low, high, scored, abstained, cost = values[definition.kind]
        results.append(
            _result(
                protocol,
                definition.baseline_id,
                artifact_sha256=definition.implementation_sha256,
                baseline_definition_sha256=definition.definition_sha256,
                metric=metric,
                low=low,
                high=high,
                scored=scored,
                abstained=abstained,
                total_cost=cost,
            )
        )
    return tuple(results)


def test_report_is_deterministic_and_keeps_unsupported_baselines_explicit_without_ranking():
    protocol = _protocol()
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
        metric="0.08",
        low="0.04",
        high="0.12",
        scored=2,
        abstained=1,
        total_cost="0.01",
    )

    report = build_external_validity_report(
        protocol,
        candidate,
        _supported_results(protocol),
    )
    payload = report.to_payload()

    assert len(report.comparisons) == 7
    no_bet = report.comparisons[0]
    assert no_bet.kind is BaselineKind.NO_BET_WAIT
    assert no_bet.candidate_minus_baseline == "0.08"
    assert no_bet.difference_lower_bound == "0.04"
    assert no_bet.difference_upper_bound == "0.12"
    assert no_bet.abstention_delta == -2
    assert no_bet.total_cost_delta == "0.01"
    unsupported = [item for item in report.comparisons if not item.supported]
    assert len(unsupported) == 4
    assert all(item.unsupported_reason for item in unsupported)
    assert payload["truth"]["winner_selected"] is False
    assert payload["truth"]["ranking_produced"] is False
    assert "winner" not in payload
    assert report.identity_sha256 == build_external_validity_report(
        protocol,
        candidate,
        _supported_results(protocol),
    ).identity_sha256


def test_mismatched_dataset_scope_fails_closed():
    protocol = _protocol()
    alternate = _scope(dataset_sha256=_hash("other-dataset"))
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
        scope=alternate,
    )

    with pytest.raises(ExternalValidityError, match="evidence scope"):
        build_external_validity_report(
            protocol,
            candidate,
            _supported_results(protocol),
        )


def test_cherry_picked_subset_fails_closed_even_with_same_dataset():
    protocol = _protocol()
    subset = _scope(keys=("a", "b"))
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
        scope=subset,
        scored=1,
        abstained=1,
    )

    with pytest.raises(ExternalValidityError, match="evidence scope"):
        build_external_validity_report(
            protocol,
            candidate,
            _supported_results(protocol),
        )


def test_baseline_config_mutation_after_freeze_is_rejected():
    original = _protocol()
    mutated = _protocol(mutate_market_config=True)
    candidate = _result(
        mutated,
        mutated.candidate_id,
        artifact_sha256=mutated.candidate_artifact_sha256,
    )
    original_market = next(
        item
        for item in original.baselines
        if item.kind is BaselineKind.MARKET_IMPLIED_DEVIG
    )
    mutated_market = next(
        item
        for item in mutated.baselines
        if item.kind is BaselineKind.MARKET_IMPLIED_DEVIG
    )
    results = list(_supported_results(mutated))
    index = next(
        i
        for i, item in enumerate(results)
        if item.policy_id == mutated_market.baseline_id
    )
    results[index] = _result(
        mutated,
        mutated_market.baseline_id,
        artifact_sha256=mutated_market.implementation_sha256,
        baseline_definition_sha256=original_market.definition_sha256,
        metric="0.03",
        low="0.01",
        high="0.05",
        scored=3,
        abstained=0,
        total_cost="0.001",
    )

    with pytest.raises(
        ExternalValidityError,
        match="definition changed after freeze",
    ):
        build_external_validity_report(mutated, candidate, results)


def test_missing_supported_baseline_result_fails_closed():
    protocol = _protocol()
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
    )
    results = [
        item
        for item in _supported_results(protocol)
        if "participant-strength" not in item.policy_id
    ]

    with pytest.raises(
        ExternalValidityError,
        match="supported baseline result is missing",
    ):
        build_external_validity_report(protocol, candidate, results)


def test_unsupported_baseline_cannot_receive_fabricated_result():
    protocol = _protocol()
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
    )
    unsupported = next(item for item in protocol.baselines if not item.supported)
    fabricated = _result(
        protocol,
        unsupported.baseline_id,
        artifact_sha256=unsupported.implementation_sha256,
        baseline_definition_sha256=unsupported.definition_sha256,
    )

    with pytest.raises(ExternalValidityError, match="unsupported baseline"):
        build_external_validity_report(
            protocol,
            candidate,
            (*_supported_results(protocol), fabricated),
        )


def test_protocol_requires_all_baseline_kinds_in_canonical_order():
    definitions = _definitions()
    with pytest.raises(ExternalValidityError, match="every required kind"):
        FrozenBaselineProtocol(
            protocol_id="extval:test:v1",
            frozen_at="2026-01-02T00:00:00+00:00",
            evidence_scope=_scope(),
            candidate_id="candidate:complex",
            candidate_artifact_sha256=SHA_F,
            evaluation_semantics="paper-net-utility",
            primary_metric="net_utility",
            uncertainty_method="frozen-bootstrap-v1",
            baselines=definitions[:-1],
        )


def test_policy_evaluation_requires_complete_observed_funnel_accounting():
    protocol = _protocol()
    with pytest.raises(
        ExternalValidityError,
        match=r"scored_count \+ abstention_count",
    ):
        PolicyEvaluation(
            policy_id=protocol.candidate_id,
            policy_artifact_sha256=protocol.candidate_artifact_sha256,
            protocol_sha256=protocol.identity_sha256,
            evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
            cohort_sha256=protocol.evidence_scope.cohort_sha256,
            primary_metric=protocol.primary_metric,
            metric_value="0.05",
            uncertainty_low="0.01",
            uncertainty_high="0.10",
            observed_count=3,
            scored_count=1,
            abstention_count=1,
            total_cost="0",
            evaluation_bundle_sha256=SHA_A,
        )
