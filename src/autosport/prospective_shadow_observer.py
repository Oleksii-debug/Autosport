"""Read-only pre-match capture and later recorded-result shadow feedback.

No accepted ticket, real balance or calibrated economic edge is asserted. Raw
public responses are retained before derived state, so a decision can be checked
against the exact prices received before kickoff. Shadow memory stays separate
from PaperBook and executed rewards.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import csv
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .probability import paper_value
from .shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory, digest, instant


@dataclass(frozen=True)
class ObserverPlan:
    minimum_probability: str = "0.30"
    minimum_model_value: str = "0.05"
    maximum_model_value: str = "0.12"
    maximum_odds: str = "3.5"
    maximum_age_seconds: int = 5400
    daily_cap: int = 5
    cost_bps: int = 100

    def __post_init__(self):
        for name in ("minimum_probability", "minimum_model_value", "maximum_model_value", "maximum_odds"):
            value = getattr(self, name)
            if type(value) is not str or not Decimal(value).is_finite():
                raise ValueError("observer numeric settings require finite decimal strings")
        if not 0 < Decimal(self.minimum_probability) < 1 or not 0 < Decimal(self.minimum_model_value) <= Decimal(self.maximum_model_value) <= 1 or not 1 < Decimal(self.maximum_odds) <= 10:
            raise ValueError("invalid observer probability/price/value limits")
        for name, low, high in (("maximum_age_seconds", 1, 5400), ("daily_cap", 1, 20), ("cost_bps", 0, 1000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid observer {name}")


def candidate(item, *, received_at, plan):
    now, kickoff = instant(received_at), instant(item["kickoff_utc"])
    if kickoff <= now or item.get("status") != "actionable" or item.get("fair_source") != "live":
        return None
    for key in ("benchmark_read_at", "fair_read_at"):
        stamp = item.get(key)
        if not stamp or not 0 <= (now - instant(stamp)).total_seconds() <= plan.maximum_age_seconds:
            return None
    value = item.get("value")
    if not value or value.get("selection") not in {"1", "X", "2"}:
        return None
    book = value.get("bookmaker", "").strip()
    if not book or any(word in book.casefold() for word in ("exchange", "matchbook")):
        return None
    side = {"1": "home", "X": "draw", "2": "away"}[value["selection"]]
    p = Decimal(str(item["fair_probabilities"][side]))
    odds = Decimal(str(value["best_soft_odds"]))
    edge = paper_value(value["selection"], p, odds).expected_profit_per_unit
    if p < Decimal(plan.minimum_probability) or odds > Decimal(plan.maximum_odds) or not Decimal(plan.minimum_model_value) <= edge <= Decimal(plan.maximum_model_value):
        return None
    return {"event_id": str(item["id"]), "home": item["home"], "away": item["away"],
            "league": item["league"], "kickoff_utc": kickoff.isoformat(),
            "decision_at": received_at, "selection": value["selection"], "observed_odds": str(odds),
            "reference_probability": str(p), "model_value": str(edge), "bookmaker": book,
            "state": "PENDING_RESULT_NOT_EXECUTED"}


def _retain_raw(root, raw, suffix):
    sha = hashlib.sha256(raw).hexdigest()
    path = root / "raw" / (sha + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("stored public source bytes changed")
    else:
        with path.open("xb") as stream:
            stream.write(raw)
    return sha


def observe_cycle(root: Path, *, predictions: bytes, results: bytes, received_at: str,
                  plan: ObserverPlan = ObserverPlan()):
    now = instant(received_at)
    forecast = json.loads(predictions, parse_float=Decimal)
    if not isinstance(forecast, dict) or not isinstance(forecast.get("predictions"), list):
        raise ValueError("invalid public forecast response")
    if not 0 <= (now - instant(forecast["generated_at"])).total_seconds() <= plan.maximum_age_seconds:
        raise ValueError("stale or future forecast response")
    outcome_rows = list(csv.DictReader(io.StringIO(results.decode("utf-8-sig"))))
    by_id = {}
    for row in outcome_rows:
        if not row.get("id") or row["id"] in by_id:
            raise ValueError("duplicate or missing public result identity")
        by_id[row["id"]] = row
    if len({str(row["id"]) for row in forecast["predictions"]}) != len(forecast["predictions"]):
        raise ValueError("duplicate public forecast identity")
    root.mkdir(parents=True, exist_ok=True)
    state_path, memory_path = root / "observer.json", root / "memory.json"
    with durable_path_lock(state_path):
        if state_path.exists():
            if not memory_path.exists():
                raise ValueError("observer memory checkpoint is missing; explicit recovery required")
            wrapper = strict_json_loads(state_path.read_text("utf-8"))
            if digest(wrapper["state"]) != wrapper["sha256"]:
                raise ValueError("observer checkpoint integrity mismatch")
            state = deepcopy(wrapper["state"])
            if state["plan"] != vars(plan):
                raise ValueError("observer protocol cannot change on resume")
            if now < instant(state["last_received_at"]):
                raise ValueError("observer receipt time cannot go backwards")
        else:
            state = {"schema": "autosport.prospective_shadow_observer", "plan": vars(plan),
                     "candidates": {}, "cycles": 0}
        memory = ShadowStrategyMemory.load(memory_path) if memory_path.exists() else ShadowStrategyMemory(ShadowMemoryPlan(("PRICE_GAP",)))
        if not state_path.exists() and memory.payload()["records"]:
            raise ValueError("observer state missing while learned feedback exists")
        forecast_sha = _retain_raw(root, predictions, ".json")
        results_sha = _retain_raw(root, results, ".csv")
        counts = Counter(item["kickoff_utc"][:10] for item in state["candidates"].values())
        rejected = 0
        # Source corrections must hold preferences before any new choice is made.
        for identity, prior in state["candidates"].items():
            row = by_id.get(identity)
            if row and ((row.get("home"), row.get("away"), row.get("league")) != (prior["home"], prior["away"], prior["league"])
                        or instant(row["kickoff_utc"]) != instant(prior["kickoff_utc"])
                        or (prior["state"] == "RESULT_RECORDED_NOT_EXECUTED" and row.get("settled") == "1" and row.get("result") != prior["result"])):
                prior["source_conflict"] = True
        for item in sorted(forecast["predictions"], key=lambda row: (row["kickoff_utc"], str(row["id"]))):
            identity = str(item["id"])
            if identity in state["candidates"]:
                continue
            if by_id.get(identity, {}).get("settled") == "1":
                rejected += 1
                continue
            try:
                proposed = candidate(item, received_at=received_at, plan=plan)
            except (KeyError, ValueError, ArithmeticError, TypeError):
                rejected += 1
                continue
            if proposed is None or counts[proposed["kickoff_utc"][:10]] >= plan.daily_cap:
                continue
            proposed["forecast_sha256"] = forecast_sha
            proposed["memory_preference"] = "WAIT" if any(x.get("source_conflict") for x in state["candidates"].values()) else memory.choose(as_of=received_at, admissible_actions=frozenset({"PRICE_GAP", "WAIT"}))
            state["candidates"][identity] = proposed
            counts[proposed["kickoff_utc"][:10]] += 1
        for identity, proposed in state["candidates"].items():
            row = by_id.get(identity)
            if not row:
                continue
            if (row.get("home"), row.get("away"), row.get("league")) != (proposed["home"], proposed["away"], proposed["league"]) or instant(row["kickoff_utc"]) != instant(proposed["kickoff_utc"]):
                proposed["source_conflict"] = True
                continue
            if row.get("settled") != "1" or row.get("result") not in {"1", "X", "2"}:
                continue  # Missing/ambiguous/void result never becomes a fabricated win/loss.
            if (now - instant(proposed["kickoff_utc"])).total_seconds() < 7200:
                continue
            if proposed["state"] == "RESULT_RECORDED_NOT_EXECUTED":
                if row["result"] != proposed["result"]:
                    proposed["source_conflict"] = True
                continue
            if proposed.get("source_conflict"):
                continue
            raw_reward = Decimal(proposed["observed_odds"]) - 1 if row["result"] == proposed["selection"] else Decimal(-1)
            net = raw_reward - Decimal(plan.cost_bps) / 10000
            # Journal first-observed source result separately from an executed reward.
            feedback_path = root / "feedback" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
            if feedback_path.exists():
                feedback = strict_json_loads(feedback_path.read_text("utf-8"))
                if set(feedback) != {"result", "available_at", "source_sha256", "net_return"} or Decimal(feedback["net_return"]) != net:
                    raise ValueError("pending observer feedback does not match source settlement")
                if type(feedback["source_sha256"]) is not str or len(feedback["source_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in feedback["source_sha256"]):
                    raise ValueError("pending observer feedback source digest is invalid")
                original = root / "raw" / (feedback["source_sha256"] + ".csv")
                if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != feedback["source_sha256"]:
                    raise ValueError("pending observer feedback source is missing or changed")
                if feedback["result"] != row["result"]:
                    proposed["source_conflict"] = True
                    continue
            else:
                feedback = {"result": row["result"], "available_at": received_at,
                            "source_sha256": results_sha, "net_return": str(net)}
                atomic_write_json(feedback_path, feedback)
            memory.observe(event_id=identity, decision_at=proposed["decision_at"],
                           result_available_at=feedback["available_at"], now=received_at,
                           net_returns={"PRICE_GAP": Decimal(feedback["net_return"])},
                           source_sha256=feedback["source_sha256"], availability_basis="recorded_source_timestamp_unverified")
            proposed.update(state="RESULT_RECORDED_NOT_EXECUTED", result=feedback["result"],
                            result_first_received_at=feedback["available_at"], result_sha256=feedback["source_sha256"],
                            hypothetical_net_return=feedback["net_return"])
        memory.save(memory_path)
        state.update(cycles=state["cycles"] + 1, last_received_at=received_at,
                     last_forecast_sha256=forecast_sha, last_results_sha256=results_sha,
                     invalid_candidates_last_cycle=rejected, real_money_execution=False,
                     profit_earned=None, quote_acceptance_verified=False, promotion_authorized=False)
        atomic_write_json(state_path, {"state": state, "sha256": digest(state)})
        return {"cycles": state["cycles"], "candidates": len(state["candidates"]),
                "results_recorded": sum(x["state"] == "RESULT_RECORDED_NOT_EXECUTED" for x in state["candidates"].values()),
                "source_conflicts": sum(bool(x.get("source_conflict")) for x in state["candidates"].values()),
                "invalid_candidates": rejected, "profit_earned": None, "real_money_execution": False}
