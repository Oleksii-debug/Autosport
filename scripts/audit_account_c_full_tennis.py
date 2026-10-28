"""Retrospective tennis result/quoted-price diagnostic; no live execution."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from datetime import date
from pathlib import Path

from audit_account_c_outcome_odds import audit

SOURCE = "https://huggingface.co/datasets/minmaxxxer/atp-wta-closing-odds-2025"
FIELDS = {"match_id", "start_time_utc", "player1_id", "player2_id", "status",
          "winner", "score", "best_close_odds1", "best_close_odds2"}


def adapt(raw: bytes) -> tuple[bytes, dict]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if (not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames))
            or not FIELDS <= set(reader.fieldnames)):
        raise ValueError("source columns missing or duplicated")
    out = io.StringIO(newline="")
    names = ["match_id", "date", "joueur1_id", "joueur2_id", "vainqueur_id",
             "score", "cote1_ouverture", "cote2_ouverture"]
    writer = csv.DictWriter(out, fieldnames=names, lineterminator="\n")
    writer.writeheader()
    exclusions, total, retained, seen = Counter(), 0, 0, set()
    for row in reader:
        total += 1
        if None in row or any(row.get(k) is None for k in FIELDS):
            raise ValueError("malformed source row")
        identity = row["match_id"].strip()
        if not identity or identity in seen:
            raise ValueError("source match identities must be unique")
        seen.add(identity)
        if row["winner"].strip() not in {"1", "2"}:
            exclusions["missing_or_unknown_winner"] += 1
            continue
        a, b = row["player1_id"].strip(), row["player2_id"].strip()
        if not a or not b or a == b:
            exclusions["missing_or_ambiguous_player"] += 1
            continue
        if not row["start_time_utc"].strip():
            exclusions["missing_match_time"] += 1
            continue
        winner = a if row["winner"].strip() == "1" else b
        # Only complete reported matches enter rating updates; retirement and
        # other statuses remain explicitly counted in the downstream audit.
        score = row["score"].strip() if row["status"].strip().lower() == "finished" else "RET"
        writer.writerow({"match_id": identity, "date": row["start_time_utc"],
                         "joueur1_id": a, "joueur2_id": b, "vainqueur_id": winner,
                         "score": score, "cote1_ouverture": row["best_close_odds1"],
                         "cote2_ouverture": row["best_close_odds2"]})
        retained += 1
    return out.getvalue().encode("utf-8"), {"source_rows": total,
            "adapted_rows": retained, "adapter_exclusions": dict(exclusions)}


def evaluate(raw: bytes) -> dict:
    adapted, counts = adapt(raw)
    report = audit([("adapted_tennis_closing_odds.csv", adapted)],
        warmup_start=date(2024, 12, 27), evaluation_start=date(2025, 7, 1),
        evaluation_end=date(2026, 1, 1), source=SOURCE,
        attribution="minmaxxxer, ATP/WTA closing odds 2025, CC BY 4.0",
        price_description="publisher_best_closing_odds_unverified_not_accepted_fills")
    report["source"]["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    report["source"]["adapter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["adapter_counts"] = counts
    report["limitations"] = {"full_public_dataset": True, "already_published": True,
        "sport": "tennis_not_table_tennis", "source_retrieval": "public_text_snapshot",
        "price_simultaneity_or_execution_verified": False,
        "match_result_publication_time_verified": False}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("output cannot overwrite input")
    result = evaluate(args.input.read_bytes())
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "adapter_counts": result["adapter_counts"],
                      "arms": result["arms"], "truth": result["truth"]}))


if __name__ == "__main__":
    main()
