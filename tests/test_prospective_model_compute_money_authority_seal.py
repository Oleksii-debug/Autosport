from __future__ import annotations

from datetime import datetime, timezone

import pytest

import autosport.prospective_model_compute_money as subject


def test_schema_v1_has_no_caller_mintable_positive_money_surface() -> None:
    assert {member.value for member in subject.ProspectiveModelComputeMoneyStatus} == {
        "UNKNOWN_UNPROVEN"
    }
    assert "_RESULT_TOKEN" not in vars(subject)
    assert not hasattr(subject.ProspectiveModelComputeMoneyEvidence, "_from_resolver")

    with pytest.raises(ValueError):
        subject.ProspectiveModelComputeMoneyStatus("KNOWN_AMOUNT")
    with pytest.raises(ValueError):
        subject.ProspectiveModelComputeMoneyStatus("KNOWN_ZERO")

    with pytest.raises(TypeError):
        subject._make_unknown_evidence(
            intent_sha256="a" * 64,
            opportunity_id="opportunity-model-compute-1",
            request_id="request-model-compute-1",
            decision_at=datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc),
            router_decided_at=datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc),
            router_request_sha256="b" * 64,
            router_decision_sha256="c" * 64,
            status="KNOWN_AMOUNT",
            amount="1",
            currency="USD",
            tariff_sha256="d" * 64,
        )
