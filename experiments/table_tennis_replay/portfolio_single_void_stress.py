"""Bounded hypothetical one-event void sensitivity of detached PAPER tickets.

The canonical PaperBook settlement is applied to assumed win/loss/void states.
Exact source IDs are only labels; no physical identity, bookmaker rule, accepted
price, outcome probability, return expectation or real-money authority is implied.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import product

from autosport.paper import PaperBook
from autosport.portfolio import _PORTFOLIO_DECIMAL_CONTEXT, _snapshot_open_tickets_for_analysis
from portfolio_scenario_fraction_reference import calculate_single_void as independent_reference
from portfolio_scenario_stress import summarize_snapshot

MAX_COMPARISONS = 65_536
_VOID = object()


def _near(actual, expected):
    return abs(Fraction(actual) - expected) <= Fraction(1, 10**24) * max(Fraction(1), abs(expected))


def summarize_single_void(tickets, *, max_comparisons=MAX_COMPARISONS):
    """Compare one void event to each non-void state with other states fixed.

    Every selected side of a void event is void across all affected tickets.
    All other exact-ID states reuse the existing binary/exclusive grid. Opposed
    sides also retain the conservative neither-wins state. Counts are scenario
    combinations, never probabilities; no costs, past settlements or book rules
    other than canonical PaperBook are inferred.
    """
    if type(max_comparisons) is not int or not 1 <= max_comparisons <= MAX_COMPARISONS:
        raise ValueError("comparison cap must be a positive integer at most 65536")
    open_tickets = _snapshot_open_tickets_for_analysis(tickets)
    baseline = summarize_snapshot(open_tickets)
    sides, exposure = defaultdict(dict), defaultdict(list)
    for ticket in open_tickets:
        for leg in ticket.legs:
            current = sides[leg.event_id].setdefault(leg.selection_id, leg.quote_key)
            if current != leg.quote_key:
                raise ValueError("same event side has conflicting quote identity")
            exposure[leg.event_id].append(ticket)
    events = sorted(sides)
    # Existing binary grid: with both sides selected, neither may win too.
    choices = [tuple(sorted(sides[event].values())) + (None,) for event in events]
    planned = len(events) * baseline["grid_states"]
    with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
        stakes = [sum((ticket.stake for ticket in group), Decimal(0)) for group in exposure.values()]
        result = {"open_tickets": len(open_tickets), "exact_event_ids": len(events),
                  "open_stake_units": baseline["open_stake_units"],
                  "affected_ticket_count_range": ([min(map(len, exposure.values())),
                                                     max(map(len, exposure.values()))] if events else None),
                  "affected_stake_units_range": ([str(min(stakes)), str(max(stakes))] if events else None),
                  "binary_baseline_states": baseline["grid_states"],
                  "planned_paired_comparisons": planned, "comparison_cap": max_comparisons,
                  "completed_paired_comparisons": 0, "comparison_status": None,
                  "void_minus_nonvoid_min_units": None, "void_minus_nonvoid_max_units": None,
                  "void_scenario_pnl_min_units": None, "void_scenario_pnl_max_units": None,
                  "void_minus_nonvoid_sign_counts": None,
                  "fraction_check": "not_run", "independent_grid_fraction_check": "not_run",
                  "same_source_rows_for_reference": True,
                  "no_probability_or_expected_return": True, "prior_settlements_and_fees_included": False,
                  "bookmaker_void_rules_verified": False, "physical_identity_verified": False,
                  "executable_fill_verified": False, "profitability_claim": False,
                  "real_money_execution": False}
        if not events:
            result["comparison_status"] = "no_open_events"
            return result
        if planned > max_comparisons:
            result["comparison_status"] = "not_enumerated_comparison_cap"
            return result

        indices = {event: i for i, event in enumerate(events)}
        cache = {}
        def portfolio_value(scenario):
            total, fraction_total = Decimal(0), Fraction(0)
            for index, ticket in enumerate(open_tickets):
                flags = tuple(2 if scenario[indices[leg.event_id]] is _VOID else
                              int(scenario[indices[leg.event_id]] == leg.quote_key)
                              for leg in ticket.legs)
                key = (index, flags)
                if key not in cache:
                    wins = {leg.quote_key for leg, flag in zip(ticket.legs, flags) if flag == 1}
                    voids = {leg.quote_key for leg, flag in zip(ticket.legs, flags) if flag == 2}
                    _status, payout, _balance = PaperBook._settlement_result(ticket, Decimal(0), wins, voids)
                    pnl = payout - ticket.stake
                    effective = [leg for leg, flag in zip(ticket.legs, flags) if flag != 2]
                    if any(flag == 0 for flag in flags):
                        rational = -Fraction(ticket.stake)
                    else:
                        rational_payout = Fraction(ticket.stake)
                        for leg in effective:
                            rational_payout *= Fraction(leg.locked_odds)
                        rational = rational_payout - Fraction(ticket.stake)
                    cache[key] = pnl, rational
                value, ref = cache[key]
                total += value
                fraction_total += ref
            return total, fraction_total

        smallest = largest = lowest_void = highest_void = None
        signs = {"negative": 0, "zero": 0, "positive": 0}
        for chosen in range(len(events)):
            for state in product(*choices):
                nonvoid, nonvoid_ref = portfolio_value(state)
                with_void = list(state)
                with_void[chosen] = _VOID
                void_pnl, void_ref = portfolio_value(with_void)
                difference = void_pnl - nonvoid
                if (not _near(nonvoid, nonvoid_ref) or not _near(void_pnl, void_ref) or
                        not _near(difference, void_ref - nonvoid_ref)):
                    raise ValueError("canonical settlement differs from rational reference")
                smallest = difference if smallest is None else min(smallest, difference)
                largest = difference if largest is None else max(largest, difference)
                lowest_void = void_pnl if lowest_void is None else min(lowest_void, void_pnl)
                highest_void = void_pnl if highest_void is None else max(highest_void, void_pnl)
                signs["positive" if difference > 0 else "negative" if difference < 0 else "zero"] += 1
                result["completed_paired_comparisons"] += 1
        if result["completed_paired_comparisons"] != planned:
            raise ValueError("incomplete scenario enumeration")
        reference = independent_reference(open_tickets)
        if reference["comparisons"] != planned:
            raise ValueError("independent grid has a different comparison count")
        if reference["status"] == "exact_fraction":
            if (not _near(smallest, reference["delta_min"]) or
                    not _near(largest, reference["delta_max"]) or
                    not _near(lowest_void, reference["void_min"]) or
                    not _near(highest_void, reference["void_max"]) or
                    signs != reference["sign_counts"]):
                raise ValueError("independent fraction grid differs")
            reference_status = "matched_within_1e-24_scaled"
        elif reference["status"] == "skipped_reference_cap":
            reference_status = "skipped_reference_cap"
        else:
            raise ValueError("unexpected independent fraction grid status")
        result.update(comparison_status="exact", void_minus_nonvoid_min_units=str(smallest),
                      void_minus_nonvoid_max_units=str(largest),
                      void_scenario_pnl_min_units=str(lowest_void),
                      void_scenario_pnl_max_units=str(highest_void),
                      void_minus_nonvoid_sign_counts=signs,
                      fraction_check="all_pairs_matched_within_1e-24_scaled",
                      independent_grid_fraction_check=reference_status)
        return result
