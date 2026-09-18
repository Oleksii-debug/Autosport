import json
from datetime import datetime, timedelta, timezone

import pytest

from autosport.research_supervisor import (
    ConflictingResearchTriggerError,
    ResearchPhase,
    ResearchSupervisor,
    ResearchSupervisorError,
    ResearchSupervisorLimitReached,
    ResearchTrigger,
    StaleResearchCheckpointError,
    SupervisorStatus,
)
from autosport.scientific_registry import Hypothesis, ResearchQuestion, ScientificRegistry


SOURCE_SHA = "1" * 64


def _workspace(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        ResearchQuestion(
            question_id="question-1",
            statement="Does the challenger improve the frozen primary metric?",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-17T12:00:00Z",
        )
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    return registry, supervisor


def _trigger(**overrides):
    values = {
        "trigger_id": "trigger-1",
        "question_id": "question-1",
        "requested_at": "2026-09-17T12:01:00Z",
        "budget_units": 32,
        "deadline_at": "2026-09-18T12:00:00Z",
    }
    values.update(overrides)
    return ResearchTrigger(**values)


def test_duplicate_timezone_equivalent_trigger_collapses_to_one_run(tmp_path):
    _, supervisor = _workspace(tmp_path)
    first = supervisor.accept_trigger(_trigger())
    second = supervisor.accept_trigger(
        _trigger(requested_at="2026-09-17T14:01:00+02:00")
    )

    assert second == first
    assert len(supervisor.list_runs()) == 1
    assert first.created_at == "2026-09-17T12:01:00Z"


def test_conflicting_trigger_identity_fails_closed(tmp_path):
    _, supervisor = _workspace(tmp_path)
    supervisor.accept_trigger(_trigger())

    with pytest.raises(ConflictingResearchTriggerError):
        supervisor.accept_trigger(_trigger(budget_units=31))


def test_restart_preserves_checkpoint_and_rejects_stale_phase(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger())
    advanced = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:02:00Z",
    )
    assert advanced.phase is ResearchPhase.HYPOTHESIS
    assert advanced.checkpoint_index == 1

    reopened = ResearchSupervisor(supervisor.path, registry)
    assert reopened.status(started.run_id) == advanced
    with pytest.raises(StaleResearchCheckpointError):
        reopened.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:03:00Z",
        )


def test_scientific_binding_must_exist_and_be_causally_available(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    registry.append(
        Hypothesis(
            hypothesis_id="hypothesis-1",
            research_question_id="question-1",
            statement="Candidate is better.",
            falsifiable_prediction="Primary metric improves.",
            failure_criteria="Primary metric does not improve.",
            primary_metric="score",
            protective_metrics=("drawdown",),
            created_at="2026-09-17T12:10:00Z",
        )
    )
    started = supervisor.accept_trigger(_trigger())

    with pytest.raises(ResearchSupervisorError, match="future Hypothesis"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:05:00Z",
            bindings=(("hypothesis_id", "hypothesis-1"),),
        )

    advanced = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:10:00Z",
        bindings=(("hypothesis_id", "hypothesis-1"),),
    )
    assert advanced.bindings == (("hypothesis_id", "hypothesis-1"),)


def test_unknown_binding_is_not_accepted_as_provenance(tmp_path):
    _, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger())

    with pytest.raises(ResearchSupervisorError, match="unsupported supervisor binding"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:02:00Z",
            bindings=(("made_up_authority", "anything"),),
        )


def test_pause_resume_and_stop_are_durable_and_fail_closed(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger())
    paused = supervisor.pause(started.run_id, at="2026-09-17T12:02:00Z")
    assert paused.status is SupervisorStatus.PAUSED

    with pytest.raises(ResearchSupervisorError, match="not active"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:03:00Z",
        )

    resumed = supervisor.resume(started.run_id, at="2026-09-17T12:04:00Z")
    assert resumed.status is SupervisorStatus.ACTIVE
    stopped = supervisor.stop(
        started.run_id,
        at="2026-09-17T12:05:00Z",
        reason="operator STOP",
    )
    assert stopped.status is SupervisorStatus.STOPPED
    assert stopped.stop_reason == "operator STOP"

    reopened = ResearchSupervisor(supervisor.path, registry)
    assert reopened.status(started.run_id) == stopped
    with pytest.raises(ResearchSupervisorError):
        reopened.resume(started.run_id, at="2026-09-17T12:06:00Z")


def test_budget_exhaustion_stops_without_advancing_phase(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger(budget_units=1))
    supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:02:00Z",
    )

    with pytest.raises(ResearchSupervisorLimitReached, match="budget exhausted"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-09-17T12:03:00Z",
        )

    reopened = ResearchSupervisor(supervisor.path, registry)
    state = reopened.status(started.run_id)
    assert state.phase is ResearchPhase.HYPOTHESIS
    assert state.status is SupervisorStatus.STOPPED
    assert state.stop_reason == "BUDGET_EXHAUSTED"
    assert state.consumed_budget_units == 1


def test_deadline_expiry_is_persisted_as_terminal_stop(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(
        _trigger(deadline_at="2026-09-17T12:02:00Z")
    )

    with pytest.raises(ResearchSupervisorLimitReached, match="deadline expired"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:03:00Z",
        )

    state = ResearchSupervisor(supervisor.path, registry).status(started.run_id)
    assert state.phase is ResearchPhase.QUESTION
    assert state.status is SupervisorStatus.STOPPED
    assert state.stop_reason == "DEADLINE_EXPIRED"


def test_full_frozen_lifecycle_reaches_complete_without_phase_skips(tmp_path):
    _, supervisor = _workspace(tmp_path)
    state = supervisor.accept_trigger(_trigger())
    now = datetime(2026, 9, 17, 12, 2, tzinfo=timezone.utc)

    expected = ResearchPhase.QUESTION
    while expected is not ResearchPhase.COMPLETE:
        state = supervisor.advance(
            state.run_id,
            expected_phase=expected,
            at=now.isoformat(),
        )
        expected = state.phase
        now += timedelta(minutes=1)

    assert state.phase is ResearchPhase.COMPLETE
    assert state.status is SupervisorStatus.COMPLETED
    assert state.checkpoint_index == len(ResearchPhase) - 1


def test_state_digest_detects_out_of_band_mutation(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    supervisor.accept_trigger(_trigger())
    payload = json.loads(supervisor.path.read_text(encoding="utf-8"))
    payload["runs"][0]["question_id"] = "tampered-question"
    supervisor.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="state digest mismatch"):
        ResearchSupervisor(supervisor.path, registry)


def test_trigger_cannot_reference_future_question(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        ResearchQuestion(
            question_id="future-question",
            statement="Future question",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-17T13:00:00Z",
        )
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    with pytest.raises(ResearchSupervisorError, match="from the future"):
        supervisor.accept_trigger(
            ResearchTrigger(
                trigger_id="future-trigger",
                question_id="future-question",
                requested_at="2026-09-17T12:00:00Z",
                budget_units=5,
            )
        )
