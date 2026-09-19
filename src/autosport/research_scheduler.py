"""Restart-safe bounded scheduler for governed research triggers.

This module owns only durable wakeup timing, immutable occurrence reservation,
misfire collapse, and pause/STOP state. ResearchTriggerAdapter and
ResearchSupervisor remain the sole authorities for causal validation and run
identity. NightResearchCurriculum remains the sole night/idle selection authority.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol

from .integrity import atomic_write_json
from .research_trigger_adapter import (
    ExternalResearchTrigger,
    ResearchTriggerReceipt,
    ResearchTriggerSource,
)
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.research_scheduler"
SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class ResearchSchedulerError(RuntimeError):
    """Scheduler state or immutable schedule evidence is invalid."""


class SchedulerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class TickAction(StrEnum):
    IDLE = "IDLE"
    DELIVERED = "DELIVERED"
    SKIPPED = "SKIPPED"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class WakeSource(StrEnum):
    DRIFT_FINDING = "DRIFT_FINDING"
    POSTMORTEM_QUESTION = "POSTMORTEM_QUESTION"
    FORWARD_EVALUATION = "FORWARD_EVALUATION"
    SCHEDULED_QUESTION = "SCHEDULED_QUESTION"


class TriggerSink(Protocol):
    def accept(self, event: ExternalResearchTrigger) -> ResearchTriggerReceipt: ...


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ResearchSchedulerError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _instant(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, name).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchSchedulerError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchSchedulerError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if text != text.lower() or len(text) != 64 or any(c not in _HEX for c in text):
        raise ResearchSchedulerError(f"{name} must be lowercase SHA-256")
    return text


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResearchSchedulerError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResearchSchedulerError(f"{name} must be a non-negative integer")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchSchedule:
    schedule_id: str
    wake_source: WakeSource
    first_fire_at: str
    interval_seconds: int
    misfire_grace_seconds: int
    question_id: str
    question_record_sha256: str
    source_evidence_sha256: str
    source_observed_at: str
    budget_units: int
    deadline_offset_seconds: int | None = None

    def __post_init__(self) -> None:
        _text(self.schedule_id, "schedule_id")
        if not isinstance(self.wake_source, WakeSource):
            raise ResearchSchedulerError("wake_source must be WakeSource")
        first = _instant(self.first_fire_at, "first_fire_at")
        observed = _instant(self.source_observed_at, "source_observed_at")
        if observed > first:
            raise ResearchSchedulerError("source_observed_at cannot follow first_fire_at")
        _positive_int(self.interval_seconds, "interval_seconds")
        _nonnegative_int(self.misfire_grace_seconds, "misfire_grace_seconds")
        _text(self.question_id, "question_id")
        _sha(self.question_record_sha256, "question_record_sha256")
        _sha(self.source_evidence_sha256, "source_evidence_sha256")
        _positive_int(self.budget_units, "budget_units")
        if self.deadline_offset_seconds is not None:
            _nonnegative_int(self.deadline_offset_seconds, "deadline_offset_seconds")

    def payload(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "wake_source": self.wake_source.value,
            "first_fire_at": _timestamp(self.first_fire_at, "first_fire_at"),
            "interval_seconds": self.interval_seconds,
            "misfire_grace_seconds": self.misfire_grace_seconds,
            "question_id": self.question_id,
            "question_record_sha256": self.question_record_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_observed_at": _timestamp(
                self.source_observed_at, "source_observed_at"
            ),
            "budget_units": self.budget_units,
            "deadline_offset_seconds": self.deadline_offset_seconds,
        }

    @property
    def schedule_sha256(self) -> str:
        return _digest(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "schedule": self.payload(),
            }
        )

    @classmethod
    def from_payload(cls, raw: object) -> "ResearchSchedule":
        if type(raw) is not dict:
            raise ResearchSchedulerError("schedule payload must be an object")
        expected = {
            "schedule_id",
            "wake_source",
            "first_fire_at",
            "interval_seconds",
            "misfire_grace_seconds",
            "question_id",
            "question_record_sha256",
            "source_evidence_sha256",
            "source_observed_at",
            "budget_units",
            "deadline_offset_seconds",
        }
        if set(raw) != expected:
            raise ResearchSchedulerError("schedule payload fields mismatch")
        try:
            wake_source = WakeSource(raw["wake_source"])
        except (TypeError, ValueError) as exc:
            raise ResearchSchedulerError("schedule wake_source is invalid") from exc
        return cls(
            schedule_id=raw["schedule_id"],
            wake_source=wake_source,
            first_fire_at=raw["first_fire_at"],
            interval_seconds=raw["interval_seconds"],
            misfire_grace_seconds=raw["misfire_grace_seconds"],
            question_id=raw["question_id"],
            question_record_sha256=raw["question_record_sha256"],
            source_evidence_sha256=raw["source_evidence_sha256"],
            source_observed_at=raw["source_observed_at"],
            budget_units=raw["budget_units"],
            deadline_offset_seconds=raw["deadline_offset_seconds"],
        )

    def event_for(self, scheduled_for: str) -> ExternalResearchTrigger:
        scheduled = _instant(scheduled_for, "scheduled_for")
        first = _instant(self.first_fire_at, "first_fire_at")
        if scheduled < first:
            raise ResearchSchedulerError("scheduled_for precedes first_fire_at")
        elapsed = int((scheduled - first).total_seconds())
        if elapsed % self.interval_seconds:
            raise ResearchSchedulerError("scheduled_for is off schedule cadence")
        occurrence_key = _digest(
            {
                "schedule_id": self.schedule_id,
                "scheduled_for": _timestamp(scheduled_for, "scheduled_for"),
            }
        )
        deadline = None
        if self.deadline_offset_seconds is not None:
            deadline = (
                scheduled + timedelta(seconds=self.deadline_offset_seconds)
            ).isoformat().replace("+00:00", "Z")
        return ExternalResearchTrigger(
            source_kind=ResearchTriggerSource.SCHEDULE,
            source_scope=(
                f"research-scheduler:{self.wake_source.value}:{self.schedule_id}"
            ),
            source_event_id=f"occurrence:{occurrence_key}",
            question_id=self.question_id,
            question_record_sha256=self.question_record_sha256,
            source_evidence_sha256=self.source_evidence_sha256,
            source_observed_at=self.source_observed_at,
            requested_at=_timestamp(scheduled_for, "scheduled_for"),
            budget_units=self.budget_units,
            deadline_at=deadline,
        )


@dataclass(frozen=True, slots=True)
class TickResult:
    action: TickAction
    schedule_id: str | None = None
    occurrence_id: str | None = None
    skipped_count: int = 0
    receipt: ResearchTriggerReceipt | None = None


class ResearchScheduler:
    """Durable one-occurrence-at-a-time direct research wakeup runtime."""

    def __init__(self, path: str | Path, trigger_sink: TriggerSink) -> None:
        self.path = Path(path)
        if not callable(getattr(trigger_sink, "accept", None)):
            raise TypeError("trigger_sink must expose accept(event)")
        self.trigger_sink = trigger_sink
        try:
            self._validate(self._read())
        except FileNotFoundError as exc:
            raise ResearchSchedulerError("research scheduler state is missing") from exc

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        trigger_sink: TriggerSink,
    ) -> "ResearchScheduler":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                body = {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "status": SchedulerStatus.ACTIVE.value,
                    "state_version": 0,
                    "stop_reason": None,
                    "schedules": {},
                    "occurrences": {},
                }
                atomic_write_json(target, {**body, "state_sha256": _digest(body)})
        return cls(target, trigger_sink)

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            state = json.loads(raw)
        except FileNotFoundError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchSchedulerError("research scheduler state is unreadable") from exc
        if type(state) is not dict:
            raise ResearchSchedulerError("research scheduler state must be an object")
        return state

    def _write(self, state: dict[str, Any]) -> None:
        body = {key: value for key, value in state.items() if key != "state_sha256"}
        atomic_write_json(self.path, {**body, "state_sha256": _digest(body)})

    @staticmethod
    def _event_from_payload(raw: object) -> ExternalResearchTrigger:
        if type(raw) is not dict:
            raise ResearchSchedulerError("occurrence event must be an object")
        expected = {
            "schema",
            "schema_version",
            "source_kind",
            "source_scope",
            "source_event_id",
            "question_id",
            "question_record_sha256",
            "source_evidence_sha256",
            "source_observed_at",
            "requested_at",
            "budget_units",
            "deadline_at",
        }
        if set(raw) != expected:
            raise ResearchSchedulerError("occurrence event fields mismatch")
        try:
            source_kind = ResearchTriggerSource(raw["source_kind"])
        except (TypeError, ValueError) as exc:
            raise ResearchSchedulerError("occurrence source_kind is invalid") from exc
        if source_kind is not ResearchTriggerSource.SCHEDULE:
            raise ResearchSchedulerError("scheduler occurrence must be SCHEDULE source")
        return ExternalResearchTrigger(
            source_kind=source_kind,
            source_scope=raw["source_scope"],
            source_event_id=raw["source_event_id"],
            question_id=raw["question_id"],
            question_record_sha256=raw["question_record_sha256"],
            source_evidence_sha256=raw["source_evidence_sha256"],
            source_observed_at=raw["source_observed_at"],
            requested_at=raw["requested_at"],
            budget_units=raw["budget_units"],
            deadline_at=raw["deadline_at"],
        )

    @staticmethod
    def _occurrence_id(schedule_id: str, scheduled_for: str) -> str:
        return _digest(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "schedule_id": schedule_id,
                "scheduled_for": _timestamp(scheduled_for, "scheduled_for"),
            }
        )

    def _validate(self, state: object) -> None:
        if type(state) is not dict:
            raise ResearchSchedulerError("research scheduler state must be an object")
        required = {
            "schema",
            "schema_version",
            "status",
            "state_version",
            "stop_reason",
            "schedules",
            "occurrences",
            "state_sha256",
        }
        if set(state) != required:
            raise ResearchSchedulerError("research scheduler state fields mismatch")
        if state["schema"] != SCHEMA or state["schema_version"] != SCHEMA_VERSION:
            raise ResearchSchedulerError("research scheduler schema mismatch")
        try:
            status = SchedulerStatus(state["status"])
        except (TypeError, ValueError) as exc:
            raise ResearchSchedulerError("research scheduler status is invalid") from exc
        _nonnegative_int(state["state_version"], "state_version")
        if status is SchedulerStatus.STOPPED:
            _text(state["stop_reason"], "stop_reason")
        elif state["stop_reason"] is not None:
            raise ResearchSchedulerError("only STOPPED scheduler may carry stop_reason")
        if type(state["schedules"]) is not dict or type(state["occurrences"]) is not dict:
            raise ResearchSchedulerError("research scheduler collections are invalid")
        body = {key: value for key, value in state.items() if key != "state_sha256"}
        if _sha(state["state_sha256"], "state_sha256") != _digest(body):
            raise ResearchSchedulerError("research scheduler state digest mismatch")

        for schedule_id, raw in state["schedules"].items():
            _text(schedule_id, "schedule key")
            if type(raw) is not dict or set(raw) != {
                "schedule",
                "schedule_sha256",
                "next_fire_at",
                "last_skip",
            }:
                raise ResearchSchedulerError("schedule state fields mismatch")
            schedule = ResearchSchedule.from_payload(raw["schedule"])
            if schedule.schedule_id != schedule_id:
                raise ResearchSchedulerError("schedule key identity mismatch")
            if _sha(raw["schedule_sha256"], "schedule_sha256") != schedule.schedule_sha256:
                raise ResearchSchedulerError("schedule digest mismatch")
            next_fire = _instant(raw["next_fire_at"], "next_fire_at")
            first = _instant(schedule.first_fire_at, "first_fire_at")
            if next_fire < first:
                raise ResearchSchedulerError("next_fire_at precedes first_fire_at")
            if int((next_fire - first).total_seconds()) % schedule.interval_seconds:
                raise ResearchSchedulerError("next_fire_at is off schedule cadence")
            skip = raw["last_skip"]
            if skip is not None:
                if type(skip) is not dict or set(skip) != {"count", "through", "reason"}:
                    raise ResearchSchedulerError("last_skip is invalid")
                _positive_int(skip["count"], "last_skip.count")
                _timestamp(skip["through"], "last_skip.through")
                _text(skip["reason"], "last_skip.reason")

        for occurrence_id, raw in state["occurrences"].items():
            self._validate_occurrence(occurrence_id, raw, state["schedules"])

    def _validate_occurrence(
        self,
        occurrence_id: object,
        raw: object,
        schedules: dict[str, Any],
    ) -> None:
        _sha(occurrence_id, "occurrence_id")
        if type(raw) is not dict or set(raw) != {
            "occurrence_id",
            "schedule_id",
            "scheduled_for",
            "status",
            "event",
            "event_sha256",
            "receipt",
            "skip_reason",
        }:
            raise ResearchSchedulerError("occurrence fields mismatch")
        if raw["occurrence_id"] != occurrence_id:
            raise ResearchSchedulerError("occurrence identity mismatch")
        schedule_id = _text(raw["schedule_id"], "occurrence.schedule_id")
        if schedule_id not in schedules:
            raise ResearchSchedulerError("occurrence references missing schedule")
        _timestamp(raw["scheduled_for"], "occurrence.scheduled_for")
        if raw["status"] == "SKIPPED":
            if (
                raw["event"] is not None
                or raw["event_sha256"] is not None
                or raw["receipt"] is not None
            ):
                raise ResearchSchedulerError("skipped occurrence carries delivery evidence")
            _text(raw["skip_reason"], "skip_reason")
            return
        if raw["status"] not in {"PENDING", "ACCEPTED"}:
            raise ResearchSchedulerError("occurrence status is invalid")
        if raw["skip_reason"] is not None:
            raise ResearchSchedulerError("deliverable occurrence carries skip reason")
        event = self._event_from_payload(raw["event"])
        if event.source_event_sha256 != _sha(raw["event_sha256"], "event_sha256"):
            raise ResearchSchedulerError("occurrence event digest mismatch")
        if self._occurrence_id(schedule_id, raw["scheduled_for"]) != occurrence_id:
            raise ResearchSchedulerError("occurrence id mismatch")
        if raw["status"] == "PENDING":
            if raw["receipt"] is not None:
                raise ResearchSchedulerError("pending occurrence carries receipt")
            return
        receipt = raw["receipt"]
        if type(receipt) is not dict or set(receipt) != {
            "schema",
            "schema_version",
            "source_event_identity_sha256",
            "source_event_sha256",
            "supervisor_trigger_id",
            "supervisor_trigger_sha256",
            "run_id",
            "checkpoint_sha256",
            "receipt_sha256",
        }:
            raise ResearchSchedulerError("accepted occurrence receipt fields mismatch")
        if (
            receipt["schema"] != "autosport.research_trigger_adapter"
            or receipt["schema_version"] != 1
        ):
            raise ResearchSchedulerError("accepted occurrence receipt schema mismatch")
        for name in (
            "source_event_identity_sha256",
            "source_event_sha256",
            "supervisor_trigger_sha256",
            "run_id",
            "checkpoint_sha256",
            "receipt_sha256",
        ):
            _sha(receipt[name], f"receipt.{name}")
        _text(receipt["supervisor_trigger_id"], "receipt.supervisor_trigger_id")
        if receipt["source_event_sha256"] != event.source_event_sha256:
            raise ResearchSchedulerError("receipt/event identity mismatch")

    def add_schedule(self, schedule: ResearchSchedule) -> None:
        if not isinstance(schedule, ResearchSchedule):
            raise TypeError("schedule must be ResearchSchedule")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            if SchedulerStatus(state["status"]) is SchedulerStatus.STOPPED:
                raise ResearchSchedulerError("stopped scheduler cannot add schedules")
            entry = {
                "schedule": schedule.payload(),
                "schedule_sha256": schedule.schedule_sha256,
                "next_fire_at": _timestamp(schedule.first_fire_at, "first_fire_at"),
                "last_skip": None,
            }
            prior = state["schedules"].get(schedule.schedule_id)
            if prior is not None:
                if prior["schedule_sha256"] != schedule.schedule_sha256:
                    raise ResearchSchedulerError("schedule identity conflict")
                return
            state["schedules"][schedule.schedule_id] = entry
            state["schedules"] = dict(sorted(state["schedules"].items()))
            state["state_version"] += 1
            self._write(state)

    @property
    def status(self) -> SchedulerStatus:
        state = self._read()
        self._validate(state)
        return SchedulerStatus(state["status"])

    def pause(self) -> None:
        self._set_status(SchedulerStatus.PAUSED)

    def resume(self) -> None:
        self._set_status(SchedulerStatus.ACTIVE)

    def stop(self, reason: str) -> None:
        _text(reason, "stop reason")
        self._set_status(SchedulerStatus.STOPPED, reason=reason)

    def _set_status(
        self,
        target: SchedulerStatus,
        *,
        reason: str | None = None,
    ) -> None:
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            if SchedulerStatus(state["status"]) is SchedulerStatus.STOPPED:
                raise ResearchSchedulerError("stopped scheduler cannot change status")
            if target is SchedulerStatus.STOPPED:
                _text(reason, "stop reason")
            elif reason is not None:
                raise ResearchSchedulerError("reason is only valid for STOPPED")
            state["status"] = target.value
            state["stop_reason"] = reason
            state["state_version"] += 1
            self._write(state)

    def snapshot(self) -> dict[str, Any]:
        state = self._read()
        self._validate(state)
        return json.loads(_canonical_json(state))

    @staticmethod
    def _oldest_pending(
        state: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        pending = [
            (occurrence_id, raw)
            for occurrence_id, raw in state["occurrences"].items()
            if raw["status"] == "PENDING"
        ]
        if not pending:
            return None
        return min(
            pending,
            key=lambda item: (
                item[1]["scheduled_for"],
                item[1]["schedule_id"],
                item[0],
            ),
        )

    def _reserve_due(
        self,
        now: str,
    ) -> TickResult | tuple[str, ExternalResearchTrigger]:
        now_dt = _instant(now, "now")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            pending = self._oldest_pending(state)
            if pending is not None:
                occurrence_id, raw = pending
                return occurrence_id, self._event_from_payload(raw["event"])

            status = SchedulerStatus(state["status"])
            if status is SchedulerStatus.PAUSED:
                return TickResult(TickAction.PAUSED)
            if status is SchedulerStatus.STOPPED:
                return TickResult(TickAction.STOPPED)

            due: list[tuple[datetime, str, dict[str, Any]]] = []
            for schedule_id, entry in state["schedules"].items():
                fire = _instant(entry["next_fire_at"], "next_fire_at")
                if fire <= now_dt:
                    due.append((fire, schedule_id, entry))
            if not due:
                return TickResult(TickAction.IDLE)

            _, schedule_id, entry = min(due, key=lambda item: (item[0], item[1]))
            schedule = ResearchSchedule.from_payload(entry["schedule"])
            first_due = _instant(entry["next_fire_at"], "next_fire_at")
            elapsed_seconds = int((now_dt - first_due).total_seconds())
            additional = elapsed_seconds // schedule.interval_seconds
            newest_due = first_due + timedelta(
                seconds=additional * schedule.interval_seconds
            )
            next_fire = newest_due + timedelta(seconds=schedule.interval_seconds)
            newest_age = (now_dt - newest_due).total_seconds()
            entry["next_fire_at"] = next_fire.isoformat().replace("+00:00", "Z")

            if additional:
                entry["last_skip"] = {
                    "count": additional,
                    "through": (
                        newest_due - timedelta(seconds=schedule.interval_seconds)
                    ).isoformat().replace("+00:00", "Z"),
                    "reason": "BACKLOG_COLLAPSED_NEWEST_ONLY",
                }

            scheduled_for = newest_due.isoformat().replace("+00:00", "Z")
            occurrence_id = self._occurrence_id(schedule_id, scheduled_for)
            if newest_age > schedule.misfire_grace_seconds:
                raw = {
                    "occurrence_id": occurrence_id,
                    "schedule_id": schedule_id,
                    "scheduled_for": scheduled_for,
                    "status": "SKIPPED",
                    "event": None,
                    "event_sha256": None,
                    "receipt": None,
                    "skip_reason": "MISFIRE_GRACE_EXCEEDED",
                }
                prior = state["occurrences"].get(occurrence_id)
                if prior is not None and prior != raw:
                    raise ResearchSchedulerError("occurrence identity conflict")
                state["occurrences"][occurrence_id] = raw
                entry["last_skip"] = {
                    "count": additional + 1,
                    "through": scheduled_for,
                    "reason": "MISFIRE_GRACE_EXCEEDED",
                }
                state["state_version"] += 1
                self._write(state)
                return TickResult(
                    TickAction.SKIPPED,
                    schedule_id=schedule_id,
                    occurrence_id=occurrence_id,
                    skipped_count=additional + 1,
                )

            event = schedule.event_for(scheduled_for)
            raw = {
                "occurrence_id": occurrence_id,
                "schedule_id": schedule_id,
                "scheduled_for": scheduled_for,
                "status": "PENDING",
                "event": event.canonical_payload(),
                "event_sha256": event.source_event_sha256,
                "receipt": None,
                "skip_reason": None,
            }
            prior = state["occurrences"].get(occurrence_id)
            if prior is not None and prior != raw:
                raise ResearchSchedulerError("occurrence identity conflict")
            state["occurrences"][occurrence_id] = raw
            state["state_version"] += 1
            self._write(state)
            return occurrence_id, event

    def _record_accepted(
        self,
        occurrence_id: str,
        event: ExternalResearchTrigger,
        receipt: ResearchTriggerReceipt,
    ) -> TickResult:
        if receipt.source_event_sha256 != event.source_event_sha256:
            raise ResearchSchedulerError("trigger sink returned receipt for another event")
        accepted_receipt = {
            **receipt.canonical_payload(),
            "receipt_sha256": receipt.receipt_sha256,
        }
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            raw = state["occurrences"].get(occurrence_id)
            if raw is None:
                raise ResearchSchedulerError("pending occurrence disappeared")
            expected_event = self._event_from_payload(raw["event"])
            if expected_event.source_event_sha256 != event.source_event_sha256:
                raise ResearchSchedulerError("pending occurrence changed during delivery")
            if raw["status"] == "ACCEPTED":
                if raw["receipt"] != accepted_receipt:
                    raise ResearchSchedulerError("accepted occurrence receipt conflict")
            elif raw["status"] == "PENDING":
                raw["status"] = "ACCEPTED"
                raw["receipt"] = accepted_receipt
                state["state_version"] += 1
                self._write(state)
            else:
                raise ResearchSchedulerError("pending occurrence status changed")
            return TickResult(
                TickAction.DELIVERED,
                schedule_id=raw["schedule_id"],
                occurrence_id=occurrence_id,
                receipt=receipt,
            )

    def tick(self, *, now: str) -> TickResult:
        """Deliver or skip at most one logical occurrence.

        Existing PENDING reservations recover before PAUSED/STOPPED state is
        consulted. Thus STOP blocks new reservations but cannot orphan a wakeup
        durably reserved before the status transition.
        """

        reserved = self._reserve_due(_timestamp(now, "now"))
        if isinstance(reserved, TickResult):
            return reserved
        occurrence_id, event = reserved
        receipt = self.trigger_sink.accept(event)
        if not isinstance(receipt, ResearchTriggerReceipt):
            raise ResearchSchedulerError(
                "trigger sink must return ResearchTriggerReceipt"
            )
        return self._record_accepted(occurrence_id, event, receipt)

    def run(
        self,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 1.0,
        max_ticks: int | None = None,
    ) -> int:
        """Run a bounded or persistent headless wake loop."""

        if not callable(clock) or not callable(sleep):
            raise TypeError("clock and sleep must be callable")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or poll_seconds <= 0
        ):
            raise ResearchSchedulerError("poll_seconds must be positive")
        if max_ticks is not None:
            _positive_int(max_ticks, "max_ticks")
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            current = clock()
            if not isinstance(current, datetime) or current.tzinfo is None:
                raise ResearchSchedulerError(
                    "clock must return timezone-aware datetime"
                )
            result = self.tick(now=current.isoformat())
            ticks += 1
            if result.action is TickAction.STOPPED:
                break
            if max_ticks is None or ticks < max_ticks:
                sleep(float(poll_seconds))
        return ticks
