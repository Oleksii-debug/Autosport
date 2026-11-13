"""Retrospective, non-overlapping recorded sessions on shared PAPER books.

This research replay validates frozen actions and dated revision histories,
then reuses the existing PAPER accounting and risk policy. It has no model,
order, bookmaker, production-book or real-money authority. Different source
IDs are not proof of different matches or players.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import hashlib
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
from autosport.json_integrity import strict_json_loads
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from canonical_history_bridge import validated_rows
from delayed_portfolio_replay import _session_results, _stable_outcome
from forward_portfolio_journal import _read_record
from forward_portfolio_revisions import _contexts, evaluate_history
from portfolio_replay import FEE, INITIAL, STAKE
from prospective_capture import verify_session
from record import instant

POLICIES = ("no_worse", "tolerance_2pct")


@dataclass(frozen=True)
class SessionEvidence:
    session: Path
    journal: Path
    excluded: frozenset[str]
    prior_head: str
    history: Path
    history_head: str


def _source_ids(raw):
    body = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("invalid recorded result body")
    ids = set()
    for row in body["results"]:
        if not isinstance(row, dict):
            raise ValueError("invalid prior result row")
        identity = row.get("fixture_id")
        if not isinstance(identity, str) or not identity or identity != identity.strip():
            raise ValueError("unusable prior result identity")
        ids.add(identity)
    return ids


def _prepare(item):
    report = evaluate_history(item.session, item.journal, item.excluded,
                              item.prior_head, item.history, item.history_head)
    snapshots = report["snapshots"]
    if not snapshots:
        raise ValueError("a pinned dated history is required for each session")
    verified = verify_session(item.session)
    actions = report["actions"]
    contexts = _contexts(item.session, actions, verified)
    timeline = _session_results(item.session, verified)
    events = []
    for decision in actions["decisions"]:
        seq = decision["from_sequence"]
        _, receipt = _read_record(item.journal, "action", seq)
        when = instant(receipt["frozen_at"])
        events.append((when, 0, seq, "action", decision))
    seen_result_ids = set()
    for (seq, kind), receipt in verified.items():
        if kind == "results":
            raw = (Path(item.session) / f"{seq:04d}.results.body.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                raise ValueError("session result changed after verification")
            seen_result_ids.update(_source_ids(raw))
    for snapshot in snapshots:
        seq = snapshot["sequence"]
        raw = (Path(item.history) / f"{seq:04d}.body.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot["source_sha256"]:
            raise ValueError("dated result changed after history verification")
        seen_result_ids.update(_source_ids(raw))
        source = strict_json_loads(raw.decode("utf-8"))
        valid, _ = validated_rows(source)
        available = instant(snapshot["as_of"])
        current = {row["fixture_id"]: (row, winner) for row, winner in valid
                   if instant(row["updated_at"]) <= available and
                   row.get("final_score") == ",".join(p["score"] for p in row["periods"])}
        records = {r["fixture_id"]: r for r in snapshot["fixture_records"]}
        if len(records) != len(snapshot["fixture_records"]):
            raise ValueError("duplicate fixture in revision state")
        events.append((available, 1, seq, "receipt", (snapshot, records, current)))
    return {"item": item, "verified": verified, "actions": actions, "contexts": contexts,
            "timeline": timeline, "events": sorted(events), "seen_result_ids": seen_result_ids,
            "first_request": instant(verified[(1, "odds")]["requested_at"]),
            "last_receipt": instant(snapshots[-1]["as_of"]),
            "history_head": report["history_head_sha256"]}


def _state():
    return {"book": PaperBook(INITIAL), "risk": PaperRiskPolicy(), "tickets": [],
            "opened": 0, "won": 0, "lost": 0, "held_at": None,
            "peak_open_tickets": 0, "peak_tickets_sharing_exact_id": 0,
            "peak_equity": INITIAL, "max_drawdown": Decimal(0)}


def _measure(state):
    book = state["book"]
    open_tickets = [record for record in state["tickets"] if record["ticket"].status is TicketStatus.OPEN]
    shared = Counter(leg.event_id for record in open_tickets for leg in record["ticket"].legs)
    state["peak_open_tickets"] = max(state["peak_open_tickets"], len(open_tickets))
    state["peak_tickets_sharing_exact_id"] = max(state["peak_tickets_sharing_exact_id"],
                                                  max(shared.values(), default=0))
    equity = book.balance + book.committed_stake - FEE * state["opened"]
    state["peak_equity"] = max(state["peak_equity"], equity)
    state["max_drawdown"] = max(state["max_drawdown"], state["peak_equity"] - equity)


def _summary(state):
    held = state["held_at"] is not None
    book = state["book"]
    return {"opened": state["opened"], "won": None if held else state["won"],
            "lost": None if held else state["lost"],
            "pending": None if held else state["opened"] - state["won"] - state["lost"],
            "settled_before_hold": ({"won": state["won"], "lost": state["lost"]} if held else None),
            "held_at": state["held_at"],
            "cash_excluding_cost": None if held else str(book.balance),
            "pending_stake": None if held else str(book.committed_stake),
            "assumed_cost": None if held else str(FEE * state["opened"]),
            "net_cost_stressed_pending_at_face_value": None if held else
                str(book.balance + book.committed_stake - FEE * state["opened"] - INITIAL),
            "max_drawdown_pending_at_face_value": None if held else str(state["max_drawdown"]),
            "peak_open_tickets": state["peak_open_tickets"],
            "peak_tickets_sharing_exact_id": state["peak_tickets_sharing_exact_id"]}


def _open(state, prepared, name, seq, decision, when, previous_seen):
    if state["held_at"] is not None:
        return "book_held_for_review"
    frozen = decision["policies"][name]
    if frozen != "assumed_open":
        return "frozen_" + frozen
    candidates = [e for e in prepared["actions"]["policies"][name]["open_events"]
                  if e["transition"] == seq]
    if len(candidates) != 1:
        raise ValueError("frozen open action missing or duplicated")
    event = candidates[0]
    age = (when - instant(event["quote_received_at"])).total_seconds()
    if not 0 <= age <= 30:
        return "quote_stale_at_frozen_action"
    bound = instant(prepared["verified"][(seq + 1, "results")]["requested_at"])
    if when >= bound:
        raise ValueError("frozen action must precede its subsequent result request")
    if any(leg["fixture_id"] in previous_seen for leg in event["legs"]):
        return "prior_session_result_seen"
    book, risk = state["book"], state["risk"]
    if not risk.evaluate(book, STAKE).allowed:
        return "shared_book_risk_rejected"
    reserve = book.balance - FEE * (state["opened"] + 1) - STAKE
    if reserve < INITIAL * risk.minimum_cash_reserve_fraction:
        return "shared_book_fee_reserve_rejected"
    legs = tuple(TicketLeg(leg["fixture_id"], "match_winner", leg["side"],
                           Decimal(leg["assumed_price"]), sport="table_tennis")
                 for leg in event["legs"])
    ticket = book.open_ticket(legs, STAKE, reason="assumed recorded quote, not accepted fill",
                              placed_at=when.isoformat())
    state["tickets"].append({"ticket": ticket, "prepared": prepared, "bound": bound})
    state["opened"] += 1
    _measure(state)
    return "counterfactual_assumed_open"


def _settle(state, prepared, when, records, current):
    if state["held_at"] is not None:
        return "book_held_for_review"
    quarantined = {fid for fid, row in records.items() if row["state"] == "quarantined"}
    relevant = [t for t in state["tickets"] if t["prepared"] is prepared]
    for entry in relevant:
        ticket = entry["ticket"]
        if ticket.status is not TicketStatus.OPEN and any(leg.event_id in quarantined for leg in ticket.legs):
            state["held_at"] = when.isoformat()
            return "book_held_after_settled_result_correction"
    settled = 0
    for entry in relevant:
        ticket = entry["ticket"]
        if ticket.status is not TicketStatus.OPEN:
            continue
        outcomes = {}
        for leg in ticket.legs:
            fid = leg.event_id
            context = prepared["contexts"].get(fid)
            if (records.get(fid, {}).get("state") != "eligible" or fid not in current or
                    not context or len(context["expected"]) != 1):
                outcomes[fid] = None
                continue
            row, winner = current[fid]
            outcomes[fid] = _stable_outcome(fid, next(iter(context["expected"])), entry["bound"],
                                             row, winner, prepared["timeline"])
        if any(value is None for value in outcomes.values()):
            continue
        winners = {leg.quote_key for leg in ticket.legs if outcomes[leg.event_id] == leg.selection_id}
        state["book"].settle(ticket.ticket_id, winners, settled_at=when.isoformat())
        state["won"] += int(ticket.status is TicketStatus.WON)
        state["lost"] += int(ticket.status is TicketStatus.LOST)
        settled += 1
    _measure(state)
    return "settled_" + str(settled)


def replay(items):
    """Replay complete, externally pinned, strictly non-overlapping sessions.

    Actions are frozen per-session choices, not decisions actually made with
    this shared book. The shared risk check is a retrospective counterfactual.
    Selected exact IDs reused across sessions are rejected rather than merged.
    """
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("at least one recorded session is required")
    states = {name: _state() for name in POLICIES}
    prior_seen, prior_selected = set(), set()
    last_receipt = None
    diary, heads = [], []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, SessionEvidence):
            raise ValueError("session evidence has an invalid type")
        prepared = _prepare(item)
        if last_receipt is not None and last_receipt >= prepared["first_request"]:
            raise ValueError("session capture and history windows must not overlap")
        heads.append(prepared["history_head"])
        selected_here = set()
        for when, _, seq, kind, value in prepared["events"]:
            if kind == "action":
                ids = set(value["fixture_ids"])
                if ids & prior_selected:
                    raise ValueError("selected exact source ID reused across sessions")
                selected_here.update(ids)
                status = {name: _open(state, prepared, name, seq, value, when, prior_seen)
                          for name, state in states.items()}
            else:
                snapshot, records, current = value
                status = {name: _settle(state, prepared, when, records, current)
                          for name, state in states.items()}
            diary.append({"session": index, "sequence": seq, "kind": kind, "at": when.isoformat(),
                          "policies": {name: {"transition": status[name], **_summary(state)}
                                       for name, state in states.items()}})
        prior_selected.update(selected_here)
        prior_seen.update(prepared["seen_result_ids"])
        last_receipt = prepared["last_receipt"]
    return {"schema": "research.tt_joint_forward_paper.v1", "session_count": len(items),
            "history_heads": heads, "initial_units_per_alternative": str(INITIAL),
            "stake_per_ticket": str(STAKE), "cost_per_open": str(FEE),
            "policies": {name: _summary(state) for name, state in states.items()},
            "events": diary, "no_bet": {"net": "0"},
            "frozen_actions_were_originally_separate_books": True,
            "shared_book_actions_are_counterfactual": True, "retrospective": True,
            "pending_stake_is_face_value_not_market_value": True,
            "different_source_ids_prove_independence": False, "identity_verified": False,
            "rights_verified": False, "bookmaker_acceptance_verified": False,
            "bookmaker_settlement_verified": False, "model_training_authorized": False,
            "real_money_execution": False, "profitability_claim": False,
            "note": "Session and dated receipt coverage is bounded; held books require manual review."}
