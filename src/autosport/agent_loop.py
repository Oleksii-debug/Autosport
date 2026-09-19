"""Restart-safe AgentLoop orchestration and causal credit-attribution seam.

This module composes existing Autosport authorities. It does not choose actions,
compute rewards, widen EconomicGoal/Risk limits, mutate promotion state, or execute
provider actions. CausalLearningEnvironment remains the owner of observation/action/
outcome/reward transitions and ResearchSupervisor remains the research-run authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .integrity import atomic_write_json
from .learning_environment import (
    Action,
    EnvironmentCheckpoint,
    Episode,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from .research_supervisor import ResearchSupervisor, SupervisorSnapshot
from .research_trigger_adapter import (
    ExternalResearchTrigger,
    ResearchTriggerAdapter,
    ResearchTriggerSource,
)
from .scientific_registry import ResearchQuestion
from .workspace_lock import WorkspaceEconomicLock


AGENT_LOOP_SCHEMA: Final = "autosport.agent_loop"
AGENT_LOOP_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class AgentLoopError(RuntimeError):
    """Base error for durable AgentLoop state or causal evidence."""


class StaleAgentLoopStateError(AgentLoopError):
    """The requested transition no longer matches durable loop state."""


class ConflictingAgentLoopEvidenceError(AgentLoopError):
    """An immutable AgentLoop identity was reused with different evidence."""


class AgentLoopPhase(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    OBSERVE = "OBSERVE"
    ASSESS = "ASSESS"
    PLAN = "PLAN"
    DECIDE = "DECIDE"
    ACT_OR_ABSTAIN = "ACT_OR_ABSTAIN"
    WAIT_OUTCOME = "WAIT_OUTCOME"
    EVALUATE = "EVALUATE"
    ATTRIBUTE = "ATTRIBUTE"
    REFLECT = "REFLECT"
    RESEARCH_HANDOFF = "RESEARCH_HANDOFF"
    CHECKPOINT = "CHECKPOINT"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    RECOVERY = "RECOVERY"
    UNKNOWN_EXTERNAL_EFFECT = "UNKNOWN_EXTERNAL_EFFECT"


class ExternalEffectState(StrEnum):
    """Truth about external side effects; AgentLoop itself performs none."""

    NONE = "NONE"
    PAPER_ONLY = "PAPER_ONLY"
    UNKNOWN_EXTERNAL_EFFECT = "UNKNOWN_EXTERNAL_EFFECT"


class AttributionComponent(StrEnum):
    DATA = "DATA"
    IDENTITY = "IDENTITY"
    FORECAST = "FORECAST"
    CALIBRATION = "CALIBRATION"
    STRATEGY = "STRATEGY"
    TIMING = "TIMING"
    STAKE = "STAKE"
    DEPENDENCE = "DEPENDENCE"
    PORTFOLIO = "PORTFOLIO"
    EXECUTION = "EXECUTION"
    RANDOMNESS = "RANDOMNESS"


class AttributionStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNKNOWN = "UNKNOWN"
    MIXED = "MIXED"


_PRIMARY_NEXT: dict[AgentLoopPhase, AgentLoopPhase] = {
    AgentLoopPhase.OBSERVE: AgentLoopPhase.ASSESS,
    AgentLoopPhase.ASSESS: AgentLoopPhase.PLAN,
    AgentLoopPhase.PLAN: AgentLoopPhase.DECIDE,
    AgentLoopPhase.DECIDE: AgentLoopPhase.ACT_OR_ABSTAIN,
    AgentLoopPhase.EVALUATE: AgentLoopPhase.ATTRIBUTE,
}


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AgentLoopError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise AgentLoopError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AgentLoopError(f"{name} must be valid UTF-8 text") from exc
    return value


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentLoopError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AgentLoopError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_identity(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise AgentLoopError(f"{name} must be canonical SHA-256 hex")
    return text


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AgentLoopError(f"{name} must be a finite exact Decimal")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AgentLoopError("AgentLoop payload is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentLoopError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise AgentLoopError(f"non-finite JSON constant: {value}")


@dataclass(frozen=True, slots=True)
class AttributionFinding:
    component: AttributionComponent
    status: AttributionStatus
    evidence_sha256: str
    evidence_available_at: str
    contribution: Decimal | None = None
    reason_code: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        if not isinstance(self.component, AttributionComponent):
            raise AgentLoopError("component must be AttributionComponent")
        if not isinstance(self.status, AttributionStatus):
            raise AgentLoopError("status must be AttributionStatus")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _instant(self.evidence_available_at, "evidence_available_at")
        _text(self.reason_code, "reason_code")
        if self.contribution is not None:
            _decimal(self.contribution, "contribution")
        if self.status is AttributionStatus.UNKNOWN and self.contribution is not None:
            raise AgentLoopError("UNKNOWN attribution cannot assert a contribution")

    def payload(self) -> dict[str, object]:
        return {
            "component": self.component.value,
            "status": self.status.value,
            "evidence_sha256": self.evidence_sha256,
            "evidence_available_at": _timestamp_identity(
                self.evidence_available_at, "evidence_available_at"
            ),
            "contribution": None if self.contribution is None else str(self.contribution),
            "reason_code": self.reason_code,
        }


# Factor-level immutable causal credit assignment record. Kept as an alias to avoid
# creating a second ledger while giving callers an explicit CreditAssignment type.
CreditAssignment = AttributionFinding


@dataclass(frozen=True, slots=True)
class OutcomeAttribution:
    environment_id: str
    episode_id: str
    transition_id: str
    action_id: str
    outcome_id: str
    reward_id: str
    reward_value: Decimal
    truth: EvidenceTruth
    simulation_model_id: str | None
    attributed_at: str
    findings: tuple[AttributionFinding, ...]

    def __post_init__(self) -> None:
        for name in (
            "environment_id",
            "episode_id",
            "transition_id",
            "action_id",
            "outcome_id",
            "reward_id",
        ):
            _sha256(getattr(self, name), name)
        _decimal(self.reward_value, "reward_value")
        if not isinstance(self.truth, EvidenceTruth):
            raise AgentLoopError("truth must be EvidenceTruth")
        if self.truth is EvidenceTruth.SIMULATED:
            _text(self.simulation_model_id, "simulation_model_id")
        elif self.simulation_model_id is not None:
            raise AgentLoopError("observed attribution cannot carry simulation_model_id")
        _instant(self.attributed_at, "attributed_at")
        if type(self.findings) is not tuple or not self.findings:
            raise AgentLoopError("findings must be a non-empty tuple")
        if any(not isinstance(item, AttributionFinding) for item in self.findings):
            raise AgentLoopError("findings must contain AttributionFinding values")
        components = [item.component.value for item in self.findings]
        if components != sorted(components):
            raise AgentLoopError("findings must be sorted by attribution component")
        if len(components) != len(set(components)):
            raise AgentLoopError("findings must not duplicate attribution components")

    def payload(self) -> dict[str, object]:
        return {
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "transition_id": self.transition_id,
            "action_id": self.action_id,
            "outcome_id": self.outcome_id,
            "reward_id": self.reward_id,
            "reward_value": str(self.reward_value),
            "truth": self.truth.value,
            "simulation_model_id": self.simulation_model_id,
            "attributed_at": _timestamp_identity(self.attributed_at, "attributed_at"),
            "findings": [item.payload() for item in self.findings],
        }

    @property
    def attribution_id(self) -> str:
        """Stable immutable key for one causal transition attribution.

        Payload changes (timestamps, evidence, findings) must collide on the same
        transition/version so a retry can fail closed rather than create a second
        attribution for the same causal event.
        """
        return _digest(
            {
                "schema": AGENT_LOOP_SCHEMA,
                "kind": "OutcomeAttribution",
                "transition_id": self.transition_id,
                "reward_id": self.reward_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ReflectionPostmortem:
    attribution_id: str
    transition_id: str
    created_at: str
    unresolved_components: tuple[AttributionComponent, ...]
    summary_code: str
    research_question_statement: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.attribution_id, "attribution_id")
        _sha256(self.transition_id, "transition_id")
        _instant(self.created_at, "created_at")
        _text(self.summary_code, "summary_code")
        if type(self.unresolved_components) is not tuple:
            raise AgentLoopError("unresolved_components must be a tuple")
        if any(
            not isinstance(item, AttributionComponent)
            for item in self.unresolved_components
        ):
            raise AgentLoopError(
                "unresolved_components must contain AttributionComponent values"
            )
        names = [item.value for item in self.unresolved_components]
        if names != sorted(names):
            raise AgentLoopError("unresolved_components must be sorted")
        if len(names) != len(set(names)):
            raise AgentLoopError("unresolved_components must be unique")
        if self.research_question_statement is not None:
            _text(self.research_question_statement, "research_question_statement")
            if not self.unresolved_components:
                raise AgentLoopError(
                    "research question requires at least one unresolved attribution component"
                )

    def payload(self) -> dict[str, object]:
        return {
            "attribution_id": self.attribution_id,
            "transition_id": self.transition_id,
            "created_at": _timestamp_identity(self.created_at, "created_at"),
            "unresolved_components": [
                item.value for item in self.unresolved_components
            ],
            "summary_code": self.summary_code,
            "research_question_statement": self.research_question_statement,
        }

    @property
    def postmortem_id(self) -> str:
        """Stable immutable key for one attribution's reflection/postmortem."""
        return _digest(
            {
                "schema": AGENT_LOOP_SCHEMA,
                "kind": "ReflectionPostmortem",
                "attribution_id": self.attribution_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ActionCommitReceipt:
    action_id: str
    newly_committed: bool
    may_execute: bool
    external_effect_state: ExternalEffectState

    def __post_init__(self) -> None:
        _sha256(self.action_id, "action_id")
        if type(self.newly_committed) is not bool or type(self.may_execute) is not bool:
            raise AgentLoopError("action receipt flags must be boolean")
        if not isinstance(self.external_effect_state, ExternalEffectState):
            raise AgentLoopError("external_effect_state must be ExternalEffectState")


@dataclass(frozen=True, slots=True)
class AgentLoopSnapshot:
    loop_id: str
    environment_id: str
    episode_id: str
    policy_id: str
    economic_goal_fingerprint: str
    risk_fingerprint: str
    source_sha256: str
    config_sha256: str
    phase: AgentLoopPhase
    sequence: int
    observation_id: str | None
    action_id: str | None
    transition_id: str | None
    outcome_id: str | None
    reward_id: str | None
    attribution_id: str | None
    postmortem_id: str | None
    research_question_id: str | None
    research_trigger_id: str | None
    research_run_id: str | None
    environment_checkpoint_id: str
    external_effect_state: ExternalEffectState
    resume_phase: AgentLoopPhase | None
    updated_at: str
    state_sha256: str


class AgentLoopRuntime:
    """Durable controller state that references canonical authorities."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._read()
        except FileNotFoundError as exc:
            raise AgentLoopError("AgentLoop state is missing") from exc

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        *,
        loop_id: str,
        environment_checkpoint: EnvironmentCheckpoint,
        policy_id: str,
        economic_goal_fingerprint: str,
        risk_fingerprint: str,
        source_sha256: str,
        config_sha256: str,
        at: str,
    ) -> "AgentLoopRuntime":
        if not isinstance(environment_checkpoint, EnvironmentCheckpoint):
            raise TypeError("environment_checkpoint must be EnvironmentCheckpoint")
        target = Path(path)
        normalized_policy_id = _text(policy_id, "policy_id")
        if environment_checkpoint.policy_id != normalized_policy_id:
            raise ConflictingAgentLoopEvidenceError(
                "initial environment checkpoint policy identity mismatch"
            )
        identity = {
            "loop_id": _text(loop_id, "loop_id"),
            "environment_id": environment_checkpoint.environment_id,
            "episode_id": environment_checkpoint.episode_id,
            "policy_id": normalized_policy_id,
            "economic_goal_fingerprint": _sha256(
                economic_goal_fingerprint, "economic_goal_fingerprint"
            ),
            "risk_fingerprint": _sha256(risk_fingerprint, "risk_fingerprint"),
            "source_sha256": _sha256(source_sha256, "source_sha256"),
            "config_sha256": _sha256(config_sha256, "config_sha256"),
        }
        initial = {
            "schema": AGENT_LOOP_SCHEMA,
            "schema_version": AGENT_LOOP_SCHEMA_VERSION,
            "identity": identity,
            "phase": AgentLoopPhase.BOOTSTRAP.value,
            "sequence": 0,
            "current": cls._empty_current(),
            "environment_checkpoint_id": environment_checkpoint.checkpoint_id,
            "external_effect_state": ExternalEffectState.NONE.value,
            "resume_phase": None,
            "decisions": [],
            "resolutions": [],
            "attributions": [],
            "postmortems": [],
            "research_handoffs": [],
            "updated_at": _timestamp_identity(at, "at"),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                cls._write_state(target, initial)
            else:
                existing = cls(target)._read()
                if existing["identity"] != identity:
                    raise ConflictingAgentLoopEvidenceError(
                        "existing AgentLoop identity conflicts with initialization"
                    )
                if (
                    existing["environment_checkpoint_id"]
                    != environment_checkpoint.checkpoint_id
                ):
                    raise ConflictingAgentLoopEvidenceError(
                        "existing AgentLoop initial checkpoint conflicts with initialization"
                    )
        return cls(target)

    @staticmethod
    def _empty_current() -> dict[str, object]:
        return {
            "observation_id": None,
            "action_id": None,
            "transition_id": None,
            "outcome_id": None,
            "reward_id": None,
            "attribution_id": None,
            "postmortem_id": None,
            "research_question_id": None,
            "research_trigger_id": None,
            "research_run_id": None,
        }

    @staticmethod
    def _without_digest(state: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in state.items() if key != "state_sha256"
        }

    @classmethod
    def _write_state(
        cls, path: Path, state_without_digest: dict[str, Any]
    ) -> None:
        state = dict(state_without_digest)
        state["state_sha256"] = _digest(state_without_digest)
        atomic_write_json(path, state)

    def _read(self) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise AgentLoopError("AgentLoop state must be valid JSON") from exc
        if type(state) is not dict:
            raise AgentLoopError("AgentLoop state must be an object")
        required = {
            "schema",
            "schema_version",
            "identity",
            "phase",
            "sequence",
            "current",
            "environment_checkpoint_id",
            "external_effect_state",
            "resume_phase",
            "decisions",
            "resolutions",
            "attributions",
            "postmortems",
            "research_handoffs",
            "updated_at",
            "state_sha256",
        }
        if set(state) != required:
            raise AgentLoopError("AgentLoop state fields mismatch")
        if (
            state["schema"] != AGENT_LOOP_SCHEMA
            or state["schema_version"] != AGENT_LOOP_SCHEMA_VERSION
        ):
            raise AgentLoopError("AgentLoop schema mismatch")
        identity = state["identity"]
        if type(identity) is not dict or set(identity) != {
            "loop_id",
            "environment_id",
            "episode_id",
            "policy_id",
            "economic_goal_fingerprint",
            "risk_fingerprint",
            "source_sha256",
            "config_sha256",
        }:
            raise AgentLoopError("AgentLoop identity fields mismatch")
        _text(identity["loop_id"], "loop_id")
        for key in (
            "environment_id",
            "episode_id",
            "economic_goal_fingerprint",
            "risk_fingerprint",
            "source_sha256",
            "config_sha256",
        ):
            _sha256(identity[key], key)
        _text(identity["policy_id"], "policy_id")
        AgentLoopPhase(state["phase"])
        if (
            isinstance(state["sequence"], bool)
            or not isinstance(state["sequence"], int)
            or state["sequence"] < 0
        ):
            raise AgentLoopError(
                "AgentLoop sequence must be a non-negative integer"
            )
        current = state["current"]
        if type(current) is not dict or set(current) != set(self._empty_current()):
            raise AgentLoopError("AgentLoop current fields mismatch")
        for key, value in current.items():
            if value is not None:
                if key in {"research_question_id", "research_trigger_id"}:
                    _text(value, key)
                else:
                    _sha256(value, key)
        _sha256(
            state["environment_checkpoint_id"], "environment_checkpoint_id"
        )
        ExternalEffectState(state["external_effect_state"])
        if state["resume_phase"] is not None:
            AgentLoopPhase(state["resume_phase"])
        for key in (
            "decisions",
            "resolutions",
            "attributions",
            "postmortems",
            "research_handoffs",
        ):
            if type(state[key]) is not list:
                raise AgentLoopError(f"{key} must be a list")
        _instant(state["updated_at"], "updated_at")
        expected = _digest(self._without_digest(state))
        if _sha256(state["state_sha256"], "state_sha256") != expected:
            raise AgentLoopError("AgentLoop state digest mismatch")
        self._validate_history(state)
        return state

    @staticmethod
    def _validate_history(state: dict[str, Any]) -> None:
        decisions: set[str] = set()
        observations: set[str] = set()
        for record in state["decisions"]:
            if type(record) is not dict:
                raise AgentLoopError("decision record must be an object")
            _sha256(record.get("action_id"), "decision action_id")
            _sha256(record.get("observation_id"), "decision observation_id")
            if type(record.get("may_execute")) is not bool:
                raise AgentLoopError("decision may_execute must be boolean")
            ActionCommitReceipt(
                action_id=record["action_id"],
                newly_committed=True,
                may_execute=record["may_execute"],
                external_effect_state=ExternalEffectState(
                    record.get("external_effect_state")
                ),
            )
            if (
                record["action_id"] in decisions
                or record["observation_id"] in observations
            ):
                raise AgentLoopError(
                    "AgentLoop contains duplicate durable decision identity"
                )
            decisions.add(record["action_id"])
            observations.add(record["observation_id"])
        for key, id_key in (
            ("resolutions", "transition_id"),
            ("attributions", "attribution_id"),
            ("postmortems", "postmortem_id"),
            ("research_handoffs", "postmortem_id"),
        ):
            seen: set[str] = set()
            for record in state[key]:
                if type(record) is not dict:
                    raise AgentLoopError(f"{key} record must be an object")
                value = record.get(id_key)
                _sha256(value, id_key)
                if value in seen:
                    raise AgentLoopError(
                        f"{key} contains duplicate immutable identity"
                    )
                seen.add(value)

    def snapshot(self) -> AgentLoopSnapshot:
        state = self._read()
        identity = state["identity"]
        current = state["current"]
        return AgentLoopSnapshot(
            loop_id=identity["loop_id"],
            environment_id=identity["environment_id"],
            episode_id=identity["episode_id"],
            policy_id=identity["policy_id"],
            economic_goal_fingerprint=identity["economic_goal_fingerprint"],
            risk_fingerprint=identity["risk_fingerprint"],
            source_sha256=identity["source_sha256"],
            config_sha256=identity["config_sha256"],
            phase=AgentLoopPhase(state["phase"]),
            sequence=state["sequence"],
            observation_id=current["observation_id"],
            action_id=current["action_id"],
            transition_id=current["transition_id"],
            outcome_id=current["outcome_id"],
            reward_id=current["reward_id"],
            attribution_id=current["attribution_id"],
            postmortem_id=current["postmortem_id"],
            research_question_id=current["research_question_id"],
            research_trigger_id=current["research_trigger_id"],
            research_run_id=current["research_run_id"],
            environment_checkpoint_id=state["environment_checkpoint_id"],
            external_effect_state=ExternalEffectState(
                state["external_effect_state"]
            ),
            resume_phase=(
                None
                if state["resume_phase"] is None
                else AgentLoopPhase(state["resume_phase"])
            ),
            updated_at=state["updated_at"],
            state_sha256=state["state_sha256"],
        )

    def _mutate(self, at: str, mutate) -> AgentLoopSnapshot:
        now = _timestamp_identity(at, "at")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            if _instant(now, "at") < _instant(
                state["updated_at"], "updated_at"
            ):
                raise AgentLoopError("AgentLoop time cannot move backwards")
            mutate(state, now)
            state["sequence"] += 1
            state["updated_at"] = now
            self._write_state(self.path, self._without_digest(state))
        return self.snapshot()

    def begin_observation(
        self, observation: Observation, *, at: str
    ) -> AgentLoopSnapshot:
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")

        def apply(state: dict[str, Any], now: str) -> None:
            phase = AgentLoopPhase(state["phase"])
            if phase not in {
                AgentLoopPhase.BOOTSTRAP,
                AgentLoopPhase.CHECKPOINT,
            }:
                raise StaleAgentLoopStateError(
                    "new observation requires BOOTSTRAP or CHECKPOINT"
                )
            if (
                observation.environment_id
                != state["identity"]["environment_id"]
            ):
                raise AgentLoopError(
                    "observation belongs to another environment"
                )
            if _instant(
                observation.available_at, "observation.available_at"
            ) > _instant(now, "at"):
                raise AgentLoopError(
                    "future observation is not available to AgentLoop"
                )
            current = self._empty_current()
            current["observation_id"] = observation.observation_id
            state["current"] = current
            state["external_effect_state"] = ExternalEffectState.NONE.value
            state["resume_phase"] = None
            state["phase"] = AgentLoopPhase.OBSERVE.value

        return self._mutate(at, apply)

    def advance(
        self, *, expected: AgentLoopPhase, at: str
    ) -> AgentLoopSnapshot:
        if not isinstance(expected, AgentLoopPhase):
            raise TypeError("expected must be AgentLoopPhase")
        target = _PRIMARY_NEXT.get(expected)
        if target is None:
            raise AgentLoopError(
                f"phase {expected.value} has no generic primary advance"
            )

        def apply(state: dict[str, Any], _now: str) -> None:
            current = AgentLoopPhase(state["phase"])
            if current is not expected:
                raise StaleAgentLoopStateError(
                    f"expected phase {expected.value}, "
                    f"current phase is {current.value}"
                )
            state["phase"] = target.value

        return self._mutate(at, apply)

    @staticmethod
    def _require_action_authority(
        action: Action,
        episode: Episode,
        state: dict[str, Any],
    ) -> None:
        if not isinstance(episode, Episode):
            raise TypeError("episode must be Episode")
        identity = state["identity"]
        if episode.environment_id != identity["environment_id"]:
            raise AgentLoopError("episode belongs to another environment")
        if episode.episode_id != identity["episode_id"]:
            raise AgentLoopError("episode identity does not match AgentLoop")
        if episode.policy_id != identity["policy_id"]:
            raise AgentLoopError("episode policy identity does not match AgentLoop")
        if action.action_type not in episode.admissible_actions:
            raise AgentLoopError(
                "action is outside the canonical episode admissible set"
            )

    def commit_action(
        self,
        action: Action,
        *,
        episode: Episode,
        effect_state: ExternalEffectState,
        at: str,
    ) -> ActionCommitReceipt:
        if not isinstance(action, Action):
            raise TypeError("action must be Action")
        if not isinstance(effect_state, ExternalEffectState):
            raise TypeError("effect_state must be ExternalEffectState")
        existing_state = self._read()
        self._require_action_authority(action, episode, existing_state)
        existing = next(
            (
                item
                for item in existing_state["decisions"]
                if item["observation_id"] == action.observation_id
            ),
            None,
        )
        if existing is not None:
            if existing["action_id"] != action.action_id:
                raise ConflictingAgentLoopEvidenceError(
                    "observation is already bound to another durable action"
                )
            persisted_effect = ExternalEffectState(
                existing["external_effect_state"]
            )
            if persisted_effect is not effect_state:
                raise ConflictingAgentLoopEvidenceError(
                    "durable action external-effect truth cannot be relabelled"
                )
            return ActionCommitReceipt(
                action_id=action.action_id,
                newly_committed=False,
                may_execute=False,
                external_effect_state=persisted_effect,
            )

        receipt_holder: dict[str, ActionCommitReceipt] = {}

        def apply(state: dict[str, Any], now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.ACT_OR_ABSTAIN
            ):
                raise StaleAgentLoopStateError(
                    "action commit requires ACT_OR_ABSTAIN"
                )
            self._require_action_authority(action, episode, state)
            current = state["current"]
            if action.environment_id != state["identity"]["environment_id"]:
                raise AgentLoopError("action belongs to another environment")
            if action.observation_id != current["observation_id"]:
                raise AgentLoopError(
                    "action does not bind the current observation"
                )
            if _instant(
                action.decided_at, "action.decided_at"
            ) > _instant(now, "at"):
                raise AgentLoopError("action decision is from the future")
            may_execute = (
                effect_state is not ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT
            )
            record = {
                "observation_id": action.observation_id,
                "action_id": action.action_id,
                "action_type": action.action_type,
                "decided_at": _timestamp_identity(
                    action.decided_at, "action.decided_at"
                ),
                "parameters_sha256": _digest(list(action.parameters)),
                "external_effect_state": effect_state.value,
                "may_execute": may_execute,
            }
            state["decisions"].append(record)
            current["action_id"] = action.action_id
            state["external_effect_state"] = effect_state.value
            state["phase"] = (
                AgentLoopPhase.UNKNOWN_EXTERNAL_EFFECT.value
                if effect_state
                is ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT
                else AgentLoopPhase.WAIT_OUTCOME.value
            )
            receipt_holder["value"] = ActionCommitReceipt(
                action_id=action.action_id,
                newly_committed=True,
                may_execute=may_execute,
                external_effect_state=effect_state,
            )

        self._mutate(at, apply)
        return receipt_holder["value"]

    def mark_unknown_external_effect(
        self, *, action_id: str, at: str
    ) -> AgentLoopSnapshot:
        wanted = _sha256(action_id, "action_id")

        def apply(state: dict[str, Any], _now: str) -> None:
            current = state["current"]
            if current["action_id"] != wanted:
                raise AgentLoopError(
                    "unknown external effect does not bind current action"
                )
            record = next(
                (
                    item
                    for item in state["decisions"]
                    if item["action_id"] == wanted
                ),
                None,
            )
            if record is None:
                raise AgentLoopError("action lacks durable decision record")
            record["external_effect_state"] = (
                ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT.value
            )
            record["may_execute"] = False
            state["external_effect_state"] = (
                ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT.value
            )
            state["phase"] = AgentLoopPhase.UNKNOWN_EXTERNAL_EFFECT.value

        return self._mutate(at, apply)

    def reconcile_unknown_external_effect(
        self,
        *,
        action_id: str,
        reconciled_as: ExternalEffectState,
        at: str,
    ) -> AgentLoopSnapshot:
        wanted = _sha256(action_id, "action_id")
        if reconciled_as not in {
            ExternalEffectState.NONE,
            ExternalEffectState.PAPER_ONLY,
        }:
            raise AgentLoopError(
                "reconciled effect must be NONE or PAPER_ONLY"
            )

        def apply(state: dict[str, Any], _now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.UNKNOWN_EXTERNAL_EFFECT
            ):
                raise StaleAgentLoopStateError(
                    "reconciliation requires UNKNOWN_EXTERNAL_EFFECT"
                )
            if state["current"]["action_id"] != wanted:
                raise AgentLoopError(
                    "reconciliation action identity mismatch"
                )
            record = next(
                item
                for item in state["decisions"]
                if item["action_id"] == wanted
            )
            record["external_effect_state"] = reconciled_as.value
            record["may_execute"] = False
            state["external_effect_state"] = reconciled_as.value
            state["phase"] = AgentLoopPhase.WAIT_OUTCOME.value

        return self._mutate(at, apply)

    def record_resolution(
        self,
        transition: Transition,
        *,
        outcome: Outcome,
        reward: RewardEvidence,
        at: str,
    ) -> AgentLoopSnapshot:
        if not isinstance(transition, Transition):
            raise TypeError("transition must be Transition")
        if not isinstance(outcome, Outcome) or not isinstance(
            reward, RewardEvidence
        ):
            raise TypeError(
                "outcome and reward must be canonical environment evidence"
            )

        existing = next(
            (
                item
                for item in self._read()["resolutions"]
                if item["transition_id"] == transition.transition_id
            ),
            None,
        )
        if existing is not None:
            expected = self._resolution_payload(
                transition, outcome, reward
            )
            if existing != expected:
                raise ConflictingAgentLoopEvidenceError(
                    "transition identity is already bound to "
                    "different resolution evidence"
                )
            return self.snapshot()

        payload = self._resolution_payload(transition, outcome, reward)

        def apply(state: dict[str, Any], now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.WAIT_OUTCOME
            ):
                raise StaleAgentLoopStateError(
                    "resolution requires WAIT_OUTCOME"
                )
            identity = state["identity"]
            current = state["current"]
            if (
                transition.environment_id != identity["environment_id"]
                or transition.episode_id != identity["episode_id"]
            ):
                raise AgentLoopError(
                    "transition belongs to another AgentLoop environment/episode"
                )
            if transition.action_id != current["action_id"]:
                raise AgentLoopError(
                    "transition does not bind current action"
                )
            if (
                transition.outcome_id != outcome.outcome_id
                or transition.reward_id != reward.reward_id
            ):
                raise AgentLoopError(
                    "transition does not bind exact outcome/reward identities"
                )
            if (
                outcome.action_id != transition.action_id
                or reward.action_id != transition.action_id
            ):
                raise AgentLoopError(
                    "resolution evidence does not bind transition action"
                )
            if reward.outcome_id != outcome.outcome_id:
                raise AgentLoopError("reward does not bind exact outcome")
            if outcome.truth is not reward.truth:
                raise AgentLoopError(
                    "outcome/reward truth labels must match in AgentLoop"
                )
            if (
                outcome.truth is EvidenceTruth.SIMULATED
                and outcome.simulation_model_id != reward.simulation_model_id
            ):
                raise AgentLoopError(
                    "simulated outcome/reward model identities must match"
                )
            now_i = _instant(now, "at")
            if (
                _instant(
                    outcome.revealed_at, "outcome.revealed_at"
                ) > now_i
                or _instant(
                    reward.available_at, "reward.available_at"
                ) > now_i
            ):
                raise AgentLoopError(
                    "future outcome/reward evidence cannot enter AgentLoop"
                )
            state["resolutions"].append(payload)
            current["transition_id"] = transition.transition_id
            current["outcome_id"] = outcome.outcome_id
            current["reward_id"] = reward.reward_id
            state["phase"] = AgentLoopPhase.EVALUATE.value

        return self._mutate(at, apply)

    @staticmethod
    def _resolution_payload(
        transition: Transition,
        outcome: Outcome,
        reward: RewardEvidence,
    ) -> dict[str, object]:
        return {
            "transition_id": transition.transition_id,
            "action_id": transition.action_id,
            "outcome_id": outcome.outcome_id,
            "reward_id": reward.reward_id,
            "reward_value": str(reward.reward),
            "truth": reward.truth.value,
            "simulation_model_id": reward.simulation_model_id,
            "reward_available_at": _timestamp_identity(
                reward.available_at, "reward.available_at"
            ),
        }

    def record_attribution(
        self,
        attribution: OutcomeAttribution,
        *,
        at: str,
    ) -> AgentLoopSnapshot:
        if not isinstance(attribution, OutcomeAttribution):
            raise TypeError("attribution must be OutcomeAttribution")
        state = self._read()
        existing = next(
            (
                item
                for item in state["attributions"]
                if item["attribution_id"] == attribution.attribution_id
            ),
            None,
        )
        payload = {
            "attribution_id": attribution.attribution_id,
            **attribution.payload(),
        }
        if existing is not None:
            if existing != payload:
                raise ConflictingAgentLoopEvidenceError(
                    "attribution identity is bound to different immutable content"
                )
            return self.snapshot()

        def apply(state: dict[str, Any], now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.ATTRIBUTE
            ):
                raise StaleAgentLoopStateError(
                    "attribution requires ATTRIBUTE"
                )
            current = state["current"]
            identity = state["identity"]
            if (
                attribution.environment_id != identity["environment_id"]
                or attribution.episode_id != identity["episode_id"]
            ):
                raise AgentLoopError(
                    "attribution belongs to another environment/episode"
                )
            for field in (
                "transition_id",
                "action_id",
                "outcome_id",
                "reward_id",
            ):
                if getattr(attribution, field) != current[field]:
                    raise AgentLoopError(
                        f"attribution {field} does not bind current evidence"
                    )
            resolution = next(
                item
                for item in state["resolutions"]
                if item["transition_id"] == attribution.transition_id
            )
            if str(attribution.reward_value) != resolution["reward_value"]:
                raise AgentLoopError(
                    "attribution reward value does not match durable RewardEvidence"
                )
            if (
                attribution.truth.value != resolution["truth"]
                or attribution.simulation_model_id
                != resolution["simulation_model_id"]
            ):
                raise AgentLoopError(
                    "attribution cannot relabel observed/simulated reward truth"
                )
            attributed = _instant(
                attribution.attributed_at, "attributed_at"
            )
            if attributed > _instant(now, "at"):
                raise AgentLoopError(
                    "attribution timestamp is in the future"
                )
            if attributed < _instant(
                resolution["reward_available_at"],
                "reward_available_at",
            ):
                raise AgentLoopError(
                    "attribution predates reward availability"
                )
            for finding in attribution.findings:
                if _instant(
                    finding.evidence_available_at,
                    "finding evidence_available_at",
                ) > attributed:
                    raise AgentLoopError(
                        "attribution uses future evidence"
                    )
            state["attributions"].append(payload)
            current["attribution_id"] = attribution.attribution_id
            state["phase"] = AgentLoopPhase.REFLECT.value

        return self._mutate(at, apply)

    def record_postmortem(
        self,
        postmortem: ReflectionPostmortem,
        *,
        at: str,
    ) -> AgentLoopSnapshot:
        if not isinstance(postmortem, ReflectionPostmortem):
            raise TypeError("postmortem must be ReflectionPostmortem")
        payload = {
            "postmortem_id": postmortem.postmortem_id,
            **postmortem.payload(),
        }
        existing = next(
            (
                item
                for item in self._read()["postmortems"]
                if item["postmortem_id"] == postmortem.postmortem_id
            ),
            None,
        )
        if existing is not None:
            if existing != payload:
                raise ConflictingAgentLoopEvidenceError(
                    "postmortem identity is bound to different immutable content"
                )
            return self.snapshot()

        def apply(state: dict[str, Any], now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.REFLECT
            ):
                raise StaleAgentLoopStateError(
                    "postmortem requires REFLECT"
                )
            current = state["current"]
            if (
                postmortem.attribution_id != current["attribution_id"]
                or postmortem.transition_id != current["transition_id"]
            ):
                raise AgentLoopError(
                    "postmortem does not bind current attribution/transition"
                )
            attribution = next(
                item
                for item in state["attributions"]
                if item["attribution_id"]
                == postmortem.attribution_id
            )
            created = _instant(
                postmortem.created_at, "postmortem.created_at"
            )
            if created > _instant(now, "at"):
                raise AgentLoopError(
                    "postmortem timestamp is in the future"
                )
            if created < _instant(
                attribution["attributed_at"], "attributed_at"
            ):
                raise AgentLoopError("postmortem predates attribution")
            known_components = {
                item["component"] for item in attribution["findings"]
            }
            if any(
                item.value not in known_components
                for item in postmortem.unresolved_components
            ):
                raise AgentLoopError(
                    "postmortem references attribution component not present"
                )
            state["postmortems"].append(payload)
            current["postmortem_id"] = postmortem.postmortem_id
            state["phase"] = (
                AgentLoopPhase.RESEARCH_HANDOFF.value
                if postmortem.research_question_statement is not None
                else AgentLoopPhase.CHECKPOINT.value
            )

        return self._mutate(at, apply)

    def handoff_research(
        self,
        supervisor: ResearchSupervisor,
        *,
        budget_units: int,
        at: str,
        deadline_at: str | None = None,
    ) -> SupervisorSnapshot:
        """Create one bounded question and route it through the canonical #537 ingress."""
        if not isinstance(supervisor, ResearchSupervisor):
            raise TypeError("supervisor must be ResearchSupervisor")
        if (
            isinstance(budget_units, bool)
            or not isinstance(budget_units, int)
            or budget_units <= 0
        ):
            raise AgentLoopError("budget_units must be a positive integer")
        normalized_deadline = (
            None
            if deadline_at is None
            else _timestamp_identity(deadline_at, "deadline_at")
        )
        state = self._read()
        if AgentLoopPhase(state["phase"]) is not AgentLoopPhase.RESEARCH_HANDOFF:
            current_run = state["current"]["research_run_id"]
            if current_run is not None:
                existing = next(
                    (
                        item
                        for item in state["research_handoffs"]
                        if item["run_id"] == current_run
                    ),
                    None,
                )
                if existing is None:
                    raise AgentLoopError("research run lacks durable handoff evidence")
                if (
                    existing["budget_units"] != budget_units
                    or existing["deadline_at"] != normalized_deadline
                ):
                    raise ConflictingAgentLoopEvidenceError(
                        "research handoff retry changes immutable budget/deadline"
                    )
                return supervisor.status(current_run)
            raise StaleAgentLoopStateError("research handoff requires RESEARCH_HANDOFF")

        current = state["current"]
        postmortem = next(
            item
            for item in state["postmortems"]
            if item["postmortem_id"] == current["postmortem_id"]
        )
        statement = postmortem["research_question_statement"]
        if statement is None:
            raise AgentLoopError("postmortem has no research question")
        requested_at = postmortem["created_at"]
        loop_id = state["identity"]["loop_id"]
        question_id = "agentloop-question-" + _digest(
            {
                "loop_id": loop_id,
                "postmortem_id": postmortem["postmortem_id"],
                "statement": statement,
            }
        )
        question = ResearchQuestion(
            question_id=question_id,
            statement=statement,
            source_sha256=state["identity"]["source_sha256"],
            created_at=requested_at,
        )
        registry = supervisor.scientific_registry
        registry.append(question)

        event = ExternalResearchTrigger(
            source_kind=ResearchTriggerSource.RECOVERY,
            source_scope=f"agent-loop:{loop_id}",
            source_event_id=postmortem["postmortem_id"],
            question_id=question.question_id,
            question_record_sha256=registry.get(
                "ResearchQuestion", question.question_id
            ).record_sha256,
            source_evidence_sha256=question.source_sha256,
            source_observed_at=requested_at,
            requested_at=requested_at,
            budget_units=budget_units,
            deadline_at=normalized_deadline,
        )
        receipt = ResearchTriggerAdapter(supervisor).accept(event)
        handoff = {
            "postmortem_id": postmortem["postmortem_id"],
            "question_id": question.question_id,
            "question_sha256": _digest(question.to_payload()),
            "trigger_id": receipt.supervisor_trigger_id,
            "trigger_sha256": receipt.supervisor_trigger_sha256,
            "run_id": receipt.run_id,
            "budget_units": budget_units,
            "deadline_at": normalized_deadline,
            "requested_at": requested_at,
            "source_event_identity_sha256": receipt.source_event_identity_sha256,
            "source_event_sha256": receipt.source_event_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "checkpoint_sha256": receipt.checkpoint_sha256,
        }

        def apply(latest: dict[str, Any], _now: str) -> None:
            if AgentLoopPhase(latest["phase"]) is not AgentLoopPhase.RESEARCH_HANDOFF:
                existing = next(
                    (
                        item
                        for item in latest["research_handoffs"]
                        if item["postmortem_id"] == postmortem["postmortem_id"]
                    ),
                    None,
                )
                if existing != handoff:
                    raise ConflictingAgentLoopEvidenceError(
                        "research handoff changed during durable publication"
                    )
                return
            existing = next(
                (
                    item
                    for item in latest["research_handoffs"]
                    if item["postmortem_id"] == postmortem["postmortem_id"]
                ),
                None,
            )
            if existing is not None and existing != handoff:
                raise ConflictingAgentLoopEvidenceError(
                    "postmortem is already bound to another research handoff"
                )
            if existing is None:
                latest["research_handoffs"].append(handoff)
            latest["current"]["research_question_id"] = question.question_id
            latest["current"]["research_trigger_id"] = receipt.supervisor_trigger_id
            latest["current"]["research_run_id"] = receipt.run_id
            latest["phase"] = AgentLoopPhase.CHECKPOINT.value

        self._mutate(at, apply)
        return supervisor.status(receipt.run_id)

    def commit_checkpoint(
        self,
        environment_checkpoint: EnvironmentCheckpoint,
        *,
        at: str,
    ) -> AgentLoopSnapshot:
        if not isinstance(environment_checkpoint, EnvironmentCheckpoint):
            raise TypeError(
                "environment_checkpoint must be EnvironmentCheckpoint"
            )

        def apply(state: dict[str, Any], _now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.CHECKPOINT
            ):
                raise StaleAgentLoopStateError(
                    "checkpoint commit requires CHECKPOINT phase"
                )
            identity = state["identity"]
            current = state["current"]
            if (
                environment_checkpoint.environment_id
                != identity["environment_id"]
            ):
                raise AgentLoopError(
                    "environment checkpoint environment mismatch"
                )
            if environment_checkpoint.episode_id != identity["episode_id"]:
                raise AgentLoopError(
                    "environment checkpoint episode mismatch"
                )
            if environment_checkpoint.policy_id != identity["policy_id"]:
                raise AgentLoopError(
                    "environment checkpoint policy mismatch"
                )
            if (
                current["transition_id"] is not None
                and environment_checkpoint.last_transition_id
                != current["transition_id"]
            ):
                raise AgentLoopError(
                    "environment checkpoint does not include current transition"
                )
            state["environment_checkpoint_id"] = (
                environment_checkpoint.checkpoint_id
            )

        return self._mutate(at, apply)

    def pause(self, *, at: str) -> AgentLoopSnapshot:
        def apply(state: dict[str, Any], _now: str) -> None:
            phase = AgentLoopPhase(state["phase"])
            if phase in {
                AgentLoopPhase.PAUSED,
                AgentLoopPhase.STOPPED,
            }:
                raise StaleAgentLoopStateError(
                    f"cannot pause from {phase.value}"
                )
            state["resume_phase"] = phase.value
            state["phase"] = AgentLoopPhase.PAUSED.value

        return self._mutate(at, apply)

    def resume(self, *, at: str) -> AgentLoopSnapshot:
        def apply(state: dict[str, Any], _now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.PAUSED
            ):
                raise StaleAgentLoopStateError(
                    "resume requires PAUSED"
                )
            if state["resume_phase"] is None:
                raise AgentLoopError(
                    "paused state lacks resume phase"
                )
            state["phase"] = AgentLoopPhase.RECOVERY.value

        return self._mutate(at, apply)

    def recover(self, *, at: str) -> AgentLoopSnapshot:
        def apply(state: dict[str, Any], _now: str) -> None:
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.RECOVERY
            ):
                raise StaleAgentLoopStateError(
                    "recover requires RECOVERY"
                )
            resume_phase = state["resume_phase"]
            if resume_phase is None:
                raise AgentLoopError(
                    "recovery lacks resume phase"
                )
            state["phase"] = AgentLoopPhase(resume_phase).value
            state["resume_phase"] = None

        return self._mutate(at, apply)

    def stop(self, *, at: str) -> AgentLoopSnapshot:
        def apply(state: dict[str, Any], _now: str) -> None:
            if AgentLoopPhase(state["phase"]) is AgentLoopPhase.STOPPED:
                return
            state["resume_phase"] = None
            state["phase"] = AgentLoopPhase.STOPPED.value

        return self._mutate(at, apply)
