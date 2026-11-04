"""Compare an externally pinned old forecast with explicitly empty stores.

Reads saved research evidence only; no provider access, training, or betting.
Output contains counts and hashes, never source fixture IDs or player labels.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import ParticipantIdentityRegistry
from causal_h2h_observer import verify_journal
from causal_strength_h2h import compare
from delay_policy_audit import _exact_snapshot

IDENTITY_EMPTY = {"aliases": [], "entities": [], "lineages": [], "rosters": [],
                  "schema": "autosport.participant_identity", "version": 2}
OPPONENTS_EMPTY = {"feature_snapshots": [], "invalidations": [], "performances": [],
                   "rating_snapshots": [], "schema": "autosport.opponent_intelligence", "version": 1}


def replay(root, *, expected_forecast_sha256):
    root = Path(root)
    identity_path, store_path = root / "identity-empty.json", root / "opponents-empty.json"
    identity_before, store_before = identity_path.read_bytes(), store_path.read_bytes()
    if (strict_json_loads(identity_before.decode("utf-8")) != IDENTITY_EMPTY or
            strict_json_loads(store_before.decode("utf-8")) != OPPONENTS_EMPTY):
        raise ValueError("explicitly empty identity and opponent stores required")
    journal, envelope, verified = verify_journal(root)
    if envelope["sha256"] != expected_forecast_sha256:
        raise ValueError("forecast differs from externally pinned SHA-256")
    forecasts = journal["forecasts"]
    ids = [item["fixture_id"] for item in forecasts]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate forecast fixture ID")
    board = _exact_snapshot(root / "snapshots", 1, verified[(1, "odds")])
    registry = ParticipantIdentityRegistry(identity_path)
    store = OpponentIntelligenceStore(store_path, registry)
    counts = Counter()
    for item in forecasts:
        fixture = item["fixture_id"]
        if board.get(fixture) is None:
            raise ValueError("frozen forecast is not an eligible source fixture")
        result = compare(fixture, board[fixture], journal["feature_cutoff"], registry, store)
        if (result["market_home_probability"] != item["market_home_probability"] or
                result["h2h_status"] != item["h2h_status"] or
                result["strength_home_probability"] is not None or
                result["strength_h2h_home_probability"] is not None):
            raise ValueError("empty-store replay differs or produced a strength candidate")
        counts[result["h2h_status"]] += 1
        counts["strength:" + result["strength_status"]] += 1
    if identity_path.read_bytes() != identity_before or store_path.read_bytes() != store_before:
        raise ValueError("empty input stores changed during replay")
    return {"schema": "research.tt_empty_strength_replay.v1",
            "forecast_sha256": envelope["sha256"], "odds_sha256": envelope["odds_sha256"],
            "frozen_forecasts": len(forecasts), "status_counts": dict(sorted(counts.items())),
            "market_values_match": True, "new_strength_values": 0, "stores_unchanged": True,
            "identity_verified": False, "source_rights_verified": False,
            "model_training": False, "bookmaker_execution": False, "profitability_claim": False,
            "note": "Frozen, previously inspected market snapshots; no actual identities or new holdout."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-forecast-sha256", required=True)
    args = parser.parse_args()
    result = replay(args.root, expected_forecast_sha256=args.expected_forecast_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
