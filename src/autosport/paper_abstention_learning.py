"""Causal PAPER learning for explicit WAIT/NO_BET abstentions.

This is a narrow adapter over the existing :class:`CausalLearningEnvironment` and
:class:`AgentLoopRuntime` authorities.  It does not choose whether to abstain,
compute a reward, widen admissible actions, execute provider effects, or promote a
policy.  In particular, an abstention never receives an implicit positive reward:
callers must supply the exact delayed ``Outcome`` and ``RewardEvidence`` that the
canonical learning environment can validate.
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
    AgentLoopRuntime,
    AttributionComponent,
    AttributionFinding,
    AttributionStatus,
    ExternalEffectState,
    OutcomeAttribution,
    ReflectionPostmortem,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    EvidenceTruth,
    LearningEnvironmentError,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from .workspace_lock import WorkspaceEconomicLock


ABSTENTION_ACTION_TYPES = frozenset({"NO_BET", "WAIT"})
_ABSTENTION_SUMMARY = "PAPER_ABSTENTION_REQUIRES_CAUSAL_REVIEW"
_ABSTENTION_REASON = "ABSTENTION_OUTCOME_ONLY_NO_CAUSAL_DECOMPOSITION"
_INTENT_SCHEMA = "autosport.paper_abstention_intents"
_INTENT_SCHEMA_VERSION = 1


class PaperAbstentionLearningError(RuntimeError):
    """Abstention evidence conflicts with the canonical causal authorities."""


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
        raise PaperAbstentionLearningError(
            "abstention intent is outside canonical JSON domain"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _instant(value: str, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise PaperAbstentionLearningError(f"{name} must be canonical non-empty text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperAbstentionLearningError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperAbstentionLearningError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: str, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PaperAbstentionFinalizationReceipt:
    """Exact terminal witness for one learned WAIT/NO_BET transition."""

    action_id: str
    transition_id: str
    attribution_id: str
    postmortem_id: str
    checkpoint_id: str
    reward_id: str
    reward_truth: EvidenceTruth


class PaperAbstentionLearningRuntime:
    """Terminalize one explicit PAPER abstention without inventing reward truth."""

    def __init__(
        self,
        *,
        environment: CausalLearningEnvironment,
        agent_loop: AgentLoopRuntime,
    ) -> None:
        if not isinstance(environment, CausalLearningEnvironment):
            raise TypeError("environment must be CausalLearningEnvironment")
        if not isinstance(agent_loop, AgentLoopRuntime):
            raise TypeError("agent_loop must be AgentLoopRuntime")
        snapshot = agent_loop.snapshot()
        if (
            snapshot.environment_id != environment.environment_id
            or snapshot.episode_id != environment.episode.episode_id
            or snapshot.policy_id != environment.episode.policy_id
        ):
            raise PaperAbstentionLearningError(
                "environment does not bind the supplied AgentLoop"
            )
        try:
            checkpoint = environment.checkpoint()
        except LearningEnvironmentError as exc:
            raise PaperAbstentionLearningError(
                "runtime construction requires a resolved restart checkpoint"
            ) from exc
        if checkpoint.checkpoint_id != snapshot.environment_checkpoint_id:
            raise PaperAbstentionLearningError(
                "environment checkpoint differs from AgentLoop checkpoint"
            )
        self.environment = environment
        self.agent_loop = agent_loop

    @property
    def _intent_path(self) -> Path:
        return self.agent_loop.path.with_name(
            f"{self.agent_loop.path.name}.paper-abstention-intents.json"
        )

    @staticmethod
    def _intent_digest_payload(record: dict[str, object]) -> dict[str, object]:
        return {
            "schema": _INTENT_SCHEMA,
            "schema_version": _INTENT_SCHEMA_VERSION,
            "environment_id": record["environment_id"],
            "episode_id": record["episode_id"],
            "policy_id": record["policy_id"],
            "observation_id": record["observation_id"],
            "action_id": record["action_id"],
            "action_type": record["action_type"],
            "decided_at": record["decided_at"],
            "parameters": record["parameters"],
        }

    def _intent_record(
        self,
        *,
        observation: Observation,
        action: Action,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "environment_id": self.environment.environment_id,
            "episode_id": self.environment.episode.episode_id,
            "policy_id": self.environment.episode.policy_id,
            "observation_id": observation.observation_id,
            "action_id": action.action_id,
            "action_type": action.action_type,
            "decided_at": _timestamp(action.decided_at, "action.decided_at"),
            "parameters": [[key, value] for key, value in action.parameters],
        }
        record["intent_sha256"] = _digest(self._intent_digest_payload(record))
        return record

    def _read_intents(self) -> list[dict[str, object]]:
        path = self._intent_path
        if not path.exists():
            return []
        try:
            raw = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise PaperAbstentionLearningError(
                "durable abstention intent journal is unreadable"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "schema_version", "records"}
            or raw.get("schema") != _INTENT_SCHEMA
            or raw.get("schema_version") != _INTENT_SCHEMA_VERSION
        ):
            raise PaperAbstentionLearningError(
                "durable abstention intent journal schema mismatch"
            )
        raw_records = raw.get("records")
        if type(raw_records) is not list:
            raise PaperAbstentionLearningError(
                "durable abstention intent records must be a list"
            )
        records: list[dict[str, object]] = []
        seen_observations: set[str] = set()
        required = {
            "environment_id",
            "episode_id",
            "policy_id",
            "observation_id",
            "action_id",
            "action_type",
            "decided_at",
            "parameters",
            "intent_sha256",
        }
        for raw_record in raw_records:
            if type(raw_record) is not dict or set(raw_record) != required:
                raise PaperAbstentionLearningError(
                    "durable abstention intent record fields mismatch"
                )
            record: dict[str, object] = dict(raw_record)
            for key in (
                "environment_id",
                "episode_id",
                "policy_id",
                "observation_id",
                "action_id",
                "action_type",
                "decided_at",
                "intent_sha256",
            ):
                value = record[key]
                if type(value) is not str or not value or value != value.strip():
                    raise PaperAbstentionLearningError(
                        f"durable abstention intent {key} is invalid"
                    )
            if record["action_type"] not in ABSTENTION_ACTION_TYPES:
                raise PaperAbstentionLearningError(
                    "durable abstention intent action_type is invalid"
                )
            if record["decided_at"] != _timestamp(
                record["decided_at"], "durable intent decided_at"  # type: ignore[arg-type]
            ):
                raise PaperAbstentionLearningError(
                    "durable abstention intent decided_at is not canonical"
                )
            parameters = record["parameters"]
            if type(parameters) is not list:
                raise PaperAbstentionLearningError(
                    "durable abstention intent parameters must be a list"
                )
            for item in parameters:
                if (
                    type(item) is not list
                    or len(item) != 2
                    or any(type(value) is not str for value in item)
                ):
                    raise PaperAbstentionLearningError(
                        "durable abstention intent parameter is invalid"
                    )
            observation_id = record["observation_id"]
            assert isinstance(observation_id, str)
            if observation_id in seen_observations:
                raise PaperAbstentionLearningError(
                    "durable abstention intent duplicates observation identity"
                )
            seen_observations.add(observation_id)
            expected = _digest(self._intent_digest_payload(record))
            if record["intent_sha256"] != expected:
                raise PaperAbstentionLearningError(
                    "durable abstention intent digest mismatch"
                )
            records.append(record)
        return records

    def _ensure_durable_intent(
        self,
        *,
        observation: Observation,
        action: Action,
        phase: AgentLoopPhase,
    ) -> None:
        candidate = self._intent_record(observation=observation, action=action)
        path = self._intent_path
        with WorkspaceEconomicLock(path.parent):
            records = self._read_intents()
            existing = next(
                (
                    record
                    for record in records
                    if record["observation_id"] == observation.observation_id
                ),
                None,
            )
            if existing is not None:
                if existing != candidate:
                    raise PaperAbstentionLearningError(
                        "durable abstention intent conflicts with retry payload"
                    )
                return
            if phase not in {
                AgentLoopPhase.BOOTSTRAP,
                AgentLoopPhase.CHECKPOINT,
            }:
                raise PaperAbstentionLearningError(
                    "durable abstention intent is missing for in-progress AgentLoop"
                )
            records.append(candidate)
            atomic_write_json(
                path,
                {
                    "schema": _INTENT_SCHEMA,
                    "schema_version": _INTENT_SCHEMA_VERSION,
                    "records": records,
                },
            )

    def begin_abstention(
        self,
        *,
        observation: Observation,
        action_type: str,
        decision_at: str,
        parameters: tuple[tuple[str, str], ...] = (),
        at: str,
    ) -> Action:
        """Commit one externally-admissible WAIT/NO_BET choice with no side effect.

        Exact retries are idempotent, including restart from every durable pre-action
        AgentLoop phase. The action intent is committed before the first phase mutation
        so a crash cannot let a retry substitute another WAIT/NO_BET payload.
        """

        if action_type not in ABSTENTION_ACTION_TYPES:
            raise PaperAbstentionLearningError(
                "abstention action_type must be WAIT or NO_BET"
            )
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if type(parameters) is not tuple:
            raise TypeError("parameters must be a canonical tuple")
        now = _timestamp(at, "at")
        _timestamp(decision_at, "decision_at")

        snapshot = self.agent_loop.snapshot()
        starting = snapshot.phase in {
            AgentLoopPhase.BOOTSTRAP,
            AgentLoopPhase.CHECKPOINT,
        }
        resumable = {
            AgentLoopPhase.OBSERVE,
            AgentLoopPhase.ASSESS,
            AgentLoopPhase.PLAN,
            AgentLoopPhase.DECIDE,
            AgentLoopPhase.ACT_OR_ABSTAIN,
            AgentLoopPhase.WAIT_OUTCOME,
        }
        if not starting and snapshot.phase not in resumable:
            raise PaperAbstentionLearningError(
                "abstention requires a new or resumable pre-outcome AgentLoop phase"
            )
        if not starting and snapshot.observation_id != observation.observation_id:
            raise PaperAbstentionLearningError(
                "durable AgentLoop observation differs from abstention retry"
            )

        try:
            retry_action = Action(
                environment_id=self.environment.environment_id,
                observation_id=observation.observation_id,
                action_type=action_type,
                decided_at=decision_at,
                parameters=parameters,
            )
        except LearningEnvironmentError as exc:
            raise PaperAbstentionLearningError(
                "abstention retry payload is not canonical"
            ) from exc

        if starting:
            try:
                baseline = self.environment.checkpoint()
            except LearningEnvironmentError:
                # A starting AgentLoop may legitimately pair with one exact pending
                # environment action only when the previous attempt failed before
                # publishing its durable intent. Reject any unrelated/multiple pending
                # authority before calling act(), because act() may otherwise create a
                # second pending action for another observation and strand the runtime.
                pending = self.environment._pending
                if len(pending) != 1 or retry_action.action_id not in pending:
                    raise PaperAbstentionLearningError(
                        "starting abstention conflicts with unresolved environment action"
                    )
                baseline = None
            if (
                baseline is not None
                and baseline.checkpoint_id != snapshot.environment_checkpoint_id
            ):
                raise PaperAbstentionLearningError(
                    "AgentLoop checkpoint differs from active environment"
                )
        else:
            self._ensure_durable_intent(
                observation=observation,
                action=retry_action,
                phase=snapshot.phase,
            )

        try:
            action = self.environment.act(
                observation,
                action_type=action_type,
                decision_at=decision_at,
                parameters=parameters,
            )
        except LearningEnvironmentError as exc:
            raise PaperAbstentionLearningError(
                "canonical environment rejected abstention intent"
            ) from exc

        if action.action_id != retry_action.action_id:
            raise PaperAbstentionLearningError(
                "canonical environment changed the durable abstention retry intent"
            )
        if starting:
            self._ensure_durable_intent(
                observation=observation,
                action=action,
                phase=snapshot.phase,
            )

        try:
            if starting:
                self.agent_loop.begin_observation(
                    observation,
                    environment_identity=self.environment.identity,
                    at=now,
                )
            snapshot = self.agent_loop.snapshot()
            if snapshot.observation_id != observation.observation_id:
                raise PaperAbstentionLearningError(
                    "durable AgentLoop observation differs from abstention intent"
                )
            while snapshot.phase in {
                AgentLoopPhase.OBSERVE,
                AgentLoopPhase.ASSESS,
                AgentLoopPhase.PLAN,
                AgentLoopPhase.DECIDE,
            }:
                self.agent_loop.advance(expected=snapshot.phase, at=now)
                snapshot = self.agent_loop.snapshot()
            if snapshot.phase not in {
                AgentLoopPhase.ACT_OR_ABSTAIN,
                AgentLoopPhase.WAIT_OUTCOME,
            }:
                raise PaperAbstentionLearningError(
                    "abstention retry did not converge to action commitment"
                )
            if (
                snapshot.phase is AgentLoopPhase.WAIT_OUTCOME
                and snapshot.action_id != action.action_id
            ):
                raise PaperAbstentionLearningError(
                    "durable AgentLoop action differs from abstention retry"
                )
            receipt = self.agent_loop.commit_action(
                action,
                episode=self.environment.episode,
                observation=observation,
                effect_state=ExternalEffectState.NONE,
                at=now,
            )
        except PaperAbstentionLearningError:
            raise
        except AgentLoopError as exc:
            raise PaperAbstentionLearningError(
                "AgentLoop rejected abstention action"
            ) from exc
        if (
            receipt.action_id != action.action_id
            or receipt.external_effect_state is not ExternalEffectState.NONE
        ):
            raise PaperAbstentionLearningError(
                "AgentLoop abstention receipt widened external-effect authority"
            )
        return action

    def finalize_abstention(
        self,
        *,
        observation: Observation,
        action: Action,
        outcome: Outcome,
        reward: RewardEvidence,
        at: str,
    ) -> PaperAbstentionFinalizationReceipt:
        """Learn from delayed abstention evidence and checkpoint exactly once.

        ``reward`` is mandatory evidence, not a value calculated here.  Negative,
        zero and positive rewards are all preserved exactly; positive abstention
        reward therefore exists only when an upstream evidence producer explicitly
        supplies and identifies it.  OBSERVED/SIMULATED truth is likewise preserved.
        """

        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if not isinstance(action, Action):
            raise TypeError("action must be Action")
        if not isinstance(outcome, Outcome) or not isinstance(reward, RewardEvidence):
            raise TypeError("outcome and reward must be canonical environment evidence")
        if action.action_type not in ABSTENTION_ACTION_TYPES:
            raise PaperAbstentionLearningError("action is not WAIT/NO_BET")
        if action.observation_id != observation.observation_id:
            raise PaperAbstentionLearningError(
                "abstention action does not bind supplied observation"
            )
        now = _timestamp(at, "at")
        reward_at = _timestamp(reward.available_at, "reward.available_at")
        if _instant(now, "at") < _instant(reward_at, "reward.available_at"):
            raise PaperAbstentionLearningError(
                "abstention finalization predates reward availability"
            )
        if outcome.truth is not reward.truth:
            raise PaperAbstentionLearningError(
                "abstention outcome/reward truth labels differ"
            )
        if outcome.simulation_model_id != reward.simulation_model_id:
            raise PaperAbstentionLearningError(
                "abstention outcome/reward simulation models differ"
            )

        snapshot = self.agent_loop.snapshot()
        if snapshot.action_id != action.action_id:
            raise PaperAbstentionLearningError(
                "AgentLoop current action differs from abstention action"
            )
        if snapshot.external_effect_state is not ExternalEffectState.NONE:
            raise PaperAbstentionLearningError(
                "abstention unexpectedly carries an external effect state"
            )

        transition = self._materialize_transition(
            observation=observation,
            action=action,
            outcome=outcome,
            reward=reward,
        )
        try:
            self.agent_loop.record_resolution(
                transition,
                outcome=outcome,
                reward=reward,
                at=now,
            )
        except AgentLoopError as exc:
            raise PaperAbstentionLearningError(
                "AgentLoop rejected abstention resolution"
            ) from exc

        snapshot = self.agent_loop.snapshot()
        if snapshot.phase is AgentLoopPhase.EVALUATE:
            self.agent_loop.advance(expected=AgentLoopPhase.EVALUATE, at=now)
            snapshot = self.agent_loop.snapshot()
        if snapshot.phase not in {
            AgentLoopPhase.ATTRIBUTE,
            AgentLoopPhase.REFLECT,
            AgentLoopPhase.CHECKPOINT,
        }:
            raise PaperAbstentionLearningError(
                "abstention resolution did not reach attribution path"
            )

        attribution = OutcomeAttribution(
            environment_id=transition.environment_id,
            episode_id=transition.episode_id,
            transition_id=transition.transition_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward_id=reward.reward_id,
            reward_value=reward.reward,
            truth=reward.truth,
            simulation_model_id=reward.simulation_model_id,
            attributed_at=reward_at,
            findings=(
                AttributionFinding(
                    component=AttributionComponent.RANDOMNESS,
                    status=AttributionStatus.UNKNOWN,
                    evidence_sha256=reward.reward_id,
                    evidence_available_at=reward_at,
                    contribution=None,
                    reason_code=_ABSTENTION_REASON,
                ),
            ),
        )
        try:
            self.agent_loop.record_attribution(attribution, at=now)
        except AgentLoopError as exc:
            raise PaperAbstentionLearningError(
                "AgentLoop rejected abstention attribution"
            ) from exc

        postmortem = ReflectionPostmortem(
            attribution_id=attribution.attribution_id,
            transition_id=transition.transition_id,
            created_at=reward_at,
            unresolved_components=(AttributionComponent.RANDOMNESS,),
            summary_code=_ABSTENTION_SUMMARY,
            research_question_statement=None,
        )
        try:
            self.agent_loop.record_postmortem(postmortem, at=now)
        except AgentLoopError as exc:
            raise PaperAbstentionLearningError(
                "AgentLoop rejected abstention postmortem"
            ) from exc

        snapshot = self.agent_loop.snapshot()
        if snapshot.phase is not AgentLoopPhase.CHECKPOINT:
            raise PaperAbstentionLearningError(
                "abstention postmortem did not reach CHECKPOINT"
            )
        next_checkpoint = self.environment.checkpoint()
        if snapshot.environment_checkpoint_id != next_checkpoint.checkpoint_id:
            try:
                self.agent_loop.commit_checkpoint(next_checkpoint, at=now)
            except AgentLoopError as exc:
                raise PaperAbstentionLearningError(
                    "AgentLoop rejected abstention checkpoint"
                ) from exc
            snapshot = self.agent_loop.snapshot()
        if (
            snapshot.environment_checkpoint_id != next_checkpoint.checkpoint_id
            or snapshot.checkpointed_transition_id != transition.transition_id
        ):
            raise PaperAbstentionLearningError(
                "AgentLoop checkpoint does not bind abstention transition"
            )

        self.environment = CausalLearningEnvironment.resume(
            self.environment.identity,
            episode_key=self.environment.episode.episode_key,
            policy_id=self.environment.episode.policy_id,
            admissible_actions=frozenset(self.environment.episode.admissible_actions),
            checkpoint=next_checkpoint,
        )
        return PaperAbstentionFinalizationReceipt(
            action_id=action.action_id,
            transition_id=transition.transition_id,
            attribution_id=attribution.attribution_id,
            postmortem_id=postmortem.postmortem_id,
            checkpoint_id=next_checkpoint.checkpoint_id,
            reward_id=reward.reward_id,
            reward_truth=reward.truth,
        )

    def _materialize_transition(
        self,
        *,
        observation: Observation,
        action: Action,
        outcome: Outcome,
        reward: RewardEvidence,
    ) -> Transition:
        """Rebuild the canonical transition across crash boundaries.

        The resolution instant is the immutable reward-availability instant rather
        than the retry wall clock.  A process that crashed after environment
        resolution but before AgentLoop/checkpoint publication can therefore replay
        from the prior checkpoint and recover the identical transition identity.
        """

        resolved_at = _timestamp(reward.available_at, "reward.available_at")
        snapshot = self.agent_loop.snapshot()
        try:
            live_checkpoint = self.environment.checkpoint()
        except LearningEnvironmentError:
            live_checkpoint = None

        if live_checkpoint is not None:
            if live_checkpoint.last_transition_id is not None:
                transition = Transition(
                    environment_id=self.environment.environment_id,
                    episode_id=self.environment.episode.episode_id,
                    step_index=live_checkpoint.step_index,
                    observation_id=observation.observation_id,
                    action_id=action.action_id,
                    outcome_id=outcome.outcome_id,
                    reward_id=reward.reward_id,
                    decision_at=action.decided_at,
                    resolved_at=resolved_at,
                )
                if transition.transition_id == live_checkpoint.last_transition_id:
                    if snapshot.transition_id not in {None, transition.transition_id}:
                        raise PaperAbstentionLearningError(
                            "resolved environment checkpoint conflicts with AgentLoop transition"
                        )
                    return transition
                if snapshot.transition_id == live_checkpoint.last_transition_id:
                    raise PaperAbstentionLearningError(
                        "resolved environment checkpoint conflicts with AgentLoop transition"
                    )

            if live_checkpoint.checkpoint_id != snapshot.environment_checkpoint_id:
                raise PaperAbstentionLearningError(
                    "environment checkpoint drifted from AgentLoop recovery boundary"
                )
            try:
                replayed = self.environment.act(
                    observation,
                    action_type=action.action_type,
                    decision_at=action.decided_at,
                    parameters=action.parameters,
                )
            except LearningEnvironmentError as exc:
                raise PaperAbstentionLearningError(
                    "cannot replay abstention from durable checkpoint"
                ) from exc
            if replayed.action_id != action.action_id:
                raise PaperAbstentionLearningError(
                    "replayed abstention action identity changed"
                )

        try:
            transition = self.environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=reward,
                resolved_at=resolved_at,
            )
        except LearningEnvironmentError as exc:
            raise PaperAbstentionLearningError(
                "canonical environment rejected abstention outcome/reward"
            ) from exc
        if snapshot.transition_id not in {None, transition.transition_id}:
            raise PaperAbstentionLearningError(
                "replayed abstention transition conflicts with AgentLoop history"
            )
        return transition


__all__ = [
    "ABSTENTION_ACTION_TYPES",
    "PaperAbstentionFinalizationReceipt",
    "PaperAbstentionLearningError",
    "PaperAbstentionLearningRuntime",
]
