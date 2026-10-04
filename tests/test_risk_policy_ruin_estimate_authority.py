from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.risk_policy_ruin_estimate_authority as policy_module
from autosport.risk_evaluation_precommit_authority import (
    ProductFixedNRiskEvaluationPrecommitAuthority,
)
from autosport.risk_of_ruin_evaluator import (
    RiskPathObservation,
    clopper_pearson_upper_bound,
)
from autosport.risk_path_observation_set_authority import (
    ProductFixedNRiskObservationSet,
)
from autosport.risk_policy_ruin_estimate_authority import (
    ProductFixedNRiskPolicyEstimate,
    ProductFixedNRiskPolicyEstimateError,
    _derive_policy_estimate_material,
)


MEMBERS = ("run-001", "run-002", "run-003")


def _precommit() -> ProductFixedNRiskEvaluationPrecommitAuthority:
    value = object.__new__(ProductFixedNRiskEvaluationPrecommitAuthority)
    fields = {
        "workspace_instance_id": "workspace-1",
        "experiment_id": "experiment-1",
        "research_protocol_id": "protocol-1",
        "protocol_sha256": "1" * 64,
        "dataset_snapshot_id": "dataset-1",
        "dataset_manifest_sha256": "2" * 64,
        "membership_design_sha256": "3" * 64,
        "sampling_manifest_sha256": "4" * 64,
        "planned_member_ids": MEMBERS,
        "confidence_level": Decimal("0.95"),
        "ruin_threshold": Decimal("0"),
        "risk_target_scope": "FROZEN_STAKE_POLICY",
        "initial_capital_state_sha256": "5" * 64,
        "stake_policy_sha256": "6" * 64,
        "authority_sha256": "7" * 64,
    }
    for name, item in fields.items():
        object.__setattr__(value, name, item)
    return value


def _observations(
    *,
    experiment_id: str = "experiment-1",
    planned_member_ids: tuple[str, ...] = MEMBERS,
    research_protocol_sha256: str = "1" * 64,
    dataset_snapshot_id: str = "dataset-1",
    dataset_manifest_sha256: str = "2" * 64,
    sampling_manifest_sha256: str = "4" * 64,
    initial_capital_state_sha256: str = "5" * 64,
    stake_policy_sha256: str = "6" * 64,
) -> ProductFixedNRiskObservationSet:
    values = (
        RiskPathObservation(
            independent_unit_id="run-001",
            dependence_group_id="iid-stream:" + "a" * 64,
            minimum_equity=Decimal("10"),
            outcome_available_at="2026-10-01T10:00:00+00:00",
            source_evidence_sha256="a" * 64,
        ),
        RiskPathObservation(
            independent_unit_id="run-002",
            dependence_group_id="iid-stream:" + "b" * 64,
            minimum_equity=Decimal("0"),
            outcome_available_at="2026-10-01T11:00:00+00:00",
            source_evidence_sha256="b" * 64,
        ),
        RiskPathObservation(
            independent_unit_id="run-003",
            dependence_group_id="iid-stream:" + "c" * 64,
            minimum_equity=Decimal("-1"),
            outcome_available_at="2026-10-01T12:00:00+00:00",
            source_evidence_sha256="c" * 64,
        ),
    )
    result = object.__new__(ProductFixedNRiskObservationSet)
    for name, item in {
        "experiment_id": experiment_id,
        "research_protocol_sha256": research_protocol_sha256,
        "dataset_snapshot_id": dataset_snapshot_id,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "risk_method": "clopper-pearson-one-sided-fixed-n-iid-v1",
        "sampling_manifest_sha256": sampling_manifest_sha256,
        "initial_capital_state_sha256": initial_capital_state_sha256,
        "stake_policy_sha256": stake_policy_sha256,
        "planned_member_ids": planned_member_ids,
        "qualification_sha256": "8" * 64,
        "occurrence_root_sha256": "9" * 64,
        "observations": values,
        "observation_manifest_sha256": "d" * 64,
        "source_evidence_sha256": "e" * 64,
    }.items():
        object.__setattr__(result, name, item)
    return result


def test_derives_exact_policy_level_bound_from_product_authority_material() -> None:
    material = _derive_policy_estimate_material(_precommit(), _observations())

    assert material["experiment_id"] == "experiment-1"
    assert material["planned_member_ids"] == MEMBERS
    assert material["confidence_level"] == Decimal("0.95")
    assert material["ruin_threshold"] == Decimal("0")
    assert material["risk_target_scope"] == "FROZEN_STAKE_POLICY"
    assert material["independent_units"] == 3
    assert material["ruin_count"] == 2
    assert material["upper_bound"] == clopper_pearson_upper_bound(
        ruin_count=2,
        independent_units=3,
        confidence_level=Decimal("0.95"),
    )
    assert material["observed_through"] == "2026-10-01T12:00:00+00:00"
    assert material["observation_manifest_sha256"] == "d" * 64
    assert material["observation_source_evidence_sha256"] == "e" * 64
    assert len(material["evaluator_source_sha256"]) == 64
    assert len(material["estimate_sha256"]) == 64


def test_policy_estimate_is_deterministic() -> None:
    first = _derive_policy_estimate_material(_precommit(), _observations())
    second = _derive_policy_estimate_material(_precommit(), _observations())
    assert first == second


def test_experiment_rebinding_fails_closed() -> None:
    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="authority are inconsistent",
    ):
        _derive_policy_estimate_material(
            _precommit(),
            _observations(experiment_id="other-experiment"),
        )


def test_membership_rebinding_fails_closed() -> None:
    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="authority are inconsistent",
    ):
        _derive_policy_estimate_material(
            _precommit(),
            _observations(
                planned_member_ids=("run-001", "run-002", "other-run")
            ),
        )


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    (
        ("research_protocol_sha256", "f" * 64),
        ("dataset_snapshot_id", "other-dataset"),
        ("dataset_manifest_sha256", "f" * 64),
        ("sampling_manifest_sha256", "f" * 64),
        ("initial_capital_state_sha256", "f" * 64),
        ("stake_policy_sha256", "f" * 64),
    ),
)
def test_precommit_observation_root_rebinding_fails_closed(
    field_name: str,
    tampered_value: str,
) -> None:
    observations = _observations()
    object.__setattr__(observations, field_name, tampered_value)

    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="authority are inconsistent",
    ):
        _derive_policy_estimate_material(_precommit(), observations)


def test_duplicate_observation_member_fails_closed() -> None:
    observations = _observations()
    first, second, _third = observations.observations
    duplicate = RiskPathObservation(
        independent_unit_id=second.independent_unit_id,
        dependence_group_id="iid-stream:" + "f" * 64,
        minimum_equity=Decimal("-10"),
        outcome_available_at="2026-10-01T13:00:00+00:00",
        source_evidence_sha256="f" * 64,
    )
    object.__setattr__(observations, "observations", (first, second, duplicate))

    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="duplicate member identity",
    ):
        _derive_policy_estimate_material(_precommit(), observations)


def test_policy_estimate_cannot_be_caller_constructed_or_subclassed() -> None:
    with pytest.raises(TypeError, match="product-resolved"):
        ProductFixedNRiskPolicyEstimate(
            workspace_instance_id="forged",
            experiment_id="forged",
            research_protocol_id="forged",
            protocol_sha256="1" * 64,
            dataset_snapshot_id="forged",
            dataset_manifest_sha256="2" * 64,
            membership_design_sha256="3" * 64,
            sampling_manifest_sha256="4" * 64,
            planned_member_ids=(),
            observation_manifest_sha256="5" * 64,
            observation_source_evidence_sha256="6" * 64,
            qualification_sha256="7" * 64,
            occurrence_root_sha256="8" * 64,
            confidence_level=Decimal("0.95"),
            ruin_threshold=Decimal("0"),
            risk_target_scope="FROZEN_STAKE_POLICY",
            initial_capital_state_sha256="9" * 64,
            stake_policy_sha256="a" * 64,
            observed_through="2026-10-01T00:00:00+00:00",
            evaluator_source_sha256="b" * 64,
            independent_units=1,
            ruin_count=0,
            upper_bound=Decimal("0.95"),
            estimate_sha256="c" * 64,
        )

    with pytest.raises(TypeError, match="must not be subclassed"):
        class ForgedEstimate(ProductFixedNRiskPolicyEstimate):
            pass



@pytest.mark.parametrize(
    "helper_name",
    ("_sha", "_decimal_text", "_instant", "_canonical_json"),
)
def test_policy_estimate_derivation_rejects_internal_helper_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    monkeypatch.setattr(policy_module, helper_name, lambda *args, **kwargs: None)

    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="dispatch changed",
    ):
        _derive_policy_estimate_material(_precommit(), _observations())


def test_policy_estimate_dispatch_rejects_derivation_helper_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        policy_module,
        "_derive_policy_estimate_material",
        lambda *args, **kwargs: {},
    )

    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="dispatch changed",
    ):
        policy_module._require_dispatch()



def test_policy_estimate_dispatch_rejects_field_manifest_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "_ESTIMATE_FIELDS", ())

    with pytest.raises(
        ProductFixedNRiskPolicyEstimateError,
        match="dispatch changed",
    ):
        policy_module._require_dispatch()
