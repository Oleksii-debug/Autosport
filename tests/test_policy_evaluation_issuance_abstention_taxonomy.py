from __future__ import annotations

import hashlib

import pytest

import autosport.external_validity_policy_issuance as issuance
from autosport.external_validity_baseline import (
    BaselineDefinition,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    REQUIRED_BASELINE_KINDS,
    canonical_evaluation_contract,
)
from autosport.opportunity import StrategyClass


T0 = "2026-10-03T05:00:00+00:00"
T1 = "2026-10-03T05:01:00+00:00"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _protocol(keys: tuple[str, ...]) -> FrozenBaselineProtocol:
    scope = FrozenEvidenceScope(
        dataset_sha256=_sha("dataset"),
        dataset_cutoff=T0,
        cohort_keys=keys,
        market_evidence_sha256=_sha("market"),
        outcome_evidence_sha256=_sha("outcome"),
        cost_model_sha256=_sha("cost"),
        execution_model_sha256=_sha("execution"),
    )
    baselines = tuple(
        BaselineDefinition(
            kind=kind,
            baseline_id=f"baseline:{kind.value}",
            implementation_sha256=_sha("impl:" + kind.value),
            config_sha256=_sha("config:" + kind.value),
            supported=False,
            unsupported_reason="not needed by issuance taxonomy regression",
        )
        for kind in REQUIRED_BASELINE_KINDS
    )
    contract = canonical_evaluation_contract(
        EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    )
    return FrozenBaselineProtocol(
        protocol_id="policy-issuance-abstention-taxonomy-v1",
        frozen_at=T0,
        evidence_scope=scope,
        candidate_id="candidate",
        candidate_artifact_sha256=_sha("candidate"),
        strategy_class=StrategyClass.PREDICTIVE_EDGE,
        evaluation_contract_family=EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        evaluation_semantics=contract["evaluation_semantics"],
        evaluation_contract_sha256=contract["evaluation_contract_sha256"],
        primary_metric=contract["primary_metric"],
        uncertainty_method=contract["uncertainty_method"],
        baselines=baselines,
    )


def _source(
    actions: tuple[tuple[str, str], ...],
    *,
    configured_abstain_action: str = "WAIT",
) -> issuance._SourceEvaluation:
    samples = [
        {
            "sample_id": sample_id,
            "regime_id": "table-tennis:pre-match",
            "challenger_action": action,
            "challenger_net_reward": "0",
            "challenger_cost": "0",
            "case_payload": {
                "sample_id": sample_id,
                "source_evidence_sha256": _sha("evidence:" + sample_id),
            },
        }
        for sample_id, action in actions
    ]
    return issuance._SourceEvaluation(
        evaluation_bundle_id="bundle",
        evaluation_bundle_sha256=_sha("bundle"),
        evaluation_bundle_record_sha256=_sha("bundle-record"),
        dataset_snapshot_id="dataset",
        strategy_version_id="strategy",
        model_version_id="model",
        experiment_id="experiment",
        completed_at=T1,
        policy_artifact_sha256=_sha("candidate"),
        model_artifact_sha256=_sha("model"),
        metrics_artifact_sha256=_sha("metrics"),
        policy_evaluation_sha256=_sha("policy-evaluation"),
        policy_evaluation={
            "kind": "autosport-policy-paired-causal-evaluation-v1",
            "completed_at": T1,
            "samples": samples,
            "challenger_metrics": {"policy_loss": "0"},
        },
        evaluator_config={"abstain_action": configured_abstain_action},
    )


def _target() -> issuance._Target:
    return issuance._Target(
        policy_id="candidate",
        policy_artifact_sha256=_sha("candidate"),
        baseline_definition_sha256=None,
        baseline_kind=None,
    )


def test_product_issuance_preserves_wait_and_no_bet_abstention_taxonomy():
    actions = (("case-a", "NO_BET"), ("case-b", "WAIT"))
    protocol = _protocol(tuple(sample_id for sample_id, _ in actions))

    issued, projection = issuance._derive_policy_evaluation(
        protocol,
        _target(),
        _source(actions),
    )

    assert issued.observed_count == 2
    assert issued.abstention_count == 2
    assert issued.scored_count == 0
    assert projection["abstain_action"] == "WAIT"


def test_product_issuance_custom_abstention_extends_canonical_taxonomy():
    actions = (
        ("case-a", "NO_BET"),
        ("case-b", "SKIP"),
        ("case-c", "WAIT"),
    )
    protocol = _protocol(tuple(sample_id for sample_id, _ in actions))

    issued, _projection = issuance._derive_policy_evaluation(
        protocol,
        _target(),
        _source(actions, configured_abstain_action="SKIP"),
    )

    assert issued.observed_count == 3
    assert issued.abstention_count == 3
    assert issued.scored_count == 0


def test_product_issuance_rejects_material_bet_as_abstain_action():
    actions = (("case-a", "BET"),)
    protocol = _protocol(("case-a",))

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="canonical material PAPER action",
    ):
        issuance._derive_policy_evaluation(
            protocol,
            _target(),
            _source(actions, configured_abstain_action="BET"),
        )
