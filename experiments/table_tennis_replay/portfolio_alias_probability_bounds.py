"""Certified hypothetical PAPER loss bounds under pairwise ID alias assumptions.

No ID is resolved, no probability is estimated and no real bet is authorized.
Exact IDs are binary labels with invented 1/2 home/away marginals. Each pair
and side orientation is a separate hypothesis, not a probability mixture.
"""
from collections import Counter
from fractions import Fraction
from itertools import combinations, product

from autosport.portfolio import PortfolioEngine, _snapshot_open_tickets_for_analysis
from portfolio_joint_probability_bounds import _certify_candidate, _run_linprog, summarize_joint_bounds
from portfolio_alias_probability_reference import count_extreme_mappings
from portfolio_probability_sensitivity import MAX_EVENTS, _close, _reference_profit
from portfolio_scenario_stress import summarize_snapshot

MAX_MAPPINGS = 56


def _bounds(states, n, which):
    """Certify extrema over a specified subset with 1/2 per-ID marginals."""
    matrix = [[1] * len(states)] + [
        [int(bits[i] == 0) for bits, _, _ in states] for i in range(n)
    ]
    objective = [row[which] for row in states]
    rhs = [1.0] + [0.5] * n
    lower, _ = _certify_candidate(matrix, objective, _run_linprog(objective, matrix, rhs))
    negative = [-value for value in objective]
    upper_negative, _ = _certify_candidate(matrix, negative, _run_linprog(negative, matrix, rhs))
    lower, upper = lower, -upper_negative
    if not (0 <= lower <= upper <= 1):
        raise ValueError("certified probability bounds outside [0,1]")
    return lower, upper


def _check_pair(all_lose, negative):
    # On every state, all losing implies strictly negative P&L for positive stakes.
    if not (all_lose[0] <= negative[0] and all_lose[1] <= negative[1]):
        raise ValueError("all-lose event is not bounded by negative P&L")


def _encode(pair):
    return {"lower": str(pair[0]), "upper": str(pair[1])}


def _distribution(counter):
    return [{**_encode(pair), "mappings": count} for pair, count in sorted(counter.items())]


def summarize_alias_probability_bounds(tickets, *, max_mappings=MAX_MAPPINGS):
    """Return aggregate, exact certificates or explicitly not-computed status.

    Each hypothesis restricts exactly one pair of distinct IDs, either with
    equal or reversed side labels. Reports never include IDs or ticket legs.
    Existing validation and exact primal/dual certificate code are reused.
    """
    if type(max_mappings) is not int or not 1 <= max_mappings <= MAX_MAPPINGS:
        raise ValueError("mapping cap must be an integer from 1 to 56")
    opened = _snapshot_open_tickets_for_analysis(tickets)
    validated = summarize_snapshot(opened, max_states=1)
    n = validated["exact_event_ids"]
    mappings = n * (n - 1)
    out = {"open_tickets": len(opened), "exact_event_ids": n,
           "distinct_id_pairs": n * (n - 1) // 2, "side_mappings": mappings,
           "event_limit": MAX_EVENTS, "mapping_limit": max_mappings,
           "status": None, "baseline": None, "alias_bounds": None,
           "marginal_home_and_away_probability": "1/2_each_invented",
           "negative_pnl_strictly_less_than_zero": True,
           "all_loss_and_negative_pnl_are_distinct_predicates": True,
           "mapping_hypotheses_have_no_probabilities": True,
           "exact_primal_and_dual_certificates_checked": False,
           "physical_event_identity_verified": False, "bookmaker_acceptance_verified": False,
           "voids_costs_and_previous_pnl_included": False,
           "profitability_claim": False, "real_money_execution": False}
    if not opened:
        out["status"] = "no_open_tickets"
        return out
    if n > MAX_EVENTS:
        out["status"] = "not_computed_event_cap"
        return out
    if mappings > max_mappings:
        out["status"] = "not_computed_mapping_cap"
        return out

    ids = sorted({leg.event_id for ticket in opened for leg in ticket.legs})
    if len(ids) != n:
        raise ValueError("event inventory changed while analyzing snapshot")
    states = []
    for bits in product((0, 1), repeat=n):
        sides = dict(zip(ids, bits))
        winners = {leg.quote_key for ticket in opened for leg in ticket.legs
                   if sides[leg.event_id] == (0 if leg.selection_id == "home" else 1)}
        profit = _reference_profit(opened, winners)
        if not _close(PortfolioEngine.scenario_profit(opened, winners), profit):
            raise ValueError("canonical scenario differs from rational reference")
        all_lose = int(all(any(leg.quote_key not in winners for leg in ticket.legs)
                           for ticket in opened))
        negative = int(profit < Fraction(0))
        if all_lose and not negative:
            raise ValueError("all-lose state is not a negative P&L state")
        states.append((bits, all_lose, negative))

    all_base, neg_base = _bounds(states, n, 1), _bounds(states, n, 2)
    _check_pair(all_base, neg_base)
    prior = summarize_joint_bounds(opened)
    if (prior["status"] != "exact_fraction_primal_dual_certified" or
        all_base != (Fraction(prior["lower_all_lose_probability"]),
                     Fraction(prior["upper_all_lose_probability"]))):
        raise ValueError("unrestricted all-lose bounds differ from previous certified report")
    out["baseline"] = {"binary_states": len(states), "all_tickets_lose": _encode(all_base),
                       "negative_open_ticket_pnl": _encode(neg_base)}
    out["exact_primal_and_dual_certificates_checked"] = True
    if not mappings:
        out["status"] = "no_distinct_id_pair"
        return out

    all_counter, neg_counter = Counter(), Counter()
    all_narrowed = neg_narrowed = 0
    for first, second in combinations(range(n), 2):
        for reverse in (False, True):
            chosen = [row for row in states if (row[0][first] != row[0][second]) == reverse]
            if len(chosen) * 2 != len(states):
                raise ValueError("invalid binary alias state filter")
            all_cond, neg_cond = _bounds(chosen, n, 1), _bounds(chosen, n, 2)
            _check_pair(all_cond, neg_cond)
            for conditional, baseline in ((all_cond, all_base), (neg_cond, neg_base)):
                if not (baseline[0] <= conditional[0] <= conditional[1] <= baseline[1]):
                    raise ValueError("conditional bounds extend beyond baseline")
            all_counter[all_cond] += 1
            neg_counter[neg_cond] += 1
            all_narrowed += all_cond != all_base
            neg_narrowed += neg_cond != neg_base
    if sum(all_counter.values()) != mappings or sum(neg_counter.values()) != mappings:
        raise ValueError("alias mapping count mismatch")
    direct = count_extreme_mappings(opened)
    zero, one = Fraction(0), Fraction(1)
    expected = {"all_lose_always": all_counter[(one, one)],
                "all_lose_never": all_counter[(zero, zero)],
                "negative_always": neg_counter[(one, one)],
                "negative_never": neg_counter[(zero, zero)]}
    if direct != expected:
        raise ValueError("direct alias state extremes differ from certified bounds")
    out["alias_bounds"] = {
        "binary_states_per_mapping": len(states) // 2,
        "all_tickets_lose_distribution": _distribution(all_counter),
        "negative_open_ticket_pnl_distribution": _distribution(neg_counter),
        "mappings_narrowing_all_tickets_lose": all_narrowed,
        "mappings_narrowing_negative_open_ticket_pnl": neg_narrowed,
        "direct_fraction_state_check": "matched_zero_and_one_extremes",
    }
    out["status"] = "exact_fraction_primal_dual_certified"
    return out
