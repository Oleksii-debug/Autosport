from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal
from fractions import Fraction
import hashlib
from math import comb
from pathlib import Path

import pytest

import autosport.risk_of_ruin_evaluator as risk_module

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


@pytest.mark.parametrize(
    "value",
    (
        Decimal("1E+1000000"),
        Decimal("1E-1000000"),
    ),
)
def test_extreme_decimal_exponents_fail_before_fixed_point_materialization(
    value: Decimal,
) -> None:
    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="fixed-point representation exceeds supported canonical size",
    ):
        replace(_observation(0), minimum_equity=value)


class _AdversarialDecimal(Decimal):
    def is_finite(self) -> bool:
        raise AssertionError("subclass hook must not run")

    def as_tuple(self):
        raise AssertionError("subclass hook must not run")

    def __format__(self, format_spec: str) -> str:
        raise AssertionError("subclass hook must not run")


def test_decimal_subclass_is_rejected_before_virtual_dispatch() -> None:
    value = _AdversarialDecimal("1")

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="must be a finite exact Decimal",
    ):
        replace(_observation(0), minimum_equity=value)


def test_decimal_materialization_bound_matches_durable_parser_domain() -> None:
    accepted_text = "9" * 512
    accepted = Decimal(accepted_text)

    assert risk_module._decimal_text(accepted) == accepted_text
    assert risk_module._decimal_from_payload(accepted_text, "value") == accepted

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="fixed-point representation exceeds supported canonical size",
    ):
        risk_module._decimal_text(Decimal("9" * 513))

    assert risk_module._decimal_text(Decimal("1." + ("0" * 510))) == "1"
    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="fixed-point representation exceeds supported canonical size",
    ):
        risk_module._decimal_text(Decimal("1." + ("0" * 511)))


def _exact_binomial_cdf(
    *,
    successes_at_most: int,
    trials: int,
    probability: Fraction,
) -> Fraction:
    complement = Fraction(1) - probability
    return sum(
        Fraction(comb(trials, successes))
        * probability**successes
        * complement ** (trials - successes)
        for successes in range(successes_at_most + 1)
    )


@pytest.mark.parametrize(
    ("ruin_count", "independent_units", "confidence"),
    [
        (0, 3, Decimal("0.95")),
        (0, 10, Decimal("0.95")),
        (1, 10, Decimal("0.95")),
        (2, 100, Decimal("0.95")),
    ],
)
def test_clopper_pearson_returned_endpoint_is_outward_conservative(
    ruin_count: int,
    independent_units: int,
    confidence: Decimal,
) -> None:
    upper = clopper_pearson_upper_bound(
        ruin_count=ruin_count,
        independent_units=independent_units,
        confidence_level=confidence,
    )

    exact_cdf = _exact_binomial_cdf(
        successes_at_most=ruin_count,
        trials=independent_units,
        probability=Fraction(upper),
    )
    alpha = Fraction(Decimal(1) - confidence)

    assert exact_cdf <= alpha, (
        "returned Clopper-Pearson endpoint rounded below the exact upper root; "
        f"exact CDF(U)-alpha={exact_cdf - alpha}"
    )


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
        (2, 100, Decimal("0.0616192003960407")),
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


def test_caller_request_cannot_mint_product_issued_authority(
    tmp_path: Path,
) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=10)

    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="canonical product-owned",
    ):
        evaluator.issue(request)

    assert not evaluator.journal_path.exists()


def test_pre_authority_v1_journal_is_quarantined_after_upgrade(
    tmp_path: Path,
) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=10)

    empty_state, records = evaluator._read_state_under_lock()
    assert records == ()

    legacy_result = evaluate_risk_of_ruin(
        request,
        workspace_instance_id=evaluator.authority.workspace_instance_id,
        issued_at="2026-01-04T00:00:00+00:00",
        source_sha256=SHA_F,
    )
    record = risk_module._record_payload(
        legacy_result,
        previous_record_sha256=None,
    )
    legacy_state = {
        "schema": empty_state["schema"],
        "schema_version": 1,
        "workspace_instance_id": evaluator.authority.workspace_instance_id,
        "records": [record],
    }
    intended = hashlib.sha256(
        risk_module._atomic_json_bytes(legacy_state)
    ).hexdigest()
    tx_id = record["authority_tx_id"]
    binding = record["semantic_binding_sha256"]

    evaluator.authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    risk_module.atomic_write_json(evaluator.journal_path, legacy_state)
    published = risk_module._file_sha256(evaluator.journal_path)
    assert published == intended
    evaluator.authority.commit(
        tx_id=tx_id,
        observed_state_sha256=published,
        semantic_binding_sha256=binding,
    )

    _, parsed = evaluator._read_state_under_lock()
    assert len(parsed) == 1
    parsed_result = IssuedRiskOfRuinResult.from_payload(parsed[0]["result"])
    assert parsed_result == legacy_result

    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="quarantined as audit history",
    ):
        evaluator.resolve(legacy_result.result_id)
    assert evaluator.verify(legacy_result) is False


def test_repeated_source_evidence_cannot_be_relabelled_as_independent_authority(
    tmp_path: Path,
) -> None:
    observations = tuple(_observation(index) for index in range(100))
    observations = tuple(
        replace(item, source_evidence_sha256=SHA_A)
        for item in observations
    )

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="unique source evidence identity",
    ):
        _request(observations=observations, planned=100)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("independent_units", True),
        ("independent_units", "10"),
        ("ruin_count", False),
        ("ruin_count", "0"),
        ("confidence_level", 0.95),
        ("confidence_level", "0.950"),
        ("ruin_threshold", 0),
        ("upper_bound", 0.1),
    ],
)
def test_durable_result_parser_rejects_json_type_coercion(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=10)
    direct = evaluate_risk_of_ruin(
        request,
        workspace_instance_id=evaluator.authority.workspace_instance_id,
        issued_at="2026-01-04T00:00:00+00:00",
        source_sha256=SHA_F,
    )
    payload = direct.canonical_payload()
    payload[field] = replacement

    with pytest.raises(RiskOfRuinIssuanceError):
        IssuedRiskOfRuinResult.from_payload(payload)


def test_durable_result_parser_rejects_bool_version_and_unknown_fields(
    tmp_path: Path,
) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=10)
    direct = evaluate_risk_of_ruin(
        request,
        workspace_instance_id=evaluator.authority.workspace_instance_id,
        issued_at="2026-01-04T00:00:00+00:00",
        source_sha256=SHA_F,
    )

    bool_version = direct.canonical_payload()
    bool_version["result_version"] = True
    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="unsupported risk-of-ruin result schema",
    ):
        IssuedRiskOfRuinResult.from_payload(bool_version)

    unknown_field = direct.canonical_payload()
    unknown_field["unexpected"] = "ignored-before-fix"
    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="risk-of-ruin result fields mismatch",
    ):
        IssuedRiskOfRuinResult.from_payload(unknown_field)

    assert IssuedRiskOfRuinResult.from_payload(direct.canonical_payload()) == direct


def test_durable_journal_parser_rejects_bool_version_and_unknown_root_fields(
    tmp_path: Path,
) -> None:
    evaluator = _evaluator(tmp_path)
    workspace_instance_id = evaluator.authority.workspace_instance_id

    bool_version = evaluator._empty_state()
    bool_version["schema_version"] = True
    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="risk-of-ruin journal identity mismatch",
    ):
        risk_module._validate_journal(
            bool_version,
            workspace_instance_id=workspace_instance_id,
        )

    unknown_field = evaluator._empty_state()
    unknown_field["unexpected"] = "ignored-before-fix"
    with pytest.raises(
        RiskOfRuinIssuanceError,
        match="risk-of-ruin journal fields mismatch",
    ):
        risk_module._validate_journal(
            unknown_field,
            workspace_instance_id=workspace_instance_id,
        )

    assert (
        risk_module._validate_journal(
            evaluator._empty_state(),
            workspace_instance_id=workspace_instance_id,
        )
        == ()
    )


def test_durable_result_parser_rejects_noncanonical_stake_text(
    tmp_path: Path,
) -> None:
    evaluator = _evaluator(tmp_path)
    request = _request(planned=10)
    direct = evaluate_risk_of_ruin(
        request,
        workspace_instance_id=evaluator.authority.workspace_instance_id,
        issued_at="2026-01-04T00:00:00+00:00",
        source_sha256=SHA_F,
    )
    payload = direct.canonical_payload()
    payload["evaluated_stakes"] = ["1.0"]

    with pytest.raises(RiskOfRuinIssuanceError):
        IssuedRiskOfRuinResult.from_payload(payload)
