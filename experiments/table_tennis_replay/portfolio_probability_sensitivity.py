"""Two hypothetical joint outcome laws for a detached PAPER snapshot.

Every exact source ID is a binary home/away label. Both laws assign 0.5 to
both sides: one assumes independence across IDs, the other puts all mass on
all-home and all-away. These are illustrations, not estimated probabilities,
settlement authority, event identity, or evidence of a tradable advantage.
"""
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import product

from autosport.domain import TicketLeg
from autosport.joint_scenario_distribution import (
    JointScenarioState, _fraction_to_decimal, analyse_joint_distribution,
)
from autosport.paper import PaperBook
from autosport.portfolio import (
    PortfolioEngine, _PORTFOLIO_DECIMAL_CONTEXT, _snapshot_open_tickets_for_analysis,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine
from portfolio_scenario_stress import summarize_snapshot

MAX_EVENTS = 8
HALF = Decimal("0.5")


def _reference_profit(tickets, winners):
    total = Fraction(0)
    for ticket in tickets:
        stake = Fraction(ticket.stake)
        if all(leg.quote_key in winners for leg in ticket.legs):
            payout = stake
            for leg in ticket.legs:
                payout *= Fraction(leg.locked_odds)
            total += payout - stake
        else:
            total -= stake
    return total


def _close(actual, expected):
    return abs(Fraction(actual) - expected) <= Fraction(1, 10 ** 24) * max(Fraction(1), abs(expected))


def _evaluate(book, tickets, groups, states, *, independent):
    report = analyse_joint_distribution(book, groups, states)
    weighted = Fraction(0)
    all_lose = Fraction(0)
    for state in states:
        winners = set(state.selected_quote_keys)
        reference = _reference_profit(tickets, winners)
        if not _close(PortfolioEngine.scenario_profit(tickets, winners), reference):
            raise ValueError("state P&L differs from exact rational reference")
        mass = Fraction(state.probability)
        weighted += mass * reference
        if all(any(leg.quote_key not in winners for leg in ticket.legs) for ticket in tickets):
            all_lose += mass
    if not _close(report.expected_case, weighted):
        raise ValueError("conditional P&L differs from exact rational reference")
    check = "not_applicable"
    if independent:
        searched = ScenarioSearchEngine(exact_state_limit=2 ** MAX_EVENTS).analyse(tickets, groups)
        if (searched.expected_mode != "exact-independent-groups" or
                searched.total_states != len(states) or searched.expected_case is None or
                not _close(searched.expected_case, weighted)):
            raise ValueError("independent scenario search differs from rational reference")
        check = "matched_exact_independent_groups"
    return {"state_count": report.scenario_count,
            "conditional_mean_open_ticket_pnl_units": str(report.expected_case),
            "conditional_probability_all_open_tickets_lose": str(_fraction_to_decimal(all_lose)),
            "fraction_reference": "matched_within_1e-24_scaled_per_state_and_mean",
            "scenario_search_cross_check": check,
            "probability_authority_proven": report.probability_authority_proven}


def summarize_probability_sensitivity(tickets):
    """Validate, bound and compare two explicitly invented distributions."""
    opened = _snapshot_open_tickets_for_analysis(tickets)
    # Reuse the established exact-ID, stake, market and duplicate validation.
    validated = summarize_snapshot(opened, max_states=1)
    count = validated["exact_event_ids"]
    out = {"open_tickets": len(opened), "exact_event_ids": count,
           "hypothetical_opposed_side_event_count": validated["hypothetical_opposed_side_event_count"],
           "event_limit": MAX_EVENTS, "status": None,
           "independent_exact_ids": None, "all_home_or_all_away": None,
           "marginal_home_and_away_probability": "0.5_each_invented",
           "probabilities_empirically_estimated": False,
           "event_identity_or_independence_proven": False,
           "outcome_authority_or_execution_proven": False,
           "voids_costs_and_previous_pnl_included": False}
    if not opened:
        out["status"] = "no_open_tickets"
        return out
    if count > MAX_EVENTS:
        out["status"] = "not_computed_event_cap"
        return out

    event_ids = sorted({leg.event_id for ticket in opened for leg in ticket.legs})
    groups = []
    for index, event_id in enumerate(event_ids):
        # Only quote identities are formed; the price below is not an offered
        # quote and does not enter any ticket's settlement arithmetic.
        keys = [TicketLeg(event_id, "match_winner", side, Decimal("2"),
                          sport="table_tennis").quote_key for side in ("home", "away")]
        groups.append(ScenarioGroup("event-" + str(index), tuple(ScenarioOutcome(key, HALF) for key in keys)))

    # A new analysis-only book reconstructs the detached stakes and legs. Its
    # arbitrary starting balance has no relationship to historic cash or P&L.
    with localcontext(_PORTFOLIO_DECIMAL_CONTEXT):
        book = PaperBook(sum((ticket.stake for ticket in opened), Decimal("1")))
    reconstructed = [book.open_ticket(ticket.legs, ticket.stake, reason="research-only detached snapshot",
                                      placed_at=ticket.placed_at) for ticket in opened]
    probability = _fraction_to_decimal(Fraction(1, 2 ** count))
    states = [JointScenarioState("state-" + str(i),
              tuple(groups[j].outcomes[side].quote_key for j, side in enumerate(sides)), probability)
              for i, sides in enumerate(product((0, 1), repeat=count))]
    common = [JointScenarioState("all-home", tuple(group.outcomes[0].quote_key for group in groups), HALF),
              JointScenarioState("all-away", tuple(group.outcomes[1].quote_key for group in groups), HALF)]
    out["independent_exact_ids"] = _evaluate(book, reconstructed, groups, states, independent=True)
    out["all_home_or_all_away"] = _evaluate(book, reconstructed, groups, common, independent=False)
    out["status"] = "computed_hypothetical_only"
    return out
