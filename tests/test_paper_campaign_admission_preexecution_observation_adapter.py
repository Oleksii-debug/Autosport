from __future__ import annotations

import pytest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.learning_environment import Observation
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from paper_campaign_admission_test_support import AdmissionFixture


def _resolved_authority(fixture: AdmissionFixture, coordinator):
    _, reservation, origin = coordinator._execution_attempt(
        run_id=fixture.execution_run_id,
        attempt_id=fixture.execution_attempt_id,
    )
    return coordinator._resolved_execution_decision_id(
        run_id=fixture.execution_run_id,
        reservation=reservation,
        origin=origin,
    )


def test_versioned_adapter_reuses_exact_preexecution_origin_without_learning_feature_drift(
    tmp_path,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    authority = _resolved_authority(fixture, coordinator)

    assert authority.observation == fixture.observation
    assert authority.observation_adapter_schema == (
        "autosport.paper_campaign_preexecution_observation"
    )
    assert authority.observation_adapter_schema_version == 1
    assert authority.observation_producer_contract == (
        "autosport.persistent_live_decision.v2"
    )
    assert dict(authority.observation.evidence).keys() == {
        "decision_context_sha256",
        "environment_checkpoint_id",
        "intent_evidence_sha256",
        "intent_provenance_sha256",
        "intent_vector_sha256",
        "market_state_sha256",
    }


def test_versioned_adapter_is_restart_idempotent(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    first = _resolved_authority(fixture, fixture.coordinator())
    resumed = _resolved_authority(fixture, fixture.coordinator(resumed=True))

    assert resumed.observation == first.observation
    assert resumed.observation.observation_id == first.observation.observation_id
    assert resumed.observation_adapter_schema == first.observation_adapter_schema
    assert resumed.observation_adapter_schema_version == first.observation_adapter_schema_version
    assert resumed.observation_producer_contract == first.observation_producer_contract


def test_same_time_substituted_learning_evidence_fails_before_decision_mutation(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    decision_path = fixture.workspace / "decisions.jsonl"
    before = decision_path.read_bytes()

    forged = Observation(
        environment_id=fixture.observation.environment_id,
        observed_at=fixture.observation.observed_at,
        available_at=fixture.observation.available_at,
        evidence=(("decision_context_sha256", "f" * 64),),
    )

    with pytest.raises(PaperCampaignAdmissionError, match="caller observation conflicts"):
        fixture.admit(coordinator, observation=forged)

    assert decision_path.read_bytes() == before
    assert JsonlDecisionLedger(decision_path).verified_records()


def test_execution_observation_evidence_cannot_become_learning_observation_identity(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    authority = _resolved_authority(fixture, coordinator)
    _, reservation, _ = coordinator._execution_attempt(
        run_id=fixture.execution_run_id,
        attempt_id=fixture.execution_attempt_id,
    )

    learning_evidence = dict(authority.observation.evidence)
    execution_evidence_ids = reservation["observation_evidence_ids"]
    assert set(learning_evidence) == {
        "decision_context_sha256",
        "environment_checkpoint_id",
        "intent_evidence_sha256",
        "intent_provenance_sha256",
        "intent_vector_sha256",
        "market_state_sha256",
    }
    assert "execution_outcome" not in learning_evidence
    assert "accepted_odds" not in learning_evidence
    assert "accepted_stake" not in learning_evidence
    assert set(execution_evidence_ids.values()).isdisjoint(learning_evidence.values())
