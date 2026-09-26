from __future__ import annotations

from decimal import Decimal

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    route_compute,
)


SHA = "a" * 64


def test_secret_data_never_enters_model_route() -> None:
    request = ComputeRouteRequest(
        request_id="secret-request",
        created_at="2026-09-24T16:00:00Z",
        decision_deadline="2026-09-24T16:05:00Z",
        required_capability="research",
        data_classification=DataClassification.SECRET,
        allow_cloud=False,
        max_cost=Decimal("100"),
        response_ttl_seconds=Decimal("60"),
        baseline_candidate_id="local",
    )
    local = ComputeCandidate(
        candidate_id="local",
        tier=ComputeTier.LOCAL,
        backend_id="local-runtime",
        model_id="local-model",
        config_sha256=SHA,
        capabilities=("research",),
        estimated_cost=Decimal("0"),
        estimated_latency_seconds=Decimal("1"),
    )
    policy = ComputeRoutingPolicy(
        policy_id="secret-failclosed",
        policy_version=1,
    )

    decision = route_compute(
        request,
        (local,),
        policy,
        as_of="2026-09-24T16:00:01Z",
    )

    assert decision.tier is ComputeTier.WAIT
    assert decision.candidate_id is None
    assert "secret/credential data" in decision.reason


def test_secret_classification_round_trips_without_downgrade() -> None:
    request = ComputeRouteRequest(
        request_id="secret-roundtrip",
        created_at="2026-09-24T16:00:00Z",
        decision_deadline="2026-09-24T16:05:00Z",
        required_capability="research",
        data_classification=DataClassification.SECRET,
        allow_cloud=True,
        max_cost=Decimal("100"),
        response_ttl_seconds=Decimal("60"),
        baseline_candidate_id="local",
        cloud_candidate_id="cloud",
    )

    restored = ComputeRouteRequest.from_payload(request.payload())

    assert restored == request
    assert restored.data_classification is DataClassification.SECRET
