from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .bookmaker_routing import RoutingDecision, RoutingState
from .bookmaker_routing_plan import ParallelRoutingProposal


@dataclass(frozen=True, slots=True)
class RoutingCapitalAtRiskTruth:
    """Truthful execution-routing exposure derived from reconciled evidence only.

    ``confirmed_at_risk`` is the stake with durable external ACCEPTED evidence for
    this routing request. ``non_money_moving_proposed`` is deliberately separate:
    routing proposals grant no placement/receipt authority and therefore must not
    be reported as capital already at risk.

    ``exact_capital_at_risk`` is ``None`` when any external effect is UNKNOWN. In
    that state the confirmed amount remains a valid lower bound, but the exact
    exposure cannot be known until reconciliation resolves whether the uncertain
    provider interaction placed capital. This contract is about execution-routing
    exposure only; it is not settlement-aware portfolio or bankroll risk.
    """

    confirmed_at_risk: Decimal
    exact_capital_at_risk: Decimal | None
    non_money_moving_proposed: Decimal
    unresolved_external_effect: bool


def routing_capital_at_risk_truth(
    routing: RoutingDecision | ParallelRoutingProposal,
) -> RoutingCapitalAtRiskTruth:
    """Return fail-closed capital-at-risk truth for one routing snapshot.

    The function intentionally consumes only already-validated routing objects. It
    never infers provider execution from proposal or residual arithmetic.
    """

    if isinstance(routing, ParallelRoutingProposal):
        proposed = routing.proposed_total
    elif isinstance(routing, RoutingDecision):
        proposed = routing.proposed_stake
    else:
        raise TypeError(
            "routing must be RoutingDecision or ParallelRoutingProposal"
        )

    unresolved = routing.state is RoutingState.BLOCKED_UNKNOWN
    return RoutingCapitalAtRiskTruth(
        confirmed_at_risk=routing.confirmed_total,
        exact_capital_at_risk=None if unresolved else routing.confirmed_total,
        non_money_moving_proposed=proposed,
        unresolved_external_effect=unresolved,
    )
