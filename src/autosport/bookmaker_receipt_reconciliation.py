from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    VenueObservation,
    VenueQuote,
    _dedupe_external_receipts,
)
from .bookmaker_routing_plan import (
    ParallelRoutingProposal,
    VenueLegProposal,
    plan_equal_split_residual,
)


def bind_leg_receipt(
    leg: VenueLegProposal,
    *,
    effect: ExternalEffect,
    external_receipt_id: str | None = None,
    confirmed_accepted: Decimal = Decimal("0"),
    observation_id: str | None = None,
) -> VenueObservation:
    """Bind external evidence to one deterministic proposal child.

    ACCEPTED evidence requires a real external receipt ID. UNKNOWN/refusal may
    truthfully have none yet; the deterministic child identity still prevents blind
    reroute. This helper does not call a bookmaker, authorize money movement, or
    create the future #353 real execution ledger.
    """
    if not isinstance(leg, VenueLegProposal):
        raise RoutingContractError("leg must be a VenueLegProposal")
    if not isinstance(effect, ExternalEffect):
        raise RoutingContractError("effect must be ExternalEffect")
    return VenueObservation(
        venue_id=leg.venue.venue_id,
        account_id=leg.venue.account_id,
        effect=effect,
        routing_request_id=leg.routing_request_id,
        quote=leg.venue.quote,
        confirmed_accepted=confirmed_accepted,
        observation_id=observation_id,
        parent_plan_id=leg.parent_plan_id,
        proposal_leg_id=leg.leg_id,
        proposed_stake=leg.proposed_stake,
        external_receipt_id=external_receipt_id,
    )


def _validated_child_receipts(
    selected_venues: tuple[VenueQuote, ...],
    observations: Iterable[VenueObservation],
    *,
    routing_request_id: str,
    parent_plan_id: str,
) -> tuple[VenueObservation, ...]:
    raw = tuple(observations)
    if any(not isinstance(item, VenueObservation) for item in raw):
        raise RoutingContractError(
            "observations must contain VenueObservation values"
        )
    normalized = _dedupe_external_receipts(raw)
    venues = {
        (venue.venue_id, venue.account_id): venue
        for venue in selected_venues
    }
    accepted_receipt_by_leg: dict[str, str] = {}

    for item in normalized:
        if (
            item.parent_plan_id is None
            or item.proposal_leg_id is None
            or item.proposed_stake is None
        ):
            raise RoutingContractError(
                "reconciliation requires deterministic child identity"
            )
        if item.parent_plan_id != parent_plan_id:
            raise RoutingContractError(
                "receipt parent_plan_id does not match parent plan"
            )
        if item.routing_request_id != routing_request_id:
            raise RoutingContractError(
                "receipt routing_request_id does not match routing request"
            )

        identity = (item.venue_id, item.account_id)
        venue = venues.get(identity)
        if venue is None:
            raise RoutingContractError(
                "receipt references an unselected venue/account"
            )
        if item.quote != venue.quote:
            raise RoutingContractError(
                "receipt quote does not exactly match selected venue quote"
            )

        # VenueLegProposal owns the canonical child hash contract. Rebuilding the
        # immutable child here proves evidence is bound to exactly parent +
        # request + venue/account + quote + proposed stake.
        try:
            VenueLegProposal(
                parent_plan_id=parent_plan_id,
                routing_request_id=routing_request_id,
                leg_id=item.proposal_leg_id,
                venue=venue,
                proposed_stake=item.proposed_stake,
            )
        except RoutingContractError as exc:
            raise RoutingContractError(
                "receipt proposal_leg_id does not match canonical child identity"
            ) from exc

        if item.effect is ExternalEffect.ACCEPTED:
            if item.external_receipt_id is None:
                raise RoutingContractError(
                    "confirmed ACCEPTED reconciliation requires external_receipt_id"
                )
            previous_receipt = accepted_receipt_by_leg.get(
                item.proposal_leg_id
            )
            if (
                previous_receipt is not None
                and previous_receipt != item.external_receipt_id
            ):
                raise RoutingContractError(
                    "multiple external receipts for one child are ambiguous"
                )
            accepted_receipt_by_leg[item.proposal_leg_id] = (
                item.external_receipt_id
            )

    return normalized


def reconcile_equal_split_residual(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation],
    *,
    routing_request_id: str,
    parent_plan_id: str,
    stake_quantum: Decimal,
) -> ParallelRoutingProposal:
    """Reconcile child evidence, then derive the next non-money-moving proposal.

    Every external effect on this strict surface carries deterministic child
    identity. Confirmed ACCEPTED effects additionally require a provider/account
    external receipt ID. UNKNOWN/refusal may have no provider receipt yet and still
    block or constrain reroute through the child binding. Exact receipt replay is
    idempotent; conflicting receipt reuse fails closed in the routing contract.
    """
    venues = tuple(selected_venues)
    normalized = _validated_child_receipts(
        venues,
        observations,
        routing_request_id=routing_request_id,
        parent_plan_id=parent_plan_id,
    )
    return plan_equal_split_residual(
        requested_stake,
        venues,
        normalized,
        routing_request_id=routing_request_id,
        parent_plan_id=parent_plan_id,
        stake_quantum=stake_quantum,
    )
