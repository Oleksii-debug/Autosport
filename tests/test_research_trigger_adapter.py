import pytest

from autosport.research_supervisor import (
    ConflictingResearchTriggerError,
    ResearchPhase,
    ResearchSupervisor,
)
from autosport.research_trigger_adapter import (
    ExternalResearchTrigger,
    ResearchTriggerAdapter,
    ResearchTriggerAdapterError,
    ResearchTriggerSource,
)
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry


SOURCE_SHA = "a" * 64


def _workspace(tmp_path, *, created_at="2026-09-18T12:00:00Z"):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        ResearchQuestion(
            question_id="question-1",
            statement="Does the bounded research trigger produce one reproducible run?",
            source_sha256=SOURCE_SHA,
            created_at=created_at,
        )
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json", registry
    )
    question = registry.get("ResearchQuestion", "question-1")
    assert question is not None
    return registry, supervisor, question


def _event(question, **overrides):
    values = {
        "source_kind": ResearchTriggerSource.SCHEDULE,
        "source_scope": "research-profile:daily-quality",
        "source_event_id": "schedule-fire-2026-09-18T12:01:00Z",
        "question_id": "question-1",
        "question_record_sha256": question.record_sha256,
        "source_evidence_sha256": SOURCE_SHA,
        "source_observed_at": "2026-09-18T12:00:30Z",
        "requested_at": "2026-09-18T12:01:00Z",
        "budget_units": 8,
        "deadline_at": "2026-09-18T13:00:00Z",
    }
    values.update(overrides)
    return ExternalResearchTrigger(**values)


def test_exact_redelivery_and_timezone_alias_return_one_deterministic_receipt(tmp_path):
    _, supervisor, question = _workspace(tmp_path)
    adapter = ResearchTriggerAdapter(supervisor)
    first = adapter.accept(_event(question))
    alias = adapter.accept(
        _event(
            question,
            source_observed_at="2026-09-18T14:00:30+02:00",
            requested_at="2026-09-18T14:01:00+02:00",
            deadline_at="2026-09-18T15:00:00+02:00",
        )
    )

    assert alias == first
    assert alias.receipt_sha256 == first.receipt_sha256
    assert len(supervisor.list_runs()) == 1
    assert supervisor.list_runs()[0].created_at == "2026-09-18T12:01:00Z"


def test_changed_content_for_same_external_event_fails_closed_without_new_run(tmp_path):
    _, supervisor, question = _workspace(tmp_path)
    adapter = ResearchTriggerAdapter(supervisor)
    adapter.accept(_event(question))
    before = supervisor.path.read_bytes()

    with pytest.raises(ConflictingResearchTriggerError):
        adapter.accept(_event(question, budget_units=9))

    assert supervisor.path.read_bytes() == before
    assert len(supervisor.list_runs()) == 1


def test_changed_observation_time_for_same_external_event_fails_closed(tmp_path):
    _, supervisor, question = _workspace(tmp_path)
    adapter = ResearchTriggerAdapter(supervisor)
    adapter.accept(_event(question))
    before = supervisor.path.read_bytes()

    with pytest.raises(ConflictingResearchTriggerError):
        adapter.accept(_event(question, source_observed_at="2026-09-18T12:00:31Z"))

    assert supervisor.path.read_bytes() == before
    assert len(supervisor.list_runs()) == 1


def test_question_and_source_evidence_are_mechanically_bound_before_supervisor_write(tmp_path):
    _, supervisor, question = _workspace(tmp_path)
    adapter = ResearchTriggerAdapter(supervisor)
    before = supervisor.path.read_bytes()

    with pytest.raises(ResearchTriggerAdapterError, match="source evidence"):
        adapter.accept(_event(question, source_evidence_sha256="b" * 64))
    assert supervisor.path.read_bytes() == before

    with pytest.raises(ResearchTriggerAdapterError, match="record hash mismatch"):
        adapter.accept(_event(question, question_record_sha256="b" * 64))
    assert supervisor.path.read_bytes() == before


def test_future_question_is_rejected_before_supervisor_write(tmp_path):
    _, supervisor, question = _workspace(tmp_path, created_at="2026-09-18T12:02:00Z")
    adapter = ResearchTriggerAdapter(supervisor)
    before = supervisor.path.read_bytes()

    with pytest.raises(ResearchTriggerAdapterError, match="from the future"):
        adapter.accept(_event(question, requested_at="2026-09-18T12:01:00Z"))

    assert supervisor.path.read_bytes() == before


def test_event_chronology_and_deadline_are_fail_closed_at_the_boundary(tmp_path):
    _, _, question = _workspace(tmp_path)
    with pytest.raises(ValueError, match="source_observed_at cannot follow"):
        _event(
            question,
            source_observed_at="2026-09-18T12:01:01Z",
            requested_at="2026-09-18T12:01:00Z",
        )
    with pytest.raises(ValueError, match="deadline_at cannot precede"):
        _event(question, deadline_at="2026-09-18T12:00:59Z")
    with pytest.raises(ValueError, match="lowercase canonical"):
        _event(question, source_evidence_sha256=("A" * 64))


def test_exact_redelivery_after_advance_and_restart_preserves_acceptance_receipt(tmp_path):
    registry, supervisor, question = _workspace(tmp_path)
    event = _event(question)
    first = ResearchTriggerAdapter(supervisor).accept(event)

    advanced = supervisor.advance(
        first.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-18T12:02:00Z",
        budget_cost=1,
    )
    assert advanced.checkpoint_sha256 != first.checkpoint_sha256

    reopened = ResearchSupervisor(supervisor.path, registry)
    replay = ResearchTriggerAdapter(reopened).accept(event)

    assert replay == first
    assert replay.receipt_sha256 == first.receipt_sha256
    assert reopened.status(first.run_id).checkpoint_sha256 == advanced.checkpoint_sha256


def test_reopened_supervisor_preserves_external_event_replay_receipt(tmp_path):
    registry, supervisor, question = _workspace(tmp_path)
    first = ResearchTriggerAdapter(supervisor).accept(_event(question))
    reopened = ResearchSupervisor(supervisor.path, registry)

    replay = ResearchTriggerAdapter(reopened).accept(_event(question))

    assert replay == first
    assert reopened.status(first.run_id).checkpoint_sha256 == first.checkpoint_sha256


def test_event_identity_is_deterministic_but_source_scope_isolation_is_preserved(tmp_path):
    _, _, question = _workspace(tmp_path)
    first = _event(question)
    same = _event(question)
    other_scope = _event(question, source_scope="research-profile:hourly-market")

    assert first.source_event_identity_sha256 == same.source_event_identity_sha256
    assert first.source_event_sha256 == same.source_event_sha256
    assert first.source_event_identity_sha256 != other_scope.source_event_identity_sha256
