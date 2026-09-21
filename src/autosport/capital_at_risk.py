from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .bookmaker_routing import (
    RoutingDecision,
    RoutingState,
    VenueObservation,
    VenueQuote,
    route_residual,
)
from .bookmaker_routing_plan import (
    ParallelRoutingProposal,
    plan_equal_split_residual,
)


@dataclass(frozen=True, slots=True)
class RoutingCapitalAtRiskTruth:
    """Truthful execution-routing exposure derived from canonical routing evidence.

    ``confirmed_at_risk`` is the stake with externally reconciled ACCEPTED evidence
    for this routing request. ``non_money_moving_proposed`` is deliberately separate:
    routing proposals grant no placement/receipt authority and therefore must not
    be reported as capital already at risk.

    ``exact_capital_at_risk`` is ``None`` when any external effect is UNKNOWN. In
    that state the confirmed amount remains a valid lower bound, but the exact
    exposure cannot be known until reconciliation resolves whether the uncertain
    provider interaction placed capital. This contract is about execution-routing
    exposure only; it is not settlement-aware portfolio or bankroll risk.
    """

    routing_state: RoutingState
    confirmed_at_risk: Decimal
    exact_capital_at_risk: Decimal | None
    non_money_moving_proposed: Decimal
    unresolved_external_effect: bool


def _truth_from_canonical_routing(
    routing: RoutingDecision | ParallelRoutingProposal,
) -> RoutingCapitalAtRiskTruth:
    if isinstance(routing, ParallelRoutingProposal):
        proposed = routing.proposed_total
    else:
        proposed = routing.proposed_stake

    unresolved = routing.state is RoutingState.BLOCKED_UNKNOWN
    return RoutingCapitalAtRiskTruth(
        routing_state=routing.state,
        confirmed_at_risk=routing.confirmed_total,
        exact_capital_at_risk=None if unresolved else routing.confirmed_total,
        non_money_moving_proposed=proposed,
        unresolved_external_effect=unresolved,
    )


def sequential_routing_capital_at_risk_truth(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation] = (),
    *,
    routing_request_id: str,
) -> RoutingCapitalAtRiskTruth:
    """Recompute canonical sequential routing before reporting exposure truth."""

    decision = route_residual(
        requested_stake,
        selected_venues,
        observations,
        routing_request_id=routing_request_id,
    )
    return _truth_from_canonical_routing(decision)


def parallel_routing_capital_at_risk_truth(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation] = (),
    *,
    routing_request_id: str,
    parent_plan_id: str,
    stake_quantum: Decimal,
) -> RoutingCapitalAtRiskTruth:
    """Recompute canonical parallel proposal state before reporting exposure truth.

    This intentionally accepts the same evidence inputs as the existing planner
    instead of trusting a caller-minted ``ParallelRoutingProposal`` as provenance.
    """

    proposal = plan_equal_split_residual(
        requested_stake,
        selected_venues,
        observations,
        routing_request_id=routing_request_id,
        parent_plan_id=parent_plan_id,
        stake_quantum=stake_quantum,
    )
    return _truth_from_canonical_routing(proposal)
