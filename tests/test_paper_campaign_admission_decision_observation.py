from __future__ import annotations

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.learning_environment import Observation
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from paper_campaign_admission_test_support import AdmissionFixture, T2, T3

import pytest


def test_campaign_observation_is_exact_preexecution_decision_projection(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    records = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    source = next(record for record in records if record.decision_id == fixture.execution_decision_id)

    assert fixture.observation.environment_id == coordinator.runtime.environment.environment_id
    assert fixture.observation.observed_at == source.observed_ts == T2
    assert fixture.observation.available_at == source.observed_ts
    evidence = dict(fixture.observation.evidence)
    assert set(evidence) == {"context_hash", "decision_id", "decision_record_sha256"}
    assert evidence["context_hash"] == source.context_hash
    assert evidence["decision_id"] == source.decision_id

    events = coordinator.execution_ledger.events(fixture.execution_run_id)
    reservation = next(event for event in events if event["event_type"] == "RUN_RESERVED")
    assert not any(event["event_type"] == "DECISION_ORIGIN_BOUND" for event in events)
    assert reservation["payload"]["decision_origin"]["record_sha256"] == evidence[
        "decision_record_sha256"
    ]


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


def test_caller_decision_time_is_assertion_only(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()

    with pytest.raises(PaperCampaignAdmissionError, match="caller decision_at conflicts"):
        fixture.admit(coordinator, decision_at=T3)

    after = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verified_records()
    assert after == before
