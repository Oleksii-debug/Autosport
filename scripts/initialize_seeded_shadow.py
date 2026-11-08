"""Initialize one separate, virtual-only shadow challenger from a fixed source."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from autosport.integrity import atomic_write_json
from autosport.prospective_shadow_observer import ObserverPlan
from autosport.shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory, digest, instant
from evaluate_recorded_live_flags import evaluate


def initialize(source: Path, root: Path, *, expected_sha: str, clock=lambda: datetime.now(timezone.utc)):
    raw = source.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != expected_sha:
        raise ValueError("historical seed source digest mismatch")
    report = evaluate(source, ordered_source_times=True)
    if report["source_sha256"] != sha:
        raise ValueError("historical source changed during initialization")
    at = clock()
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("seed clock requires timezone")
    seeded_at = at.astimezone(timezone.utc).isoformat()
    at = instant(seeded_at)
    selected = [row for row in report["decisions"] if row["current_rule_selected"] and row["state"] == "SETTLED"]
    if not selected:
        raise ValueError("historical seed has no resolved candidate evidence")
    if any((at - instant(row["kickoff"])).total_seconds() < 48 * 3600 for row in selected):
        raise ValueError("historical seed outcome is too recent")
    memory = ShadowStrategyMemory(ShadowMemoryPlan(("PRICE_GAP",)))
    for row in selected:
        memory.observe(event_id=row["id"], decision_at=row["recorded_at"],
                       result_available_at=seeded_at, now=seeded_at,
                       net_returns={"PRICE_GAP": Decimal(row["reward"]) - Decimal(".01")},
                       source_sha256=sha, availability_basis="recorded_source_timestamp_unverified")
    seed = {"schema": "autosport.historical_shadow_seed.v1", "seeded_at": seeded_at,
            "source_sha256": sha, "historical_event_ids": [row["id"] for row in selected],
            "historical_feedback_count": len(selected), "memory_sha256": digest(memory.payload()),
            "selection_rule": "recorded_live_flags_current_rule_ordered_source_times_posthoc",
            "future_performance_begins_at": seeded_at, "profit_earned": None,
            "real_money_execution": False, "promotion_authorized": False,
            "attribution": "TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0"}
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw").mkdir()
    (root / "raw" / (sha + ".csv")).write_bytes(raw)
    memory.save(root / "memory.json")
    plan = ObserverPlan()
    state = {"schema": "autosport.prospective_shadow_observer", "plan": vars(plan),
             "candidates": {}, "cycles": 0, "last_received_at": seeded_at}
    atomic_write_json(root / "observer.json", {"state": state, "sha256": digest(state)})
    atomic_write_json(root / "historical-seed.json", {"seed": seed, "sha256": digest(seed)})
    return {"seeded_at": seeded_at, "historical_feedback_count": len(selected),
            "research_preference": memory.choose(as_of=seeded_at, admissible_actions=frozenset({"PRICE_GAP", "WAIT"})),
            "source_sha256": sha, "profit_earned": None, "real_money_execution": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    print(json.dumps(initialize(args.source, args.workspace, expected_sha=args.expected_sha)))
