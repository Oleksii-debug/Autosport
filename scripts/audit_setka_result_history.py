"""Exploratory day-batched rating evaluation; source rights remain unknown."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from audit_table_tennis_day_batch_elo import K_CANDIDATES, predict_day_batched

CUTOFF = date(2022, 7, 1)
SOURCE_SHA256 = "d400562f49ced084133c8e926025efb6023dd62a8341fd4484864d567a4e392d"


def load_history(path):
    result, seen, unresolved = [], set(), []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"X", "Date", "Player1", "Player2", "Sets_P1", "Sets_P2", "HomeWinner"}.issubset(reader.fieldnames):
            raise ValueError("required result fields missing")
        for row in reader:
            match_id = row["X"]
            if not match_id or match_id in seen:
                raise ValueError("missing or duplicate match ID")
            seen.add(match_id)
            # The source description says September, but its file parses fully
            # as month/day and only partially as day/month. Preserve the conflict.
            day = datetime.strptime(row["Date"], "%m/%d/%Y").date()
            a, b = int(row["Sets_P1"]), int(row["Sets_P2"])
            outcome = int(row["HomeWinner"])
            if a < 0 or b < 0 or a == b or outcome not in {0, 1} or (a > b) != bool(outcome):
                raise ValueError("score and winner disagree")
            if not row["Player1"] or not row["Player2"] or row["Player1"] == row["Player2"]:
                unresolved.append(match_id)
                continue
            result.append((match_id, day, row["Player1"], row["Player2"], outcome))
    if not result:
        raise ValueError("empty source")
    return result, unresolved


def brier(rows, prediction):
    if not rows:
        raise ValueError("empty evaluation")
    return sum((prediction[r[0]] - r[4]) ** 2 for r in rows) / len(rows)


def pre_day_minimum_history(rows):
    """Count appearances strictly before each day; do not order same-day matches."""
    days = defaultdict(list)
    for row in rows:
        days[row[1]].append(row)
    counts = Counter()
    prior = {}
    for day in sorted(days):
        for match_id, _, a, b, _ in days[day]:
            prior[match_id] = min(counts[a], counts[b])
        for _, _, a, b, _ in days[day]:
            counts[a] += 1
            counts[b] += 1
    return prior


def audit(path):
    rows, unresolved = load_history(path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    known_source = source_hash == SOURCE_SHA256
    training = [r for r in rows if r[1] < CUTOFF]
    evaluation = [r for r in rows if r[1] >= CUTOFF]
    if not training or not evaluation:
        raise ValueError("insufficient chronological segments")
    training_losses = {k: brier(training, predict_day_batched(training, k=k)) for k in K_CANDIDATES}
    chosen = min(K_CANDIDATES, key=lambda k: (training_losses[k], k))
    result = {}
    for name, k in (("constant_50", 0.0), ("fixed_k20", 20.0), ("training_selected", chosen)):
        prediction = predict_day_batched(rows, k=k)
        result[name] = {"brier": brier(evaluation, prediction),
                        "accuracy": sum((prediction[r[0]] >= .5) == bool(r[4]) for r in evaluation) / len(evaluation)}
    support = pre_day_minimum_history(rows)
    selected_prediction = predict_day_batched(rows, k=chosen)
    groups = {}
    for threshold in (0, 3, 5, 10, 20):
        subset = [r for r in evaluation if support[r[0]] >= threshold]
        if subset:
            groups[str(threshold)] = {"count": len(subset), "elo_brier": brier(subset, selected_prediction),
                "elo_accuracy": sum((selected_prediction[r[0]] >= .5) == bool(r[4]) for r in subset) / len(subset),
                "constant_brier": 0.25}
    return {"schema": "autosport.setka_history.research.v1",
            "source_url": ("https://github.com/SCOREnetworkorg/sports-data-repository/blob/66a314e63ca1ce649d24f4daf48b5564ead01e34/data/table_tennis-sept2022.csv" if known_source else None),
            "source_sha256": source_hash, "known_source_hash_match": known_source,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "model_sha256": hashlib.sha256((Path(__file__).resolve().parent / "audit_table_tennis_day_batch_elo.py").read_bytes()).hexdigest(),
            "source_rights": "UNKNOWN", "source_page_month_conflicts_with_file": True if known_source else None,
            "raw_date_interpretation": "month/day/year; timezone and source timing unverified",
            "cutoff_date": CUTOFF.isoformat(), "training_count": len(training),
            "evaluation_count": len(evaluation), "candidate_training_brier": training_losses,
            "excluded_unresolved_identity_count": len(unresolved),
            "excluded_unresolved_match_ids": unresolved,
            "selected_k": chosen, "results": result,
            "posthoc_minimum_prior_matches": groups,
            "name_identity_independently_verified": False,
            "evaluation_is_untouched_holdout": False, "odds_present": False,
            "profitability_claim": False, "promotion_authorized": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.csv), indent=2, sort_keys=True))
