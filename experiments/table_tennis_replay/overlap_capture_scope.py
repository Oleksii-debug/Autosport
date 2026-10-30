"""Derive the second PAPER session's exact-ID exclusions before its capture.

Source IDs and names are not participant identity, and the scoped union is not
an exhaustive universe of prior matches. No source request or bet is made.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from forward_portfolio_journal import _excluded_sha, _read_record, proposal
from forward_scoped_capture import load_scope, write_json
from prospective_capture import verify_session
from record import instant


def derive(first_root, history_head, expected_scope, *, before):
    first_root = Path(first_root)
    previous, base = load_scope(first_root, history_head, expected_scope)
    session, journal = first_root / "session", first_root / "journal"
    verified = verify_session(session)
    if set(verified) != {(1, "odds"), (1, "results")}:
        raise ValueError("first session must contain exactly one complete capture cycle")
    frozen, receipt = _read_record(journal, "proposal", 1)
    if frozen != proposal(session, 1, base, history_head):
        raise ValueError("first proposal does not match its captured sources")
    if (instant(previous["prior_as_of"]) >= instant(verified[(1, "odds")]["requested_at"]) or
            instant(receipt["computed_at"]) < instant(verified[(1, "results")]["received_at"]) or
            instant(receipt["frozen_at"]) >= instant(before)):
        raise ValueError("first proposal or prior scope not strictly available before second scope")
    selected = [item["fixture_id"] for item in frozen["selected"]]
    if len(selected) != len(set(selected)):
        raise ValueError("duplicate selected source ID")
    ids = set(base) | set(selected)
    return {"schema": "research.tt_forward_prior_scope.v1", "history_head": history_head,
            "prior_as_of": receipt["frozen_at"], "id_set_sha256": _excluded_sha(ids),
            "ids": sorted(ids), "source_scope_sha256": expected_scope,
            "first_proposal_sha256": receipt["sha256"], "first_proposal_selected_count": len(selected),
            "first_selected_new_ids": len(set(selected) - set(base)), "union_count": len(ids),
            "exact_provider_ids_only": True, "complete_prior_universe": False,
            "player_identity_verified": False, "independent_holdout": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first_root", type=Path)
    parser.add_argument("second_root", type=Path)
    parser.add_argument("history_head")
    parser.add_argument("first_scope_sha256")
    parser.add_argument("--before", required=True)
    args = parser.parse_args()
    result = derive(args.first_root, args.history_head, args.first_scope_sha256, before=args.before)
    args.second_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(args.second_root / "prior-scope.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "ids"}, sort_keys=True))


if __name__ == "__main__":
    main()
