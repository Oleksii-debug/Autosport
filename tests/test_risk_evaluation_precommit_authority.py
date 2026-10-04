from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.risk_evaluation_precommit_authority import (
    ProductFixedNRiskEvaluationPrecommitAuthority,
    ProductRiskEvaluationPrecommitError,
    resolve_product_fixed_n_risk_evaluation_precommit,
    verify_product_fixed_n_risk_evaluation_precommit,
)
from autosport.risk_membership_publication import (
    publish_fixed_n_membership_structure,
)
from autosport.risk_randomization_precommit import (
    issue_risk_randomization_precommit,
)
from autosport.risk_sampling_membership import (
    inspect_fixed_n_risk_membership_structure,
)
from autosport.run_registry import RunRegistry
from autosport.scientific_registry import (
    DatasetSnapshot,
    ResearchProtocol,
    ScientificRegistry,
)
from autosport.strategy_experiment import ScientificProtocolBinding


PROTOCOL_ID = "risk-fixed-n-protocol-v2"
DATASET_ID = "risk-fixed-n-dataset-v2"
DATASET_MANIFEST_SHA = "3" * 64
FRAME_SHA = "4" * 64
CAPITAL_SHA = "5" * 64
STAKE_SHA = "6" * 64
HORIZON_SHA = "7" * 64
MEMBERS = ("run-001", "run-002", "run-003")
CUTOFF = "2026-09-01T00:00:00+00:00"
DATASET_AVAILABLE = "2026-09-02T00:00:00+00:00"
FROZEN_AT = "2026-09-02T12:00:00+00:00"
PROTOCOL_AVAILABLE = "2026-09-02T12:05:00+00:00"
REVEAL_AFTER = "2026-09-10T00:00:00+00:00"


def _design(
    *,
    confidence_level: str = "0.95",
    ruin_threshold: str = "0",
    initial_capital_state_sha256: str = CAPITAL_SHA,
    stake_policy_sha256: str = STAKE_SHA,
) -> str:
    return json.dumps(
        {
            "kind": "autosport-risk-fixed-n-run-membership-v2",
            "dataset_snapshot_id": DATASET_ID,
            "planned_run_ids": list(MEMBERS),
            "planned_n": len(MEMBERS),
            "sampling_frame_sha256": FRAME_SHA,
            "risk_method": "CLOPPER_PEARSON_ONE_SIDED",
            "dependence_qualification": "SEPARATE_REQUIRED",
            "confidence_level": confidence_level,
            "ruin_threshold": ruin_threshold,
            "risk_target_scope": "FROZEN_STAKE_POLICY",
            "initial_capital_state_sha256": initial_capital_state_sha256,
            "stake_policy_sha256": stake_policy_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _binding(design: str) -> ScientificProtocolBinding:
    return ScientificProtocolBinding(
        research_protocol_id=PROTOCOL_ID,
        research_question_id="risk-question",
        research_question_sha256="c" * 64,
        hypothesis_id="risk-hypothesis",
        hypothesis_sha256="d" * 64,
        inclusion_criteria="exact frozen run cohort",
        exclusion_criteria="no post-freeze cohort edits",
        lawful_source_requirements="canonical product evidence only",
        causal_cutoff=CUTOFF,
        evaluation_design=design,
        feature_set_version="risk-path-v2",
        uncertainty_method="one-sided exact Clopper-Pearson",
        multiple_comparison_control="separate familywise authority required",
        robustness_checks=("restart re-resolution",),
        random_seed_policy="product randomization precommit",
        stopping_rule="fixed N; no early stopping",
        promotion_rule="risk evidence only; no direct promotion",
        expected_artifacts=("risk-path observations",),
        code_config_sha256="e" * 64,
        frozen_at_utc=FROZEN_AT,
    )


def _state(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    RunRegistry.initialize_pristine(workspace / "run_registry.json")
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific_registry.json"
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=DATASET_ID,
            manifest_sha256=DATASET_MANIFEST_SHA,
            source_identity="canonical-risk-run-cohort",
            license_identity="internal-product-evidence",
            causal_cutoff=CUTOFF,
            available_at_utc=DATASET_AVAILABLE,
            outcome_reveal_after=REVEAL_AFTER,
        )
    )
    registry.append(
        ResearchProtocol(
            binding=_binding(_design()),
            source_sha256="f" * 64,
            environment_sha256="1" * 64,
            dataset_manifest_sha256=DATASET_MANIFEST_SHA,
            available_at_utc=PROTOCOL_AVAILABLE,
        )
    )
    membership = inspect_fixed_n_risk_membership_structure(
        registry.path,
        research_protocol_id=PROTOCOL_ID,
        dataset_snapshot_id=DATASET_ID,
    )
    authority_root = tmp_path / "authority"
    published = publish_fixed_n_membership_structure(
        registry.path,
        workspace=workspace,
        research_protocol_id=PROTOCOL_ID,
        dataset_snapshot_id=DATASET_ID,
        authority_root=authority_root,
    )
    issued = issue_risk_randomization_precommit(
        registry.path,
        workspace=workspace,
        research_protocol_id=PROTOCOL_ID,
        dataset_snapshot_id=DATASET_ID,
        experiment_id="iid-risk-exp-v2",
        authority_root=authority_root,
    )
    manifest = json.dumps(
        {
            "kind": "autosport-risk-iid-resample-with-replacement-v1",
            "experiment_id": "iid-risk-exp-v2",
            "membership_design_sha256": membership.design_sha256,
            "membership_protocol_record_sha256": membership.protocol_record_sha256,
            "membership_dataset_record_sha256": membership.dataset_record_sha256,
            "membership_causal_cutoff": membership.causal_cutoff,
            "membership_precommitted_at": membership.precommitted_at,
            "membership_outcome_reveal_after": membership.outcome_reveal_after,
            "research_protocol_id": membership.research_protocol_id,
            "protocol_sha256": membership.protocol_sha256,
            "dataset_snapshot_id": membership.dataset_snapshot_id,
            "dataset_manifest_sha256": membership.dataset_manifest_sha256,
            "sampling_frame_sha256": membership.sampling_frame_sha256,
            "initial_capital_state_sha256": CAPITAL_SHA,
            "stake_policy_sha256": STAKE_SHA,
            "horizon_sha256": HORIZON_SHA,
            "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
            "with_replacement": True,
            "rng_algorithm": "PCG64",
            "rng_version": "numpy-compatible-contract-v1",
            "randomization_root_sha256": issued.randomization_root_sha256,
            "planned_n": len(MEMBERS),
            "planned_member_ids": list(MEMBERS),
            "stopping_rule": "FIXED_N_NO_EARLY_STOP",
            "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
            "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        membership,
        workspace,
        registry.path,
        authority_root,
        published,
        issued,
        manifest,
    )


def test_product_authority_binds_v2_statistics_to_preoutcome_product_roots(
    tmp_path,
) -> None:
    membership, workspace, registry, authority_root, _published, _issued, manifest = (
        _state(tmp_path)
    )

    first = resolve_product_fixed_n_risk_evaluation_precommit(
        membership,
        registry_path=registry,
        workspace=workspace,
        sampling_manifest_json=manifest,
        authority_root=authority_root,
    )
    second = verify_product_fixed_n_risk_evaluation_precommit(
        first,
        membership,
        registry_path=registry,
        workspace=workspace,
        sampling_manifest_json=manifest,
        authority_root=authority_root,
    )

    assert type(first) is ProductFixedNRiskEvaluationPrecommitAuthority
    assert second == first
    assert first.confidence_level == Decimal("0.95")
    assert first.ruin_threshold == Decimal("0")
    assert first.risk_target_scope == "FROZEN_STAKE_POLICY"
    assert first.initial_capital_state_sha256 == CAPITAL_SHA
    assert first.stake_policy_sha256 == STAKE_SHA
    assert first.product_preoutcome_chronology_proven is True
    assert first.statistical_policy_precommitted is True
    assert first.target_execution_proven is False
    assert first.iid_qualified is False
    assert first.risk_upper_bound_issued is False
    assert first.grants_real_money_authority is False
    assert len(first.authority_sha256) == 64


def test_product_authority_cannot_be_caller_constructed() -> None:
    with pytest.raises(TypeError, match="product-issued"):
        ProductFixedNRiskEvaluationPrecommitAuthority(
            workspace_instance_id="forged",
            experiment_id="forged",
            research_protocol_id="forged",
            protocol_sha256="1" * 64,
            dataset_snapshot_id="forged",
            dataset_manifest_sha256="2" * 64,
            membership_design_sha256="3" * 64,
            sampling_manifest_sha256="4" * 64,
            planned_member_ids=(),
            confidence_level=Decimal("0.95"),
            ruin_threshold=Decimal("0"),
            risk_target_scope="FROZEN_STAKE_POLICY",
            initial_capital_state_sha256="5" * 64,
            stake_policy_sha256="6" * 64,
            authority_sha256="7" * 64,
        )


def test_manifest_policy_rebinding_fails_closed(tmp_path) -> None:
    membership, workspace, registry, authority_root, _published, _issued, manifest = (
        _state(tmp_path)
    )
    payload = json.loads(manifest)
    payload["stake_policy_sha256"] = "a" * 64
    rebound = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(
        ProductRiskEvaluationPrecommitError,
        match="preregistration differs from product IID precommit",
    ):
        resolve_product_fixed_n_risk_evaluation_precommit(
            membership,
            registry_path=registry,
            workspace=workspace,
            sampling_manifest_json=rebound,
            authority_root=authority_root,
        )


def test_manifest_capital_rebinding_fails_closed(tmp_path) -> None:
    membership, workspace, registry, authority_root, _published, _issued, manifest = (
        _state(tmp_path)
    )
    payload = json.loads(manifest)
    payload["initial_capital_state_sha256"] = "a" * 64
    rebound = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(
        ProductRiskEvaluationPrecommitError,
        match="preregistration differs from product IID precommit",
    ):
        resolve_product_fixed_n_risk_evaluation_precommit(
            membership,
            registry_path=registry,
            workspace=workspace,
            sampling_manifest_json=rebound,
            authority_root=authority_root,
        )
