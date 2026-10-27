"""Paired chronological sandbox comparison; never economic/forward proof.

Both policies receive exactly the same snapshots. Neither can select using the
next snapshot. Existing PAPER settlement, risk caps and learner are reused.
The fixed guard parameters are hashed before collecting the validation stream.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .integrity import atomic_write_json
from .paper_live_sandbox_campaign import (
    BlindPaperCampaignPlan, BlindPaperSandboxCampaign, SandboxCampaignError,
    SandboxLiveQuote, SandboxLiveSnapshot,
)


@dataclass(frozen=True)
class ComparisonConfig:
    samples: int = 121
    poll_seconds: int = 5
    minimum_observations: int = 10
    retry_cycles: int = 5

    def __post_init__(self):
        for name, low, high in (
            ("samples", 3, 1000), ("poll_seconds", 5, 300),
            ("minimum_observations", 1, 10000), ("retry_cycles", 2, 100),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise SandboxCampaignError(f"{name} must be in {low}..{high}")

    def payload(self):
        return dict(vars(self))

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def snapshot_payload(snapshot: SandboxLiveSnapshot) -> dict:
    return {
        "observed_at": snapshot.observed_at,
        "available_at": snapshot.available_at,
        "quotes": [quote.payload() for quote in snapshot.quotes],
        "snapshot_sha256": snapshot.snapshot_sha256,
    }


def restore_snapshot(raw: dict) -> SandboxLiveSnapshot:
    if type(raw) is not dict or set(raw) != {
        "observed_at", "available_at", "quotes", "snapshot_sha256"
    } or type(raw["quotes"]) is not list:
        raise SandboxCampaignError("comparison snapshot fields mismatch")
    quotes = []
    for item in raw["quotes"]:
        if type(item) is not dict or set(item) != {
            "match_id", "market", "side", "decimal_odds", "source_timestamp_ms", "age_seconds"
        } or type(item["decimal_odds"]) is not str:
            raise SandboxCampaignError("comparison quote fields mismatch")
        quotes.append(SandboxLiveQuote(**{
            **item, "decimal_odds": Decimal(item["decimal_odds"])
        }))
    snapshot = SandboxLiveSnapshot(raw["observed_at"], raw["available_at"], tuple(quotes))
    if snapshot_payload(snapshot) != raw:
        raise SandboxCampaignError("comparison snapshot identity mismatch")
    return snapshot


def compare_snapshots(
    snapshots: tuple[SandboxLiveSnapshot, ...], config: ComparisonConfig,
    *, output: Path | None = None,
) -> dict:
    if output is not None and output.exists():
        raise SandboxCampaignError("comparison refuses to overwrite existing results")
    if len(snapshots) != config.samples:
        raise SandboxCampaignError("comparison requires the complete frozen sample count")
    times = [datetime.fromisoformat(item.available_at) for item in snapshots]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise SandboxCampaignError("comparison snapshots must be strictly chronological")
    identities = [item.snapshot_sha256 for item in snapshots]
    if len(set(identities)) != len(identities):
        raise SandboxCampaignError("comparison cannot count duplicate snapshots")
    dataset_sha = hashlib.sha256(json.dumps(
        [snapshot_payload(item) for item in snapshots], sort_keys=True,
        separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    campaigns = {}
    for name, minimum in (("baseline", None), ("loss_guard", config.minimum_observations)):
        campaigns[name] = BlindPaperSandboxCampaign(BlindPaperCampaignPlan(
            campaign_id=f"paired-{config.sha256[:16]}-{dataset_sha[:16]}-{name}",
            started_at=snapshots[0].available_at,
            cutoff_at=(times[-1] + timedelta(seconds=1)).isoformat(timespec="microseconds"),
            loss_guard_min_observations=minimum,
            loss_guard_retry_cycles=config.retry_cycles,
        ))
    decisions = []
    for index, (current, later) in enumerate(zip(snapshots, snapshots[1:])):
        row = {"index": index, "decision_snapshot": current.snapshot_sha256,
               "outcome_snapshot": later.snapshot_sha256}
        # Capture both decisions before handing either policy the next snapshot.
        choices = {
            name: campaign.policy.choose(admissible_actions=campaign._admissible_actions(current))
            for name, campaign in campaigns.items()
        }
        for name, campaign in campaigns.items():
            before = campaign.book.balance
            campaign.step(current, later)
            row[name] = {"action": choices[name], "balance": str(campaign.book.balance),
                         "reward": str(campaign.book.balance - before)}
        decisions.append(row)
    reports = {name: {"summary": campaign.summary(), "diagnostics": campaign.diagnostics()}
               for name, campaign in campaigns.items()}
    difference = campaigns["loss_guard"].book.balance - campaigns["baseline"].book.balance
    report = {
        "schema": "autosport.paired_sandbox_loss_guard", "schema_version": 1,
        "config": config.payload(), "config_sha256": config.sha256,
        "dataset_sha256": dataset_sha, "sample_count": len(snapshots),
        "truth": "simulated", "paper_only": True, "real_money_execution": False,
        "forward_evidence_eligible": False, "promotion_authorized": False,
        "profitability_claim": False, "statistical_significance_claim": False,
        "costs_modelled": False,
        "economic_result_basis": "synthetic price movement, NOT authoritative match outcomes",
        "guard_minus_baseline_reward": str(difference),
        "comparison_result": "HIGHER" if difference > 0 else "LOWER" if difference < 0 else "EQUAL",
        "variants": reports,
    }
    if output is not None:
        from .offline_release_lifecycle import _publish_directory_no_replace

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".comparison-", dir=output.parent) as temporary:
            stage = Path(temporary) / "result"
            stage.mkdir()
            for name, campaign in campaigns.items():
                campaign.save(stage / name)
                restored = BlindPaperSandboxCampaign.load(stage / name)
                if restored.summary() != campaign.summary():
                    raise SandboxCampaignError("comparison restart readback mismatch")
            atomic_write_json(stage / "decisions.json", {"decisions": decisions})
            atomic_write_json(stage / "comparison.json", report)
            _publish_directory_no_replace(stage, output)
    return report
