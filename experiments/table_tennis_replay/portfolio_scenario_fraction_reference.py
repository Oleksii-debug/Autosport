"""Independent rational arithmetic for a bounded synthetic win/loss grid.

Uses the same already-selected PAPER snapshot; it cannot verify its inputs,
identities, event independence, execution, probabilities or bookmaker rules.
"""
from __future__ import annotations

from fractions import Fraction
from itertools import product

from autosport.portfolio import _snapshot_open_tickets_for_analysis

REFERENCE_CAP = 8192


def calculate(tickets):
    """Enumerate the same abstract outcomes with exact Fraction arithmetic."""
    opened = _snapshot_open_tickets_for_analysis(tickets)
    events = {}
    for ticket in opened:
        for leg in ticket.legs:
            events.setdefault(leg.event_id, set()).add(leg.quote_key)
    choices = [tuple(sorted(keys)) + (None,) for _, keys in sorted(events.items())]
    size = 1
    for alternatives in choices:
        size *= len(alternatives)
    if size > REFERENCE_CAP:
        return {"status": "skipped_reference_cap", "states": size, "min": None, "max": None,
                "one_event_max_drop": None}

    def profit(winners):
        total = Fraction(0)
        for ticket in opened:
            stake = Fraction(ticket.stake)
            if all(leg.quote_key in winners for leg in ticket.legs):
                payout = stake
                for leg in ticket.legs:
                    payout *= Fraction(leg.locked_odds)
                total += payout - stake
            else:
                total -= stake
        return total

    scenarios = [profit(set(choice) - {None}) for choice in product(*choices)]
    max_drop = None
    if opened and all(len(keys) == 1 for keys in events.values()):
        winners = {next(iter(keys)) for keys in events.values()}
        all_win = profit(winners)
        max_drop = max(all_win - profit(winners - {next(iter(keys))}) for keys in events.values())
    return {"status": "exact_fraction", "states": len(scenarios), "min": min(scenarios),
            "max": max(scenarios), "one_event_max_drop": max_drop}


def calculate_single_void(tickets):
    """Independently enumerate paired one-event void effects by side labels."""
    opened = _snapshot_open_tickets_for_analysis(tickets)
    events = {}
    for ticket in opened:
        for leg in ticket.legs:
            events.setdefault(leg.event_id, set()).add(leg.selection_id)
    ids = sorted(events)
    options = [tuple(sorted(events[event])) + (None,) for event in ids]
    states = 1
    for values in options:
        states *= len(values)
    comparisons = states * len(ids)
    if comparisons > REFERENCE_CAP:
        return {"status": "skipped_reference_cap", "comparisons": comparisons}
    if not ids:
        return {"status": "no_open_events", "comparisons": 0}

    def pnl(outcomes, voided):
        total = Fraction(0)
        for ticket in opened:
            stake = Fraction(ticket.stake)
            remaining = [leg for leg in ticket.legs if leg.event_id != voided]
            if any(outcomes[leg.event_id] != leg.selection_id for leg in remaining):
                total -= stake
                continue
            payout = stake
            for leg in remaining:
                payout *= Fraction(leg.locked_odds)
            total += payout - stake
        return total

    deltas, void_values = [], []
    signs = {"negative": 0, "zero": 0, "positive": 0}
    for assignment in product(*options):
        outcomes = dict(zip(ids, assignment))
        old = pnl(outcomes, None)
        for event in ids:
            new = pnl(outcomes, event)
            delta = new - old
            deltas.append(delta)
            void_values.append(new)
            signs["positive" if delta > 0 else "negative" if delta < 0 else "zero"] += 1
    return {"status": "exact_fraction", "comparisons": len(deltas),
            "delta_min": min(deltas), "delta_max": max(deltas),
            "void_min": min(void_values), "void_max": max(void_values), "sign_counts": signs}
