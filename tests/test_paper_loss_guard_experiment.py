from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import subprocess
import sys

import pytest

from autosport.paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, SandboxCampaignError,
    SandboxLiveQuote, SandboxLiveSnapshot,
)
from autosport.paper_loss_guard_experiment import (
    ComparisonConfig, compare_snapshots, restore_snapshot, snapshot_payload,
)


T0 = datetime(2026, 10, 27, tzinfo=timezone.utc)


def stream(count, direction=1):
    result = []
    for index in range(count):
        stamp = (T0 + timedelta(seconds=index)).isoformat(timespec="microseconds")
        quotes = tuple(sorted((SandboxLiveQuote(
            event, "h2h", "home", Decimal("3") + Decimal(direction * index) / 100,
            int((T0 + timedelta(seconds=index)).timestamp() * 1000), 0,
        ) for event in ("event-a", "event-b")), key=lambda quote: quote.quote_identity))
        result.append(SandboxLiveSnapshot(stamp, stamp, quotes))
    return tuple(result)


def plan(**kwargs):
    return BlindPaperCampaignPlan(
        "loss-guard-regression", T0.isoformat(), (T0 + timedelta(hours=1)).isoformat(),
        **kwargs,
    )


def test_negative_training_cannot_force_every_later_loss_and_guard_retries():
    campaign = BlindPaperSandboxCampaign(plan(loss_guard_min_observations=1))
    snapshots = stream(62)
    for index in range(20):
        campaign.step(snapshots[index], snapshots[index + 1])
    assert campaign._admissible_actions(snapshots[20]) == frozenset({"WAIT"})
    for index in range(20, 50):
        campaign.step(snapshots[index], snapshots[index + 1])
    # Frozen periodic exploration resumes the original slot, under the same cap.
    assert campaign._admissible_actions(snapshots[50]) == frozenset({"PARLAY_2_LOW_ODDS"})
    campaign.step(snapshots[50], snapshots[51])
    assert campaign.counters["WAIT"] > 0
    assert all(ticket.stake <= 50 for ticket in campaign.book.tickets.values())


def test_same_stream_sustained_losses_reports_actual_drawdown_reduction():
    report = compare_snapshots(stream(41), ComparisonConfig(samples=41, minimum_observations=1))
    base = report["variants"]["baseline"]["diagnostics"]
    guarded = report["variants"]["loss_guard"]["diagnostics"]
    assert Decimal(guarded["net_reward"]) > Decimal(base["net_reward"])
    assert Decimal(guarded["maximum_drawdown"]) < Decimal(base["maximum_drawdown"])
    assert guarded["ticket_count"] < base["ticket_count"]
    assert report["profitability_claim"] is False
    assert report["statistical_significance_claim"] is False
    assert report["forward_evidence_eligible"] is False


def test_positive_and_neutral_streams_do_not_get_artificial_improvement():
    for direction in (-1, 0):
        report = compare_snapshots(stream(31, direction), ComparisonConfig(samples=31, minimum_observations=1))
        assert report["comparison_result"] == "EQUAL"
        assert report["guard_minus_baseline_reward"] == "0" or Decimal(report["guard_minus_baseline_reward"]) == 0


def test_guard_is_opt_in_and_old_plan_identity_roundtrips(tmp_path):
    original = BlindPaperSandboxCampaign(plan())
    assert "loss_guard_min_observations" not in original.plan.payload()
    original.save(tmp_path / "original")
    loaded = BlindPaperSandboxCampaign.load(tmp_path / "original")
    assert loaded.summary() == original.summary()
    guarded = BlindPaperSandboxCampaign(plan(loss_guard_min_observations=3, loss_guard_retry_cycles=4))
    assert guarded.plan.plan_sha256 != original.plan.plan_sha256
    snapshots = stream(12)
    for left, right in zip(snapshots, snapshots[1:]):
        guarded.step(left, right)
    guarded.save(tmp_path / "guarded")
    loaded_guard = BlindPaperSandboxCampaign.load(tmp_path / "guarded")
    assert loaded_guard.plan.loss_guard_min_observations == 3
    assert loaded_guard.plan.loss_guard_retry_cycles == 4
    assert loaded_guard.summary() == guarded.summary()
    guarded.step(stream(13)[11], stream(13)[12])
    loaded_guard.step(stream(13)[11], stream(13)[12])
    assert loaded_guard.policy == guarded.policy
    assert loaded_guard.book.balance == guarded.book.balance


@pytest.mark.parametrize("kwargs", [
    {"loss_guard_min_observations": True}, {"loss_guard_min_observations": 0},
    {"loss_guard_min_observations": 10001}, {"loss_guard_retry_cycles": 1},
    {"loss_guard_retry_cycles": False},
])
def test_invalid_guard_configuration_rejected(kwargs):
    with pytest.raises(SandboxCampaignError):
        plan(**kwargs)


def test_snapshot_roundtrip_and_tamper_rejection():
    snapshot = stream(1)[0]
    raw = snapshot_payload(snapshot)
    assert restore_snapshot(raw) == snapshot
    raw["quotes"][0]["decimal_odds"] = "9"
    with pytest.raises(SandboxCampaignError, match="identity mismatch"):
        restore_snapshot(raw)


def test_incomplete_or_reversed_comparison_cannot_select_winner():
    with pytest.raises(SandboxCampaignError, match="complete"):
        compare_snapshots(stream(3), ComparisonConfig(samples=4))
    with pytest.raises(SandboxCampaignError, match="chronological"):
        compare_snapshots(tuple(reversed(stream(3))), ComparisonConfig(samples=3))


def test_saved_decisions_are_causal_and_results_are_not_overwritten(tmp_path):
    config = ComparisonConfig(samples=25, minimum_observations=1)
    original = stream(25)
    altered = original[:15] + tuple(
        replace(item, quotes=tuple(replace(q, decimal_odds=q.decimal_odds + 1) for q in item.quotes))
        for item in original[15:]
    )
    compare_snapshots(original, config, output=tmp_path / "a")
    compare_snapshots(altered, config, output=tmp_path / "b")
    a = json.loads((tmp_path / "a/decisions.json").read_text())["decisions"]
    b = json.loads((tmp_path / "b/decisions.json").read_text())["decisions"]
    assert a[:14] == b[:14]
    # The decision immediately BEFORE the changed outcome also remains unchanged.
    for name in ("baseline", "loss_guard"):
        assert a[14][name]["action"] == b[14][name]["action"]
    with pytest.raises(SandboxCampaignError, match="overwrite"):
        compare_snapshots(original, config, output=tmp_path / "a")


def test_published_campaigns_reload_from_their_final_paths(tmp_path):
    output = tmp_path / "published"
    report = compare_snapshots(stream(12), ComparisonConfig(samples=12), output=output)
    for name in ("baseline", "loss_guard"):
        loaded = BlindPaperSandboxCampaign.load(output / name)
        assert loaded.summary() == report["variants"][name]["summary"]


def test_interrupted_publication_preserves_evidence_without_success_marker(tmp_path, monkeypatch):
    output = tmp_path / "partial"
    save = BlindPaperSandboxCampaign.save
    count = 0

    def fail_second_save(self, path):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected write failure")
        return save(self, path)

    monkeypatch.setattr(BlindPaperSandboxCampaign, "save", fail_second_save)
    snapshots, config = stream(12), ComparisonConfig(samples=12)
    with pytest.raises(OSError, match="injected"):
        compare_snapshots(snapshots, config, output=output)
    assert not (output / "comparison.json").exists()
    baseline = BlindPaperSandboxCampaign.load(output / "baseline")
    assert baseline.policy.generation == 11
    with pytest.raises(SandboxCampaignError, match="overwrite"):
        compare_snapshots(snapshots, config, output=output)


def test_published_campaign_reload_in_new_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR", str(tmp_path / "authority"))
    output = tmp_path / "published"
    report = compare_snapshots(stream(12), ComparisonConfig(samples=12), output=output)
    code = (
        "import json,sys; from pathlib import Path; "
        "from autosport.paper_live_sandbox_campaign import BlindPaperSandboxCampaign as C; "
        "print(json.dumps({n:C.load(Path(sys.argv[1])/n).summary() "
        "for n in ('baseline','loss_guard')},sort_keys=True))"
    )
    child = subprocess.run([sys.executable, "-c", code, str(output)],
                           env=os.environ.copy(), capture_output=True, text=True, check=True)
    loaded = json.loads(child.stdout)
    assert loaded == {name: item["summary"] for name, item in report["variants"].items()}
