from __future__ import annotations

import json
from hashlib import sha256

import pytest

from autosport.research_supervisor import (
    ConflictingResearchTriggerError,
    InvalidResearchTransitionError,
    ResearchPhase,
    ResearchSupervisorError,
    ResearchSupervisorState,
    ResearchSupervisorStore,
    ResearchTrigger,
)


NOW = "2026-09-17T17:00:00Z"


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _trigger(*, requested_at: str = NOW, fingerprint: str = "request-a") -> ResearchTrigger:
    return ResearchTrigger(
        trigger_id="trigger-1",
        trigger_type="scheduled",
        question_id="question-1",
        requested_at=requested_at,
        source_id="autopilot",
        request_fingerprint=_fingerprint(fingerprint),
    )


def _store(tmp_path):
    return ResearchSupervisorStore.initialize_pristine(tmp_path / "research-supervisor.json")


def test_trigger_identity_canonicalizes_equivalent_instants() -> None:
    first = _trigger(requested_at="2026-09-17T19:00:00+02:00")
    second = _trigger(requested_at="2026-09-17T17:00:00Z")
    assert first.canonical_id == second.canonical_id


def test_duplicate_trigger_delivery_resumes_existing_run(tmp_path) -> None:
    store = _store(tmp_path)
    trigger = _trigger()
    created = store.start_or_resume(
        trigger,
        supervisor_id="supervisor-1",
        run_id="run-1",
        budget_units=5,
        deadline=None,
        updated_at=NOW,
        protocol_id=None,
    )
    resumed = store.start_or_resume(
        trigger,
        supervisor_id="different-supervisor",
        run_id="run-2",
        budget_units=99,
        deadline=None,
        updated_at="2026-09-17T17:01:00Z",
        protocol_id="unexpected",
    )
    assert resumed == created
    assert store.get_run("run-2") is None


def test_conflicting_reuse_of_trigger_id_fails_closed(tmp_path) -> None:
    store = _store(tmp_path)
    store.start_or_resume(
        _trigger(),
        supervisor_id="supervisor-1",
        run_id="run-1",
        budget_units=5,
        deadline=None,
        updated_at=NOW,
        protocol_id=None,
    )
    conflicting = _trigger(fingerprint="different-request")
    with pytest.raises(ConflictingResearchTriggerError):
        store.start_or_resume(
            conflicting,
            supervisor_id="supervisor-1",
            run_id="run-2",
            budget_units=5,
            deadline=None,
            updated_at=NOW,
            protocol_id=None,
        )


def test_phase_machine_requires_adjacent_frozen_transitions() -> None:
    state = ResearchSupervisorState.create(
        run_id="run-1",
        trigger_id="trigger-1",
        supervisor_id="supervisor-1",
        question_id="question-1",
        budget_units=5,
        deadline=None,
        protocol_id=None,
        updated_at=NOW,
    )
    advanced = state.transition(ResearchPhase.HYPOTHESIS, updated_at="2026-09-17T17:01:00Z")
    assert advanced.phase is ResearchPhase.HYPOTHESIS
    with pytest.raises(InvalidResearchTransitionError):
        state.transition(ResearchPhase.PROTOCOL_FREEZE, updated_at="2026-09-17T17:01:00Z")


def test_cancelled_run_cannot_advance() -> None:
    state = ResearchSupervisorState.create(
        run_id="run-1",
        trigger_id="trigger-1",
        supervisor_id="supervisor-1",
        question_id="question-1",
        budget_units=5,
        deadline=None,
        protocol_id=None,
        updated_at=NOW,
    ).cancel(updated_at="2026-09-17T17:01:00Z")
    with pytest.raises(InvalidResearchTransitionError):
        state.transition(ResearchPhase.HYPOTHESIS, updated_at="2026-09-17T17:02:00Z")


def test_budget_is_bounded_and_checkpoint_is_immutable_identity(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.start_or_resume(
        _trigger(),
        supervisor_id="supervisor-1",
        run_id="run-1",
        budget_units=2,
        deadline=None,
        updated_at=NOW,
        protocol_id=None,
    )
    consumed = state.consume_budget(2, updated_at="2026-09-17T17:01:00Z")
    store.persist(consumed)
    with pytest.raises(ResearchSupervisorError):
        consumed.consume_budget(1, updated_at="2026-09-17T17:02:00Z")
    checkpoint_id = sha256(b"checkpoint-1").hexdigest()
    checkpointed = consumed.checkpoint(checkpoint_id=checkpoint_id, updated_at="2026-09-17T17:03:00Z")
    store.persist(checkpointed)
    restored = ResearchSupervisorStore(store.path).get_run("run-1")
    assert restored is not None
    assert restored.checkpoint_id == checkpoint_id
    assert restored.verify().state_sha256 == restored.state_sha256


def test_tampered_state_is_rejected_on_restart(tmp_path) -> None:
    store = _store(tmp_path)
    store.start_or_resume(
        _trigger(),
        supervisor_id="supervisor-1",
        run_id="run-1",
        budget_units=5,
        deadline=None,
        updated_at=NOW,
        protocol_id=None,
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["runs"]["run-1"]["phase"] = ResearchPhase.HYPOTHESIS.value
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResearchSupervisorError):
        ResearchSupervisorStore(store.path)


def test_snapshot_is_operator_readable_and_reports_next_phase(tmp_path) -> None:
    store = _store(tmp_path)
    store.start_or_resume(
        _trigger(),
        supervisor_id="supervisor-1",
        run_id="run-1",
        budget_units=3,
        deadline="2026-09-18T17:00:00+02:00",
        updated_at=NOW,
        protocol_id="protocol-1",
    )
    snapshot = store.snapshot("run-1")
    assert snapshot["phase"] == "QUESTION"
    assert snapshot["next_phase"] == "HYPOTHESIS"
    assert snapshot["remaining_budget_units"] == 3
    assert snapshot["protocol_id"] == "protocol-1"
