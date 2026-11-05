"""Pinned retrospective PAPER replay; write conditional aggregates only."""
import argparse
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from portfolio_replay import replay
from portfolio_scenario_stress_replay import MAX_BYTES


def audit(raw, *, expected_sha256):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized replay input")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("replay input differs from externally pinned SHA-256")
    data = strict_json_loads(raw.decode("utf-8"))
    before = replay(data, include_scenario_stress=True, include_single_void_stress=True)
    after = replay(data, include_scenario_stress=True, include_single_void_stress=True,
                   include_alias_sensitivity=True)
    conditional = after.pop("alias_sensitivity")
    if after != before:
        raise ValueError("alias sensitivity changed the baseline or previous stress")
    return {"schema": "research.tt_paper_alias_sensitivity_replay.v1",
            "input_sha256": digest, "input_bytes": len(raw),
            "replay_fixtures": before["fixtures"], "assumed_opened_tickets": before["opened"],
            "baseline_and_previous_stresses_unchanged": True,
            "alias_sensitivity": conditional, "retrospective_postselected": True,
            "source_identity_verified": False, "source_rights_verified": False,
            "bookmaker_acceptance_verified": False, "independent_holdout": False,
            "model_training": False, "profitability_claim": False, "real_money_execution": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    report = audit(args.input.read_bytes(), expected_sha256=args.expected_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
