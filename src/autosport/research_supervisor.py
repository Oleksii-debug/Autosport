"""Durable, restart-safe orchestration state for the continuous research supervisor.

This module is intentionally a bounded control/state seam, not a second workflow
engine. Scheduling is external trigger input; scientific truth remains owned by
``ScientificRegistry``/``ExperimentRunner`` and environment truth by the learning
environment module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


SCHEMA_VERSION = 1


class ResearchSupervisorError(ValueError):
    """Base error for invalid or conflicting supervisor state."""


class ConflictingResearchTriggerError(ResearchSupervisorError):
    """Raised when one trigger identity is reused for different work."""


class InvalidResearchTransitionError(ResearchSupervisorError):
    """Raised when a supervisor phase moves outside the frozen lifecycle."""


class ResearchPhase(StrEnum):
    QUESTION = "QUESTION"
    HYPOTHESIS = "HYPOTHESIS"
    SOURCE_SEARCH = "SOURCE_SEARCH"
    PROTOCOL_FREEZE = "PROTOCOL_FREEZE"
    DATASET_SNAPSHOT = "DATASET_SNAPSHOT"
    EXPERIMENT = "EXPERIMENT"
    CAUSAL_EVALUATION = "CAUSAL_EVALUATION"
    ROBUSTNESS = "ROBUSTNESS"
    CHAMPION_CHALLENGER = "CHAMPION_CHALLENGER"
    FORWARD_PAPER_SHADOW = "FORWARD_PAPER_SHADOW"
    PROMOTE_REJECT_RETIRE = "PROMOTE_REJECT_RETIRE"
    POSTMORTEM = "POSTMORTEM"
    MEMORY = "MEMORY"
    NEXT_QUESTION = "NEXT_QUESTION"


_PHASE_ORDER = tuple(ResearchPhase)
_PHASE_INDEX = {phase: index for index, phase in enumerate(_PHASE_ORDER)}


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ResearchSupervisorError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ResearchSupervisorError(f"{name} must not contain NUL")
    value.encode("utf-8")
    return value


def _timestamp(name: str, value: object) -> str:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchSupervisorError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchSupervisorError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(name: str, value: object) -> str:
    text = _text(name, value).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ResearchSupervisorError(f"{name} must be lowercase SHA-256 hex")
    return text


def _canonical_json(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ResearchSupervisorError("supervisor payload is not canonical JSON") from exc


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchTrigger:
    """A deterministic external trigger; the scheduler itself is not durable truth."""

    trigger_id: str
    trigger_type: str
    question_id: str
    requested_at: str
    source_id: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("trigger_id", "trigger_type", "question_id", "source_id"):
            _text(name, getattr(self, name))
        _timestamp("requested_at", self.requested_at)
        _sha256("request_fingerprint", self.request_fingerprint)

    @property
    def canonical_id(self) -> str:
        return _digest(
            {
                "trigger_id": self.trigger_id,
                "trigger_type": self.trigger_type,
                "question_id": self.question_id,
                "requested_at": _timestamp("requested_at", self.requested_at),
                "source_id": self.source_id,
                "request_fingerprint": self.request_fingerprint.lower(),
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type,
            "question_id": self.question_id,
            "requested_at": _timestamp("requested_at", self.requested_at),
            "source_id": self.source_id,
            "request_fingerprint": self.request_fingerprint.lower(),
        }


@dataclass(frozen=True, slots=True)
class ResearchSupervisorState:
    """Durable state machine snapshot for one research Run."""

    run_id: str
    trigger_id: str
    supervisor_id: str
    question_id: str
    phase: ResearchPhase
    protocol_id: str | None
    checkpoint_id: str | None
    budget_units: int
    consumed_units: int
    deadline: str | None
    cancelled: bool
    updated_at: str
    state_sha256: str

    def __post_init__(self) -> None:
        for name in ("run_id", "trigger_id", "supervisor_id", "question_id"):
            _text(name, getattr(self, name))
        if not isinstance(self.phase, ResearchPhase):
            raise ResearchSupervisorError("phase must be ResearchPhase")
        if self.protocol_id is not None:
            _text("protocol_id", self.protocol_id)
        if self.checkpoint_id is not None:
            _sha256("checkpoint_id", self.checkpoint_id)
        if isinstance(self.budget_units, bool) or not isinstance(self.budget_units, int) or self.budget_units < 0:
            raise ResearchSupervisorError("budget_units must be a non-negative integer")
        if isinstance(self.consumed_units, bool) or not isinstance(self.consumed_units, int) or self.consumed_units < 0:
            raise ResearchSupervisorError("consumed_units must be a non-negative integer")
        if self.consumed_units > self.budget_units:
            raise ResearchSupervisorError("consumed_units cannot exceed budget_units")
        if self.deadline is not None:
            _timestamp("deadline", self.deadline)
        if type(self.cancelled) is not bool:
            raise ResearchSupervisorError("cancelled must be boolean")
        _timestamp("updated_at", self.updated_at)
        _sha256("state_sha256", self.state_sha256)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trigger_id": self.trigger_id,
            "supervisor_id": self.supervisor_id,
            "question_id": self.question_id,
            "phase": self.phase.value,
            "protocol_id": self.protocol_id,
            "checkpoint_id": self.checkpoint_id,
            "budget_units": self.budget_units,
            "consumed_units": self.consumed_units,
            "deadline": None if self.deadline is None else _timestamp("deadline", self.deadline),
            "cancelled": self.cancelled,
            "updated_at": _timestamp("updated_at", self.updated_at),
        }

    @property
    def computed_sha256(self) -> str:
        return _digest(self.canonical_payload())

    def verify(self) -> "ResearchSupervisorState":
        if self.computed_sha256 != self.state_sha256:
            raise ResearchSupervisorError("research supervisor state digest mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        trigger_id: str,
        supervisor_id: str,
        question_id: str,
        budget_units: int,
        deadline: str | None,
        protocol_id: str | None = None,
        updated_at: str,
    ) -> "ResearchSupervisorState":
        base = cls(
            run_id=run_id,
            trigger_id=trigger_id,
            supervisor_id=supervisor_id,
            question_id=question_id,
            phase=ResearchPhase.QUESTION,
            protocol_id=protocol_id,
            checkpoint_id=None,
            budget_units=budget_units,
            consumed_units=0,
            deadline=deadline,
            cancelled=False,
            updated_at=updated_at,
            state_sha256="0" * 64,
        )
        return replace(base, state_sha256=base.computed_sha256)

    def transition(self, next_phase: ResearchPhase, *, updated_at: str) -> "ResearchSupervisorState":
        if not isinstance(next_phase, ResearchPhase):
            raise InvalidResearchTransitionError("next_phase must be ResearchPhase")
        if self.cancelled:
            raise InvalidResearchTransitionError("cancelled supervisor cannot advance")
        if _PHASE_INDEX[next_phase] != _PHASE_INDEX[self.phase] + 1:
            raise InvalidResearchTransitionError(
                f"invalid phase transition {self.phase.value}->{next_phase.value}"
            )
        changed = replace(self, phase=next_phase, updated_at=_timestamp("updated_at", updated_at), state_sha256="0" * 64)
        return replace(changed, state_sha256=changed.computed_sha256)

    def consume_budget(self, units: int, *, updated_at: str) -> "ResearchSupervisorState":
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise ResearchSupervisorError("units must be a non-negative integer")
        if self.consumed_units + units > self.budget_units:
            raise ResearchSupervisorError("research supervisor budget exhausted")
        changed = replace(
            self,
            consumed_units=self.consumed_units + units,
            updated_at=_timestamp("updated_at", updated_at),
            state_sha256="0" * 64,
        )
        return replace(changed, state_sha256=changed.computed_sha256)

    def cancel(self, *, updated_at: str) -> "ResearchSupervisorState":
        if self.cancelled:
            return self
        changed = replace(self, cancelled=True, updated_at=_timestamp("updated_at", updated_at), state_sha256="0" * 64)
        return replace(changed, state_sha256=changed.computed_sha256)

    def checkpoint(self, *, checkpoint_id: str, updated_at: str) -> "ResearchSupervisorState":
        changed = replace(
            self,
            checkpoint_id=_sha256("checkpoint_id", checkpoint_id),
            updated_at=_timestamp("updated_at", updated_at),
            state_sha256="0" * 64,
        )
        return replace(changed, state_sha256=changed.computed_sha256)

    def to_payload(self) -> dict[str, Any]:
        self.verify()
        payload = self.canonical_payload()
        payload["state_sha256"] = self.state_sha256
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "ResearchSupervisorState":
        if type(payload) is not dict:
            raise ResearchSupervisorError("state payload must be an object")
        required = {
            "run_id", "trigger_id", "supervisor_id", "question_id", "phase",
            "protocol_id", "checkpoint_id", "budget_units", "consumed_units",
            "deadline", "cancelled", "updated_at", "state_sha256",
        }
        if set(payload) != required:
            raise ResearchSupervisorError("state payload fields mismatch")
        try:
            state = cls(
                run_id=payload["run_id"],
                trigger_id=payload["trigger_id"],
                supervisor_id=payload["supervisor_id"],
                question_id=payload["question_id"],
                phase=ResearchPhase(payload["phase"]),
                protocol_id=payload["protocol_id"],
                checkpoint_id=payload["checkpoint_id"],
                budget_units=payload["budget_units"],
                consumed_units=payload["consumed_units"],
                deadline=payload["deadline"],
                cancelled=payload["cancelled"],
                updated_at=payload["updated_at"],
                state_sha256=payload["state_sha256"],
            )
        except (KeyError, TypeError) as exc:
            raise ResearchSupervisorError("invalid state payload") from exc
        return state.verify()


class ResearchSupervisorExecutor(Protocol):
    """Optional application seam; scientific truth stays in canonical executors."""

    def resume(self, state: ResearchSupervisorState) -> ResearchSupervisorState: ...


class ResearchSupervisorStore:
    """Atomic durable trigger/run state store; no scheduler or scientific registry semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._read()

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "ResearchSupervisorStore":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                atomic_write_json(
                    target,
                    {"schema_version": SCHEMA_VERSION, "triggers": {}, "runs": {}},
                )
        return cls(target)

    def _read(self) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except json.JSONDecodeError as exc:
            raise ResearchSupervisorError("supervisor store must contain valid JSON") from exc
        if type(state) is not dict or state.get("schema_version") != SCHEMA_VERSION:
            raise ResearchSupervisorError("supervisor store schema_version mismatch")
        if type(state.get("triggers")) is not dict or type(state.get("runs")) is not dict:
            raise ResearchSupervisorError("supervisor store collections must be objects")
        for run_id, payload in state["runs"].items():
            if type(run_id) is not str or type(payload) is not dict:
                raise ResearchSupervisorError("invalid supervisor run entry")
            ResearchSupervisorState.from_payload(payload)
        return state

    @staticmethod
    def _trigger_record(trigger: ResearchTrigger) -> dict[str, Any]:
        return {
            "canonical_id": trigger.canonical_id,
            "payload": trigger.to_payload(),
        }

    def start_or_resume(
        self,
        trigger: ResearchTrigger,
        *,
        supervisor_id: str,
        run_id: str,
        budget_units: int,
        deadline: str | None,
        updated_at: str,
        protocol_id: str | None = None,
    ) -> ResearchSupervisorState:
        """Collapse repeated delivery of one trigger into one durable run."""

        _text("run_id", run_id)
        _text("supervisor_id", supervisor_id)
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            existing_trigger = state["triggers"].get(trigger.trigger_id)
            canonical = self._trigger_record(trigger)
            if existing_trigger is not None:
                if existing_trigger != canonical:
                    raise ConflictingResearchTriggerError(
                        f"trigger identity {trigger.trigger_id!r} already binds different work"
                    )
                bound_run_id = existing_trigger["run_id"]
                existing = state["runs"].get(bound_run_id)
                if existing is None:
                    raise ResearchSupervisorError("trigger references missing durable run")
                return ResearchSupervisorState.from_payload(existing)

            if run_id in state["runs"]:
                raise ResearchSupervisorError("run_id already belongs to a different trigger")

            run_state = ResearchSupervisorState.create(
                run_id=run_id,
                trigger_id=trigger.trigger_id,
                supervisor_id=supervisor_id,
                question_id=trigger.question_id,
                budget_units=budget_units,
                deadline=deadline,
                protocol_id=protocol_id,
                updated_at=updated_at,
            )
            trigger_record = dict(canonical)
            trigger_record["run_id"] = run_id
            state["triggers"][trigger.trigger_id] = trigger_record
            state["runs"][run_id] = run_state.to_payload()
            atomic_write_json(self.path, state)
            self._read()
            return run_state

    def get_run(self, run_id: str) -> ResearchSupervisorState | None:
        _text("run_id", run_id)
        state = self._read()
        payload = state["runs"].get(run_id)
        return None if payload is None else ResearchSupervisorState.from_payload(payload)

    def get_trigger_run(self, trigger_id: str) -> ResearchSupervisorState | None:
        _text("trigger_id", trigger_id)
        state = self._read()
        record = state["triggers"].get(trigger_id)
        if record is None:
            return None
        run_id = record.get("run_id")
        if not isinstance(run_id, str):
            raise ResearchSupervisorError("trigger run binding is invalid")
        return self.get_run(run_id)

    def persist(self, updated: ResearchSupervisorState) -> ResearchSupervisorState:
        updated.verify()
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            current_payload = state["runs"].get(updated.run_id)
            if current_payload is None:
                raise ResearchSupervisorError("cannot persist unknown run")
            current = ResearchSupervisorState.from_payload(current_payload)
            if current.trigger_id != updated.trigger_id:
                raise ResearchSupervisorError("run trigger identity cannot change")
            state["runs"][updated.run_id] = updated.to_payload()
            atomic_write_json(self.path, state)
            self._read()
        return updated

    def snapshot(self, run_id: str) -> dict[str, Any]:
        state = self.get_run(run_id)
        if state is None:
            raise ResearchSupervisorError("unknown run")
        return {
            "run_id": state.run_id,
            "trigger_id": state.trigger_id,
            "supervisor_id": state.supervisor_id,
            "question_id": state.question_id,
            "phase": state.phase.value,
            "protocol_id": state.protocol_id,
            "checkpoint_id": state.checkpoint_id,
            "budget_units": state.budget_units,
            "consumed_units": state.consumed_units,
            "remaining_budget_units": state.budget_units - state.consumed_units,
            "deadline": state.deadline,
            "cancelled": state.cancelled,
            "updated_at": state.updated_at,
            "state_sha256": state.state_sha256,
            "next_phase": (
                _PHASE_ORDER[_PHASE_INDEX[state.phase] + 1].value
                if _PHASE_INDEX[state.phase] + 1 < len(_PHASE_ORDER)
                else None
            ),
        }
