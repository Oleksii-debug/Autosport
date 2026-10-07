from decimal import Decimal

import pytest

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    route_compute,
)


SHA256 = "a" * 64


def _candidate(record_type=ComputeCandidate):
    return record_type(
        candidate_id="candidate-1",
        tier=ComputeTier.LOCAL,
        backend_id="backend-1",
        model_id="model-1",
        config_sha256=SHA256,
        capabilities=("forecast",),
        estimated_cost=Decimal("0"),
        estimated_latency_seconds=Decimal("1"),
    )


def _request(record_type=ComputeRouteRequest):
    return record_type(
        request_id="request-1",
        created_at="2026-10-07T00:00:00Z",
        decision_deadline="2026-10-07T00:01:00Z",
        required_capability="forecast",
        data_classification=DataClassification.PUBLIC,
        allow_cloud=False,
        max_cost=Decimal("0"),
        response_ttl_seconds=Decimal("1"),
        baseline_candidate_id="candidate-1",
    )


def _policy(record_type=ComputeRoutingPolicy):
    return record_type(policy_id="policy-1", policy_version=1)


def test_route_rejects_candidate_subclass() -> None:
    class CandidateAlias(ComputeCandidate):
        pass

    with pytest.raises(TypeError, match="exact ComputeCandidate"):
        route_compute(
            _request(),
            (_candidate(CandidateAlias),),
            _policy(),
            as_of="2026-10-07T00:00:30Z",
        )


def test_route_rejects_request_subclass() -> None:
    class RequestAlias(ComputeRouteRequest):
        pass

    with pytest.raises(TypeError, match="exact ComputeRouteRequest"):
        route_compute(
            _request(RequestAlias),
            (_candidate(),),
            _policy(),
            as_of="2026-10-07T00:00:30Z",
        )


def test_route_rejects_policy_subclass() -> None:
    class PolicyAlias(ComputeRoutingPolicy):
        pass

    with pytest.raises(TypeError, match="exact ComputeRoutingPolicy"):
        route_compute(
            _request(),
            (_candidate(),),
            _policy(PolicyAlias),
            as_of="2026-10-07T00:00:30Z",
        )
