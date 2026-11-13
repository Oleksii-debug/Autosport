"""Pinned retrospective PAPER replay; publish certified hypothetical bounds only."""
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
    flags = dict(include_scenario_stress=True, include_single_void_stress=True,
                 include_alias_sensitivity=True, include_probability_sensitivity=True)
    before = replay(data, **flags)
    after = replay(data, **flags, include_joint_probability_bounds=True)
    conditional = after.pop("joint_probability_bounds")
    if after != before:
        raise ValueError("joint bounds changed baseline or prior stresses")
    from fractions import Fraction
    for name in ("first_peak_open_tickets", "first_peak_exact_id_sharing"):
        bounds, old = conditional[name], before["probability_sensitivity"][name]
        if bounds["status"] == "exact_fraction_primal_dual_certified":
            if (Fraction(bounds["independent_all_lose_probability"]) !=
                    Fraction(old["independent_exact_ids"]["conditional_probability_all_open_tickets_lose"]) or
                Fraction(bounds["all_home_or_away_all_lose_probability"]) !=
                    Fraction(old["all_home_or_all_away"]["conditional_probability_all_open_tickets_lose"])):
                raise ValueError("previous hypothetical distribution changed")
    return {"schema": "research.tt_paper_certified_joint_bounds_replay.v1",
            "input_sha256": digest, "input_bytes": len(raw),
            "replay_fixtures": before["fixtures"], "assumed_opened_tickets": before["opened"],
            "baseline_and_previous_stresses_unchanged": True,
            "joint_probability_bounds": conditional, "retrospective_postselected": True,
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
