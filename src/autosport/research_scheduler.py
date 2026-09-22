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
from typing import Any, Callable, Iterable, Protocol

from apscheduler.triggers.interval import IntervalTrigger

from .integrity import atomic_write_json, durable_path_lock
from .research_curriculum import (
    CurriculumDispatchReceipt,
    CurriculumPurpose,
    CurriculumStatus,
    NightResearchCurriculum,
    ReplayCandidate,
)
from .research_trigger_adapter import (
    ExternalResearchTrigger,
    ResearchTriggerReceipt,
    ResearchTriggerSource,
)
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.research_scheduler"
SCHEMA_VERSION = 3
_HEX = frozenset("0123456789abcdef")
_HISTORY_SCHEMA = "autosport.research_scheduler.occurrence_history_chunk"
_HISTORY_SCHEMA_VERSION = 1
_HISTORY_CHUNK_SIZE = 256
_HISTORY_ZERO_SHA256 = "0" * 64
_HISTORY_FIELDS = frozenset(
    {"occurrence_history_count", "occurrence_history_tail_sha256"}
)


class ResearchSchedulerError(RuntimeError):
    """Scheduler state or immutable schedule evidence is invalid."""


class SchedulerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class _CurriculumDispatchBlocked(RuntimeError):
    """Internal control-flow signal for a scheduler transition before dispatch."""

    def __init__(self, status: SchedulerStatus) -> None:
        super().__init__(f"curriculum dispatch blocked by scheduler {status.value}")
        self.status = status


class TickAction(StrEnum):
    IDLE = "IDLE"
    DELIVERED = "DELIVERED"
    SKIPPED = "SKIPPED"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ADMISSION_BLOCKED = "ADMISSION_BLOCKED"


class WakeSource(StrEnum):
    DRIFT_FINDING = "DRIFT_FINDING"
    POSTMORTEM_QUESTION = "POSTMORTEM_QUESTION"
    FORWARD_EVALUATION = "FORWARD_EVALUATION"
    SCHEDULED_QUESTION = "SCHEDULED_QUESTION"


class EvidenceReusePolicy(StrEnum):
    """How one schedule may reuse the evidence named by its wake purpose."""

    IMMUTABLE_RECURRING = "IMMUTABLE_RECURRING"
    SINGLE_CANONICAL_OCCURRENCE = "SINGLE_CANONICAL_OCCURRENCE"


_WAKE_SOURCE_RECORD_TYPE = {
    WakeSource.DRIFT_FINDING: "DriftFinding",
    WakeSource.POSTMORTEM_QUESTION: "Postmortem",
    WakeSource.FORWARD_EVALUATION: "EvaluationBundle",
}


class TriggerSink(Protocol):
    def accept(self, event: ExternalResearchTrigger) -> ResearchTriggerReceipt: ...


class CurriculumWakeRuntime(Protocol):
    def select_and_dispatch(
        self,
        candidates: Iterable[object],
        *,
        purpose: CurriculumPurpose,
        selector_policy_version: str,
        as_of: str,
        seed: int,
        budget_units: int,
        deadline_at: str | None = None,
    ) -> object: ...


class IntervalCadenceAdapter:
    """Thin APScheduler boundary; Autosport durable state remains authoritative."""

    @staticmethod
    def validate(*, first_fire_at: str, interval_seconds: int) -> None:
        try:
            IntervalTrigger(
                seconds=_positive_int(interval_seconds, "interval_seconds"),
                start_date=_instant(first_fire_at, "first_fire_at"),
                timezone=timezone.utc,
            )
        except (TypeError, ValueError) as exc:
            raise ResearchSchedulerError("invalid interval cadence") from exc

    @staticmethod
    def next_fire(*, previous_fire_at: str, first_fire_at: str, interval_seconds: int) -> datetime:
        IntervalCadenceAdapter.validate(
            first_fire_at=first_fire_at,
            interval_seconds=interval_seconds,
        )
        previous = _instant(previous_fire_at, "previous_fire_at")
        trigger = IntervalTrigger(
            seconds=interval_seconds,
            start_date=_instant(first_fire_at, "first_fire_at"),
            timezone=timezone.utc,
        )
        next_fire = trigger.get_next_fire_time(previous, previous)
        if next_fire is None:
            raise ResearchSchedulerError("interval cadence produced no next fire")
        return next_fire.astimezone(timezone.utc)


class NightResearchCurriculumWake:
    """Governed curriculum wake adapter; timing state stays in ResearchScheduler."""

    def __init__(
        self,
        curriculum: CurriculumWakeRuntime,
        *,
        candidate_loader: Callable[[str], Iterable[object]],
        selector_policy_version: str,
        seed: int,
        max_budget_units: int,
        admit_budget: Callable[[int], None] | None = None,
    ) -> None:
        if not callable(getattr(curriculum, "select_and_dispatch", None)):
            raise TypeError("curriculum must expose select_and_dispatch")
        if not callable(candidate_loader):
            raise TypeError("candidate_loader must be callable")
        _text(selector_policy_version, "selector_policy_version")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ResearchSchedulerError("seed must be non-negative")
        _positive_int(max_budget_units, "max_budget_units")
        if admit_budget is not None and not callable(admit_budget):
            raise TypeError("admit_budget must be callable")
        self.curriculum = curriculum
        self.candidate_loader = candidate_loader
        self.selector_policy_version = selector_policy_version
        self.seed = seed
        self.max_budget_units = max_budget_units
        self.admit_budget = admit_budget

    def dispatch(self, *, scheduled_for: str, budget_units: int, deadline_at: str | None) -> object:
        as_of = _timestamp(scheduled_for, "scheduled_for")
        _positive_int(budget_units, "budget_units")
        if budget_units > self.max_budget_units:
            raise ResearchSchedulerError("curriculum wake budget exceeds external admission")
        if self.admit_budget is not None:
            self.admit_budget(budget_units)
        candidates = tuple(self.candidate_loader(as_of))
        return self.curriculum.select_and_dispatch(
            candidates,
            purpose=CurriculumPurpose.CURRICULUM,
            selector_policy_version=self.selector_policy_version,
            as_of=as_of,
            seed=self.seed,
            budget_units=budget_units,
            deadline_at=deadline_at,
        )


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
    source_record_id: str | None = None
    evidence_reuse_policy: EvidenceReusePolicy = EvidenceReusePolicy.IMMUTABLE_RECURRING

    def __post_init__(self) -> None:
        _text(self.schedule_id, "schedule_id")
        if not isinstance(self.wake_source, WakeSource):
            raise ResearchSchedulerError("wake_source must be WakeSource")
        if not isinstance(self.evidence_reuse_policy, EvidenceReusePolicy):
            raise ResearchSchedulerError("evidence_reuse_policy must be EvidenceReusePolicy")
        if self.wake_source is WakeSource.SCHEDULED_QUESTION:
            if self.source_record_id is not None:
                raise ResearchSchedulerError(
                    "scheduled question must not claim typed source evidence"
                )
            if self.evidence_reuse_policy is not EvidenceReusePolicy.IMMUTABLE_RECURRING:
                raise ResearchSchedulerError(
                    "scheduled question must use immutable recurring evidence policy"
                )
        else:
            _text(self.source_record_id, "source_record_id")
            if (
                self.evidence_reuse_policy
                is not EvidenceReusePolicy.SINGLE_CANONICAL_OCCURRENCE
            ):
                raise ResearchSchedulerError(
                    "typed wake evidence cannot be reused by a recurring schedule"
                )
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
        IntervalCadenceAdapter.validate(
            first_fire_at=self.first_fire_at,
            interval_seconds=self.interval_seconds,
        )

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
            "source_record_id": self.source_record_id,
            "evidence_reuse_policy": self.evidence_reuse_policy.value,
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
            "source_record_id",
            "evidence_reuse_policy",
        }
        if set(raw) != expected:
            raise ResearchSchedulerError("schedule payload fields mismatch")
        try:
            wake_source = WakeSource(raw["wake_source"])
            evidence_reuse_policy = EvidenceReusePolicy(raw["evidence_reuse_policy"])
        except (TypeError, ValueError) as exc:
            raise ResearchSchedulerError(
                "schedule wake_source/evidence_reuse_policy is invalid"
            ) from exc
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
            source_record_id=raw["source_record_id"],
            evidence_reuse_policy=evidence_reuse_policy,
        )

    @property
    def is_single_occurrence(self) -> bool:
        return (
            self.evidence_reuse_policy
            is EvidenceReusePolicy.SINGLE_CANONICAL_OCCURRENCE
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
                "research-scheduler:"
                f"{self.wake_source.value}:"
                f"{self.source_record_id or self.question_id}:"
                f"{self.schedule_id}"
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
    curriculum_wake_id: str | None = None
    curriculum_selection_id: str | None = None


class ResearchScheduler:
    """Durable one-occurrence-at-a-time direct research wakeup runtime."""

    def __init__(
        self,
        path: str | Path,
        trigger_sink: TriggerSink,
        *,
        source_registry: ScientificRegistry | None = None,
    ) -> None:
        self.path = Path(path)
        if not callable(getattr(trigger_sink, "accept", None)):
            raise TypeError("trigger_sink must expose accept(event)")
        if source_registry is not None and not isinstance(
            source_registry, ScientificRegistry
        ):
            raise TypeError("source_registry must be ScientificRegistry")
        self.trigger_sink = trigger_sink
        self.source_registry = source_registry
        try:
            with WorkspaceEconomicLock(self.path.parent):
                state = self._read()
                self._validate(state, check_history_tail=False)
                state = self._prepare_occurrence_history_locked(state)
                self._validate(state)
        except FileNotFoundError as exc:
            raise ResearchSchedulerError("research scheduler state is missing") from exc

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        trigger_sink: TriggerSink,
        *,
        source_registry: ScientificRegistry | None = None,
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
                    "curriculum_wakes": {},
                    "occurrence_history_count": 0,
                    "occurrence_history_tail_sha256": _HISTORY_ZERO_SHA256,
                }
                atomic_write_json(target, {**body, "state_sha256": _digest(body)})
        return cls(target, trigger_sink, source_registry=source_registry)

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

    @property
    def _occurrence_history_dir(self) -> Path:
        return self.path.with_name(f".{self.path.name}.occurrence-history")

    def _occurrence_history_chunk_path(self, chunk_index: int) -> Path:
        _nonnegative_int(chunk_index, "occurrence history chunk_index")
        return self._occurrence_history_dir / f"{chunk_index:08d}.json"

    @staticmethod
    def _occurrence_history_anchor(
        state: dict[str, Any],
    ) -> tuple[int, str] | None:
        has_count = "occurrence_history_count" in state
        has_tail = "occurrence_history_tail_sha256" in state
        if has_count != has_tail:
            raise ResearchSchedulerError("occurrence history anchor is incomplete")
        if not has_count:
            return None
        count = _nonnegative_int(
            state["occurrence_history_count"],
            "occurrence_history_count",
        )
        tail = _sha(
            state["occurrence_history_tail_sha256"],
            "occurrence_history_tail_sha256",
        )
        if count == 0 and tail != _HISTORY_ZERO_SHA256:
            raise ResearchSchedulerError("empty occurrence history has non-empty tail")
        if count > 0 and tail == _HISTORY_ZERO_SHA256:
            raise ResearchSchedulerError("non-empty occurrence history has empty tail")
        return count, tail

    @staticmethod
    def _occurrence_history_entry(
        *,
        sequence: int,
        previous_entry_sha256: str,
        occurrence_id: str,
        occurrence: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "sequence": _positive_int(sequence, "history sequence"),
            "previous_entry_sha256": _sha(
                previous_entry_sha256,
                "history previous_entry_sha256",
            ),
            "occurrence_id": _sha(occurrence_id, "history occurrence_id"),
            "occurrence": occurrence,
        }
        return {**body, "entry_sha256": _digest(body)}

    def _read_occurrence_history_chunk_raw(
        self,
        chunk_index: int,
    ) -> dict[str, Any]:
        path = self._occurrence_history_chunk_path(chunk_index)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchSchedulerError(
                "research scheduler occurrence history chunk is unreadable"
            ) from exc
        if type(raw) is not dict or set(raw) != {
            "schema",
            "schema_version",
            "chunk_index",
            "start_sequence",
            "previous_tail_sha256",
            "entries",
            "chunk_sha256",
        }:
            raise ResearchSchedulerError("occurrence history chunk fields mismatch")
        if (
            raw["schema"] != _HISTORY_SCHEMA
            or raw["schema_version"] != _HISTORY_SCHEMA_VERSION
        ):
            raise ResearchSchedulerError("occurrence history chunk schema mismatch")
        if raw["chunk_index"] != chunk_index:
            raise ResearchSchedulerError("occurrence history chunk index mismatch")
        expected_start = chunk_index * _HISTORY_CHUNK_SIZE + 1
        if raw["start_sequence"] != expected_start:
            raise ResearchSchedulerError(
                "occurrence history chunk start sequence mismatch"
            )
        _sha(raw["previous_tail_sha256"], "history previous_tail_sha256")
        if type(raw["entries"]) is not list or not raw["entries"]:
            raise ResearchSchedulerError("occurrence history chunk entries are invalid")
        if len(raw["entries"]) > _HISTORY_CHUNK_SIZE:
            raise ResearchSchedulerError("occurrence history chunk exceeds bound")
        body = {key: value for key, value in raw.items() if key != "chunk_sha256"}
        if _sha(raw["chunk_sha256"], "history chunk_sha256") != _digest(body):
            raise ResearchSchedulerError("occurrence history chunk digest mismatch")
        return raw

    def _validate_occurrence_history_entry(
        self,
        raw: object,
        *,
        expected_sequence: int,
        expected_previous: str,
        schedules: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str]:
        if type(raw) is not dict or set(raw) != {
            "sequence",
            "previous_entry_sha256",
            "occurrence_id",
            "occurrence",
            "entry_sha256",
        }:
            raise ResearchSchedulerError("occurrence history entry fields mismatch")
        if _positive_int(raw["sequence"], "history sequence") != expected_sequence:
            raise ResearchSchedulerError("occurrence history sequence mismatch")
        previous = _sha(
            raw["previous_entry_sha256"],
            "history previous_entry_sha256",
        )
        if previous != expected_previous:
            raise ResearchSchedulerError("occurrence history chain mismatch")
        occurrence_id = _sha(raw["occurrence_id"], "history occurrence_id")
        occurrence = raw["occurrence"]
        self._validate_occurrence(occurrence_id, occurrence, schedules)
        if occurrence["status"] not in {"ACCEPTED", "SKIPPED"}:
            raise ResearchSchedulerError(
                "occurrence history may contain only completed occurrences"
            )
        body = {
            "sequence": raw["sequence"],
            "previous_entry_sha256": previous,
            "occurrence_id": occurrence_id,
            "occurrence": occurrence,
        }
        entry_sha256 = _sha(raw["entry_sha256"], "history entry_sha256")
        if entry_sha256 != _digest(body):
            raise ResearchSchedulerError("occurrence history entry digest mismatch")
        return occurrence_id, occurrence, entry_sha256

    def _occurrence_history_chunk_indices(self) -> tuple[int, ...]:
        directory = self._occurrence_history_dir
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise ResearchSchedulerError(
                "research scheduler occurrence history path is not a directory"
            )
        indices: list[int] = []
        for path in directory.glob("*.json"):
            stem = path.stem
            if len(stem) != 8 or not stem.isdigit():
                raise ResearchSchedulerError(
                    "occurrence history chunk filename is invalid"
                )
            indices.append(int(stem))
        indices.sort()
        if indices and indices != list(range(indices[-1] + 1)):
            raise ResearchSchedulerError("occurrence history chunk sequence has gaps")
        return tuple(indices)

    def _read_occurrence_history_records(
        self,
        schedules: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[tuple[str, dict[str, Any], str]]:
        if limit is not None:
            _nonnegative_int(limit, "occurrence history read limit")
            if limit == 0:
                return []
        records: list[tuple[str, dict[str, Any], str]] = []
        previous = _HISTORY_ZERO_SHA256
        expected_sequence = 1
        indices = self._occurrence_history_chunk_indices()
        seen: set[str] = set()
        for position, chunk_index in enumerate(indices):
            chunk = self._read_occurrence_history_chunk_raw(chunk_index)
            if chunk["previous_tail_sha256"] != previous:
                raise ResearchSchedulerError(
                    "occurrence history chunk predecessor mismatch"
                )
            entries = chunk["entries"]
            if position < len(indices) - 1 and len(entries) != _HISTORY_CHUNK_SIZE:
                raise ResearchSchedulerError(
                    "non-final occurrence history chunk is not sealed"
                )
            for raw in entries:
                occurrence_id, occurrence, entry_sha256 = (
                    self._validate_occurrence_history_entry(
                        raw,
                        expected_sequence=expected_sequence,
                        expected_previous=previous,
                        schedules=schedules,
                    )
                )
                if occurrence_id in seen:
                    raise ResearchSchedulerError(
                        "duplicate occurrence identity in history"
                    )
                seen.add(occurrence_id)
                records.append((occurrence_id, occurrence, entry_sha256))
                previous = entry_sha256
                expected_sequence += 1
                if limit is not None and len(records) == limit:
                    return records
        return records

    def _validate_occurrence_history_tail(self, state: dict[str, Any]) -> None:
        anchor = self._occurrence_history_anchor(state)
        if anchor is None:
            return
        count, tail = anchor
        if count == 0:
            if self._occurrence_history_chunk_indices():
                raise ResearchSchedulerError(
                    "occurrence history exists beyond empty state anchor"
                )
            return
        chunk_index = (count - 1) // _HISTORY_CHUNK_SIZE
        expected_entries = ((count - 1) % _HISTORY_CHUNK_SIZE) + 1
        try:
            chunk = self._read_occurrence_history_chunk_raw(chunk_index)
        except FileNotFoundError as exc:
            raise ResearchSchedulerError(
                "occurrence history tail chunk is missing"
            ) from exc
        if len(chunk["entries"]) != expected_entries:
            raise ResearchSchedulerError(
                "occurrence history tail cardinality mismatches state anchor"
            )
        last = chunk["entries"][-1]
        if type(last) is not dict or set(last) != {
            "sequence",
            "previous_entry_sha256",
            "occurrence_id",
            "occurrence",
            "entry_sha256",
        }:
            raise ResearchSchedulerError("occurrence history tail entry is invalid")
        if last["sequence"] != count:
            raise ResearchSchedulerError("occurrence history tail sequence mismatch")
        if _sha(last["entry_sha256"], "history tail entry_sha256") != tail:
            raise ResearchSchedulerError("occurrence history tail digest mismatch")
        if self._occurrence_history_chunk_path(chunk_index + 1).exists():
            raise ResearchSchedulerError(
                "occurrence history extends beyond state anchor"
            )

    def _ensure_occurrence_history_anchor_locked(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        if self._occurrence_history_anchor(state) is not None:
            return state
        if self._occurrence_history_chunk_indices():
            raise ResearchSchedulerError(
                "legacy scheduler state has detached occurrence history"
            )
        state["occurrence_history_count"] = 0
        state["occurrence_history_tail_sha256"] = _HISTORY_ZERO_SHA256
        state["state_version"] += 1
        self._write(state)
        migrated = self._read()
        self._validate(migrated, check_history_tail=False)
        return migrated

    def _recover_occurrence_history_locked(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        anchor = self._occurrence_history_anchor(state)
        if anchor is None:
            raise ResearchSchedulerError("occurrence history anchor is missing")
        count, tail = anchor
        records = self._read_occurrence_history_records(state["schedules"])
        if len(records) < count:
            raise ResearchSchedulerError("occurrence history was truncated")
        if count == 0:
            if tail != _HISTORY_ZERO_SHA256:
                raise ResearchSchedulerError("empty occurrence history tail mismatch")
        elif records[count - 1][2] != tail:
            raise ResearchSchedulerError(
                "occurrence history does not match durable state anchor"
            )

        changed = False
        anchored: dict[str, dict[str, Any]] = {}
        for occurrence_id, occurrence, _ in records[:count]:
            anchored[occurrence_id] = occurrence
            hot = state["occurrences"].get(occurrence_id)
            if hot is not None:
                if hot != occurrence:
                    raise ResearchSchedulerError(
                        "hot occurrence conflicts with archived evidence"
                    )
                del state["occurrences"][occurrence_id]
                changed = True

        if len(records) > count:
            for occurrence_id, occurrence, _ in records[count:]:
                hot = state["occurrences"].get(occurrence_id)
                if hot != occurrence:
                    raise ResearchSchedulerError(
                        "unanchored occurrence history cannot be recovered"
                    )
                del state["occurrences"][occurrence_id]
                anchored[occurrence_id] = occurrence
            state["occurrence_history_count"] = len(records)
            state["occurrence_history_tail_sha256"] = records[-1][2]
            changed = True

        if changed:
            state["state_version"] += 1
            self._write(state)
            recovered = self._read()
            self._validate(recovered)
            return recovered
        self._validate_occurrence_history_tail(state)
        return state

    def _append_occurrence_history_batch_locked(
        self,
        state: dict[str, Any],
        completed: list[tuple[str, dict[str, Any]]],
    ) -> None:
        if not completed:
            return
        anchor = self._occurrence_history_anchor(state)
        if anchor is None:
            raise ResearchSchedulerError("occurrence history anchor is missing")
        count, tail = anchor
        active_chunk_index: int | None = None
        active_previous_tail: str | None = None
        active_entries: list[dict[str, Any]] | None = None

        def flush_active_chunk() -> None:
            if (
                active_chunk_index is None
                or active_previous_tail is None
                or active_entries is None
            ):
                return
            body = {
                "schema": _HISTORY_SCHEMA,
                "schema_version": _HISTORY_SCHEMA_VERSION,
                "chunk_index": active_chunk_index,
                "start_sequence": (
                    active_chunk_index * _HISTORY_CHUNK_SIZE + 1
                ),
                "previous_tail_sha256": active_previous_tail,
                "entries": active_entries,
            }
            self._occurrence_history_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                self._occurrence_history_chunk_path(active_chunk_index),
                {**body, "chunk_sha256": _digest(body)},
            )

        for occurrence_id, occurrence in completed:
            chunk_index = count // _HISTORY_CHUNK_SIZE
            offset = count % _HISTORY_CHUNK_SIZE
            if active_chunk_index != chunk_index:
                flush_active_chunk()
                active_chunk_index = chunk_index
                chunk_path = self._occurrence_history_chunk_path(chunk_index)
                if offset == 0:
                    if chunk_path.exists():
                        raise ResearchSchedulerError(
                            "unanchored occurrence history chunk already exists"
                        )
                    active_entries = []
                    active_previous_tail = tail
                else:
                    try:
                        chunk = self._read_occurrence_history_chunk_raw(
                            chunk_index
                        )
                    except FileNotFoundError as exc:
                        raise ResearchSchedulerError(
                            "occurrence history append chunk is missing"
                        ) from exc
                    active_entries = list(chunk["entries"])
                    if len(active_entries) != offset:
                        raise ResearchSchedulerError(
                            "occurrence history append offset mismatch"
                        )
                    if (
                        _sha(
                            active_entries[-1]["entry_sha256"],
                            "history entry_sha256",
                        )
                        != tail
                    ):
                        raise ResearchSchedulerError(
                            "occurrence history append tail mismatch"
                        )
                    active_previous_tail = chunk[
                        "previous_tail_sha256"
                    ]

            assert active_entries is not None
            entry = self._occurrence_history_entry(
                sequence=count + 1,
                previous_entry_sha256=tail,
                occurrence_id=occurrence_id,
                occurrence=occurrence,
            )
            active_entries.append(entry)
            count += 1
            tail = entry["entry_sha256"]
            state["occurrence_history_count"] = count
            state["occurrence_history_tail_sha256"] = tail

        flush_active_chunk()

    def _archive_completed_occurrences_locked(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        completed = sorted(
            (
                (occurrence_id, raw)
                for occurrence_id, raw in state["occurrences"].items()
                if raw["status"] in {"ACCEPTED", "SKIPPED"}
            ),
            key=lambda item: (
                item[1]["scheduled_for"],
                item[1]["schedule_id"],
                item[0],
            ),
        )
        if not completed:
            return state
        for occurrence_id, raw in completed:
            self._validate_occurrence(
                occurrence_id,
                raw,
                state["schedules"],
            )
        self._append_occurrence_history_batch_locked(state, completed)
        for occurrence_id, _ in completed:
            del state["occurrences"][occurrence_id]
        state["state_version"] += 1
        self._write(state)
        archived = self._read()
        self._validate(archived)
        return archived

    def _prepare_occurrence_history_locked(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._ensure_occurrence_history_anchor_locked(state)
        state = self._recover_occurrence_history_locked(state)
        state = self._archive_completed_occurrences_locked(state)
        records = self._read_occurrence_history_records(state["schedules"])
        anchor = self._occurrence_history_anchor(state)
        assert anchor is not None
        count, tail = anchor
        if len(records) != count:
            raise ResearchSchedulerError(
                "occurrence history cardinality mismatch after recovery"
            )
        if count and records[-1][2] != tail:
            raise ResearchSchedulerError(
                "occurrence history tail mismatch after recovery"
            )
        return state

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

    def _validate(
        self,
        state: object,
        *,
        check_history_tail: bool = True,
    ) -> None:
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
            "curriculum_wakes",
            "state_sha256",
        }
        fields = set(state)
        if fields != required and fields != required | _HISTORY_FIELDS:
            raise ResearchSchedulerError("research scheduler state fields mismatch")
        self._occurrence_history_anchor(state)
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
                "retired",
            }:
                raise ResearchSchedulerError("schedule state fields mismatch")
            schedule = ResearchSchedule.from_payload(raw["schedule"])
            self._validate_schedule_authority(schedule)
            if not isinstance(raw["retired"], bool):
                raise ResearchSchedulerError("schedule retired flag must be boolean")
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
        for wake_id, raw in state["curriculum_wakes"].items():
            self._validate_curriculum_wake(wake_id, raw)
        if check_history_tail:
            self._validate_occurrence_history_tail(state)

    def _validate_schedule_authority(self, schedule: ResearchSchedule) -> None:
        if schedule.wake_source is WakeSource.SCHEDULED_QUESTION:
            return
        if self.source_registry is None:
            raise ResearchSchedulerError(
                "typed wake source requires canonical ScientificRegistry authority"
            )
        record_type = _WAKE_SOURCE_RECORD_TYPE.get(schedule.wake_source)
        if record_type is None:
            raise ResearchSchedulerError("typed wake source has no canonical record mapping")
        source = self.source_registry.get(record_type, schedule.source_record_id)
        if source is None:
            raise ResearchSchedulerError(
                f"{schedule.wake_source.value} source record is missing"
            )
        if source.record_sha256 != schedule.source_evidence_sha256:
            raise ResearchSchedulerError(
                "wake source evidence hash does not match canonical source record"
            )
        question = self.source_registry.get("ResearchQuestion", schedule.question_id)
        if question is None:
            raise ResearchSchedulerError("scheduled wake ResearchQuestion is missing")
        if question.record_sha256 != schedule.question_record_sha256:
            raise ResearchSchedulerError(
                "scheduled wake ResearchQuestion hash mismatch"
            )
        if question.payload.get("source_sha256") != source.record_sha256:
            raise ResearchSchedulerError(
                "wake source purpose is not bound to the ResearchQuestion"
            )
        observed = _instant(schedule.source_observed_at, "source_observed_at")
        if _instant(source.available_at, "source.available_at") > observed:
            raise ResearchSchedulerError(
                "source_observed_at precedes canonical source availability"
            )
        first = _instant(schedule.first_fire_at, "first_fire_at")
        if _instant(question.available_at, "question.available_at") > first:
            raise ResearchSchedulerError(
                "scheduled wake ResearchQuestion is unavailable at first fire"
            )

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
        schedule = ResearchSchedule.from_payload(schedules[schedule_id]["schedule"])
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
        expected_event = schedule.event_for(raw["scheduled_for"])
        if event.source_event_sha256 != expected_event.source_event_sha256:
            raise ResearchSchedulerError(
                "occurrence event is not bound to its schedule purpose/evidence"
            )
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


    @staticmethod
    def _curriculum_population(candidates: Iterable[ReplayCandidate]) -> tuple[ReplayCandidate, ...]:
        population = tuple(candidates)
        if not population:
            raise ResearchSchedulerError("curriculum population must not be empty")
        for candidate in population:
            if not isinstance(candidate, ReplayCandidate):
                raise ResearchSchedulerError("curriculum population must contain ReplayCandidate values")
        ids = [candidate.candidate_id for candidate in population]
        if len(ids) != len(set(ids)):
            raise ResearchSchedulerError("curriculum population contains duplicate candidate identity")
        return tuple(sorted(population, key=lambda candidate: candidate.candidate_id))

    @classmethod
    def _curriculum_population_digest(
        cls, candidates: Iterable[ReplayCandidate]
    ) -> tuple[tuple[str, ...], str]:
        population = cls._curriculum_population(candidates)
        frozen = [
            {"candidate_id": c.candidate_id, "candidate_payload": c.payload()}
            for c in population
        ]
        return tuple(item["candidate_id"] for item in frozen), _digest(frozen)

    @staticmethod
    def _curriculum_wake_id(
        *,
        selector_policy_version: str,
        purpose: CurriculumPurpose,
        candidate_population_sha256: str,
        as_of: str,
        seed: int,
        budget_units: int,
        deadline_at: str | None,
    ) -> str:
        return _digest(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "kind": "CurriculumWake",
                "selector_policy_version": selector_policy_version,
                "purpose": purpose.value,
                "candidate_population_sha256": candidate_population_sha256,
                "as_of": _timestamp(as_of, "as_of"),
                "seed": seed,
                "budget_units": budget_units,
                "deadline_at": None if deadline_at is None else _timestamp(deadline_at, "deadline_at"),
            }
        )

    @staticmethod
    def _validate_curriculum_wake(wake_id: object, raw: object) -> None:
        _sha(wake_id, "curriculum_wake_id")
        if type(raw) is not dict or set(raw) != {
            "wake_id", "status", "selector_policy_version", "purpose",
            "candidate_ids", "candidate_population_sha256", "as_of", "seed",
            "budget_units", "deadline_at", "selection_id", "run_id", "receipt_sha256",
        }:
            raise ResearchSchedulerError("curriculum wake fields mismatch")
        if raw["wake_id"] != wake_id:
            raise ResearchSchedulerError("curriculum wake identity mismatch")
        _text(raw["selector_policy_version"], "curriculum selector_policy_version")
        try:
            CurriculumPurpose(raw["purpose"])
        except (TypeError, ValueError) as exc:
            raise ResearchSchedulerError("curriculum wake purpose is invalid") from exc
        ids = raw["candidate_ids"]
        if type(ids) is not list or ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ResearchSchedulerError("curriculum wake candidate_ids are invalid")
        for candidate_id in ids:
            _sha(candidate_id, "curriculum candidate_id")
        _sha(raw["candidate_population_sha256"], "curriculum candidate_population_sha256")
        _timestamp(raw["as_of"], "curriculum as_of")
        _nonnegative_int(raw["seed"], "curriculum seed")
        _positive_int(raw["budget_units"], "curriculum budget_units")
        if raw["deadline_at"] is not None and _instant(raw["deadline_at"], "curriculum deadline_at") < _instant(raw["as_of"], "curriculum as_of"):
            raise ResearchSchedulerError("curriculum deadline_at cannot precede as_of")
        if raw["status"] == "PENDING":
            if any(raw[k] is not None for k in ("selection_id", "run_id", "receipt_sha256")):
                raise ResearchSchedulerError("pending curriculum wake carries acceptance evidence")
        elif raw["status"] == "DISPATCHING":
            _sha(raw["selection_id"], "curriculum selection_id")
            if any(raw[k] is not None for k in ("run_id", "receipt_sha256")):
                raise ResearchSchedulerError("dispatching curriculum wake carries acceptance evidence")
        elif raw["status"] == "ACCEPTED":
            _sha(raw["selection_id"], "curriculum selection_id")
            _sha(raw["run_id"], "curriculum run_id")
            _sha(raw["receipt_sha256"], "curriculum receipt_sha256")
        else:
            raise ResearchSchedulerError("curriculum wake status is invalid")

    @staticmethod
    def _oldest_pending_curriculum(state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        pending = [
            (wake_id, raw)
            for wake_id, raw in state["curriculum_wakes"].items()
            if raw["status"] in {"PENDING", "DISPATCHING"}
        ]
        if not pending:
            return None
        return min(
            pending,
            key=lambda item: (item[1]["as_of"], item[1]["selector_policy_version"], item[0]),
        )

    def _begin_curriculum_dispatch_locked(
        self,
        wake_id: str,
        selection_id: str,
    ) -> None:
        """Linearize or recover a curriculum dispatch under the shared workspace lock.

        NightResearchCurriculum invokes this callback immediately before writing its
        downstream PENDING reservation while holding WorkspaceEconomicLock.  A
        PAUSE/STOP committed before this point blocks a new dispatch.  Once the
        scheduler durably records DISPATCHING, later PAUSE/STOP cannot orphan that
        already-started immutable dispatch; restart may only recover the same
        selection identity.
        """

        _sha(selection_id, "curriculum selection_id")
        state = self._read()
        self._validate(state)
        wake = state["curriculum_wakes"].get(wake_id)
        if wake is None:
            raise ResearchSchedulerError("curriculum wake disappeared before dispatch")
        if wake["status"] == "DISPATCHING":
            if wake["selection_id"] != selection_id:
                raise ResearchSchedulerError("curriculum dispatch selection identity conflict")
            return
        if wake["status"] != "PENDING":
            raise ResearchSchedulerError("curriculum wake is no longer dispatchable")
        status = SchedulerStatus(state["status"])
        if status in {SchedulerStatus.PAUSED, SchedulerStatus.STOPPED}:
            raise _CurriculumDispatchBlocked(status)
        wake["status"] = "DISPATCHING"
        wake["selection_id"] = selection_id
        state["state_version"] += 1
        self._write(state)

    def queue_curriculum_wake(
        self,
        curriculum: NightResearchCurriculum,
        candidates: Iterable[ReplayCandidate],
        *,
        purpose: CurriculumPurpose,
        selector_policy_version: str,
        as_of: str,
        seed: int,
        budget_units: int,
        max_concurrency: int,
        active_concurrency: int,
        remaining_budget_units: int,
        deadline_at: str | None = None,
    ) -> str:
        if not isinstance(curriculum, NightResearchCurriculum):
            raise TypeError("curriculum must be NightResearchCurriculum")
        if curriculum.path.parent.resolve() != self.path.parent.resolve():
            raise ResearchSchedulerError("curriculum must share the scheduler research workspace")
        if not isinstance(purpose, CurriculumPurpose):
            raise ResearchSchedulerError("purpose must be CurriculumPurpose")
        selector_policy_version = _text(selector_policy_version, "selector_policy_version")
        as_of = _timestamp(as_of, "as_of")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ResearchSchedulerError("seed must be non-negative")
        _positive_int(budget_units, "budget_units")
        _positive_int(max_concurrency, "max_concurrency")
        _nonnegative_int(active_concurrency, "active_concurrency")
        _nonnegative_int(remaining_budget_units, "remaining_budget_units")
        if active_concurrency >= max_concurrency:
            raise ResearchSchedulerError("curriculum concurrency admission unavailable")
        if budget_units > remaining_budget_units:
            raise ResearchSchedulerError("curriculum external budget admission unavailable")
        if budget_units > curriculum.max_budget_units:
            raise ResearchSchedulerError("curriculum budget exceeds curriculum authority")
        if curriculum.status is not CurriculumStatus.ACTIVE:
            raise ResearchSchedulerError("curriculum is not active")
        if deadline_at is not None:
            deadline_at = _timestamp(deadline_at, "deadline_at")
            if _instant(deadline_at, "deadline_at") < _instant(as_of, "as_of"):
                raise ResearchSchedulerError("deadline_at cannot precede as_of")

        candidate_ids, population_sha256 = self._curriculum_population_digest(candidates)
        wake_id = self._curriculum_wake_id(
            selector_policy_version=selector_policy_version,
            purpose=purpose,
            candidate_population_sha256=population_sha256,
            as_of=as_of,
            seed=seed,
            budget_units=budget_units,
            deadline_at=deadline_at,
        )
        entry = {
            "wake_id": wake_id,
            "status": "PENDING",
            "selector_policy_version": selector_policy_version,
            "purpose": purpose.value,
            "candidate_ids": list(candidate_ids),
            "candidate_population_sha256": population_sha256,
            "as_of": as_of,
            "seed": seed,
            "budget_units": budget_units,
            "deadline_at": deadline_at,
            "selection_id": None,
            "run_id": None,
            "receipt_sha256": None,
        }
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            if SchedulerStatus(state["status"]) is SchedulerStatus.STOPPED:
                raise ResearchSchedulerError("stopped scheduler cannot queue curriculum wake")
            prior = state["curriculum_wakes"].get(wake_id)
            if prior is not None:
                if prior != entry:
                    raise ResearchSchedulerError("curriculum wake identity conflict")
                return wake_id
            if self._oldest_pending_curriculum(state) is not None:
                raise ResearchSchedulerError("another curriculum wake is already pending")
            state["curriculum_wakes"][wake_id] = entry
            state["curriculum_wakes"] = dict(sorted(state["curriculum_wakes"].items()))
            state["state_version"] += 1
            self._write(state)
        return wake_id

    def tick_curriculum(
        self,
        curriculum: NightResearchCurriculum,
        candidates: Iterable[ReplayCandidate],
        *,
        max_concurrency: int,
        active_concurrency: int,
        remaining_budget_units: int,
    ) -> TickResult:
        if not isinstance(curriculum, NightResearchCurriculum):
            raise TypeError("curriculum must be NightResearchCurriculum")
        if curriculum.path.parent.resolve() != self.path.parent.resolve():
            raise ResearchSchedulerError("curriculum must share the scheduler research workspace")
        _positive_int(max_concurrency, "max_concurrency")
        _nonnegative_int(active_concurrency, "active_concurrency")
        _nonnegative_int(remaining_budget_units, "remaining_budget_units")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            pending = self._oldest_pending_curriculum(state)
            status = SchedulerStatus(state["status"])
            if pending is None:
                if status is SchedulerStatus.PAUSED:
                    return TickResult(TickAction.PAUSED)
                if status is SchedulerStatus.STOPPED:
                    return TickResult(TickAction.STOPPED)
                return TickResult(TickAction.IDLE)
            wake_id, wake = pending
            recovering_dispatch = wake["status"] == "DISPATCHING"
            if not recovering_dispatch:
                if status is SchedulerStatus.PAUSED:
                    return TickResult(TickAction.PAUSED, curriculum_wake_id=wake_id)
                if status is SchedulerStatus.STOPPED:
                    return TickResult(TickAction.STOPPED, curriculum_wake_id=wake_id)
                if active_concurrency >= max_concurrency or remaining_budget_units < wake["budget_units"]:
                    return TickResult(TickAction.ADMISSION_BLOCKED, curriculum_wake_id=wake_id)
                if curriculum.status is CurriculumStatus.STOPPED:
                    return TickResult(TickAction.STOPPED, curriculum_wake_id=wake_id)
            population = self._curriculum_population(candidates)
            actual_ids, actual_digest = self._curriculum_population_digest(population)
            if actual_ids != tuple(wake["candidate_ids"]) or actual_digest != wake["candidate_population_sha256"]:
                raise ResearchSchedulerError("curriculum wake population identity/cutoff evidence changed")
            purpose = CurriculumPurpose(wake["purpose"])
            selector_policy_version = wake["selector_policy_version"]
            as_of = wake["as_of"]
            budget_units = wake["budget_units"]
            deadline_at = wake["deadline_at"]
            seed = wake["seed"]

        try:
            receipt = curriculum.select_and_dispatch(
                population,
                purpose=purpose,
                selector_policy_version=selector_policy_version,
                as_of=as_of,
                seed=seed,
                budget_units=budget_units,
                deadline_at=deadline_at,
                before_reservation=lambda record: self._begin_curriculum_dispatch_locked(
                    wake_id,
                    record.selection_id,
                ),
            )
        except _CurriculumDispatchBlocked as blocked:
            action = (
                TickAction.PAUSED
                if blocked.status is SchedulerStatus.PAUSED
                else TickAction.STOPPED
            )
            return TickResult(action, curriculum_wake_id=wake_id)
        if not isinstance(receipt, CurriculumDispatchReceipt):
            raise ResearchSchedulerError("curriculum selector must return CurriculumDispatchReceipt")

        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            prior = state["curriculum_wakes"].get(wake_id)
            if prior is None:
                raise ResearchSchedulerError("curriculum wake disappeared")
            if prior["status"] == "ACCEPTED":
                if prior["selection_id"] != receipt.selection_id or prior["run_id"] != receipt.run_id or prior["receipt_sha256"] != receipt.trigger_receipt.receipt_sha256:
                    raise ResearchSchedulerError("curriculum acceptance identity conflict")
            elif prior["status"] == "DISPATCHING":
                if prior["selection_id"] != receipt.selection_id:
                    raise ResearchSchedulerError("curriculum dispatch selection identity conflict")
                prior["status"] = "ACCEPTED"
                prior["run_id"] = receipt.run_id
                prior["receipt_sha256"] = receipt.trigger_receipt.receipt_sha256
                state["state_version"] += 1
                self._write(state)
            else:
                raise ResearchSchedulerError("curriculum wake status changed unexpectedly")
        return TickResult(
            TickAction.DELIVERED,
            curriculum_wake_id=wake_id,
            curriculum_selection_id=receipt.selection_id,
        )

    def add_schedule(self, schedule: ResearchSchedule) -> None:
        if not isinstance(schedule, ResearchSchedule):
            raise TypeError("schedule must be ResearchSchedule")
        self._validate_schedule_authority(schedule)
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
                "retired": False,
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
        with WorkspaceEconomicLock(self.path.parent):
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
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            self._validate(state)
            frozen = json.loads(_canonical_json(state))
        anchor = self._occurrence_history_anchor(frozen)
        assert anchor is not None
        count, tail = anchor
        records = self._read_occurrence_history_records(
            frozen["schedules"],
            limit=count,
        )
        if len(records) < count:
            raise ResearchSchedulerError("occurrence history was truncated")
        if count and records[count - 1][2] != tail:
            raise ResearchSchedulerError(
                "occurrence history does not match snapshot anchor"
            )
        merged = {
            occurrence_id: occurrence
            for occurrence_id, occurrence, _ in records[:count]
        }
        for occurrence_id, occurrence in frozen["occurrences"].items():
            prior = merged.get(occurrence_id)
            if prior is not None and prior != occurrence:
                raise ResearchSchedulerError(
                    "hot occurrence conflicts with snapshot history"
                )
            merged[occurrence_id] = occurrence
        frozen["occurrences"] = dict(sorted(merged.items()))
        body = {
            key: value
            for key, value in frozen.items()
            if key != "state_sha256"
        }
        frozen["state_sha256"] = _digest(body)
        return frozen

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
                if entry["retired"]:
                    continue
                fire = _instant(entry["next_fire_at"], "next_fire_at")
                if fire <= now_dt:
                    due.append((fire, schedule_id, entry))
            if not due:
                return TickResult(TickAction.IDLE)

            _, schedule_id, entry = min(due, key=lambda item: (item[0], item[1]))
            schedule = ResearchSchedule.from_payload(entry["schedule"])
            first_due = _instant(entry["next_fire_at"], "next_fire_at")
            if schedule.is_single_occurrence:
                additional = 0
                newest_due = first_due
                entry["retired"] = True
            else:
                elapsed_seconds = int((now_dt - first_due).total_seconds())
                additional = elapsed_seconds // schedule.interval_seconds
                newest_due = first_due + timedelta(
                    seconds=additional * schedule.interval_seconds
                )
                next_fire = IntervalCadenceAdapter.next_fire(
                    previous_fire_at=newest_due.isoformat().replace("+00:00", "Z"),
                    first_fire_at=schedule.first_fire_at,
                    interval_seconds=schedule.interval_seconds,
                )
                entry["next_fire_at"] = next_fire.isoformat().replace("+00:00", "Z")

                if additional:
                    entry["last_skip"] = {
                        "count": additional,
                        "through": (
                            newest_due - timedelta(seconds=schedule.interval_seconds)
                        ).isoformat().replace("+00:00", "Z"),
                        "reason": "BACKLOG_COLLAPSED_NEWEST_ONLY",
                    }
            newest_age = (now_dt - newest_due).total_seconds()

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
                state = self._read()
                self._validate(state)
                self._archive_completed_occurrences_locked(state)
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
            schedule_id = raw["schedule_id"]
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
                state = self._read()
                self._validate(state)
            else:
                raise ResearchSchedulerError("pending occurrence status changed")
            self._archive_completed_occurrences_locked(state)
            return TickResult(
                TickAction.DELIVERED,
                schedule_id=schedule_id,
                occurrence_id=occurrence_id,
                receipt=receipt,
            )

    def tick(self, *, now: str) -> TickResult:
        """Deliver or skip at most one logical occurrence.

        Existing PENDING reservations recover before PAUSED/STOPPED state is
        consulted. Thus STOP blocks new reservations but cannot orphan a wakeup
        durably reserved before the status transition.

        The scheduler-state workspace lock intentionally is not held across the
        external trigger sink: pause/STOP must remain able to commit while a
        delivery is in flight.  A separate crash-releasing durable path lock
        serializes reserve -> sink -> receipt publication across cooperating
        scheduler processes, so the same PENDING occurrence cannot be delivered
        concurrently by duplicate scheduler instances.  Crash-after-acceptance
        recovery still replays the identical immutable event and therefore
        relies on the trigger sink's existing idempotent event identity.
        """

        dispatch_lock_target = self.path.with_name(
            f"{self.path.name}.occurrence-dispatch"
        )
        with durable_path_lock(dispatch_lock_target):
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
