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
from weakref import WeakKeyDictionary

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
    "feature_set_id": "FeatureSet",
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


def _scientific_registry_binding_descriptor():
    bindings: WeakKeyDictionary[
        object, tuple[ScientificRegistry, Path, Path, Path, Path]
    ] = WeakKeyDictionary()

    class _ScientificRegistryBinding:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            bound = bindings.get(instance)
            if bound is None:
                raise ResearchSupervisorError(
                    "scientific registry authority is unbound"
                )
            (
                registry,
                supervisor_path,
                registry_path,
                resolved_supervisor_path,
                resolved_registry_path,
            ) = bound
            current_supervisor_path = Path(instance.path)
            current_registry_path = Path(registry.path)
            try:
                current_resolved_supervisor_path = current_supervisor_path.resolve()
                current_resolved_registry_path = current_registry_path.resolve()
            except (OSError, RuntimeError) as exc:
                raise ResearchSupervisorError(
                    "scientific registry authority path cannot be resolved"
                ) from exc
            if (
                current_supervisor_path != supervisor_path
                or current_registry_path != registry_path
                or current_resolved_supervisor_path != resolved_supervisor_path
                or current_resolved_registry_path != resolved_registry_path
            ):
                raise ResearchSupervisorError(
                    "scientific registry authority binding changed"
                )
            return registry

        def __set__(self, instance, value) -> None:
            if not isinstance(value, ScientificRegistry):
                raise TypeError("scientific_registry must be ScientificRegistry")
            supervisor_path = Path(instance.path)
            registry_path = Path(value.path)
            try:
                resolved_supervisor_path = supervisor_path.resolve()
                resolved_registry_path = registry_path.resolve()
            except (OSError, RuntimeError) as exc:
                raise ResearchSupervisorError(
                    "scientific registry authority path cannot be resolved"
                ) from exc
            binding = (
                value,
                supervisor_path,
                registry_path,
                resolved_supervisor_path,
                resolved_registry_path,
            )
            current = bindings.get(instance)
            if current is None:
                bindings[instance] = binding
                return
            if current == binding:
                return
            raise ResearchSupervisorError(
                "scientific registry authority binding is immutable"
            )

        def __delete__(self, instance) -> None:
            raise ResearchSupervisorError(
                "scientific registry authority binding is immutable"
            )

    return _ScientificRegistryBinding()


_SCIENTIFIC_REGISTRY_BINDING = _scientific_registry_binding_descriptor()


class ResearchSupervisor:
    """Durable idempotent control seam for one scientific-research workspace."""

    scientific_registry = _SCIENTIFIC_REGISTRY_BINDING

    def _assert_scientific_registry_class_binding(self) -> None:
        live_binding = None
        for owner in type(self).__mro__:
            if "scientific_registry" in owner.__dict__:
                live_binding = owner.__dict__["scientific_registry"]
                break
        if live_binding is not _SCIENTIFIC_REGISTRY_BINDING:
            raise ResearchSupervisorError(
                "scientific registry authority class binding changed"
            )

    def _scientific_registry_authority(self) -> ScientificRegistry:
        self._assert_scientific_registry_class_binding()
        return _SCIENTIFIC_REGISTRY_BINDING.__get__(self, type(self))

    def __init__(self, path: str | Path, scientific_registry: ScientificRegistry) -> None:
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        self._assert_scientific_registry_class_binding()
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
            self._validate_scientific_lineage(
                run_question_id=run["question_id"],
                bindings=run["bindings"],
            )
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

    @classmethod
    def _acceptance_run(cls, trigger: ResearchTrigger) -> dict[str, Any]:
        """Reconstruct the immutable checkpoint created when a trigger is accepted."""

        if not isinstance(trigger, ResearchTrigger):
            raise TypeError("trigger must be ResearchTrigger")
        payload = trigger.canonical_payload()
        requested_at = payload["requested_at"]
        return cls._seal_run(
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

    @classmethod
    def acceptance_checkpoint_sha256(cls, trigger: ResearchTrigger) -> str:
        """Return the immutable checkpoint digest for first acceptance of trigger."""

        return cls._acceptance_run(trigger)["run_sha256"]

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
                entry = self._scientific_registry_authority().get(record_type, value)
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

    def _validate_scientific_lineage(
        self,
        *,
        run_question_id: str,
        bindings: dict[str, str],
    ) -> None:
        """Fail closed when individually valid scientific IDs cross causal lineages."""

        question_id = _text(run_question_id, "run_question_id")
        question = self._scientific_registry_authority().get("ResearchQuestion", question_id)
        if question is None:
            raise ResearchSupervisorError(
                f"run references missing ResearchQuestion:{question_id}"
            )
        question_payload_sha256 = _digest(question.payload)

        hypothesis_id = bindings.get("hypothesis_id")
        hypothesis = None
        if hypothesis_id is not None:
            hypothesis = self._scientific_registry_authority().get("Hypothesis", hypothesis_id)
            if hypothesis is None:
                raise ResearchSupervisorError(
                    f"binding references missing Hypothesis:{hypothesis_id}"
                )
            if hypothesis.payload.get("research_question_id") != question_id:
                raise ResearchSupervisorError(
                    "hypothesis research question does not match supervisor run"
                )

        protocol_id = bindings.get("research_protocol_id")
        protocol = None
        protocol_binding: dict[str, Any] | None = None
        if protocol_id is not None:
            if hypothesis is None:
                raise ResearchSupervisorError(
                    "research protocol requires explicit supervisor hypothesis binding"
                )

            protocol = self._scientific_registry_authority().get("ResearchProtocol", protocol_id)
            if protocol is None:
                raise ResearchSupervisorError(
                    f"binding references missing ResearchProtocol:{protocol_id}"
                )
            protocol_binding = protocol.payload.get("binding")
            if type(protocol_binding) is not dict:
                raise ResearchSupervisorError(
                    "research protocol lacks canonical scientific binding"
                )
            if protocol_binding.get("research_question_id") != question_id:
                raise ResearchSupervisorError(
                    "research protocol question does not match supervisor run"
                )
            if protocol_binding.get("research_question_sha256") != question_payload_sha256:
                raise ResearchSupervisorError(
                    "research protocol question digest does not match canonical question"
                )

            protocol_hypothesis_id = protocol_binding.get("hypothesis_id")
            if type(protocol_hypothesis_id) is not str or not protocol_hypothesis_id:
                raise ResearchSupervisorError(
                    "research protocol lacks canonical hypothesis identity"
                )
            protocol_hypothesis = self._scientific_registry_authority().get(
                "Hypothesis", protocol_hypothesis_id
            )
            if protocol_hypothesis is None:
                raise ResearchSupervisorError(
                    "research protocol references missing canonical hypothesis"
                )
            if protocol_hypothesis.payload.get("research_question_id") != question_id:
                raise ResearchSupervisorError(
                    "research protocol hypothesis belongs to another research question"
                )
            if protocol_binding.get("hypothesis_sha256") != _digest(
                protocol_hypothesis.payload
            ):
                raise ResearchSupervisorError(
                    "research protocol hypothesis digest does not match canonical hypothesis"
                )
            if protocol_hypothesis_id != hypothesis.record_id:
                raise ResearchSupervisorError(
                    "research protocol hypothesis does not match supervisor binding"
                )

        def require_binding(binding_key: str, label: str) -> str:
            value = bindings.get(binding_key)
            if value is None:
                raise ResearchSupervisorError(
                    f"{label} requires explicit supervisor {binding_key} binding"
                )
            return value

        dataset_snapshot_id = bindings.get("dataset_snapshot_id")
        if dataset_snapshot_id is not None:
            if protocol is None or protocol_binding is None:
                raise ResearchSupervisorError(
                    "dataset snapshot requires explicit supervisor research_protocol_id binding"
                )
            dataset = self._scientific_registry_authority().get(
                "DatasetSnapshot", dataset_snapshot_id
            )
            if dataset is None:
                raise ResearchSupervisorError(
                    f"binding references missing DatasetSnapshot:{dataset_snapshot_id}"
                )
            if dataset.payload.get("manifest_sha256") != protocol.payload.get(
                "dataset_manifest_sha256"
            ):
                raise ResearchSupervisorError(
                    "dataset snapshot manifest does not match research protocol"
                )
            try:
                dataset_cutoff = _instant(
                    dataset.payload.get("causal_cutoff"),
                    "DatasetSnapshot.causal_cutoff",
                )
                protocol_cutoff = _instant(
                    protocol_binding.get("causal_cutoff"),
                    "ResearchProtocol.binding.causal_cutoff",
                )
            except (TypeError, ValueError) as exc:
                raise ResearchSupervisorError(
                    "dataset/protocol causal cutoff is invalid"
                ) from exc
            if dataset_cutoff != protocol_cutoff:
                raise ResearchSupervisorError(
                    "dataset snapshot causal cutoff does not match research protocol"
                )

        feature_set_id = bindings.get("feature_set_id")
        if feature_set_id is not None:
            if protocol_binding is None:
                raise ResearchSupervisorError(
                    "feature set requires explicit supervisor research_protocol_id binding"
                )
            feature = self._scientific_registry_authority().get("FeatureSet", feature_set_id)
            if feature is None:
                raise ResearchSupervisorError(
                    f"binding references missing FeatureSet:{feature_set_id}"
                )
            if feature.payload.get("version") != protocol_binding.get(
                "feature_set_version"
            ):
                raise ResearchSupervisorError(
                    "feature set version does not match research protocol"
                )

        model_version_id = bindings.get("model_version_id")
        if model_version_id is not None:
            if protocol_binding is None:
                raise ResearchSupervisorError(
                    "model version requires explicit supervisor research_protocol_id binding"
                )
            model = self._scientific_registry_authority().get("ModelVersion", model_version_id)
            if model is None:
                raise ResearchSupervisorError(
                    f"binding references missing ModelVersion:{model_version_id}"
                )
            expected_model_links = {
                "research_protocol_id": require_binding(
                    "research_protocol_id", "model version"
                ),
                "dataset_snapshot_id": require_binding(
                    "dataset_snapshot_id", "model version"
                ),
                "feature_set_id": require_binding("feature_set_id", "model version"),
            }
            for field, expected in expected_model_links.items():
                if model.payload.get(field) != expected:
                    raise ResearchSupervisorError(
                        f"model version {field} does not match supervisor binding"
                    )
            if model.payload.get("config_sha256") != protocol_binding.get(
                "code_config_sha256"
            ):
                raise ResearchSupervisorError(
                    "model version config does not match research protocol"
                )

        strategy_version_id = bindings.get("strategy_version_id")
        if strategy_version_id is not None:
            if protocol_binding is None:
                raise ResearchSupervisorError(
                    "strategy version requires explicit supervisor research_protocol_id binding"
                )
            strategy = self._scientific_registry_authority().get(
                "StrategyVersion", strategy_version_id
            )
            if strategy is None:
                raise ResearchSupervisorError(
                    f"binding references missing StrategyVersion:{strategy_version_id}"
                )
            if strategy.payload.get("config_sha256") != protocol_binding.get(
                "code_config_sha256"
            ):
                raise ResearchSupervisorError(
                    "strategy version config does not match research protocol"
                )
            strategy_model_id = strategy.payload.get("model_version_id")
            if strategy_model_id is not None:
                if strategy_model_id != require_binding(
                    "model_version_id", "strategy version"
                ):
                    raise ResearchSupervisorError(
                        "strategy version model does not match supervisor binding"
                    )

        evaluation_bundle_id = bindings.get("evaluation_bundle_id")
        if evaluation_bundle_id is not None:
            if protocol is None:
                raise ResearchSupervisorError(
                    "evaluation bundle requires explicit supervisor research_protocol_id binding"
                )
            evaluation = self._scientific_registry_authority().get(
                "EvaluationBundle", evaluation_bundle_id
            )
            if evaluation is None:
                raise ResearchSupervisorError(
                    f"binding references missing EvaluationBundle:{evaluation_bundle_id}"
                )
            if evaluation.payload.get("dataset_snapshot_id") != require_binding(
                "dataset_snapshot_id", "evaluation bundle"
            ):
                raise ResearchSupervisorError(
                    "evaluation bundle dataset does not match supervisor binding"
                )
            if evaluation.payload.get("protocol_sha256") != protocol.payload.get(
                "protocol_sha256"
            ):
                raise ResearchSupervisorError(
                    "evaluation bundle protocol does not match supervisor binding"
                )
            evaluated_strategy_id = evaluation.payload.get(
                "evaluated_strategy_version_id"
            )
            if (
                evaluated_strategy_id is not None
                and evaluated_strategy_id
                != require_binding("strategy_version_id", "evaluation bundle")
            ):
                raise ResearchSupervisorError(
                    "evaluation bundle strategy does not match supervisor binding"
                )
            evaluated_model_id = evaluation.payload.get("evaluated_model_version_id")
            if (
                evaluated_model_id is not None
                and evaluated_model_id
                != require_binding("model_version_id", "evaluation bundle")
            ):
                raise ResearchSupervisorError(
                    "evaluation bundle model does not match supervisor binding"
                )

        experiment_id = bindings.get("experiment_id")
        if experiment_id is not None:
            experiment = self._scientific_registry_authority().get("Experiment", experiment_id)
            if experiment is None:
                raise ResearchSupervisorError(
                    f"binding references missing Experiment:{experiment_id}"
                )
            experiment_links = {
                "research_protocol_id": "research_protocol_id",
                "dataset_snapshot_id": "dataset_snapshot_id",
                "feature_set_id": "feature_set_id",
                "strategy_version_id": "strategy_version_id",
                "evaluation_bundle_id": "evaluation_bundle_id",
            }
            for field, binding_key in experiment_links.items():
                if experiment.payload.get(field) != require_binding(
                    binding_key, "experiment"
                ):
                    raise ResearchSupervisorError(
                        f"experiment {field} does not match supervisor binding"
                    )
            experiment_model_id = experiment.payload.get("model_version_id")
            if (
                experiment_model_id is not None
                and experiment_model_id != require_binding("model_version_id", "experiment")
            ):
                raise ResearchSupervisorError(
                    "experiment model_version_id does not match supervisor binding"
                )
            if (
                protocol_binding is not None
                and experiment.payload.get("config_sha256")
                != protocol_binding.get("code_config_sha256")
            ):
                raise ResearchSupervisorError(
                    "experiment config does not match research protocol"
                )

        promotion_decision_id = bindings.get("promotion_decision_id")
        if promotion_decision_id is not None:
            decision = self._scientific_registry_authority().get(
                "PromotionDecision", promotion_decision_id
            )
            if decision is None:
                raise ResearchSupervisorError(
                    f"binding references missing PromotionDecision:{promotion_decision_id}"
                )
            decision_links = {
                "research_protocol_id": "research_protocol_id",
                "candidate_strategy_version_id": "strategy_version_id",
                "evaluation_bundle_id": "evaluation_bundle_id",
            }
            for field, binding_key in decision_links.items():
                if decision.payload.get(field) != require_binding(
                    binding_key, "promotion decision"
                ):
                    raise ResearchSupervisorError(
                        f"promotion decision {field} does not match supervisor binding"
                    )
            decision_model_id = decision.payload.get("candidate_model_version_id")
            if (
                decision_model_id is not None
                and decision_model_id
                != require_binding("model_version_id", "promotion decision")
            ):
                raise ResearchSupervisorError(
                    "promotion decision model does not match supervisor binding"
                )

        postmortem_id = bindings.get("postmortem_id")
        if postmortem_id is not None:
            postmortem = self._scientific_registry_authority().get("Postmortem", postmortem_id)
            if postmortem is None:
                raise ResearchSupervisorError(
                    f"binding references missing Postmortem:{postmortem_id}"
                )
            if postmortem.payload.get("experiment_id") != require_binding(
                "experiment_id", "postmortem"
            ):
                raise ResearchSupervisorError(
                    "postmortem experiment does not match supervisor binding"
                )

    def _validate_drift_context(
        self,
        existing_bindings: dict[str, str],
        incoming_bindings: tuple[tuple[str, str], ...],
    ) -> None:
        merged = dict(existing_bindings)
        for key, value in incoming_bindings:
            existing = merged.get(key)
            if existing is not None and existing != value:
                raise ResearchSupervisorError(
                    f"immutable binding conflict for {key}"
                )
            merged[key] = value
        drift_finding_id = merged.get("drift_finding_id")
        if drift_finding_id is None:
            return
        finding = self._scientific_registry_authority().get("DriftFinding", drift_finding_id)
        if finding is None:
            raise ResearchSupervisorError(
                f"binding references missing DriftFinding:{drift_finding_id}"
            )
        for binding_key in (
            "model_version_id",
            "strategy_version_id",
            "feature_set_id",
            "experiment_id",
        ):
            bound_value = merged.get(binding_key)
            if bound_value is None:
                continue
            if finding.payload.get(binding_key) != bound_value:
                raise ResearchSupervisorError(
                    f"drift finding context mismatch for {binding_key}"
                )

    def accept_trigger(
        self,
        trigger: ResearchTrigger,
        *,
        exclusive_trigger_prefix: str | None = None,
    ) -> SupervisorSnapshot:
        if not isinstance(trigger, ResearchTrigger):
            raise TypeError("trigger must be ResearchTrigger")
        if exclusive_trigger_prefix is not None:
            exclusive_trigger_prefix = _text(
                exclusive_trigger_prefix, "exclusive_trigger_prefix"
            )
            if not exclusive_trigger_prefix.endswith(":"):
                raise ValueError("exclusive_trigger_prefix must end with ':'")
        question = self._scientific_registry_authority().get("ResearchQuestion", trigger.question_id)
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
                if (
                    exclusive_trigger_prefix is not None
                    and existing["trigger_id"].startswith(exclusive_trigger_prefix)
                    and existing["trigger_id"] != trigger.trigger_id
                ):
                    raise ConflictingResearchTriggerError(
                        "exclusive trigger namespace is already bound to different immutable content"
                    )
                if existing["trigger_id"] != trigger.trigger_id:
                    continue
                if existing["trigger_sha256"] != trigger.trigger_sha256:
                    raise ConflictingResearchTriggerError(
                        "trigger_id is already bound to different immutable content"
                    )
                return self._snapshot(existing)

            run = self._acceptance_run(trigger)
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
            self._validate_drift_context(run["bindings"], validated_bindings)
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
                self._validate_scientific_lineage(
                    run_question_id=run["question_id"],
                    bindings=merged,
                )
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
