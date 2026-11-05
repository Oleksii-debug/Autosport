"""Retrospective WTT format comparison, not a match or identity verifier.

This narrowly scoped audit assumes the publisher's exact event and round
labels refer to the corresponding 2023 Singapore Smash singles rounds. It
never rewrites data or admits a result to a learner or a portfolio.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

from wtt_archive_inventory import FILES, _day, _score_shape, audit

POLICY_URL = ("https://worldtabletennis.com/assets/files/"
              "Singapore%20Smash%20Event%20Ticket%20Policy%20-%20V1.pdf")
POLICY_SHA256 = "aa86a20cb4efc51ebfb81cf2f1028b4e286bf8eb3150bc373e6774c52c99d30a"
EVENT = "Singapore Smash 2023"
# The mapping is conditional on the source labels denoting these exact rounds.
# In particular, 8FNL and RND1/2/3 are intentionally not interpreted.
ROUND_POLICY = {"R64": "5", "R32": "5", "QFNL": "7", "SFNL": "7", "FNL": "7"}
KNOWN_EXCLUDED_ROUNDS = frozenset({"8FNL", "RND1", "RND2", "RND3"})
MAX_POLICY_BYTES = 3_000_000


def reconcile(archive_raw, policy_raw, *, expected_archive_sha256):
    """Count source claims against a pinned policy and score-shape assumption."""
    if not isinstance(policy_raw, bytes) or not 0 < len(policy_raw) <= MAX_POLICY_BYTES:
        raise ValueError("empty or oversized policy document")
    policy_digest = hashlib.sha256(policy_raw).hexdigest()
    if policy_digest != POLICY_SHA256:
        raise ValueError("policy document SHA-256 mismatch")
    source = audit(archive_raw, expected_sha256=expected_archive_sha256)
    tables = []
    with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
        for filename in FILES:
            rows = csv.DictReader(io.StringIO(archive.read(filename).decode("utf-8-sig"), newline=""))
            groups = {rnd: {"selected": 0, "declared_matches_policy": 0,
                            "declared_differs_policy": 0, "declared_missing": 0,
                            "score_shape_declared": Counter(), "score_shape_policy": Counter()}
                      for rnd in ROUND_POLICY}
            excluded_round = Counter(); invalid_date = 0; event_rows = 0
            for row in rows:
                if row.get("event_name") != EVENT:
                    continue
                event_rows += 1
                day = _day(row.get("event_date"))
                if day is None or day.year != 2023:
                    invalid_date += 1
                    continue
                rnd = row.get("round")
                if rnd not in ROUND_POLICY:
                    label = rnd if rnd in KNOWN_EXCLUDED_ROUNDS else "<other_or_missing>"
                    excluded_round[label] += 1
                    continue
                g = groups[rnd]; g["selected"] += 1
                declared = row.get("best_of") or ""
                key = ("declared_missing" if not declared.strip() else
                       "declared_matches_policy" if declared == ROUND_POLICY[rnd] else
                       "declared_differs_policy")
                g[key] += 1
                g["score_shape_declared"][_score_shape(row["score"], row["winner"], declared)] += 1
                g["score_shape_policy"][_score_shape(row["score"], row["winner"], ROUND_POLICY[rnd])] += 1
            for g in groups.values():
                for key in ("score_shape_declared", "score_shape_policy"):
                    g[key] = dict(sorted(g[key].items()))
            tables.append({"file": filename, "event_name_label_rows": event_rows,
                           "excluded_invalid_or_outside_2023_date_label": invalid_date,
                           "excluded_round_labels": dict(sorted(excluded_round.items())),
                           "rounds": groups})
    return {"schema": "research.wtt_singapore_2023_policy_comparison.v1",
            "archive_sha256": source["archive_sha256"], "policy_sha256": policy_digest,
            "policy_url": POLICY_URL, "event_label": EVENT,
            "round_policy_if_labels_correspond": ROUND_POLICY.copy(), "tables": tables,
            "score_assumptions": "11-point sets, two-point lead; winner codes 1/2 match source sides",
            "retrospective": True, "independent_match_verification": False,
            "player_identity_verified": False, "source_rights_verified": False,
            "historical_availability_verified": False, "data_modified": False,
            "training_authorized": False, "real_money_execution": False,
            "note": "Policy and source labels are not independently matched at the individual match level."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("policy_pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-archive-sha256", required=True)
    args = parser.parse_args()
    result = reconcile(args.archive.read_bytes(), args.policy_pdf.read_bytes(),
                       expected_archive_sha256=args.expected_archive_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
