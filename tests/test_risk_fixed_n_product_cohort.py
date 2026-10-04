from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.risk_fixed_n_product_cohort import (
    ProductFixedNIidCohort,
    ProductFixedNIidCohortError,
    _compose_product_fixed_n_iid_cohort,
)
from autosport.risk_path_observation_authority import ProductRunCapitalPathEvidence
from autosport.risk_sampling_dependence import (
    ResolvedFixedNIidPrecommitAuthority,
    ResolvedFixedNIidSamplingStructure,
)


def _structure() -> ResolvedFixedNIidSamplingStructure:
    members = ("run-a", "run-b")
    streams = ("a" * 64, "b" * 64)
    return ResolvedFixedNIidSamplingStructure(
        experiment_id="experiment-1",
        membership_design_sha256="1" * 64,
        membership_protocol_record_sha256="2" * 64,
        membership_dataset_record_sha256="3" * 64,
        membership_causal_cutoff="2026-01-01T00:00:00+00:00",
        membership_precommitted_at="2025-12-31T23:00:00+00:00",
        membership_outcome_reveal_after="2026-01-01T01:00:00+00:00",
        research_protocol_id="protocol-1",
        protocol_sha256="4" * 64,
        dataset_snapshot_id="dataset-1",
        dataset_manifest_sha256="5" * 64,
        sampling_frame_sha256="6" * 64,
        initial_capital_state_sha256="7" * 64,
        stake_policy_sha256="8" * 64,
        horizon_sha256="9" * 64,
        rng_algorithm="AUTOSPORT_SHA256_REJECTION_V1",
        rng_version="1",
        randomization_root_sha256="c" * 64,
        planned_member_ids=members,
        member_stream_sha256=streams,
        manifest_sha256="d" * 64,
    )


def _precommit() -> ResolvedFixedNIidPrecommitAuthority:
    return ResolvedFixedNIidPrecommitAuthority(
        workspace_instance_id="workspace-1",
        experiment_id="experiment-1",
        membership_sha256="e" * 64,
        membership_receipt_sha256="f" * 64,
        randomization_precommit_receipt_sha256="0" * 64,
        randomization_root_sha256="c" * 64,
        membership_design_sha256="1" * 64,
        sampling_manifest_sha256="d" * 64,
        planned_member_ids=("run-a", "run-b"),
    )


def _evidence(
    *,
    member_id: str,
    member_index: int,
    stream: str,
    source: str,
    execution: str,
    minimum: str,
    available_at: str,
) -> ProductRunCapitalPathEvidence:
    result = object.__new__(ProductRunCapitalPathEvidence)
    values = {
        "member_id": member_id,
        "member_index": member_index,
        "expected_stream_sha256": stream,
        "source_evidence_sha256": source,
        "run_execution_receipt_sha256": execution,
        "minimum_equity": Decimal(minimum),
        "outcome_available_at": available_at,
        "complete": True,
    }
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


def _cohort_inputs() -> tuple[ProductRunCapitalPathEvidence, ...]:
    return (
        _evidence(
            member_id="run-a",
            member_index=0,
            stream="a" * 64,
            source="1" * 64,
            execution="3" * 64,
            minimum="91.25",
            available_at="2026-01-02T10:00:00+00:00",
        ),
        _evidence(
            member_id="run-b",
            member_index=1,
            stream="b" * 64,
            source="2" * 64,
            execution="4" * 64,
            minimum="87.50",
            available_at="2026-01-02T11:00:00+00:00",
        ),
    )


def test_complete_product_cohort_promotes_only_simulator_scoped_iid_truth() -> None:
    result = _compose_product_fixed_n_iid_cohort(
        _structure(),
        _precommit(),
        _cohort_inputs(),
    )

    assert type(result) is ProductFixedNIidCohort
    assert result.planned_member_ids == ("run-a", "run-b")
    assert result.member_stream_sha256 == ("a" * 64, "b" * 64)
    assert result.run_source_evidence_sha256 == ("1" * 64, "2" * 64)
    assert result.run_execution_receipt_sha256 == ("3" * 64, "4" * 64)
    assert result.planned_n == 2
    assert result.product_precommit_bound is True
    assert result.execution_consumption_proven is True
    assert result.occurrence_ancestry_proven is True
    assert result.run_path_ancestry_proven is True
    assert result.fixed_n_complete is True
    assert result.iid_qualified is True
    assert result.risk_scope == "SIMULATOR_DISTRIBUTION_ONLY"
    assert result.grants_real_money_authority is False
    assert result.outcomes_available_at == "2026-01-02T11:00:00+00:00"
    assert tuple(item.independent_unit_id for item in result.observations) == (
        "run-a",
        "run-b",
    )
    assert tuple(item.dependence_group_id for item in result.observations) == (
        "a" * 64,
        "b" * 64,
    )
    assert tuple(item.minimum_equity for item in result.observations) == (
        Decimal("91.25"),
        Decimal("87.50"),
    )
    assert len(result.cohort_sha256) == 64


def test_product_cohort_digest_is_deterministic() -> None:
    first = _compose_product_fixed_n_iid_cohort(
        _structure(),
        _precommit(),
        _cohort_inputs(),
    )
    second = _compose_product_fixed_n_iid_cohort(
        _structure(),
        _precommit(),
        _cohort_inputs(),
    )
    assert first == second
    assert first.cohort_sha256 == second.cohort_sha256


def test_missing_fixed_n_member_fails_closed() -> None:
    with pytest.raises(
        ProductFixedNIidCohortError,
        match="every precommitted member",
    ):
        _compose_product_fixed_n_iid_cohort(
            _structure(),
            _precommit(),
            _cohort_inputs()[:1],
        )


def test_member_reordering_fails_closed() -> None:
    first, second = _cohort_inputs()
    with pytest.raises(
        ProductFixedNIidCohortError,
        match="exact precommitted IID member",
    ):
        _compose_product_fixed_n_iid_cohort(
            _structure(),
            _precommit(),
            (second, first),
        )


def test_duplicate_run_source_evidence_fails_closed() -> None:
    first, second = _cohort_inputs()
    duplicate = _evidence(
        member_id=second.member_id,
        member_index=second.member_index,
        stream=second.expected_stream_sha256,
        source=first.source_evidence_sha256,
        execution=second.run_execution_receipt_sha256,
        minimum="87.50",
        available_at=second.outcome_available_at,
    )
    with pytest.raises(
        ProductFixedNIidCohortError,
        match="unique product run evidence",
    ):
        _compose_product_fixed_n_iid_cohort(
            _structure(),
            _precommit(),
            (first, duplicate),
        )


def test_duplicate_execution_receipt_fails_closed() -> None:
    first, second = _cohort_inputs()
    duplicate = _evidence(
        member_id=second.member_id,
        member_index=second.member_index,
        stream=second.expected_stream_sha256,
        source=second.source_evidence_sha256,
        execution=first.run_execution_receipt_sha256,
        minimum="87.50",
        available_at=second.outcome_available_at,
    )
    with pytest.raises(
        ProductFixedNIidCohortError,
        match="unique replay execution receipt",
    ):
        _compose_product_fixed_n_iid_cohort(
            _structure(),
            _precommit(),
            (first, duplicate),
        )


def test_duplicate_member_stream_fails_closed() -> None:
    structure = _structure()
    object.__setattr__(
        structure,
        "member_stream_sha256",
        ("a" * 64, "a" * 64),
    )
    with pytest.raises(
        ProductFixedNIidCohortError,
        match="distinct product randomization streams",
    ):
        _compose_product_fixed_n_iid_cohort(
            structure,
            _precommit(),
            _cohort_inputs(),
        )


def test_non_exact_member_index_fails_closed() -> None:
    first, second = _cohort_inputs()
    forged = _evidence(
        member_id=first.member_id,
        member_index=True,
        stream=first.expected_stream_sha256,
        source=first.source_evidence_sha256,
        execution=first.run_execution_receipt_sha256,
        minimum="91.25",
        available_at=first.outcome_available_at,
    )
    with pytest.raises(
        ProductFixedNIidCohortError,
        match="exact precommitted IID member",
    ):
        _compose_product_fixed_n_iid_cohort(
            _structure(),
            _precommit(),
            (forged, second),
        )


def test_product_cohort_cannot_be_caller_constructed_or_subclassed() -> None:
    with pytest.raises(TypeError, match="product-resolved"):
        ProductFixedNIidCohort(
            experiment_id="forged",
            sampling_manifest_sha256="1" * 64,
            planned_member_ids=(),
            member_stream_sha256=(),
            run_source_evidence_sha256=(),
            run_execution_receipt_sha256=(),
            observations=(),
            outcomes_available_at="2026-01-01T00:00:00+00:00",
            cohort_sha256="2" * 64,
        )

    with pytest.raises(TypeError, match="must not be subclassed"):
        class ForgedCohort(ProductFixedNIidCohort):
            pass
