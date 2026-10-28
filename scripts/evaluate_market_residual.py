"""Fixed chronological team-residual challenger over recorded match outcomes."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import heapq
import json
from pathlib import Path

from autosport.probability import ForecastObservation, brier_score
from autosport.prospective_forecast_learning import RESULTS, SIDES
from benchmark_recorded_match_returns import summarize
from evaluate_recorded_cross_book_value import pick_quote


def team_keys(row):
    values = [row.get(key, "").strip() for key in ("country", "league", "home", "away")]
    if not all(values) or values[2] == values[3]:
        return None
    country, league, home, away = values
    return (country, league, home), (country, league, away)


class MarketResidual:
    def __init__(self):
        self.ratings = defaultdict(Decimal)
        self.counts = Counter()

    def predict(self, row, baseline):
        keys = team_keys(row)
        if keys is None or min(self.counts[key] for key in keys) < 5:
            return baseline.copy()
        home, away = keys
        shift = (self.ratings[home] - self.ratings[away]) / 4000
        shift = min(Decimal(".05"), max(Decimal("-.05"), shift))
        floor = min(Decimal(".001"), baseline["home"] / 2, baseline["away"] / 2)
        shift = min(baseline["away"] - floor,
                    max(floor - baseline["home"], shift))
        return {"home": baseline["home"] + shift, "draw": baseline["draw"], "away": baseline["away"] - shift}

    def update(self, row, baseline, result):
        keys = team_keys(row)
        if keys is None:
            return
        home, away = keys
        observed = {"1": Decimal(1), "X": Decimal(".5"), "2": Decimal(0)}[result]
        expected = baseline["home"] + baseline["draw"] / 2
        change = Decimal(24) * (observed - expected)
        self.ratings[home] += change
        self.ratings[away] -= change
        self.counts[home] += 1
        self.counts[away] += 1


def evaluate(path, config):
    raw = Path(path).read_bytes()
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    parsed, seen, exclusions = [], set(), Counter()
    for row in rows:
        identity = row.get("id")
        if not identity or identity in seen:
            raise ValueError("duplicate or missing event identity")
        seen.add(identity)
        try:
            kickoff = datetime.fromisoformat(row["kickoff_utc"])
            if kickoff.tzinfo is None:
                raise ValueError("naive kickoff")
            p = {side: Decimal(row[f"fair_prob_{side}"]) for side in SIDES}
            if any(not x.is_finite() or not 0 < x < 1 for x in p.values()) or abs(sum(p.values()) - 1) > Decimal(".001"):
                raise ValueError("invalid probability")
            total = sum(p.values())
            p = {side: value / total for side, value in p.items()}
        except (KeyError, ValueError, InvalidOperation):
            exclusions["invalid_probability_or_kickoff"] += 1
            continue
        parsed.append((kickoff.astimezone(timezone.utc), identity, row, p))
    parsed.sort(key=lambda x: (x[0], x[1]))
    start = datetime.fromisoformat(config["earliest_quote_date"]).replace(tzinfo=timezone.utc)
    model, pending = MarketResidual(), []
    daily = {name: Counter() for name in ("baseline", "team_residual")}
    selected, scored = {name: [] for name in daily}, {name: [] for name in daily}
    journal = []
    for index, (kickoff, identity, row, p) in enumerate(parsed):
        while pending and pending[0][0] <= kickoff:
            _, _, old_row, old_p, old_result = heapq.heappop(pending)
            model.update(old_row, old_p, old_result)
        challenger = model.predict(row, p)
        forecasts = {"baseline": p, "team_residual": challenger}
        if kickoff >= start:
            keys = team_keys(row)
            entry = {"id": identity, "kickoff": kickoff.isoformat(), "result": row.get("result"),
                     "void_reason": row.get("void_reason"), "probabilities": {name: {s: str(v[s]) for s in SIDES} for name, v in forecasts.items()},
                     "team_support": [model.counts[key] for key in keys] if keys else None, "decisions": {}}
            for name, forecast in forecasts.items():
                if row.get("result") in RESULTS and not row.get("void_reason"):
                    scored[name].append(brier_score([ForecastObservation(float(forecast[side]),
                                            int(RESULTS[row["result"]] == side)) for side in SIDES]))
                inputs = {**row, **{f"fair_prob_{side}": str(forecast[side]) for side in SIDES}}
                try:
                    choice = pick_quote(inputs, config)
                except (KeyError, ValueError, InvalidOperation):
                    choice = None
                    exclusions[name + "_invalid_quote_inputs"] += 1
                day = kickoff.date().isoformat()
                if choice is None or daily[name][day] >= config["maximum_candidates_per_kickoff_day"]:
                    entry["decisions"][name] = None
                    continue
                daily[name][day] += 1
                value, pick, odds, book = choice
                decision = {"selection": pick, "quote": str(odds), "bookmaker": book,
                            "model_value": str(value), "state": "PENDING", "reward": None}
                if row.get("void_reason"):
                    decision.update(state="VOID", reward="0")
                elif row.get("result") in RESULTS:
                    reward = odds - 1 if row["result"] == pick else Decimal(-1)
                    decision.update(state="SETTLED", reward=str(reward))
                    selected[name].append({"kickoff": kickoff, "rewards": {"CHOSEN": reward}})
                entry["decisions"][name] = decision
            journal.append(entry)
        if row.get("result") in RESULTS and not row.get("void_reason"):
            heapq.heappush(pending, (kickoff + timedelta(hours=48), index, row, p, row["result"]))
    overlap = Counter()
    for item in journal:
        a, b = item["decisions"]["baseline"], item["decisions"]["team_residual"]
        if a and b:
            overlap["same_selection" if a["selection"] == b["selection"] else "different_selection"] += 1
        elif a:
            overlap["baseline_only"] += 1
        elif b:
            overlap["team_residual_only"] += 1
    return {"schema": "autosport.market_residual_economics.v1",
            "source_sha256": hashlib.sha256(raw).hexdigest(), "config": config,
            "source_rows": len(rows), "valid_rows": len(parsed), "exclusions": dict(exclusions),
            "decisions": journal, "selection_overlap": dict(overlap),
            "variants": {name: {"zero_cost": summarize(selected[name], "CHOSEN", 0),
                                "one_percent_cost": summarize(selected[name], "CHOSEN", 100),
                                "brier": sum(scored[name])/len(scored[name]) if scored[name] else None,
                                "scored_matches": len(scored[name]),
                                "candidates_including_void_and_pending": sum(bool(item["decisions"][name]) for item in journal)}
                         for name in daily},
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0",
            "evaluation_mode": "exploratory retrospective, assumed 48h label delay, not untouched holdout",
            "quote_timing_verified": False, "quote_acceptance_verified": False,
            "profitability_claim": False, "promotion_authorized": False, "real_money_execution": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.csv, json.loads(args.config.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"variants": result["variants"], "selection_overlap": result["selection_overlap"]}))
