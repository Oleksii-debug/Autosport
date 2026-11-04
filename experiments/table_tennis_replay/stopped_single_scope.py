"""Derive exact-ID exclusions from one pinned, stopped PAPER evidence bundle.

This is an offline reconstruction, not a player identity merge, new history
link, evidence of independence, authorization to train, or permission to bet.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from forward_portfolio_journal import _excluded_sha, _read_record, proposal, verify_actions
from forward_prior_scope import _ids
from forward_scoped_capture import load_scope, write_json
from prospective_capture import verify_session
from record import instant
from stopped_overlap_scope import _extract_verified


def derive(raw, *, expected_archive_sha256, history_head, prior_scope_sha256, before):
    """Verify the stopped first action and union all four recorded bodies."""
    cutoff = instant(before)
    with tempfile.TemporaryDirectory(prefix="tt-stopped-single-scope-") as temp:
        entries = _extract_verified(raw, expected_archive_sha256, temp,
                                    schema="research.tt_stopped_single_bundle.v1")
        root = Path(temp) / "run"
        scope, prior_ids = load_scope(root, history_head, prior_scope_sha256)
        session, journal = root / "session", root / "journal"
        verified = verify_session(session)
        if set(verified) != {(1, "odds"), (1, "results"), (2, "odds"), (2, "results")}:
            raise ValueError("unexpected stopped single-session captures")
        if instant(scope["prior_as_of"]) >= instant(verified[(1, "odds")]["requested_at"]):
            raise ValueError("prior scope does not precede the stopped session")
        expected_journal = {f"{kind}-0001.{part}.json"
                            for kind in ("proposal", "action") for part in ("body", "receipt")}
        if {p.name for p in journal.iterdir()} != expected_journal:
            raise ValueError("unexpected stopped single-session journal entries")
        first, penv = _read_record(journal, "proposal", 1)
        if first != proposal(session, 1, prior_ids, history_head):
            raise ValueError("stopped proposal differs")
        verify_actions(session, journal, prior_ids, history_head, through=1)
        _, aenv = _read_record(journal, "action", 1)
        ids, times = set(prior_ids), []
        for (seq, kind), receipt in verified.items():
            body = (session / f"{seq:04d}.{kind}.body.json").read_bytes()
            if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
                raise ValueError("stopped source changed after verification")
            ids.update(_ids(body, "matches" if kind == "odds" else "results",
                            "id" if kind == "odds" else "fixture_id"))
            times.append(instant(receipt["received_at"]))
        times.extend((instant(scope["prior_as_of"]), instant(penv["frozen_at"]),
                      instant(aenv["frozen_at"])))
        prior_as_of = max(times)
        if prior_as_of >= cutoff:
            raise ValueError("stopped evidence is not strictly before new scope")
        return {"schema": "research.tt_forward_prior_scope.v1", "history_head": history_head,
                "prior_as_of": prior_as_of.isoformat(), "id_set_sha256": _excluded_sha(ids),
                "ids": sorted(ids), "union_count": len(ids), "base_scope_count": len(prior_ids),
                "capture_counts": {"odds": 2, "results": 2},
                "archive_sha256": expected_archive_sha256, "archive_manifest_entries": entries,
                "first_action_verified": True, "second_proposal_frozen": False,
                "exact_provider_ids_only": True, "complete_prior_universe": False,
                "player_identity_verified": False, "independent_holdout": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("root", type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--history-head", required=True)
    parser.add_argument("--prior-scope-sha256", required=True)
    args = parser.parse_args()
    scope = derive(args.archive.read_bytes(), expected_archive_sha256=args.archive_sha256,
                   history_head=args.history_head, prior_scope_sha256=args.prior_scope_sha256,
                   before=datetime.now(timezone.utc).isoformat())
    args.root.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(args.root / "prior-scope.json", scope)
    print(json.dumps({k: v for k, v in scope.items() if k != "ids"}, sort_keys=True))


if __name__ == "__main__":
    main()
