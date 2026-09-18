"""Restart-safe orchestration state for the Autosport research lifecycle.

The supervisor is deliberately a thin durable control seam. It does not replace
ScientificRegistry, Strategy/Model Factory, the causal learning environment, or a
scheduler. External triggers collapse idempotently into one durable run and the
run may move only through the frozen scientific lifecycle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


SUPERVISOR_SCHEMA = "autosport.research_supervisor"
SUPERVISOR_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class ResearchSupervisorError(RuntimeError):
    """Base error for durable research-supervisor state."""


class ConflictingResearchTriggerError(ResearchSupervisorError):
    """A trigger identity was reused with different immutable content."""


class StaleResearchCheckpointError(ResearchSupervisorError):
    """A caller attempted to advance from a checkpoint that is no longer current."""


class ResearchSupervisorLimitReached(ResearchSupervisorError):
    """A run was stopped because a configured budget/deadline was reached."""


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
    DECISION = "DECISION"
    POSTMORTEM = "POSTMORTEM"
    MEMORY = "MEMORY"
    NEXT_QUESTION = "NEXT_QUESTION"
    COMPLETE = "COMPLETE"


_PHASE_SEQUENCE = tuple(ResearchPhase)
_NEXT_PHASE = {
    phase: _PHASE_SEQUENCE[index + 1]
    for index, phase in enumerate(_PHASE_SEQUENCE[:-1])
}


class SupervisorStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


_SCIENTIFIC_BINDINGS = {
    "hypothesis_id": "Hypothesis",
    "research_protocol_id": "ResearchProtocol",
    "dataset_snapshot_id": "DatasetSnapshot",
    "model_version_id": "ModelVersion",
    "strategy_version_id": "StrategyVersion",
    "evaluation_bundle_id": "EvaluationBundle",
    "experiment_id": "Experiment",
    "promotion_decision_id": "PromotionDecision",
    "postmortem_id": "Postmortem",
    "drift_finding_id": "DriftFinding",
    "next_question_id": "ResearchQuestion",
}
_SHA_BINDINGS = frozenset(
    {
        "environment_id",
        "environment_checkpoint_id",
        "factory_artifact_sha256",
        "evaluation_evidence_sha256",
    }
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    value.encode("utf-8")
    return value


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_identity(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be canonical SHA-256 hex")
    return text


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _binding_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise ValueError("bindings must be a tuple")
    normalized: list[tuple[str, str]] = []
    for entry in value:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError("bindings entries must be two-item tuples")
        key = _text(entry[0], "binding key")
        item = _text(entry[1], f"binding {key}")
        normalized.append((key, item))
    if normalized != sorted(normalized):
        raise ValueError("bindings must be sorted")
    keys = [key for key, _ in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("bindings keys must be unique")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class ResearchTrigger:
    trigger_id: str
    question_id: str
    requested_at: str
    budget_units: int
    deadline_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.trigger_id, "trigger_id")
        _text(self.question_id, "question_id")
        requested = _instant(self.requested_at, "requested_at")
        if isinstance(self.budget_units, bool) or not isinstance(self.budget_units, int):
            raise ValueError("budget_units must be an integer")
        if self.budget_units <= 0:
            raise ValueError("budget_units must be positive")
        if self.deadline_at is not None:
            deadline = _instant(self.deadline_at, "deadline_at")
            if deadline < requested:
                raise ValueError("deadline_at cannot precede requested_at")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "trigger_id": self.trigger_id,
            "question_id": self.question_id,
            "requested_at": _timestamp_identity(self.requested_at, "requested_at"),
            "budget_units": self.budget_units,
            "deadline_at": (
                None
                if self.deadline_at is None
                else _timestamp_identity(self.deadline_at, "deadline_at")
            ),
        }

    @property
    def trigger_sha256(self) -> str:
        return _digest(self.canonical_payload())

    @property
    def run_id(self) -> str:
        return _digest(
            {
                "schema": SUPERVISOR_SCHEMA,
                "schema_version": SUPERVISOR_SCHEMA_VERSION,
                "trigger_sha256": self.trigger_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    run_id: str
    trigger_id: str
    question_id: str
    phase: ResearchPhase
    status: SupervisorStatus
    budget_units: int
    consumed_budget_units: int
    deadline_at: str | None
    checkpoint_index: int
    checkpoint_sha256: str
    bindings: tuple[tuple[str, str], ...]
    stop_reason: str | None
    created_at: str
    updated_at: str


class ResearchSupervisor:
    """Durable idempotent control seam for one scientific-research workspace."""

    def __init__(self, path: str | Path, scientific_registry: ScientificRegistry) -> None:
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        self.path = Path(path)
        self.scientific_registry = scientific_registry
        if self.path.parent.resolve() != scientific_registry.path.parent.resolve():
            raise ValueError(
                "research supervisor and scientific registry must share one workspace"
            )
        try:
            self._read()
        except FileNotFoundError as exc:
            raise ValueError("research supervisor state is missing") from exc

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        scientific_registry: ScientificRegistry,
    ) -> "ResearchSupervisor":
        target = Path(path)
        if target.parent.resolve() != scientific_registry.path.parent.resolve():
            raise ValueError(
                "research supervisor and scientific registry must share one workspace"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                cls._write_state(
                    target,
                    {
                        "schema": SUPERVISOR_SCHEMA,
                        "schema_version": SUPERVISOR_SCHEMA_VERSION,
                        "runs": [],
                    },
                )
        return cls(target, scientific_registry)

    @staticmethod
    def _write_state(path: Path, state_without_digest: dict[str, Any]) -> None:
        state = dict(state_without_digest)
        state["state_sha256"] = _digest(state_without_digest)
        atomic_write_json(path, state)

    @staticmethod
    def _run_without_digest(run: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in run.items() if key != "run_sha256"}

    @classmethod
    def _seal_run(cls, run: dict[str, Any]) -> dict[str, Any]:
        sealed = cls._run_without_digest(run)
        sealed["run_sha256"] = _digest(sealed)
        return sealed

    def _read(self) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("research supervisor state must be valid JSON") from exc
        if type(state) is not dict:
            raise ValueError("research supervisor state must be an object")
        required = {"schema", "schema_version", "runs", "state_sha256"}
        if set(state) != required:
            raise ValueError("research supervisor state fields mismatch")
        if state["schema"] != SUPERVISOR_SCHEMA:
            raise ValueError("research supervisor schema mismatch")
        if state["schema_version"] != SUPERVISOR_SCHEMA_VERSION:
            raise ValueError("research supervisor schema_version mismatch")
        if type(state["runs"]) is not list:
            raise ValueError("research supervisor runs must be a list")
        expected_state_sha = _digest(
            {
                "schema": state["schema"],
                "schema_version": state["schema_version"],
                "runs": state["runs"],
            }
        )
        if _sha256(state["state_sha256"], "state_sha256") != expected_state_sha:
            raise ValueError("research supervisor state digest mismatch")

        seen_runs: set[str] = set()
        seen_triggers: dict[str, str] = {}
        for run in state["runs"]:
            self._validate_run(run)
            if run["run_id"] in seen_runs:
                raise ValueError("research supervisor contains duplicate run identity")
            seen_runs.add(run["run_id"])
            prior = seen_triggers.get(run["trigger_id"])
            if prior is not None and prior != run["trigger_sha256"]:
                raise ValueError("research supervisor contains conflicting trigger identity")
            if prior is not None:
                raise ValueError("research supervisor contains duplicate trigger identity")
            seen_triggers[run["trigger_id"]] = run["trigger_sha256"]
        if [run["run_id"] for run in state["runs"]] != sorted(seen_runs):
            raise ValueError("research supervisor runs must be sorted by run_id")
        return state

    @classmethod
    def _validate_run(cls, run: object) -> None:
        if type(run) is not dict:
            raise ValueError("research supervisor run must be an object")
        required = {
            "run_id",
            "trigger_id",
            "trigger_sha256",
            "question_id",
            "phase",
            "status",
            "created_at",
            "updated_at",
            "deadline_at",
            "budget_units",
            "consumed_budget_units",
            "checkpoint_index",
            "bindings",
            "stop_reason",
            "run_sha256",
        }
        if set(run) != required:
            raise ValueError("research supervisor run fields mismatch")
        run_id = _sha256(run["run_id"], "run_id")
        trigger_id = _text(run["trigger_id"], "trigger_id")
        trigger_sha256 = _sha256(run["trigger_sha256"], "trigger_sha256")
        question_id = _text(run["question_id"], "question_id")
        phase = ResearchPhase(run["phase"])
        status = SupervisorStatus(run["status"])
        created = _instant(run["created_at"], "created_at")
        updated = _instant(run["updated_at"], "updated_at")
        if updated < created:
            raise ValueError("research supervisor updated_at precedes created_at")
        if run["deadline_at"] is not None:
            deadline = _instant(run["deadline_at"], "deadline_at")
            if deadline < created:
                raise ValueError("research supervisor deadline precedes created_at")
        for field in ("budget_units", "consumed_budget_units", "checkpoint_index"):
            value = run[field]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{field} must be an integer")
        if run["budget_units"] <= 0:
            raise ValueError("budget_units must be positive")
        if not 0 <= run["consumed_budget_units"] <= run["budget_units"]:
            raise ValueError("consumed_budget_units is outside budget")
        canonical_deadline = (
            None
            if run["deadline_at"] is None
            else _timestamp_identity(run["deadline_at"], "deadline_at")
        )
        expected_trigger_sha256 = _digest(
            {
                "trigger_id": trigger_id,
                "question_id": question_id,
                "requested_at": _timestamp_identity(run["created_at"], "created_at"),
                "budget_units": run["budget_units"],
                "deadline_at": canonical_deadline,
            }
        )
        if trigger_sha256 != expected_trigger_sha256:
            raise ValueError("research supervisor trigger digest mismatch")
        expected_run_id = _digest(
            {
                "schema": SUPERVISOR_SCHEMA,
                "schema_version": SUPERVISOR_SCHEMA_VERSION,
                "trigger_sha256": trigger_sha256,
            }
        )
        if run_id != expected_run_id:
            raise ValueError("research supervisor run identity mismatch")
        if run["checkpoint_index"] < 0:
            raise ValueError("checkpoint_index must be non-negative")
        if type(run["bindings"]) is not dict:
            raise ValueError("bindings must be an object")
        for key, value in run["bindings"].items():
            _text(key, "binding key")
            _text(value, f"binding {key}")
        if list(run["bindings"]) != sorted(run["bindings"]):
            raise ValueError("bindings must be sorted by key")
        if run["stop_reason"] is not None:
            _text(run["stop_reason"], "stop_reason")
        if status is SupervisorStatus.COMPLETED and phase is not ResearchPhase.COMPLETE:
            raise ValueError("completed run must be in COMPLETE phase")
        if phase is ResearchPhase.COMPLETE and status is not SupervisorStatus.COMPLETED:
            raise ValueError("COMPLETE phase must have completed status")
        if status is SupervisorStatus.STOPPED and run["stop_reason"] is None:
            raise ValueError("stopped run requires stop_reason")
        if status is not SupervisorStatus.STOPPED and run["stop_reason"] is not None:
            raise ValueError("only stopped run may carry stop_reason")
        expected = _digest(cls._run_without_digest(run))
        if _sha256(run["run_sha256"], "run_sha256") != expected:
            raise ValueError("research supervisor run digest mismatch")

    @staticmethod
    def _state_without_digest(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "runs": state["runs"],
        }

    @staticmethod
    def _find_run(state: dict[str, Any], run_id: str) -> dict[str, Any]:
        wanted = _sha256(run_id, "run_id")
        for run in state["runs"]:
            if run["run_id"] == wanted:
                return run
        raise KeyError(wanted)

    @staticmethod
    def _snapshot(run: dict[str, Any]) -> SupervisorSnapshot:
        return SupervisorSnapshot(
            run_id=run["run_id"],
            trigger_id=run["trigger_id"],
            question_id=run["question_id"],
            phase=ResearchPhase(run["phase"]),
            status=SupervisorStatus(run["status"]),
            budget_units=run["budget_units"],
            consumed_budget_units=run["consumed_budget_units"],
            deadline_at=run["deadline_at"],
            checkpoint_index=run["checkpoint_index"],
            checkpoint_sha256=run["run_sha256"],
            bindings=tuple(sorted(run["bindings"].items())),
            stop_reason=run["stop_reason"],
            created_at=run["created_at"],
            updated_at=run["updated_at"],
        )

    def _validate_bindings(
        self,
        bindings: tuple[tuple[str, str], ...],
        *,
        as_of: str,
    ) -> tuple[tuple[str, str], ...]:
        values = _binding_pairs(bindings)
        cutoff = _instant(as_of, "binding as_of")
        normalized: list[tuple[str, str]] = []
        for key, value in values:
            record_type = _SCIENTIFIC_BINDINGS.get(key)
            if record_type is not None:
                entry = self.scientific_registry.get(record_type, value)
                if entry is None:
                    raise ResearchSupervisorError(
                        f"binding references missing {record_type}:{value}"
                    )
                if _instant(entry.available_at, f"{record_type}.available_at") > cutoff:
                    raise ResearchSupervisorError(
                        f"binding references future {record_type}:{value}"
                    )
                reveal_after = entry.reveal_after
                if reveal_after is not None and _instant(
                    reveal_after, f"{record_type}.reveal_after"
                ) > cutoff:
                    raise ResearchSupervisorError(
                        f"binding references unrevealed {record_type}:{value}"
                    )
                normalized.append((key, value))
            elif key in _SHA_BINDINGS:
                normalized.append((key, _sha256(value, key)))
            else:
                raise ResearchSupervisorError(f"unsupported supervisor binding: {key}")
        return tuple(normalized)

    def accept_trigger(self, trigger: ResearchTrigger) -> SupervisorSnapshot:
        if not isinstance(trigger, ResearchTrigger):
            raise TypeError("trigger must be ResearchTrigger")
        question = self.scientific_registry.get("ResearchQuestion", trigger.question_id)
        if question is None:
            raise ResearchSupervisorError(
                f"trigger references missing ResearchQuestion:{trigger.question_id}"
            )
        payload = trigger.canonical_payload()
        requested_at = payload["requested_at"]
        if _instant(question.available_at, "ResearchQuestion.available_at") > _instant(
            requested_at, "requested_at"
        ):
            raise ResearchSupervisorError(
                "trigger cannot reference a research question from the future"
            )
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            for existing in state["runs"]:
                if existing["trigger_id"] != trigger.trigger_id:
                    continue
                if existing["trigger_sha256"] != trigger.trigger_sha256:
                    raise ConflictingResearchTriggerError(
                        "trigger_id is already bound to different immutable content"
                    )
                return self._snapshot(existing)

            run = self._seal_run(
                {
                    "run_id": trigger.run_id,
                    "trigger_id": trigger.trigger_id,
                    "trigger_sha256": trigger.trigger_sha256,
                    "question_id": trigger.question_id,
                    "phase": ResearchPhase.QUESTION.value,
                    "status": SupervisorStatus.ACTIVE.value,
                    "created_at": requested_at,
                    "updated_at": requested_at,
                    "deadline_at": payload["deadline_at"],
                    "budget_units": trigger.budget_units,
                    "consumed_budget_units": 0,
                    "checkpoint_index": 0,
                    "bindings": {},
                    "stop_reason": None,
                }
            )
            state["runs"].append(run)
            state["runs"].sort(key=lambda item: item["run_id"])
            self._write_state(self.path, self._state_without_digest(state))
            return self._snapshot(run)

    def status(self, run_id: str) -> SupervisorSnapshot:
        state = self._read()
        return self._snapshot(self._find_run(state, run_id))

    def list_runs(self) -> tuple[SupervisorSnapshot, ...]:
        return tuple(self._snapshot(run) for run in self._read()["runs"])

    def advance(
        self,
        run_id: str,
        *,
        expected_phase: ResearchPhase,
        at: str,
        budget_cost: int = 1,
        bindings: tuple[tuple[str, str], ...] = (),
    ) -> SupervisorSnapshot:
        if not isinstance(expected_phase, ResearchPhase):
            raise TypeError("expected_phase must be ResearchPhase")
        if expected_phase is ResearchPhase.COMPLETE:
            raise ValueError("COMPLETE phase cannot advance")
        if isinstance(budget_cost, bool) or not isinstance(budget_cost, int):
            raise ValueError("budget_cost must be an integer")
        if budget_cost <= 0:
            raise ValueError("budget_cost must be positive")
        now = _timestamp_identity(at, "at")
        now_instant = _instant(now, "at")
        validated_bindings = self._validate_bindings(bindings, as_of=now)

        limit_error: str | None = None
        snapshot: SupervisorSnapshot | None = None
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            run = self._find_run(state, run_id)
            if ResearchPhase(run["phase"]) is not expected_phase:
                raise StaleResearchCheckpointError(
                    f"expected phase {expected_phase.value}, current phase is {run['phase']}"
                )
            if SupervisorStatus(run["status"]) is not SupervisorStatus.ACTIVE:
                raise ResearchSupervisorError(f"run is not active: {run['status']}")
            if now_instant < _instant(run["updated_at"], "updated_at"):
                raise ValueError("advance timestamp cannot move backwards")
            deadline = run["deadline_at"]
            if deadline is not None and now_instant > _instant(deadline, "deadline_at"):
                run["status"] = SupervisorStatus.STOPPED.value
                run["stop_reason"] = "DEADLINE_EXPIRED"
                limit_error = "research run deadline expired"
            elif run["consumed_budget_units"] + budget_cost > run["budget_units"]:
                run["status"] = SupervisorStatus.STOPPED.value
                run["stop_reason"] = "BUDGET_EXHAUSTED"
                limit_error = "research run budget exhausted"
            else:
                merged = dict(run["bindings"])
                for key, value in validated_bindings:
                    existing = merged.get(key)
                    if existing is not None and existing != value:
                        raise ResearchSupervisorError(
                            f"immutable binding conflict for {key}"
                        )
                    merged[key] = value
                run["bindings"] = dict(sorted(merged.items()))
                run["consumed_budget_units"] += budget_cost
                run["phase"] = _NEXT_PHASE[expected_phase].value
                if ResearchPhase(run["phase"]) is ResearchPhase.COMPLETE:
                    run["status"] = SupervisorStatus.COMPLETED.value
            run["checkpoint_index"] += 1
            run["updated_at"] = now
            sealed = self._seal_run(run)
            run.clear()
            run.update(sealed)
            self._write_state(self.path, self._state_without_digest(state))
            snapshot = self._snapshot(run)

        if limit_error is not None:
            raise ResearchSupervisorLimitReached(limit_error)
        assert snapshot is not None
        return snapshot

    def pause(self, run_id: str, *, at: str) -> SupervisorSnapshot:
        return self._set_status(
            run_id,
            expected=SupervisorStatus.ACTIVE,
            target=SupervisorStatus.PAUSED,
            at=at,
        )

    def resume(self, run_id: str, *, at: str) -> SupervisorSnapshot:
        return self._set_status(
            run_id,
            expected=SupervisorStatus.PAUSED,
            target=SupervisorStatus.ACTIVE,
            at=at,
        )

    def stop(self, run_id: str, *, at: str, reason: str) -> SupervisorSnapshot:
        stop_reason = _text(reason, "reason")
        now = _timestamp_identity(at, "at")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            run = self._find_run(state, run_id)
            status = SupervisorStatus(run["status"])
            if status is SupervisorStatus.COMPLETED:
                raise ResearchSupervisorError("completed run cannot be stopped")
            if status is SupervisorStatus.STOPPED:
                if run["stop_reason"] != stop_reason:
                    raise ResearchSupervisorError("run is already stopped for another reason")
                return self._snapshot(run)
            if _instant(now, "at") < _instant(run["updated_at"], "updated_at"):
                raise ValueError("stop timestamp cannot move backwards")
            run["status"] = SupervisorStatus.STOPPED.value
            run["stop_reason"] = stop_reason
            run["checkpoint_index"] += 1
            run["updated_at"] = now
            sealed = self._seal_run(run)
            run.clear()
            run.update(sealed)
            self._write_state(self.path, self._state_without_digest(state))
            return self._snapshot(run)

    def _set_status(
        self,
        run_id: str,
        *,
        expected: SupervisorStatus,
        target: SupervisorStatus,
        at: str,
    ) -> SupervisorSnapshot:
        now = _timestamp_identity(at, "at")
        now_instant = _instant(now, "at")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            run = self._find_run(state, run_id)
            status = SupervisorStatus(run["status"])
            if status is not expected:
                raise ResearchSupervisorError(
                    f"cannot transition supervisor status {status.value} -> {target.value}"
                )
            if now_instant < _instant(run["updated_at"], "updated_at"):
                raise ValueError("status timestamp cannot move backwards")
            limit_error: str | None = None
            if target is SupervisorStatus.ACTIVE:
                deadline = run["deadline_at"]
                if deadline is not None and now_instant > _instant(deadline, "deadline_at"):
                    run["status"] = SupervisorStatus.STOPPED.value
                    run["stop_reason"] = "DEADLINE_EXPIRED"
                    limit_error = "cannot resume after research run deadline"
                elif run["consumed_budget_units"] >= run["budget_units"]:
                    run["status"] = SupervisorStatus.STOPPED.value
                    run["stop_reason"] = "BUDGET_EXHAUSTED"
                    limit_error = "cannot resume exhausted research run budget"
            if limit_error is None:
                run["status"] = target.value
            run["checkpoint_index"] += 1
            run["updated_at"] = now
            sealed = self._seal_run(run)
            run.clear()
            run.update(sealed)
            self._write_state(self.path, self._state_without_digest(state))
            snapshot = self._snapshot(run)
        if limit_error is not None:
            raise ResearchSupervisorLimitReached(limit_error)
        return snapshot
