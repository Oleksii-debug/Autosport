from __future__ import annotations

import json
import shutil
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.risk_of_ruin_evaluator import (
    IssuedRiskOfRuinResult,
    ProductRiskOfRuinEvaluator,
    RiskEvidenceClass,
    RiskOfRuinEvaluationError,
    RiskOfRuinEvaluationRequest,
    RiskOfRuinIssuanceError,
    RiskPathObservation,
    RiskTargetKind,
    clopper_pearson_upper_bound,
    evaluate_risk_of_ruin,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _observation(
    index: int,
    *,
    ruined: bool = False,
    group: str | None = None,
    available_at: str = "2026-01-01T00:00:00+00:00",
) -> RiskPathObservation:
    return RiskPathObservation(
        independent_unit_id=f"unit-{index:04d}",
        dependence_group_id=group or f"group-{index:04d}",
        minimum_equity=Decimal("-1") if ruined else Decimal("100"),
        outcome_available_at=available_at,
        source_evidence_sha256=f"{index % 10}" * 64,
    )


def _request(
    *,
    observations: tuple[RiskPathObservation, ...] | None = None,
    planned: int = 100,
    target_kind: RiskTargetKind = RiskTargetKind.SINGLE,
    stakes: tuple[Decimal, ...] = (Decimal("1"),),
    capital_state_sha256: str = SHA_C,
) -> RiskOfRuinEvaluationRequest:
    if observations is None:
        observations = tuple(_observation(index) for index in range(planned))
    return RiskOfRuinEvaluationRequest(
        target_kind=target_kind,
        bankroll_id="bankroll:test",
        currency="EUR",
        base_portfolio_sha256=SHA_A,
        capital_state_sha256=capital_state_sha256,
        target_sha256=SHA_B,
        evaluated_stakes=stakes,
        research_protocol_sha256=SHA_D,
        reproducibility_bundle_sha256=SHA_E,
        dataset_snapshot_id="dataset:test:001",
        dataset_manifest_sha256=SHA_F,
        causal_cutoff="2026-01-02T00:00:00+00:00",
        evaluated_at="2026-01-03T00:00:00+00:00",
        confidence_level=Decimal("0.95"),
        ruin_threshold=Decimal("0"),
        planned_independent_units=planned,
        evidence_class=RiskEvidenceClass.PAPER,
        observations=observations,
    )


def _evaluator(tmp_path: Path, name: str = "workspace") -> ProductRiskOfRuinEvaluator:
    workspace = (tmp_path / name).resolve()
    authority_root = (tmp_path / f"{name}-authority").resolve()
    return ProductRiskOfRuinEvaluator(
        workspace=workspace,
        authority_root=authority_root,
    )


def test_request_has_no_caller_upper_bound_field() -> None:
    names = {field.name for field in fields(RiskOfRuinEvaluationRequest)}
    assert "upper_bound" not in names


def test_zero_events_never_become_zero_risk() -> None:
    bound = clopper_pearson_upper_bound(
        ruin_count=0,
        independent_units=100,
        confidence_level=Decimal("0.95"),
    )

    assert bound > 0
    assert abs(bound - Decimal("0.0295130496070399")) < Decimal("1e-15")


@pytest.mark.parametrize(
    ("ruin_count", "independent_units", "expected"),
    [
        (0, 10, Decimal("0.2588655508930523")),
        (1, 10, Decimal("0.3941633024365047")),
        (5, 10, Decimal("0.7775588989918709")),
        (2, 100, Decimal("0.0616191149798715")),
    ],
)
def test_exact_bound_matches_reference_cases(
    ruin_count: int,
    independent_units: int,
    expected: Decimal,
) -> None:
    bound = clopper_pearson_upper_bound(
        ruin_count=ruin_count,
        independent_units=independent_units,
        confidence_level=Decimal("0.95"),
    )
    assert abs(bound - expected) < Decimal("1e-14")


def test_transient_path_breach_counts_as_ruin() -> None:
    observations = tuple(
        _observation(index, ruined=(index == 4)) for index in range(10)
    )
    request = _request(observations=observations, planned=10)

    result = evaluate_risk_of_ruin(
        request,
        workspace_instance_id="workspace:test",
        issued_at="2026-01-04T00:00:00+00:00",
        source_sha256=SHA_F,
    )

    assert result.ruin_count == 1
    assert result.upper_bound > Decimal("0.1")
    assert result.real_money_execution_authority is False


def test_duplicate_dependence_group_is_rejected() -> None:
    observations = (
        _observation(0, group="shared"),
        _observation(1, group="shared"),
    )

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="cannot reuse a dependence group",
    ):
        _request(observations=observations, planned=2)


def test_fixed_n_requires_complete_preregistered_population() -> None:
    observations = tuple(_observation(index) for index in range(9))

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="exactly the pre-registered unit count",
    ):
        _request(observations=observations, planned=10)


def test_future_outcome_is_rejected_before_evaluation() -> None:
    observations = (
        _observation(
            0,
            available_at="2026-01-02T00:00:01+00:00",
        ),
    )

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="causally available",
    ):
        _request(observations=observations, planned=1)


def test_capital_state_and_vector_order_are_authority_inputs() -> None:
    vector = _request(
        planned=3,
        target_kind=RiskTargetKind.VECTOR,
        stakes=(Decimal("1"), Decimal("2")),
    )
    changed_capital = replace(vector, capital_state_sha256=SHA_D)
    reordered = replace(vector, evaluated_stakes=(Decimal("2"), Decimal("1")))

    assert vector.request_sha256 != changed_capital.request_sha256
    assert vector.request_sha256 != reordered.request_sha256


def test_direct_evaluation_object_is_not_product_issued(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=5)
    direct = evaluate_risk_of_ruin(
        request,
        workspace_instance_id=evaluator.authority.workspace_instance_id,
        issued_at="2026-01-04T00:00:00+00:00",
        source_sha256=SHA_F,
    )

    assert isinstance(direct, IssuedRiskOfRuinResult)
    assert evaluator.verify(direct) is False


def test_issue_is_idempotent_and_restart_resolves(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=10)

    first = evaluator.issue(request)
    second = evaluator.issue(request)

    assert second == first
    assert first.real_money_execution_authority is False

    restarted = ProductRiskOfRuinEvaluator(
        workspace=evaluator.workspace,
        authority_root=evaluator.authority.authority_root,
    )
    resolved = restarted.resolve(first.result_id)

    assert resolved == first
    assert restarted.verify(first) is True
    assert IssuedRiskOfRuinResult.from_payload(first.canonical_payload()) == first


def test_tampered_journal_fails_closed(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    issued = evaluator.issue(_request(planned=10))

    payload = json.loads(evaluator.journal_path.read_text(encoding="utf-8"))
    payload["records"][0]["result"]["upper_bound"] = "0"
    evaluator.journal_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restarted = ProductRiskOfRuinEvaluator(
        workspace=evaluator.workspace,
        authority_root=evaluator.authority.authority_root,
    )
    with pytest.raises(RiskOfRuinIssuanceError):
        restarted.resolve(issued.result_id)


def test_deleted_journal_after_issue_fails_closed(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    issued = evaluator.issue(_request(planned=10))
    evaluator.journal_path.unlink()

    restarted = ProductRiskOfRuinEvaluator(
        workspace=evaluator.workspace,
        authority_root=evaluator.authority.authority_root,
    )
    with pytest.raises(RiskOfRuinIssuanceError, match="stale, copied, deleted or unproven"):
        restarted.resolve(issued.result_id)


def test_copied_journal_does_not_mint_authority_in_another_workspace(
    tmp_path: Path,
) -> None:
    source = _evaluator(tmp_path, "source")
    issued = source.issue(_request(planned=10))

    foreign = _evaluator(tmp_path, "foreign")
    shutil.copy2(source.journal_path, foreign.journal_path)

    restarted_foreign = ProductRiskOfRuinEvaluator(
        workspace=foreign.workspace,
        authority_root=foreign.authority.authority_root,
    )
    with pytest.raises(RiskOfRuinIssuanceError):
        restarted_foreign.resolve(issued.result_id)
