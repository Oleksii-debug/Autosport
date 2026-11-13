"""Append one separately precommitted source recheck to a frozen PAPER history.

This preserves even malformed response bytes for audit. Hash links do not
prove omitted source reads, independent identities or bookmaker settlement.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from forward_portfolio_revisions import append_followup, evaluate_history
from forward_scoped_capture import load_scope, write_json
from prospective_capture import _get, _write_exclusive
from record import instant


def _now():
    return datetime.now(timezone.utc)


def capture_next(session, journal, excluded, prior_head, history, expected_head, output, *,
                 date, not_before, fetch=_get, clock=_now):
    """Validate the pin and cutoff before one GET; retain bytes before parsing."""
    if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
        raise ValueError("invalid UTC source date")
    earliest = instant(not_before)
    previous = evaluate_history(session, journal, excluded, prior_head, history, expected_head)
    last = previous["snapshots"][-1]
    if last["source_audit"]["source_date"] != date:
        raise ValueError("source date differs from prior history")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("recheck destination already exists")
    started = clock()
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("aware local request clock required")
    if started < earliest or started <= instant(last["as_of"]):
        raise ValueError("recheck requested too early")
    output.mkdir(mode=0o700)
    url = "https://moviodds.com/results?sport=table_tennis&date=" + date
    raw = fetch(url)
    received = clock()
    if received.tzinfo is None or received.utcoffset() is None or received < started:
        raise ValueError("invalid local receipt clock")
    if not isinstance(raw, bytes):
        raise ValueError("response must be bytes")
    receipt = {"schema": "autosport.tt_dated_followup.v1", "bytes": len(raw),
               "sha256": hashlib.sha256(raw).hexdigest(), "url": url,
               "requested_at": started.isoformat(), "received_at": received.isoformat(),
               "rights_verified": False, "identity_verified": False, "executable_price_verified": False}
    receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    _write_exclusive(output / "dated-results.body.json", raw)
    _write_exclusive(output / "dated-results.receipt.json", receipt_bytes)
    head = append_followup(session, journal, excluded, prior_head, history, raw, receipt_bytes,
                           expected_head=expected_head)
    report = evaluate_history(session, journal, excluded, prior_head, history, head)
    if report["snapshots"][:-1] != previous["snapshots"]:
        raise ValueError("earlier as-of reports changed")
    path = output / "revision-report.json"
    write_json(path, report)
    return {"receipt": receipt, "previous_head": expected_head, "history_head": head,
            "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "fixture_counts": report["snapshots"][-1]["fixture_counts"],
            "source_audit": report["snapshots"][-1]["source_audit"],
            "policies": report["snapshots"][-1]["policies"],
            "earlier_reports_preserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--prior-head", required=True)
    parser.add_argument("--scope-sha", required=True)
    parser.add_argument("--history-head", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--not-before", required=True)
    args = parser.parse_args()
    _, ids = load_scope(args.root, args.prior_head, args.scope_sha)
    report = capture_next(args.root / "session", args.root / "journal", ids, args.prior_head,
                          args.root / "history", args.history_head, args.root / "followup2",
                          date=args.date, not_before=args.not_before)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
