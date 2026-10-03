from __future__ import annotations

import json
from dataclasses import replace

import pytest

import autosport.campaign_forward_universe_cycle_binding as forward_module
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.forward_evidence_completeness import CostEvidence
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from paper_campaign_admission_test_support import AdmissionFixture


def _admission_state_path(fixture: AdmissionFixture):
    return fixture.workspace / "paper-campaign-admission.json"


def test_public_admit_requires_exact_forward_verification_before_prepared(tmp_path) -> None:
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises(TypeError, match="campaign_forward_verification"):
        coordinator.admit(**fixture.admission_values())

    assert not _admission_state_path(fixture).exists()


def test_forward_verification_identity_is_bound_to_decision_and_action(tmp_path) -> None:
    fixture = AdmissionFixture(tmp_path)
    forward = fixture.forward_verification_kwargs()
    verification = forward["campaign_forward_verification"]

    fixture.admit(fixture.coordinator())

    ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")
    decision = ledger.verified_economic_decision_for_material_action(
        "admission-1",
        fixture.goal,
        risk_policy=fixture.risk,
    )
    assert decision is not None
    bound = decision.payload["campaign_forward_verification"]
    assert bound["receipt_sha256"] == verification.receipt_sha256
    assert bound["campaign_id"] == verification.campaign_id
    assert bound["protocol_sha256"] == verification.protocol_sha256
    assert (
        bound["campaign_cycle_authority_sha256"]
        == verification.campaign_cycle_authority_sha256
    )
    assert (
        bound["prospective_evaluation_plan_sha256"]
        == verification.prospective_evaluation_plan_sha256
    )
    assert bound["universe_sha256"] == verification.universe_sha256
    assert bound["membership_sha256"] == verification.membership_sha256
    assert bound["structural_ok"] is True
    assert tuple(bound["structural_codes"]) == ("PASS",)
    assert bound["provider_universe_authority_resolved"] is True
    assert bound["promotion_ready"] is False
    assert bound["real_money_ready"] is False

    bridge_state = json.loads(
        (fixture.workspace / "paper-learning-bridge.json").read_text(encoding="utf-8")
    )
    parameters = dict(
        bridge_state["bindings"][fixture.execution_ticket_id]["action_parameters"]
    )
    assert (
        parameters["campaign_forward_verification_sha256"]
        == verification.receipt_sha256
    )


def test_changed_structural_evidence_is_rejected_before_prepared(tmp_path) -> None:
    fixture = AdmissionFixture(tmp_path)
    forward = fixture.forward_verification_kwargs()
    evidence = forward["campaign_forward_evidence"]
    negative = replace(
        evidence,
        cost_evidence=(CostEvidence(1, False),),
    )

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="changed on canonical re-resolution|not exact structural PASS",
    ):
        fixture.admit(
            fixture.coordinator(),
            campaign_forward_evidence=negative,
        )

    assert not _admission_state_path(fixture).exists()


@pytest.mark.parametrize(
    "overrides,match",
    [
        (
            {
                "decision_payload": {
                    "campaign_forward_verification": {"receipt_sha256": "forged"}
                }
            },
            "replace campaign forward verification authority",
        ),
        (
            {
                "action_parameters": (
                    ("campaign_forward_verification_sha256", "f" * 64),
                )
            },
            "replace campaign forward verification authority",
        ),
    ],
)
def test_caller_cannot_replace_forward_verification_identity(
    tmp_path,
    overrides,
    match,
) -> None:
    fixture = AdmissionFixture(tmp_path)

    with pytest.raises(PaperCampaignAdmissionError, match=match):
        fixture.admit(fixture.coordinator(), **overrides)

    assert not _admission_state_path(fixture).exists()


def test_forward_verifier_surface_rebind_fails_before_prepared(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = AdmissionFixture(tmp_path)
    forward = fixture.forward_verification_kwargs()
    coordinator = fixture.coordinator()
    called: list[str] = []

    def hostile(**_kwargs):
        called.append("verify")
        return forward["campaign_forward_verification"]

    monkeypatch.setattr(
        forward_module,
        "verify_campaign_forward_evidence",
        hostile,
    )

    values = fixture.admission_values()
    values.update(forward)
    with pytest.raises(
        PaperCampaignAdmissionError,
        match="forward verification authority surface changed",
    ):
        coordinator.admit(**values)

    assert called == []
    assert not _admission_state_path(fixture).exists()


def test_admit_instance_shadow_never_executes_before_forward_gate(
    tmp_path,
) -> None:
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    called: list[str] = []

    def hostile(**_kwargs):
        called.append("admit")
        raise AssertionError("hostile admit executed")

    coordinator.__dict__["admit"] = hostile

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="PAPER campaign admission authority method is shadowed",
    ):
        fixture.admit(coordinator)

    assert called == []
    assert not _admission_state_path(fixture).exists()


def test_admit_class_rebind_never_executes_before_forward_gate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    called: list[str] = []

    def hostile(self, **_kwargs):
        del self
        called.append("admit")
        raise AssertionError("hostile admit executed")

    monkeypatch.setattr(type(coordinator), "admit", hostile)

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="admission entry point changed",
    ):
        fixture.admit(coordinator)

    assert called == []
    assert not _admission_state_path(fixture).exists()
