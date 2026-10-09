from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterError,
    _VOC_PRECOMPUTE_FIELDS,
    _validate_persisted_voc_precompute_admission,
)


_SHA = "a" * 64


def _candidate(**overrides: object) -> ComputeCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "tier": ComputeTier.LOCAL,
        "backend_id": "backend-1",
        "model_id": "model-1",
        "config_sha256": _SHA,
        "capabilities": ("forecast",),
        "estimated_cost": Decimal("0"),
        "estimated_latency_seconds": Decimal("1"),
    }
    values.update(overrides)
    return ComputeCandidate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("candidate_id", "candidate\n1"),
        ("backend_id", "backend\t1"),
        ("model_id", "model\r1"),
    ),
)
def test_compute_candidate_rejects_control_character_identity_alias(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ModelComputeRouterError, match="control characters"):
        _candidate(**{field: value})


def test_route_request_rejects_control_character_request_identity() -> None:
    with pytest.raises(ModelComputeRouterError, match="control characters"):
        ComputeRouteRequest(
            request_id="request\n1",
            created_at="2026-10-07T00:00:00Z",
            decision_deadline="2026-10-07T00:01:00Z",
            required_capability="forecast",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("1"),
            baseline_candidate_id="candidate-1",
        )


def test_routing_policy_rejects_del_control_character_identity() -> None:
    with pytest.raises(ModelComputeRouterError, match="control characters"):
        ComputeRoutingPolicy(policy_id="policy\x7f1", policy_version=1)


def test_valid_router_identity_payload_is_unchanged() -> None:
    candidate = _candidate()
    assert candidate.payload()["candidate_id"] == "candidate-1"
    assert candidate.payload()["backend_id"] == "backend-1"
    assert candidate.payload()["model_id"] == "model-1"


@pytest.mark.parametrize("schema_version", (True, 1.0))
def test_persisted_voc_precompute_rejects_non_exact_int_schema_version(
    schema_version: object,
) -> None:
    raw = {field: None for field in _VOC_PRECOMPUTE_FIELDS}
    raw["schema_version"] = schema_version

    with pytest.raises(
        ModelComputeRouterError,
        match="schema version is unsupported",
    ):
        _validate_persisted_voc_precompute_admission(
            request=None,  # type: ignore[arg-type]
            candidates=(),
            decision=None,  # type: ignore[arg-type]
            domain_observation=None,
            raw=raw,
        )

