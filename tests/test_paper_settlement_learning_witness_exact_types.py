from autosport.continuous_session import SettlementResolution
from decimal import Decimal
import hashlib

import pytest

from autosport.learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.paper_settlement_learning import (
    PaperSettlementLearningBridge,
    PaperSettlementLearningBridgeError,
    PaperSettlementLearningWitness,
)


def _graph(observation_type=Observation):
    environment_id = "a" * 64
    episode_id = "b" * 64
    observation = observation_type(
        environment_id=environment_id,
        observed_at="2026-10-07T00:00:00Z",
        available_at="2026-10-07T00:00:01Z",
        evidence=(),
    )
    action = Action(
        environment_id=environment_id,
        observation_id=observation.observation_id,
        action_type="PAPER_PROPOSAL",
        decided_at="2026-10-07T00:00:02Z",
        parameters=(),
    )
    outcome = Outcome(
        environment_id=environment_id,
        action_id=action.action_id,
        revealed_at="2026-10-07T00:00:03Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(),
    )
    reward = RewardEvidence(
        environment_id=environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("1"),
        available_at="2026-10-07T00:00:04Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(),
    )
    transition = Transition(
        environment_id=environment_id,
        episode_id=episode_id,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at="2026-10-07T00:00:02Z",
        resolved_at="2026-10-07T00:00:04Z",
    )
    baseline = EnvironmentCheckpoint(
        environment_id=environment_id,
        episode_id=episode_id,
        policy_id="policy-1",
        step_index=0,
        chain_sha256=hashlib.sha256(b"").hexdigest(),
        last_transition_id=None,
        committed_action_ids=(),
        committed_decision_intents=(),
    )
    next_checkpoint = EnvironmentCheckpoint(
        environment_id=environment_id,
        episode_id=episode_id,
        policy_id="policy-1",
        step_index=1,
        chain_sha256="d" * 64,
        last_transition_id=transition.transition_id,
        committed_action_ids=(action.action_id,),
        committed_decision_intents=(("e" * 64, "f" * 64),),
    )
    return observation, action, outcome, reward, transition, baseline, next_checkpoint


def test_witness_rejects_observation_subclass_before_identity_graph_dispatch() -> None:
    class ObservationAlias(Observation):
        pass

    observation, action, outcome, reward, transition, baseline, next_checkpoint = _graph(
        ObservationAlias
    )

    with pytest.raises(TypeError, match="exact canonical record type"):
        PaperSettlementLearningWitness(
            ticket_id="ticket-1",
            binding_id="e" * 64,
            settlement_bundle_sha256="f" * 64,
            observation=observation,
            action=action,
            outcome=outcome,
            reward=reward,
            transition=transition,
            baseline_checkpoint=baseline,
            next_checkpoint=next_checkpoint,
        )


class _TrapEnvironment(CausalLearningEnvironment):
    def __getattribute__(self, name):
        raise AssertionError("environment subtype dispatch must not execute")


class _TrapObservation(Observation):
    def __getattribute__(self, name):
        raise AssertionError("observation subtype dispatch must not execute")


class _TrapAction(Action):
    def __getattribute__(self, name):
        raise AssertionError("action subtype dispatch must not execute")


class _TrapCheckpoint(EnvironmentCheckpoint):
    def __getattribute__(self, name):
        raise AssertionError("checkpoint subtype dispatch must not execute")


class _TrapSettlementResolution(SettlementResolution):
    def __getattribute__(self, name):
        raise AssertionError("settlement subtype dispatch must not execute")


@pytest.mark.parametrize(
    ("field", "alias_type"),
    (
        ("environment", _TrapEnvironment),
        ("observation", _TrapObservation),
        ("action", _TrapAction),
        ("baseline_checkpoint", _TrapCheckpoint),
    ),
)
def test_bind_ticket_rejects_identity_record_subclasses_before_dispatch(
    field,
    alias_type,
) -> None:
    values = {
        "environment": object.__new__(CausalLearningEnvironment),
        "observation": object.__new__(Observation),
        "action": object.__new__(Action),
        "baseline_checkpoint": object.__new__(EnvironmentCheckpoint),
    }
    values[field] = object.__new__(alias_type)

    with pytest.raises(TypeError, match=field):
        PaperSettlementLearningBridge.bind_ticket(
            object(),
            ticket_id="ticket-identity-fence",
            decision_id="decision-identity-fence",
            **values,
        )


def test_collect_evidence_rejects_settlement_subclass_before_validate_dispatch() -> None:
    class _TicketStub:
        legs = ()

    alias = object.__new__(_TrapSettlementResolution)
    with pytest.raises(
        PaperSettlementLearningBridgeError,
        match="non-canonical settlement evidence",
    ):
        PaperSettlementLearningBridge._collect_evidence(
            _TicketStub(),
            (alias,),
            at="2026-10-07T00:00:00Z",
        )
