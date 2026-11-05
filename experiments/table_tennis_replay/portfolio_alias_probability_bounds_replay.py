"""Pinned retrospective PAPER replay; emit anonymous hypothetical bounds only."""
import argparse
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from portfolio_replay import replay
from portfolio_scenario_stress_replay import MAX_BYTES


def audit(raw, *, expected_sha256):
    if type(raw) is not bytes or not raw or len(raw) > MAX_BYTES:
        raise ValueError("empty or oversized replay input")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("replay input differs from externally pinned SHA-256")
    data = strict_json_loads(raw.decode("utf-8"))
    flags = dict(include_scenario_stress=True, include_single_void_stress=True,
                 include_alias_sensitivity=True, include_probability_sensitivity=True,
                 include_joint_probability_bounds=True)
    before = replay(data, **flags)
    after = replay(data, **flags, include_alias_probability_bounds=True)
    conditional = after.pop("alias_probability_bounds")
    if after != before:
        raise ValueError("alias probability bounds changed prior replay or stress reports")
    for name in ("first_peak_open_tickets", "first_peak_exact_id_sharing"):
        new, previous = conditional[name], before["alias_sensitivity"][name]
        if (new["open_tickets"] != previous["open_tickets"] or
            new["exact_event_ids"] != previous["exact_event_ids"] or
            new["distinct_id_pairs"] != previous["distinct_id_pairs"] or
            new["side_mappings"] != previous["side_mappings"]):
            raise ValueError("alias hypothesis inventory differs from previous stress")
    return {"schema": "research.tt_paper_alias_probability_bounds_replay.v1",
            "input_sha256": digest, "input_bytes": len(raw),
            "replay_fixtures": before["fixtures"], "assumed_opened_tickets": before["opened"],
            "baseline_and_previous_stresses_unchanged": True,
            "alias_probability_bounds": conditional, "retrospective_postselected": True,
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
