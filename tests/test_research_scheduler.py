from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from tests.test_research_curriculum import _candidate, _workspace

import pytest

from autosport.research_curriculum import CurriculumPurpose
from autosport.research_scheduler import (
    EvidenceReusePolicy,
    ResearchSchedule,
    ResearchScheduler,
    ResearchSchedulerError,
    SchedulerStatus,
    TickAction,
    WakeSource,
)
from autosport.research_trigger_adapter import ResearchTriggerReceipt
from autosport.scientific_registry import (
    Postmortem,
    ResearchOutcome,
    ResearchQuestion,
    ScientificRegistry,
)


SHA_Q = "1" * 64
SHA_SOURCE = "2" * 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeTriggerSink:
    def __init__(self, *, fail_after_accept_once: bool = False) -> None:
        self.calls = []
        self.accepted = {}
        self.fail_after_accept_once = fail_after_accept_once

    def accept(self, event):
        self.calls.append(event)
        identity = event.source_event_identity_sha256
        prior = self.accepted.get(identity)
        if prior is not None and prior[0] != event.source_event_sha256:
            raise RuntimeError("conflicting event identity")
        if prior is None:
            trigger = event.to_research_trigger()
            receipt = ResearchTriggerReceipt(
                source_event_identity_sha256=identity,
                source_event_sha256=event.source_event_sha256,
                supervisor_trigger_id=trigger.trigger_id,
                supervisor_trigger_sha256=trigger.trigger_sha256,
                run_id=trigger.run_id,
                checkpoint_sha256=_sha("checkpoint:" + trigger.run_id),
            )
            self.accepted[identity] = (event.source_event_sha256, receipt)
        else:
            receipt = prior[1]
        if self.fail_after_accept_once:
            self.fail_after_accept_once = False
            raise RuntimeError("simulated crash after downstream acceptance")
        return receipt


def _schedule(
    *,
    schedule_id: str = "drift-main",
    first_fire_at: str = "2026-09-19T10:00:00Z",
    interval_seconds: int = 60,
    grace_seconds: int = 30,
) -> ResearchSchedule:
    return ResearchSchedule(
        schedule_id=schedule_id,
        wake_source=WakeSource.SCHEDULED_QUESTION,
        first_fire_at=first_fire_at,
        interval_seconds=interval_seconds,
        misfire_grace_seconds=grace_seconds,
        question_id="question-1",
        question_record_sha256=SHA_Q,
        source_evidence_sha256=SHA_SOURCE,
        source_observed_at="2026-09-19T09:59:00Z",
        budget_units=3,
        deadline_offset_seconds=120,
    )


def test_exact_fire_persists_acceptance_and_restart_does_not_duplicate(tmp_path):
    path = tmp_path / "research-scheduler.json"
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(path, sink)
    scheduler.add_schedule(_schedule())

    result = scheduler.tick(now="2026-09-19T10:00:00Z")

    assert result.action is TickAction.DELIVERED
    assert len(sink.calls) == 1
    snapshot = scheduler.snapshot()
    occurrence = snapshot["occurrences"][result.occurrence_id]
    assert occurrence["status"] == "ACCEPTED"
    assert occurrence["event"]["requested_at"] == "2026-09-19T10:00:00Z"

    reopened = ResearchScheduler(path, sink)
    assert reopened.tick(now="2026-09-19T10:00:00Z").action is TickAction.IDLE
    assert len(sink.calls) == 1


def test_crash_after_acceptance_replays_identical_pending_event(tmp_path):
    path = tmp_path / "research-scheduler.json"
    sink = FakeTriggerSink(fail_after_accept_once=True)
    scheduler = ResearchScheduler.initialize_pristine(path, sink)
    scheduler.add_schedule(_schedule())

    with pytest.raises(RuntimeError, match="simulated crash"):
        scheduler.tick(now="2026-09-19T10:00:00Z")

    pending = ResearchScheduler(path, sink).snapshot()
    raw = next(iter(pending["occurrences"].values()))
    assert raw["status"] == "PENDING"
    frozen_event = raw["event"]

    recovered = ResearchScheduler(path, sink)
    result = recovered.tick(now="2026-09-19T10:45:00Z")

    assert result.action is TickAction.DELIVERED
    assert sink.calls[0].canonical_payload() == frozen_event
    assert sink.calls[1].canonical_payload() == frozen_event
    assert len(sink.accepted) == 1


def test_newest_within_grace_collapses_backlog(tmp_path):
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json", sink
    )
    scheduler.add_schedule(_schedule(grace_seconds=30))

    result = scheduler.tick(now="2026-09-19T10:05:10Z")

    assert result.action is TickAction.DELIVERED
    assert len(sink.calls) == 1
    assert sink.calls[0].requested_at == "2026-09-19T10:05:00Z"
    entry = scheduler.snapshot()["schedules"]["drift-main"]
    assert entry["next_fire_at"] == "2026-09-19T10:06:00Z"
    assert entry["last_skip"] == {
        "count": 5,
        "through": "2026-09-19T10:04:00Z",
        "reason": "BACKLOG_COLLAPSED_NEWEST_ONLY",
    }


def test_misfire_outside_grace_is_durably_skipped(tmp_path):
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json", sink
    )
    scheduler.add_schedule(_schedule(interval_seconds=3600, grace_seconds=300))

    result = scheduler.tick(now="2026-09-19T13:10:00Z")

    assert result.action is TickAction.SKIPPED
    assert result.skipped_count == 4
    assert not sink.calls
    snapshot = scheduler.snapshot()
    raw = snapshot["occurrences"][result.occurrence_id]
    assert raw["status"] == "SKIPPED"
    assert raw["skip_reason"] == "MISFIRE_GRACE_EXCEEDED"
    assert snapshot["schedules"]["drift-main"]["next_fire_at"] == (
        "2026-09-19T14:00:00Z"
    )


def test_pause_and_stop_survive_restart_and_stop_is_irreversible(tmp_path):
    path = tmp_path / "research-scheduler.json"
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(path, sink)
    scheduler.add_schedule(_schedule())
    scheduler.pause()

    reopened = ResearchScheduler(path, sink)
    assert reopened.status is SchedulerStatus.PAUSED
    assert reopened.tick(now="2026-09-19T10:00:00Z").action is TickAction.PAUSED
    assert not sink.calls

    reopened.resume()
    reopened.stop("operator STOP")
    stopped = ResearchScheduler(path, sink)
    assert stopped.status is SchedulerStatus.STOPPED
    assert stopped.tick(now="2026-09-19T10:00:00Z").action is TickAction.STOPPED
    with pytest.raises(ResearchSchedulerError, match="cannot change status"):
        stopped.resume()


def test_stop_after_pending_reservation_recovers_before_stop(tmp_path):
    path = tmp_path / "research-scheduler.json"
    sink = FakeTriggerSink(fail_after_accept_once=True)
    scheduler = ResearchScheduler.initialize_pristine(path, sink)
    scheduler.add_schedule(_schedule())

    with pytest.raises(RuntimeError):
        scheduler.tick(now="2026-09-19T10:00:00Z")
    scheduler.stop("operator STOP")

    recovered = ResearchScheduler(path, sink)
    result = recovered.tick(now="2026-09-19T11:00:00Z")

    assert result.action is TickAction.DELIVERED
    assert recovered.status is SchedulerStatus.STOPPED
    assert len(sink.accepted) == 1
    assert recovered.tick(now="2026-09-19T11:00:00Z").action is TickAction.STOPPED


def test_changed_schedule_authority_fails_closed(tmp_path):
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json", sink
    )
    scheduler.add_schedule(_schedule())
    changed = ResearchSchedule(
        schedule_id="drift-main",
        wake_source=WakeSource.SCHEDULED_QUESTION,
        first_fire_at="2026-09-19T10:00:00Z",
        interval_seconds=60,
        misfire_grace_seconds=30,
        question_id="question-1",
        question_record_sha256=SHA_Q,
        source_evidence_sha256="3" * 64,
        source_observed_at="2026-09-19T09:59:00Z",
        budget_units=3,
        deadline_offset_seconds=120,
    )
    with pytest.raises(ResearchSchedulerError, match="identity conflict"):
        scheduler.add_schedule(changed)


def test_state_tampering_fails_before_delivery(tmp_path):
    path = tmp_path / "research-scheduler.json"
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(path, sink)
    scheduler.add_schedule(_schedule())

    state = json.loads(path.read_text(encoding="utf-8"))
    state["status"] = "PAUSED"
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ResearchSchedulerError, match="digest mismatch"):
        ResearchScheduler(path, sink)
    assert not sink.calls


def test_schedule_rejects_future_evidence_and_naive_time():
    with pytest.raises(ResearchSchedulerError, match="cannot follow"):
        ResearchSchedule(
            schedule_id="bad",
            wake_source=WakeSource.SCHEDULED_QUESTION,
            first_fire_at="2026-09-19T10:00:00Z",
            interval_seconds=60,
            misfire_grace_seconds=30,
            question_id="q",
            question_record_sha256=SHA_Q,
            source_evidence_sha256=SHA_SOURCE,
            source_observed_at="2026-09-19T10:00:01Z",
            budget_units=1,
        )
    with pytest.raises(ResearchSchedulerError, match="timezone"):
        _schedule(first_fire_at="2026-09-19T10:00:00")


def _postmortem_bound_schedule(tmp_path, *, wake_source=WakeSource.POSTMORTEM_QUESTION):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    postmortem = Postmortem(
        postmortem_id="postmortem-1",
        experiment_id="experiment-1",
        classification=ResearchOutcome.NULL,
        finding="A frozen negative result needs one governed retest question.",
        retest_conditions=("new-data-window",),
        created_at="2026-09-19T09:58:00Z",
    )
    registry.append(postmortem)
    source = registry.get("Postmortem", "postmortem-1")
    assert source is not None
    registry.append(
        ResearchQuestion(
            question_id="question-postmortem-1",
            statement="Should this postmortem be retested under its frozen conditions?",
            source_sha256=source.record_sha256,
            created_at="2026-09-19T09:59:00Z",
        )
    )
    question = registry.get("ResearchQuestion", "question-postmortem-1")
    assert question is not None
    schedule = ResearchSchedule(
        schedule_id="postmortem-once",
        wake_source=wake_source,
        first_fire_at="2026-09-19T10:00:00Z",
        interval_seconds=60,
        misfire_grace_seconds=30,
        question_id=question.record_id,
        question_record_sha256=question.record_sha256,
        source_evidence_sha256=source.record_sha256,
        source_observed_at=source.available_at,
        budget_units=2,
        source_record_id=source.record_id,
        evidence_reuse_policy=EvidenceReusePolicy.SINGLE_CANONICAL_OCCURRENCE,
    )
    return registry, schedule


def test_typed_wake_is_bound_to_canonical_source_purpose_and_fires_once(tmp_path):
    registry, schedule = _postmortem_bound_schedule(tmp_path)
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        sink,
        source_registry=registry,
    )
    scheduler.add_schedule(schedule)

    first = scheduler.tick(now="2026-09-19T10:00:00Z")
    second = scheduler.tick(now="2026-09-19T10:01:00Z")

    assert first.action is TickAction.DELIVERED
    assert second.action is TickAction.IDLE
    assert len(sink.calls) == 1
    assert sink.calls[0].source_scope == (
        "research-scheduler:POSTMORTEM_QUESTION:postmortem-1:postmortem-once"
    )
    assert scheduler.snapshot()["schedules"]["postmortem-once"]["retired"] is True


def test_mislabeled_typed_wake_cannot_authorize_another_source_kind(tmp_path):
    registry, schedule = _postmortem_bound_schedule(
        tmp_path,
        wake_source=WakeSource.DRIFT_FINDING,
    )
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        FakeTriggerSink(),
        source_registry=registry,
    )

    with pytest.raises(ResearchSchedulerError, match="DRIFT_FINDING source record is missing"):
        scheduler.add_schedule(schedule)


def test_typed_wake_requires_registry_and_forbids_recurring_evidence_reuse(tmp_path):
    registry, schedule = _postmortem_bound_schedule(tmp_path)
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        FakeTriggerSink(),
    )
    with pytest.raises(
        ResearchSchedulerError,
        match="requires canonical ScientificRegistry authority",
    ):
        scheduler.add_schedule(schedule)

    with pytest.raises(
        ResearchSchedulerError,
        match="typed wake evidence cannot be reused",
    ):
        ResearchSchedule(
            schedule_id="bad-recurring-postmortem",
            wake_source=WakeSource.POSTMORTEM_QUESTION,
            first_fire_at="2026-09-19T10:00:00Z",
            interval_seconds=60,
            misfire_grace_seconds=30,
            question_id=schedule.question_id,
            question_record_sha256=schedule.question_record_sha256,
            source_evidence_sha256=schedule.source_evidence_sha256,
            source_observed_at=schedule.source_observed_at,
            budget_units=2,
            source_record_id=schedule.source_record_id,
            evidence_reuse_policy=EvidenceReusePolicy.IMMUTABLE_RECURRING,
        )


def test_bounded_run_loop_uses_fake_clock_without_busy_spin(tmp_path):
    sink = FakeTriggerSink()
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json", sink
    )
    scheduler.add_schedule(_schedule())
    sleeps = []
    times = iter(
        [
            datetime(2026, 9, 19, 9, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
        ]
    )

    ticks = scheduler.run(
        clock=lambda: next(times),
        sleep=sleeps.append,
        poll_seconds=2,
        max_ticks=2,
    )

    assert ticks == 2
    assert sleeps == [2.0]
    assert len(sink.calls) == 1
    
def test_interval_cadence_adapter_delegates_fire_calculation():
    from autosport.research_scheduler import IntervalCadenceAdapter

    IntervalCadenceAdapter.validate(
        first_fire_at="2026-09-19T10:00:00Z",
        interval_seconds=60,
    )
    next_fire = IntervalCadenceAdapter.next_fire(
        previous_fire_at="2026-09-19T10:00:00Z",
        first_fire_at="2026-09-19T10:00:00Z",
        interval_seconds=60,
    )
    assert next_fire.isoformat().replace("+00:00", "Z") == "2026-09-19T10:01:00Z"


def test_curriculum_wake_freezes_scheduled_cutoff_and_external_budget():
    from autosport.research_scheduler import NightResearchCurriculumWake
    from autosport.research_curriculum import CurriculumPurpose

    calls = []
    admissions = []

    class FakeCurriculum:
        def select_and_dispatch(self, candidates, **kwargs):
            calls.append((tuple(candidates), kwargs))
            return "receipt"

    wake = NightResearchCurriculumWake(
        FakeCurriculum(),
        candidate_loader=lambda as_of: [{"candidate_snapshot_as_of": as_of}],
        selector_policy_version="night-v1",
        seed=7,
        max_budget_units=5,
        admit_budget=admissions.append,
    )
    assert wake.dispatch(
        scheduled_for="2026-09-19T10:00:00+00:00",
        budget_units=3,
        deadline_at="2026-09-19T10:02:00Z",
    ) == "receipt"
    assert admissions == [3]
    assert calls[0][0] == ({"candidate_snapshot_as_of": "2026-09-19T10:00:00Z"},)
    assert calls[0][1] == {
        "purpose": CurriculumPurpose.CURRICULUM,
        "selector_policy_version": "night-v1",
        "as_of": "2026-09-19T10:00:00Z",
        "seed": 7,
        "budget_units": 3,
        "deadline_at": "2026-09-19T10:02:00Z",
    }


def test_curriculum_wake_budget_fails_closed_before_selection():
    from autosport.research_scheduler import NightResearchCurriculumWake

    calls = []

    class FakeCurriculum:
        def select_and_dispatch(self, *args, **kwargs):
            calls.append(1)
            return "receipt"

    wake = NightResearchCurriculumWake(
        FakeCurriculum(),
        candidate_loader=lambda as_of: [{"candidate_snapshot_as_of": as_of}],
        selector_policy_version="night-v1",
        seed=7,
        max_budget_units=2,
    )
    with pytest.raises(ResearchSchedulerError, match="budget exceeds external admission"):
        wake.dispatch(
            scheduled_for="2026-09-19T10:00:00Z",
            budget_units=3,
            deadline_at=None,
        )
    assert calls == []


def test_curriculum_wake_freezes_population_and_dispatches_once(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    candidates = (
        _candidate(episode_id="c-wake-1"),
        _candidate(episode_id="c-wake-2", priority=4),
    )
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        curriculum.trigger_adapter,
    )

    wake_id = scheduler.queue_curriculum_wake(
        curriculum,
        candidates,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=17,
        budget_units=2,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )
    result = scheduler.tick_curriculum(
        curriculum,
        candidates,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    assert result.action is TickAction.DELIVERED
    assert result.curriculum_wake_id == wake_id
    assert result.curriculum_selection_id
    assert len(supervisor.list_runs()) == 1
    assert scheduler.snapshot()["curriculum_wakes"][wake_id]["status"] == "ACCEPTED"

    reopened = ResearchScheduler(scheduler.path, curriculum.trigger_adapter)
    again = reopened.tick_curriculum(
        curriculum,
        candidates,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )
    assert again.action is TickAction.IDLE
    assert len(supervisor.list_runs()) == 1


def test_curriculum_wake_rejects_changed_population_before_dispatch(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    original = (_candidate(episode_id="c-wake-3"),)
    changed = (
        _candidate(
            episode_id="c-wake-3",
            reasons=("changed-population",),
        ),
    )
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        curriculum.trigger_adapter,
    )
    scheduler.queue_curriculum_wake(
        curriculum,
        original,
        purpose=__import__("autosport.research_curriculum", fromlist=["CurriculumPurpose"]).CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=18,
        budget_units=1,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    with pytest.raises(ResearchSchedulerError, match="population identity/cutoff evidence changed"):
        scheduler.tick_curriculum(
            curriculum,
            changed,
            max_concurrency=1,
            active_concurrency=0,
            remaining_budget_units=8,
        )
    assert not supervisor.list_runs()


def test_curriculum_wake_honors_external_admission_and_pending_recovery(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    candidate = (_candidate(episode_id="c-wake-4"),)
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        curriculum.trigger_adapter,
    )
    with pytest.raises(ResearchSchedulerError, match="concurrency admission"):
        scheduler.queue_curriculum_wake(
            curriculum,
            candidate,
            purpose=__import__("autosport.research_curriculum", fromlist=["CurriculumPurpose"]).CurriculumPurpose.CURRICULUM,
            selector_policy_version="night-v1",
            as_of="2026-09-19T03:20:00Z",
            seed=19,
            budget_units=1,
            max_concurrency=1,
            active_concurrency=1,
            remaining_budget_units=8,
        )

    scheduler.queue_curriculum_wake(
        curriculum,
        candidate,
        purpose=__import__("autosport.research_curriculum", fromlist=["CurriculumPurpose"]).CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=19,
        budget_units=1,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )
    blocked = scheduler.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=1,
        remaining_budget_units=8,
    )
    assert blocked.action is TickAction.ADMISSION_BLOCKED
    assert not supervisor.list_runs()
    delivered = scheduler.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )
    assert delivered.action is TickAction.DELIVERED
    assert len(supervisor.list_runs()) == 1

def test_curriculum_pending_wake_honors_scheduler_pause_before_dispatch(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    candidate = (_candidate(episode_id="c-wake-paused"),)
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        curriculum.trigger_adapter,
    )
    wake_id = scheduler.queue_curriculum_wake(
        curriculum,
        candidate,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=20,
        budget_units=1,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    scheduler.pause()
    blocked = scheduler.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    assert blocked.action is TickAction.PAUSED
    assert blocked.curriculum_wake_id == wake_id
    assert not supervisor.list_runs()
    assert scheduler.snapshot()["curriculum_wakes"][wake_id]["status"] == "PENDING"

    scheduler.resume()
    delivered = scheduler.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )
    assert delivered.action is TickAction.DELIVERED
    assert len(supervisor.list_runs()) == 1


def test_curriculum_pending_wake_honors_scheduler_stop_before_dispatch(tmp_path):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    candidate = (_candidate(episode_id="c-wake-stopped"),)
    path = tmp_path / "research-scheduler.json"
    scheduler = ResearchScheduler.initialize_pristine(path, curriculum.trigger_adapter)
    wake_id = scheduler.queue_curriculum_wake(
        curriculum,
        candidate,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=21,
        budget_units=1,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    scheduler.stop("operator STOP")
    blocked = scheduler.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    assert blocked.action is TickAction.STOPPED
    assert blocked.curriculum_wake_id == wake_id
    assert not supervisor.list_runs()
    assert scheduler.snapshot()["curriculum_wakes"][wake_id]["status"] == "PENDING"

    reopened = ResearchScheduler(path, curriculum.trigger_adapter)
    again = reopened.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )
    assert again.action is TickAction.STOPPED
    assert again.curriculum_wake_id == wake_id
    assert not supervisor.list_runs()


@pytest.mark.parametrize(
    ("transition", "expected_action"),
    (
        ("pause", TickAction.PAUSED),
        ("stop", TickAction.STOPPED),
    ),
)
def test_curriculum_wake_rechecks_status_at_dispatch_reservation(
    tmp_path,
    monkeypatch,
    transition,
    expected_action,
):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    candidate = (_candidate(episode_id=f"c-wake-race-{transition}"),)
    scheduler = ResearchScheduler.initialize_pristine(
        tmp_path / "research-scheduler.json",
        curriculum.trigger_adapter,
    )
    wake_id = scheduler.queue_curriculum_wake(
        curriculum,
        candidate,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=22,
        budget_units=1,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    curriculum_type = type(curriculum)
    original = curriculum_type.select_and_dispatch
    transitioned = False

    def transition_then_dispatch(self, *args, **kwargs):
        nonlocal transitioned
        if not transitioned:
            transitioned = True
            if transition == "pause":
                scheduler.pause()
            else:
                scheduler.stop("operator STOP during pre-dispatch race")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        curriculum_type,
        "select_and_dispatch",
        transition_then_dispatch,
    )

    blocked = scheduler.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    assert blocked.action is expected_action
    assert blocked.curriculum_wake_id == wake_id
    assert scheduler.snapshot()["curriculum_wakes"][wake_id]["status"] == "PENDING"
    assert curriculum.snapshot()["dispatches"] == {}
    assert not supervisor.list_runs()


def test_curriculum_dispatching_marker_recovers_after_crash_then_stop(
    tmp_path,
    monkeypatch,
):
    _, supervisor, curriculum = _workspace(tmp_path, max_budget_units=8)
    candidate = (_candidate(episode_id="c-wake-dispatching-recovery"),)
    path = tmp_path / "research-scheduler.json"
    scheduler = ResearchScheduler.initialize_pristine(path, curriculum.trigger_adapter)
    wake_id = scheduler.queue_curriculum_wake(
        curriculum,
        candidate,
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="night-v1",
        as_of="2026-09-19T03:20:00Z",
        seed=23,
        budget_units=1,
        max_concurrency=1,
        active_concurrency=0,
        remaining_budget_units=8,
    )

    original_begin = scheduler._begin_curriculum_dispatch_locked

    def crash_after_begin(reserved_wake_id, selection_id):
        original_begin(reserved_wake_id, selection_id)
        raise RuntimeError("simulated crash after scheduler dispatch linearization")

    monkeypatch.setattr(
        scheduler,
        "_begin_curriculum_dispatch_locked",
        crash_after_begin,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        scheduler.tick_curriculum(
            curriculum,
            candidate,
            max_concurrency=1,
            active_concurrency=0,
            remaining_budget_units=8,
        )

    interrupted = ResearchScheduler(path, curriculum.trigger_adapter)
    raw = interrupted.snapshot()["curriculum_wakes"][wake_id]
    assert raw["status"] == "DISPATCHING"
    assert raw["selection_id"] is not None
    assert curriculum.snapshot()["dispatches"] == {}
    assert not supervisor.list_runs()

    interrupted.stop("operator STOP after dispatch linearization")
    recovered = ResearchScheduler(path, curriculum.trigger_adapter)
    result = recovered.tick_curriculum(
        curriculum,
        candidate,
        max_concurrency=1,
        active_concurrency=1,
        remaining_budget_units=0,
    )

    assert result.action is TickAction.DELIVERED
    accepted = recovered.snapshot()["curriculum_wakes"][wake_id]
    assert accepted["status"] == "ACCEPTED"
    assert accepted["selection_id"] == result.curriculum_selection_id
    assert len(supervisor.list_runs()) == 1
    assert recovered.status is SchedulerStatus.STOPPED

