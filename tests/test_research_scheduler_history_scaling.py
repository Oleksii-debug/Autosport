from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import autosport.research_scheduler as research_scheduler
from autosport.research_curriculum import CurriculumPurpose
from autosport.research_scheduler import (
    ResearchSchedule,
    ResearchScheduler,
    ResearchSchedulerError,
    WakeSource,
)
from autosport.research_trigger_adapter import ResearchTriggerReceipt


_FIRST = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)
_SMALL_HISTORY = 4
_LARGE_HISTORY = 128
_CONSTANT_SLACK = 8


class _UnusedSink:
    def accept(self, event):
        raise AssertionError("pause/status mutation must not deliver a research event")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _schedule() -> ResearchSchedule:
    return ResearchSchedule(
        schedule_id="long-run-research",
        wake_source=WakeSource.SCHEDULED_QUESTION,
        first_fire_at=_FIRST.isoformat().replace("+00:00", "Z"),
        interval_seconds=60,
        misfire_grace_seconds=30,
        question_id="question-long-run",
        question_record_sha256="1" * 64,
        source_evidence_sha256="2" * 64,
        source_observed_at=(
            _FIRST - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z"),
        budget_units=1,
        deadline_offset_seconds=120,
    )


def _accepted_occurrence(
    schedule: ResearchSchedule,
    *,
    index: int,
) -> tuple[str, dict[str, object]]:
    scheduled = _FIRST + timedelta(minutes=index)
    scheduled_for = scheduled.isoformat().replace("+00:00", "Z")
    event = schedule.event_for(scheduled_for)
    trigger = event.to_research_trigger()
    receipt = ResearchTriggerReceipt(
        source_event_identity_sha256=event.source_event_identity_sha256,
        source_event_sha256=event.source_event_sha256,
        supervisor_trigger_id=trigger.trigger_id,
        supervisor_trigger_sha256=trigger.trigger_sha256,
        run_id=trigger.run_id,
        checkpoint_sha256=_sha(f"checkpoint:{index}:{trigger.run_id}"),
    )
    occurrence_id = ResearchScheduler._occurrence_id(
        schedule.schedule_id,
        scheduled_for,
    )
    return occurrence_id, {
        "occurrence_id": occurrence_id,
        "schedule_id": schedule.schedule_id,
        "scheduled_for": scheduled_for,
        "status": "ACCEPTED",
        "event": event.canonical_payload(),
        "event_sha256": event.source_event_sha256,
        "receipt": {
            **receipt.canonical_payload(),
            "receipt_sha256": receipt.receipt_sha256,
        },
        "skip_reason": None,
    }


def _scheduler_with_history(root: Path, history_size: int) -> ResearchScheduler:
    schedule = _schedule()
    occurrences = dict(
        _accepted_occurrence(schedule, index=index)
        for index in range(history_size)
    )
    next_fire = (_FIRST + timedelta(minutes=history_size)).isoformat().replace(
        "+00:00",
        "Z",
    )
    body: dict[str, object] = {
        "schema": research_scheduler.SCHEMA,
        "schema_version": research_scheduler.SCHEMA_VERSION,
        "status": "ACTIVE",
        "state_version": history_size,
        "stop_reason": None,
        "schedules": {
            schedule.schedule_id: {
                "schedule": schedule.payload(),
                "schedule_sha256": schedule.schedule_sha256,
                "next_fire_at": next_fire,
                "last_skip": None,
                "retired": False,
            }
        },
        "occurrences": occurrences,
        "curriculum_wakes": {},
    }
    path = root / "research-scheduler.json"
    path.write_text(
        json.dumps(
            {**body, "state_sha256": research_scheduler._digest(body)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return ResearchScheduler(path, _UnusedSink())


def _accepted_curriculum_wake(index: int) -> tuple[str, dict[str, object]]:
    as_of = (_FIRST + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")
    wake_id = _sha(f"curriculum-wake:{index}")
    candidate_id = _sha(f"candidate:{index}")
    return wake_id, {
        "wake_id": wake_id,
        "status": "ACCEPTED",
        "selector_policy_version": "night-v1",
        "purpose": CurriculumPurpose.CURRICULUM.value,
        "candidate_ids": [candidate_id],
        "candidate_population_sha256": _sha(f"population:{candidate_id}"),
        "as_of": as_of,
        "seed": index,
        "budget_units": 1,
        "deadline_at": None,
        "selection_id": _sha(f"selection:{index}"),
        "run_id": _sha(f"run:{index}"),
        "receipt_sha256": _sha(f"receipt:{index}"),
    }


def _scheduler_with_curriculum_history(
    root: Path,
    history_size: int,
) -> ResearchScheduler:
    wakes = dict(_accepted_curriculum_wake(index) for index in range(history_size))
    body: dict[str, object] = {
        "schema": research_scheduler.SCHEMA,
        "schema_version": research_scheduler.SCHEMA_VERSION,
        "status": "ACTIVE",
        "state_version": history_size,
        "stop_reason": None,
        "schedules": {},
        "occurrences": {},
        "curriculum_wakes": wakes,
    }
    path = root / "research-scheduler.json"
    path.write_text(
        json.dumps(
            {**body, "state_sha256": research_scheduler._digest(body)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return ResearchScheduler(path, _UnusedSink())


def _occurrence_validations_for_pause(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_history(Path(directory), history_size)
        original = ResearchScheduler._validate_occurrence
        count = 0

        def counting_validate(self, occurrence_id, raw, schedules):
            nonlocal count
            count += 1
            return original(self, occurrence_id, raw, schedules)

        with patch.object(
            ResearchScheduler,
            "_validate_occurrence",
            counting_validate,
        ):
            scheduler.pause()
        return count


def _occurrences_rewritten_by_pause(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_history(Path(directory), history_size)
        original_write = research_scheduler.atomic_write_json
        rewritten: list[int] = []

        def recording_write(path: Path, payload: object) -> None:
            if isinstance(payload, dict):
                occurrences = payload.get("occurrences")
                rewritten.append(
                    len(occurrences) if isinstance(occurrences, dict) else 0
                )
            original_write(path, payload)

        with patch.object(
            research_scheduler,
            "atomic_write_json",
            recording_write,
        ):
            scheduler.pause()

        assert rewritten, "scheduler operational-state write was not observed"
        return max(rewritten)


def _curriculum_validations_for_pause(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_curriculum_history(
            Path(directory),
            history_size,
        )
        original = ResearchScheduler._validate_curriculum_wake
        count = 0

        def counting_validate(wake_id, raw):
            nonlocal count
            count += 1
            return original(wake_id, raw)

        with patch.object(
            ResearchScheduler,
            "_validate_curriculum_wake",
            staticmethod(counting_validate),
        ):
            scheduler.pause()
        return count


def _curriculum_wakes_rewritten_by_pause(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_curriculum_history(
            Path(directory),
            history_size,
        )
        original_write = research_scheduler.atomic_write_json
        rewritten: list[int] = []

        def recording_write(path: Path, payload: object) -> None:
            if isinstance(payload, dict):
                wakes = payload.get("curriculum_wakes")
                rewritten.append(len(wakes) if isinstance(wakes, dict) else 0)
            original_write(path, payload)

        with patch.object(
            research_scheduler,
            "atomic_write_json",
            recording_write,
        ):
            scheduler.pause()

        assert rewritten, "scheduler operational-state write was not observed"
        return max(rewritten)


def test_pause_validation_work_is_bounded_by_active_state_not_occurrence_history() -> None:
    small = _occurrence_validations_for_pause(_SMALL_HISTORY)
    large = _occurrence_validations_for_pause(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "PAUSE revalidated work proportional to historical scheduler occurrences: "
        f"small={small}, large={large}"
    )


def test_pause_does_not_rewrite_full_completed_occurrence_history() -> None:
    small = _occurrences_rewritten_by_pause(_SMALL_HISTORY)
    large = _occurrences_rewritten_by_pause(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "PAUSE rewrote the complete historical occurrence population instead of "
        f"bounded operational state: small={small}, large={large}"
    )


def test_pause_validation_work_is_bounded_by_active_state_not_curriculum_history() -> None:
    small = _curriculum_validations_for_pause(_SMALL_HISTORY)
    large = _curriculum_validations_for_pause(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "PAUSE revalidated work proportional to accepted curriculum history: "
        f"small={small}, large={large}"
    )


def test_pause_does_not_rewrite_full_accepted_curriculum_history() -> None:
    small = _curriculum_wakes_rewritten_by_pause(_SMALL_HISTORY)
    large = _curriculum_wakes_rewritten_by_pause(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "PAUSE rewrote the complete accepted curriculum population instead of "
        f"bounded operational state: small={small}, large={large}"
    )


def test_completed_history_is_cold_but_remains_visible_after_restart() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_history(Path(directory), _SMALL_HISTORY)
        hot = json.loads(scheduler.path.read_text(encoding="utf-8"))

        assert hot["occurrences"] == {}
        assert hot["cold_history_count"] == _SMALL_HISTORY
        assert len(scheduler.snapshot()["occurrences"]) == _SMALL_HISTORY

        reopened = ResearchScheduler(scheduler.path, _UnusedSink())
        reopened_hot = json.loads(reopened.path.read_text(encoding="utf-8"))
        assert reopened_hot["occurrences"] == {}
        assert len(reopened.snapshot()["occurrences"]) == _SMALL_HISTORY


def test_self_consistent_history_truncation_fails_closed_against_state_anchor() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_history(Path(directory), _SMALL_HISTORY)
        connection = sqlite3.connect(scheduler._cold_history_path)
        try:
            connection.execute(
                "DELETE FROM cold_history WHERE sequence = "
                "(SELECT MAX(sequence) FROM cold_history)"
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(ResearchSchedulerError, match="truncated"):
            ResearchScheduler(scheduler.path, _UnusedSink())




def test_interior_cold_history_gap_fails_closed_on_hot_state_validation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_curriculum_history(
            Path(directory),
            _SMALL_HISTORY,
        )
        connection = sqlite3.connect(scheduler._cold_history_path)
        try:
            connection.execute(
                "DELETE FROM cold_history WHERE sequence = 2"
            )
            connection.commit()
        finally:
            connection.close()

        # A missing archived ACCEPTED wake must be detected before any ordinary
        # hot-state mutation can proceed. Otherwise indexed history lookup could
        # treat that wake as absent and permit resurrection before restart.
        with pytest.raises(ResearchSchedulerError, match="cardinality"):
            scheduler.pause()


def test_db_ahead_of_state_recovers_only_matching_completed_hot_suffix() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_history(Path(directory), _SMALL_HISTORY)
        schedule = _schedule()
        occurrence_id, occurrence = _accepted_occurrence(
            schedule,
            index=_SMALL_HISTORY,
        )

        persisted = json.loads(scheduler.path.read_text(encoding="utf-8"))
        persisted["occurrences"][occurrence_id] = occurrence
        persisted["state_version"] += 1
        body = {
            key: value
            for key, value in persisted.items()
            if key != "state_sha256"
        }
        persisted["state_sha256"] = research_scheduler._digest(body)
        scheduler.path.write_text(
            json.dumps(persisted, sort_keys=True),
            encoding="utf-8",
        )

        interrupted_state = scheduler._read()
        scheduler._append_cold_history_locked(
            interrupted_state,
            [
                (
                    research_scheduler._COLD_HISTORY_OCCURRENCE,
                    occurrence_id,
                    occurrence,
                )
            ],
        )
        on_disk_before_restart = json.loads(
            scheduler.path.read_text(encoding="utf-8")
        )
        assert on_disk_before_restart["cold_history_count"] == _SMALL_HISTORY
        assert occurrence_id in on_disk_before_restart["occurrences"]

        recovered = ResearchScheduler(scheduler.path, _UnusedSink())
        hot = json.loads(recovered.path.read_text(encoding="utf-8"))
        assert hot["cold_history_count"] == _SMALL_HISTORY + 1
        assert hot["occurrences"] == {}
        snapshot = recovered.snapshot()
        assert len(snapshot["occurrences"]) == _SMALL_HISTORY + 1
        assert snapshot["occurrences"][occurrence_id] == occurrence


def test_db_ahead_of_state_without_matching_hot_record_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_history(Path(directory), _SMALL_HISTORY)
        schedule = _schedule()
        occurrence_id, occurrence = _accepted_occurrence(
            schedule,
            index=_SMALL_HISTORY,
        )
        interrupted_state = scheduler._read()
        scheduler._append_cold_history_locked(
            interrupted_state,
            [
                (
                    research_scheduler._COLD_HISTORY_OCCURRENCE,
                    occurrence_id,
                    occurrence,
                )
            ],
        )

        on_disk = json.loads(scheduler.path.read_text(encoding="utf-8"))
        assert on_disk["cold_history_count"] == _SMALL_HISTORY
        assert occurrence_id not in on_disk["occurrences"]

        with pytest.raises(
            ResearchSchedulerError,
            match="unanchored cold history cannot be recovered",
        ):
            ResearchScheduler(scheduler.path, _UnusedSink())


def test_accepted_curriculum_history_is_cold_but_snapshot_visible() -> None:
    with tempfile.TemporaryDirectory() as directory:
        scheduler = _scheduler_with_curriculum_history(
            Path(directory),
            _SMALL_HISTORY,
        )
        hot = json.loads(scheduler.path.read_text(encoding="utf-8"))

        assert hot["curriculum_wakes"] == {}
        assert hot["cold_history_count"] == _SMALL_HISTORY
        snapshot = scheduler.snapshot()
        assert len(snapshot["curriculum_wakes"]) == _SMALL_HISTORY
        assert {
            raw["status"] for raw in snapshot["curriculum_wakes"].values()
        } == {"ACCEPTED"}

        reopened = ResearchScheduler(scheduler.path, _UnusedSink())
        reopened_hot = json.loads(reopened.path.read_text(encoding="utf-8"))
        assert reopened_hot["curriculum_wakes"] == {}
        assert len(reopened.snapshot()["curriculum_wakes"]) == _SMALL_HISTORY
