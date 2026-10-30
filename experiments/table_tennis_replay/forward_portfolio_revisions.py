"""Reconcile dated source revisions for frozen, hypothetical PAPER tickets.

The externally supplied latest hash protects only the receipts actually recorded
in this history. It cannot prove source truth, missing unrecorded responses,
clock accuracy, participant identity, accepted prices or real-money settlement.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
from autosport.json_integrity import strict_json_loads
from autosport.paper import PaperBook
from canonical_history_bridge import validated_rows
from causal_h2h_longitudinal import _signature
from causal_h2h_revision_history import MAX_RECEIPTS, _read_history
from dated_result_audit import audit as validate_followup
from delay_policy_audit import _exact_snapshot
from delayed_portfolio_replay import _session_results, _stable_outcome
from forward_portfolio_journal import _read_record, verify_actions
from portfolio_replay import INITIAL, STAKE, FEE
from prospective_capture import _write_exclusive, verify_session
from record import instant


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _contexts(session, actions, verified):
    contexts = {}
    for policy in actions["policies"].values():
        for event in policy["open_events"]:
            seq = event["transition"]
            bound = instant(verified[(seq + 1, "results")]["requested_at"])
            initial = _exact_snapshot(session, seq, verified[(seq, "odds")])
            for leg in event["legs"]:
                fid = leg["fixture_id"]
                expected = initial[fid][:3]
                entry = contexts.setdefault(fid, {"expected": set(), "earliest_bound": bound})
                entry["expected"].add(expected)
                entry["earliest_bound"] = min(entry["earliest_bound"], bound)
    return contexts


def _policy_report(session, actions, contexts, states, current, verified, timeline, received_at):
    result = {}
    for name, policy in actions["policies"].items():
        book, tickets = PaperBook(INITIAL), []
        for event in policy["open_events"]:
            bound = verified[(event["transition"] + 1, "results")]["requested_at"]
            legs = tuple(TicketLeg(leg["fixture_id"], "match_winner", leg["side"],
                                   Decimal(leg["assumed_price"]), sport="table_tennis")
                         for leg in event["legs"])
            ticket = book.open_ticket(legs, STAKE, reason="hypothetical snapshot, no accepted fill",
                                      placed_at=bound)
            tickets.append(ticket)
        counts = Counter()
        for ticket in tickets:
            outcomes = {}
            for leg in ticket.legs:
                fid = leg.event_id
                if states[fid]["state"] != "eligible" or fid not in current:
                    outcomes[fid] = None
                    continue
                row, winner = current[fid]
                expected = next(iter(contexts[fid]["expected"]))
                outcomes[fid] = _stable_outcome(fid, expected, instant(ticket.placed_at), row, winner, timeline)
            if any(v is None for v in outcomes.values()):
                counts["pending"] += 1
                continue
            winners = {leg.quote_key for leg in ticket.legs if outcomes[leg.event_id] == leg.selection_id}
            book.settle(ticket.ticket_id, winners, settled_at=received_at)
            counts["won" if ticket.status is TicketStatus.WON else "lost"] += 1
        fees = FEE * len(tickets)
        gross = book.balance + book.committed_stake - INITIAL
        result[name] = {"assumed_open": len(tickets), "won": counts["won"],
                        "lost": counts["lost"], "pending": counts["pending"],
                        "hypothetical_cash": str(book.balance), "open_stake": str(book.committed_stake),
                        "assumed_cost": str(fees), "gross_net_pending_at_face_value": str(gross),
                        "cost_stressed_net_pending_at_face_value": str(gross - fees)}
    return result


def evaluate_history(session, journal, excluded, prior_head, directory, expected_head):
    """Replay all supplied receipts, retaining earlier reports and quarantine."""
    actions = verify_actions(session, journal, excluded, prior_head)
    _, action_receipt = _read_record(journal, "action", 2)
    action_sha = action_receipt["sha256"]
    checked = _read_history(directory, expected_head, action_sha)
    verified = verify_session(session)
    timeline = _session_results(session, verified)
    contexts = _contexts(session, actions, verified)
    states = {}
    for fid, context in contexts.items():
        conflict = len(context["expected"]) != 1
        states[fid] = {"state": "quarantined" if conflict else "unresolved",
                       "reason": "conflicting_frozen_source_labels" if conflict else None,
                       "unqualified_finished_seen": False, "bad_identity_seen": False,
                       "signature": None, "first_eligible_at": None, "quarantined_at": None}
    snapshots, previous_receipt, previous_date = [], None, None
    for seq, raw, receipt, link_sha in checked:
        if previous_receipt and instant(previous_receipt["received_at"]) >= instant(receipt.get("requested_at")):
            raise ValueError("later dated request must follow previous receipt")
        # Actions were validated once above; the existing dated auditor checks
        # each response without rebuilding the same paper books for every row.
        audit = validate_followup(session, raw, receipt)
        if previous_date is not None and previous_date != audit["source_date"]:
            raise ValueError("dated responses have different source dates")
        source = strict_json_loads(raw.decode("utf-8"))
        valid, _ = validated_rows(source)
        reported = {row["fixture_id"]: row for row in source["results"]}
        available = instant(receipt["received_at"])
        complete = {row["fixture_id"]: (row, winner) for row, winner in valid
                    if instant(row["updated_at"]) <= available and
                    row.get("final_score") == ",".join(p["score"] for p in row["periods"])}
        current = {}
        for fid, context in contexts.items():
            state = states[fid]
            if state["state"] == "quarantined":
                continue
            expected = next(iter(context["expected"]))
            row = reported.get(fid)
            accepted = None
            if fid in complete:
                candidate, winner = complete[fid]
                side = _stable_outcome(fid, expected, context["earliest_bound"], candidate, winner, timeline)
                if side is not None:
                    accepted = (candidate, winner)
            if state["state"] == "eligible":
                if row is None:
                    reason = "previously_eligible_missing"
                elif accepted is None:
                    reason = "previously_eligible_now_invalid"
                elif _signature(accepted[0]) != state["signature"]:
                    reason = "previously_eligible_changed"
                else:
                    reason = None
                if reason:
                    state.update(state="quarantined", reason=reason, quarantined_at=receipt["received_at"])
                else:
                    current[fid] = accepted
            elif accepted is not None:
                if state["unqualified_finished_seen"] or state["bad_identity_seen"]:
                    state.update(state="quarantined", reason="earlier_conflicting_or_ineligible_result",
                                 quarantined_at=receipt["received_at"])
                else:
                    state.update(state="eligible", signature=_signature(accepted[0]),
                                 first_eligible_at=receipt["received_at"])
                    current[fid] = accepted
            elif row is not None:
                if row.get("status") == "finished":
                    state["unqualified_finished_seen"] = True
                if tuple(row.get(k) for k in ("home", "away", "league")) != expected:
                    state["bad_identity_seen"] = True
        counts = Counter(v["state"] for v in states.values())
        records = [{"fixture_id": fid, "state": states[fid]["state"],
                    "reason": states[fid]["reason"], "first_eligible_at": states[fid]["first_eligible_at"],
                    "quarantined_at": states[fid]["quarantined_at"]} for fid in sorted(states)]
        policies = _policy_report(session, actions, contexts, states, current, verified, timeline,
                                  receipt["received_at"])
        snapshots.append({"sequence": seq, "link_sha256": link_sha, "source_sha256": receipt["sha256"],
                          "as_of": receipt["received_at"], "source_audit": audit,
                          "fixture_counts": {k: counts[k] for k in ("eligible", "quarantined", "unresolved")},
                          "fixture_records": records, "policies": policies})
        previous_receipt, previous_date = receipt, audit["source_date"]
    return {"schema": "research.tt_forward_portfolio_revision_history.v1", "action_sha256": action_sha,
            "history_head_sha256": expected_head, "actions": actions, "snapshots": snapshots,
            "no_bet": {"net": "0"}, "outcomes_source_reported_only": True,
            "identity_verified": False, "rights_verified": False, "independent_holdout": False,
            "bookmaker_settlement_verified": False, "bookmaker_acceptance_verified": False,
            "model_training_authorized": False, "real_money_execution": False, "profitability_claim": False,
            "note": "Quarantined or unknown legs keep the paper ticket pending; "
                    "hash links cannot prove omitted source reads, source truth or pin freshness."}


def append_followup(session, journal, excluded, prior_head, directory, raw, receipt_bytes, *, expected_head):
    """Append an immutable body/receipt/link triplet to a single-writer history."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("history must be a non-symlink directory")
    if list(directory.iterdir()):
        previous = evaluate_history(session, journal, excluded, prior_head, directory, expected_head)
        last = previous["snapshots"][-1]
        sequence = last["sequence"] + 1
        previous_at, previous_date = instant(last["as_of"]), last["source_audit"]["source_date"]
    else:
        if expected_head is not None:
            raise ValueError("empty history cannot match an existing head")
        verify_actions(session, journal, excluded, prior_head)
        sequence, previous_at, previous_date = 1, None, None
    _, action_receipt = _read_record(journal, "action", 2)
    if sequence > MAX_RECEIPTS:
        raise ValueError("history exceeds supported sequence")
    receipt = strict_json_loads(receipt_bytes.decode("utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("invalid history receipt")
    audit = validate_followup(session, raw, receipt)
    if previous_at is not None and previous_at >= instant(receipt.get("requested_at")):
        raise ValueError("later dated request must follow previous receipt")
    if previous_date is not None and previous_date != audit["source_date"]:
        raise ValueError("dated responses have different source dates")
    link = {"schema": "research.tt_result_history_link.v1", "sequence": sequence,
            # Reuse the link structure; this SHA binds the frozen action journal, not a forecast.
            "forecast_sha256": action_receipt["sha256"], "previous_link_sha256": expected_head,
            "body_sha256": _sha(raw), "receipt_sha256": _sha(receipt_bytes)}
    link_bytes = (json.dumps(link, sort_keys=True, separators=(",", ":")) + "\n").encode()
    stem = f"{sequence:04d}"
    _write_exclusive(directory / f"{stem}.body.json", raw)
    _write_exclusive(directory / f"{stem}.receipt.json", receipt_bytes)
    _write_exclusive(directory / f"{stem}.link.json", link_bytes)
    return _sha(link_bytes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("journal", type=Path)
    parser.add_argument("excluded_json", type=Path)
    parser.add_argument("prior_head")
    parser.add_argument("history", type=Path)
    parser.add_argument("expected_head")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    exclusions = strict_json_loads(args.excluded_json.read_text("utf-8"))
    result = evaluate_history(args.session, args.journal, set(exclusions["ids"]), args.prior_head,
                              args.history, args.expected_head)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
