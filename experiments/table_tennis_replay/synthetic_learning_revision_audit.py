"""Audit a SIMULATED TT policy against recorded, pinned result revisions.

This read-only research check is not an execution, training, promotion, or
production enforcement authority. It cannot discover omitted source receipts,
unknown aliases, untrustworthy clocks, or independently establish identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
from pathlib import Path

from autosport.learning_environment import EvidenceTruth, Observation, Outcome
from autosport.transparent_bandit_policy import BanditPolicyState, PolicyUpdateEvidence
from causal_h2h_observer import verify_journal
from causal_strength_revision_history import evaluate_history
from record import instant

SIMULATION = "tt-revision-synthetic-v1"
_FIELDS = {"MARKET": "market_home_probability", "FORM": "strength_home_probability",
           "FORM_H2H": "strength_h2h_home_probability"}


@dataclass(frozen=True)
class SimulatedBinding:
    """In-memory witness: no claim that a source ID is a physical event."""

    fixture_id: str
    observation: Observation
    outcome: Outcome
    update: PolicyUpdateEvidence


def _metadata(**values):
    return tuple(sorted(values.items()))


def _forecast_candidates(forecast):
    result = {}
    for action, field in _FIELDS.items():
        value = forecast.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("frozen probability must be decimal text")
        probability = Decimal(value)
        if not probability.is_finite() or not 0 < probability < 1:
            raise ValueError("frozen probability outside open interval")
        result[action] = value
    return result


def _snapshot_before(snapshots, cutoff):
    visible = [snapshot for snapshot in snapshots if instant(snapshot["as_of"]) < cutoff]
    if not visible:
        raise ValueError("no result receipt strictly before cutoff")
    return visible[-1]


def _record(snapshot, fixture_id):
    matches = [item for item in snapshot["records"] if item["fixture_id"] == fixture_id]
    if len(matches) != 1:
        raise ValueError("fixture missing or duplicated in revision history")
    return matches[0]


def audit(root, history, expected_head, *, expected_forecast_sha256,
          expected_forecast_receipt_sha256, policy, bindings, decision_at):
    """Read verified history and replay all simulated updates before decision.

    A caller must separately retain the correct history and forecast pins at
    the time; hashes cannot prove freshness, completeness, or an honest clock.
    This function does not intercept direct calls to ``policy.choose``.
    """
    if type(policy) is not BanditPolicyState or type(bindings) not in (tuple, list):
        raise ValueError("policy and complete binding list are required")
    if any(type(binding) is not SimulatedBinding for binding in bindings):
        raise ValueError("invalid simulated binding")
    cutoff = instant(decision_at)
    report = evaluate_history(root, history, expected_head,
                              expected_forecast_sha256=expected_forecast_sha256,
                              expected_forecast_receipt_sha256=expected_forecast_receipt_sha256)
    journal, envelope, verified = verify_journal(Path(root))
    receipt_bytes = (Path(root) / "forecast.receipt.json").read_bytes()
    if (envelope["sha256"] != expected_forecast_sha256 or
            hashlib.sha256(receipt_bytes).hexdigest() != expected_forecast_receipt_sha256):
        raise ValueError("forecast or receipt changed between verification reads")
    snapshots = report["snapshots"]
    current = _snapshot_before(snapshots, cutoff)
    forecasts = {row["fixture_id"]: row for row in journal["forecasts"]}
    if len(forecasts) != len(journal["forecasts"]):
        raise ValueError("duplicate frozen fixture")
    try:
        updates = sorted(bindings, key=lambda binding: binding.update.update_index)
    except AttributeError as exc:
        raise ValueError("binding lacks canonical policy update") from exc
    if len(updates) != policy.generation:
        raise ValueError("binding ledger does not cover the policy generation")
    fixture_ids = [binding.fixture_id for binding in updates]
    if (any(type(fid) is not str or not fid or fid != fid.strip() for fid in fixture_ids)
            or len(set(fixture_ids)) != len(fixture_ids)):
        raise ValueError("missing or duplicate exact fixture binding")

    action_types = frozenset(item.action_type for item in policy.estimates)
    replayed = BanditPolicyState.initial(environment_id=policy.environment_id,
                                          protocol_id=policy.protocol_id,
                                          config_sha256=policy.config_sha256,
                                          seed=policy.seed, action_types=action_types)
    already_learned = []
    previous_resolution = None
    past_states = []
    for index, binding in enumerate(updates, 1):
        update, observation, outcome = binding.update, binding.observation, binding.outcome
        if (type(update) is not PolicyUpdateEvidence or type(observation) is not Observation
                or type(outcome) is not Outcome or update.update_index != index
                or update.action is None or update.reward is None or update.transition is None):
            raise ValueError("missing or out-of-order update witness")
        action, reward, transition = update.action, update.reward, update.transition
        if (outcome.truth is not EvidenceTruth.SIMULATED or reward.truth is not EvidenceTruth.SIMULATED
                or outcome.simulation_model_id != SIMULATION or reward.simulation_model_id != SIMULATION):
            raise ValueError("only explicitly simulated evidence is supported")
        if (outcome.environment_id != policy.environment_id or observation.environment_id != policy.environment_id
                or outcome.action_id != action.action_id or outcome.outcome_id != reward.outcome_id
                or action.observation_id != observation.observation_id):
            raise ValueError("outcome or observation is not bound to the policy action")
        fixture = binding.fixture_id
        forecast = forecasts.get(fixture)
        if forecast is None:
            raise ValueError("binding fixture absent from frozen forecast")
        candidates = _forecast_candidates(forecast)
        if not candidates or set(candidates) - action_types:
            raise ValueError("policy does not cover available frozen candidates")
        decision = instant(action.decided_at)
        decision_policy = next((state for when, state in reversed(past_states) if when < decision), None)
        if decision_policy is None:
            decision_policy = BanditPolicyState.initial(
                environment_id=policy.environment_id, protocol_id=policy.protocol_id,
                config_sha256=policy.config_sha256, seed=policy.seed, action_types=action_types)
        if action.action_type != decision_policy.choose(admissible_actions=frozenset(candidates)):
            raise ValueError("action differs from the predecessor policy choice")
        observed = _metadata(fixture_id=fixture, forecast_sha256=expected_forecast_sha256,
                             **{"prob_" + key: value for key, value in candidates.items()})
        parameters = _metadata(fixture_id=fixture, forecast_sha256=expected_forecast_sha256)
        if observation.evidence != observed or action.parameters != parameters:
            raise ValueError("action or observation does not bind frozen candidates")
        resolved = instant(transition.resolved_at)
        next_request = instant(verified[(1, "results")]["requested_at"])
        if (not instant(envelope["frozen_at"]) <= decision < next_request
                or not instant(observation.observed_at) <= instant(observation.available_at) <= decision
                or not decision < resolved < cutoff
                or (previous_resolution is not None and not previous_resolution < resolved)):
            raise ValueError("policy action, result or audit chronology is invalid")
        # Only the latest response already known at this update can be used.
        prior = _snapshot_before(snapshots, resolved)
        if reward.evidence != _metadata(history_link_sha256=prior["link_sha256"]):
            raise ValueError("reward does not bind the latest visible history link")
        if any(_record(prior, fid)["state"] != "eligible" for fid in already_learned):
            raise ValueError("policy updated after an earlier used result was quarantined")
        record = _record(prior, fixture)
        matching = [row for row in prior["rows"] if row["fixture_id"] == fixture]
        if record["state"] != "eligible" or len(matching) != 1:
            raise ValueError("training result was not eligible at update time")
        winner = matching[0]["outcome"]
        if (type(winner) is not int or winner not in (0, 1)
                or outcome.evidence != _metadata(winner_home=str(winner))
                or instant(outcome.revealed_at) != instant(prior["as_of"])
                or instant(reward.available_at) != instant(prior["as_of"])):
            raise ValueError("simulated outcome does not match the visible result receipt")
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            probability = Decimal(candidates[action.action_type])
            expected_reward = -((probability - winner) ** 2)
        if reward.reward != expected_reward:
            raise ValueError("simulated reward differs from frozen-probability Brier")
        successor, replayed_update = replayed.update(action=action, reward=reward, transition=transition)
        if replayed_update != update:
            raise ValueError("policy update witness differs from canonical replay")
        replayed = successor
        previous_resolution = resolved
        past_states.append((resolved, successor))
        already_learned.append(fixture)
    if replayed != policy:
        raise ValueError("replayed policy differs from supplied state")

    quarantined = sum(_record(current, fixture)["state"] == "quarantined" for fixture in fixture_ids)
    if any(_record(current, fixture)["state"] not in ("eligible", "quarantined") for fixture in fixture_ids):
        raise ValueError("used result has inconsistent current revision status")
    return {"schema": "research.tt_simulated_learning_revision_audit.v1",
            "status": "held_for_review" if quarantined else "ready_for_simulation",
            "policy_generation": policy.generation, "verified_bindings": len(updates),
            "used_results_quarantined": quarantined,
            "visible_receipt_count": sum(instant(s["as_of"]) < cutoff for s in snapshots),
            "forecast_sha256": expected_forecast_sha256,
            "only_simulated_evidence": True, "policy_modified": False,
            "source_identity_verified": False, "source_rights_verified": False,
            "pin_freshness_verified": False, "source_completeness_verified": False,
            "production_enforcement": False, "model_training_authorized": False,
            "real_money_execution": False, "profitability_claim": False}
