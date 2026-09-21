from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.research_control_runtime import (
    ResearchControlRuntimeError,
    initialize_research_control_runtime,
    open_research_control_runtime,
)
from autosport.research_curriculum import ResearchCurriculumError
from autosport.research_scheduler import ResearchSchedule, TickAction, WakeSource
from autosport.scientific_registry import ResearchQuestion


SOURCE_SHA = "1" * 64


def _add_question(runtime) -> None:
    runtime.scientific_registry.append(
        ResearchQuestion(
            question_id="question-24x7",
            statement="Does the frozen candidate improve the governed objective?",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-21T12:00:00Z",
        )
    )


def _schedule(runtime) -> ResearchSchedule:
    question = runtime.scientific_registry.get("ResearchQuestion", "question-24x7")
    assert question is not None
    return ResearchSchedule(
        schedule_id="scheduled-question-24x7",
        wake_source=WakeSource.SCHEDULED_QUESTION,
        first_fire_at="2026-09-21T12:01:00Z",
        interval_seconds=3600,
        misfire_grace_seconds=60,
        question_id=question.record_id,
        question_record_sha256=question.record_sha256,
        source_evidence_sha256=SOURCE_SHA,
        source_observed_at=question.available_at,
        budget_units=3,
    )


def test_initialize_and_reopen_compose_one_canonical_authority_graph(tmp_path):
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)

    assert runtime.supervisor.scientific_registry is runtime.scientific_registry
    assert runtime.trigger_adapter.supervisor is runtime.supervisor
    assert runtime.curriculum.trigger_adapter is runtime.trigger_adapter
    assert runtime.scheduler.trigger_sink is runtime.trigger_adapter
    assert runtime.scheduler.source_registry is runtime.scientific_registry
    assert {path.name for path in runtime.paths.all()} == {
        "scientific_registry.json",
        "research_supervisor.json",
        "research_curriculum.json",
        "research_scheduler.json",
    }

    reopened = open_research_control_runtime(tmp_path, max_budget_units=8)
    assert reopened.paths == runtime.paths
    assert reopened.scheduler.snapshot() == runtime.scheduler.snapshot()
    assert reopened.curriculum.snapshot() == runtime.curriculum.snapshot()
    assert reopened.supervisor.list_runs() == runtime.supervisor.list_runs()


def test_scheduled_wake_flows_through_adapter_and_supervisor_once_across_restart(tmp_path):
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)
    _add_question(runtime)
    runtime.scheduler.add_schedule(_schedule(runtime))

    delivered = runtime.tick_scheduled(now="2026-09-21T12:01:00Z")
    assert delivered.action is TickAction.DELIVERED
    assert delivered.receipt is not None
    assert len(runtime.supervisor.list_runs()) == 1

    reopened = open_research_control_runtime(tmp_path, max_budget_units=8)
    assert len(reopened.supervisor.list_runs()) == 1
    same_instant = reopened.tick_scheduled(now="2026-09-21T12:01:00Z")
    assert same_instant.action is TickAction.IDLE
    assert len(reopened.supervisor.list_runs()) == 1


def test_partial_state_is_never_silently_rebootstrapped(tmp_path):
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)
    runtime.paths.scheduler.unlink()

    with pytest.raises(ResearchControlRuntimeError, match="incomplete"):
        open_research_control_runtime(tmp_path, max_budget_units=8)
    with pytest.raises(ResearchControlRuntimeError, match="incomplete"):
        initialize_research_control_runtime(tmp_path, max_budget_units=8)
    assert not runtime.paths.scheduler.exists()


def test_existing_complete_state_requires_explicit_open(tmp_path):
    initialize_research_control_runtime(tmp_path, max_budget_units=8)

    with pytest.raises(ResearchControlRuntimeError, match="use open_research_control_runtime"):
        initialize_research_control_runtime(tmp_path, max_budget_units=8)


def test_restart_rejects_changed_curriculum_budget_authority(tmp_path):
    initialize_research_control_runtime(tmp_path, max_budget_units=8)

    with pytest.raises(ResearchCurriculumError, match="budget authority changed"):
        open_research_control_runtime(tmp_path, max_budget_units=9)


def test_run_scheduled_delegates_to_existing_scheduler_loop(tmp_path):
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)
    _add_question(runtime)
    runtime.scheduler.add_schedule(_schedule(runtime))
    sleeps: list[float] = []
    times = iter(
        [
            datetime(2026, 9, 21, 12, 0, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 21, 12, 1, 0, tzinfo=timezone.utc),
        ]
    )

    ticks = runtime.run_scheduled(
        clock=lambda: next(times),
        sleep=sleeps.append,
        poll_seconds=2,
        max_ticks=2,
    )

    assert ticks == 2
    assert sleeps == [2.0]
    assert len(runtime.supervisor.list_runs()) == 1
