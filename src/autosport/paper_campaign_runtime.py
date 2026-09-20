"""PAPER-only terminal composition for the Autosport learning loop.

This module deliberately owns neither a market feed, a ticket, settlement math,
the risk policy, a reward function, a research registry, nor promotion.  It is the
narrow last-mile composition which turns the already durable authoritative outcome
published by :class:`PaperSettlementLearningBridge` into conservative causal
attribution, reflection, an optional bounded research request and an exact next
environment checkpoint.

The runtime is serial by design: one ``AgentLoopRuntime`` has one current action.
Concurrent PAPER opportunities require distinct independently durable episodes;
they must not be multiplexed through this class.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .agent_loop import (
    AgentLoopError,
    AgentLoopPhase,
    AttributionComponent,
    AttributionFinding,
    AttributionStatus,
    ExternalEffectState,
    OutcomeAttribution,
    ReflectionPostmortem,
)
from .continuous_session import SettlementResolution
from .integrity import atomic_write_json
from .learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    LearningEnvironmentError,
    Observation,
)
from .paper_settlement_learning import (
    PaperSettlementLearningBridge,
    PaperSettlementLearningBridgeError,
    PaperSettlementLearningWitness,
)
from .research_supervisor import ResearchSupervisor
from .workspace_lock import WorkspaceEconomicLock


CAMPAIGN_SCHEMA = "autosport.paper_campaign_runtime"
CAMPAIGN_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class PaperCampaignRuntimeError(RuntimeError):
    """The campaign composition conflicts with durable canonical evidence."""


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
        raise PaperCampaignRuntimeError(
            "campaign evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PaperCampaignRuntimeError(f"campaign JSON duplicate key: {key}")
        result[key] = value
    return result


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise PaperCampaignRuntimeError(f"{name} must be canonical SHA-256 hex")
    return text


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PaperCampaignRuntimeError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PaperCampaignRuntimeError(f"{name} must be valid UTF-8") from exc
    return value


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperCampaignRuntimeError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperCampaignRuntimeError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PaperReflectionPlan:
    """Frozen, conservative terminal reflection policy for one PAPER episode.

    It intentionally does not attempt to infer why a wager won or lost.  Every
    declared component is marked ``UNKNOWN`` until another canonical evidence
    producer can support a stronger conclusion.  This prevents a lucky result
    from becoming fictional forecast, sizing or psychological evidence.
    """

    summary_code: str = "PAPER_SETTLEMENT_REQUIRES_CAUSAL_REVIEW"
    reason_code: str = "PAPER_SETTLEMENT_ONLY_NO_CAUSAL_DECOMPOSITION"
    unresolved_components: tuple[AttributionComponent, ...] = (
        AttributionComponent.RANDOMNESS,
    )
    research_question_statement: str | None = None
    research_budget_units: int | None = None
    research_deadline_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.summary_code, "summary_code")
        _text(self.reason_code, "reason_code")
        if type(self.unresolved_components) is not tuple or not self.unresolved_components:
            raise PaperCampaignRuntimeError(
                "unresolved_components must be a non-empty tuple"
            )
        if any(
            not isinstance(component, AttributionComponent)
            for component in self.unresolved_components
        ):
            raise PaperCampaignRuntimeError(
                "unresolved_components must contain AttributionComponent values"
            )
        names = [component.value for component in self.unresolved_components]
        if names != sorted(names) or len(names) != len(set(names)):
            raise PaperCampaignRuntimeError(
                "unresolved_components must be sorted and unique"
            )
        if self.research_question_statement is None:
            if (
                self.research_budget_units is not None
                or self.research_deadline_at is not None
            ):
                raise PaperCampaignRuntimeError(
                    "research budget/deadline requires a research question"
                )
            return
        _text(self.research_question_statement, "research_question_statement")
        if (
            isinstance(self.research_budget_units, bool)
            or not isinstance(self.research_budget_units, int)
            or self.research_budget_units <= 0
        ):
            raise PaperCampaignRuntimeError(
                "research_budget_units must be a positive integer with a question"
            )
        if self.research_deadline_at is not None:
            _instant(self.research_deadline_at, "research_deadline_at")


@dataclass(frozen=True, slots=True)
class PaperCampaignFinalizationReceipt:
    """Typed acknowledgement of one fully terminalized PAPER transition."""

    ticket_id: str
    transition_id: str
    attribution_id: str
    postmortem_id: str
    checkpoint_id: str
    research_run_id: str | None


class PaperCampaignRuntime:
    """Compose one serial PAPER episode using existing canonical authorities."""

    def __init__(
        self,
        *,
        environment: CausalLearningEnvironment,
        settlement_bridge: PaperSettlementLearningBridge,
        reflection_plan: PaperReflectionPlan | None = None,
        research_supervisor: ResearchSupervisor | None = None,
    ) -> None:
        if not isinstance(environment, CausalLearningEnvironment):
            raise TypeError("environment must be CausalLearningEnvironment")
        if not isinstance(settlement_bridge, PaperSettlementLearningBridge):
            raise TypeError("settlement_bridge must be PaperSettlementLearningBridge")
        if reflection_plan is None:
            reflection_plan = PaperReflectionPlan()
        if not isinstance(reflection_plan, PaperReflectionPlan):
            raise TypeError("reflection_plan must be PaperReflectionPlan")
        if research_supervisor is not None and not isinstance(
            research_supervisor, ResearchSupervisor
        ):
            raise TypeError("research_supervisor must be ResearchSupervisor")
        if (
            reflection_plan.research_question_statement is not None
            and research_supervisor is None
        ):
            raise PaperCampaignRuntimeError(
                "a research question requires the canonical ResearchSupervisor"
            )

        snapshot = settlement_bridge.agent_loop.snapshot()
        episode = environment.episode
        if (
            snapshot.environment_id != environment.environment_id
            or snapshot.episode_id != episode.episode_id
            or snapshot.policy_id != episode.policy_id
        ):
            raise PaperCampaignRuntimeError(
                "environment does not bind the settlement bridge AgentLoop"
            )
        try:
            active_checkpoint = environment.checkpoint()
        except LearningEnvironmentError as exc:
            raise PaperCampaignRuntimeError(
                "campaign runtime must be constructed at an exact resolved environment checkpoint"
            ) from exc
        if snapshot.environment_checkpoint_id != active_checkpoint.checkpoint_id:
            raise PaperCampaignRuntimeError(
                "active environment checkpoint differs from AgentLoop durable checkpoint"
            )
        self.environment = environment
        self.settlement_bridge = settlement_bridge
        self.reflection_plan = reflection_plan
        self.research_supervisor = research_supervisor
        self._environment_checkpoint_id = active_checkpoint.checkpoint_id
        self.state_path = settlement_bridge.state_path.with_name(
            f"{settlement_bridge.state_path.name}.campaign.json"
        )
        self._ensure_campaign_state()

    def _write_campaign_state(self, plans: dict[str, object]) -> None:
        bare = {
            "schema": CAMPAIGN_SCHEMA,
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "plans": plans,
        }
        atomic_write_json(
            self.state_path,
            {**bare, "state_sha256": _digest(bare)},
        )

    def _read_campaign_state(self) -> dict[str, object]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    PaperCampaignRuntimeError(
                        f"campaign JSON contains non-finite value {value}"
                    )
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperCampaignRuntimeError("campaign state is unreadable") from exc
        if type(state) is not dict or set(state) != {
            "schema",
            "schema_version",
            "plans",
            "state_sha256",
        }:
            raise PaperCampaignRuntimeError("campaign state schema mismatch")
        if (
            state["schema"] != CAMPAIGN_SCHEMA
            or state["schema_version"] != CAMPAIGN_SCHEMA_VERSION
            or type(state["plans"]) is not dict
        ):
            raise PaperCampaignRuntimeError("unsupported campaign state")
        bare = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "plans": state["plans"],
        }
        if _sha(state["state_sha256"], "state_sha256") != _digest(bare):
            raise PaperCampaignRuntimeError("campaign state digest mismatch")
        expected_fields = {
            "plan_id",
            "ticket_id",
            "binding_id",
            "settlement_bundle_sha256",
            "transition_id",
            "attribution_id",
            "postmortem_id",
            "observation_id",
            "action_id",
            "baseline_checkpoint_id",
            "next_checkpoint_id",
            "summary_code",
            "reason_code",
            "unresolved_components",
            "research_question_statement",
            "research_budget_units",
            "research_deadline_at",
        }
        for ticket_id, record in state["plans"].items():
            _text(ticket_id, "campaign ticket_id")
            if type(record) is not dict or set(record) != expected_fields:
                raise PaperCampaignRuntimeError("campaign plan fields mismatch")
            if record["ticket_id"] != ticket_id:
                raise PaperCampaignRuntimeError("campaign plan ticket identity mismatch")
            for field in (
                "binding_id",
                "settlement_bundle_sha256",
                "transition_id",
                "attribution_id",
                "postmortem_id",
                "observation_id",
                "action_id",
                "baseline_checkpoint_id",
                "next_checkpoint_id",
            ):
                _sha(record[field], field)
            _text(record["summary_code"], "summary_code")
            _text(record["reason_code"], "reason_code")
            if type(record["unresolved_components"]) is not list:
                raise PaperCampaignRuntimeError(
                    "campaign unresolved_components must be a list"
                )
            if record["research_question_statement"] is not None:
                _text(
                    record["research_question_statement"],
                    "research_question_statement",
                )
            budget = record["research_budget_units"]
            if budget is not None and (
                isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0
            ):
                raise PaperCampaignRuntimeError(
                    "campaign research_budget_units must be positive"
                )
            deadline = record["research_deadline_at"]
            if deadline is not None:
                _timestamp(deadline, "research_deadline_at")
            semantic = {key: value for key, value in record.items() if key != "plan_id"}
            if _sha(record["plan_id"], "plan_id") != _digest(semantic):
                raise PaperCampaignRuntimeError("campaign plan digest mismatch")
        return state

    def _ensure_campaign_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(self.state_path.parent):
            if self.state_path.exists():
                self._read_campaign_state()
            else:
                self._write_campaign_state({})

    def _bind_finalization_plan(
        self,
        witness: PaperSettlementLearningWitness,
        attribution: OutcomeAttribution,
        postmortem: ReflectionPostmortem,
    ) -> None:
        deadline = self.reflection_plan.research_deadline_at
        canonical_deadline = (
            None if deadline is None else _timestamp(deadline, "research_deadline_at")
        )
        semantic = {
            "ticket_id": witness.ticket_id,
            "binding_id": witness.binding_id,
            "settlement_bundle_sha256": witness.settlement_bundle_sha256,
            "transition_id": witness.transition.transition_id,
            "attribution_id": attribution.attribution_id,
            "postmortem_id": postmortem.postmortem_id,
            "observation_id": witness.observation.observation_id,
            "action_id": witness.action.action_id,
            "baseline_checkpoint_id": witness.baseline_checkpoint.checkpoint_id,
            "next_checkpoint_id": witness.next_checkpoint.checkpoint_id,
            "summary_code": self.reflection_plan.summary_code,
            "reason_code": self.reflection_plan.reason_code,
            "unresolved_components": [
                component.value
                for component in self.reflection_plan.unresolved_components
            ],
            "research_question_statement": self.reflection_plan.research_question_statement,
            "research_budget_units": self.reflection_plan.research_budget_units,
            "research_deadline_at": canonical_deadline,
        }
        record = {"plan_id": _digest(semantic), **semantic}
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read_campaign_state()
            existing = state["plans"].get(witness.ticket_id)
            if existing is not None and existing != record:
                raise PaperCampaignRuntimeError(
                    "durable campaign finalization plan conflicts with retry"
                )
            if existing is None:
                state["plans"][witness.ticket_id] = record
                self._write_campaign_state(state["plans"])

    @property
    def agent_loop(self):
        """The existing bridge-bound durable AgentLoop authority."""

        return self.settlement_bridge.agent_loop

    def begin_and_bind_paper_ticket(
        self,
        *,
        ticket_id: str,
        decision_id: str,
        observation: Observation,
        action_type: str,
        decision_at: str,
        parameters: tuple[tuple[str, str], ...],
        at: str,
        baseline_checkpoint: EnvironmentCheckpoint | None = None,
    ) -> Action:
        """Commit one PAPER_ONLY action and bind its existing ticket by retry.

        Ticket placement and economic decision recording remain owned by the
        existing paper strategy/risk path.  This method only makes their already
        durable identities causally bindable to the AgentLoop.  It does not claim
        atomic admission across ticket placement, ledger publication and bridge
        binding; that still needs a dedicated admission journal.  If a process
        dies between action commit and bridge binding, repeating the exact call
        with a rehydrated environment/checkpoint is idempotent; changing any
        identity fails closed.
        """

        _text(ticket_id, "ticket_id")
        _text(decision_id, "decision_id")
        _text(action_type, "action_type")
        _timestamp(decision_at, "decision_at")
        now = _timestamp(at, "at")
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if type(parameters) is not tuple:
            raise TypeError("parameters must be a canonical tuple")
        if baseline_checkpoint is not None and not isinstance(
            baseline_checkpoint, EnvironmentCheckpoint
        ):
            raise TypeError("baseline_checkpoint must be EnvironmentCheckpoint")

        try:
            self.settlement_bridge.verify_decision_observation_binding(
                decision_id=decision_id,
                observation=observation,
            )
        except PaperSettlementLearningBridgeError as exc:
            raise PaperCampaignRuntimeError(
                "economic decision does not bind the exact causal Observation"
            ) from exc

        snapshot = self.agent_loop.snapshot()
        if snapshot.phase in {AgentLoopPhase.BOOTSTRAP, AgentLoopPhase.CHECKPOINT}:
            calculated_baseline = self.environment.checkpoint()
            if baseline_checkpoint is not None and (
                baseline_checkpoint.checkpoint_id != calculated_baseline.checkpoint_id
            ):
                raise PaperCampaignRuntimeError(
                    "supplied baseline checkpoint differs from active environment"
                )
            baseline = calculated_baseline
            if snapshot.environment_checkpoint_id != baseline.checkpoint_id:
                raise PaperCampaignRuntimeError(
                    "AgentLoop checkpoint differs from active environment"
                )
            self.agent_loop.begin_observation(
                observation,
                environment_identity=self.environment.identity,
                at=now,
            )
            for phase in (
                AgentLoopPhase.OBSERVE,
                AgentLoopPhase.ASSESS,
                AgentLoopPhase.PLAN,
                AgentLoopPhase.DECIDE,
            ):
                self.agent_loop.advance(expected=phase, at=now)
        elif snapshot.phase is AgentLoopPhase.WAIT_OUTCOME:
            if baseline_checkpoint is None:
                raise PaperCampaignRuntimeError(
                    "WAIT_OUTCOME retry requires the exact baseline checkpoint"
                )
            baseline = baseline_checkpoint
            if snapshot.environment_checkpoint_id != baseline.checkpoint_id:
                raise PaperCampaignRuntimeError(
                    "retry baseline differs from AgentLoop checkpoint"
                )
        else:
            raise PaperCampaignRuntimeError(
                "PAPER action requires BOOTSTRAP, CHECKPOINT, or retryable WAIT_OUTCOME"
            )

        action = self.environment.act(
            observation,
            action_type=action_type,
            decision_at=decision_at,
            parameters=parameters,
        )
        try:
            receipt = self.agent_loop.commit_action(
                action,
                episode=self.environment.episode,
                observation=observation,
                effect_state=ExternalEffectState.PAPER_ONLY,
                at=now,
            )
        except AgentLoopError as exc:
            raise PaperCampaignRuntimeError(
                "AgentLoop rejected PAPER ticket action"
            ) from exc
        if receipt.action_id != action.action_id:
            raise PaperCampaignRuntimeError(
                "AgentLoop action receipt does not bind constructed action"
            )
        self.settlement_bridge.bind_ticket(
            ticket_id=ticket_id,
            decision_id=decision_id,
            environment=self.environment,
            observation=observation,
            action=action,
            baseline_checkpoint=baseline,
        )
        return action

    def finalize_ticket(
        self,
        *,
        ticket_id: str,
        at: str,
    ) -> PaperCampaignFinalizationReceipt:
        """Converge one resolved ticket through attribution, reflection and checkpoint.

        The durable AgentLoop phase is the recovery outbox.  A crash after any
        mutation resumes from the next phase and uses deterministic witness times,
        so it cannot manufacture a second attribution, postmortem or research run.
        """

        _text(ticket_id, "ticket_id")
        now = _timestamp(at, "at")
        witness = self.settlement_bridge.resolution_witness(ticket_id)
        if _instant(now, "at") < _instant(
            witness.reward.available_at, "reward.available_at"
        ):
            raise PaperCampaignRuntimeError(
                "campaign finalization predates authoritative reward availability"
            )
        self._verify_witness_against_loop(witness)

        snapshot = self.agent_loop.snapshot()
        if snapshot.phase is AgentLoopPhase.EVALUATE:
            self.agent_loop.advance(expected=AgentLoopPhase.EVALUATE, at=now)
            snapshot = self.agent_loop.snapshot()
        if snapshot.phase not in {
            AgentLoopPhase.ATTRIBUTE,
            AgentLoopPhase.REFLECT,
            AgentLoopPhase.RESEARCH_HANDOFF,
            AgentLoopPhase.CHECKPOINT,
        }:
            raise PaperCampaignRuntimeError(
                "bridge resolution is not yet acknowledged by AgentLoop"
            )

        attribution = self._attribution(witness)
        # Always route through the canonical immutable-evidence writer, even
        # after a restart.  ``attribution_id`` intentionally identifies the
        # transition/reward boundary, not a particular interpretation of it;
        # comparing just that id would let a later reflection policy silently
        # replace its findings.  The AgentLoop compares the full durable
        # payload and fails closed on that conflict.
        try:
            self.agent_loop.record_attribution(attribution, at=now)
        except AgentLoopError as exc:
            raise PaperCampaignRuntimeError(
                "AgentLoop is bound to conflicting attribution evidence"
            ) from exc
        snapshot = self.agent_loop.snapshot()
        if snapshot.phase not in {
            AgentLoopPhase.REFLECT,
            AgentLoopPhase.RESEARCH_HANDOFF,
            AgentLoopPhase.CHECKPOINT,
        }:
            raise PaperCampaignRuntimeError(
                "AgentLoop did not advance to a postmortem-capable phase"
            )

        postmortem = self._postmortem(attribution, witness)
        # Freeze all optional research bounds and the exact bridge/checkpoint
        # identities before the durable postmortem can cross the crash boundary.
        self._bind_finalization_plan(witness, attribution, postmortem)
        # The same idempotent full-payload rule applies to a postmortem.  It
        # prevents a changed question/summary from being accepted merely
        # because it refers to the same attribution.
        try:
            self.agent_loop.record_postmortem(postmortem, at=now)
        except AgentLoopError as exc:
            raise PaperCampaignRuntimeError(
                "AgentLoop is bound to conflicting postmortem evidence"
            ) from exc
        snapshot = self.agent_loop.snapshot()

        if snapshot.phase is AgentLoopPhase.RESEARCH_HANDOFF:
            if self.research_supervisor is None:
                raise PaperCampaignRuntimeError(
                    "AgentLoop requires research handoff but no supervisor was supplied"
                )
            assert self.reflection_plan.research_budget_units is not None
            self.agent_loop.handoff_research(
                self.research_supervisor,
                budget_units=self.reflection_plan.research_budget_units,
                deadline_at=self.reflection_plan.research_deadline_at,
                at=now,
            )
            snapshot = self.agent_loop.snapshot()

        if snapshot.phase is not AgentLoopPhase.CHECKPOINT:
            raise PaperCampaignRuntimeError(
                "resolved PAPER ticket did not reach AgentLoop CHECKPOINT"
            )
        if snapshot.environment_checkpoint_id != witness.next_checkpoint.checkpoint_id:
            self.agent_loop.commit_checkpoint(witness.next_checkpoint, at=now)
            snapshot = self.agent_loop.snapshot()
        if (
            snapshot.environment_checkpoint_id != witness.next_checkpoint.checkpoint_id
            or snapshot.checkpointed_transition_id != witness.transition.transition_id
        ):
            raise PaperCampaignRuntimeError(
                "AgentLoop checkpoint acknowledgement differs from bridge witness"
            )
        self.environment = CausalLearningEnvironment.resume(
            self.environment.identity,
            episode_key=self.environment.episode.episode_key,
            policy_id=self.environment.episode.policy_id,
            admissible_actions=frozenset(self.environment.episode.admissible_actions),
            checkpoint=witness.next_checkpoint,
        )
        self._environment_checkpoint_id = witness.next_checkpoint.checkpoint_id
        return PaperCampaignFinalizationReceipt(
            ticket_id=ticket_id,
            transition_id=witness.transition.transition_id,
            attribution_id=attribution.attribution_id,
            postmortem_id=postmortem.postmortem_id,
            checkpoint_id=witness.next_checkpoint.checkpoint_id,
            research_run_id=snapshot.research_run_id,
        )

    def _verify_witness_against_loop(
        self,
        witness: PaperSettlementLearningWitness,
    ) -> None:
        snapshot = self.agent_loop.snapshot()
        expected_environment_checkpoint_id: str
        if snapshot.environment_checkpoint_id == witness.baseline_checkpoint.checkpoint_id:
            expected_environment_checkpoint_id = witness.baseline_checkpoint.checkpoint_id
        elif snapshot.environment_checkpoint_id == witness.next_checkpoint.checkpoint_id:
            expected_environment_checkpoint_id = witness.next_checkpoint.checkpoint_id
        else:
            raise PaperCampaignRuntimeError(
                "AgentLoop checkpoint does not bind bridge baseline or resolved checkpoint"
            )
        if self._environment_checkpoint_id != expected_environment_checkpoint_id:
            raise PaperCampaignRuntimeError(
                "active environment checkpoint differs from bridge/AgentLoop boundary"
            )
        try:
            live_checkpoint = self.environment.checkpoint()
        except LearningEnvironmentError:
            # A pre-resolution in-memory environment legitimately carries the
            # one pending action and therefore cannot emit a public checkpoint.
            live_checkpoint = None
        if (
            live_checkpoint is not None
            and live_checkpoint.checkpoint_id != self._environment_checkpoint_id
        ):
            raise PaperCampaignRuntimeError(
                "active environment state drifted from its bound checkpoint"
            )
        if (
            snapshot.environment_id != witness.transition.environment_id
            or snapshot.episode_id != witness.transition.episode_id
            or snapshot.observation_id != witness.observation.observation_id
            or snapshot.action_id != witness.action.action_id
        ):
            raise PaperCampaignRuntimeError(
                "bridge witness does not bind the current AgentLoop action"
            )
        if snapshot.transition_id not in {None, witness.transition.transition_id}:
            raise PaperCampaignRuntimeError(
                "AgentLoop is bound to another resolved transition"
            )
        if snapshot.outcome_id not in {None, witness.outcome.outcome_id}:
            raise PaperCampaignRuntimeError(
                "AgentLoop is bound to another outcome"
            )
        if snapshot.reward_id not in {None, witness.reward.reward_id}:
            raise PaperCampaignRuntimeError(
                "AgentLoop is bound to another reward"
            )

    def _attribution(
        self,
        witness: PaperSettlementLearningWitness,
    ) -> OutcomeAttribution:
        available_at = _timestamp(
            witness.reward.available_at,
            "reward.available_at",
        )
        findings = tuple(
            AttributionFinding(
                component=component,
                status=AttributionStatus.UNKNOWN,
                evidence_sha256=witness.settlement_bundle_sha256,
                evidence_available_at=available_at,
                contribution=None,
                reason_code=self.reflection_plan.reason_code,
            )
            for component in self.reflection_plan.unresolved_components
        )
        return OutcomeAttribution(
            environment_id=witness.transition.environment_id,
            episode_id=witness.transition.episode_id,
            transition_id=witness.transition.transition_id,
            action_id=witness.action.action_id,
            outcome_id=witness.outcome.outcome_id,
            reward_id=witness.reward.reward_id,
            reward_value=witness.reward.reward,
            truth=witness.reward.truth,
            simulation_model_id=witness.reward.simulation_model_id,
            attributed_at=available_at,
            findings=findings,
        )

    def _postmortem(
        self,
        attribution: OutcomeAttribution,
        witness: PaperSettlementLearningWitness,
    ) -> ReflectionPostmortem:
        return ReflectionPostmortem(
            attribution_id=attribution.attribution_id,
            transition_id=witness.transition.transition_id,
            created_at=_timestamp(witness.reward.available_at, "reward.available_at"),
            unresolved_components=self.reflection_plan.unresolved_components,
            summary_code=self.reflection_plan.summary_code,
            research_question_statement=self.reflection_plan.research_question_statement,
        )


class PaperCampaignLearningHandoff:
    """Existing ``ContinuousSessionCoordinator`` handoff protocol plus terminalization."""

    def __init__(self, runtime: PaperCampaignRuntime, *, ticket_id: str) -> None:
        if not isinstance(runtime, PaperCampaignRuntime):
            raise TypeError("runtime must be PaperCampaignRuntime")
        self.runtime = runtime
        self.ticket_id = _text(ticket_id, "ticket_id")

    def prepare_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        at: str,
    ) -> tuple[str, ...]:
        return self.runtime.settlement_bridge.prepare_settlement(
            paper_book_path=paper_book_path,
            resolutions=resolutions,
            at=at,
        )

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        settled_ticket_ids: tuple[str, ...],
        at: str,
    ) -> tuple[str, ...]:
        transitions = self.runtime.settlement_bridge.reconcile_after_settlement(
            paper_book_path=paper_book_path,
            resolutions=resolutions,
            settled_ticket_ids=settled_ticket_ids,
            at=at,
        )
        try:
            self.runtime.settlement_bridge.resolution_witness(self.ticket_id)
        except PaperSettlementLearningBridgeError as exc:
            if str(exc) == "ticket has no durable learner outbox":
                return transitions
            raise
        self.runtime.finalize_ticket(ticket_id=self.ticket_id, at=at)
        return transitions


__all__ = [
    "PaperCampaignFinalizationReceipt",
    "PaperCampaignLearningHandoff",
    "PaperCampaignRuntime",
    "PaperCampaignRuntimeError",
    "PaperReflectionPlan",
]
