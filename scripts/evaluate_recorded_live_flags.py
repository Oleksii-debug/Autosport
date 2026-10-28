"""Evaluate pre-result source-committed live flags and conditional later re-quotes."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from benchmark_recorded_match_returns import summarize


OUTCOMES = {"1": "home", "X": "draw", "2": "away"}
CUTOFF = datetime(2026, 9, 30, tzinfo=timezone.utc)


def instant(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timezone required")
    return stamp.astimezone(timezone.utc)


def read_candidate(row):
    if row.get("value_origin") != "live" or row.get("value_sealed_before_kickoff") != "1":
        raise ValueError("not_source_sealed_live")
    recorded, kickoff = instant(row["value_recorded_at_utc"]), instant(row["kickoff_utc"])
    if recorded < CUTOFF or recorded >= kickoff:
        raise ValueError("outside_live_v2_or_late")
    selection, book = row["value_selection"], row["value_bookmaker"].strip()
    if selection not in OUTCOMES or not book or any(word in book.casefold() for word in ("exchange", "matchbook")):
        raise ValueError("unsupported_selection_or_book")
    preimage = row["value_preimage"]
    if hashlib.sha256(preimage.encode("utf-8")).hexdigest() != row["value_commitment"]:
        raise ValueError("commitment_mismatch")
    parts = preimage.split("|")
    if len(parts) != 7 or not parts[0] or not parts[6]:
        raise ValueError("invalid_preimage")
    odds, fair = Decimal(parts[2]), Decimal(parts[4])
    if not odds.is_finite() or not fair.is_finite() or odds <= 1 or fair <= 1:
        raise ValueError("invalid_odds")
    if parts[1] != OUTCOMES[selection] or parts[3] != book or odds != Decimal(row["value_odds_taken"]) or instant(parts[5]) != recorded:
        raise ValueError("preimage_field_mismatch")
    probability, gap = Decimal(1)/fair, odds/fair - 1
    if probability < Decimal(".30") or not Decimal(".005") <= gap <= Decimal(".12"):
        raise ValueError("outside_provider_v2_rule")
    return {"id": row["id"], "kickoff": kickoff, "recorded": recorded, "selection": selection,
            "bookmaker": book, "odds": odds, "fair": fair, "probability": probability, "gap": gap,
            "row": row}


def score(item, odds):
    row = item["row"]
    if row.get("settled") != "1" or row.get("result") not in OUTCOMES:
        return "PENDING", None
    won = row["result"] == item["selection"]
    reward = item["odds"] - 1 if won else Decimal(-1)
    try:
        if row.get("value_won") not in {"0", "1"} or int(row["value_won"]) != int(won) or Decimal(row["value_profit_units"]) != reward:
            return "CONFLICT", None
    except (InvalidOperation, ValueError):
        return "CONFLICT", None
    return "SETTLED", odds - 1 if won else Decimal(-1)


def report(rows):
    return {str(cost): summarize(rows, "CHOSEN", cost) for cost in (0, 50, 100, 200)}


def evaluate(path):
    raw = Path(path).read_bytes()
    source = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    seen, candidates, excluded = set(), [], Counter()
    for row in source:
        identity = row.get("id")
        if not identity or identity in seen:
            raise ValueError("duplicate or missing identity")
        seen.add(identity)
        try:
            candidates.append(read_candidate(row))
        except (KeyError, ValueError, InvalidOperation, ArithmeticError) as exc:
            reason = str(exc) if str(exc) in {"not_source_sealed_live", "outside_live_v2_or_late", "unsupported_selection_or_book", "commitment_mismatch", "invalid_preimage", "invalid_odds", "preimage_field_mismatch", "outside_provider_v2_rule"} else "invalid_source_fields"
            excluded[reason] += 1
    candidates.sort(key=lambda item: (item["recorded"], item["id"]))
    by_day, decisions = Counter(), []
    for item in candidates:
        day = item["kickoff"].date().isoformat()
        current_eligible = item["odds"] <= Decimal("3.5") and item["gap"] >= Decimal(".05")
        current_selected = current_eligible and by_day[day] < 5
        if current_selected:
            by_day[day] += 1
        state, reward = score(item, item["odds"])
        later = item["row"].get("value_realizable_odds", "").strip()
        requote, later_reward = None, None
        if later:
            try:
                parsed = Decimal(later)
                if parsed.is_finite() and parsed > 1:
                    requote = parsed
                    _, later_reward = score(item, parsed)
                else:
                    excluded["invalid_requote"] += 1
            except InvalidOperation:
                excluded["invalid_requote"] += 1
        decisions.append({"id": item["id"], "recorded_at": item["recorded"].isoformat(),
                          "kickoff": item["kickoff"].isoformat(), "selection": item["selection"],
                          "odds": str(item["odds"]), "fair_odds": str(item["fair"]),
                          "price_gap": str(item["gap"]), "bookmaker": item["bookmaker"],
                          "current_rule_selected": current_selected, "state": state,
                          "reward": None if reward is None else str(reward),
                          "later_requote": None if requote is None else str(requote),
                          "later_reward": None if later_reward is None else str(later_reward)})
    def records(subset, field):
        result = []
        for row in subset:
            if row["state"] == "SETTLED" and row[field] is not None:
                result.append({"kickoff": instant(row["kickoff"]), "rewards": {"CHOSEN": Decimal(row[field])}})
        return sorted(result, key=lambda row: row["kickoff"])
    current = [row for row in decisions if row["current_rule_selected"]]
    conditional = [row for row in decisions if row["state"] == "SETTLED" and row["later_reward"] is not None]
    states = {name: dict(Counter(row["state"] for row in subset)) for name, subset in (("provider_flags", decisions), ("current_rule", current))}
    return {"schema": "autosport.recorded_live_flags.v1", "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_rows": len(source), "exclusions": dict(excluded), "decisions": decisions,
            "cohorts": {"provider_flags": {"candidates": len(decisions), "states": states["provider_flags"], "cost_scenarios": report(records(decisions, "reward"))},
                        "current_rule": {"candidates": len(current), "states": states["current_rule"], "cost_scenarios": report(records(current, "reward"))},
                        "conditional_later_requote": {"matched_settled": len(conditional), "initial_prices": report(records(conditional, "reward")),
                                                        "later_prices": report(records(conditional, "later_reward"))}},
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0",
            "evaluation_mode": "exploratory source-recorded flags; not untouched holdout",
            "source_seal_independently_timestamped": False, "quote_acceptance_verified": False,
            "requote_subgroup_future_conditioned": True, "profitability_claim": False,
            "promotion_authorized": False, "real_money_execution": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.csv)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"source_rows": result["source_rows"], "exclusions": result["exclusions"], "cohorts": result["cohorts"]}))
