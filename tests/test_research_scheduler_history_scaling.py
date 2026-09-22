from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import autosport.research_scheduler as research_scheduler
from autosport.research_scheduler import (
    ResearchSchedule,
    ResearchScheduler,
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
