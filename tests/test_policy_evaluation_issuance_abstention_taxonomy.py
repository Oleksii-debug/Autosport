from __future__ import annotations

import hashlib
from decimal import Decimal, localcontext

import pytest

import autosport.external_validity_policy_issuance as issuance
from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
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


def _protocol(
    keys: tuple[str, ...],
    *,
    supported_baseline_kind: BaselineKind | None = None,
) -> FrozenBaselineProtocol:
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
            supported=kind is supported_baseline_kind,
            unsupported_reason=(
                None
                if kind is supported_baseline_kind
                else "not needed by issuance taxonomy regression"
            ),
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
    net_reward: str = "0",
    cost: str = "0",
    policy_loss: str | None = None,
) -> issuance._SourceEvaluation:
    canonical_net_reward = str(Decimal(net_reward))
    canonical_cost = str(Decimal(cost))
    canonical_policy_loss = (
        str(-Decimal(canonical_net_reward))
        if policy_loss is None
        else str(Decimal(policy_loss))
    )
    samples = [
        {
            "sample_id": sample_id,
            "regime_id": "table-tennis:pre-match",
            "challenger_action": action,
            "challenger_net_reward": canonical_net_reward,
            "challenger_cost": canonical_cost,
            "case_payload": {
                "sample_id": sample_id,
                "source_evidence_sha256": _sha("evidence:" + sample_id),
            },
        }
        for sample_id, action in actions
    ]
    abstention_actions = {"NO_BET", "WAIT", configured_abstain_action}
    abstention_count = sum(action in abstention_actions for _sample_id, action in actions)
    with localcontext() as context:
        context.prec = 50
        abstention_rate = Decimal(abstention_count) / Decimal(len(actions))
        action_rate = Decimal(len(actions) - abstention_count) / Decimal(len(actions))
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
            "challenger_metrics": {
                "policy_loss": canonical_policy_loss,
                "abstention_rate": str(abstention_rate),
                "action_rate": str(action_rate),
            },
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


def test_product_issuance_rejects_abstention_metric_disagreement():
    actions = (("case-a", "NO_BET"), ("case-b", "WAIT"))
    protocol = _protocol(("case-a", "case-b"))
    source = _source(actions)
    source.policy_evaluation["challenger_metrics"] = {
        "policy_loss": "0",
        "abstention_rate": "0",
        "action_rate": "1",
    }

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="abstention metrics do not reconcile",
    ):
        issuance._derive_policy_evaluation(
            protocol,
            _target(),
            source,
        )


def test_product_issuance_matches_canonical_nonterminating_rate_precision():
    actions = (
        ("case-a", "NO_BET"),
        ("case-b", "BET"),
        ("case-c", "BET"),
    )
    protocol = _protocol(("case-a", "case-b", "case-c"))

    issued, _projection = issuance._derive_policy_evaluation(
        protocol,
        _target(),
        _source(actions),
    )

    assert issued.observed_count == 3
    assert issued.abstention_count == 1
    assert issued.scored_count == 2



def test_no_action_baseline_excludes_hindsight_reward_and_preserves_cost():
    actions = (("case-a", "WAIT"), ("case-b", "NO_BET"))
    protocol = _protocol(
        ("case-a", "case-b"),
        supported_baseline_kind=BaselineKind.NO_BET_WAIT,
    )

    issued, projection = issuance._derive_policy_evaluation(
        protocol,
        issuance._target(protocol, BaselineKind.NO_BET_WAIT),
        _source(actions, net_reward="0.75", cost="0.25"),
    )

    assert issued.observed_count == 2
    assert issued.scored_count == 0
    assert issued.abstention_count == 2
    assert Decimal(issued.metric_value) == Decimal("0")
    assert Decimal(issued.uncertainty_low) == Decimal("0")
    assert Decimal(issued.uncertainty_high) == Decimal("0")
    assert Decimal(issued.total_cost) == Decimal("0.5")
    assert projection["projection_rule"] == (
        "no_action_action_utility=0;"
        "counterfactual_wait_reward_excluded;"
        "applicable_cost_preserved_separately"
    )


def test_no_action_baseline_rejects_material_action_even_when_source_metrics_reconcile():
    actions = (("case-a", "WAIT"), ("case-b", "BET"))
    protocol = _protocol(
        ("case-a", "case-b"),
        supported_baseline_kind=BaselineKind.NO_BET_WAIT,
    )

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="must abstain canonically on every frozen sample",
    ):
        issuance._derive_policy_evaluation(
            protocol,
            issuance._target(protocol, BaselineKind.NO_BET_WAIT),
            _source(actions, net_reward="0.5"),
        )


def test_no_action_baseline_rejects_custom_abstention_alias():
    actions = (("case-a", "SKIP"),)
    protocol = _protocol(
        ("case-a",),
        supported_baseline_kind=BaselineKind.NO_BET_WAIT,
    )

    # SKIP remains a valid custom abstention for generic policy metrics, but the
    # frozen NO_BET_WAIT control is narrower: only canonical WAIT/NO_BET semantics
    # can establish that no betting action occurred.
    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="must abstain canonically on every frozen sample",
    ):
        issuance._derive_policy_evaluation(
            protocol,
            issuance._target(protocol, BaselineKind.NO_BET_WAIT),
            _source(
                actions,
                configured_abstain_action="SKIP",
                net_reward="1.25",
            ),
        )


def test_no_action_baseline_rejects_policy_loss_mismatch_before_zeroing():
    actions = (("case-a", "WAIT"), ("case-b", "NO_BET"))
    protocol = _protocol(
        ("case-a", "case-b"),
        supported_baseline_kind=BaselineKind.NO_BET_WAIT,
    )

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="does not reconcile to canonical policy_loss",
    ):
        issuance._derive_policy_evaluation(
            protocol,
            issuance._target(protocol, BaselineKind.NO_BET_WAIT),
            _source(
                actions,
                net_reward="0.75",
                cost="0.25",
                policy_loss="0",
            ),
        )


def test_candidate_projection_keeps_reconciled_predictive_utility_semantics():
    actions = (("case-a", "WAIT"), ("case-b", "NO_BET"))
    protocol = _protocol(("case-a", "case-b"))

    issued, projection = issuance._derive_policy_evaluation(
        protocol,
        _target(),
        _source(actions, net_reward="0.75", cost="0.25"),
    )

    assert Decimal(issued.metric_value) == Decimal("0.75")
    assert Decimal(issued.total_cost) == Decimal("0.5")
    assert projection["projection_rule"] == "predictive_net_utility=-policy_loss"



def test_no_action_issuance_versions_corrected_projection_without_changing_generic_v3():
    protocol = _protocol(
        ("case-a",),
        supported_baseline_kind=BaselineKind.NO_BET_WAIT,
    )
    authority = issuance.ProductPolicyEvaluationWorkspace(
        workspace="unused-for-identity-only",
        workspace_instance_id="workspace-instance-v1",
        workspace_locator_sha256=_sha("workspace-locator"),
    )
    no_action_target = issuance._target(protocol, BaselineKind.NO_BET_WAIT)
    candidate_target = _target()

    assert issuance._NO_ACTION_ISSUER_SOURCE_SHA256 == hashlib.sha256(
        b"autosport.external-validity-policy-issuance.no-action.v1"
    ).hexdigest()
    assert issuance._issuer_source_sha256(no_action_target) == (
        issuance._NO_ACTION_ISSUER_SOURCE_SHA256
    )
    assert issuance._issuer_source_sha256(candidate_target) == issuance._ISSUER_SOURCE_SHA256

    legacy_no_action_v3 = issuance._digest(
        {
            "schema_version": 3,
            "kind": "autosport-external-validity-product-issuance-identity-v3",
            "workspace_instance_id": authority.workspace_instance_id,
            "workspace_locator_sha256": authority.workspace_locator_sha256,
            "protocol_sha256": protocol.identity_sha256,
            "evidence_scope_sha256": protocol.evidence_scope.identity_sha256,
            "cohort_sha256": protocol.evidence_scope.cohort_sha256,
            "policy_id": no_action_target.policy_id,
            "policy_artifact_sha256": no_action_target.policy_artifact_sha256,
            "baseline_definition_sha256": no_action_target.baseline_definition_sha256,
        }
    )
    assert issuance._issuance_id(authority, protocol, no_action_target) != legacy_no_action_v3

    generic_candidate_v3 = issuance._digest(
        {
            "schema_version": 3,
            "kind": "autosport-external-validity-product-issuance-identity-v3",
            "workspace_instance_id": authority.workspace_instance_id,
            "workspace_locator_sha256": authority.workspace_locator_sha256,
            "protocol_sha256": protocol.identity_sha256,
            "evidence_scope_sha256": protocol.evidence_scope.identity_sha256,
            "cohort_sha256": protocol.evidence_scope.cohort_sha256,
            "policy_id": candidate_target.policy_id,
            "policy_artifact_sha256": candidate_target.policy_artifact_sha256,
            "baseline_definition_sha256": candidate_target.baseline_definition_sha256,
        }
    )
    assert issuance._issuance_id(authority, protocol, candidate_target) == generic_candidate_v3
