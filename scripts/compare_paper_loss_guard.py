"""Capture a resumable fixed sandbox stream and compare two causal strategies."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from autosport.integrity import atomic_write_json
from autosport.json_integrity import strict_json_loads
from autosport.paper_live_sandbox_campaign import SandboxCampaignError, fetch_sandbox_live_snapshot
from autosport.paper_loss_guard_experiment import (
    ComparisonConfig, compare_snapshots, restore_snapshot, snapshot_payload,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = ComparisonConfig(**strict_json_loads(args.config.read_text("utf-8")))
    root = args.workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    frozen_path = root / "frozen_config.json"
    frozen = {"config": config.payload(), "config_sha256": config.sha256}
    if frozen_path.exists():
        if strict_json_loads(frozen_path.read_text("utf-8")) != frozen:
            raise SandboxCampaignError("refusing changed comparison config on resume")
    else:
        atomic_write_json(frozen_path, frozen)
    records = root / "snapshots"
    records.mkdir(exist_ok=True)
    snapshots = []
    existing = sorted(records.glob("*.json"))
    if len(existing) > config.samples:
        raise SandboxCampaignError("snapshot count exceeds frozen protocol")
    for index, path in enumerate(existing):
        if path.name != f"{index:06d}.json":
            raise SandboxCampaignError("snapshot journal has a gap")
        snapshots.append(restore_snapshot(strict_json_loads(path.read_text("utf-8"))))
    failures = 0
    while len(snapshots) < config.samples:
        try:
            current = fetch_sandbox_live_snapshot()
            if snapshots and current.available_at <= snapshots[-1].available_at:
                raise SandboxCampaignError("new snapshot is not later than saved stream")
            if any(current.snapshot_sha256 == item.snapshot_sha256 for item in snapshots):
                raise SandboxCampaignError("duplicate source snapshot")
        except Exception as exc:
            failures += 1
            print(json.dumps({"event": "snapshot_unavailable", "error_type": type(exc).__name__,
                              "failures": failures, "saved": len(snapshots)}), flush=True)
            if failures >= 5:
                raise SandboxCampaignError("bounded collection failed; saved snapshots retained") from None
            time.sleep(max(30, config.poll_seconds))
            continue
        atomic_write_json(records / f"{len(snapshots):06d}.json", snapshot_payload(current))
        snapshots.append(current)
        print(json.dumps({"event": "snapshot_saved", "count": len(snapshots),
                          "target": config.samples, "truth": "simulated"}), flush=True)
        if len(snapshots) < config.samples:
            time.sleep(config.poll_seconds)
    report = compare_snapshots(tuple(snapshots), config, output=root / "results")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
