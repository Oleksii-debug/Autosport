"""Causal PAPER learning for explicit WAIT/NO_BET abstentions.

This is a narrow adapter over the existing :class:`CausalLearningEnvironment` and
:class:`AgentLoopRuntime` authorities.  It does not choose whether to abstain,
compute a reward, widen admissible actions, execute provider effects, or promote a
policy.  In particular, an abstention never receives an implicit positive reward:
callers must supply the exact delayed ``Outcome`` and ``RewardEvidence`` that the
canonical learning environment can validate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

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


ABSTENTION_ACTION_TYPES = frozenset({"NO_BET", "WAIT"})
_ABSTENTION_SUMMARY = "PAPER_ABSTENTION_REQUIRES_CAUSAL_REVIEW"
_ABSTENTION_REASON = "ABSTENTION_OUTCOME_ONLY_NO_CAUSAL_DECOMPOSITION"


class PaperAbstentionLearningError(RuntimeError):
    """Abstention evidence conflicts with the canonical causal authorities."""


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

        Exact retries are idempotent.  The canonical environment still owns the
        admissible-action set, so this method cannot introduce WAIT/NO_BET when the
        episode did not authorize it.
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
        if snapshot.phase in {AgentLoopPhase.BOOTSTRAP, AgentLoopPhase.CHECKPOINT}:
            try:
                baseline = self.environment.checkpoint()
            except LearningEnvironmentError as exc:
                raise PaperAbstentionLearningError(
                    "new abstention requires an exact resolved checkpoint"
                ) from exc
            if baseline.checkpoint_id != snapshot.environment_checkpoint_id:
                raise PaperAbstentionLearningError(
                    "AgentLoop checkpoint differs from active environment"
                )
            try:
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
            except AgentLoopError as exc:
                raise PaperAbstentionLearningError(
                    "AgentLoop rejected abstention observation"
                ) from exc
        elif snapshot.phase is not AgentLoopPhase.WAIT_OUTCOME:
            raise PaperAbstentionLearningError(
                "abstention requires BOOTSTRAP, CHECKPOINT, or exact WAIT_OUTCOME retry"
            )

        try:
            action = self.environment.act(
                observation,
                action_type=action_type,
                decision_at=decision_at,
                parameters=parameters,
            )
            receipt = self.agent_loop.commit_action(
                action,
                episode=self.environment.episode,
                observation=observation,
                effect_state=ExternalEffectState.NONE,
                at=now,
            )
        except (LearningEnvironmentError, AgentLoopError) as exc:
            raise PaperAbstentionLearningError(
                "canonical authorities rejected abstention action"
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
            if (
                snapshot.transition_id is not None
                and live_checkpoint.last_transition_id == snapshot.transition_id
            ):
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
                if transition.transition_id != snapshot.transition_id:
                    raise PaperAbstentionLearningError(
                        "resolved environment checkpoint conflicts with AgentLoop transition"
                    )
                return transition

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
