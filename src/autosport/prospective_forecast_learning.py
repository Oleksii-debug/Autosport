"""Public pre-result calibration experiment, separate from executed rewards.

Capture the full eligible forecast population, including declined/no-value events.
Persist prequential predictions before feedback; never backfill old snapshots.
"""
from __future__ import annotations

import csv
from decimal import Decimal
import io
import json
from pathlib import Path

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .probability import ForecastObservation, brier_score
from .prospective_shadow_observer import _retain_raw
from .shadow_strategy_memory import digest, instant


SIDES = ("home", "draw", "away")
RESULTS = {"1": "home", "X": "draw", "2": "away"}
PROTOCOL = {"version": 1, "bins": 10, "prior_weight": "100",
            "minimum_bin_observations": 100, "maximum_age_seconds": 5400,
            "result_delay_seconds": 7200}


def _probabilities(item, received_at):
    now = instant(received_at)
    if instant(item["kickoff_utc"]) <= now or item.get("fair_source") != "live":
        return None
    if not 0 <= (now - instant(item["fair_read_at"])).total_seconds() <= PROTOCOL["maximum_age_seconds"]:
        return None
    p = {side: Decimal(str(item["fair_probabilities"][side])) for side in SIDES}
    if any(not value.is_finite() or not 0 < value < 1 for value in p.values()):
        return None
    total = sum(p.values())
    if abs(total - 1) > Decimal("0.001"):
        return None
    return {side: p[side] / total for side in SIDES}


def _bin(p):
    return min(PROTOCOL["bins"] - 1, int(p * PROTOCOL["bins"]))


def _fit(events):
    bins = {side: [[0, Decimal(0), Decimal(0)] for _ in range(PROTOCOL["bins"])] for side in SIDES}
    for row in events.values():
        if "result" not in row or row.get("source_conflict"):
            continue
        for side in SIDES:
            p = Decimal(row["baseline"][side])
            cell = bins[side][_bin(p)]
            cell[0] += 1
            cell[1] += int(RESULTS[row["result"]] == side)
            cell[2] += p
    return bins


def _predict(p, bins):
    adjusted = {}
    for side in SIDES:
        n, wins, total = bins[side][_bin(p[side])]
        prior = Decimal(PROTOCOL["prior_weight"])
        adjusted[side] = ((wins + prior * total / n) / (n + prior)
                          if n >= PROTOCOL["minimum_bin_observations"] else p[side])
    total = sum(adjusted.values())
    return {side: str(adjusted[side] / total) for side in SIDES}


def _same_event(row, event):
    return (all(row.get(key) == event[key] for key in ("home", "away", "league"))
            and instant(row["kickoff_utc"]) == instant(event["kickoff_utc"]))


def forecast_learning_cycle(root: Path, *, predictions: bytes, results: bytes, received_at: str):
    now = instant(received_at)
    forecast = json.loads(predictions, parse_float=Decimal)
    if not 0 <= (now - instant(forecast["generated_at"])).total_seconds() <= PROTOCOL["maximum_age_seconds"]:
        raise ValueError("stale or future forecast response")
    items = forecast["predictions"]
    outcomes = list(csv.DictReader(io.StringIO(results.decode("utf-8-sig"))))
    if any(not row.get("id") for row in outcomes) or len({row["id"] for row in outcomes}) != len(outcomes):
        raise ValueError("duplicate or missing result identity")
    if len({str(item["id"]) for item in items}) != len(items):
        raise ValueError("duplicate forecast identity")
    by_id = {row["id"]: row for row in outcomes}
    path = root / "forecast-learning.json"
    with durable_path_lock(path):
        if path.exists():
            wrapper = strict_json_loads(path.read_text("utf-8"))
            state = wrapper["state"]
            if digest(state) != wrapper["sha256"] or state["protocol"] != PROTOCOL:
                raise ValueError("forecast learner checkpoint or protocol mismatch")
            if now < instant(state["last_received_at"]):
                raise ValueError("forecast receipt cannot go backwards")
        else:
            state = {"schema": "autosport.prospective_forecast_learning", "protocol": PROTOCOL,
                     "events": {}, "receipts": [], "promotion_authorized": False,
                     "real_money_execution": False, "profit_earned": None}
        forecast_sha = _retain_raw(root, predictions, ".json")
        result_sha = _retain_raw(root, results, ".csv")
        events = state["events"]
        # Quarantine revisions before constructing the model used for new forecasts.
        for identity, event in events.items():
            outcome = by_id.get(identity)
            if outcome and (not _same_event(outcome, event) or
                            ("result" in event and (outcome.get("settled") != "1" or outcome.get("result") != event["result"]))):
                event["source_conflict"] = True
        held = any(event.get("source_conflict") for event in events.values())
        bins = _fit(events)
        training_ids = sorted(identity for identity, event in events.items()
                              if "result" in event and not event.get("source_conflict"))
        training_sha = digest({identity: events[identity] for identity in training_ids})
        rejected = 0
        for item in sorted(items, key=lambda row: str(row["id"])):
            identity = str(item["id"])
            if identity in events or by_id.get(identity, {}).get("settled") == "1":
                continue
            try:
                p = _probabilities(item, received_at)
                if p is None:
                    continue
                event = {key: item[key] for key in ("home", "away", "league", "kickoff_utc")}
                if not all(type(value) is str and value.strip() for value in event.values()):
                    raise ValueError("invalid event identity")
            except (KeyError, ValueError, TypeError, ArithmeticError):
                rejected += 1
                continue
            baseline = {side: str(p[side]) for side in SIDES}
            events[identity] = {**event, "decision_at": received_at, "forecast_sha256": forecast_sha,
                                "baseline": baseline, "challenger": baseline if held else _predict(p, bins),
                                "training_events": len(training_ids), "training_sha256": training_sha,
                                "challenger_held": held}
        # Current-cycle labels are revealed only after this cycle's predictions.
        for identity, event in events.items():
            outcome = by_id.get(identity)
            if not outcome or "result" in event or event.get("source_conflict"):
                continue
            if not _same_event(outcome, event):
                event["source_conflict"] = True
                continue
            if outcome.get("settled") != "1" or outcome.get("result") not in RESULTS:
                continue
            if (now - instant(event["kickoff_utc"])).total_seconds() < PROTOCOL["result_delay_seconds"]:
                continue
            event.update(result=outcome["result"], result_received_at=received_at, result_sha256=result_sha)
            event["scores"] = {name: brier_score([
                ForecastObservation(float(event[name][side]), int(RESULTS[outcome["result"]] == side))
                for side in SIDES]) for name in ("baseline", "challenger")}
        receipt = {"received_at": received_at, "forecast_sha256": forecast_sha,
                   "results_sha256": result_sha,
                   "previous_sha256": state["receipts"][-1]["sha256"] if state["receipts"] else None}
        state["receipts"].append({**receipt, "sha256": digest(receipt)})
        scored = [event for event in events.values() if "scores" in event and not event.get("source_conflict")]
        summary = {"forecast_events": len(events), "results_learned": len(scored),
                   "source_conflicts": sum(bool(event.get("source_conflict")) for event in events.values()),
                   "rejected_last_cycle": rejected, "profit_earned": None,
                   "mean_brier": {name: sum(event["scores"][name] for event in scored) / len(scored) if scored else None
                                  for name in ("baseline", "challenger")}}
        state.update(last_received_at=received_at, summary=summary)
        atomic_write_json(path, {"state": state, "sha256": digest(state)})
        return summary
