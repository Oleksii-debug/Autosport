"""Independent direct state check for the zero/one extremes of alias LPs.

Every admissible binary state has positive mass under a uniform distribution
on the directly constructed alias-compatible states, which also has each
ID's invented 1/2 marginal. Thus a certified upper bound of zero, or a lower
bound of one, is equivalent to the relevant predicate being uniformly false
or true across those states. This does not certify nontrivial interior bounds.
"""
from fractions import Fraction
from itertools import combinations, product


def count_extreme_mappings(tickets):
    ids = sorted({leg.event_id for ticket in tickets for leg in ticket.legs})
    counts = {"all_lose_always": 0, "all_lose_never": 0,
              "negative_always": 0, "negative_never": 0}
    for first, second in combinations(ids, 2):
        independent_ids = [event for event in ids if event != second]
        for reverse in (False, True):
            all_lose_flags, negative_flags = [], []
            for sides in product(("home", "away"), repeat=len(independent_ids)):
                state = dict(zip(independent_ids, sides))
                state[second] = ({"home": "away", "away": "home"}[state[first]]
                                 if reverse else state[first])
                pnl = Fraction(0)
                won_count = 0
                for ticket in tickets:
                    won = all(state[leg.event_id] == leg.selection_id for leg in ticket.legs)
                    won_count += won
                    payout = Fraction(ticket.stake) if won else Fraction(0)
                    if won:
                        for leg in ticket.legs:
                            payout *= Fraction(leg.locked_odds)
                    pnl += payout - Fraction(ticket.stake)
                all_lose_flags.append(won_count == 0)
                negative_flags.append(pnl < 0)
            counts["all_lose_always"] += all(all_lose_flags)
            counts["all_lose_never"] += not any(all_lose_flags)
            counts["negative_always"] += all(negative_flags)
            counts["negative_never"] += not any(negative_flags)
    return counts
