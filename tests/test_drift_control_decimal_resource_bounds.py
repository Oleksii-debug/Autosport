from __future__ import annotations

import pytest

import autosport.drift_control as drift_control
from autosport import _drift_decimal_resource_guard as resource_guard


@pytest.mark.parametrize(
    "value",
    (
        "1e1000000",
        "1e-1000000",
        "0e1000000",
        "-9E-999999",
    ),
)
def test_exponent_notation_fails_before_decimal_construction(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    decimal_called = False

    def bomb_decimal(*_args: object, **_kwargs: object) -> object:
        nonlocal decimal_called
        decimal_called = True
        raise AssertionError("noncanonical exponent must fail before Decimal construction")

    monkeypatch.setattr(drift_control, "Decimal", bomb_decimal)

    with pytest.raises(ValueError, match="fixed-point canonical decimal text"):
        drift_control._canonical_decimal(value, "drift_value")

    assert decimal_called is False


def test_oversized_fixed_point_text_fails_before_decimal_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decimal_called = False

    def bomb_decimal(*_args: object, **_kwargs: object) -> object:
        nonlocal decimal_called
        decimal_called = True
        raise AssertionError("oversized fixed-point text must fail before Decimal construction")

    monkeypatch.setattr(drift_control, "Decimal", bomb_decimal)
    value = "1" + ("0" * resource_guard._MAX_DRIFT_DECIMAL_TEXT_LENGTH)

    with pytest.raises(ValueError, match="canonical decimal text exceeds resource limit"):
        drift_control._canonical_decimal(value, "drift_value")

    assert decimal_called is False


def test_normal_canonical_fixed_point_values_preserve_existing_semantics() -> None:
    assert getattr(
        drift_control._canonical_decimal,
        "_autosport_drift_decimal_resource_bounded",
        False,
    )
    for value in ("0", "1", "-1.25", "0.001", "123456.789"):
        assert drift_control._canonical_decimal(value, "drift_value") == value


@pytest.mark.parametrize("value", ("1.0", "01", "-0", "+1"))
def test_prevalidator_does_not_make_previously_noncanonical_text_valid(value: str) -> None:
    with pytest.raises(ValueError, match="must be canonical decimal text"):
        drift_control._canonical_decimal(value, "drift_value")
