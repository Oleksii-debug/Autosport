"""PAPER campaign runtime with pre-settlement reflection-plan commitment.

The implementation remains the existing campaign runtime.  This module adds one
narrow causal fence: the exact reflection semantics are hashed into the AgentLoop
Action before the PaperTicket is bound or can settle.  The bridge already seals
that exact Action in its binding, so a later rollback of the campaign sidecar and
bridge plan anchor cannot substitute different reflection semantics.
"""

from __future__ import annotations

from . import _paper_campaign_runtime_base as _base
from .learning_environment import (
    Action,
    EnvironmentCheckpoint,
    Observation,
    Outcome,
    RewardEvidence,
)
from .paper_abstention_learning import (
    PaperAbstentionFinalizationReceipt,
    PaperAbstentionLearningRuntime,
)
from .paper_settlement_learning import PaperSettlementLearningWitness


CAMPAIGN_SCHEMA = _base.CAMPAIGN_SCHEMA
CAMPAIGN_SCHEMA_VERSION = _base.CAMPAIGN_SCHEMA_VERSION
PaperCampaignRuntimeError = _base.PaperCampaignRuntimeError
PaperReflectionPlan = _base.PaperReflectionPlan
PaperCampaignFinalizationReceipt = _base.PaperCampaignFinalizationReceipt
PaperCampaignLearningHandoff = _base.PaperCampaignLearningHandoff

_REFLECTION_PLAN_PARAMETER = "paper_reflection_plan_sha256"


class PaperCampaignRuntime(_base.PaperCampaignRuntime):
    """Existing PAPER campaign runtime plus a causal reflection-plan precommit."""

    def _reflection_commitment_semantic(self, *, committed_at: str) -> dict[str, object]:
        canonical_committed_at = _base._timestamp(
            committed_at,
            "reflection plan committed_at",
        )
        deadline = self.reflection_plan.research_deadline_at
        return {
            "summary_code": self.reflection_plan.summary_code,
            "reason_code": self.reflection_plan.reason_code,
            "unresolved_components": [
                component.value for component in self.reflection_plan.unresolved_components
            ],
            "research_question_statement": self.reflection_plan.research_question_statement,
            "research_budget_units": self.reflection_plan.research_budget_units,
            "research_deadline_at": (
                None
                if deadline is None
                else _base._timestamp(deadline, "research_deadline_at")
            ),
            "committed_at": canonical_committed_at,
        }

    def _reflection_commitment_id(self, *, committed_at: str) -> str:
        return _base._digest(
            self._reflection_commitment_semantic(committed_at=committed_at)
        )

    def _parameters_with_reflection_commitment(
        self,
        parameters: tuple[tuple[str, str], ...],
        *,
        decision_at: str,
    ) -> tuple[tuple[str, str], ...]:
        if type(parameters) is not tuple:
            raise TypeError("parameters must be a canonical tuple")
        if any(
            type(entry) is tuple
            and len(entry) == 2
            and entry[0] == _REFLECTION_PLAN_PARAMETER
            for entry in parameters
        ):
            raise PaperCampaignRuntimeError(
                "caller cannot supply the internal reflection-plan commitment"
            )
        committed = (
            _REFLECTION_PLAN_PARAMETER,
            self._reflection_commitment_id(committed_at=decision_at),
        )
        return tuple(sorted((*parameters, committed)))

    def _canonical_abstention_runtime(self) -> PaperAbstentionLearningRuntime:
        """Bind WAIT/NO_BET to the campaign's current environment authority."""

        runtime = getattr(self, "_abstention_learning_runtime", None)
        if (
            runtime is None
            or runtime.environment is not self.environment
            or runtime.agent_loop is not self.agent_loop
        ):
            runtime = PaperAbstentionLearningRuntime(
                environment=self.environment,
                agent_loop=self.agent_loop,
            )
            self._abstention_learning_runtime = runtime
        return runtime

    def begin_abstention(
        self,
        *,
        observation: Observation,
        action_type: str,
        decision_at: str,
        parameters: tuple[tuple[str, str], ...] = (),
        at: str,
    ) -> Action:
        """Route a no-ticket WAIT/NO_BET through the canonical campaign entrypoint."""

        return self._canonical_abstention_runtime().begin_abstention(
            observation=observation,
            action_type=action_type,
            decision_at=decision_at,
            parameters=parameters,
            at=at,
        )

    def finalize_abstention(
        self,
        *,
        observation: Observation,
        action: Action,
        outcome: Outcome,
        reward: RewardEvidence,
        at: str,
    ) -> PaperAbstentionFinalizationReceipt:
        """Finalize no-ticket learning without touching settlement authority."""

        abstention_runtime = self._canonical_abstention_runtime()
        receipt = abstention_runtime.finalize_abstention(
            observation=observation,
            action=action,
            outcome=outcome,
            reward=reward,
            at=at,
        )
        # The helper resumes from the exact committed checkpoint. Keep the
        # canonical campaign object on that same environment authority so the
        # next observation starts from the durable checkpoint just returned.
        self.environment = abstention_runtime.environment
        self._environment_checkpoint_id = receipt.checkpoint_id
        return receipt

    def _bound_reflection_commitment(
        self,
        witness: PaperSettlementLearningWitness,
    ) -> tuple[str, str]:
        parameters = dict(witness.action.parameters)
        durable = parameters.get(_REFLECTION_PLAN_PARAMETER)
        if durable is None:
            raise PaperCampaignRuntimeError(
                "PAPER action lacks pre-settlement reflection-plan commitment"
            )
        durable_id = _base._sha(durable, "reflection_plan_commitment_id")
        committed_at = _base._timestamp(
            witness.action.decided_at,
            "reflection plan committed_at",
        )
        expected_id = self._reflection_commitment_id(committed_at=committed_at)
        if durable_id != expected_id:
            raise PaperCampaignRuntimeError(
                "durable campaign finalization plan conflicts with pre-settlement reflection commitment"
            )
        return durable_id, committed_at

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
        """Bind the immutable reflection policy into the exact PAPER Action.

        ``decision_at`` is deterministic across an exact retry and is the latest
        possible causal availability of a plan that is already supplied to this
        runtime before the action is committed.  Including the resulting digest
        in Action parameters makes it part of both ``action_id`` and the bridge's
        existing durable binding without adding a second persistence authority.
        """

        bound_parameters = self._parameters_with_reflection_commitment(
            parameters,
            decision_at=decision_at,
        )
        return super().begin_and_bind_paper_ticket(
            ticket_id=ticket_id,
            decision_id=decision_id,
            observation=observation,
            action_type=action_type,
            decision_at=decision_at,
            parameters=bound_parameters,
            at=at,
            baseline_checkpoint=baseline_checkpoint,
        )

    def _bind_finalization_plan(
        self,
        witness: PaperSettlementLearningWitness,
        *,
        available_at: str,
        require_existing: bool = False,
    ) -> str:
        """Derive one retry-stable plan time from precommit + reward evidence.

        The caller's finalization time is only an upper causal bound.  Reflection
        semantics existed by the committed Action time, but attribution cannot be
        available before the authoritative reward.  Therefore the canonical first
        availability is the later of those two immutable times, independent of a
        T5/T6 retry clock.  Sidecar/bridge anchors remain redundant recovery caches;
        they are no longer the root proving which reflection semantics existed.
        """

        requested_at = _base._timestamp(available_at, "reflection_plan available_at")
        _commitment_id, committed_at = self._bound_reflection_commitment(witness)
        reward_at = _base._timestamp(
            witness.reward.available_at,
            "reward.available_at",
        )
        causal_available_at = max(
            (committed_at, reward_at),
            key=lambda value: _base._instant(value, "reflection availability"),
        )
        if _base._instant(
            requested_at,
            "reflection_plan available_at",
        ) < _base._instant(
            causal_available_at,
            "causal reflection availability",
        ):
            raise PaperCampaignRuntimeError(
                "campaign finalization predates causal reflection availability"
            )

        # The deadline fences creation of the *first* external research request.
        # Once AgentLoop has durably recorded that handoff, an exact restart must
        # still be able to replay/ack it and commit the checkpoint after expiry.
        # `research_run_id` is the canonical durable proof that this episode's
        # handoff already exists; the pre-settlement reflection commitment above
        # still verifies that the retried plan is exactly the one that was bound.
        deadline = self.reflection_plan.research_deadline_at
        loop_snapshot = self.agent_loop.snapshot()
        research_run_id = loop_snapshot.research_run_id
        recovering_unacked_handoff = (
            loop_snapshot.phase is _base.AgentLoopPhase.RESEARCH_HANDOFF
        )
        if (
            deadline is not None
            and research_run_id is None
            and not recovering_unacked_handoff
            and _base._instant(
                deadline,
                "research_deadline_at",
            )
            < _base._instant(requested_at, "reflection_plan available_at")
        ):
            raise PaperCampaignRuntimeError(
                "research deadline predates frozen reflection availability"
            )

        # A valid Action commitment is the causal root.  Even if every later
        # campaign-plan cache is restored to a pre-finalization snapshot, exact
        # retry may safely regenerate the same deterministic plan while changed
        # semantics fail above before any AgentLoop mutation.
        return super()._bind_finalization_plan(
            witness,
            available_at=causal_available_at,
            require_existing=False,
        )


__all__ = [
    "PaperAbstentionFinalizationReceipt",
    "PaperCampaignFinalizationReceipt",
    "PaperCampaignLearningHandoff",
    "PaperCampaignRuntime",
    "PaperCampaignRuntimeError",
    "PaperReflectionPlan",
]
