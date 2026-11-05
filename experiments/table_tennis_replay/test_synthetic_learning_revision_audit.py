"""Integration uses invented forecasts, players, receipts and results only."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, localcontext

import pytest

from autosport.learning_environment import (
    CausalLearningEnvironment, EnvironmentIdentity, EvidenceTruth,
    Observation, Outcome, RewardEvidence,
)
from autosport.transparent_bandit_policy import BanditPolicyState
from causal_h2h_observer import verify_journal
from causal_strength_revision_history import evaluate_history
from synthetic_learning_revision_audit import (
    SIMULATION, SimulatedBinding, _forecast_candidates, _metadata, audit,
)
from test_causal_strength_frozen import dated, seconds
from test_causal_strength_revision_history import add, rows, setup_history


def prepare(root, *, fixtures=("full",)):
    history, pins = setup_history(root)
    original = rows()
    head = add(root, history, pins, None, original, 0)
    journal, _, _ = verify_journal(root)
    forecasts = {item["fixture_id"]: item for item in journal["forecasts"]}
    snapshot = evaluate_history(root, history, head, **pins)["snapshots"][0]
    selected_rows = {item["fixture_id"]: item for item in snapshot["rows"]}
    identity = EnvironmentIdentity(source_id="synthetic", config_id=SIMULATION,
                                   data_id="invented-tt-records", protocol_id=SIMULATION,
                                   cutoff_ts=seconds(112.9).isoformat(), seed=0)
    policy = BanditPolicyState.initial(environment_id=identity.environment_id,
                                      protocol_id=SIMULATION, config_sha256="a" * 64,
                                      seed=0, action_types=frozenset({"MARKET", "FORM", "FORM_H2H"}))
    env = CausalLearningEnvironment(identity, episode_key="synthetic-revision-test",
                                    policy_id=policy.policy_id,
                                    admissible_actions=frozenset({"MARKET", "FORM", "FORM_H2H"}))
    # Decisions intentionally precede all results, even when updates overlap.
    pending = []
    for n, fixture in enumerate(fixtures):
        candidates = _forecast_candidates(forecasts[fixture])
        at = seconds(112.6 + n * 0.01).isoformat()
        evidence = _metadata(fixture_id=fixture, forecast_sha256=pins["expected_forecast_sha256"],
                             **{"prob_" + key: value for key, value in candidates.items()})
        observation = Observation(identity.environment_id, at, at, evidence)
        choice = policy.choose(admissible_actions=frozenset(candidates))
        action = env.act(observation, action_type=choice, decision_at=at,
                         parameters=_metadata(fixture_id=fixture,
                                              forecast_sha256=pins["expected_forecast_sha256"]))
        pending.append((fixture, candidates, observation, action))
    bindings = []
    for n, (fixture, candidates, observation, action) in enumerate(pending):
        winner = selected_rows[fixture]["outcome"]
        outcome = Outcome(identity.environment_id, action.action_id, snapshot["as_of"],
                          EvidenceTruth.SIMULATED, _metadata(winner_home=str(winner)), SIMULATION)
        with localcontext() as ctx:
            ctx.prec = 28
            loss = -((Decimal(candidates[action.action_type]) - winner) ** 2)
        reward = RewardEvidence(identity.environment_id, action.action_id, outcome.outcome_id,
                                loss, snapshot["as_of"], EvidenceTruth.SIMULATED,
                                _metadata(history_link_sha256=snapshot["link_sha256"]), SIMULATION)
        transition = env.resolve(action.action_id, outcome=outcome, reward=reward,
                                 resolved_at=seconds(201 + n).isoformat())
        policy, update = policy.update(action=action, reward=reward, transition=transition)
        bindings.append(SimulatedBinding(fixture, observation, outcome, update))
    return history, pins, head, original, policy, bindings


def check(root, history, head, pins, policy, bindings, when=250):
    return audit(root, history, head, policy=policy, bindings=bindings,
                 decision_at=seconds(when).isoformat(), **pins)


def rebuild_one(policy, binding, *, outcome=None, reward=None, transition=None):
    """Create a valid policy chain around deliberately altered evidence."""
    update = binding.update
    outcome = outcome or binding.outcome
    reward = reward or update.reward
    transition = transition or update.transition
    transition = replace(transition, outcome_id=outcome.outcome_id, reward_id=reward.reward_id)
    initial = BanditPolicyState.initial(environment_id=policy.environment_id,
                                       protocol_id=policy.protocol_id,
                                       config_sha256=policy.config_sha256, seed=policy.seed,
                                       action_types=frozenset(x.action_type for x in policy.estimates))
    successor, evidence = initial.update(action=update.action, reward=reward, transition=transition)
    return successor, replace(binding, outcome=outcome, update=evidence)


def test_changed_result_holds_later_and_reversion_cannot_clear_quarantine(tmp_path):
    history, pins, head, original, policy, bindings = prepare(tmp_path)
    before = check(tmp_path, history, head, pins, policy, bindings)
    changed = [dated("full", "A", "B", "away"), original[1], original[2]]
    head = add(tmp_path, history, pins, head, changed, 1)
    # Equal-time result is outside the strict decision-time view.
    equal = check(tmp_path, history, head, pins, policy, bindings, 300)
    after = check(tmp_path, history, head, pins, policy, bindings, 301)
    assert equal == before
    assert after["status"] == "held_for_review" and after["used_results_quarantined"] == 1
    head = add(tmp_path, history, pins, head, original, 2)
    later = check(tmp_path, history, head, pins, policy, bindings, 401)
    assert later["status"] == "held_for_review"
    assert later["policy_generation"] == 1 and later["policy_modified"] is False
    assert later["production_enforcement"] is later["model_training_authorized"] is False


@pytest.mark.parametrize("change", ["missing", "score", "invalid"])
def test_missing_changed_or_invalid_used_result_holds(tmp_path, change):
    history, pins, head, original, policy, bindings = prepare(tmp_path)
    changed = deepcopy(original)
    if change == "missing":
        changed = changed[1:]
    elif change == "score":
        changed[0]["periods"][0] = {"score": "11-3"}
        changed[0]["final_score"] = "11-3,11-2,11-2"
    else:
        changed[0]["final_score"] = None
    head = add(tmp_path, history, pins, head, changed, 1)
    assert check(tmp_path, history, head, pins, policy, bindings, 301)["status"] == "held_for_review"


def test_unrelated_quarantine_does_not_hold_used_result(tmp_path):
    history, pins, head, original, policy, bindings = prepare(tmp_path)
    changed = [original[0], dated("form", "A", "E", "home"), original[2]]
    head = add(tmp_path, history, pins, head, changed, 1)
    result = check(tmp_path, history, head, pins, policy, bindings, 301)
    assert result["status"] == "ready_for_simulation" and result["used_results_quarantined"] == 0


def test_overlapping_decisions_can_have_two_causally_ordered_updates(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path, fixtures=("full", "form"))
    result = check(tmp_path, history, head, pins, policy, bindings)
    assert result["verified_bindings"] == result["policy_generation"] == 2
    assert result["status"] == "ready_for_simulation"
    assert bindings[0].update.action.decided_at < bindings[1].update.action.decided_at
    assert bindings[1].update.action.decided_at < bindings[0].update.transition.resolved_at


def test_duplicate_fixture_learned_twice_is_rejected(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path, fixtures=("full", "full"))
    with pytest.raises(ValueError, match="duplicate exact fixture"):
        check(tmp_path, history, head, pins, policy, bindings)


def test_incomplete_ledger_and_modified_policy_are_rejected(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    with pytest.raises(ValueError, match="does not cover"):
        check(tmp_path, history, head, pins, policy, [])
    estimates = list(policy.estimates)
    estimates[0] = replace(estimates[0], reward_sum=estimates[0].reward_sum + Decimal("1"))
    changed = replace(policy, estimates=tuple(estimates))
    with pytest.raises(ValueError, match="policy differs"):
        check(tmp_path, history, head, pins, changed, bindings)


def test_modified_observation_or_outcome_is_rejected(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    binding = bindings[0]
    changed_observation = replace(binding.observation, evidence=_metadata(fixture_id="other"))
    with pytest.raises(ValueError, match="not bound"):
        check(tmp_path, history, head, pins, policy, [replace(binding, observation=changed_observation)])
    changed_outcome = replace(binding.outcome, evidence=_metadata(winner_home="0"))
    with pytest.raises(ValueError, match="not bound"):
        check(tmp_path, history, head, pins, policy, [replace(binding, outcome=changed_outcome)])


def test_recomputed_policy_chain_cannot_hide_wrong_winner_or_brier(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    binding = bindings[0]
    wrong = replace(binding.outcome, evidence=_metadata(winner_home="0"))
    wrong_reward = replace(binding.update.reward, outcome_id=wrong.outcome_id)
    forged, witness = rebuild_one(policy, binding, outcome=wrong, reward=wrong_reward)
    with pytest.raises(ValueError, match="does not match the visible result"):
        check(tmp_path, history, head, pins, forged, [witness])
    altered = replace(binding.update.reward, reward=Decimal("-0.75"))
    forged, witness = rebuild_one(policy, binding, reward=altered)
    with pytest.raises(ValueError, match="Brier"):
        check(tmp_path, history, head, pins, forged, [witness])


def test_recomputed_policy_chain_cannot_hide_wrong_link_or_observed_truth(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    binding = bindings[0]
    altered = replace(binding.update.reward, evidence=_metadata(history_link_sha256="f" * 64))
    forged, witness = rebuild_one(policy, binding, reward=altered)
    with pytest.raises(ValueError, match="latest visible history link"):
        check(tmp_path, history, head, pins, forged, [witness])
    outcome = replace(binding.outcome, truth=EvidenceTruth.OBSERVED, simulation_model_id=None)
    reward = replace(binding.update.reward, truth=EvidenceTruth.OBSERVED,
                     simulation_model_id=None, outcome_id=outcome.outcome_id)
    forged, witness = rebuild_one(policy, binding, outcome=outcome, reward=reward)
    with pytest.raises(ValueError, match="only explicitly simulated"):
        check(tmp_path, history, head, pins, forged, [witness])


def test_update_after_quarantine_of_an_earlier_used_result_fails_closed(tmp_path):
    history, pins, head, original, policy, bindings = prepare(tmp_path, fixtures=("full", "form"))
    changed = [dated("full", "A", "B", "away"), original[1], original[2]]
    head = add(tmp_path, history, pins, head, changed, 1)
    link = evaluate_history(tmp_path, history, head, **pins)["snapshots"][-1]["link_sha256"]
    second = bindings[1]
    reward = replace(second.update.reward, evidence=_metadata(history_link_sha256=link))
    transition = replace(second.update.transition, reward_id=reward.reward_id,
                         resolved_at=seconds(301).isoformat())
    first = bindings[0].update
    initial = BanditPolicyState.initial(environment_id=policy.environment_id,
                                       protocol_id=policy.protocol_id,
                                       config_sha256=policy.config_sha256, seed=policy.seed,
                                       action_types=frozenset(x.action_type for x in policy.estimates))
    state, _ = initial.update(action=first.action, reward=first.reward, transition=first.transition)
    state, witness = state.update(action=second.update.action, reward=reward, transition=transition)
    with pytest.raises(ValueError, match="earlier used result was quarantined"):
        check(tmp_path, history, head, pins, state, [bindings[0], replace(second, update=witness)], 302)


def test_reward_check_does_not_depend_on_ambient_decimal_rounding(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    with localcontext() as context:
        context.prec = 7
        context.rounding = ROUND_DOWN
        assert check(tmp_path, history, head, pins, policy, bindings)["status"] == "ready_for_simulation"


def test_source_history_and_both_forecast_pins_are_required(tmp_path):
    history, pins, head, original, policy, bindings = prepare(tmp_path)
    for key in pins:
        wrong = dict(pins, **{key: "f" * 64})
        with pytest.raises(ValueError):
            check(tmp_path, history, head, wrong, policy, bindings)
    old_head = head
    head = add(tmp_path, history, pins, head, original, 1)
    with pytest.raises(ValueError, match="head differs"):
        check(tmp_path, history, old_head, pins, policy, bindings, 301)
    (history / "0002.body.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        check(tmp_path, history, head, pins, policy, bindings, 301)


def test_future_policy_update_cannot_enter_same_time_decision(tmp_path):
    history, pins, head, _, policy, bindings = prepare(tmp_path)
    for at in (200, 201):
        with pytest.raises(ValueError):
            check(tmp_path, history, head, pins, policy, bindings, at)
    assert check(tmp_path, history, head, pins, policy, bindings, 202)["status"] == "ready_for_simulation"
