from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from autosport.ablation_sizing_identity import (
    AblationSizingIdentityError,
    SizingAblationContext,
    assess_sizing_only_ablation,
    derive_sizing_policy_identity,
)


IMPL = "1" * 64
OTHER_IMPL = "2" * 64
NON_TARGET = "a" * 64


def identity(**overrides):
    params = {
        "fraction": Decimal("0.01"),
        "limits": {"max": Decimal("50.00"), "min": Decimal("1.0")},
        "tiers": ["low", "high"],
        "enabled": True,
        "count": 3,
    }
    params.update(overrides.pop("parameters", {}))
    return derive_sizing_policy_identity(
        implementation_sha256=overrides.pop("implementation_sha256", IMPL),
        risk_authority_identity=overrides.pop("risk_authority_identity", "risk-policy:v7"),
        parameters=params,
        **overrides,
    )


def context(**overrides):
    values = dict(
        experiment_id="experiment:sizing-ablation:1",
        data_window_id="window:2026-09-forward-01",
        evaluator_id="evaluator:utility-v3",
        market_universe_id="universe:football-match-odds",
        provider_snapshot_id="provider-snapshot:bf-20260921",
        non_target_policy_fingerprint=NON_TARGET,
    )
    values.update(overrides)
    return SizingAblationContext(**values)


def test_mapping_order_and_nested_order_are_invariant():
    first = derive_sizing_policy_identity(
        implementation_sha256=IMPL,
        risk_authority_identity="risk:v1",
        parameters={"b": {"y": Decimal("2.0"), "x": 1}, "a": "z"},
    )
    second = derive_sizing_policy_identity(
        implementation_sha256=IMPL,
        risk_authority_identity="risk:v1",
        parameters={"a": "z", "b": {"x": 1, "y": Decimal("2.00")}},
    )
    assert first == second


def test_decimal_lexical_forms_and_negative_zero_are_canonical():
    one = derive_sizing_policy_identity(
        implementation_sha256=IMPL,
        risk_authority_identity="risk:v1",
        parameters={"x": Decimal("1.000"), "z": Decimal("-0.000")},
    )
    two = derive_sizing_policy_identity(
        implementation_sha256=IMPL,
        risk_authority_identity="risk:v1",
        parameters={"x": Decimal("1"), "z": Decimal("0")},
    )
    assert one.policy_fingerprint == two.policy_fingerprint


def test_decimal_identity_is_ambient_context_independent():
    with localcontext() as ctx:
        ctx.prec = 3
        low = identity(parameters={"fraction": Decimal("0.01234567890123456789")})
    with localcontext() as ctx:
        ctx.prec = 50
        high = identity(parameters={"fraction": Decimal("0.01234567890123456789")})
    assert low == high


def test_int_and_decimal_remain_distinct_types():
    integer = identity(parameters={"count": 1})
    decimal = identity(parameters={"count": Decimal("1")})
    assert integer.policy_fingerprint != decimal.policy_fingerprint


def test_unicode_nfc_equivalence_and_duplicate_normalized_keys():
    composed = identity(parameters={"label": "café"})
    decomposed = identity(parameters={"label": "cafe\u0301"})
    assert composed.policy_fingerprint == decomposed.policy_fingerprint
    with pytest.raises(AblationSizingIdentityError, match="duplicate keys"):
        identity(parameters={"é": 1, "e\u0301": 2})


def test_sequence_order_is_semantic():
    first = identity(parameters={"tiers": ["low", "high"]})
    second = identity(parameters={"tiers": ["high", "low"]})
    assert first.policy_fingerprint != second.policy_fingerprint


def test_implementation_risk_and_parameters_each_bind_identity():
    base = identity()
    assert identity(implementation_sha256=OTHER_IMPL).policy_fingerprint != base.policy_fingerprint
    assert identity(risk_authority_identity="risk-policy:v8").policy_fingerprint != base.policy_fingerprint
    assert identity(parameters={"fraction": Decimal("0.02")}).policy_fingerprint != base.policy_fingerprint


@pytest.mark.parametrize(
    "bad",
    [
        {"x": 1.25},
        {"x": Decimal("NaN")},
        {"x": Decimal("Infinity")},
        {"x": {1: "not-a-text-key"}},
        {"x": b"bytes"},
        {"x": {"set": {1, 2}}},
    ],
)
def test_unsafe_or_ambiguous_parameter_types_fail_closed(bad):
    with pytest.raises(AblationSizingIdentityError):
        derive_sizing_policy_identity(
            implementation_sha256=IMPL,
            risk_authority_identity="risk:v1",
            parameters=bad,
        )


def test_invalid_top_level_contract_fails_closed():
    with pytest.raises(AblationSizingIdentityError):
        derive_sizing_policy_identity(
            implementation_sha256="not-a-sha",
            risk_authority_identity="risk:v1",
            parameters={},
        )
    with pytest.raises(AblationSizingIdentityError):
        derive_sizing_policy_identity(
            implementation_sha256=IMPL,
            risk_authority_identity=" risk:v1",
            parameters={},
        )
    with pytest.raises(AblationSizingIdentityError, match="mapping"):
        derive_sizing_policy_identity(
            implementation_sha256=IMPL,
            risk_authority_identity="risk:v1",
            parameters=[("x", 1)],
        )


def test_identity_never_grants_financial_or_promotion_authority():
    item = identity()
    assert item.authorizes_stake is False
    assert item.authorizes_risk is False
    assert item.authorizes_execution is False
    assert item.authorizes_promotion is False


def test_exact_single_factor_sizing_change_is_eligible_metadata_only():
    baseline = identity(parameters={"fraction": Decimal("0.01")})
    candidate = identity(parameters={"fraction": Decimal("0.02")})
    result = assess_sizing_only_ablation(
        baseline_sizing=baseline,
        candidate_sizing=candidate,
        baseline_context=context(),
        candidate_context=context(),
    )
    assert result.eligible is True
    assert result.reason == "single_factor_sizing_identity_diff"
    assert result.mismatch_fields == ()
    assert result.causal_effect_proven is False
    assert result.authorizes_stake is False
    assert result.authorizes_execution is False
    assert result.authorizes_promotion is False


def test_same_sizing_identity_is_not_an_ablation():
    item = identity()
    result = assess_sizing_only_ablation(
        baseline_sizing=item,
        candidate_sizing=item,
        baseline_context=context(),
        candidate_context=context(),
    )
    assert result.eligible is False
    assert result.reason == "sizing_identity_unchanged"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("experiment_id", "experiment:other"),
        ("data_window_id", "window:other"),
        ("evaluator_id", "evaluator:other"),
        ("market_universe_id", "universe:other"),
        ("provider_snapshot_id", "provider-snapshot:other"),
        ("non_target_policy_fingerprint", "b" * 64),
    ],
)
def test_each_non_target_confound_blocks_single_factor_claim(field, value):
    result = assess_sizing_only_ablation(
        baseline_sizing=identity(parameters={"fraction": Decimal("0.01")}),
        candidate_sizing=identity(parameters={"fraction": Decimal("0.02")}),
        baseline_context=context(),
        candidate_context=context(**{field: value}),
    )
    assert result.eligible is False
    assert result.reason == "non_target_context_mismatch"
    assert result.mismatch_fields == (field,)


def test_multiple_non_target_confounds_are_reported_deterministically():
    result = assess_sizing_only_ablation(
        baseline_sizing=identity(parameters={"fraction": Decimal("0.01")}),
        candidate_sizing=identity(parameters={"fraction": Decimal("0.02")}),
        baseline_context=context(),
        candidate_context=context(
            evaluator_id="evaluator:other",
            provider_snapshot_id="provider-snapshot:other",
        ),
    )
    assert result.mismatch_fields == ("evaluator_id", "provider_snapshot_id")


def test_constructed_identity_detects_tampered_parameter_digest():
    item = identity()
    with pytest.raises(AblationSizingIdentityError, match="parameters_sha256"):
        replace(item, parameters_sha256="f" * 64)


def test_constructed_identity_detects_tampered_fingerprint():
    item = identity()
    with pytest.raises(AblationSizingIdentityError, match="policy_fingerprint"):
        replace(item, policy_fingerprint="f" * 64)
