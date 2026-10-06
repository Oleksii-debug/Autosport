from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .bookmaker_routing import (
    RoutingContractError,
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


def _non_negative_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise RoutingContractError(f"{name} must be an exact finite non-negative Decimal")
    return value


@dataclass(frozen=True, slots=True)
class RoutingCapitalAtRiskTruth:
    """Fail-closed exposure truth derived from canonical routing evidence.

    Canonical routing proves externally reconciled accepted *routing notional* and
    non-money-moving proposal amount. It does not prove provider/order-side liability
    semantics, so generic routing evidence cannot establish an exact economic
    capital-at-risk amount. ``exact_capital_at_risk`` therefore remains ``None`` until
    a separate canonical execution/economic authority supplies those semantics.

    ``unresolved_external_effect`` is narrower: it records whether provider outcome
    itself is UNKNOWN. A fully reconciled routing request can still have
    ``exact_capital_at_risk is None`` because accepted notional is not universally
    equal to principal/liability at risk.
    """

    routing_state: RoutingState
    confirmed_routing_notional: Decimal
    exact_capital_at_risk: Decimal | None
    non_money_moving_proposed: Decimal
    unresolved_external_effect: bool

    def __post_init__(self) -> None:
        if type(self.routing_state) is not RoutingState:
            raise RoutingContractError("routing_state must be exact RoutingState")
        confirmed = _non_negative_decimal(
            self.confirmed_routing_notional,
            "confirmed_routing_notional",
        )
        proposed = _non_negative_decimal(
            self.non_money_moving_proposed,
            "non_money_moving_proposed",
        )
        if self.exact_capital_at_risk is not None:
            raise RoutingContractError(
                "generic routing evidence cannot establish exact capital at risk"
            )
        if type(self.unresolved_external_effect) is not bool:
            raise RoutingContractError(
                "unresolved_external_effect must be an exact boolean"
            )

        unresolved = self.routing_state is RoutingState.BLOCKED_UNKNOWN
        if self.unresolved_external_effect is not unresolved:
            raise RoutingContractError(
                "unresolved_external_effect must match BLOCKED_UNKNOWN routing state"
            )

        if self.routing_state in {
            RoutingState.BLOCKED_UNKNOWN,
            RoutingState.COMPLETE,
            RoutingState.UNEXECUTABLE,
        }:
            if proposed != 0:
                raise RoutingContractError(
                    "routing state cannot carry non-money-moving proposal authority"
                )
            if (
                self.routing_state is RoutingState.UNEXECUTABLE
                and confirmed != 0
            ):
                raise RoutingContractError(
                    "UNEXECUTABLE state cannot carry confirmed routing notional"
                )
            if self.routing_state is RoutingState.COMPLETE and confirmed <= 0:
                raise RoutingContractError(
                    "COMPLETE state requires positive confirmed routing notional"
                )
        elif self.routing_state is RoutingState.ROUTE:
            if proposed <= 0:
                raise RoutingContractError(
                    "ROUTE state requires positive non-money-moving proposal"
                )
        elif self.routing_state is RoutingState.PARTIAL:
            if proposed == 0 and confirmed == 0:
                raise RoutingContractError(
                    "PARTIAL state without proposal requires confirmed routing notional"
                )


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
        confirmed_routing_notional=routing.confirmed_total,
        exact_capital_at_risk=None,
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
