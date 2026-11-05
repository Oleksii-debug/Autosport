"""Independent rational enumeration of one hypothetical two-ID linkage.

This is a check on arithmetic and the conditional grid, not a source of
identity evidence or bookmaker rules. The inputs are the same PAPER tickets.
"""
from fractions import Fraction
from itertools import product


def calculate(tickets, first, second, reverse):
    sides = {}
    for ticket in tickets:
        for leg in ticket.legs:
            sides.setdefault(leg.event_id, set()).add(leg.selection_id)
    events = sorted(sides)
    options = [tuple(sorted(sides[event])) + (None,) for event in events]

    def possible(event, selected):
        if selected is not None:
            return {selected}
        return {"home", "away", None} - sides[event]

    def flip(side):
        if side is None:
            return None
        return "away" if side == "home" else "home"

    values = []
    could_win = set()
    for choice in product(*options):
        chosen = dict(zip(events, choice))
        left = possible(first, chosen[first])
        right = possible(second, chosen[second])
        if reverse:
            right = {flip(side) for side in right}
        if not left & right:
            continue
        value = Fraction(0)
        for index, ticket in enumerate(tickets):
            stake = Fraction(ticket.stake)
            if all(chosen[leg.event_id] == leg.selection_id for leg in ticket.legs):
                could_win.add(index)
                payout = stake
                for leg in ticket.legs:
                    payout *= Fraction(leg.locked_odds)
                value += payout - stake
            else:
                value -= stake
        values.append(value)
    if not values:
        raise ValueError("linkage reference unexpectedly has no states")
    return {"states": len(values), "min": min(values), "max": max(values),
            "unwinnable": len(tickets) - len(could_win)}
