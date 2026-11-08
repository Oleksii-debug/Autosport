"""Continue the separate historically-seeded observer; no real orders or promotion."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

from autosport.integrity import atomic_write_json
from autosport.prospective_shadow_observer import ObserverPlan, observe_cycle
from autosport.recorded_paper_projection import validated_recorded_replay
from autosport.shadow_strategy_memory import ShadowStrategyMemory, digest, instant
from run_prospective_shadow_observer import fetch, SOURCES


def validate_seed(root):
    wrapper = json.loads((root / "historical-seed.json").read_text("utf-8"))
    seed = wrapper["seed"]
    if digest(seed) != wrapper["sha256"]:
        raise ValueError("historical seed manifest changed")
    source = (root / "raw" / (seed["source_sha256"] + ".csv")).read_bytes()
    if hashlib.sha256(source).hexdigest() != seed["source_sha256"]:
        raise ValueError("historical seed source changed")
    memory = ShadowStrategyMemory.load(root / "memory.json")
    payload = memory.payload()
    count = seed["historical_feedback_count"]
    history = payload["records"][:count]
    if len(history) != count or [row["event_id"] for row in history] != seed["historical_event_ids"]:
        raise ValueError("historical feedback changed")
    if any(row["source_sha256"] != seed["source_sha256"] or instant(row["ingested_at"]) != instant(seed["seeded_at"]) for row in history):
        raise ValueError("historical feedback provenance changed")
    initial_payload = {**payload, "records": history}
    if digest(initial_payload) != seed["memory_sha256"]:
        raise ValueError("historical memory changed")
    return seed


def summarize(root):
    seed = validate_seed(root)
    observer_sha256, actions = validated_recorded_replay(root, historical_seed=seed)
    values = [event for _, kind, _, _, event in actions if kind == 0]
    chosen = [v for v in values if v["memory_preference"] == "PRICE_GAP"]
    settled = [v for v in chosen if v["state"] == "RESULT_RECORDED_NOT_EXECUTED"]
    net = sum((Decimal(v["hypothetical_net_return"]) for v in settled), Decimal(0))
    return {"status": "OK", "observer_sha256": observer_sha256,
            "seeded_at": seed["seeded_at"], "historical_feedback_count": seed["historical_feedback_count"],
            "future_candidates": len(values), "future_selected": len(chosen),
            "future_wait": sum(v["memory_preference"] == "WAIT" for v in values),
            "future_selected_settled": len(settled), "future_hypothetical_net_units": str(net),
            "source_conflicts": sum(bool(v.get("source_conflict")) for v in values),
            "historical_return_included": False, "quote_acceptance_verified": False,
            "profit_earned": None, "real_money_execution": False, "promotion_authorized": False}


def save_summary(root):
    try:
        summary = summarize(root)
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        # A held report has no economic result. Observation can still continue.
        summary = {"status": "HELD", "reason_type": type(exc).__name__,
                   "profit_earned": None, "real_money_execution": False, "promotion_authorized": False}
    atomic_write_json(root / "seeded-summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=49)
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 49 or not 60 <= args.interval_seconds <= 300:
        parser.error("cycles must be 1..49 and interval 60..300 seconds")
    validate_seed(args.workspace)
    failures = 0
    for cycle in range(args.cycles):
        try:
            predictions = fetch(SOURCES["predictions"])
            results = fetch(SOURCES["results"])
            capture = observe_cycle(args.workspace, predictions=predictions, results=results,
                                    received_at=datetime.now(timezone.utc).isoformat(), plan=ObserverPlan())
            summary = save_summary(args.workspace)
            print(json.dumps({"event": "seeded_shadow_cycle", "capture": capture, "summary": summary}), flush=True)
            failures = 0
        except (OSError, TimeoutError) as exc:
            failures += 1
            print(json.dumps({"event": "public_source_unavailable", "error_type": type(exc).__name__,
                              "consecutive_failures": failures}), flush=True)
            if failures >= 5:
                raise RuntimeError("seeded observer stopped after bounded source failures; checkpoint retained") from None
        if cycle + 1 < args.cycles:
            time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
