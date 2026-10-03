from __future__ import annotations

import json

import pytest

from autosport import campaign_forward_universe_cycle_binding as forward_module
from autosport.paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from autosport import paper_campaign_forward_admission as forward_admission_module
from autosport.paper_campaign_forward_admission import (
    PaperCampaignForwardAdmissionError,
    admit_forward_verified,
)
from paper_campaign_admission_test_support import AdmissionFixture


def _admissions(fixture: AdmissionFixture) -> dict[str, object]:
    raw = json.loads(
        (fixture.workspace / "paper-campaign-admission.json").read_text(
            encoding="utf-8"
        )
    )
    return raw["admissions"]


def test_legacy_admit_cannot_claim_forward_verification(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="cannot claim campaign forward verification authority",
    ):
        fixture.admit(
            coordinator,
            decision_payload={
                "campaign_forward_verification": {
                    "receipt_sha256": "a" * 64,
                }
            },
        )

    assert _admissions(fixture) == {}


def test_legacy_admit_cannot_claim_forward_action_parameter(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="cannot claim campaign forward verification authority",
    ):
        fixture.admit(
            coordinator,
            action_parameters=(
                ("campaign_forward_verification_receipt_sha256", "a" * 64),
            ),
        )

    assert _admissions(fixture) == {}


def test_forward_admission_resolves_before_prepared(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises((TypeError, RuntimeError)):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert _admissions(fixture) == {}


def test_forward_admission_rejects_verifier_surface_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile_verify(**_kwargs):
        hostile_calls.append("verify")
        raise AssertionError("hostile forward verifier executed")

    monkeypatch.setattr(
        forward_module,
        "verify_campaign_forward_evidence",
        hostile_verify,
    )

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="authority surface changed",
    ):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert hostile_calls == []
    assert _admissions(fixture) == {}


def test_forward_admission_rejects_legacy_admit_surface_rebind(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile_admit(*_args, **_kwargs):
        hostile_calls.append("admit")
        raise AssertionError("hostile admission executed")

    monkeypatch.setattr(PaperCampaignAdmissionCoordinator, "admit", hostile_admit)

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="authority surface changed",
    ):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert hostile_calls == []
    assert _admissions(fixture) == {}


def test_forward_admission_rejects_internal_expected_verifier_rebind(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile_verify(**_kwargs):
        hostile_calls.append("verify")
        raise AssertionError("hostile expected verifier executed")

    monkeypatch.setattr(
        forward_admission_module,
        "_EXPECTED_VERIFY",
        hostile_verify,
    )

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="guard internals changed",
    ):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert hostile_calls == []
    assert _admissions(fixture) == {}


def test_forward_admission_rejects_public_surface_rebind_via_captured_reference(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    captured = admit_forward_verified

    def hostile_public(*_args, **_kwargs):
        raise AssertionError("hostile public forward admission executed")

    monkeypatch.setattr(
        forward_admission_module,
        "admit_forward_verified",
        hostile_public,
    )

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="public surface changed",
    ):
        captured(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert _admissions(fixture) == {}
