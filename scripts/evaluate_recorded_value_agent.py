"""One frozen retrospective value-agent experiment; no live promotion authority.

The provider's actual recorded results train a probability calibration. Opening
prices select one hypothetical single bet only when the calibrated expected
return clears the frozen threshold. No validation outcome updates this model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from autosport.probability import implied_probability, paper_value
from benchmark_recorded_match_returns import parse_rows, summarize


def validate_config(config):
    if set(config) != {"split_date", "bins", "prior_observations",
                       "minimum_bin_observations", "minimum_expected_return", "maximum_odds"}:
        raise ValueError("unexpected experiment configuration")
    datetime.strptime(config["split_date"], "%Y-%m-%d")
    for key, low, high in (("bins", 2, 100), ("prior_observations", 1, 100000),
                           ("minimum_bin_observations", 1, 100000)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"invalid {key}")
    for key, low, high in (("minimum_expected_return", Decimal(0), Decimal(1)),
                           ("maximum_odds", Decimal(1), Decimal(1000))):
        if type(config[key]) is not str:
            raise ValueError(f"{key} requires decimal string")
        value = Decimal(config[key])
        if not value.is_finite() or not low < value <= high:
            raise ValueError(f"invalid {key}")


def fair_probabilities(odds):
    raw = {key: implied_probability(value) for key, value in odds.items()}
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items()}


def bin_index(probability, bins):
    return min(bins - 1, int(probability * bins))


def fit(training, config):
    bins = [{"observations": 0, "wins": 0, "expected_wins": Decimal(0)}
            for _ in range(config["bins"])]
    for row in training:
        for pick, probability in fair_probabilities(row["odds"]).items():
            item = bins[bin_index(probability, len(bins))]
            item["observations"] += 1
            item["wins"] += pick == row["result"]
            item["expected_wins"] += probability
    return bins


def forecast(odds, bins, config):
    base = fair_probabilities(odds)
    adjusted = {}
    supported = True
    for pick, probability in base.items():
        item = bins[bin_index(probability, len(bins))]
        supported &= item["observations"] >= config["minimum_bin_observations"]
        residual = (item["wins"] - item["expected_wins"]) / (
            item["observations"] + config["prior_observations"])
        adjusted[pick] = min(Decimal("0.999999"), max(Decimal("0.000001"), probability + residual))
    total = sum(adjusted.values())
    return base, {key: value / total for key, value in adjusted.items()}, supported


def select(odds, probabilities, supported, config):
    if not supported:
        return None
    candidates = [(paper_value(key, probabilities[key], price).expected_profit_per_unit, key)
                  for key, price in odds.items() if price <= Decimal(config["maximum_odds"])]
    if not candidates:
        return None
    value, pick = min(candidates, key=lambda item: (-item[0], item[1]))
    return pick if value >= Decimal(config["minimum_expected_return"]) else None


def evaluate(path: Path, config: dict):
    validate_config(config)
    rows, source = parse_rows(path)
    boundary = datetime.fromisoformat(config["split_date"]).replace(tzinfo=timezone.utc)
    training = [row for row in rows if row["kickoff"] < boundary]
    validation = [row for row in rows if row["kickoff"] >= boundary]
    if not training or not validation:
        raise ValueError("both chronological cohorts required")
    bins = fit(training, config)
    selected, decisions = [], []
    base_brier = adjusted_brier = Decimal(0)
    for row in validation:
        base, probabilities, supported = forecast(row["odds"], bins, config)
        # Deliberately choose before reading this match's result.
        pick = select(row["odds"], probabilities, supported, config)
        decisions.append({"id": row["id"], "selection": pick or "WAIT",
                          "supported": supported,
                          "probabilities": {key: str(value) for key, value in probabilities.items()}})
        if pick is not None:
            reward = row["odds"][pick] - 1 if pick == row["result"] else Decimal(-1)
            selected.append({"kickoff": row["kickoff"], "rewards": {"VALUE_AGENT": reward}})
        for key in base:
            outcome = int(key == row["result"])
            base_brier += (base[key] - outcome) ** 2
            adjusted_brier += (probabilities[key] - outcome) ** 2
    report = {
        "schema": "autosport.recorded_value_agent_experiment", "schema_version": 1,
        "source": {**source, "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0"},
        "config": config,
        "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "training_matches": len(training), "validation_matches": len(validation),
        "model": [{**item, "expected_wins": str(item["expected_wins"])} for item in bins],
        "validation": {"zero_cost": summarize(selected, "VALUE_AGENT"),
                       "one_percent_stake_cost": summarize(selected, "VALUE_AGENT", 100),
                       "wait_count": len(validation) - len(selected),
                       "coverage": str(Decimal(len(selected)) / len(validation)),
                       "market_brier_mean_per_outcome": str(base_brier / (3 * len(validation))),
                       "calibrated_brier_mean_per_outcome": str(adjusted_brier / (3 * len(validation)))},
        "decisions": decisions,
        "evaluation_mode": "exploratory retrospective; baseline validation aggregates already inspected",
        "settlement_basis": "provider-recorded match result, hypothetical fixed one-unit single stakes",
        "quote_timing_verified": False, "outcome_availability_verified": False,
        "forward_evidence_eligible": False, "profitability_claim": False,
        "promotion_authorized": False, "real_money_execution": False,
        "limitations": ["Opening prices may be backfilled; no executable availability proof.",
                        "Final results are provider records, not independently verified event-time settlement.",
                        "Three outcomes per match are correlated, not three independent matches.",
                        "Parameters were fixed for this run; this is not a pristine prospective holdout.",
                        "Costs are hypothetical; liquidity, limits and concurrent capital are not modelled.",
                        "Source coverage may be selected and differ from the agent's intended markets."],
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.csv, json.loads(args.config.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"config_sha256": report["config_sha256"], "validation": report["validation"]}))
