"""Chronological, research-only PAPER replay across overlapping sessions.

All inputs are already frozen and externally pinned. This module neither
trains nor executes a bet. Different source IDs do not establish different
physical matches, and source receipt times do not establish official results.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from itertools import groupby
from pathlib import Path

from autosport.domain import TicketStatus
from autosport.json_integrity import strict_json_loads
from canonical_history_bridge import validated_rows
from causal_h2h_longitudinal import _signature
from delayed_portfolio_replay import _stable_outcome
from joint_forward_portfolio import (POLICIES, SessionEvidence, _open, _prepare,
                                     _settle, _source_ids, _state, _summary)
from portfolio_replay import FEE, INITIAL, STAKE
from record import instant


def _group_rows(raw):
    _source_ids(raw)  # Refuse malformed identities before considering an absence.
    value = strict_json_loads(raw.decode("utf-8"))
    grouped = defaultdict(list)
    for row in value["results"]:
        grouped[row["fixture_id"]].append(row)
    return dict(grouped)


def _events(prepared):
    """Materialize validated source evidence, never promoting it to availability."""
    events = []
    for index, part in enumerate(prepared):
        results = sorted((seq, receipt) for (seq, kind), receipt in part["verified"].items()
                         if kind == "results")
        if len(results) != len(part["timeline"]):
            raise ValueError("generic receipt timeline mismatch")
        for (seq, receipt), (at, by_id) in zip(results, part["timeline"]):
            if instant(receipt["received_at"]) != at:
                raise ValueError("generic receipt timestamp mismatch")
            events.append((at, 1, index, seq, "generic", by_id))
        for at, _, seq, kind, payload in part["events"]:
            if kind == "action":
                events.append((at, 0, index, seq, kind, payload))
            else:
                snapshot, records, current = payload
                raw = (Path(part["item"].history) / f"{seq:04d}.body.json").read_bytes()
                if hashlib.sha256(raw).hexdigest() != snapshot["source_sha256"]:
                    raise ValueError("dated result changed after history verification")
                events.append((at, 1, index, seq, "dated", (records, current, _group_rows(raw))))
    return sorted(events, key=lambda e: (e[0], e[1], e[2], e[3], e[4]))


def _incompatible_with_baseline(row, baseline, received_at):
    """A positive later contradiction is enough to hold; absence proves nothing."""
    try:
        valid, _ = validated_rows({"sport": "table_tennis", "results": [row]})
        return (len(valid) != 1 or row.get("final_score") !=
                ",".join(p["score"] for p in row["periods"]) or
                row.get("status") != "finished" or
                instant(row.get("updated_at")) > received_at or
                _signature(row) != baseline)
    except (TypeError, ValueError, AttributeError, KeyError):
        return True


def _candidate_conflicts(fid, row, winner, context, timeline, now, first_open):
    """Compare only source observations received no later than this candidate."""
    if len(context["expected"]) != 1:
        return True
    expected = next(iter(context["expected"]))
    bound = context["earliest_bound"]
    if _stable_outcome(fid, expected, bound, row, winner, timeline) is None:
        return True
    signature = _signature(row)
    # The inherited bound is deliberately conservative for whether the later
    # source result can settle. Adverse evidence from another session can arrive
    # earlier, immediately after the actual frozen opening; do not skip it.
    since = min(bound, first_open) if first_open is not None else bound
    for at, source in timeline:
        if at < since:
            continue
        copies = source.get(fid, [])
        if len(copies) > 1:
            return True
        for prior in copies:
            try:
                if (tuple(prior.get(k) for k in ("home", "away", "league")) != expected or
                        instant(prior.get("updated_at")) > at):
                    return True
                if prior.get("status") == "finished":
                    valid, _ = validated_rows({"sport": "table_tennis", "results": [prior]})
                    if (len(valid) != 1 or prior.get("final_score") !=
                            ",".join(p["score"] for p in prior["periods"]) or
                            _signature(prior) != signature):
                        return True
                # A same-time unfinished and finished record cannot be ordered.
                elif at == now:
                    return True
            except (TypeError, ValueError, AttributeError, KeyError):
                return True
    return False


def _hold_settled(states, quarantined, when):
    for state in states.values():
        if state["held_at"] is not None:
            continue
        if any(entry["ticket"].status is not TicketStatus.OPEN and
               any(leg.event_id in quarantined for leg in entry["ticket"].legs)
               for entry in state["tickets"]):
            state["held_at"] = when.isoformat()


def _public_status(states, transitions):
    return {name: {"transitions": dict(sorted(transitions[name].items())), **_summary(state)}
            for name, state in states.items()}


def replay(items):
    """Replay overlapping frozen sessions with a single PAPER book per policy.

    Per-session action journals remain authoritative for frozen proposals.
    A different session's result is adverse evidence only, never sufficient
    by itself to settle a ticket. Two actions at the same instant do not get
    an invented shared-bankroll priority.
    """
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("at least one recorded session is required")
    if any(not isinstance(item, SessionEvidence) for item in items):
        raise ValueError("session evidence has an invalid type")
    prepared = [_prepare(item) for item in items]
    if len({part["history_head"] for part in prepared}) != len(prepared):
        raise ValueError("duplicate pinned session history")
    prepared.sort(key=lambda p: (p["first_request"], p["history_head"]))
    events = _events(prepared)
    states = {name: _state() for name in POLICIES}
    # Every result ID is usable as adverse prior evidence only after its receipt.
    seen, owners = set(), defaultdict(set)
    timeline, baseline, quarantined, first_open = [], {}, set(), {}
    diary = []

    for when, bucket in groupby(events, key=lambda event: event[0]):
        batch = list(bucket)
        actions = [event for event in batch if event[4] == "action"]
        receipts = [event for event in batch if event[4] != "action"]
        if actions:
            transitions = {name: Counter() for name in POLICIES}
            competing = len({event[2] for event in actions}) > 1
            # A selected ID in another session at the same timestamp is not
            # made safe by processing the sessions in a convenient order.
            at_time = defaultdict(set)
            for _, _, index, _, _, decision in actions:
                for fid in decision["fixture_ids"]:
                    at_time[fid].add(index)
            for _, _, index, seq, _, decision in actions:
                chosen = set(decision["fixture_ids"])
                collision = any((owners[fid] - {index}) or (at_time[fid] - {index}) for fid in chosen)
                for name, state in states.items():
                    frozen = decision["policies"][name]
                    if state["held_at"] is not None:
                        status = "book_held_for_review"
                    elif frozen != "assumed_open":
                        status = "frozen_" + frozen
                    elif collision:
                        status = "cross_session_exact_id_ambiguous"
                    elif competing:
                        status = "simultaneous_action_time_ambiguous"
                    else:
                        status = _open(state, prepared[index], name, seq, decision, when, seen)
                        if status == "prior_session_result_seen":
                            status = "prior_result_seen"
                    if status == "counterfactual_assumed_open":
                        for fid in chosen:
                            first_open[fid] = min(first_open.get(fid, when), when)
                    transitions[name][status] += 1
            for _, _, index, _, _, decision in actions:
                for fid in decision["fixture_ids"]:
                    owners[fid].add(index)
            diary.append({"at": when.isoformat(), "kind": "actions", "count": len(actions),
                          "policies": _public_status(states, transitions)})

        if not receipts:
            continue
        before = len(quarantined)
        dated = []
        for _, _, index, seq, kind, payload in receipts:
            if kind == "dated":
                records, current, by_id = payload
                dated.append((index, seq, records, current))
                quarantined.update(fid for fid, record in records.items() if record["state"] == "quarantined")
            else:
                by_id = payload
            seen.update(by_id)
            timeline.append((when, by_id))
        # Evaluate the whole same-time batch before admitting any settlement.
        for _, _, _, _, kind, payload in receipts:
            by_id = payload[2] if kind == "dated" else payload
            for fid in baseline.keys() & by_id.keys():
                copies = by_id[fid]
                if len(copies) != 1 or _incompatible_with_baseline(copies[0], baseline[fid], when):
                    quarantined.add(fid)
        for index, _, records, current in dated:
            contexts = prepared[index]["contexts"]
            for fid, state_record in records.items():
                if state_record["state"] != "eligible":
                    continue
                if fid not in current or fid not in contexts:
                    raise ValueError("eligible result missing its verified source context")
                row, winner = current[fid]
                signature = _signature(row)
                if (fid in baseline and baseline[fid] != signature) or _candidate_conflicts(
                        fid, row, winner, contexts[fid], timeline, when, first_open.get(fid)):
                    quarantined.add(fid)
                elif fid not in quarantined:
                    baseline[fid] = signature
        _hold_settled(states, quarantined, when)
        transitions = {name: Counter() for name in POLICIES}
        for index, _, records, current in dated:
            part = prepared[index]
            # The original adapter sees the global history only up to this
            # timestamp; its own complete future timeline is never substituted.
            part["timeline"] = list(timeline)
            adjusted = {fid: ({**record, "state": "quarantined"} if fid in quarantined else record)
                        for fid, record in records.items()}
            for name, state in states.items():
                transitions[name][_settle(state, part, when, adjusted, current)] += 1
        diary.append({"at": when.isoformat(), "kind": "receipts", "count": len(receipts),
                      "dated_receipts": len(dated), "quarantined_total": len(quarantined),
                      "newly_quarantined": len(quarantined) - before,
                      "policies": _public_status(states, transitions)})

    return {"schema": "research.tt_interleaved_forward_paper.v1",
            "session_count": len(prepared), "history_heads": [p["history_head"] for p in prepared],
            "initial_units_per_alternative": str(INITIAL), "stake_per_ticket": str(STAKE),
            "cost_per_open": str(FEE), "policies": {n: _summary(s) for n, s in states.items()},
            "events": diary, "no_bet": {"net": "0"}, "retrospective": True,
            "shared_book_actions_are_counterfactual": True,
            "different_source_ids_prove_independence": False, "identity_verified": False,
            "rights_verified": False, "bookmaker_acceptance_verified": False,
            "bookmaker_settlement_verified": False, "model_training_authorized": False,
            "real_money_execution": False, "profitability_claim": False,
            "note": "Cross-session exact-ID reuse and simultaneous actions fail closed; "
                    "unrecorded source revisions and aliases remain unknown."}
