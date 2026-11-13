"""Demonstrate a causal TT forecast-choice update on invented inputs only.

This is not a data adapter, evaluation, betting policy, or promotion authority.
The canonical opponent store requires OBSERVED; those records are exclusively
invented test fixtures in a disposable temporary directory.  Learning outcomes
and rewards use SIMULATED truth and remain distinct from factual evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from autosport.learning_environment import (
    CausalLearningEnvironment, EnvironmentIdentity, EvidenceTruth,
    LearningEnvironmentError, Observation, Outcome, RewardEvidence,
)
from autosport.opponent_intelligence import ObservedPerformance, OpponentIntelligenceStore
from autosport.participant_identity import (
    AliasRecord, EntityIdentity, EntityKind, ParticipantIdentityRegistry,
)
from autosport.transparent_bandit_policy import BanditPolicyState
from causal_h2h_observer import SOURCE
from causal_strength_h2h import compare

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURE_SHA = "a" * 64
SIMULATION = "synthetic-tt-learning-example-v1"
ACTIONS = frozenset({"FORM_H2H", "MARKET"})


def _time(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def _fixture_stores(root):
    registry = ParticipantIdentityRegistry.initialize_pristine(root / "identity.json")
    for label in ("A", "B", "C", "D", "E", "F", "L"):
        kind = EntityKind.LEAGUE if label == "L" else EntityKind.PARTICIPANT
        registry.add_entity(EntityIdentity(label, kind, "synthetic:" + label,
                                           FIXTURE_SHA, _time(0), _time(0)))
        # SOURCE is required by the existing comparator. It is a fixture label,
        # not a claim that the named provider supplied any of these records.
        registry.add_alias(AliasRecord(SOURCE, label, label, _time(0), None,
                                       _time(0), FIXTURE_SHA, _time(0)))
    store = OpponentIntelligenceStore.initialize_pristine(root / "opponents.json", registry)
    for event, subject, opponent, score in (
        ("fake-ac", "A", "C", "1"), ("fake-ad", "A", "D", "1"),
        ("fake-be", "B", "E", "0"), ("fake-bf", "B", "F", "0"),
        ("fake-ab", "A", "B", "1"),
    ):
        store.record_performance(_performance(event, subject, opponent, score, 10, 20))
    return registry, store


def _performance(event, subject, opponent, score, observed, available):
    return ObservedPerformance(
        event_id=event, source_id=SOURCE, subject_alias=subject, opponent_alias=opponent,
        sport_id="table_tennis", league_alias="L", market_context_id="match-outcome",
        score=score, observed_at=_time(observed), available_at=_time(available),
        recorded_at=_time(available), evidence_sha256=FIXTURE_SHA,
        truth=EvidenceTruth.OBSERVED,
    )


def run_demo(*, first_outcome=0):
    """Return a deterministic, explicitly simulated trace; no real-data input."""
    if type(first_outcome) is not int or first_outcome not in (0, 1):
        raise ValueError("synthetic outcome must be the integer 0 or 1")
    with TemporaryDirectory(prefix="autosport-synthetic-tt-") as directory:
        registry, store = _fixture_stores(Path(directory))
        identity = EnvironmentIdentity(source_id="synthetic", config_id=SIMULATION,
                                       data_id="invented-fixtures", protocol_id=SIMULATION,
                                       cutoff_ts=_time(35), seed=0)
        policy = BanditPolicyState.initial(environment_id=identity.environment_id,
                                           protocol_id=SIMULATION, config_sha256=FIXTURE_SHA,
                                           seed=0, action_types=ACTIONS)
        env = CausalLearningEnvironment(identity, episode_key="synthetic-overlap",
                                        policy_id=policy.policy_id, admissible_actions=ACTIONS)
        market = ("A", "B", "L", {"A": ("1", Decimal("1.5")),
                                   "B": ("2", Decimal("2.5"))})

        def decide(fixture, second, state):
            features = compare(fixture, market, _time(second), registry, store)
            probabilities = {"MARKET": features["market_home_probability"],
                             "FORM_H2H": features["strength_h2h_home_probability"]}
            admissible = frozenset(k for k, value in probabilities.items() if value is not None)
            selected = state.choose(admissible_actions=admissible)
            # Include the invented fixture label so equal market values at two
            # different decisions are distinct observation intents.
            evidence = tuple(sorted([(k, str(v)) for k, v in probabilities.items()]
                                    + [("fixture", fixture)]))
            observation = Observation(identity.environment_id, _time(second), _time(second), evidence)
            action = env.act(observation, action_type=selected, decision_at=_time(second))
            trace = {"fixture": fixture, "decision_at": _time(second),
                     "policy_generation": state.generation, "action": selected,
                     "market_probability": probabilities["MARKET"],
                     "form_h2h_probability": probabilities["FORM_H2H"],
                     "prior_h2h": features["h2h_support"],
                     "strength_status": features["strength_status"]}
            return action, probabilities, trace

        action1, probabilities1, first = decide("fake-current-1", 31, policy)
        _, _, second = decide("fake-current-2", 32, policy)
        outcome = Outcome(identity.environment_id, action1.action_id, _time(33),
                          EvidenceTruth.SIMULATED, (("winner_a", str(first_outcome)),), SIMULATION)
        chosen_p = Decimal(probabilities1[action1.action_type])
        reward = RewardEvidence(identity.environment_id, action1.action_id, outcome.outcome_id,
                                -((chosen_p - first_outcome) ** 2), _time(33),
                                EvidenceTruth.SIMULATED, (), SIMULATION)
        try:
            env.resolve(action1.action_id, outcome=outcome, reward=reward, resolved_at=_time(32.5))
        except LearningEnvironmentError as exc:
            if "not yet available" not in str(exc):
                raise
            early_rejected = True
        else:
            raise AssertionError("future outcome unexpectedly resolved")
        transition = env.resolve(action1.action_id, outcome=outcome, reward=reward,
                                 resolved_at=_time(33))
        next_policy, update = policy.update(action=action1, reward=reward, transition=transition)
        try:
            next_policy.update(action=action1, reward=reward, transition=transition)
        except LearningEnvironmentError as exc:
            if "already has a durable policy update" not in str(exc):
                raise
            duplicate_rejected = True
        else:
            raise AssertionError("duplicate reward unexpectedly learned")

        store.record_performance(_performance("fake-current-1", "A", "B", str(first_outcome), 31, 33))
        at_reveal = compare("fake-current-3", market, _time(33), registry, store)
        _, _, third = decide("fake-current-3", 34, next_policy)
        return {
            "schema": "research.synthetic_tt_learning_cycle.v1",
            "fixture_only": True, "learning_evidence_truth": reward.truth.value,
            "first_outcome": first_outcome, "decisions": [first, second, third],
            "first_reward_negative_brier": str(reward.reward),
            "policy_updates": next_policy.generation,
            "update_binds_first_action": update.action_id == action1.action_id,
            "early_resolution_rejected": early_rejected,
            "duplicate_update_rejected": duplicate_rejected,
            "prior_h2h_exactly_at_reveal": at_reveal["h2h_support"],
            "physical_people_verified": False, "physical_results_verified": False,
            "real_market_data_used": False, "independent_holdout": False,
            "calibration_verified": False, "portfolio_executed": False,
            "real_money_execution": False, "profitability_claim": False,
            "note": "All fixtures and times are invented; negative Brier is not profit.",
        }


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
