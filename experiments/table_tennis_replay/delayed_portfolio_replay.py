"""Exploratory delayed-price PAPER stress, with explicitly assumed fills.

The study has already inspected its price snapshots; it is not an independent
holdout, a real wager, or a profitability claim. Decisions are made as of each
receipt, and even early known outcomes are only settled at the later follow-up.
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
from autosport.risk import PaperRiskPolicy
from canonical_history_bridge import validated_rows
from dated_result_audit import audit as validate_followup
from delay_policy_audit import _choices, _classify, _exact_snapshot, _later_price
from portfolio_replay import INITIAL, STAKE, FEE
from prospective_capture import verify_session
from record import instant


def _session_results(directory, verified):
    """Read verified observation receipts; no earlier receipt is backdated."""
    timeline = []
    for (seq, kind), receipt in sorted(verified.items()):
        if kind != "results":
            continue
        raw = (Path(directory) / f"{seq:04d}.results.body.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("session result changed after verification")
        value = strict_json_loads(raw.decode("utf-8"))
        # Keep all copies for conflict detection. No player-name join occurs.
        by_id = {}
        for row in value["results"]:
            if not isinstance(row, dict):
                raise ValueError("invalid session result row")
            identity = row.get("fixture_id")
            if isinstance(identity, str) and identity:
                by_id.setdefault(identity, []).append(row)
        timeline.append((instant(receipt["received_at"]), by_id))
    return timeline


def _seen_before(timeline, fixtures, cutoff):
    return any(any(fixture in rows for fixture in fixtures)
               for at, rows in timeline if at < cutoff)


def _stable_outcome(identity, expected, opened_at, later_row, winner, timeline):
    """No cross-ID or name-only joining; corrections stay unresolved."""
    if later_row is None:
        return None
    try:
        if (tuple(later_row.get(k) for k in ("home", "away", "league")) != expected or
                instant(later_row.get("finished_at")) < opened_at):
            return None
        signature = (later_row.get("final_score"), later_row.get("periods"))
        for at, rows in timeline:
            if at < opened_at:
                continue
            copies = rows.get(identity, [])
            if len(copies) > 1:
                return None
            for prior in copies:
                if tuple(prior.get(k) for k in ("home", "away", "league")) != expected:
                    return None
                if instant(prior.get("updated_at")) > at:
                    return None
                if prior.get("status") == "finished":
                    valid, _ = validated_rows({"sport": "table_tennis", "results": [prior]})
                    if (len(valid) != 1 or
                            (prior.get("final_score"), prior.get("periods")) != signature or
                            valid[0][1] != winner):
                        return None
        return "home" if winner == 0 else "away"
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _finish(book, tickets, followup, received_at, timeline):
    """Settlement is delayed to the follow-up receipt and requires both legs."""
    valid, _ = validated_rows(followup)
    later = {row["fixture_id"]: (row, winner) for row, winner in valid
             if instant(row["updated_at"]) <= received_at}
    counts = Counter()
    for ticket, expected_by_id in tickets:
        opening = instant(ticket.placed_at)
        outcomes = {}
        for leg in ticket.legs:
            row, winner = later.get(leg.event_id, (None, None))
            outcomes[leg.event_id] = _stable_outcome(
                leg.event_id, expected_by_id[leg.event_id], opening, row, winner, timeline)
        if any(value is None for value in outcomes.values()):
            counts["pending"] += 1
            continue
        winners = {leg.quote_key for leg in ticket.legs
                   if outcomes[leg.event_id] == leg.selection_id}
        book.settle(ticket.ticket_id, winners, settled_at=received_at.isoformat())
        counts["won" if ticket.status is TicketStatus.WON else "lost"] += 1
    return counts


def replay(session, raw, receipt):
    # This validates source, date, digest, duplicates and chronology before any
    # use of the later result. It does not prove source identity or entitlement.
    followup_audit = validate_followup(session, raw, receipt)
    received_at = instant(receipt["received_at"])
    followup = strict_json_loads(raw.decode("utf-8"))
    verified = verify_session(session)
    odds_seq = sorted(seq for seq, kind in verified if kind == "odds")
    if len(odds_seq) < 2:
        raise ValueError("at least two odds snapshots required")
    timeline = _session_results(session, verified)
    states = {name: {"book": PaperBook(INITIAL), "risk": PaperRiskPolicy(),
                     "counts": Counter(), "tickets": [], "peak_shared": 0,
                     "events": []} for name in ("no_worse", "tolerance_2pct")}
    common = Counter()
    events = []

    for left, right in zip(odds_seq, odds_seq[1:]):
        a, b = verified[(left, "odds")], verified[(right, "odds")]
        decision_at, fill_at = instant(a["received_at"]), instant(b["received_at"])
        delay = (fill_at - decision_at).total_seconds()
        if not 30 <= delay <= 60:
            common["outside_30_60_seconds"] += 1
            continue
        initial = _choices(_exact_snapshot(session, left, a))
        fixtures = sorted(initial)[:2]
        if len(fixtures) < 2:
            common["no_pair_at_decision"] += 1
            continue
        common["frozen_pairs"] += 1
        event = {"from_sequence": left, "to_sequence": right,
                 "decision_at": decision_at.isoformat(), "comparison_at": fill_at.isoformat(),
                 "fixture_ids": fixtures}
        # A result already in a previously received response invalidates the
        # frozen pair; do not replace it with another event using future data.
        if _seen_before(timeline, fixtures, decision_at):
            common["result_seen_before_proposal"] += 1
            event["category"] = "result_seen_before_proposal"
            events.append(event)
            continue
        if _seen_before(timeline, fixtures, fill_at):
            common["result_seen_before_fill"] += 1
            event["category"] = "result_seen_before_fill"
            events.append(event)
            continue
        later_state = _exact_snapshot(session, right, b)
        pairs = [_later_price(initial[i], later_state.get(i)) for i in fixtures]
        category = _classify(*pairs)
        common[category] += 1
        event["category"] = category
        events.append(event)
        for name, state in states.items():
            counts = state["counts"]
            if category == "missing_or_invalid":
                counts["quote_unavailable"] += 1
                continue
            if category == "below_98_percent" or (name == "no_worse" and
                                                   category != "at_least_100_percent"):
                counts["threshold_rejected"] += 1
                continue
            book, risk = state["book"], state["risk"]
            decision = risk.evaluate(book, STAKE)
            reserve_ok = (book.balance - FEE * (len(state["tickets"]) + 1) - STAKE >=
                          INITIAL * risk.minimum_cash_reserve_fraction)
            if not decision.allowed or not reserve_ok:
                counts["risk_rejected"] += 1
                continue
            legs = tuple(TicketLeg(i, "match_winner", "home" if initial[i][1] == initial[i][0][0]
                                   else "away", pairs[index][1], sport="table_tennis")
                         for index, i in enumerate(fixtures))
            ticket = book.open_ticket(legs, STAKE, reason="hypothetical displayed prices; no fill",
                                      placed_at=fill_at.isoformat())
            expected = {i: initial[i][0][:3] for i in fixtures}
            state["tickets"].append((ticket, expected))
            counts["assumed_fills"] += 1
            shared = Counter(leg.event_id for t, _ in state["tickets"]
                             if t.status is TicketStatus.OPEN for leg in t.legs)
            state["peak_shared"] = max(state["peak_shared"], max(shared.values(), default=0))
            state["events"].append({"from_sequence": left, "to_sequence": right,
                                    "assumed_open_at": fill_at.isoformat(),
                                    "fixture_ids": fixtures,
                                    "legs": [{"fixture_id": leg.event_id,
                                              "side": leg.selection_id,
                                              "assumed_price": str(leg.locked_odds)} for leg in legs]})

    reports = {}
    for name, state in states.items():
        book, counts = state["book"], state["counts"]
        counts.update(_finish(book, state["tickets"], followup, received_at, timeline))
        committed = book.committed_stake
        fees = FEE * counts["assumed_fills"]
        # Open stake carried at face value is merely a convenient accounting
        # convention, never a claim that pending capital can be recovered.
        gross = book.balance + committed - INITIAL
        reports[name] = {"assumed_fills": counts["assumed_fills"],
                         "threshold_rejected": counts["threshold_rejected"],
                         "quote_unavailable": counts["quote_unavailable"],
                         "risk_rejected": counts["risk_rejected"],
                         "won": counts["won"], "lost": counts["lost"], "pending": counts["pending"],
                         "hypothetical_cash": str(book.balance), "open_stake": str(committed),
                         "peak_tickets_sharing_fixture": state["peak_shared"],
                         "assumed_cost": str(fees),
                         "gross_net_pending_at_face_value": str(gross),
                         "cost_stressed_net_pending_at_face_value": str(gross - fees),
                         "open_events": state["events"]}
    return {"schema": "research.tt_delayed_paper_portfolio.v1", "common_counts": dict(common),
            "decisions": events, "policies": reports,
            "no_bet": {"assumed_fills": 0, "net": "0"}, "followup_audit": followup_audit,
            "fills_are_assumed": True, "bookmaker_acceptance_verified": False,
            "outcomes_are_source_reported_only": True, "official_finish_verified": False,
            "bookmaker_settlement_verified": False,
            "independent_holdout": False, "identity_verified": False, "rights_verified": False,
            "real_money_execution": False,
            "note": "Exploratory assumed displayed-price fills; pending stake is not liquid or marked to market."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("followup_body", type=Path)
    parser.add_argument("followup_receipt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = replay(args.session, args.followup_body.read_bytes(),
                    strict_json_loads(args.followup_receipt.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
