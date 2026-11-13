"""Certified bounds for an illustrative binary PAPER portfolio distribution.

All home/away marginal probabilities are invented as 1/2. The bounds are over
all joint distributions on exact source ID labels, not physical matches or
bookmaker outcome rules. SciPy proposes a solution; Fraction certificates
independently prove feasibility and optimality of each finite LP result.
"""
from fractions import Fraction
from itertools import product
import math

from autosport.portfolio import PortfolioEngine, _snapshot_open_tickets_for_analysis
from portfolio_probability_sensitivity import MAX_EVENTS, _close, _reference_profit
from portfolio_scenario_stress import summarize_snapshot

MAX_DENOMINATOR = 65_536


def _run_linprog(objective, matrix, rhs):
    try:
        from scipy.optimize import linprog
    except ImportError as exc:
        raise RuntimeError("optional SciPy is required for this research analysis") from exc
    return linprog(objective, A_eq=matrix, b_eq=rhs, bounds=(0, None), method="highs")


def _certify_candidate(matrix, objective, result):
    """Validate a proposed primal/dual pair as exact rational LP proof.

    Minimize c*x subject to A*x=b, x>=0; its dual is maximize b*y
    subject to A^T*y<=c. These checks use all enumerated columns and no
    optimizer tolerance. Failed rational reconstruction is not a bound.
    """
    rows, columns = len(matrix), len(objective)
    if not result.success:
        raise ValueError("joint-bound solver did not succeed")
    try:
        x_raw, y_raw = result.x, result.eqlin.marginals
        if len(x_raw) != columns or len(y_raw) != rows:
            raise ValueError("wrong solver candidate shape")
        if any(not math.isfinite(float(v)) for v in (*x_raw, *y_raw)):
            raise ValueError("non-finite solver candidate")
        x = tuple(Fraction(float(v)).limit_denominator(MAX_DENOMINATOR) for v in x_raw)
        y = tuple(Fraction(float(v)).limit_denominator(MAX_DENOMINATOR) for v in y_raw)
    except (AttributeError, TypeError, OverflowError) as exc:
        raise ValueError("invalid solver candidate") from exc
    rhs = (Fraction(1),) + (Fraction(1, 2),) * (rows - 1)
    if any(v < 0 for v in x):
        raise ValueError("joint-bound primal witness is negative")
    if any(sum((Fraction(a) * mass for a, mass in zip(row, x)), Fraction()) != target
           for row, target in zip(matrix, rhs)):
        raise ValueError("joint-bound primal witness violates exact marginals")
    for j in range(columns):
        dual_value = sum((Fraction(matrix[i][j]) * y[i] for i in range(rows)), Fraction())
        if dual_value > objective[j]:
            raise ValueError("joint-bound dual witness violates an exact state inequality")
    primal = sum((Fraction(c) * mass for c, mass in zip(objective, x)), Fraction())
    dual = sum((target * value for target, value in zip(rhs, y)), Fraction())
    if primal != dual:
        raise ValueError("joint-bound primal and dual objectives differ")
    return primal, sum(mass > 0 for mass in x)


def summarize_joint_bounds(tickets):
    """Return exact certified bounds or fail closed without reporting them."""
    opened = _snapshot_open_tickets_for_analysis(tickets)
    validated = summarize_snapshot(opened, max_states=1)
    n = validated["exact_event_ids"]
    out = {"open_tickets": len(opened), "exact_event_ids": n,
           "opposed_side_event_count": validated["hypothetical_opposed_side_event_count"],
           "event_limit": MAX_EVENTS, "status": None, "states": None,
           "lower_all_lose_probability": None, "upper_all_lose_probability": None,
           "independent_all_lose_probability": None, "all_home_or_away_all_lose_probability": None,
           "marginal_home_and_away_probability": "1/2_each_invented",
           "probability_authority_proven": False, "physical_event_identity_verified": False,
           "voids_costs_and_previous_pnl_included": False,
           "bound_is_for_all_tickets_losing_not_negative_total_pnl": True}
    if not opened:
        out["status"] = "no_open_tickets"
        return out
    if n > MAX_EVENTS:
        out["status"] = "not_computed_event_cap"
        return out
    event_ids = sorted({leg.event_id for ticket in opened for leg in ticket.legs})
    outcomes = list(product((0, 1), repeat=n))  # 0=home, 1=away
    matrix = [[1] * len(outcomes)] + [[int(s[i] == 0) for s in outcomes] for i in range(n)]
    losses = []
    for state in outcomes:
        side = dict(zip(event_ids, state))
        winners = {leg.quote_key for ticket in opened for leg in ticket.legs
                   if side[leg.event_id] == (0 if leg.selection_id == "home" else 1)}
        if not _close(PortfolioEngine.scenario_profit(opened, winners), _reference_profit(opened, winners)):
            raise ValueError("canonical scenario differs from rational reference")
        losses.append(int(all(any(leg.quote_key not in winners for leg in ticket.legs)
                              for ticket in opened)))
    rhs = [1.0] + [0.5] * n
    lower_result = _run_linprog(losses, matrix, rhs)
    lower, lower_support = _certify_candidate(matrix, losses, lower_result)
    negative = [-value for value in losses]
    upper_result = _run_linprog(negative, matrix, rhs)
    negative_upper, upper_support = _certify_candidate(matrix, negative, upper_result)
    upper = -negative_upper
    independent = Fraction(sum(losses), len(losses))
    aligned = Fraction(losses[0] + losses[-1], 2)
    if not (0 <= lower <= independent <= upper <= 1 and lower <= aligned <= upper):
        raise ValueError("previous hypothetical distributions outside certified bounds")
    out.update(status="exact_fraction_primal_dual_certified", states=len(outcomes),
               lower_all_lose_probability=str(lower), upper_all_lose_probability=str(upper),
               independent_all_lose_probability=str(independent),
               all_home_or_away_all_lose_probability=str(aligned),
               lower_witness_nonzero_states=lower_support, upper_witness_nonzero_states=upper_support,
               state_economics_fraction_tolerance="1e-24_scaled",
               exact_primal_and_dual_certificates_checked=True)
    return out
