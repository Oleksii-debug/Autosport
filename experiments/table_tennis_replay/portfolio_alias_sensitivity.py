"""Conditional PAPER sensitivity, never a claim that source IDs match.

Exactly one unordered pair is assumed to refer to one event at a time. Both
side orientations are tested, and all other source IDs retain the original
scenario grid. No actual matches or player identities are resolved here.
"""
from collections import defaultdict
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import combinations, product

from autosport.portfolio import PortfolioEngine, _PORTFOLIO_DECIMAL_CONTEXT, _snapshot_open_tickets_for_analysis
from portfolio_alias_fraction_reference import calculate as fraction_reference
from portfolio_scenario_stress import summarize_snapshot

MAX_WORK = 65_536  # conservative upper bound on (pair, orientation, grid state)


def summarize_alias_sensitivity(tickets, *, max_work=MAX_WORK):
    if type(max_work) is not int or not 1 <= max_work <= MAX_WORK:
        raise ValueError("alias work cap must be an integer from 1 to 65536")
    opened = _snapshot_open_tickets_for_analysis(tickets)
    # Reuse the existing validator, exclusive-side grid and independently
    # checked baseline. This also rejects duplicate ticket IDs and bad legs.
    baseline = summarize_snapshot(opened)
    events, exposure = defaultdict(dict), defaultdict(set)
    for index, ticket in enumerate(opened):
        for leg in ticket.legs:
            existing = events[leg.event_id].setdefault(leg.selection_id, leg.quote_key)
            if existing != leg.quote_key:
                raise ValueError("inconsistent quote key for event and side")
            exposure[leg.event_id].add(index)
    ids = sorted(events)
    pairs = list(combinations(ids, 2))
    same_ticket = sum(bool(exposure[a] & exposure[b]) for a, b in pairs)
    with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
        max_exact_stake = max((sum((opened[i].stake for i in indices), Decimal(0))
                               for indices in exposure.values()), default=Decimal(0))
        joint_counts = [len(exposure[a] | exposure[b]) for a, b in pairs]
        joint_stakes = [sum((opened[i].stake for i in exposure[a] | exposure[b]), Decimal(0))
                        for a, b in pairs]
    result = {"open_tickets": len(opened), "exact_event_ids": len(ids),
              "distinct_id_pairs": len(pairs), "side_mappings": 2 * len(pairs),
              "pairs_cooccurring_in_a_ticket": same_ticket,
              "max_tickets_one_exact_id": max((len(x) for x in exposure.values()), default=0),
              "max_stake_one_exact_id": str(max_exact_stake),
              "max_tickets_either_id": max(joint_counts, default=0),
              "max_stake_either_id": str(max(joint_stakes, default=Decimal(0))),
              "baseline_grid_states": baseline["grid_states"],
              "baseline_min_units": baseline["grid_min_open_ticket_pnl_units"],
              "baseline_max_units": baseline["grid_max_open_ticket_pnl_units"],
              "work_upper_bound": baseline["grid_states"] * 2 * len(pairs),
              "scenario_status": None, "conditional_min_range_units": None,
              "conditional_max_range_units": None,
              "mappings_with_lower_max": None, "mappings_with_unwinnable_ticket": None,
              "fraction_reference": None,
              "identity_verified": False, "bookmaker_acceptance_verified": False,
              "void_or_fees_included": False, "probabilities_assigned": False,
              "profitability_claim": False, "real_money_execution": False}
    if not pairs:
        result.update(scenario_status="no_distinct_id_pair", fraction_reference="not_applicable")
        return result
    if result["work_upper_bound"] > max_work or baseline["grid_status"] == "not_enumerated_state_cap":
        result.update(scenario_status="not_enumerated_work_cap", fraction_reference="not_run")
        return result

    def close(value, reference):
        return abs(Fraction(value) - reference) <= Fraction(1, 10**24) * max(1, abs(reference))

    minima, maxima, lower_max, unwinnable = [], [], 0, 0
    engine = PortfolioEngine()
    for first, second in pairs:
        others = [event for event in ids if event not in (first, second)]
        other_options = [tuple(frozenset([q]) for q in events[e].values()) + (frozenset(),)
                         for e in others]
        for reverse in (False, True):
            joint = set()
            for outcome in ("home", "away", None):
                mapped = ({"home": "away", "away": "home", None: None}[outcome]
                          if reverse else outcome)
                keys = set()
                if outcome in events[first]:
                    keys.add(events[first][outcome])
                if mapped in events[second]:
                    keys.add(events[second][mapped])
                joint.add(frozenset(keys))
            values, can_win = [], set()
            for joint_winners, *rest in product(tuple(joint), *other_options):
                winners = set(joint_winners)
                for addition in rest:
                    winners.update(addition)
                values.append(engine.scenario_profit(opened, winners))
                can_win.update(i for i, ticket in enumerate(opened)
                               if all(leg.quote_key in winners for leg in ticket.legs))
            minimum, maximum = min(values), max(values)
            ref = fraction_reference(opened, first, second, reverse)
            impossible = len(opened) - len(can_win)
            if (ref["states"] != len(values) or ref["unwinnable"] != impossible
                    or not close(minimum, ref["min"]) or not close(maximum, ref["max"])):
                raise ValueError("alias scenario differs from rational reference")
            if minimum < Decimal(baseline["grid_min_open_ticket_pnl_units"]):
                raise ValueError("conditional grid minimum below baseline")
            if maximum > Decimal(baseline["grid_max_open_ticket_pnl_units"]):
                raise ValueError("conditional grid maximum above baseline")
            minima.append(minimum)
            maxima.append(maximum)
            lower_max += maximum < Decimal(baseline["grid_max_open_ticket_pnl_units"])
            unwinnable += impossible > 0
    result.update(scenario_status="enumerated", conditional_min_range_units=[str(min(minima)), str(max(minima))],
                  conditional_max_range_units=[str(min(maxima)), str(max(maxima))],
                  mappings_with_lower_max=lower_max, mappings_with_unwinnable_ticket=unwinnable,
                  fraction_reference="matched_each_mapping_within_1e-24_scaled")
    return result
