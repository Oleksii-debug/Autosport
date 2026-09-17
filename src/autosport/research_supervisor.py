"""Restart-safe orchestration spine for the continuous Autosport research lifecycle.

The supervisor owns orchestration checkpoints only. Scientific identities, experiment
results, promotion/rejection evidence, causal environment evidence, economic state and
execution authority remain in their existing Autosport authorities. A scheduler may
deliver a ``ResearchTrigger``; it never becomes durable product truth by itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Mapping

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


SUPERVISOR_SCHEMA_VERSION = 1


class ResearchSupervisorError(RuntimeError):
    """Base error for fail-closed supervisor state handling."""


class TriggerIdentityConflictError(ResearchSupervisorError):
    """The same external trigger identity was reused with different immutable input."""


class StaleSupervisorCheckpointError(ResearchSupervisorError):
    """A caller attempted to mutate a checkpoint revision that is no longer current."""


class InvalidResearchTransitionError(ResearchSupervisorError):
    """A phase transition would skip, rewrite, or weaken committed research state."""


class ResearchRunStoppedError(ResearchSupervisorError):
    """A paused/cancelled/deadline/budget boundary prevents forward progress."""


class ResearchPhase(str, Enum):
    QUESTION = "question"
    HYPOTHESIS = "hypothesis"
    SOURCE_SEARCH = "source_search"
    PROTOCOL_FREEZE = "protocol_freeze"
    DATASET_SNAPSHOT = "dataset_snapshot"
    EXPERIMENT = "experiment"
    CAUSAL_EVALUATION = "causal_evaluation"
    ROBUSTNESS = "robustness"
    CHAMPION_CHALLENGER = "champion_challenger"
    FORWARD_PAPER_SHADOW = "forward_paper_shadow"
    PROMOTION_DECISION = "promotion_decision"
    POSTMORTEM = "postmortem"
    MEMORY = "memory"
    NEXT_QUESTION = "next_question"


_PHASES = tuple(ResearchPhase)
_PHASE_INDEX = {phase: index for index, phase in enumerate(_PHASES)}
_HEX = frozenset("0123456789abcdef")

_BINDING_KEYS = frozenset(
    {
        "question_id",
        "hypothesis_id",
        "source_search_sha256",
        "negative_memory_check_sha256",
        "protocol_id",
        "protocol_sha256",
        "dataset_snapshot_id",
        "experiment_id",
        "evaluation_bundle_id",
        "robustness_evidence_sha256",
        "champion_strategy_version_id",
        "challenger_strategy_version_id",
        "environment_id",
        "forward_evidence_sha256",
        "promotion_decision_id",
        "postmortem_id",
        "memory_checkpoint_sha256",
        "next_question_id",
    }
)

_REQUIRED_BINDINGS = {
    ResearchPhase.QUESTION: frozenset({"question_id"}),
    ResearchPhase.HYPOTHESIS: frozenset({"question_id", "hypothesis_id"}),
    ResearchPhase.SOURCE_SEARCH: frozenset(
        {"question_id", "hypothesis_id", "source_search_sha256", "negative_memory_check_sha256"}
    ),
    ResearchPhase.PROTOCOL_FREEZE: frozenset(
        {
            "question_id",
            "hypothesis_id",
            "source_search_sha256",
            "negative_memory_check_sha256",
            "protocol_id",
            "protocol_sha256",
        }
    ),
    ResearchPhase.DATASET_SNAPSHOT: frozenset(
        {
            "question_id",
            "hypothesis_id",
            "protocol_id",
            "protocol_sha256",
            "dataset_snapshot_id",
        }
    ),
    ResearchPhase.EXPERIMENT: frozenset(
        {
            "question_id",
            "hypothesis_id",
            "protocol_id",
            "protocol_sha256",
            "dataset_snapshot_id",
            "experiment_id",
        }
    ),
    ResearchPhase.CAUSAL_EVALUATION: frozenset(
        {
            "protocol_id",
            "protocol_sha256",
            "experiment_id",
            "evaluation_bundle_id",
        }
    ),
    ResearchPhase.ROBUSTNESS: frozenset(
        {
            "protocol_id",
            "experiment_id",
            "evaluation_bundle_id",
            "robustness_evidence_sha256",
        }
    ),
    ResearchPhase.CHAMPION_CHALLENGER: frozenset(
        {
            "experiment_id",
            "champion_strategy_version_id",
            "challenger_strategy_version_id",
        }
    ),
    ResearchPhase.FORWARD_PAPER_SHADOW: frozenset(
        {
            "champion_strategy_version_id",
            "challenger_strategy_version_id",
            "environment_id",
            "forward_evidence_sha256",
        }
    ),
    ResearchPhase.PROMOTION_DECISION: frozenset(
        {
            "challenger_strategy_version_id",
            "evaluation_bundle_id",
            "promotion_decision_id",
        }
    ),
    ResearchPhase.POSTMORTEM: frozenset({"experiment_id", "promotion_decision_id"}),
    ResearchPhase.MEMORY: frozenset(
        {"experiment_id", "promotion_decision_id", "memory_checkpoint_sha256"}
    ),
    ResearchPhase.NEXT_QUESTION: frozenset(
        {"memory_checkpoint_sha256", "next_question_id"}
    ),
}


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _instant_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _decimal(name: str, value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative Decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    return parsed


def _stable_hash(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _bindings(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise ValueError("bindings must be a canonical tuple")
    normalized: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("binding entries must be two-item tuples")
        key = _text("binding key", item[0])
        if key not in _BINDING_KEYS:
            raise ValueError(f"unsupported supervisor binding key: {key}")
        raw_value = _text(f"{key} value", item[1])
        if key.endswith("_sha256") or key in {"protocol_sha256", "environment_id"}:
            raw_value = _sha256(key, raw_value)
        normalized.append((key, raw_value))
    if normalized != sorted(normalized):
        raise ValueError("bindings must be sorted")
    keys = [key for key, _ in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("binding keys must be unique")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class ResearchTrigger:
    """One deterministic external request to create/resume a research cycle."""

    trigger_id: str
    trigger_kind: str
    question_id: str
    requested_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _text("trigger_id", self.trigger_id)
        _text("trigger_kind", self.trigger_kind)
        _text("question_id", self.question_id)
        _instant("requested_at", self.requested_at)
        _sha256("payload_sha256", self.payload_sha256)

    @property
    def fingerprint(self) -> str:
        return _stable_hash(
            {
                "trigger_id": self.trigger_id,
                "trigger_kind": self.trigger_kind,
                "question_id": self.question_id,
                "requested_at": _instant_text("requested_at", self.requested_at),
                "payload_sha256": self.payload_sha256,
            }
        )

    @property
    def run_id(self) -> str:
        return f"research-{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class SupervisorControls:
    """Frozen bounded-autonomy controls for one research run."""

    priority: int
    budget_limit: Decimal
    deadline: str
    max_retries: int = 0
    max_experiments: int = 1
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.priority) is not int or not 0 <= self.priority <= 100:
            raise ValueError("priority must be an integer from 0 to 100")
        object.__setattr__(self, "budget_limit", _decimal("budget_limit", self.budget_limit))
        _instant("deadline", self.deadline)
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if type(self.max_experiments) is not int or self.max_experiments < 1:
            raise ValueError("max_experiments must be a positive integer")
        if type(self.dependencies) is not tuple:
            raise ValueError("dependencies must be a tuple")
        for dependency in self.dependencies:
            _text("dependency", dependency)
        if self.dependencies != tuple(sorted(self.dependencies)):
            raise ValueError("dependencies must be sorted")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must be unique")

    @property
    def fingerprint(self) -> str:
        return _stable_hash(
            {
                "priority": self.priority,
                "budget_limit": str(self.budget_limit),
                "deadline": _instant_text("deadline", self.deadline),
                "max_retries": self.max_retries,
                "max_experiments": self.max_experiments,
                "dependencies": list(self.dependencies),
            }
        )


@dataclass(frozen=True, slots=True)
class SupervisorCheckpoint:
    """Committed orchestration cursor; domain truth remains in canonical registries."""

    run_id: str
    trigger_id: str
    trigger_fingerprint: str
    controls_fingerprint: str
    revision: int
    phase: ResearchPhase
    bindings: tuple[tuple[str, str], ...]
    budget_spent: Decimal
    retries: int
    checkpointed_at: str
    paused: bool = False
    cancelled: bool = False
    blocker: str | None = None
    last_decision: str | None = None
    next_action: str | None = None

    def __post_init__(self) -> None:
        _text("run_id", self.run_id)
        _text("trigger_id", self.trigger_id)
        _sha256("trigger_fingerprint", self.trigger_fingerprint)
        _sha256("controls_fingerprint", self.controls_fingerprint)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if not isinstance(self.phase, ResearchPhase):
            raise ValueError("phase must be ResearchPhase")
        _bindings(self.bindings)
        object.__setattr__(self, "budget_spent", _decimal("budget_spent", self.budget_spent))
        if type(self.retries) is not int or self.retries < 0:
            raise ValueError("retries must be a non-negative integer")
        _instant("checkpointed_at", self.checkpointed_at)
        if type(self.paused) is not bool or type(self.cancelled) is not bool:
            raise ValueError("paused/cancelled must be boolean")
        for name in ("blocker", "last_decision", "next_action"):
            value = getattr(self, name)
            if value is not None:
                _text(name, value)
        self._validate_phase_bindings()

    @property
    def binding_map(self) -> dict[str, str]:
        return dict(self.bindings)

    def _validate_phase_bindings(self) -> None:
        present = frozenset(self.binding_map)
        missing = _REQUIRED_BINDINGS[self.phase] - present
        if missing:
            raise ValueError(
                f"{self.phase.value} checkpoint is missing bindings: {','.join(sorted(missing))}"
            )

    @property
    def checkpoint_sha256(self) -> str:
        return _stable_hash(_checkpoint_payload(self))


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    run_id: str
    phase: ResearchPhase
    revision: int
    question_id: str
    hypothesis_id: str | None
    protocol_id: str | None
    protocol_sha256: str | None
    experiment_id: str | None
    champion_strategy_version_id: str | None
    challenger_strategy_version_id: str | None
    budget_spent: Decimal
    budget_limit: Decimal
    deadline: str
    checkpointed_at: str
    paused: bool
    cancelled: bool
    blocker: str | None
    last_decision: str | None
    next_action: str | None


def _controls_payload(controls: SupervisorControls) -> dict[str, object]:
    return {
        "priority": controls.priority,
        "budget_limit": str(controls.budget_limit),
        "deadline": _instant_text("deadline", controls.deadline),
        "max_retries": controls.max_retries,
        "max_experiments": controls.max_experiments,
        "dependencies": list(controls.dependencies),
    }


def _controls_from_payload(payload: object) -> SupervisorControls:
    if type(payload) is not dict:
        raise ValueError("supervisor controls must be an object")
    required = {
        "priority",
        "budget_limit",
        "deadline",
        "max_retries",
        "max_experiments",
        "dependencies",
    }
    if set(payload) != required:
        raise ValueError("supervisor controls fields mismatch")
    dependencies = payload["dependencies"]
    if type(dependencies) is not list:
        raise ValueError("supervisor control dependencies must be a list")
    return SupervisorControls(
        priority=payload["priority"],
        budget_limit=_decimal("budget_limit", payload["budget_limit"]),
        deadline=payload["deadline"],
        max_retries=payload["max_retries"],
        max_experiments=payload["max_experiments"],
        dependencies=tuple(dependencies),
    )


def _checkpoint_payload(checkpoint: SupervisorCheckpoint) -> dict[str, object]:
    return {
        "run_id": checkpoint.run_id,
        "trigger_id": checkpoint.trigger_id,
        "trigger_fingerprint": checkpoint.trigger_fingerprint,
        "controls_fingerprint": checkpoint.controls_fingerprint,
        "revision": checkpoint.revision,
        "phase": checkpoint.phase.value,
        "bindings": [[key, value] for key, value in checkpoint.bindings],
        "budget_spent": str(checkpoint.budget_spent),
        "retries": checkpoint.retries,
        "checkpointed_at": _instant_text("checkpointed_at", checkpoint.checkpointed_at),
        "paused": checkpoint.paused,
        "cancelled": checkpoint.cancelled,
        "blocker": checkpoint.blocker,
        "last_decision": checkpoint.last_decision,
        "next_action": checkpoint.next_action,
    }


def _checkpoint_from_payload(payload: object) -> SupervisorCheckpoint:
    if type(payload) is not dict:
        raise ValueError("supervisor checkpoint must be an object")
    required = {
        "run_id",
        "trigger_id",
        "trigger_fingerprint",
        "controls_fingerprint",
        "revision",
        "phase",
        "bindings",
        "budget_spent",
        "retries",
        "checkpointed_at",
        "paused",
        "cancelled",
        "blocker",
        "last_decision",
        "next_action",
    }
    if set(payload) != required:
        raise ValueError("supervisor checkpoint fields mismatch")
    raw_bindings = payload["bindings"]
    if type(raw_bindings) is not list:
        raise ValueError("supervisor bindings must be a list")
    bindings: list[tuple[str, str]] = []
    for item in raw_bindings:
        if type(item) is not list or len(item) != 2:
            raise ValueError("supervisor binding entry must be a two-item list")
        bindings.append((item[0], item[1]))
    try:
        phase = ResearchPhase(payload["phase"])
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported supervisor phase") from exc
    return SupervisorCheckpoint(
        run_id=payload["run_id"],
        trigger_id=payload["trigger_id"],
        trigger_fingerprint=payload["trigger_fingerprint"],
        controls_fingerprint=payload["controls_fingerprint"],
        revision=payload["revision"],
        phase=phase,
        bindings=tuple(bindings),
        budget_spent=_decimal("budget_spent", payload["budget_spent"]),
        retries=payload["retries"],
        checkpointed_at=payload["checkpointed_at"],
        paused=payload["paused"],
        cancelled=payload["cancelled"],
        blocker=payload["blocker"],
        last_decision=payload["last_decision"],
        next_action=payload["next_action"],
    )


class ResearchSupervisorStore:
    """One durable, idempotent supervisor checkpoint file per Autosport workspace."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._read()
        except FileNotFoundError as exc:
            raise ValueError("research supervisor store is missing") from exc

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "ResearchSupervisorStore":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                atomic_write_json(
                    target,
                    {
                        "schema_version": SUPERVISOR_SCHEMA_VERSION,
                        "triggers": {},
                        "runs": [],
                    },
                )
            else:
                cls(target)._read()
        return cls(target)

    def _read(self) -> dict[str, object]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("research supervisor store must be valid UTF-8 JSON") from exc
        if type(state) is not dict or set(state) != {"schema_version", "triggers", "runs"}:
            raise ValueError("research supervisor store fields mismatch")
        if state["schema_version"] != SUPERVISOR_SCHEMA_VERSION:
            raise ValueError("research supervisor schema_version mismatch")
        if type(state["triggers"]) is not dict or type(state["runs"]) is not list:
            raise ValueError("research supervisor trigger/run collections are invalid")

        run_ids: set[str] = set()
        for record in state["runs"]:
            if type(record) is not dict or set(record) != {"controls", "checkpoint"}:
                raise ValueError("research supervisor run record fields mismatch")
            controls = _controls_from_payload(record["controls"])
            checkpoint = _checkpoint_from_payload(record["checkpoint"])
            if controls.fingerprint != checkpoint.controls_fingerprint:
                raise ValueError("supervisor controls fingerprint mismatch")
            if checkpoint.run_id in run_ids:
                raise ValueError("duplicate research supervisor run_id")
            run_ids.add(checkpoint.run_id)

        for trigger_id, raw_entry in state["triggers"].items():
            _text("trigger_id", trigger_id)
            if type(raw_entry) is not dict or set(raw_entry) != {"fingerprint", "run_id"}:
                raise ValueError("research supervisor trigger index fields mismatch")
            _sha256("trigger fingerprint", raw_entry["fingerprint"])
            run_id = _text("trigger run_id", raw_entry["run_id"])
            if run_id not in run_ids:
                raise ValueError("research supervisor trigger references missing run")
        return state

    @staticmethod
    def _find_run(state: dict[str, object], run_id: str) -> tuple[int, dict[str, object]]:
        _text("run_id", run_id)
        for index, record in enumerate(state["runs"]):
            checkpoint = _checkpoint_from_payload(record["checkpoint"])
            if checkpoint.run_id == run_id:
                return index, record
        raise KeyError(run_id)

    def start_or_resume(
        self,
        trigger: ResearchTrigger,
        controls: SupervisorControls,
        *,
        checkpointed_at: str,
    ) -> SupervisorCheckpoint:
        if not isinstance(trigger, ResearchTrigger):
            raise TypeError("trigger must be ResearchTrigger")
        if not isinstance(controls, SupervisorControls):
            raise TypeError("controls must be SupervisorControls")
        at = _instant_text("checkpointed_at", checkpointed_at)

        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            trigger_index = state["triggers"]
            prior = trigger_index.get(trigger.trigger_id)
            if prior is not None:
                if prior["fingerprint"] != trigger.fingerprint:
                    raise TriggerIdentityConflictError(
                        "trigger_id was reused with different immutable trigger input"
                    )
                _, record = self._find_run(state, prior["run_id"])
                prior_controls = _controls_from_payload(record["controls"])
                if prior_controls.fingerprint != controls.fingerprint:
                    raise TriggerIdentityConflictError(
                        "duplicate trigger attempted to rewrite frozen supervisor controls"
                    )
                return _checkpoint_from_payload(record["checkpoint"])

            if _instant("checkpointed_at", at) > _instant("deadline", controls.deadline):
                raise ResearchRunStoppedError("research trigger arrived after the frozen deadline")
            checkpoint = SupervisorCheckpoint(
                run_id=trigger.run_id,
                trigger_id=trigger.trigger_id,
                trigger_fingerprint=trigger.fingerprint,
                controls_fingerprint=controls.fingerprint,
                revision=0,
                phase=ResearchPhase.QUESTION,
                bindings=(("question_id", trigger.question_id),),
                budget_spent=Decimal("0"),
                retries=0,
                checkpointed_at=at,
                next_action=ResearchPhase.HYPOTHESIS.value,
            )
            state["triggers"][trigger.trigger_id] = {
                "fingerprint": trigger.fingerprint,
                "run_id": checkpoint.run_id,
            }
            state["runs"].append(
                {
                    "controls": _controls_payload(controls),
                    "checkpoint": _checkpoint_payload(checkpoint),
                }
            )
            atomic_write_json(self.path, state)
            return checkpoint

    def get(self, run_id: str) -> tuple[SupervisorCheckpoint, SupervisorControls]:
        state = self._read()
        _, record = self._find_run(state, run_id)
        return (
            _checkpoint_from_payload(record["checkpoint"]),
            _controls_from_payload(record["controls"]),
        )

    def advance(
        self,
        run_id: str,
        *,
        expected_revision: int,
        phase: ResearchPhase,
        checkpointed_at: str,
        bindings: Mapping[str, str] | None = None,
        budget_delta: Decimal | str = Decimal("0"),
        retry_delta: int = 0,
        blocker: str | None = None,
        last_decision: str | None = None,
        next_action: str | None = None,
    ) -> SupervisorCheckpoint:
        if not isinstance(phase, ResearchPhase):
            raise TypeError("phase must be ResearchPhase")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if type(retry_delta) is not int or retry_delta < 0:
            raise ValueError("retry_delta must be a non-negative integer")
        delta = _decimal("budget_delta", budget_delta)
        at = _instant_text("checkpointed_at", checkpointed_at)
        additions = dict(bindings or {})
        for key, value in additions.items():
            if key not in _BINDING_KEYS:
                raise ValueError(f"unsupported supervisor binding key: {key}")
            _text(f"{key} value", value)
            if key.endswith("_sha256") or key in {"protocol_sha256", "environment_id"}:
                _sha256(key, value)

        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            index, record = self._find_run(state, run_id)
            current = _checkpoint_from_payload(record["checkpoint"])
            controls = _controls_from_payload(record["controls"])
            self._assert_mutable(current, controls, expected_revision, at)

            expected_phase_index = _PHASE_INDEX[current.phase] + 1
            if expected_phase_index >= len(_PHASES) or _PHASES[expected_phase_index] is not phase:
                raise InvalidResearchTransitionError(
                    f"phase must advance exactly from {current.phase.value} "
                    f"to {_PHASES[expected_phase_index].value if expected_phase_index < len(_PHASES) else 'none'}"
                )

            merged = current.binding_map
            for key, value in additions.items():
                prior = merged.get(key)
                if prior is not None and prior != value:
                    raise InvalidResearchTransitionError(
                        f"committed binding {key} cannot be rewritten"
                    )
                merged[key] = value

            budget_spent = current.budget_spent + delta
            if budget_spent > controls.budget_limit:
                raise ResearchRunStoppedError("research budget limit would be exceeded")
            retries = current.retries + retry_delta
            if retries > controls.max_retries:
                raise ResearchRunStoppedError("research retry limit would be exceeded")

            updated = SupervisorCheckpoint(
                run_id=current.run_id,
                trigger_id=current.trigger_id,
                trigger_fingerprint=current.trigger_fingerprint,
                controls_fingerprint=current.controls_fingerprint,
                revision=current.revision + 1,
                phase=phase,
                bindings=tuple(sorted(merged.items())),
                budget_spent=budget_spent,
                retries=retries,
                checkpointed_at=at,
                paused=False,
                cancelled=False,
                blocker=blocker,
                last_decision=last_decision if last_decision is not None else current.last_decision,
                next_action=next_action,
            )
            state["runs"][index] = {
                "controls": _controls_payload(controls),
                "checkpoint": _checkpoint_payload(updated),
            }
            atomic_write_json(self.path, state)
            return updated

    @staticmethod
    def _assert_mutable(
        current: SupervisorCheckpoint,
        controls: SupervisorControls,
        expected_revision: int,
        checkpointed_at: str,
    ) -> None:
        if current.revision != expected_revision:
            raise StaleSupervisorCheckpointError(
                f"expected revision {expected_revision}, current is {current.revision}"
            )
        if current.cancelled:
            raise ResearchRunStoppedError("research run is cancelled")
        if current.paused:
            raise ResearchRunStoppedError("research run is paused")
        if _instant("checkpointed_at", checkpointed_at) > _instant("deadline", controls.deadline):
            raise ResearchRunStoppedError("research run deadline has passed")

    def set_paused(
        self,
        run_id: str,
        *,
        expected_revision: int,
        paused: bool,
        checkpointed_at: str,
        blocker: str | None = None,
    ) -> SupervisorCheckpoint:
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        at = _instant_text("checkpointed_at", checkpointed_at)
        if blocker is not None:
            _text("blocker", blocker)
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            index, record = self._find_run(state, run_id)
            current = _checkpoint_from_payload(record["checkpoint"])
            controls = _controls_from_payload(record["controls"])
            if current.revision != expected_revision:
                raise StaleSupervisorCheckpointError(
                    f"expected revision {expected_revision}, current is {current.revision}"
                )
            if current.cancelled:
                raise ResearchRunStoppedError("cancelled research run cannot be resumed")
            if not paused and _instant("checkpointed_at", at) > _instant("deadline", controls.deadline):
                raise ResearchRunStoppedError("research run deadline has passed")
            updated = SupervisorCheckpoint(
                run_id=current.run_id,
                trigger_id=current.trigger_id,
                trigger_fingerprint=current.trigger_fingerprint,
                controls_fingerprint=current.controls_fingerprint,
                revision=current.revision + 1,
                phase=current.phase,
                bindings=current.bindings,
                budget_spent=current.budget_spent,
                retries=current.retries,
                checkpointed_at=at,
                paused=paused,
                cancelled=False,
                blocker=blocker if paused else None,
                last_decision=current.last_decision,
                next_action=current.next_action,
            )
            state["runs"][index] = {
                "controls": _controls_payload(controls),
                "checkpoint": _checkpoint_payload(updated),
            }
            atomic_write_json(self.path, state)
            return updated

    def cancel(
        self,
        run_id: str,
        *,
        expected_revision: int,
        checkpointed_at: str,
        reason: str,
    ) -> SupervisorCheckpoint:
        at = _instant_text("checkpointed_at", checkpointed_at)
        _text("reason", reason)
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            index, record = self._find_run(state, run_id)
            current = _checkpoint_from_payload(record["checkpoint"])
            controls = _controls_from_payload(record["controls"])
            if current.revision != expected_revision:
                raise StaleSupervisorCheckpointError(
                    f"expected revision {expected_revision}, current is {current.revision}"
                )
            if current.cancelled:
                return current
            updated = SupervisorCheckpoint(
                run_id=current.run_id,
                trigger_id=current.trigger_id,
                trigger_fingerprint=current.trigger_fingerprint,
                controls_fingerprint=current.controls_fingerprint,
                revision=current.revision + 1,
                phase=current.phase,
                bindings=current.bindings,
                budget_spent=current.budget_spent,
                retries=current.retries,
                checkpointed_at=at,
                paused=False,
                cancelled=True,
                blocker=reason,
                last_decision=current.last_decision,
                next_action=None,
            )
            state["runs"][index] = {
                "controls": _controls_payload(controls),
                "checkpoint": _checkpoint_payload(updated),
            }
            atomic_write_json(self.path, state)
            return updated

    def status(self, run_id: str) -> SupervisorStatus:
        checkpoint, controls = self.get(run_id)
        bindings = checkpoint.binding_map
        return SupervisorStatus(
            run_id=checkpoint.run_id,
            phase=checkpoint.phase,
            revision=checkpoint.revision,
            question_id=bindings["question_id"],
            hypothesis_id=bindings.get("hypothesis_id"),
            protocol_id=bindings.get("protocol_id"),
            protocol_sha256=bindings.get("protocol_sha256"),
            experiment_id=bindings.get("experiment_id"),
            champion_strategy_version_id=bindings.get("champion_strategy_version_id"),
            challenger_strategy_version_id=bindings.get("challenger_strategy_version_id"),
            budget_spent=checkpoint.budget_spent,
            budget_limit=controls.budget_limit,
            deadline=_instant_text("deadline", controls.deadline),
            checkpointed_at=_instant_text("checkpointed_at", checkpoint.checkpointed_at),
            paused=checkpoint.paused,
            cancelled=checkpoint.cancelled,
            blocker=checkpoint.blocker,
            last_decision=checkpoint.last_decision,
            next_action=checkpoint.next_action,
        )
