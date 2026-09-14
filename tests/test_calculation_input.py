from __future__ import annotations

import pytest

from autosport.calculation_input import CalculationInputBoundary, CalculationInputLimits


def test_decimal_text_accepts_canonical_ascii_forms_without_normalizing() -> None:
    boundary = CalculationInputBoundary()
    for value in ("0", "-1", "+1.25", ".25", "1.", "1e-5", "2E+3"):
        assert boundary.decimal_text(value, field="value") == value


@pytest.mark.parametrize(
    "value",
    (1, 1.0, True, "", " 1", "1 ", "NaN", "Infinity", "1_000", "１２", "1 2"),
)
def test_decimal_text_rejects_non_text_or_noncanonical_syntax(value: object) -> None:
    boundary = CalculationInputBoundary()
    with pytest.raises(ValueError):
        boundary.decimal_text(value, field="value")


def test_decimal_text_rejects_oversized_input_before_engine_parse() -> None:
    boundary = CalculationInputBoundary(CalculationInputLimits(numeric_text_chars=8))
    assert boundary.decimal_text("12345678", field="value") == "12345678"
    with pytest.raises(ValueError, match="text limit"):
        boundary.decimal_text("123456789", field="value")


def test_identifier_enforces_character_and_utf8_budgets() -> None:
    boundary = CalculationInputBoundary(
        CalculationInputLimits(identifier_chars=4, identifier_utf8_bytes=8)
    )
    assert boundary.identifier("ABCD", field="selection") == "ABCD"
    assert boundary.identifier("éé", field="selection") == "éé"
    with pytest.raises(ValueError, match="character limit"):
        boundary.identifier("ABCDE", field="selection")
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        boundary.identifier("🙂🙂🙂", field="selection")


def test_identifier_rejects_lone_surrogate_before_hashing() -> None:
    boundary = CalculationInputBoundary()
    with pytest.raises(ValueError, match="UTF-8 encodable"):
        boundary.identifier("\ud800", field="selection")


def test_decimal_sequence_is_bounded_before_element_validation() -> None:
    boundary = CalculationInputBoundary(CalculationInputLimits(collection_items=2))
    assert boundary.decimal_sequence(["1", "2"], field="balance") == ("1", "2")
    with pytest.raises(ValueError, match="item limit"):
        boundary.decimal_sequence(["1", "2", object()], field="balance")


def test_decimal_mapping_validates_keys_and_values_without_reordering() -> None:
    boundary = CalculationInputBoundary()
    raw = {"β": "2.10", "alpha": "3"}
    validated = boundary.decimal_mapping(raw, field="selection_odds")
    assert list(validated) == ["β", "alpha"]
    assert validated == raw


def test_decimal_mapping_rejects_oversized_collection_before_values() -> None:
    boundary = CalculationInputBoundary(CalculationInputLimits(collection_items=2))
    with pytest.raises(ValueError, match="item limit"):
        boundary.decimal_mapping(
            {"a": "2", "b": "3", "c": object()},
            field="selection_odds",
        )


def test_decimal_mapping_rejects_non_utf8_key_and_invalid_decimal_value() -> None:
    boundary = CalculationInputBoundary()
    with pytest.raises(ValueError, match="UTF-8 encodable"):
        boundary.decimal_mapping({"\ud800": "2"}, field="selection_odds")
    with pytest.raises(ValueError, match="canonical ASCII decimal syntax"):
        boundary.decimal_mapping({"a": "NaN"}, field="selection_odds")


@pytest.mark.parametrize(
    "kwargs",
    (
        {"numeric_text_chars": 0},
        {"identifier_chars": -1},
        {"identifier_utf8_bytes": True},
        {"collection_items": 1.5},
    ),
)
def test_limits_reject_invalid_policy_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CalculationInputLimits(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("maximum_items", (0, -1, True, 1.5))
def test_per_call_collection_limit_must_be_positive_integer(maximum_items: object) -> None:
    boundary = CalculationInputBoundary()
    with pytest.raises(ValueError, match="maximum_items"):
        boundary.decimal_sequence(
            [],
            field="values",
            maximum_items=maximum_items,  # type: ignore[arg-type]
        )


def test_per_call_collection_limit_cannot_exceed_global_policy() -> None:
    boundary = CalculationInputBoundary(CalculationInputLimits(collection_items=2))
    with pytest.raises(ValueError, match="item limit"):
        boundary.decimal_sequence(["1", "2", "3"], field="values", maximum_items=10)


@pytest.mark.parametrize("field", ("", " field", "field "))
def test_field_name_must_be_canonical(field: str) -> None:
    boundary = CalculationInputBoundary()
    with pytest.raises(ValueError, match="field"):
        boundary.decimal_text("1", field=field)
