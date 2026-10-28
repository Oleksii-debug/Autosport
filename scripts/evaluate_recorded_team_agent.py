"""Frozen and delayed-online team learners on identical recorded matches.

Opening market prices are a prior. Past revealed team results train bounded
residual corrections. Neither closing prices nor current results are inputs.
Retrospective timing is assumed, so this does not qualify executable profit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from autosport.probability import implied_probability, paper_value
from autosport.team_residual_model import OUTCOMES, TeamMarketInput, TeamResidualConfig, TeamResidualModel
from benchmark_recorded_match_returns import parse_rows, summarize


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_config(config):
    if type(config) is not dict or set(config) != {
        "split_date", "outcome_lag_hours", "cost_bps_per_stake", "minimum_net_return", "maximum_odds", "model"
    }:
        raise ValueError("unexpected team experiment configuration")
    boundary = datetime.strptime(config["split_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for key, low, high in (("outcome_lag_hours", 24, 720), ("cost_bps_per_stake", 0, 10000)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"invalid {key}")
    for key, low, high in (("minimum_net_return", 0, 1), ("maximum_odds", 1, 1000)):
        if type(config[key]) is not str or len(config[key]) > 32:
            raise ValueError(f"invalid {key}")
        value = Decimal(config[key])
        if not value.is_finite() or not low < value <= high:
            raise ValueError(f"invalid {key}")
    return boundary, TeamResidualConfig(**config["model"])


def observation(row):
    context = row["team_context"]
    if any(type(v) is not str or not v.strip() or v != v.strip() for v in context.values()):
        raise ValueError("missing or noncanonical team context")
    with localcontext(Context(prec=28)):
        raw = [implied_probability(row["odds"][key]) for key in OUTCOMES]
        total = sum(raw)
        home, draw = raw[0] / total, raw[1] / total
        probabilities = (home, draw, Decimal(1) - home - draw)
    # Country namespaces display-name identities. Provider entity resolution and
    # renamed clubs remain limitations, explicitly not claimed solved here.
    fields = {"id": row["id"], "kickoff": row["kickoff"].isoformat(),
              "team_context": context, "odds": {k: str(v) for k, v in row["odds"].items()}}
    return TeamMarketInput(row["id"], digest([context["country"], context["home"]]),
                           digest([context["country"], context["away"]]),
                           row["kickoff"].isoformat(), probabilities, digest(fields))


def select(probabilities, odds, supported, config):
    if not supported:
        return None
    fee = Decimal(config["cost_bps_per_stake"]) / 10000
    candidates = [(paper_value(key, p, odds[key]).expected_profit_per_unit - fee, key)
                  for key, p in zip(OUTCOMES, probabilities, strict=True)
                  if odds[key] <= Decimal(config["maximum_odds"])]
    if not candidates:
        return None
    value, pick = min(candidates, key=lambda pair: (-pair[0], pair[1]))
    return pick if value >= Decimal(config["minimum_net_return"]) else None


def evaluate(path: Path, config: dict) -> dict:
    validate_config(config)
    rows, source = parse_rows(path, include_team_context=True)
    return evaluate_rows(rows, source, config)


def evaluate_rows(rows, source, config: dict) -> dict:
    """Evaluate already-normalized research rows; source metadata grants no rights."""
    boundary, model_config = validate_config(config)
    admitted, rejected_context = [], 0
    for row in rows:
        try:
            item = observation(row)
        except ValueError:
            rejected_context += 1
            continue
        admitted.append((row, item))
    lag = timedelta(hours=config["outcome_lag_hours"])
    validation = [(r, i) for r, i in admitted if r["kickoff"] >= boundary]
    training = [(r, i) for r, i in admitted if r["kickoff"] + lag < boundary]
    if not training or not validation:
        raise ValueError("both causally separated training and validation cohorts required")
    frozen = TeamResidualModel(model_config, created_at=(admitted[0][0]["kickoff"] - timedelta(seconds=1)).isoformat())
    for row, item in training:
        frozen.learn(item, result=row["result"], available_at=(row["kickoff"] + lag).isoformat())
    checkpoint = frozen.payload()
    online = TeamResidualModel.restore(checkpoint)
    variants = ("market", "frozen_team", "online_team")
    bets = {key: [] for key in variants}
    brier = {key: Decimal(0) for key in variants}
    log_loss = {key: Decimal(0) for key in variants}
    supported_count = {key: 0 for key in variants}
    decisions = []
    released = len(training)
    for row, item in validation:
        while released < len(admitted) and admitted[released][0]["kickoff"] + lag < row["kickoff"]:
            known, known_input = admitted[released]
            online.learn(known_input, result=known["result"], available_at=(known["kickoff"] + lag).isoformat())
            released += 1
        probabilities = {"market": item.market_probabilities,
                         "frozen_team": frozen.predict(item), "online_team": online.predict(item)}
        supported = {"market": True, "frozen_team": frozen.supported(item), "online_team": online.supported(item)}
        choices = {key: select(probabilities[key], row["odds"], supported[key], config) for key in variants}
        decisions.append({"id": row["id"], "decision_at": item.available_at,
                          "online_training_cutoff": online.training_cutoff,
                          "variants": {key: {"selection": choices[key] or "WAIT", "supported": supported[key],
                                             "probabilities": [str(p) for p in probabilities[key]]}
                                       for key in variants}})
        # The current outcome is consumed only after every variant has chosen.
        for key in variants:
            supported_count[key] += supported[key]
            pick = choices[key]
            if pick is not None:
                reward = row["odds"][pick] - 1 if pick == row["result"] else Decimal(-1)
                bets[key].append({"kickoff": row["kickoff"], "rewards": {key: reward}})
            for outcome, probability in zip(OUTCOMES, probabilities[key], strict=True):
                brier[key] += (probability - int(outcome == row["result"])) ** 2
                if outcome == row["result"]:
                    log_loss[key] -= probability.ln()
    assert frozen.payload() == checkpoint
    metrics = {key: {**summarize(bets[key], key, config["cost_bps_per_stake"]),
                     "wait_count": len(validation) - len(bets[key]),
                     "supported_matches": supported_count[key],
                     "brier_mean_per_outcome": str(brier[key] / (3 * len(validation))),
                     "log_loss_mean": str(log_loss[key] / len(validation))} for key in variants}
    assessments = {}
    for key in ("frozen_team", "online_team"):
        reasons = []
        if brier[key] >= brier["market"] or log_loss[key] >= log_loss["market"]:
            reasons.append("forecast scores do not both improve on the same-cohort market baseline")
        if Decimal(metrics[key]["net_units"]) <= 0:
            reasons.append("no positive chosen-action return after configured costs")
        assessments[key] = {
            "research_verdict": "REJECTED_ON_THIS_SAMPLE" if reasons else "REQUIRES_INDEPENDENT_FORWARD_EVIDENCE",
            "reasons": reasons, "promotion_authorized": False,
        }
    return {
        "schema": "autosport.recorded_team_agent_experiment", "schema_version": 1,
        "source": {"attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0",
                   **source, "rejected_team_context": rejected_context},
        "config": config, "config_sha256": digest(config),
        "training_matches": len(training), "validation_matches": len(validation),
        "training_checkpoint": checkpoint, "online_final_checkpoint": online.payload(),
        "validation": metrics, "candidate_assessments": assessments,
        "canonical_forecast_examples": [r.to_dict() for r in online.forecasts(validation[-1][1])],
        "decisions": decisions,
        "evaluation_mode": "exploratory retrospective; frozen model and delayed online model reported separately",
        "settlement_basis": "provider-recorded final results; hypothetical fixed one-unit single stakes",
        "quote_timing_verified": False, "outcome_availability_verified": False,
        "forward_evidence_eligible": False, "profitability_claim": False,
        "promotion_authorized": False, "real_money_execution": False,
        "limitations": ["48-hour/default configured outcome lag is assumed, not provider availability proof.",
                        "Opening prices may be backfilled; liquidity, limits and capital concurrency are unverified.",
                        "Country/name team identity can split renamed clubs or conflate namesakes.",
                        "No complete team fixture history: selected-source sampling and sparse support remain.",
                        "Previous validation experiments were inspected; this is not pristine holdout evidence.",
                        "Uncertainty is uncalibrated; model/checkpoint/forecast objects authorize no deployment."],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with localcontext(Context(prec=28)):
        report = evaluate(args.csv, strict_json_loads(args.config.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in ("config_sha256", "training_matches", "validation_matches", "validation")}))
