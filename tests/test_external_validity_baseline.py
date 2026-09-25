from __future__ import annotations

import hashlib
from decimal import localcontext

import pytest

from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    ExternalValidityError,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    PolicyEvaluation,
    REQUIRED_BASELINE_KINDS,
    build_external_validity_report,
    canonical_evaluation_contract,
)
from autosport.opportunity import StrategyClass


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
    strategy_class: StrategyClass = StrategyClass.PREDICTIVE_EDGE,
    evaluation_contract_family: EvaluationContractFamily = (
        EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    ),
) -> FrozenBaselineProtocol:
    contract = canonical_evaluation_contract(evaluation_contract_family)
    return FrozenBaselineProtocol(
        protocol_id="extval:test:v1",
        frozen_at="2026-01-02T00:00:00+00:00",
        evidence_scope=scope or _scope(),
        candidate_id="candidate:complex",
        candidate_artifact_sha256=SHA_F,
        strategy_class=strategy_class,
        evaluation_contract_family=evaluation_contract_family,
        evaluation_semantics=contract["evaluation_semantics"],
        evaluation_contract_sha256=contract["evaluation_contract_sha256"],
        primary_metric=contract["primary_metric"],
        uncertainty_method=contract["uncertainty_method"],
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
        evaluated_at="2026-01-03T00:00:00+00:00",
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


def test_mismatched_cutoff_fails_closed_even_with_same_corpus():
    protocol = _protocol()
    alternate = FrozenEvidenceScope(
        dataset_sha256=protocol.evidence_scope.dataset_sha256,
        dataset_cutoff="2026-01-01T01:00:00+00:00",
        cohort_keys=protocol.evidence_scope.cohort_keys,
        market_evidence_sha256=protocol.evidence_scope.market_evidence_sha256,
        outcome_evidence_sha256=protocol.evidence_scope.outcome_evidence_sha256,
        cost_model_sha256=protocol.evidence_scope.cost_model_sha256,
        execution_model_sha256=protocol.evidence_scope.execution_model_sha256,
    )
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
    family = EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    contract = canonical_evaluation_contract(family)
    with pytest.raises(ExternalValidityError, match="every required kind"):
        FrozenBaselineProtocol(
            protocol_id="extval:test:v1",
            frozen_at="2026-01-02T00:00:00+00:00",
            evidence_scope=_scope(),
            candidate_id="candidate:complex",
            candidate_artifact_sha256=SHA_F,
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            evaluation_contract_family=family,
            evaluation_semantics=contract["evaluation_semantics"],
            evaluation_contract_sha256=contract["evaluation_contract_sha256"],
            primary_metric=contract["primary_metric"],
            uncertainty_method=contract["uncertainty_method"],
            baselines=definitions[:-1],
        )


@pytest.mark.parametrize(
    ("strategy_class", "evaluation_contract_family"),
    (
        (
            StrategyClass.PREDICTIVE_EDGE,
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        ),
        (
            StrategyClass.LIVE_PRICE_MOVEMENT,
            EvaluationContractFamily.LIVE_PRICE_EXECUTION,
        ),
        (StrategyClass.ARBITRAGE, EvaluationContractFamily.ARBITRAGE_EXECUTION),
        (StrategyClass.DUTCHING, EvaluationContractFamily.DUTCHING_EXECUTION),
        (
            StrategyClass.HEDGE_REBALANCE,
            EvaluationContractFamily.HEDGE_PORTFOLIO_RISK,
        ),
        (StrategyClass.HYBRID, EvaluationContractFamily.HYBRID_COMPOSITE),
    ),
)
def test_protocol_binds_canonical_strategy_to_evaluation_contract_family(
    strategy_class: StrategyClass,
    evaluation_contract_family: EvaluationContractFamily,
):
    protocol = _protocol(
        strategy_class=strategy_class,
        evaluation_contract_family=evaluation_contract_family,
    )
    contract = canonical_evaluation_contract(evaluation_contract_family)

    payload = protocol.to_payload()
    assert payload["schema_version"] == 3
    assert payload["strategy_class"] == strategy_class.value
    assert payload["evaluation_contract_family"] == evaluation_contract_family.value
    assert payload["evaluation_contract_id"] == contract["contract_id"]
    assert payload["evaluation_contract_sha256"] == contract["evaluation_contract_sha256"]
    assert payload["primary_metric"] == contract["primary_metric"]
    assert payload["uncertainty_method"] == contract["uncertainty_method"]


@pytest.mark.parametrize(
    ("strategy_class", "wrong_family"),
    (
        (
            StrategyClass.PREDICTIVE_EDGE,
            EvaluationContractFamily.ARBITRAGE_EXECUTION,
        ),
        (
            StrategyClass.LIVE_PRICE_MOVEMENT,
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        ),
        (
            StrategyClass.ARBITRAGE,
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        ),
        (
            StrategyClass.DUTCHING,
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        ),
        (
            StrategyClass.HEDGE_REBALANCE,
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        ),
        (
            StrategyClass.HYBRID,
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        ),
    ),
)
def test_strategy_evaluation_contract_family_mismatch_fails_closed(
    strategy_class: StrategyClass,
    wrong_family: EvaluationContractFamily,
):
    with pytest.raises(ExternalValidityError, match="evaluation contract family"):
        _protocol(
            strategy_class=strategy_class,
            evaluation_contract_family=wrong_family,
        )


def test_arbitrage_family_rejects_predictive_contract_details():
    family = EvaluationContractFamily.ARBITRAGE_EXECUTION
    predictive = canonical_evaluation_contract(
        EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    )
    with pytest.raises(ExternalValidityError, match="canonical family authority"):
        FrozenBaselineProtocol(
            protocol_id="extval:arb-wrong-contract:v1",
            frozen_at="2026-01-02T00:00:00+00:00",
            evidence_scope=_scope(),
            candidate_id="candidate:arbitrage",
            candidate_artifact_sha256=SHA_F,
            strategy_class=StrategyClass.ARBITRAGE,
            evaluation_contract_family=family,
            evaluation_semantics=predictive["evaluation_semantics"],
            evaluation_contract_sha256=predictive["evaluation_contract_sha256"],
            primary_metric=predictive["primary_metric"],
            uncertainty_method=predictive["uncertainty_method"],
            baselines=_definitions(),
        )


def test_predictive_family_rejects_execution_only_contract_details():
    family = EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    execution = canonical_evaluation_contract(EvaluationContractFamily.ARBITRAGE_EXECUTION)
    with pytest.raises(ExternalValidityError, match="canonical family authority"):
        FrozenBaselineProtocol(
            protocol_id="extval:predictive-wrong-contract:v1",
            frozen_at="2026-01-02T00:00:00+00:00",
            evidence_scope=_scope(),
            candidate_id="candidate:predictive",
            candidate_artifact_sha256=SHA_F,
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            evaluation_contract_family=family,
            evaluation_semantics=execution["evaluation_semantics"],
            evaluation_contract_sha256=execution["evaluation_contract_sha256"],
            primary_metric=execution["primary_metric"],
            uncertainty_method=execution["uncertainty_method"],
            baselines=_definitions(),
        )


def test_correct_family_rejects_unknown_contract_digest():
    family = EvaluationContractFamily.ARBITRAGE_EXECUTION
    contract = canonical_evaluation_contract(family)
    with pytest.raises(ExternalValidityError, match="canonical family authority"):
        FrozenBaselineProtocol(
            protocol_id="extval:arb-forged-digest:v1",
            frozen_at="2026-01-02T00:00:00+00:00",
            evidence_scope=_scope(),
            candidate_id="candidate:arbitrage",
            candidate_artifact_sha256=SHA_F,
            strategy_class=StrategyClass.ARBITRAGE,
            evaluation_contract_family=family,
            evaluation_semantics=contract["evaluation_semantics"],
            evaluation_contract_sha256=_hash("forged-contract"),
            primary_metric=contract["primary_metric"],
            uncertainty_method=contract["uncertainty_method"],
            baselines=_definitions(),
        )


def test_evaluation_before_protocol_freeze_fails_closed():
    protocol = _protocol()
    candidate = PolicyEvaluation(
        policy_id=protocol.candidate_id,
        policy_artifact_sha256=protocol.candidate_artifact_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        cohort_sha256=protocol.evidence_scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        evaluated_at="2026-01-01T12:00:00+00:00",
        metric_value="0.05",
        uncertainty_low="0.01",
        uncertainty_high="0.10",
        observed_count=3,
        scored_count=2,
        abstention_count=1,
        total_cost="0",
        evaluation_bundle_sha256=SHA_A,
    )

    with pytest.raises(ExternalValidityError, match="predates frozen protocol"):
        build_external_validity_report(
            protocol,
            candidate,
            _supported_results(protocol),
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
            evaluated_at="2026-01-03T00:00:00+00:00",
            metric_value="0.05",
            uncertainty_low="0.01",
            uncertainty_high="0.10",
            observed_count=3,
            scored_count=1,
            abstention_count=1,
            total_cost="0",
            evaluation_bundle_sha256=SHA_A,
        )


def test_decimal_evidence_and_report_identity_ignore_ambient_decimal_context():
    def build_at_precision(precision: int):
        with localcontext() as context:
            context.prec = precision
            protocol = _protocol()
            candidate = _result(
                protocol,
                protocol.candidate_id,
                artifact_sha256=protocol.candidate_artifact_sha256,
                metric="1.2345678901234567890123456789012345",
                low="1.1000000000000000000000000000000000",
                high="1.3000000000000000000000000000000000",
                scored=2,
                abstained=1,
                total_cost="0.0100000000000000000000000000000001",
            )
            report = build_external_validity_report(
                protocol,
                candidate,
                _supported_results(protocol),
            )
            return (
                candidate.to_payload(),
                report.to_payload(),
                candidate.identity_sha256,
                report.identity_sha256,
            )

    low_precision = build_at_precision(9)
    high_precision = build_at_precision(60)

    assert low_precision == high_precision
    assert (
        low_precision[0]["metric_value"]
        == "1.2345678901234567890123456789012345"
    )
    assert low_precision[0]["uncertainty_low"] == "1.1"
    assert low_precision[0]["uncertainty_high"] == "1.3"
    assert (
        low_precision[0]["total_cost"]
        == "0.0100000000000000000000000000000001"
    )


@pytest.mark.parametrize(
    ("metric", "low", "high", "scored", "abstained", "total_cost"),
    (
        ("0.01", "0", "0.01", 0, 3, "0"),
        ("0", "-0.01", "0.01", 0, 3, "0"),
        ("0", "0", "0", 1, 2, "0"),
    ),
)
def test_no_bet_wait_baseline_rejects_nonzero_action_evidence(
    metric: str,
    low: str,
    high: str,
    scored: int,
    abstained: int,
    total_cost: str,
):
    protocol = _protocol()
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
    )
    definition = next(
        item for item in protocol.baselines if item.kind is BaselineKind.NO_BET_WAIT
    )
    results = list(_supported_results(protocol))
    index = next(
        i for i, item in enumerate(results) if item.policy_id == definition.baseline_id
    )
    results[index] = _result(
        protocol,
        definition.baseline_id,
        artifact_sha256=definition.implementation_sha256,
        baseline_definition_sha256=definition.definition_sha256,
        metric=metric,
        low=low,
        high=high,
        scored=scored,
        abstained=abstained,
        total_cost=total_cost,
    )

    with pytest.raises(
        ExternalValidityError,
        match="no-bet-wait baseline must represent deterministic zero action",
    ):
        build_external_validity_report(protocol, candidate, results)

