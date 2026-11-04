"""Exact-label PAPER ticket stress; no probabilities or execution authority.

Scenarios are a hypothetical win/loss grid on selected quote keys. Different
source IDs are not proven independent events, and void/refund rules are absent.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
from fractions import Fraction

from autosport.portfolio import (PortfolioEngine, _PORTFOLIO_DECIMAL_CONTEXT,
                                 _snapshot_open_tickets_for_analysis)
from portfolio_scenario_fraction_reference import calculate as fraction_reference

MAX_STATES = 65_536


def summarize_snapshot(tickets, *, max_states=MAX_STATES):
    """Summarize one detached PAPER position using the canonical scenario engine."""
    if type(max_states) is not int or max_states < 1 or max_states > MAX_STATES:
        raise ValueError("scenario state cap must be a positive integer at most 65536")
    open_tickets = _snapshot_open_tickets_for_analysis(tickets)
    if len({ticket.ticket_id for ticket in open_tickets}) != len(open_tickets):
        raise ValueError("duplicate ticket identity in scenario snapshot")
    if any(type(ticket.stake) is not Decimal or not ticket.stake.is_finite() or ticket.stake <= 0
           for ticket in open_tickets):
        raise ValueError("scenario stake must be a positive finite Decimal")
    quotes, exposure = defaultdict(set), defaultdict(list)
    for ticket in open_tickets:
        event_ids = [leg.event_id for leg in ticket.legs]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("a ticket cannot have multiple legs of the same exact event")
        for leg in ticket.legs:
            if (leg.sport != "table_tennis" or leg.market_id != "match_winner" or
                    leg.selection_id not in ("home", "away") or leg.exchange_side is not None):
                raise ValueError("unsupported scenario leg")
            quotes[leg.event_id].add(leg.quote_key)
            exposure[leg.event_id].append(ticket)
    if any(len(keys) > 2 for keys in quotes.values()):
        raise ValueError("too many exact event sides")
    with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
        committed = sum((ticket.stake for ticket in open_tickets), Decimal("0"))
        shared_stakes = [sum((ticket.stake for ticket in group), Decimal("0"))
                         for group in exposure.values()]
        out = {"open_tickets": len(open_tickets), "exact_event_ids": len(quotes),
               "distinct_selected_quote_keys": sum(map(len, quotes.values())),
               "open_stake_units": str(committed),
               "max_tickets_sharing_exact_event_id": max((len(x) for x in exposure.values()), default=0),
               "max_stake_sharing_exact_event_id": str(max(shared_stakes, default=Decimal("0"))),
               "hypothetical_opposed_side_event_count": sum(len(x) > 1 for x in quotes.values()),
               "state_space_limit": max_states, "grid_states": 1, "grid_status": None,
               "grid_min_open_ticket_pnl_units": None, "grid_max_open_ticket_pnl_units": None,
               "one_event_loss_from_all_win": None}
        for keys in quotes.values():
            out["grid_states"] *= 2 if len(keys) == 1 else len(keys) + 1
        engine = PortfolioEngine(max_exact_states=max_states)
        if out["grid_states"] > max_states:
            out["grid_status"] = "not_enumerated_state_cap"
        else:
            groups = [set(keys) for keys in quotes.values() if len(keys) > 1]
            report = engine.analyse(open_tickets, exclusive_groups=groups)
            if report.scenario_count != out["grid_states"] or report.mode not in ("exact", "conservative-enumeration"):
                raise ValueError("scenario enumeration did not match the declared grid")
            out.update(grid_status=report.mode,
                       grid_min_open_ticket_pnl_units=str(report.worst_case),
                       grid_max_open_ticket_pnl_units=str(report.best_case))
        if open_tickets and all(len(keys) == 1 for keys in quotes.values()):
            all_winners = {next(iter(keys)) for keys in quotes.values()}
            all_win = engine.scenario_profit(open_tickets, all_winners)
            candidates = []
            for event, keys in quotes.items():
                losing = next(iter(keys))
                loss = engine.scenario_profit(open_tickets, all_winners - {losing})
                group = exposure[event]
                candidates.append((all_win - loss, len(group), sum((t.stake for t in group), Decimal("0"))))
            largest = max(x[0] for x in candidates)
            ties = [x for x in candidates if x[0] == largest]
            out["one_event_loss_from_all_win"] = {
                "max_scenario_pnl_drop_units": str(largest), "tied_event_count": len(ties),
                "affected_ticket_count_range": [min(x[1] for x in ties), max(x[1] for x in ties)],
                "affected_stake_units_range": [str(min(x[2] for x in ties)), str(max(x[2] for x in ties))]}
        reference = fraction_reference(open_tickets)
        if reference["states"] != out["grid_states"]:
            raise ValueError("rational reference state count differs")
        if reference["status"] == "exact_fraction" and out["grid_status"] != "not_enumerated_state_cap":
            def close(value, expected):
                tolerance = Fraction(1, 10 ** 24) * max(Fraction(1), abs(expected))
                return abs(Fraction(value) - expected) <= tolerance
            if (not close(out["grid_min_open_ticket_pnl_units"], reference["min"]) or
                    not close(out["grid_max_open_ticket_pnl_units"], reference["max"])):
                raise ValueError("scenario extrema differ from rational reference")
            if out["one_event_loss_from_all_win"] is not None and not close(
                    out["one_event_loss_from_all_win"]["max_scenario_pnl_drop_units"],
                    reference["one_event_max_drop"]):
                raise ValueError("single-event impact differs from rational reference")
            reference_status = "matched_within_1e-24_scaled"
        else:
            reference_status = "not_cross_checked_due_to_state_cap"
        out["fraction_reference"] = {"status": reference_status,
                                     "independent_input_or_event_selection": False}
    out.update(no_probability_or_expected_return=True, prior_settlements_and_fees_included=False,
               void_refund_states_included=False, physical_identity_verified=False,
               executable_fill_verified=False, profitability_claim=False, real_money_execution=False)
    return out
