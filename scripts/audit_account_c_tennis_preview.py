"""Analyze the already viewed first 100 public tennis dataset rows; nonpromotional."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from audit_account_c_outcome_odds import audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("output cannot overwrite input")
    report = audit([(args.input.name, args.input.read_bytes())],
        warmup_start=date(2024, 12, 27), evaluation_start=date(2024, 12, 30),
        evaluation_end=date(2025, 1, 1),
        source="https://huggingface.co/datasets/minmaxxxer/atp-wta-closing-odds-2025",
        attribution="minmaxxxer, ATP/WTA closing odds 2025, public viewer preview",
        price_description="publisher_best_closing_odds_unverified_not_accepted_fills")
    report["preview_limits"] = {"postselected": True, "only_first_100_viewer_rows": True,
                                "provider_claimed_license": "CC BY 4.0",
                                "sport": "tennis_not_table_tennis",
                                "same_market_prices_simultaneously_available": False}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+"\n",
                           encoding="utf-8")
    print(json.dumps({"counts": report["counts"], "arms": report["arms"],
                      "truth": report["truth"]}))


if __name__ == "__main__":
    main()
