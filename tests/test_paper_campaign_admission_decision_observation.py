from __future__ import annotations

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.learning_environment import Observation
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from paper_campaign_admission_test_support import AdmissionFixture, T0, T1, T2, T3

import pytest


def test_campaign_observation_is_exact_preexecution_origin_evidence(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    records = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    source = next(
        record
        for record in records
        if record.decision_id == fixture.execution_decision_id
    )

    assert fixture.observation.environment_id == coordinator.runtime.environment.environment_id
    assert fixture.observation.observed_at == T0
    assert fixture.observation.available_at == T1
    assert source.observed_ts == T2
    assert T1 < T2
    evidence = dict(fixture.observation.evidence)
    assert set(evidence) == {
        "decision_context_sha256",
        "environment_checkpoint_id",
        "intent_evidence_sha256",
        "intent_provenance_sha256",
        "intent_vector_sha256",
        "market_state_sha256",
    }
    assert not any(key.startswith("execution_") for key in evidence)

    source_payload = source.to_dict()["payload"]["learning_observation"]
    assert source_payload["observation_id"] == fixture.observation.observation_id

    events = coordinator.execution_ledger.events(fixture.execution_run_id)
    reservation = next(
        event for event in events if event["event_type"] == "RUN_RESERVED"
    )
    assert not any(
        event["event_type"] == "DECISION_ORIGIN_BOUND" for event in events
    )
    origin = reservation["payload"]["decision_origin"]
    assert origin["schema_version"] == 2
    assert origin["learning_observation"] == source_payload
    durable = origin["learning_observation"]
    assert durable["observation_id"] == fixture.observation.observation_id
    assert (
        tuple(tuple(item) for item in durable["evidence"])
        == fixture.observation.evidence
    )


def test_same_decision_context_with_different_learning_evidence_rejects(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    changed = dict(fixture.observation.evidence)
    changed["intent_evidence_sha256"] = "f" * 64
    forged = Observation(
        environment_id=fixture.observation.environment_id,
        observed_at=fixture.observation.observed_at,
        available_at=fixture.observation.available_at,
        evidence=tuple(sorted(changed.items())),
    )

    with pytest.raises(PaperCampaignAdmissionError, match="caller observation conflicts"):
        fixture.admit(coordinator, observation=forged)

    after = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    assert after == before


def test_postexecution_observation_cannot_select_campaign_learning_identity(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    forged = Observation(
        environment_id=fixture.observation.environment_id,
        observed_at=T3,
        available_at=T3,
        evidence=(("execution_outcome", "ACCEPTED"),),
    )

    with pytest.raises(PaperCampaignAdmissionError, match="caller observation conflicts"):
        fixture.admit(coordinator, observation=forged)

    after = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    assert after == before


def test_reservation_execution_evidence_substitution_rejects(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    events = coordinator.execution_ledger.events(fixture.execution_run_id)
    event = next(item for item in events if item["event_type"] == "RUN_RESERVED")
    reservation = dict(event["payload"])
    origin = reservation["decision_origin"]
    substituted = dict(reservation["observation_evidence_ids"])
    action_id = next(iter(substituted))
    substituted[action_id] = substituted[action_id] + "-substituted"
    reservation["observation_evidence_ids"] = substituted

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="reservation argument conflicts",
    ):
        coordinator._resolved_execution_decision_id(
            run_id=fixture.execution_run_id,
            reservation=reservation,
            origin=origin,
        )


def test_restart_reresolves_identical_preexecution_observation_bytes(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    resumed = fixture.coordinator(resumed=True)
    events = resumed.execution_ledger.events(fixture.execution_run_id)
    event = next(item for item in events if item["event_type"] == "RUN_RESERVED")
    reservation = event["payload"]
    origin = reservation["decision_origin"]

    authority = resumed._resolved_execution_decision_id(
        run_id=fixture.execution_run_id,
        reservation=reservation,
        origin=origin,
    )

    assert authority.observation == fixture.observation
    assert authority.observation.observation_id == fixture.observation.observation_id
    assert authority.observation.evidence == fixture.observation.evidence


def test_caller_decision_time_is_assertion_only(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()

    with pytest.raises(PaperCampaignAdmissionError, match="caller decision_at conflicts"):
        fixture.admit(coordinator, decision_at=T3)

    after = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    assert after == before
