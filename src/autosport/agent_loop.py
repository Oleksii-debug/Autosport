"""Restart-safe AgentLoop orchestration and causal credit-attribution seam.

This module composes existing Autosport authorities. It does not choose actions,
compute rewards, widen EconomicGoal/Risk limits, mutate promotion state, or execute
provider actions. CausalLearningEnvironment remains the owner of observation/action/
outcome/reward transitions and ResearchSupervisor remains the research-run authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .learning_environment import (
    Action,
    EnvironmentCheckpoint,
    EnvironmentIdentity,
    Episode,
    LearningEnvironmentError,
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
from .skill_registry import (
    AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE,
    SkillRegistry,
    SkillRun,
)
from .workspace_lock import WorkspaceEconomicLock


AGENT_LOOP_SCHEMA: Final = "autosport.agent_loop"
AGENT_LOOP_SCHEMA_VERSION: Final = 3
AGENT_LOOP_LEGACY_SCHEMA_VERSION: Final = 2
_AGENT_LOOP_MONOTONIC_DOMAIN: Final = "agent-loop-state"
_AGENT_LOOP_MONOTONIC_BINDING_SCHEMA: Final = "autosport.agent_loop.monotonic_binding"
_AGENT_LOOP_MONOTONIC_BINDING_VERSION: Final = 1
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
    checkpointed_transition_id: str | None
    external_effect_state: ExternalEffectState
    resume_phase: AgentLoopPhase | None
    updated_at: str
    state_sha256: str
    activation_binding_id: str | None = None


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
        activation_binding_id: str | None = None,
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
        if activation_binding_id is not None:
            identity["activation_binding_id"] = _sha256(
                activation_binding_id, "activation_binding_id"
            )
        initial = {
            "schema": AGENT_LOOP_SCHEMA,
            "schema_version": AGENT_LOOP_SCHEMA_VERSION,
            "identity": identity,
            "phase": AgentLoopPhase.BOOTSTRAP.value,
            "sequence": 0,
            "current": cls._empty_current(),
            "environment_checkpoint_id": environment_checkpoint.checkpoint_id,
            "checkpointed_transition_id": environment_checkpoint.last_transition_id,
            "checkpoint_history": [
                cls._checkpoint_record(
                    environment_checkpoint,
                    committed_at=_timestamp_identity(at, "at"),
                )
            ],
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
        runtime = object.__new__(cls)
        runtime.path = target
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                runtime._assert_pristine_creation_allowed()
                cls._write_state(target, initial)
            else:
                existing = runtime._read_local()
                runtime._ensure_monotonic_state(
                    existing,
                    adopt_if_missing=existing["sequence"] > 0,
                )
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
    def _checkpoint_record(
        checkpoint: EnvironmentCheckpoint,
        *,
        committed_at: str,
    ) -> dict[str, Any]:
        if not isinstance(checkpoint, EnvironmentCheckpoint):
            raise TypeError("checkpoint must be EnvironmentCheckpoint")
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "environment_id": checkpoint.environment_id,
            "episode_id": checkpoint.episode_id,
            "policy_id": checkpoint.policy_id,
            "step_index": checkpoint.step_index,
            "chain_sha256": checkpoint.chain_sha256,
            "last_transition_id": checkpoint.last_transition_id,
            "committed_action_ids": list(checkpoint.committed_action_ids),
            "committed_decision_intents": [
                [intent_id, payload_id]
                for intent_id, payload_id in checkpoint.committed_decision_intents
            ],
            "committed_at": _timestamp_identity(committed_at, "checkpoint committed_at"),
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

    @staticmethod
    def _monotonic_key(path: Path) -> str:
        name = os.path.normcase(Path(path).name)
        return "agent-loop-" + hashlib.sha256(name.encode("utf-8")).hexdigest()

    @classmethod
    def _monotonic_authority(cls, path: Path) -> MonotonicWorkspaceAuthority:
        absolute = Path(os.path.abspath(os.fspath(path)))
        return MonotonicWorkspaceAuthority(
            workspace=absolute.parent,
            domain=_AGENT_LOOP_MONOTONIC_DOMAIN,
            key=cls._monotonic_key(absolute),
        )

    @classmethod
    def _monotonic_binding(cls, path: Path, state: dict[str, Any]) -> str:
        return _digest(
            {
                "schema": _AGENT_LOOP_MONOTONIC_BINDING_SCHEMA,
                "schema_version": _AGENT_LOOP_MONOTONIC_BINDING_VERSION,
                "state_key": cls._monotonic_key(path),
                "identity": state["identity"],
            }
        )

    @staticmethod
    def _monotonic_tx_id(
        *,
        operation: str,
        observed_state_sha256: str | None,
        intended_state_sha256: str,
        semantic_binding_sha256: str,
        authority_tip_sha256: str | None = None,
    ) -> str:
        return _digest(
            {
                "operation": operation,
                "observed_state_sha256": observed_state_sha256,
                "intended_state_sha256": intended_state_sha256,
                "semantic_binding_sha256": semantic_binding_sha256,
                "authority_tip_sha256": authority_tip_sha256,
            }
        )

    @staticmethod
    def _raise_monotonic_error(exc: MonotonicWorkspaceAuthorityError) -> None:
        raise AgentLoopError(
            f"AgentLoop monotonic state authority rejected local state: {exc}"
        ) from exc

    def _assert_pristine_creation_allowed(self) -> None:
        try:
            history = self._monotonic_authority(self.path).read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)
        if history:
            raise AgentLoopError(
                "AgentLoop state is missing but monotonic authority preserves prior history"
            )

    def _ensure_monotonic_state(
        self,
        state: dict[str, Any],
        *,
        adopt_if_missing: bool,
    ) -> None:
        state_sha256 = state["state_sha256"]
        binding_sha256 = self._monotonic_binding(self.path, state)
        try:
            authority = self._monotonic_authority(self.path)
            history = authority.read_history()
            if not history:
                if not adopt_if_missing:
                    return
                tx_id = self._monotonic_tx_id(
                    operation="ADOPT_VALIDATED_BASELINE",
                    observed_state_sha256=None,
                    intended_state_sha256=state_sha256,
                    semantic_binding_sha256=binding_sha256,
                )
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=state_sha256,
                    semantic_binding_sha256=binding_sha256,
                )
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=state_sha256,
                    semantic_binding_sha256=binding_sha256,
                )
                return

            latest = history[-1]
            if latest.phase is AuthorityPhase.PREPARE:
                authority.recover(
                    observed_state_sha256=state_sha256,
                    tx_id=latest.tx_id,
                    semantic_binding_sha256=binding_sha256,
                )
            else:
                authority.recover(observed_state_sha256=state_sha256)
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _publish_monotonic_state(
        self,
        *,
        observed_state_sha256: str,
        state_without_digest: dict[str, Any],
    ) -> None:
        intended_state_sha256 = _digest(state_without_digest)
        candidate = dict(state_without_digest)
        candidate["state_sha256"] = intended_state_sha256
        binding_sha256 = self._monotonic_binding(self.path, candidate)
        try:
            authority = self._monotonic_authority(self.path)
            history = authority.read_history()
            if not history:
                raise AgentLoopError(
                    "AgentLoop monotonic baseline is missing before state publication"
                )
            if history[-1].phase is AuthorityPhase.PREPARE:
                raise AgentLoopError(
                    "AgentLoop monotonic authority still has an unresolved PREPARE"
                )
            tx_id = self._monotonic_tx_id(
                operation="PUBLISH",
                observed_state_sha256=observed_state_sha256,
                intended_state_sha256=intended_state_sha256,
                semantic_binding_sha256=binding_sha256,
                authority_tip_sha256=history[-1].record_sha256,
            )
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed_state_sha256,
                intended_state_sha256=intended_state_sha256,
                semantic_binding_sha256=binding_sha256,
            )
            self._write_state(self.path, state_without_digest)
            published = self._read_local()
            if published["state_sha256"] != intended_state_sha256:
                raise AgentLoopError(
                    "published AgentLoop state digest differs from prepared monotonic state"
                )
            if self._monotonic_binding(self.path, published) != binding_sha256:
                raise AgentLoopError(
                    "published AgentLoop identity differs from prepared monotonic binding"
                )
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended_state_sha256,
                semantic_binding_sha256=binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _read(self) -> dict[str, Any]:
        with WorkspaceEconomicLock(self.path.parent):
            try:
                state = self._read_local()
            except FileNotFoundError:
                self._assert_pristine_creation_allowed()
                raise
            self._ensure_monotonic_state(
                state,
                adopt_if_missing=state["sequence"] > 0,
            )
            return state

    def _read_local(self) -> dict[str, Any]:
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

        version = state.get("schema_version")
        if (
            state.get("schema") != AGENT_LOOP_SCHEMA
            or version
            not in {
                AGENT_LOOP_LEGACY_SCHEMA_VERSION,
                AGENT_LOOP_SCHEMA_VERSION,
            }
        ):
            raise AgentLoopError("AgentLoop schema mismatch")
        required = {
            "schema",
            "schema_version",
            "identity",
            "phase",
            "sequence",
            "current",
            "environment_checkpoint_id",
            "checkpointed_transition_id",
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
        if version == AGENT_LOOP_SCHEMA_VERSION:
            required.add("checkpoint_history")
        if set(state) != required:
            raise AgentLoopError("AgentLoop state fields mismatch")
        identity = state["identity"]
        identity_fields = {
            "loop_id",
            "environment_id",
            "episode_id",
            "policy_id",
            "economic_goal_fingerprint",
            "risk_fingerprint",
            "source_sha256",
            "config_sha256",
        }
        if (
            type(identity) is not dict
            or set(identity)
            not in (
                identity_fields,
                identity_fields | {"activation_binding_id"},
            )
        ):
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
        if "activation_binding_id" in identity:
            _sha256(identity["activation_binding_id"], "activation_binding_id")
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
        if state["checkpointed_transition_id"] is not None:
            _sha256(
                state["checkpointed_transition_id"],
                "checkpointed_transition_id",
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
        if version == AGENT_LOOP_LEGACY_SCHEMA_VERSION:
            self._validate_legacy_v2_history(state)
        else:
            self._validate_history(state)
        return state

    @staticmethod
    def _validate_legacy_v2_history(state: dict[str, Any]) -> None:
        """Validate every semantic relationship provable from integrated v2.

        V2 intentionally lacks checkpoint history and decision intent/payload
        witnesses. Those fields are not invented here; all shared durable
        history/current/effect/phase invariants are validated by the canonical
        semantic validator in legacy mode.
        """

        AgentLoopRuntime._validate_history(state, legacy_v2=True)

    @staticmethod
    def _validate_history(state: dict[str, Any], *, legacy_v2: bool = False) -> None:
        def require_fields(
            record: object,
            *,
            expected: set[str],
            label: str,
        ) -> dict[str, Any]:
            if type(record) is not dict:
                raise AgentLoopError(f"{label} must be an object")
            if set(record) != expected:
                raise AgentLoopError(f"{label} fields mismatch")
            return record

        def stored_decimal(value: object, name: str) -> Decimal:
            if type(value) is not str:
                raise AgentLoopError(f"{name} must be a canonical Decimal string")
            try:
                parsed = Decimal(value)
            except ArithmeticError as exc:
                raise AgentLoopError(
                    f"{name} must be a canonical Decimal string"
                ) from exc
            if not parsed.is_finite() or str(parsed) != value:
                raise AgentLoopError(f"{name} must be a canonical finite Decimal")
            return parsed

        identity = state["identity"]
        decisions_by_action: dict[str, dict[str, Any]] = {}
        observations: set[str] = set()
        decisions_without_canonical_parameters: set[str] = set()
        decision_fields = {
            "observation_id",
            "action_id",
            "action_type",
            "decided_at",
            "parameters_sha256",
            "external_effect_state",
            "may_execute",
        }
        if not legacy_v2:
            decision_fields.update(
                {"parameters", "decision_intent_id", "decision_payload_id"}
            )
        for record in state["decisions"]:
            record = require_fields(
                record,
                expected=decision_fields,
                label="decision record",
            )
            _sha256(record.get("action_id"), "decision action_id")
            _sha256(record.get("observation_id"), "decision observation_id")
            _text(record.get("action_type"), "decision action_type")
            if (
                _timestamp_identity(record.get("decided_at"), "decision decided_at")
                != record["decided_at"]
            ):
                raise AgentLoopError("decision decided_at is not canonical")
            _sha256(record.get("parameters_sha256"), "decision parameters_sha256")
            if not legacy_v2:
                _sha256(record.get("decision_intent_id"), "decision intent_id")
                _sha256(record.get("decision_payload_id"), "decision payload_id")
                expected_intent_id = _digest(
                    {
                        "environment_id": identity["environment_id"],
                        "episode_id": identity["episode_id"],
                        "observation_id": record["observation_id"],
                    }
                )
                if record["decision_intent_id"] != expected_intent_id:
                    raise AgentLoopError("decision intent does not bind observation")
                raw_parameters = record["parameters"]
                if raw_parameters is None:
                    decisions_without_canonical_parameters.add(record["action_id"])
                else:
                    if type(raw_parameters) is not list:
                        raise AgentLoopError(
                            "decision parameters must be a canonical list"
                        )
                    parameter_pairs: list[tuple[str, str]] = []
                    for item in raw_parameters:
                        if type(item) is not list or len(item) != 2:
                            raise AgentLoopError(
                                "decision parameters entry mismatch"
                            )
                        parameter_pairs.append((item[0], item[1]))
                    try:
                        canonical_action = Action(
                            environment_id=identity["environment_id"],
                            observation_id=record["observation_id"],
                            action_type=record["action_type"],
                            decided_at=record["decided_at"],
                            parameters=tuple(parameter_pairs),
                        )
                    except LearningEnvironmentError as exc:
                        raise AgentLoopError(
                            "decision parameters are not canonical"
                        ) from exc
                    canonical_parameters = [
                        [key, value] for key, value in canonical_action.parameters
                    ]
                    if raw_parameters != canonical_parameters:
                        raise AgentLoopError(
                            "decision parameters are not canonical"
                        )
                    if record["parameters_sha256"] != _digest(canonical_parameters):
                        raise AgentLoopError(
                            "decision parameters hash mismatch"
                        )
                    if record["action_id"] != canonical_action.action_id:
                        raise AgentLoopError(
                            "decision action identity mismatch"
                        )
                    expected_payload_id = _digest(
                        {
                            "action_type": canonical_action.action_type,
                            "parameters": canonical_parameters,
                        }
                    )
                    if record["decision_payload_id"] != expected_payload_id:
                        raise AgentLoopError(
                            "decision payload identity mismatch"
                        )
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
            if record["action_id"] in decisions_by_action or record[
                "observation_id"
            ] in observations:
                raise AgentLoopError(
                    "AgentLoop contains duplicate durable decision identity"
                )
            decisions_by_action[record["action_id"]] = record
            observations.add(record["observation_id"])

        resolutions_by_transition: dict[str, dict[str, Any]] = {}
        resolved_actions: set[str] = set()
        resolved_outcomes: set[str] = set()
        resolved_rewards: set[str] = set()
        for record in state["resolutions"]:
            record = require_fields(
                record,
                expected={
                    "transition_id",
                    "action_id",
                    "outcome_id",
                    "reward_id",
                    "reward_value",
                    "truth",
                    "simulation_model_id",
                    "reward_available_at",
                },
                label="resolution record",
            )
            for key in ("transition_id", "action_id", "outcome_id", "reward_id"):
                _sha256(record[key], f"resolution {key}")
            stored_decimal(record["reward_value"], "resolution reward_value")
            try:
                truth = EvidenceTruth(record["truth"])
            except (TypeError, ValueError) as exc:
                raise AgentLoopError("resolution truth is invalid") from exc
            if truth is EvidenceTruth.SIMULATED:
                _text(record["simulation_model_id"], "resolution simulation_model_id")
            elif record["simulation_model_id"] is not None:
                raise AgentLoopError(
                    "observed resolution cannot carry simulation_model_id"
                )
            if (
                _timestamp_identity(
                    record["reward_available_at"], "resolution reward_available_at"
                )
                != record["reward_available_at"]
            ):
                raise AgentLoopError("resolution reward_available_at is not canonical")
            decision = decisions_by_action.get(record["action_id"])
            if decision is None:
                raise AgentLoopError("resolution does not bind a durable decision")
            if _instant(record["reward_available_at"], "reward_available_at") < _instant(
                decision["decided_at"], "decision decided_at"
            ):
                raise AgentLoopError("resolution reward predates its decision")
            if (
                record["transition_id"] in resolutions_by_transition
                or record["action_id"] in resolved_actions
                or record["outcome_id"] in resolved_outcomes
                or record["reward_id"] in resolved_rewards
            ):
                raise AgentLoopError("AgentLoop contains duplicate resolution identity")
            resolutions_by_transition[record["transition_id"]] = record
            resolved_actions.add(record["action_id"])
            resolved_outcomes.add(record["outcome_id"])
            resolved_rewards.add(record["reward_id"])

        if legacy_v2:
            checkpointed_transition_id = state["checkpointed_transition_id"]
            if checkpointed_transition_id is None:
                if len(state["resolutions"]) > 1:
                    raise AgentLoopError(
                        "legacy AgentLoop has multiple resolutions beyond checkpoint head"
                    )
            else:
                checkpointed_index = next(
                    (
                        index
                        for index, resolution in enumerate(state["resolutions"])
                        if resolution["transition_id"] == checkpointed_transition_id
                    ),
                    None,
                )
                if checkpointed_index is None:
                    raise AgentLoopError(
                        "legacy checkpoint head does not bind durable resolution history"
                    )
                if len(state["resolutions"]) - checkpointed_index - 1 > 1:
                    raise AgentLoopError(
                        "legacy AgentLoop has multiple resolutions beyond checkpoint head"
                    )
        else:
            checkpoint_history = state["checkpoint_history"]
            if type(checkpoint_history) is not list or not checkpoint_history:
                raise AgentLoopError("checkpoint_history must be a non-empty list")
            parsed_checkpoints: list[tuple[EnvironmentCheckpoint, str]] = []
            for index, raw_checkpoint in enumerate(checkpoint_history):
                record = require_fields(
                    raw_checkpoint,
                    expected={
                        "checkpoint_id",
                        "environment_id",
                        "episode_id",
                        "policy_id",
                        "step_index",
                        "chain_sha256",
                        "last_transition_id",
                        "committed_action_ids",
                        "committed_decision_intents",
                        "committed_at",
                    },
                    label="checkpoint record",
                )
                if type(record["committed_action_ids"]) is not list:
                    raise AgentLoopError("checkpoint committed_action_ids must be a list")
                if type(record["committed_decision_intents"]) is not list:
                    raise AgentLoopError(
                        "checkpoint committed_decision_intents must be a list"
                    )
                intents: list[tuple[str, str]] = []
                for item in record["committed_decision_intents"]:
                    if type(item) is not list or len(item) != 2:
                        raise AgentLoopError(
                            "checkpoint committed_decision_intents entry mismatch"
                        )
                    intents.append(
                        (
                            _sha256(item[0], "checkpoint decision intent_id"),
                            _sha256(item[1], "checkpoint decision payload_id"),
                        )
                    )
                try:
                    checkpoint = EnvironmentCheckpoint(
                        environment_id=record["environment_id"],
                        episode_id=record["episode_id"],
                        policy_id=record["policy_id"],
                        step_index=record["step_index"],
                        chain_sha256=record["chain_sha256"],
                        last_transition_id=record["last_transition_id"],
                        committed_action_ids=tuple(record["committed_action_ids"]),
                        committed_decision_intents=tuple(intents),
                    )
                except LearningEnvironmentError as exc:
                    raise AgentLoopError("checkpoint record is not canonical") from exc
                if record["checkpoint_id"] != checkpoint.checkpoint_id:
                    raise AgentLoopError("checkpoint record identity mismatch")
                if (
                    checkpoint.environment_id != identity["environment_id"]
                    or checkpoint.episode_id != identity["episode_id"]
                    or checkpoint.policy_id != identity["policy_id"]
                ):
                    raise AgentLoopError("checkpoint belongs to another AgentLoop")
                committed_at = _timestamp_identity(
                    record["committed_at"], "checkpoint committed_at"
                )
                if committed_at != record["committed_at"]:
                    raise AgentLoopError("checkpoint committed_at is not canonical")
                if not parsed_checkpoints:
                    if checkpoint.step_index > len(state["resolutions"]):
                        raise AgentLoopError(
                            "checkpoint baseline advances beyond durable resolution history"
                        )
                    baseline_resolutions = state["resolutions"][: checkpoint.step_index]
                    expected_chain = hashlib.sha256(b"").hexdigest()
                    expected_actions: list[str] = []
                    expected_intents: dict[str, str] = {}
                    for resolution in baseline_resolutions:
                        expected_chain = hashlib.sha256(
                            bytes.fromhex(expected_chain)
                            + bytes.fromhex(resolution["transition_id"])
                        ).hexdigest()
                        expected_actions.append(resolution["action_id"])
                        decision = decisions_by_action.get(resolution["action_id"])
                        if decision is None:
                            raise AgentLoopError(
                                "checkpoint baseline resolution lacks durable decision"
                            )
                        intent_id = decision["decision_intent_id"]
                        if intent_id in expected_intents:
                            raise AgentLoopError(
                                "checkpoint baseline reuses a committed decision intent"
                            )
                        expected_intents[intent_id] = decision["decision_payload_id"]
                        if _instant(
                            committed_at, "checkpoint committed_at"
                        ) < _instant(
                            resolution["reward_available_at"],
                            "reward_available_at",
                        ):
                            raise AgentLoopError(
                                "checkpoint baseline predates durable resolution evidence"
                            )
                    expected_last_transition = (
                        None
                        if not baseline_resolutions
                        else baseline_resolutions[-1]["transition_id"]
                    )
                    if checkpoint.last_transition_id != expected_last_transition:
                        raise AgentLoopError(
                            "checkpoint baseline head does not bind durable resolution history"
                        )
                    if checkpoint.chain_sha256 != expected_chain:
                        raise AgentLoopError(
                            "checkpoint baseline chain does not bind durable transition history"
                        )
                    if checkpoint.committed_action_ids != tuple(
                        sorted(expected_actions)
                    ):
                        raise AgentLoopError(
                            "checkpoint baseline actions do not bind durable history"
                        )
                    if checkpoint.committed_decision_intents != tuple(
                        sorted(expected_intents.items())
                    ):
                        raise AgentLoopError(
                            "checkpoint baseline decision intents do not bind durable history"
                        )
                else:
                    previous, previous_at = parsed_checkpoints[-1]
                    if checkpoint.step_index != previous.step_index + 1:
                        raise AgentLoopError(
                            "checkpoint step_index does not advance by one"
                        )
                    resolution_index = checkpoint.step_index - 1
                    if resolution_index >= len(state["resolutions"]):
                        raise AgentLoopError(
                            "checkpoint advances beyond durable resolution history"
                        )
                    resolution = state["resolutions"][resolution_index]
                    if checkpoint.last_transition_id != resolution["transition_id"]:
                        raise AgentLoopError(
                            "checkpoint head does not bind durable resolution history"
                        )
                    expected_chain = hashlib.sha256(
                        bytes.fromhex(previous.chain_sha256)
                        + bytes.fromhex(resolution["transition_id"])
                    ).hexdigest()
                    if checkpoint.chain_sha256 != expected_chain:
                        raise AgentLoopError(
                            "checkpoint chain does not bind durable transition history"
                        )
                    expected_actions = tuple(
                        sorted((*previous.committed_action_ids, resolution["action_id"]))
                    )
                    if checkpoint.committed_action_ids != expected_actions:
                        raise AgentLoopError(
                            "checkpoint committed actions do not bind durable history"
                        )
                    decision = decisions_by_action.get(resolution["action_id"])
                    if decision is None:
                        raise AgentLoopError(
                            "checkpoint resolution lacks durable decision"
                        )
                    expected_intents = dict(previous.committed_decision_intents)
                    if decision["decision_intent_id"] in expected_intents:
                        raise AgentLoopError(
                            "checkpoint reuses a committed decision intent"
                        )
                    expected_intents[decision["decision_intent_id"]] = decision[
                        "decision_payload_id"
                    ]
                    if checkpoint.committed_decision_intents != tuple(
                        sorted(expected_intents.items())
                    ):
                        raise AgentLoopError(
                            "checkpoint decision intents do not bind durable history"
                        )
                    if _instant(committed_at, "checkpoint committed_at") < _instant(
                        resolution["reward_available_at"], "reward_available_at"
                    ):
                        raise AgentLoopError(
                            "checkpoint predates durable resolution evidence"
                        )
                    if _instant(committed_at, "checkpoint committed_at") < _instant(
                        previous_at, "previous checkpoint committed_at"
                    ):
                        raise AgentLoopError("checkpoint history moves backwards")
                parsed_checkpoints.append((checkpoint, committed_at))
            first_checkpoint, _ = parsed_checkpoints[0]
            migration_baseline_actions = (
                set(first_checkpoint.committed_action_ids)
                if first_checkpoint.step_index > 0
                else set()
            )
            if not decisions_without_canonical_parameters.issubset(
                migration_baseline_actions
            ):
                raise AgentLoopError(
                    "decision parameters are missing outside legacy migration baseline"
                )
            latest_checkpoint, _ = parsed_checkpoints[-1]
            committed_resolution_count = latest_checkpoint.step_index
            if len(state["resolutions"]) - committed_resolution_count not in {0, 1}:
                raise AgentLoopError(
                    "AgentLoop has resolutions outside the checkpoint progression"
                )
            if state["environment_checkpoint_id"] != latest_checkpoint.checkpoint_id:
                raise AgentLoopError(
                    "environment checkpoint id differs from latest durable checkpoint"
                )
            if state["checkpointed_transition_id"] != latest_checkpoint.last_transition_id:
                raise AgentLoopError(
                    "checkpoint head differs from latest durable checkpoint"
                )

        attributions_by_id: dict[str, dict[str, Any]] = {}
        attributed_transitions: set[str] = set()
        for record in state["attributions"]:
            record = require_fields(
                record,
                expected={
                    "attribution_id",
                    "environment_id",
                    "episode_id",
                    "transition_id",
                    "action_id",
                    "outcome_id",
                    "reward_id",
                    "reward_value",
                    "truth",
                    "simulation_model_id",
                    "attributed_at",
                    "findings",
                },
                label="attribution record",
            )
            findings_raw = record["findings"]
            if type(findings_raw) is not list:
                raise AgentLoopError("attribution findings must be a list")
            findings: list[AttributionFinding] = []
            for raw_finding in findings_raw:
                raw_finding = require_fields(
                    raw_finding,
                    expected={
                        "component",
                        "status",
                        "evidence_sha256",
                        "evidence_available_at",
                        "contribution",
                        "reason_code",
                    },
                    label="attribution finding",
                )
                try:
                    component = AttributionComponent(raw_finding["component"])
                    status = AttributionStatus(raw_finding["status"])
                except (TypeError, ValueError) as exc:
                    raise AgentLoopError("attribution finding enum is invalid") from exc
                contribution = (
                    None
                    if raw_finding["contribution"] is None
                    else stored_decimal(
                        raw_finding["contribution"], "attribution contribution"
                    )
                )
                findings.append(
                    AttributionFinding(
                        component=component,
                        status=status,
                        evidence_sha256=raw_finding["evidence_sha256"],
                        evidence_available_at=raw_finding["evidence_available_at"],
                        contribution=contribution,
                        reason_code=raw_finding["reason_code"],
                    )
                )
            try:
                truth = EvidenceTruth(record["truth"])
            except (TypeError, ValueError) as exc:
                raise AgentLoopError("attribution truth is invalid") from exc
            attribution = OutcomeAttribution(
                environment_id=record["environment_id"],
                episode_id=record["episode_id"],
                transition_id=record["transition_id"],
                action_id=record["action_id"],
                outcome_id=record["outcome_id"],
                reward_id=record["reward_id"],
                reward_value=stored_decimal(
                    record["reward_value"], "attribution reward_value"
                ),
                truth=truth,
                simulation_model_id=record["simulation_model_id"],
                attributed_at=record["attributed_at"],
                findings=tuple(findings),
            )
            expected = {"attribution_id": attribution.attribution_id, **attribution.payload()}
            if record != expected:
                raise AgentLoopError("attribution record is not canonical")
            if (
                attribution.environment_id != identity["environment_id"]
                or attribution.episode_id != identity["episode_id"]
            ):
                raise AgentLoopError("attribution belongs to another AgentLoop")
            resolution = resolutions_by_transition.get(attribution.transition_id)
            if resolution is None:
                raise AgentLoopError("attribution does not bind a durable resolution")
            for key in ("action_id", "outcome_id", "reward_id", "reward_value"):
                actual = (
                    str(attribution.reward_value)
                    if key == "reward_value"
                    else getattr(attribution, key)
                )
                if actual != resolution[key]:
                    raise AgentLoopError(
                        f"attribution {key} does not bind its resolution"
                    )
            if (
                attribution.truth.value != resolution["truth"]
                or attribution.simulation_model_id
                != resolution["simulation_model_id"]
            ):
                raise AgentLoopError("attribution relabels resolution truth")
            if _instant(attribution.attributed_at, "attributed_at") < _instant(
                resolution["reward_available_at"], "reward_available_at"
            ):
                raise AgentLoopError("attribution predates reward availability")
            if (
                attribution.attribution_id in attributions_by_id
                or attribution.transition_id in attributed_transitions
            ):
                raise AgentLoopError("AgentLoop contains duplicate attribution identity")
            attributions_by_id[attribution.attribution_id] = record
            attributed_transitions.add(attribution.transition_id)

        postmortems_by_id: dict[str, dict[str, Any]] = {}
        reflected_attributions: set[str] = set()
        for record in state["postmortems"]:
            record = require_fields(
                record,
                expected={
                    "postmortem_id",
                    "attribution_id",
                    "transition_id",
                    "created_at",
                    "unresolved_components",
                    "summary_code",
                    "research_question_statement",
                },
                label="postmortem record",
            )
            unresolved_raw = record["unresolved_components"]
            if type(unresolved_raw) is not list:
                raise AgentLoopError("postmortem unresolved_components must be a list")
            try:
                unresolved = tuple(
                    AttributionComponent(value) for value in unresolved_raw
                )
            except (TypeError, ValueError) as exc:
                raise AgentLoopError("postmortem component is invalid") from exc
            postmortem = ReflectionPostmortem(
                attribution_id=record["attribution_id"],
                transition_id=record["transition_id"],
                created_at=record["created_at"],
                unresolved_components=unresolved,
                summary_code=record["summary_code"],
                research_question_statement=record["research_question_statement"],
            )
            expected = {"postmortem_id": postmortem.postmortem_id, **postmortem.payload()}
            if record != expected:
                raise AgentLoopError("postmortem record is not canonical")
            attribution = attributions_by_id.get(postmortem.attribution_id)
            if attribution is None:
                raise AgentLoopError("postmortem does not bind a durable attribution")
            if postmortem.transition_id != attribution["transition_id"]:
                raise AgentLoopError("postmortem transition does not bind attribution")
            if _instant(postmortem.created_at, "postmortem.created_at") < _instant(
                attribution["attributed_at"], "attributed_at"
            ):
                raise AgentLoopError("postmortem predates attribution")
            statuses = {
                item["component"]: item["status"] for item in attribution["findings"]
            }
            if any(
                item.value not in statuses
                or statuses[item.value] not in {
                    AttributionStatus.UNKNOWN.value,
                    AttributionStatus.MIXED.value,
                }
                for item in postmortem.unresolved_components
            ):
                raise AgentLoopError("postmortem unresolved components are unsupported")
            if (
                postmortem.postmortem_id in postmortems_by_id
                or postmortem.attribution_id in reflected_attributions
            ):
                raise AgentLoopError("AgentLoop contains duplicate postmortem identity")
            postmortems_by_id[postmortem.postmortem_id] = record
            reflected_attributions.add(postmortem.attribution_id)

        handoffs_by_postmortem: dict[str, dict[str, Any]] = {}
        handoff_runs: set[str] = set()
        for record in state["research_handoffs"]:
            record = require_fields(
                record,
                expected={
                    "postmortem_id",
                    "question_id",
                    "question_sha256",
                    "trigger_id",
                    "trigger_sha256",
                    "run_id",
                    "budget_units",
                    "deadline_at",
                    "requested_at",
                    "source_event_identity_sha256",
                    "source_event_sha256",
                    "receipt_sha256",
                    "checkpoint_sha256",
                },
                label="research handoff record",
            )
            _sha256(record["postmortem_id"], "handoff postmortem_id")
            _text(record["question_id"], "handoff question_id")
            _text(record["trigger_id"], "handoff trigger_id")
            for key in (
                "question_sha256",
                "trigger_sha256",
                "run_id",
                "source_event_identity_sha256",
                "source_event_sha256",
                "receipt_sha256",
                "checkpoint_sha256",
            ):
                _sha256(record[key], f"handoff {key}")
            if (
                isinstance(record["budget_units"], bool)
                or not isinstance(record["budget_units"], int)
                or record["budget_units"] <= 0
            ):
                raise AgentLoopError("handoff budget_units must be a positive integer")
            requested_at = _timestamp_identity(record["requested_at"], "requested_at")
            if requested_at != record["requested_at"]:
                raise AgentLoopError("handoff requested_at is not canonical")
            if record["deadline_at"] is not None:
                deadline_at = _timestamp_identity(record["deadline_at"], "deadline_at")
                if deadline_at != record["deadline_at"]:
                    raise AgentLoopError("handoff deadline_at is not canonical")
                if _instant(deadline_at, "deadline_at") < _instant(
                    requested_at, "requested_at"
                ):
                    raise AgentLoopError("handoff deadline predates request")
            postmortem = postmortems_by_id.get(record["postmortem_id"])
            if postmortem is None:
                raise AgentLoopError("research handoff does not bind a postmortem")
            statement = postmortem["research_question_statement"]
            if statement is None:
                raise AgentLoopError("research handoff postmortem lacks a question")
            if requested_at != postmortem["created_at"]:
                raise AgentLoopError("research handoff request time differs from postmortem")
            expected_question_id = "agentloop-question-" + _digest(
                {
                    "loop_id": identity["loop_id"],
                    "postmortem_id": record["postmortem_id"],
                    "statement": statement,
                }
            )
            if record["question_id"] != expected_question_id:
                raise AgentLoopError("research handoff question identity mismatch")
            question = ResearchQuestion(
                question_id=record["question_id"],
                statement=statement,
                source_sha256=identity["source_sha256"],
                created_at=requested_at,
            )
            if record["question_sha256"] != _digest(question.to_payload()):
                raise AgentLoopError("research handoff question hash mismatch")
            if (
                record["postmortem_id"] in handoffs_by_postmortem
                or record["run_id"] in handoff_runs
            ):
                raise AgentLoopError("AgentLoop contains duplicate research handoff")
            handoffs_by_postmortem[record["postmortem_id"]] = record
            handoff_runs.add(record["run_id"])

        current = state["current"]
        ordered = (
            "observation_id",
            "action_id",
            "transition_id",
            "outcome_id",
            "reward_id",
            "attribution_id",
            "postmortem_id",
        )
        seen_gap = False
        for key in ordered:
            if current[key] is None:
                seen_gap = True
            elif seen_gap:
                raise AgentLoopError("AgentLoop current causal chain contains a gap")
        research_values = (
            current["research_question_id"],
            current["research_trigger_id"],
            current["research_run_id"],
        )
        if any(value is None for value in research_values) and any(
            value is not None for value in research_values
        ):
            raise AgentLoopError("AgentLoop current research identity is partial")

        decision = None
        if current["action_id"] is not None:
            decision = decisions_by_action.get(current["action_id"])
            if decision is None or decision["observation_id"] != current["observation_id"]:
                raise AgentLoopError("current action does not bind its decision")
        resolution = None
        if current["transition_id"] is not None:
            resolution = resolutions_by_transition.get(current["transition_id"])
            if resolution is None:
                raise AgentLoopError("current transition does not bind a resolution")
            for key in ("action_id", "outcome_id", "reward_id"):
                if resolution[key] != current[key]:
                    raise AgentLoopError(f"current {key} does not bind resolution")
        attribution = None
        if current["attribution_id"] is not None:
            attribution = attributions_by_id.get(current["attribution_id"])
            if (
                attribution is None
                or attribution["transition_id"] != current["transition_id"]
            ):
                raise AgentLoopError("current attribution does not bind resolution")
        postmortem = None
        if current["postmortem_id"] is not None:
            postmortem = postmortems_by_id.get(current["postmortem_id"])
            if (
                postmortem is None
                or postmortem["attribution_id"] != current["attribution_id"]
            ):
                raise AgentLoopError("current postmortem does not bind attribution")
        if research_values[0] is not None:
            if postmortem is None:
                raise AgentLoopError("current research identity lacks a postmortem")
            handoff = handoffs_by_postmortem.get(current["postmortem_id"])
            if handoff is None or (
                handoff["question_id"], handoff["trigger_id"], handoff["run_id"]
            ) != research_values:
                raise AgentLoopError("current research identity does not bind handoff")

        external_state = ExternalEffectState(state["external_effect_state"])
        if decision is None:
            if external_state is not ExternalEffectState.NONE:
                raise AgentLoopError("AgentLoop without an action has an external effect")
        elif decision["external_effect_state"] != external_state.value:
            raise AgentLoopError("current external effect differs from decision")

        phase = AgentLoopPhase(state["phase"])
        resume_phase = (
            None
            if state["resume_phase"] is None
            else AgentLoopPhase(state["resume_phase"])
        )
        if phase in {AgentLoopPhase.PAUSED, AgentLoopPhase.RECOVERY}:
            if resume_phase is None:
                raise AgentLoopError(f"{phase.value} state lacks resume phase")
            effective_phase = resume_phase
        else:
            if resume_phase is not None:
                raise AgentLoopError("non-paused AgentLoop retains resume phase")
            effective_phase = phase

        if effective_phase is AgentLoopPhase.BOOTSTRAP:
            if any(current.values()):
                raise AgentLoopError("BOOTSTRAP AgentLoop contains current evidence")
        elif effective_phase in {
            AgentLoopPhase.OBSERVE,
            AgentLoopPhase.ASSESS,
            AgentLoopPhase.PLAN,
            AgentLoopPhase.DECIDE,
            AgentLoopPhase.ACT_OR_ABSTAIN,
        }:
            if current["observation_id"] is None or current["action_id"] is not None:
                raise AgentLoopError(f"{effective_phase.value} current evidence mismatch")
        elif effective_phase in {
            AgentLoopPhase.WAIT_OUTCOME,
            AgentLoopPhase.UNKNOWN_EXTERNAL_EFFECT,
        }:
            if current["action_id"] is None or current["transition_id"] is not None:
                raise AgentLoopError(f"{effective_phase.value} current evidence mismatch")
            if (
                effective_phase is AgentLoopPhase.UNKNOWN_EXTERNAL_EFFECT
                and external_state is not ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT
            ):
                raise AgentLoopError("UNKNOWN_EXTERNAL_EFFECT phase lacks unknown effect")
        elif effective_phase in {AgentLoopPhase.EVALUATE, AgentLoopPhase.ATTRIBUTE}:
            if current["transition_id"] is None or current["attribution_id"] is not None:
                raise AgentLoopError(f"{effective_phase.value} current evidence mismatch")
        elif effective_phase is AgentLoopPhase.REFLECT:
            if current["attribution_id"] is None or current["postmortem_id"] is not None:
                raise AgentLoopError("REFLECT current evidence mismatch")
        elif effective_phase is AgentLoopPhase.RESEARCH_HANDOFF:
            if current["postmortem_id"] is None or current["research_run_id"] is not None:
                raise AgentLoopError("RESEARCH_HANDOFF current evidence mismatch")
        elif effective_phase is AgentLoopPhase.CHECKPOINT:
            if current["postmortem_id"] is None:
                raise AgentLoopError("CHECKPOINT current evidence lacks reflection")

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
            checkpointed_transition_id=state["checkpointed_transition_id"],
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
            activation_binding_id=identity.get("activation_binding_id"),
        )

    def invoke_skill(
        self,
        registry: SkillRegistry,
        *,
        skill_id: str,
        version: str,
        capability: str,
        definition_id: str,
        call_id: str,
        input_payload: dict[str, Any],
        requested_mutations: tuple[str, ...] = (),
        provenance: tuple[tuple[str, str], ...],
        requested_compute_units: int = 1,
        requested_data_units: int = 0,
        requested_ai_units: int = 0,
        at: str,
    ) -> SkillRun:
        """Run one exact governed procedure bound to this immutable loop snapshot.

        SkillRegistry remains read/analysis procedure authority only. Any candidate
        research question or other evidence returned here must still cross the
        existing canonical ResearchSupervisor/scientific handoff before it can
        affect research or promotion state.
        """
        if not isinstance(registry, SkillRegistry):
            raise TypeError("registry must be SkillRegistry")
        snapshot = self.snapshot()
        return registry.invoke(
            skill_id=skill_id,
            version=version,
            capability=capability,
            definition_id=definition_id,
            call_id=call_id,
            caller_loop_id=snapshot.loop_id,
            caller_state_sha256=snapshot.state_sha256,
            source_sha256=snapshot.source_sha256,
            input_payload=input_payload,
            authority_profile_id=AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE,
            requested_mutations=requested_mutations,
            provenance=provenance,
            requested_compute_units=requested_compute_units,
            requested_data_units=requested_data_units,
            requested_ai_units=requested_ai_units,
            at=at,
        )

    def _mutate(self, at: str, mutate) -> AgentLoopSnapshot:
        now = _timestamp_identity(at, "at")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read_local()
            self._ensure_monotonic_state(state, adopt_if_missing=True)
            observed_state_sha256 = state["state_sha256"]
            if _instant(now, "at") < _instant(
                state["updated_at"], "updated_at"
            ):
                raise AgentLoopError("AgentLoop time cannot move backwards")
            mutate(state, now)
            state["sequence"] += 1
            state["updated_at"] = now
            self._publish_monotonic_state(
                observed_state_sha256=observed_state_sha256,
                state_without_digest=self._without_digest(state),
            )
        return self.snapshot()

    def begin_observation(
        self,
        observation: Observation,
        *,
        environment_identity: EnvironmentIdentity,
        at: str,
    ) -> AgentLoopSnapshot:
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if not isinstance(environment_identity, EnvironmentIdentity):
            raise TypeError("environment_identity must be EnvironmentIdentity")

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
                phase is AgentLoopPhase.CHECKPOINT
                and state["current"]["transition_id"]
                != state["checkpointed_transition_id"]
            ):
                raise StaleAgentLoopStateError(
                    "new observation requires durable checkpoint for current transition"
                )
            if (
                environment_identity.environment_id
                != state["identity"]["environment_id"]
            ):
                raise AgentLoopError(
                    "environment identity does not match AgentLoop"
                )
            if (
                observation.environment_id
                != environment_identity.environment_id
            ):
                raise AgentLoopError(
                    "observation belongs to another environment"
                )
            cutoff = _instant(
                environment_identity.cutoff_ts,
                "environment_identity.cutoff_ts",
            )
            if (
                _instant(observation.observed_at, "observation.observed_at")
                > cutoff
                or _instant(
                    observation.available_at, "observation.available_at"
                )
                > cutoff
            ):
                raise AgentLoopError(
                    "observation exceeds canonical environment evidence cutoff"
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
        observation: Observation,
        state: dict[str, Any],
    ) -> None:
        if not isinstance(episode, Episode):
            raise TypeError("episode must be Episode")
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        identity = state["identity"]
        current = state["current"]
        if episode.environment_id != identity["environment_id"]:
            raise AgentLoopError("episode belongs to another environment")
        if episode.episode_id != identity["episode_id"]:
            raise AgentLoopError("episode identity does not match AgentLoop")
        if episode.policy_id != identity["policy_id"]:
            raise AgentLoopError("episode policy identity does not match AgentLoop")
        if observation.environment_id != identity["environment_id"]:
            raise AgentLoopError("observation belongs to another environment")
        if (
            observation.observation_id != action.observation_id
            or observation.observation_id != current["observation_id"]
        ):
            raise AgentLoopError(
                "action does not bind the durable current observation evidence"
            )
        if action.action_type not in episode.admissible_actions:
            raise AgentLoopError(
                "action is outside the canonical episode admissible set"
            )
        if _instant(
            action.decided_at, "action.decided_at"
        ) < _instant(
            observation.available_at, "observation.available_at"
        ):
            raise AgentLoopError(
                "action decision predates observation availability"
            )

    def commit_action(
        self,
        action: Action,
        *,
        episode: Episode,
        observation: Observation,
        effect_state: ExternalEffectState,
        at: str,
    ) -> ActionCommitReceipt:
        if not isinstance(action, Action):
            raise TypeError("action must be Action")
        if not isinstance(effect_state, ExternalEffectState):
            raise TypeError("effect_state must be ExternalEffectState")
        existing_state = self._read()
        self._require_action_authority(
            action, episode, observation, existing_state
        )
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
            self._require_action_authority(
                action, episode, observation, state
            )
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
            canonical_parameters = [
                [key, value] for key, value in action.parameters
            ]
            record: dict[str, object] = {
                "observation_id": action.observation_id,
                "action_id": action.action_id,
                "action_type": action.action_type,
                "decided_at": _timestamp_identity(
                    action.decided_at, "action.decided_at"
                ),
                "parameters_sha256": _digest(canonical_parameters),
                "external_effect_state": effect_state.value,
                "may_execute": may_execute,
            }
            if state["schema_version"] == AGENT_LOOP_SCHEMA_VERSION:
                record["parameters"] = canonical_parameters
                record["decision_intent_id"] = _digest(
                    {
                        "environment_id": state["identity"]["environment_id"],
                        "episode_id": state["identity"]["episode_id"],
                        "observation_id": action.observation_id,
                    }
                )
                record["decision_payload_id"] = _digest(
                    {
                        "action_type": action.action_type,
                        "parameters": canonical_parameters,
                    }
                )
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
            if (
                AgentLoopPhase(state["phase"])
                is not AgentLoopPhase.WAIT_OUTCOME
            ):
                raise StaleAgentLoopStateError(
                    "unknown external effect marking requires WAIT_OUTCOME"
                )
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
                outcome.environment_id != identity["environment_id"]
                or reward.environment_id != identity["environment_id"]
            ):
                raise AgentLoopError(
                    "resolution evidence belongs to another AgentLoop environment"
                )
            decision = next(
                (
                    item
                    for item in state["decisions"]
                    if item["action_id"] == current["action_id"]
                ),
                None,
            )
            if decision is None:
                raise AgentLoopError(
                    "current action lacks durable decision evidence"
                )
            if (
                transition.observation_id != current["observation_id"]
                or transition.observation_id != decision["observation_id"]
            ):
                raise AgentLoopError(
                    "transition does not bind the durable current observation"
                )
            if _timestamp_identity(
                transition.decision_at, "transition.decision_at"
            ) != _timestamp_identity(
                decision["decided_at"], "decision.decided_at"
            ):
                raise AgentLoopError(
                    "transition decision_at does not bind durable action decision"
                )
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
            decision_i = _instant(
                decision["decided_at"], "decision.decided_at"
            )
            reveal_i = _instant(
                outcome.revealed_at, "outcome.revealed_at"
            )
            reward_i = _instant(
                reward.available_at, "reward.available_at"
            )
            resolved_i = _instant(
                transition.resolved_at, "transition.resolved_at"
            )
            now_i = _instant(now, "at")
            if reveal_i < decision_i or reward_i < decision_i:
                raise AgentLoopError(
                    "outcome/reward evidence predates action decision"
                )
            if reward_i < reveal_i:
                raise AgentLoopError(
                    "reward evidence predates outcome reveal"
                )
            if resolved_i < reveal_i or resolved_i < reward_i:
                raise AgentLoopError(
                    "transition resolves before outcome/reward availability"
                )
            if (
                reveal_i > now_i
                or reward_i > now_i
                or resolved_i > now_i
            ):
                raise AgentLoopError(
                    "future resolution evidence cannot enter AgentLoop"
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
            finding_status = {
                item["component"]: item["status"]
                for item in attribution["findings"]
            }
            if any(
                item.value not in finding_status
                for item in postmortem.unresolved_components
            ):
                raise AgentLoopError(
                    "postmortem references attribution component not present"
                )
            unresolved_statuses = {
                AttributionStatus.UNKNOWN.value,
                AttributionStatus.MIXED.value,
            }
            if any(
                finding_status[item.value] not in unresolved_statuses
                for item in postmortem.unresolved_components
            ):
                raise AgentLoopError(
                    "postmortem unresolved components must be UNKNOWN or MIXED"
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

    @classmethod
    def _upgrade_legacy_v2_checkpoint(
        cls,
        state: dict[str, Any],
        checkpoint: EnvironmentCheckpoint,
        *,
        committed_at: str,
    ) -> None:
        """Promote v2 only from a complete canonical environment witness."""

        if state["schema_version"] != AGENT_LOOP_LEGACY_SCHEMA_VERSION:
            raise AgentLoopError("legacy checkpoint upgrade requires schema v2")
        if checkpoint.step_index != len(state["resolutions"]):
            raise AgentLoopError(
                "legacy checkpoint must cover the complete durable resolution history"
            )
        committed_actions = set(checkpoint.committed_action_ids)
        committed_intents = dict(checkpoint.committed_decision_intents)
        if len(committed_actions) != len(state["decisions"]):
            raise AgentLoopError(
                "legacy checkpoint action set does not cover durable decisions"
            )
        if len(committed_intents) != len(state["decisions"]):
            raise AgentLoopError(
                "legacy checkpoint decision intents do not cover durable decisions"
            )
        identity = state["identity"]
        upgraded_decisions: list[dict[str, object]] = []
        seen_intents: set[str] = set()
        seen_actions: set[str] = set()
        for legacy in state["decisions"]:
            intent_id = _digest(
                {
                    "environment_id": identity["environment_id"],
                    "episode_id": identity["episode_id"],
                    "observation_id": legacy["observation_id"],
                }
            )
            payload_id = committed_intents.get(intent_id)
            if payload_id is None:
                raise AgentLoopError(
                    "legacy decision lacks canonical checkpoint payload witness"
                )
            if legacy["action_id"] not in committed_actions:
                raise AgentLoopError(
                    "legacy decision lacks canonical checkpoint action witness"
                )
            if intent_id in seen_intents or legacy["action_id"] in seen_actions:
                raise AgentLoopError(
                    "legacy checkpoint upgrade contains duplicate decision witness"
                )
            seen_intents.add(intent_id)
            seen_actions.add(legacy["action_id"])
            upgraded = dict(legacy)
            upgraded["parameters"] = None
            upgraded["decision_intent_id"] = intent_id
            upgraded["decision_payload_id"] = payload_id
            upgraded_decisions.append(upgraded)
        if seen_intents != set(committed_intents):
            raise AgentLoopError(
                "legacy checkpoint contains an unbound decision intent"
            )
        if seen_actions != committed_actions:
            raise AgentLoopError(
                "legacy checkpoint contains an unbound committed action"
            )
        state["decisions"] = upgraded_decisions
        state["schema_version"] = AGENT_LOOP_SCHEMA_VERSION
        state["checkpoint_history"] = [
            cls._checkpoint_record(
                checkpoint,
                committed_at=committed_at,
            )
        ]
        state["environment_checkpoint_id"] = checkpoint.checkpoint_id
        state["checkpointed_transition_id"] = checkpoint.last_transition_id

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

        def apply(state: dict[str, Any], now: str) -> None:
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
            if (
                state["schema_version"]
                == AGENT_LOOP_LEGACY_SCHEMA_VERSION
            ):
                self._upgrade_legacy_v2_checkpoint(
                    state,
                    environment_checkpoint,
                    committed_at=now,
                )
                # Validate the fully promoted candidate before _mutate can
                # publish any schema-v3 bytes.
                self._validate_history(state)
                return
            checkpoint_record = self._checkpoint_record(
                environment_checkpoint,
                committed_at=now,
            )
            latest_checkpoint = state["checkpoint_history"][-1]
            if latest_checkpoint["checkpoint_id"] != environment_checkpoint.checkpoint_id:
                state["checkpoint_history"].append(checkpoint_record)
            state["environment_checkpoint_id"] = (
                environment_checkpoint.checkpoint_id
            )
            state["checkpointed_transition_id"] = (
                environment_checkpoint.last_transition_id
            )
            # Validate the candidate state before _mutate can persist it.
            self._validate_history(state)

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
