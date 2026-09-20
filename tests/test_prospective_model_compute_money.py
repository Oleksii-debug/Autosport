from __future__ import annotations

from datetime import datetime, timezone

import pytest

import autosport.prospective_model_compute_money as subject


DECISION_AT = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


class _Opportunity:
    def __init__(self, opportunity_id: str = "opportunity-model-compute-1") -> None:
        self.opportunity_id = opportunity_id


class _Intent:
    def __init__(
        self,
        *,
        intent_sha256: str = "a" * 64,
        opportunity_id: str = "opportunity-model-compute-1",
    ) -> None:
        self.intent_sha256 = intent_sha256
        self.opportunity = _Opportunity(opportunity_id)


class _Decision:
    decided_at = "2026-09-20T23:59:59Z"

    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload or {
            "decision_id": "decision-model-compute-1",
            "request_id": "request-model-compute-1",
            "tier": "LOCAL",
            "candidate_id": "model-local-1",
            "backend_id": "backend-local-1",
            "model_id": "model-1",
            "config_sha256": "c" * 64,
            "estimated_cost": "7.5",
        }
        self.request_id = self._payload["request_id"]

    def payload(self) -> dict:
        return self._payload


class _Store:
    def __init__(self, decision: _Decision | None = None) -> None:
        self.decision = decision or _Decision()
        self.reads = 0

    def get_decision(self, request_id: str):
        self.reads += 1
        return self.decision


def _patch_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "OpportunityIntent", _Intent)
    monkeypatch.setattr(subject, "ModelComputeRouterStore", _Store)
    monkeypatch.setattr(subject, "ComputeRouteDecision", _Decision)


def _resolve(store: _Store | None = None, intent: _Intent | None = None):
    return subject.resolve_prospective_model_compute_money(
        intent=intent or _Intent(),
        router_store=store or _Store(),
        request_id="request-model-compute-1",
        decision_at=DECISION_AT,
    )


def test_current_product_truth_stays_unknown_without_money_tariff(monkeypatch):
    _patch_types(monkeypatch)

    result = _resolve()

    assert result.status is subject.ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN
    assert (
        result.reason
        is subject.ProspectiveModelComputeMoneyReason.NO_PREDECISION_MONETARY_TARIFF_AUTHORITY
    )
    assert result.amount is None
    assert result.currency is None
    assert result.tariff_sha256 is None
    assert result.intent_sha256 == "a" * 64
    assert result.opportunity_id == "opportunity-model-compute-1"
    assert result.request_id == "request-model-compute-1"
    assert result.router_decided_at < result.decision_at
    assert result.to_dict()["evidence_id"] == result.evidence_id


def test_real_router_decision_shape_needs_no_nested_request_payload(monkeypatch):
    _patch_types(monkeypatch)
    decision = _Decision(
        {
            "decision_id": "decision-model-compute-1",
            "request_id": "request-model-compute-1",
            "tier": "WAIT",
            "candidate_id": None,
            "backend_id": None,
            "model_id": None,
            "config_sha256": None,
            "estimated_cost": "0",
        }
    )

    result = _resolve(_Store(decision))

    assert result.request_id == "request-model-compute-1"
    assert result.status is subject.ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN


def test_compute_units_or_money_looking_route_fields_cannot_mint_money(monkeypatch):
    _patch_types(monkeypatch)
    decision = _Decision(
        {
            "decision_id": "decision-model-compute-1",
            "request_id": "request-model-compute-1",
            "tier": "CLOUD",
            "candidate_id": "model-cloud-1",
            "backend_id": "backend-cloud-1",
            "model_id": "cloud-model",
            "config_sha256": "d" * 64,
            "estimated_cost": "0",
            "measured_compute_cost": "0",
            "currency": "USD",
            "billing_tariff_sha256": "b" * 64,
        }
    )

    result = _resolve(_Store(decision))

    assert result.status is subject.ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN
    assert result.amount is None
    assert result.currency is None
    assert result.tariff_sha256 is None


def test_route_or_backend_substitution_changes_evidence_identity(monkeypatch):
    _patch_types(monkeypatch)
    first = _resolve(
        _Store(
            _Decision(
                {
                    "decision_id": "decision-model-compute-1",
                    "request_id": "request-model-compute-1",
                    "tier": "LOCAL",
                    "candidate_id": "model-local-1",
                    "backend_id": "backend-local-1",
                    "model_id": "model-local",
                    "config_sha256": "c" * 64,
                    "estimated_cost": "7.5",
                }
            )
        )
    )
    second = _resolve(
        _Store(
            _Decision(
                {
                    "decision_id": "decision-model-compute-1",
                    "request_id": "request-model-compute-1",
                    "tier": "CLOUD",
                    "candidate_id": "model-cloud-1",
                    "backend_id": "backend-cloud-1",
                    "model_id": "model-cloud",
                    "config_sha256": "d" * 64,
                    "estimated_cost": "7.5",
                }
            )
        )
    )

    assert first.router_request_sha256 == second.router_request_sha256
    assert first.router_decision_sha256 != second.router_decision_sha256
    assert first.evidence_id != second.evidence_id


def test_cross_intent_or_opportunity_reuse_changes_proof_identity(monkeypatch):
    _patch_types(monkeypatch)

    first = _resolve(intent=_Intent(intent_sha256="a" * 64, opportunity_id="opportunity-1"))
    second = _resolve(intent=_Intent(intent_sha256="b" * 64, opportunity_id="opportunity-2"))

    assert first.router_decision_sha256 == second.router_decision_sha256
    assert first.intent_sha256 != second.intent_sha256
    assert first.opportunity_id != second.opportunity_id
    assert first.evidence_id != second.evidence_id


def test_future_router_decision_cannot_backdate_prospective_truth(monkeypatch):
    _patch_types(monkeypatch)
    decision = _Decision()
    decision.decided_at = "2026-09-21T00:00:01Z"

    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="from the future",
    ):
        _resolve(_Store(decision))


def test_cross_request_reuse_fails_closed(monkeypatch):
    _patch_types(monkeypatch)
    decision = _Decision(
        {
            "decision_id": "decision-model-compute-2",
            "request_id": "different-request",
            "tier": "LOCAL",
            "candidate_id": "model-local-1",
            "backend_id": "backend-local-1",
            "model_id": "model-local",
            "config_sha256": "c" * 64,
            "estimated_cost": "7.5",
        }
    )

    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="request identity mismatch",
    ):
        _resolve(_Store(decision))


def test_payload_request_identity_mismatch_fails_closed(monkeypatch):
    _patch_types(monkeypatch)
    decision = _Decision()
    decision.request_id = "request-model-compute-1"
    decision._payload = {**decision._payload, "request_id": "different-request"}

    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="decision payload request identity mismatch",
    ):
        _resolve(_Store(decision))


def test_router_subclass_is_rejected_before_authority_read(monkeypatch):
    _patch_types(monkeypatch)
    touched = False

    class _EvilStore(_Store):
        def get_decision(self, request_id: str):
            nonlocal touched
            touched = True
            raise AssertionError("subclass authority was invoked")

    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="exact canonical ModelComputeRouterStore",
    ):
        subject.resolve_prospective_model_compute_money(
            intent=_Intent(),
            router_store=_EvilStore(),
            request_id="request-model-compute-1",
            decision_at=DECISION_AT,
        )

    assert touched is False


def test_intent_subclass_is_rejected_before_router_read(monkeypatch):
    _patch_types(monkeypatch)

    class _EvilIntent(_Intent):
        pass

    store = _Store()
    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="exact canonical OpportunityIntent",
    ):
        subject.resolve_prospective_model_compute_money(
            intent=_EvilIntent(),
            router_store=store,
            request_id="request-model-compute-1",
            decision_at=DECISION_AT,
        )

    assert store.reads == 0


def test_noncanonical_decision_type_fails_closed(monkeypatch):
    _patch_types(monkeypatch)

    class _EvilDecision(_Decision):
        pass

    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="non-canonical ComputeRouteDecision",
    ):
        _resolve(_Store(_EvilDecision()))


def test_result_cannot_be_caller_constructed(monkeypatch):
    _patch_types(monkeypatch)

    with pytest.raises(
        subject.ProspectiveModelComputeMoneyError,
        match="created only by the canonical resolver",
    ):
        subject.ProspectiveModelComputeMoneyEvidence()


def test_schema_v1_has_no_positive_status_member():
    assert tuple(subject.ProspectiveModelComputeMoneyStatus) == (
        subject.ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN,
    )


def test_money_arguments_are_not_a_caller_authority_surface(monkeypatch):
    _patch_types(monkeypatch)

    with pytest.raises(TypeError):
        subject.resolve_prospective_model_compute_money(
            intent=_Intent(),
            router_store=_Store(),
            request_id="request-model-compute-1",
            decision_at=DECISION_AT,
            amount="0",  # type: ignore[call-arg]
            currency="USD",  # type: ignore[call-arg]
        )
