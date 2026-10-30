"""Run individually anchored stages of a scoped, hypothetical PAPER observation.

This is a research orchestrator. The operator must publicly pin each frozen
proposal/action before requesting the next source observation; the code cannot
prove that external publication or trust clock synchronization. No live bets.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time

from autosport.json_integrity import strict_json_loads
from forward_portfolio_journal import (_excluded_sha, freeze_action, freeze_proposal,
                                       known_prior_ids, verify_actions)
from forward_portfolio_revisions import append_followup, evaluate_history
from forward_prior_scope import build_scope
from prospective_capture import _get, _parse, _write_exclusive, capture_one, verify_session
from record import instant


def now():
    return datetime.now(timezone.utc)


def write_json(path, value):
    _write_exclusive(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())


def load_scope(root, history_head, expected_scope):
    data = strict_json_loads((root / "prior-scope.json").read_text("utf-8"))
    ids = data["ids"]
    if (data["schema"] != "research.tt_forward_prior_scope.v1" or
            data["history_head"] != history_head or data["id_set_sha256"] != expected_scope or
            not isinstance(ids, list) or len(set(ids)) != len(ids) or
            _excluded_sha(set(ids)) != expected_scope):
        raise ValueError("prior exclusion scope differs from pinned protocol")
    return data, set(ids)


def wait_for_quote(session, seq):
    receipt = verify_session(session)[(seq, "odds")]
    delay = (instant(receipt["received_at"]) + timedelta(seconds=31) - now()).total_seconds()
    if delay > 0:
        time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "initial", "action1", "results2", "action2", "finish", "followup"))
    parser.add_argument("root", type=Path)
    parser.add_argument("previous", type=Path, help="prior revision bundle containing original/ and history/")
    parser.add_argument("--base-head", required=True)
    parser.add_argument("--history-head", required=True)
    parser.add_argument("--scope-sha", required=True)
    parser.add_argument("--date", required=True, help="UTC source date YYYY-MM-DD")
    args = parser.parse_args()
    root, original = args.root, args.previous / "original"
    session, journal = root / "session", root / "journal"
    if args.stage == "prepare":
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
        base, _ = known_prior_ids(original / "prior/observer", original / "prior/history", args.base_head)
        scope = build_scope(original / "session", original / "journal", base, args.base_head,
                            args.previous / "history", args.history_head, before=now().isoformat())
        if scope["id_set_sha256"] != args.scope_sha:
            raise ValueError("computed prior scope differs from proposed pin")
        write_json(root / "prior-scope.json", scope)
        print(json.dumps({k: v for k, v in scope.items() if k != "ids"}))
        return
    scope, excluded = load_scope(root, args.history_head, args.scope_sha)
    if args.stage == "initial":
        session.mkdir(); journal.mkdir()
        if instant(scope["prior_as_of"]) >= now():
            raise ValueError("prior evidence is not available before observation")
        odds = capture_one(session, 1, "odds")
        if instant(scope["prior_as_of"]) >= instant(odds["requested_at"]):
            raise ValueError("prior evidence overlaps new observation")
        results = capture_one(session, 1, "results")
        envelope = freeze_proposal(session, journal, 1, excluded, args.history_head)
        body = strict_json_loads((journal / "proposal-0001.body.json").read_text("utf-8"))
        out = {"odds_requested_at": odds["requested_at"], "odds_received_at": odds["received_at"],
               "results_received_at": results["received_at"], "proposal": envelope,
               "eligible_before_exclusions": body["eligible_before_exclusions"],
               "eligible_after_exclusions": body["eligible_after_exclusions"], "selected_count": len(body["selected"])}
    elif args.stage in ("action1", "action2"):
        seq = int(args.stage[-1])
        wait_for_quote(session, seq)
        odds = capture_one(session, seq + 1, "odds")
        envelope = freeze_action(session, journal, seq, excluded, args.history_head)
        report = strict_json_loads((journal / f"action-{seq:04d}.body.json").read_text("utf-8"))
        out = {"odds_requested_at": odds["requested_at"], "odds_received_at": odds["received_at"],
               "action": envelope, "category": report["decisions"][-1]["category"],
               "policies": {k: {n: v[n] for n in ("assumed_open", "open_stake", "peak_tickets_sharing_fixture")}
                            for k, v in report["policies"].items()}}
    elif args.stage == "results2":
        result = capture_one(session, 2, "results")
        envelope = freeze_proposal(session, journal, 2, excluded, args.history_head)
        body = strict_json_loads((journal / "proposal-0002.body.json").read_text("utf-8"))
        out = {"results_requested_at": result["requested_at"], "results_received_at": result["received_at"],
               "proposal": envelope, "eligible_before_exclusions": body["eligible_before_exclusions"],
               "eligible_after_exclusions": body["eligible_after_exclusions"], "selected_count": len(body["selected"])}
    elif args.stage == "finish":
        result = capture_one(session, 3, "results")
        report = verify_actions(session, journal, excluded, args.history_head)
        path = root / "actions-verified.json"
        write_json(path, report)
        out = {"results_requested_at": result["requested_at"], "results_received_at": result["received_at"],
               "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "policies": {k: {n: v[n] for n in ("assumed_open", "open_stake", "peak_tickets_sharing_fixture")}
                            for k, v in report["policies"].items()}}
    else:
        history = root / "history"; history.mkdir()
        followup = root / "followup"; followup.mkdir()
        url = "https://moviodds.com/results?sport=table_tennis&date=" + args.date
        # The date is a query component, not a caller-selected arbitrary URL.
        if datetime.strptime(args.date, "%Y-%m-%d").strftime("%Y-%m-%d") != args.date:
            raise ValueError("invalid source date")
        started = now()
        raw = _get(url)
        received = now()
        obj = _parse(raw, "results")
        if obj.get("date") != args.date or received < started:
            raise ValueError("source date or receipt clock invalid")
        receipt = {"schema": "autosport.tt_dated_followup.v1", "bytes": len(raw),
                   "sha256": hashlib.sha256(raw).hexdigest(), "url": url,
                   "requested_at": started.isoformat(), "received_at": received.isoformat(),
                   "rights_verified": False, "identity_verified": False, "executable_price_verified": False}
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        # Preserve the response even if the more stringent history audit fails.
        _write_exclusive(followup / "dated-results.body.json", raw)
        _write_exclusive(followup / "dated-results.receipt.json", receipt_bytes)
        head = append_followup(session, journal, excluded, args.history_head, history, raw, receipt_bytes, expected_head=None)
        report = evaluate_history(session, journal, excluded, args.history_head, history, head)
        path = root / "revision-report.json"; write_json(path, report)
        out = {"receipt": receipt, "rows": len(obj["results"]), "source": obj.get("source"),
               "history_head": head, "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "fixture_counts": report["snapshots"][-1]["fixture_counts"],
               "policies": report["snapshots"][-1]["policies"]}
    print(json.dumps({"stage": args.stage, **out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
