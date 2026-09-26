from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

from autosport.research_control_runtime import (
    ResearchControlRuntimeError,
    initialize_research_control_runtime,
    open_research_control_runtime,
)
from autosport.research_curriculum import ResearchCurriculumError
from autosport.research_scheduler import ResearchSchedule, TickAction, WakeSource
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry


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



def test_initialize_reuses_existing_canonical_scientific_registry(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    registry.append(
        ResearchQuestion(
            question_id="preexisting-question",
            statement="Pre-existing governed research question.",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-21T11:00:00Z",
        )
    )

    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)

    entry = runtime.scientific_registry.get("ResearchQuestion", "preexisting-question")
    assert entry is not None
    assert entry.record_sha256 == registry.get(
        "ResearchQuestion", "preexisting-question"
    ).record_sha256
    assert runtime.paths.supervisor.exists()
    assert runtime.paths.curriculum.exists()
    assert runtime.paths.scheduler.exists()

def test_partial_state_is_never_silently_rebootstrapped(tmp_path):
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)
    runtime.paths.scheduler.unlink()

    with pytest.raises(ResearchControlRuntimeError, match="incomplete"):
        open_research_control_runtime(tmp_path, max_budget_units=8)
    with pytest.raises(ResearchControlRuntimeError, match="incomplete"):
        initialize_research_control_runtime(tmp_path, max_budget_units=8)
    assert not runtime.paths.scheduler.exists()



def test_concurrent_first_initialization_has_one_creator_and_no_partial_state(tmp_path):
    barrier = Barrier(2)

    def initialize_once():
        barrier.wait()
        try:
            initialize_research_control_runtime(tmp_path, max_budget_units=8)
        except ResearchControlRuntimeError as exc:
            return ("error", str(exc))
        return ("created", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: initialize_once(), range(2)))

    assert sum(kind == "created" for kind, _ in outcomes) == 1
    errors = [message for kind, message in outcomes if kind == "error"]
    assert len(errors) == 1
    assert "use open_research_control_runtime" in errors[0]

    reopened = open_research_control_runtime(tmp_path, max_budget_units=8)
    assert all(path.exists() for path in reopened.paths.all())

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


@pytest.mark.parametrize("bad_budget", [0, -1, True, 1.5, "8"])
def test_invalid_budget_fails_before_any_research_state_is_created(tmp_path, bad_budget):
    with pytest.raises(ResearchControlRuntimeError, match="max_budget_units"):
        initialize_research_control_runtime(tmp_path, max_budget_units=bad_budget)

    assert not (tmp_path / "scientific_registry.json").exists()
    assert not (tmp_path / "research_supervisor.json").exists()
    assert not (tmp_path / "research_curriculum.json").exists()
    assert not (tmp_path / "research_scheduler.json").exists()


def test_product_scheduler_loop_has_production_timing_defaults(tmp_path) -> None:
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)

    assert runtime.run_scheduled(max_ticks=1) == 1

    snapshot = runtime.scheduler.snapshot()
    assert snapshot["occurrences"] == {}


def test_runtime_rejects_scheduler_rebound_to_another_workspace(tmp_path):
    canonical = initialize_research_control_runtime(
        tmp_path / "canonical",
        max_budget_units=8,
    )
    other = initialize_research_control_runtime(
        tmp_path / "other",
        max_budget_units=8,
    )

    object.__setattr__(canonical, "scheduler", other.scheduler)

    with pytest.raises(ResearchControlRuntimeError):
        canonical.tick_scheduled(now="2026-09-21T12:00:00Z")
