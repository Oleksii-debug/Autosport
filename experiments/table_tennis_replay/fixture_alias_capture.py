"""Bounded retrospective /closing alias capture for the frozen PAPER cohort.

The caller must externally pin the input hashes before running `capture`.
Later alias responses cannot retroactively authorize or alter earlier actions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from autosport.json_integrity import strict_json_loads
from fixture_alias_audit import audit_as_of, capture_one, read_captures, session_ids
from forward_portfolio_journal import _excluded_sha, _read_record
from forward_portfolio_revisions import evaluate_history
from forward_scoped_capture import load_scope, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "capture"))
    parser.add_argument("session_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prior-head", required=True)
    parser.add_argument("--history-head", required=True)
    parser.add_argument("--scope-sha", required=True)
    parser.add_argument("--cohort-sha", required=True)
    args = parser.parse_args()
    root, output = args.session_root, args.output
    _, prior_ids = load_scope(root, args.prior_head, args.scope_sha)
    history = evaluate_history(root / "session", root / "journal", prior_ids, args.prior_head,
                               root / "history", args.history_head)
    ids = session_ids(root / "session")
    if _excluded_sha(ids) != args.cohort_sha:
        raise ValueError("captured fixture cohort differs from external pin")
    _, proposal = _read_record(root / "journal", "proposal", 1)
    cohort = {"schema": "research.tt_fixture_alias_cohort.v1", "ids": sorted(ids),
              "cohort_sha256": args.cohort_sha, "prior_scope_sha256": args.scope_sha,
              "prior_history_head": args.prior_head, "paper_history_head": args.history_head,
              "paper_history_as_of": history["snapshots"][-1]["as_of"],
              "decision_cutoff": proposal["computed_at"]}
    if args.stage == "prepare":
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
        write_json(output / "cohort.json", cohort)
        print(json.dumps({k: v for k, v in cohort.items() if k != "ids"} | {"fixtures": len(ids)}))
        return
    if (output.is_symlink() or not output.is_dir() or
            strict_json_loads((output / "cohort.json").read_text("utf-8")) != cohort):
        raise ValueError("prepared cohort differs from validated source")
    captures = output / "captures"; captures.mkdir()
    stopped_on_error = False
    for index, fid in enumerate(cohort["ids"], 1):
        receipt = capture_one(captures, index, fid)
        print(json.dumps({"attempt": index, "outcome": receipt["outcome"],
                          "received_at": receipt["received_at"],
                          "http_status": receipt.get("http_status")}), flush=True)
        if receipt["outcome"] != "response":
            stopped_on_error = True
            break
        if index < len(ids):
            time.sleep(1)
    observations = read_captures(captures)
    before = audit_as_of(ids, prior_ids, observations, cutoff=cohort["decision_cutoff"])
    after = audit_as_of(ids, prior_ids, observations, cutoff=datetime.now(timezone.utc).isoformat())
    for name, report in (("asof-decision.json", before), ("after-capture.json", after)):
        write_json(output / name, report)
    summary = {"cohort_count": len(ids), "requests_attempted": len(observations),
               "stopped_on_error": stopped_on_error,
               "asof_decision_counts": before["counts"], "after_capture_counts": after["counts"],
               "asof_decision_sha256": hashlib.sha256((output / "asof-decision.json").read_bytes()).hexdigest(),
               "after_capture_sha256": hashlib.sha256((output / "after-capture.json").read_bytes()).hexdigest(),
               "retrospective_only": True, "real_money_execution": False}
    write_json(output / "summary.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
