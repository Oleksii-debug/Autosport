"""Freeze two sequential research-only paper-parlay decisions before results.

Source quotes are assumed prices, not bookmaker-accepted orders. Local receipt
timestamps are not independent clock evidence. No training or real execution.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
from autosport.json_integrity import strict_json_loads
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from canonical_history_bridge import validated_rows
from causal_h2h_observer import verify_journal
from causal_h2h_revision_history import evaluate_history
from dated_result_audit import audit as validate_followup
from delay_policy_audit import _choices, _classify, _exact_snapshot, _later_price
from delayed_portfolio_replay import _session_results, _stable_outcome
from portfolio_replay import INITIAL, STAKE, FEE
from prospective_capture import _write_exclusive, verify_session
from record import instant


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def known_prior_ids(observer, history, expected_head):
    """Validate the pinned earlier history and return only its known source IDs."""
    report = evaluate_history(observer, history, expected_head)
    last = report["snapshots"][-1]
    body = (Path(history) / f'{last["sequence"]:04d}.body.json').read_bytes()
    if _sha(body) != last["source_sha256"]:
        raise ValueError("previous dated source changed")
    earlier = strict_json_loads(body.decode("utf-8"))
    _, _, verified = verify_journal(observer)
    receipt = verified[(1, "odds")]
    raw = (Path(observer) / "snapshots/0001.odds.body.json").read_bytes()
    if _sha(raw) != receipt["sha256"]:
        raise ValueError("previous odds source changed")
    odds = strict_json_loads(raw.decode("utf-8"))
    ids = {r["fixture_id"] for r in earlier["results"]}
    ids.update(m["id"] for m in odds["matches"])
    return ids, last["as_of"]


def _excluded_sha(ids):
    return _sha((json.dumps(sorted(ids), separators=(",", ":")) + "\n").encode())


def _seen_results(session, verified, through):
    seen = set()
    for seq in range(1, through + 1):
        receipt = verified[(seq, "results")]
        raw = (Path(session) / f"{seq:04d}.results.body.json").read_bytes()
        if _sha(raw) != receipt["sha256"]:
            raise ValueError("session results changed")
        obj = strict_json_loads(raw.decode("utf-8"))
        for row in obj["results"]:
            if not isinstance(row, dict):
                raise ValueError("invalid result row")
            fid = row.get("fixture_id")
            if isinstance(fid, str) and fid:
                seen.add(fid)
    return seen


def proposal(session, seq, excluded, prior_head):
    """Deterministic selection from this odds snapshot and only earlier receipts."""
    if type(seq) is not int or seq not in (1, 2):
        raise ValueError("only two sequential proposals are supported")
    verified = verify_session(session)
    odds, results = verified[(seq, "odds")], verified[(seq, "results")]
    if not isinstance(prior_head, str) or len(prior_head) != 64:
        raise ValueError("prior history head required")
    age = (instant(results["received_at"]) - instant(odds["received_at"])).total_seconds()
    choices = _choices(_exact_snapshot(session, seq, odds))
    unavailable = set(excluded) | _seen_results(session, verified, seq)
    candidates = sorted(i for i in choices if i not in unavailable)
    if age < 0 or age > 30:
        candidates = []
    selected = []
    for fid in candidates[:2]:
        state, name, price = choices[fid]
        selected.append({"fixture_id": fid, "side": "home" if name == state[0] else "away",
                         "selection_id": state[3][name][0], "initial_price": str(price)})
    return {"schema": "research.tt_forward_portfolio_proposal.v1", "sequence": seq,
            "prior_history_head": prior_head, "prior_excluded_sha256": _excluded_sha(excluded),
            "odds_sha256": odds["sha256"], "results_sha256": results["sha256"],
            "odds_received_at": odds["received_at"], "results_received_at": results["received_at"],
            "eligible_before_exclusions": len(choices), "eligible_after_exclusions": len(candidates),
            "selected": selected, "selection_age_seconds": age}


def _record_paths(directory, kind, seq):
    stem = Path(directory) / f"{kind}-{seq:04d}"
    return Path(str(stem) + ".body.json"), Path(str(stem) + ".receipt.json")


def _read_record(directory, kind, seq):
    body_path, receipt_path = _record_paths(directory, kind, seq)
    if any(p.is_symlink() or not p.is_file() for p in (body_path, receipt_path)):
        raise ValueError("missing or unsafe journal record")
    raw = body_path.read_bytes()
    envelope = strict_json_loads(receipt_path.read_text("utf-8"))
    if (not isinstance(envelope, dict) or envelope.get("schema") != "research.tt_forward_portfolio_receipt.v1" or
            envelope.get("kind") != kind or type(envelope.get("sequence")) is not int or
            envelope["sequence"] != seq or type(envelope.get("bytes")) is not int or
            envelope["bytes"] != len(raw) or envelope.get("sha256") != _sha(raw) or
            instant(envelope.get("computed_at")) > instant(envelope.get("frozen_at"))):
        raise ValueError("journal receipt mismatch")
    return strict_json_loads(raw.decode("utf-8")), envelope


def _freeze(directory, kind, seq, body, clock):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("journal must be a non-symlink directory")
    body_path, receipt_path = _record_paths(directory, kind, seq)
    if body_path.exists() or receipt_path.exists():
        raise ValueError("journal record already exists")
    computed = clock()
    raw = (json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    _write_exclusive(body_path, raw)
    frozen = clock()
    if computed.tzinfo is None or frozen.tzinfo is None or frozen < computed:
        raise ValueError("invalid journal clock")
    envelope = {"schema": "research.tt_forward_portfolio_receipt.v1", "kind": kind,
                "sequence": seq, "bytes": len(raw), "sha256": _sha(raw),
                "computed_at": computed.isoformat(), "frozen_at": frozen.isoformat()}
    _write_exclusive(receipt_path, (json.dumps(envelope, sort_keys=True) + "\n").encode())
    return envelope


def _utc_now():
    return datetime.now(timezone.utc)


def _last_key(verified):
    return max(verified, key=lambda x: (x[0], 0 if x[1] == "odds" else 1))


def freeze_proposal(session, journal, seq, excluded, prior_head, *, clock=_utc_now):
    verified = verify_session(session)
    if _last_key(verified) != (seq, "results"):
        raise ValueError("proposal must be frozen before next odds request")
    body = proposal(session, seq, excluded, prior_head)
    env = _freeze(journal, "proposal", seq, body, clock)
    if not instant(verified[(seq, "results")]["received_at"]) <= instant(env["computed_at"]):
        raise ValueError("proposal frozen before its source receipt")
    if (instant(env["computed_at"]) - instant(verified[(seq, "odds")]["received_at"])).total_seconds() > 30:
        raise ValueError("proposal was computed more than 30 seconds after its quote")
    return env


def _make_states():
    return {name: {"book": PaperBook(INITIAL), "risk": PaperRiskPolicy(),
                   "tickets": [], "peak_shared": 0, "events": []}
            for name in ("no_worse", "tolerance_2pct")}


def _summary(states, decisions, proposal_shas, through):
    policies = {}
    for name, state in states.items():
        book = state["book"]
        policies[name] = {"assumed_open": len(state["tickets"]),
                          "hypothetical_cash": str(book.balance),
                          "open_stake": str(book.committed_stake),
                          "peak_tickets_sharing_fixture": state["peak_shared"],
                          "assumed_cost_if_opened": str(FEE * len(state["tickets"])),
                          "open_events": list(state["events"])}
    return {"schema": "research.tt_forward_paper_actions.v1", "through_transition": through,
            "proposal_sha256": list(proposal_shas), "decisions": list(decisions),
            "policies": policies, "no_bet": {"assumed_open": 0},
            "outcomes_evaluated": False, "displayed_prices_are_assumed": True,
            "bookmaker_acceptance_verified": False, "rights_verified": False,
            "identity_verified": False, "independent_holdout": False,
            "model_training_authorized": False, "real_money_execution": False,
            "note": "Hypothetical quote-snapshot accounting; all opened stakes are pending."}


def _replay(session, journal, excluded, prior_head, through):
    verified = verify_session(session)
    states, decisions, proposal_shas = _make_states(), [], []
    for seq in range(1, through + 1):
        proposed, penv = _read_record(journal, "proposal", seq)
        expected = proposal(session, seq, excluded, prior_head)
        if proposed != expected:
            raise ValueError("frozen proposal differs from causal source selection")
        left, right = verified[(seq, "odds")], verified[(seq + 1, "odds")]
        if (instant(penv["computed_at"]) < instant(verified[(seq, "results")]["received_at"]) or
                (instant(penv["computed_at"]) - instant(left["received_at"])).total_seconds() > 30 or
                instant(penv["frozen_at"]) >= instant(right["requested_at"])):
            raise ValueError("proposal was not frozen before next odds request")
        proposal_shas.append(penv["sha256"])
        selected = proposed["selected"]
        delay = (instant(right["received_at"]) - instant(left["received_at"])).total_seconds()
        event = {"from_sequence": seq, "to_sequence": seq + 1,
                 "quote_received_at": right["received_at"], "delay_seconds": delay,
                 "fixture_ids": [x["fixture_id"] for x in selected], "policies": {}}
        original = _choices(_exact_snapshot(session, seq, left))
        if len(selected) < 2:
            category, pairs = "no_pair", []
        elif not 30 <= delay <= 60:
            category, pairs = "outside_30_60_seconds", []
        else:
            later = _exact_snapshot(session, seq + 1, right)
            pairs = [_later_price(original[x["fixture_id"]], later.get(x["fixture_id"])) for x in selected]
            category = _classify(*pairs)
        event["category"] = category
        for name, state in states.items():
            if category in ("no_pair", "outside_30_60_seconds", "missing_or_invalid"):
                status = category
            elif category == "below_98_percent" or (name == "no_worse" and category != "at_least_100_percent"):
                status = "threshold_rejected"
            else:
                book, risk = state["book"], state["risk"]
                reserve = book.balance - FEE * (len(state["tickets"]) + 1) - STAKE
                if not risk.evaluate(book, STAKE).allowed or reserve < INITIAL * risk.minimum_cash_reserve_fraction:
                    status = "risk_rejected"
                else:
                    legs = tuple(TicketLeg(x["fixture_id"], "match_winner", x["side"], pairs[i][1],
                                           sport="table_tennis") for i, x in enumerate(selected))
                    ticket = book.open_ticket(legs, STAKE, reason="hypothetical snapshot, no accepted fill",
                                              placed_at=right["received_at"])
                    state["tickets"].append(ticket)
                    shared = Counter(leg.event_id for t in state["tickets"] if t.status is TicketStatus.OPEN
                                     for leg in t.legs)
                    state["peak_shared"] = max(state["peak_shared"], max(shared.values(), default=0))
                    state["events"].append({"transition": seq, "quote_received_at": right["received_at"],
                                            "legs": [{"fixture_id": x["fixture_id"], "side": x["side"],
                                                      "assumed_price": str(pairs[i][1])}
                                                     for i, x in enumerate(selected)]})
                    status = "assumed_open"
            event["policies"][name] = status
        decisions.append(event)
        report = _summary(states, decisions, proposal_shas, seq)
        if seq < through:
            previous, aenv = _read_record(journal, "action", seq)
            if previous != report:
                raise ValueError("previous action differs from causal replay")
            if (instant(aenv["computed_at"]) < instant(right["received_at"]) or
                    (instant(aenv["computed_at"]) - instant(right["received_at"])).total_seconds() > 30 or
                    instant(aenv["frozen_at"]) >= instant(verified[(seq + 1, "results")]["requested_at"])):
                raise ValueError("action was not frozen before next results request")
    return report


def freeze_action(session, journal, seq, excluded, prior_head, *, clock=_utc_now):
    verified = verify_session(session)
    if _last_key(verified) != (seq + 1, "odds"):
        raise ValueError("action must be frozen before next results request")
    report = _replay(session, journal, excluded, prior_head, seq)
    env = _freeze(journal, "action", seq, report, clock)
    if instant(env["computed_at"]) < instant(verified[(seq + 1, "odds")]["received_at"]):
        raise ValueError("action frozen before its source receipt")
    if (instant(env["computed_at"]) - instant(verified[(seq + 1, "odds")]["received_at"])).total_seconds() > 30:
        raise ValueError("action was computed more than 30 seconds after its quote")
    return env


def verify_actions(session, journal, excluded, prior_head, through=2):
    verified = verify_session(session)
    report = _replay(session, journal, excluded, prior_head, through)
    stored, env = _read_record(journal, "action", through)
    if stored != report:
        raise ValueError("final action differs from causal replay")
    if (instant(env["computed_at"]) < instant(verified[(through + 1, "odds")]["received_at"]) or
            (instant(env["computed_at"]) - instant(verified[(through + 1, "odds")]["received_at"])).total_seconds() > 30 or
            instant(env["frozen_at"]) >= instant(verified[(through + 1, "results")]["requested_at"])):
        raise ValueError("action was not frozen before next results request")
    return report


def settle_actions(session, journal, excluded, prior_head, raw, receipt):
    """Settle only source-confirmed paper legs, never before follow-up receipt.

    The next results-request start is used as a conservative upper bound on
    publication of each decision, not as an official match-finish time.
    """
    report = verify_actions(session, journal, excluded, prior_head)
    audit = validate_followup(session, raw, receipt)
    source = strict_json_loads(raw.decode("utf-8"))
    valid, _ = validated_rows(source)
    received = instant(receipt["received_at"])
    complete = {row["fixture_id"]: (row, winner) for row, winner in valid
                if instant(row["updated_at"]) <= received and
                row.get("final_score") == ",".join(p["score"] for p in row["periods"])}
    verified = verify_session(session)
    timeline = _session_results(session, verified)
    policies = {}
    for name, p in report["policies"].items():
        book, tickets = PaperBook(INITIAL), []
        for event in p["open_events"]:
            seq = event["transition"]
            bound = verified[(seq + 1, "results")]["requested_at"]
            initial = _exact_snapshot(session, seq, verified[(seq, "odds")])
            legs = tuple(TicketLeg(leg["fixture_id"], "match_winner", leg["side"],
                                   Decimal(leg["assumed_price"]), sport="table_tennis")
                         for leg in event["legs"])
            ticket = book.open_ticket(legs, STAKE, reason="hypothetical snapshot, no accepted fill",
                                      placed_at=bound)
            expected = {leg.event_id: initial[leg.event_id][:3] for leg in legs}
            tickets.append((ticket, expected))
        counts = Counter()
        for ticket, expected in tickets:
            outcomes = {}
            for leg in ticket.legs:
                row, winner = complete.get(leg.event_id, (None, None))
                outcomes[leg.event_id] = _stable_outcome(leg.event_id, expected[leg.event_id],
                                                          instant(ticket.placed_at), row, winner, timeline)
            if any(value is None for value in outcomes.values()):
                counts["pending"] += 1
                continue
            winners = {leg.quote_key for leg in ticket.legs if outcomes[leg.event_id] == leg.selection_id}
            book.settle(ticket.ticket_id, winners, settled_at=receipt["received_at"])
            counts["won" if ticket.status is TicketStatus.WON else "lost"] += 1
        fees = FEE * len(tickets)
        gross = book.balance + book.committed_stake - INITIAL
        policies[name] = {"assumed_open": len(tickets), "won": counts["won"], "lost": counts["lost"],
                          "pending": counts["pending"], "hypothetical_cash": str(book.balance),
                          "open_stake": str(book.committed_stake), "assumed_cost": str(fees),
                          "gross_net_pending_at_face_value": str(gross),
                          "cost_stressed_net_pending_at_face_value": str(gross - fees)}
    return {"schema": "research.tt_forward_paper_settlement.v1", "actions": report,
            "followup_audit": audit, "policies": policies, "no_bet": {"net": "0"},
            "outcomes_source_reported_only": True, "official_finish_verified": False,
            "bookmaker_settlement_verified": False, "bookmaker_acceptance_verified": False,
            "identity_verified": False, "rights_verified": False, "independent_holdout": False,
            "model_training_authorized": False, "real_money_execution": False,
            "profitability_claim": False,
            "note": "Pending stake carried at face value, not liquid or marked to market."}
