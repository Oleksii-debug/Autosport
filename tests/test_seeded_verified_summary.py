from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

import pytest

from autosport.prospective_shadow_observer import ObserverPlan
from autosport.shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory, digest
from test_prospective_shadow_observer import cycle, stamp

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from run_seeded_shadow import save_summary, summarize


def save_wrapper(path, key, payload):
    path.write_text(json.dumps({key: payload, "sha256": digest(payload)}), encoding="utf-8")


@pytest.fixture
def seeded(tmp_path):
    seeded_at = stamp(-1)
    raw = b"constructed historical feedback fixture"
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / (sha + ".csv")).write_bytes(raw)
    memory = ShadowStrategyMemory(ShadowMemoryPlan(("PRICE_GAP",)))
    ids = [f"old-{i}" for i in range(30)]
    for identity in ids:
        memory.observe(event_id=identity, decision_at=stamp(-100), result_available_at=seeded_at,
                       now=seeded_at, net_returns={"PRICE_GAP": Decimal("0.2")},
                       source_sha256=sha, availability_basis="retrospective_assumed_delay")
    memory.save(tmp_path / "memory.json")
    seed = {"seeded_at": seeded_at, "historical_feedback_count": len(ids), "historical_event_ids": ids,
            "source_sha256": sha, "memory_sha256": digest(memory.payload())}
    save_wrapper(tmp_path / "historical-seed.json", "seed", seed)
    save_wrapper(tmp_path / "observer.json", "state", {
        "schema": "autosport.prospective_shadow_observer", "plan": vars(ObserverPlan()),
        "candidates": {}, "cycles": 0, "last_received_at": seeded_at,
    })
    cycle(tmp_path)
    cycle(tmp_path, 4, "2", "1")
    return tmp_path


def test_valid_seed_changes_preference_but_not_future_reward(seeded):
    report = summarize(seeded)
    assert report["historical_feedback_count"] == 30
    assert report["future_selected"] == report["future_selected_settled"] == 1
    assert Decimal(report["future_hypothetical_net_units"]) == Decimal("-1.01")
    assert report["profit_earned"] is None


@pytest.mark.parametrize("field,value,reason", [
    ("hypothetical_net_return", "999", "shadow learning disagree"),
    ("memory_preference", "WAIT", "cannot be derived"),
    ("result", "1", "outcome mismatch"),
])
def test_rehashed_summary_cannot_override_source(seeded, field, value, reason):
    path = seeded / "observer.json"
    state = json.loads(path.read_text("utf-8"))["state"]
    state["candidates"]["1"][field] = value
    save_wrapper(path, "state", state)
    with pytest.raises(ValueError, match=reason):
        summarize(seeded)


def test_source_correction_holds_report_without_training_again(seeded):
    cycle(seeded, 5, "1", "1")
    report = save_summary(seeded)
    assert report["status"] == "HELD"
    assert "future_hypothetical_net_units" not in report
    assert report["profit_earned"] is None
    assert len(ShadowStrategyMemory.load(seeded / "memory.json").payload()["records"]) == 31
    assert json.loads((seeded / "seeded-summary.json").read_text("utf-8")) == report


def test_source_bytes_must_match_original_digest(seeded):
    state = json.loads((seeded / "observer.json").read_text("utf-8"))["state"]
    sha = state["candidates"]["1"]["result_sha256"]
    path = seeded / "raw" / (sha + ".csv")
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source snapshot digest mismatch"):
        summarize(seeded)
