"""Fixed cross-book price-gap rule on recorded outcomes, not executable proof.

Only post-2026-09-21 soft prices are eligible because the provider documents a
different earlier price definition. Third-party fair probabilities are a market
reference, not demonstrated calibrated Autosport probabilities.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from autosport.probability import paper_value
from autosport.shadow_strategy_memory import digest
from benchmark_recorded_match_returns import summarize


def pick_quote(item, config):
    candidates = []
    probabilities = [Decimal(item[f"fair_prob_{side}"]) for side in ("home", "draw", "away")]
    if any(not value.is_finite() or not 0 < value < 1 for value in probabilities) or abs(sum(probabilities) - 1) > Decimal("0.001"):
        raise ValueError("invalid reference probabilities")
    for pick, side, probability in zip(("1", "X", "2"), ("home", "draw", "away"), probabilities):
        book = item[f"best_soft_book_{side}"].strip()
        if not book or any(word in book.casefold() for word in ("exchange", "matchbook")):
            continue
        odds = Decimal(item[f"best_soft_{side}"])
        if not odds.is_finite() or not 1 < odds <= Decimal(config["maximum_odds"]) or probability < Decimal(config["minimum_probability"]):
            continue
        value = paper_value(pick, probability, odds).expected_profit_per_unit
        if Decimal(config["minimum_model_value"]) <= value <= Decimal(config["maximum_model_value"]):
            candidates.append((value, pick, odds, book))
    return min(candidates, key=lambda candidate: (-candidate[0], candidate[1])) if candidates else None


def evaluate(path: Path, config):
    boundary = datetime.fromisoformat(config["earliest_quote_date"]).replace(tzinfo=timezone.utc)
    if boundary < datetime(2026, 9, 21, tzinfo=timezone.utc):
        raise ValueError("earlier soft quotes have an incompatible availability definition")
    cap = config["maximum_candidates_per_kickoff_day"]
    if type(cap) is not int or not 1 <= cap <= 100:
        raise ValueError("invalid daily candidate cap")
    raw = path.read_bytes()
    items = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    seen, parsed, exclusions = set(), [], Counter()
    for item in items:
        if not item.get("id") or item["id"] in seen:
            raise ValueError("missing or duplicate match identity")
        seen.add(item["id"])
        kickoff = datetime.fromisoformat(item["kickoff_utc"])
        if kickoff.tzinfo is None:
            raise ValueError("kickoff timezone required")
        kickoff = kickoff.astimezone(timezone.utc)
        if kickoff < boundary:
            exclusions["before_reliable_soft_price_definition"] += 1
            continue
        # Choose using pre-match columns even for subsequently void/unsettled matches.
        try:
            choice = pick_quote(item, config)
        except (KeyError, InvalidOperation, ValueError):
            exclusions["invalid_or_missing_price_inputs"] += 1
            continue
        if choice is None:
            exclusions["outside_frozen_rule"] += 1
            continue
        parsed.append((kickoff, item["id"], choice, item))
    daily, journal, settled = Counter(), [], []
    pending = voids = 0
    for kickoff, identity, choice, item in sorted(parsed, key=lambda x: (x[0], x[1])):
        day = kickoff.date().isoformat()
        if daily[day] >= cap:
            exclusions["daily_cap"] += 1
            continue
        daily[day] += 1
        value, pick, odds, book = choice
        state, reward = "PENDING", None
        if item.get("void_reason"):
            state, reward = "VOID", Decimal(0)
            voids += 1
        elif item.get("result") in {"1", "X", "2"}:
            state = "SETTLED"
            reward = odds - 1 if item["result"] == pick else Decimal(-1)
        else:
            pending += 1
        journal.append({"id": identity, "kickoff": kickoff.isoformat(), "selection": pick,
                        "quote": str(odds), "bookmaker": book, "model_value": str(value),
                        "state": state, "hypothetical_reward": None if reward is None else str(reward)})
        if state == "SETTLED":
            settled.append({"kickoff": kickoff, "rewards": {"PRICE_GAP": reward}})
    return {"schema": "autosport.recorded_cross_book_value", "config": config,
            "config_sha256": digest(config), "source_sha256": hashlib.sha256(raw).hexdigest(),
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0",
            "source_rows": len(items), "exclusions": dict(exclusions), "decisions": journal,
            "selected": len(journal), "voids": voids, "pending": pending,
            "zero_cost": summarize(settled, "PRICE_GAP"),
            "stress_cost": summarize(settled, "PRICE_GAP", config["stress_cost_bps_per_stake"]),
            "void_cost_treatment": "voids reported separately; no stake return or stress cost included in settled ROI",
            "evaluation_mode": "exploratory retrospective price-gap proxy; not pristine holdout",
            "quote_timing_verified": False, "soft_quote_timestamps_available": False,
            "quote_acceptance_verified": False, "capital_schedule_verified": False,
            "profitability_claim": False, "promotion_authorized": False,
            "real_money_execution": False,
            "limitations": ["Bookmaker prices may be unavailable or restricted to a particular user.",
                            "Provider-recorded price and probability timing is not independently verified.",
                            "Fair probabilities are market-derived third-party references, not proven edge.",
                            "This selects among historical soft quotes; the forward API exposes one provider-selected current gap.",
                            "Overlapping bankroll, limits, taxes and actual execution costs are not modelled."]}


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
    print(json.dumps({key: report[key] for key in ("selected", "pending", "voids", "zero_cost", "stress_cost", "exclusions")}))
