"""Run a pinned, already-observed research replay and emit stress aggregates.

This is not a holdout, independent probability model or execution evidence.
The source may contain private fixture data; output deliberately omits them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from portfolio_replay import replay

MAX_BYTES = 2_000_000


def audit(raw, *, expected_sha256):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized replay input")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError("replay input differs from externally pinned SHA-256")
    data = strict_json_loads(raw.decode("utf-8"))
    baseline = replay(data)
    with_stress = replay(data, include_scenario_stress=True)
    stress = with_stress.pop("scenario_stress")
    if with_stress != baseline:
        raise ValueError("opt-in stress changed baseline paper replay")
    return {"schema": "research.tt_paper_scenario_stress_replay.v1",
            "input_sha256": actual, "input_bytes": len(raw),
            "replay_fixtures": baseline["fixtures"], "assumed_opened_tickets": baseline["opened"],
            "baseline_replay_unchanged": True, "scenario_stress": stress,
            "retrospective_postselected": True, "source_identity_verified": False,
            "source_rights_verified": False, "bookmaker_acceptance_verified": False,
            "independent_holdout": False, "model_training": False,
            "profitability_claim": False, "real_money_execution": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    report = audit(args.input.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
