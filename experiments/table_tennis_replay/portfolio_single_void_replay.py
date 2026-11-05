"""One-event void stress on a pinned, already-inspected PAPER replay.

Outputs only aggregates. Displayed prices are assumed, not accepted orders.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from portfolio_replay import replay
from portfolio_scenario_stress_replay import audit as audit_binary


def audit(raw, *, expected_sha256):
    """Preserve and compare both the baseline and existing binary stress."""
    prior = audit_binary(raw, expected_sha256=expected_sha256)
    data = strict_json_loads(raw.decode("utf-8"))
    original = replay(data)
    both = replay(data, include_scenario_stress=True, include_single_void_stress=True)
    new_stress = both.pop("single_void_stress")
    old_stress = both.pop("scenario_stress")
    if both != original or old_stress != prior["scenario_stress"]:
        raise ValueError("void stress changed the baseline or existing binary stress")
    return {"schema": "research.tt_paper_single_void_replay.v1",
            "input_sha256": prior["input_sha256"], "input_bytes": prior["input_bytes"],
            "replay_fixtures": prior["replay_fixtures"],
            "assumed_opened_tickets": prior["assumed_opened_tickets"],
            "baseline_replay_unchanged": True, "prior_binary_stress_unchanged": True,
            "single_void_stress": new_stress, "retrospective_postselected": True,
            "source_identity_verified": False, "source_rights_verified": False,
            "bookmaker_acceptance_verified": False, "bookmaker_void_rules_verified": False,
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
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
