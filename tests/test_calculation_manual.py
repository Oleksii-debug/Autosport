from __future__ import annotations

from dataclasses import replace
import json

import pytest

import autosport.calculation_manual as calculation_manual
from autosport.calculation import CalculationEngine
from autosport.calculation_input import (
    CalculationInputBoundary,
    CalculationInputLimits,
    MANUAL_CALCULATION_INPUT,
)
from autosport.calculation_manual import ManualCalculationEvidence, ManualCalculationService


def _assert_same_result(evidence: ManualCalculationEvidence, expected) -> None:
    assert evidence.result == expected
    assert evidence.service_version == "manual-calculation-service-v1"
    assert evidence.input_mode == "manual"
    assert evidence.real_money_execution is False
    assert len(evidence.evidence_sha256) == 64


def test_manual_service_delegates_every_v1_formula_to_canonical_engine() -> None:
    engine = CalculationEngine()
    service = ManualCalculationService(engine=engine)

    _assert_same_result(
        service.odds_conversion("2.5"),
        engine.odds_conversion("2.5"),
    )
    _assert_same_result(
        service.american_to_decimal_odds("+150"),
        engine.american_to_decimal_odds("+150"),
    )
    _assert_same_result(
        service.fractional_to_decimal_odds("1", "2"),
        engine.fractional_to_decimal_odds("1", "2"),
    )
    _assert_same_result(
        service.implied_probability("2"),
        engine.implied_probability("2"),
    )
    _assert_same_result(
        service.multiplicative_devig({"home": "2", "away": "2.2"}),
        engine.multiplicative_devig({"home": "2", "away": "2.2"}),
    )
    _assert_same_result(
        service.expected_return("0.6", "2", "10"),
        engine.expected_return("0.6", "2", "10"),
    )
    _assert_same_result(
        service.paper_payout("10", "2.5"),
        engine.paper_payout("10", "2.5"),
    )
    _assert_same_result(
        service.fractional_kelly("0.6", "2", fraction="0.5", cap="0.25"),
        engine.fractional_kelly("0.6", "2", fraction="0.5", cap="0.25"),
    )
    _assert_same_result(
        service.performance_summary("10", "100", "1000"),
        engine.performance_summary("10", "100", "1000"),
    )
    _assert_same_result(
        service.return_dispersion(["1", "2", "3"], sample=True),
        engine.return_dispersion(("1", "2", "3"), sample=True),
    )
    _assert_same_result(
        service.normal_confidence_interval(
            "0.5",
            "0.1",
            "1.96",
            assumption="normal_approximation_acknowledged",
        ),
        engine.normal_confidence_interval(
            "0.5",
            "0.1",
            "1.96",
            assumption="normal_approximation_acknowledged",
        ),
    )
    _assert_same_result(
        service.paper_parlay("10", ["2", "3"]),
        engine.paper_parlay("10", ("2", "3")),
    )
    _assert_same_result(
        service.paper_parlay(
            "10",
            ["2", "3"],
            probabilities=["0.5", "0.4"],
            probability_assumption="independent",
        ),
        engine.paper_parlay(
            "10",
            ("2", "3"),
            probabilities=("0.5", "0.4"),
            probability_assumption="independent",
        ),
    )
    _assert_same_result(
        service.finite_scenario_table(
            {"win": "10", "lose": "-5"},
            completeness="complete",
        ),
        engine.finite_scenario_table(
            {"win": "10", "lose": "-5"},
            completeness="complete",
        ),
    )
    _assert_same_result(
        service.maximum_drawdown(["100", "90", "110", "80"]),
        engine.maximum_drawdown(("100", "90", "110", "80")),
    )


def test_invalid_raw_numeric_text_is_rejected_before_engine_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManualCalculationService()

    def explode(*args, **kwargs):
        raise AssertionError("engine must not receive rejected raw manual input")

    monkeypatch.setattr(CalculationEngine, "odds_conversion", explode)

    with pytest.raises(ValueError, match="decimal text"):
        service.odds_conversion(2)
    with pytest.raises(ValueError, match="text limit"):
        service.odds_conversion("1" * 129)


def test_collection_limits_reject_before_later_values_or_engine_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManualCalculationService()

    def explode(*args, **kwargs):
        raise AssertionError("engine must not receive over-limit collection")

    monkeypatch.setattr(CalculationEngine, "multiplicative_devig", explode)
    monkeypatch.setattr(CalculationEngine, "paper_parlay", explode)

    oversized_market = {f"s{index}": "2" for index in range(1000)}
    oversized_market["poison"] = object()

    with pytest.raises(ValueError, match="item limit"):
        service.multiplicative_devig(oversized_market)

    oversized_parlay = ["2"] * 100
    oversized_parlay.append(object())
    with pytest.raises(ValueError, match="item limit"):
        service.paper_parlay("1", oversized_parlay)


def test_manual_identifier_and_enum_text_fail_closed_before_engine() -> None:
    service = ManualCalculationService()

    with pytest.raises(ValueError, match="UTF-8 encodable"):
        service.multiplicative_devig({"\ud800": "2", "other": "2"})
    with pytest.raises(ValueError, match="completeness must be exactly one of"):
        service.finite_scenario_table({"a": "1"}, completeness="unknown")
    with pytest.raises(ValueError, match="assumption must be exactly one of"):
        service.normal_confidence_interval(
            "0",
            "1",
            "1.96",
            assumption="not_acknowledged",
        )
    with pytest.raises(ValueError, match="probability_assumption"):
        service.paper_parlay(
            "1",
            ["2", "2"],
            probabilities=["0.5", "0.5"],
            probability_assumption="dependent",
        )


def test_boolean_control_is_exact_and_not_coerced() -> None:
    service = ManualCalculationService()

    with pytest.raises(ValueError, match="sample must be a boolean"):
        service.return_dispersion(["1", "2"], sample=1)


def test_default_numeric_arguments_also_cross_manual_input_boundary() -> None:
    boundary = CalculationInputBoundary(CalculationInputLimits(numeric_text_chars=1))
    service = ManualCalculationService(input_boundary=boundary)

    _assert_same_result(
        service.expected_return("0", "2"),
        CalculationEngine().expected_return("0", "2", "1"),
    )
    _assert_same_result(
        service.fractional_kelly("0", "2"),
        CalculationEngine().fractional_kelly("0", "2", fraction="1", cap="1"),
    )


def test_manual_evidence_is_deterministic_strict_text_export() -> None:
    service = ManualCalculationService()
    first = service.expected_return("0.6", "2", "10")
    second = service.expected_return("0.6", "2", "10")
    changed = service.expected_return("0.61", "2", "10")

    assert first == second
    assert first.evidence_sha256 == second.evidence_sha256
    assert first.evidence_sha256 != changed.evidence_sha256
    assert first.result.result_hash != changed.result.result_hash

    text = first.to_text()
    assert text.endswith("\n")
    assert text.encode("utf-8").decode("utf-8") == text
    parsed = json.loads(text)
    assert parsed["service_version"] == "manual-calculation-service-v1"
    assert parsed["input_mode"] == "manual"
    assert parsed["real_money_execution"] is False
    assert parsed["evidence_sha256"] == first.evidence_sha256
    assert parsed["result"] == first.result.as_dict()


def test_manual_service_rejects_coercive_container_subclasses() -> None:
    service = ManualCalculationService()

    class _ListSubclass(list[str]):
        pass

    class _DictSubclass(dict[str, str]):
        pass

    with pytest.raises(ValueError, match="list or tuple"):
        service.return_dispersion(_ListSubclass(["1", "2"]))
    with pytest.raises(ValueError, match="must be a dict"):
        service.multiplicative_devig(_DictSubclass({"a": "2", "b": "2"}))


def test_manual_evidence_constructor_rejects_forged_truth_fields() -> None:
    valid = ManualCalculationService().implied_probability("2")

    with pytest.raises(ValueError, match="service_version"):
        ManualCalculationEvidence(
            service_version="forged-service",
            input_mode=valid.input_mode,
            result=valid.result,
            real_money_execution=valid.real_money_execution,
            evidence_sha256=valid.evidence_sha256,
        )
    with pytest.raises(ValueError, match="input_mode"):
        ManualCalculationEvidence(
            service_version=valid.service_version,
            input_mode="product_quote",
            result=valid.result,
            real_money_execution=valid.real_money_execution,
            evidence_sha256=valid.evidence_sha256,
        )
    with pytest.raises(ValueError, match="real_money_execution"):
        ManualCalculationEvidence(
            service_version=valid.service_version,
            input_mode=valid.input_mode,
            result=valid.result,
            real_money_execution=True,
            evidence_sha256=valid.evidence_sha256,
        )
    with pytest.raises(ValueError, match="evidence_sha256"):
        ManualCalculationEvidence(
            service_version=valid.service_version,
            input_mode=valid.input_mode,
            result=valid.result,
            real_money_execution=valid.real_money_execution,
            evidence_sha256="0" * 64,
        )


def test_manual_service_rejects_subclassed_authority_collaborators() -> None:
    class _EngineSubclass(CalculationEngine):
        pass

    class _BoundarySubclass(CalculationInputBoundary):
        pass

    with pytest.raises(ValueError, match="exact CalculationEngine"):
        ManualCalculationService(engine=_EngineSubclass())
    with pytest.raises(ValueError, match="exact CalculationInputBoundary"):
        ManualCalculationService(input_boundary=_BoundarySubclass())


def test_manual_evidence_rejects_forged_inner_result_even_with_matching_outer_hash() -> None:
    valid = ManualCalculationService().expected_return("0.6", "2", "10")
    first_key, _ = valid.result.outputs[0]
    forged_outputs = ((first_key, "999"),) + valid.result.outputs[1:]
    forged_result = replace(valid.result, outputs=forged_outputs)
    matching_outer_hash = calculation_manual._sha256(
        calculation_manual._evidence_payload(forged_result)
    )

    with pytest.raises(ValueError, match="result_hash"):
        ManualCalculationEvidence(
            service_version=valid.service_version,
            input_mode=valid.input_mode,
            result=forged_result,
            real_money_execution=False,
            evidence_sha256=matching_outer_hash,
        )


@pytest.mark.parametrize(
    "field",
    [
        "numeric_text_chars",
        "identifier_chars",
        "identifier_utf8_bytes",
        "collection_items",
    ],
)
def test_manual_service_rejects_input_boundary_looser_than_canonical(field: str) -> None:
    canonical = MANUAL_CALCULATION_INPUT.limits
    values = {
        "numeric_text_chars": canonical.numeric_text_chars,
        "identifier_chars": canonical.identifier_chars,
        "identifier_utf8_bytes": canonical.identifier_utf8_bytes,
        "collection_items": canonical.collection_items,
    }
    values[field] += 1
    boundary = CalculationInputBoundary(CalculationInputLimits(**values))

    with pytest.raises(ValueError, match="must not be looser"):
        ManualCalculationService(input_boundary=boundary)
