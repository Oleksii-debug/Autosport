from __future__ import annotations

import json

import pytest

from autosport.product_decision_activation import (
    ProductDecisionActivationError,
    _product_composition,
)


def _write_composition(tmp_path, payload: dict[str, object]) -> None:
    (tmp_path / "product_composition.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _v2_payload() -> dict[str, object]:
    return {
        "schema": "autosport.autonomous_product_composition",
        "schema_version": 2,
        "source_id": "provider-a",
        "initial_bankroll": "1000",
        "settlement_authority_identity": None,
    }


def test_activation_rejects_future_product_composition_schema(tmp_path) -> None:
    payload = _v2_payload()
    payload["schema_version"] = 3
    _write_composition(tmp_path, payload)

    with pytest.raises(ProductDecisionActivationError, match="schema"):
        _product_composition(tmp_path)


def test_activation_rejects_unknown_v2_product_composition_fields(tmp_path) -> None:
    payload = _v2_payload()
    payload["unknown_future_authority"] = "must-not-be-silently-accepted"
    _write_composition(tmp_path, payload)

    with pytest.raises(ProductDecisionActivationError, match="schema"):
        _product_composition(tmp_path)


def test_activation_rejects_invalid_v2_settlement_authority_identity(tmp_path) -> None:
    payload = _v2_payload()
    payload["settlement_authority_identity"] = "not-a-sha256"
    _write_composition(tmp_path, payload)

    with pytest.raises(ProductDecisionActivationError, match="settlement"):
        _product_composition(tmp_path)
