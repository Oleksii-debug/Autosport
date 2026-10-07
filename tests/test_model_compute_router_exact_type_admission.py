from decimal import Decimal

import pytest

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeExecutionEvidence,
    ComputeRouteDecision,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterError,
    VOCEvidenceProvenance,
    ValueOfComputationEvidence,
    route_compute,
)
from autosport.voc_evaluation import PairedVOCEvaluation


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


class _HostilePayload(dict):
    def _dispatch(self, *_args, **_kwargs):
        raise AssertionError(
            "hostile model-router mapping dispatched before exact payload admission"
        )

    __getitem__ = _dispatch
    get = _dispatch
    keys = _dispatch
    __iter__ = _dispatch


class _HostileJsonList(list):
    def _dispatch(self, *_args, **_kwargs):
        raise AssertionError(
            "hostile model-router list dispatched before exact payload admission"
        )

    __iter__ = _dispatch
    __len__ = _dispatch


class _HostileJsonText(str):
    def _dispatch(self, *_args, **_kwargs):
        raise AssertionError(
            "hostile model-router text dispatched before exact payload admission"
        )

    __hash__ = _dispatch
    __eq__ = _dispatch
    __iter__ = _dispatch
    __len__ = _dispatch


@pytest.mark.parametrize(
    "parser",
    (
        ComputeCandidate.from_payload,
        ComputeRouteRequest.from_payload,
        ComputeRoutingPolicy.from_payload,
        ValueOfComputationEvidence.from_payload,
        ComputeRouteDecision.from_payload,
        ComputeExecutionEvidence.from_payload,
    ),
)
def test_public_from_payload_rejects_mapping_subclass_before_dispatch(parser) -> None:
    with pytest.raises(ModelComputeRouterError, match="exact JSON object"):
        parser(_HostilePayload())


def test_candidate_from_payload_rejects_nested_list_subclass_before_dispatch() -> None:
    payload = _candidate().payload()
    payload["capabilities"] = _HostileJsonList(["forecast"])
    with pytest.raises(ModelComputeRouterError, match="exact JSON carrier types"):
        ComputeCandidate.from_payload(payload)


def test_candidate_from_payload_rejects_nested_text_subclass_before_enum_dispatch() -> None:
    payload = _candidate().payload()
    payload["tier"] = _HostileJsonText("LOCAL")
    with pytest.raises(ModelComputeRouterError, match="exact JSON carrier types"):
        ComputeCandidate.from_payload(payload)


def test_candidate_constructor_rejects_capability_text_subclass_before_hash_dispatch() -> None:
    with pytest.raises(ModelComputeRouterError, match="capability"):
        ComputeCandidate(
            candidate_id="candidate-1",
            tier=ComputeTier.LOCAL,
            backend_id="backend-1",
            model_id="model-1",
            config_sha256=SHA256,
            capabilities=(_HostileJsonText("forecast"),),
            estimated_cost=Decimal("0"),
            estimated_latency_seconds=Decimal("1"),
        )


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



class _HostilePairedVOCEvaluation(PairedVOCEvaluation):
    member_accesses = 0

    def __getattribute__(self, name: str):
        if name in {"member_accesses", "__class__", "__dict__"}:
            return object.__getattribute__(self, name)
        type(self).member_accesses += 1
        raise AssertionError(f"hostile PairedVOCEvaluation member accessed: {name}")


def test_voc_evidence_rejects_evaluation_subclass_before_identity_dispatch() -> None:
    _HostilePairedVOCEvaluation.member_accesses = 0
    hostile = object.__new__(_HostilePairedVOCEvaluation)

    with pytest.raises(
        ModelComputeRouterError,
        match="evaluation must be an exact PairedVOCEvaluation",
    ):
        ValueOfComputationEvidence(
            evidence_id="voc-evidence-1",
            baseline_candidate_id="candidate-base",
            challenger_candidate_id="candidate-challenger",
            baseline_backend_id="backend-base",
            baseline_model_id="model-base",
            baseline_config_sha256=SHA256,
            challenger_backend_id="backend-challenger",
            challenger_model_id="model-challenger",
            challenger_config_sha256="b" * 64,
            measured_at="2026-10-07T00:00:00Z",
            available_at="2026-10-07T00:00:01Z",
            provenance=VOCEvidenceProvenance.MEASURED_SHADOW,
            baseline_utility=Decimal("1"),
            challenger_utility=Decimal("2"),
            compute_cost_penalty=Decimal("0"),
            latency_opportunity_cost_penalty=Decimal("0"),
            measured_compute_cost=Decimal("0"),
            evaluation_sha256="c" * 64,
            evaluation=hostile,
        )

    assert _HostilePairedVOCEvaluation.member_accesses == 0
