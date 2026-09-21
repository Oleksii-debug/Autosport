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
from .real_execution_ledger import (
    AttemptState,
    ExternalReceiptIdentity,
    RealExecutionLedger,
)


_LEDGER_STATES_BY_EFFECT = {
    ExternalEffect.ACCEPTED: frozenset(
        {AttemptState.ACCEPTED, AttemptState.PARTIAL}
    ),
    ExternalEffect.MARKET_REFUSED: frozenset({AttemptState.REJECTED}),
    ExternalEffect.UNKNOWN: frozenset({AttemptState.UNKNOWN}),
}


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
    reroute. This helper does not call a bookmaker or write the durable execution
    ledger. Use reconcile_equal_split_residual_against_ledger when existing durable
    execution facts must authorize terminal receipt reconciliation.
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


def _validate_durable_effect_receipts(
    observations: tuple[VenueObservation, ...],
    *,
    ledger: RealExecutionLedger,
    parent_plan_id: str,
) -> None:
    """Fail closed unless claimed receipt effects match durable execution facts."""

    if not isinstance(ledger, RealExecutionLedger):
        raise RoutingContractError("ledger must be a RealExecutionLedger")
    try:
        saga = ledger.saga(parent_plan_id)
    except KeyError as exc:
        raise RoutingContractError(
            "parent plan is absent from durable execution ledger"
        ) from exc

    for item in observations:
        # UNKNOWN without a receipt is safe to preserve: it blocks routing rather
        # than claiming a terminal external effect. If a receipt is supplied, it
        # must still agree with the durable attempt below.
        if item.external_receipt_id is None:
            if item.effect is ExternalEffect.UNKNOWN:
                continue
            raise RoutingContractError(
                "ledger-backed terminal reconciliation requires external_receipt_id"
            )

        identity = ExternalReceiptIdentity(
            bookmaker_id=item.venue_id,
            account_id=item.account_id,
            external_receipt_id=item.external_receipt_id,
        )
        attempt_id = saga.receipts.get(identity)
        if attempt_id is None:
            raise RoutingContractError(
                "routing receipt is absent from durable execution ledger"
            )
        if saga.attempt_action_ids.get(attempt_id) != item.proposal_leg_id:
            raise RoutingContractError(
                "durable receipt attempt does not own canonical proposal child"
            )

        state = saga.attempts.get(attempt_id)
        if state not in _LEDGER_STATES_BY_EFFECT[item.effect]:
            raise RoutingContractError(
                "routing receipt effect disagrees with durable execution state"
            )


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


def reconcile_equal_split_residual_against_ledger(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation],
    *,
    routing_request_id: str,
    parent_plan_id: str,
    stake_quantum: Decimal,
    ledger: RealExecutionLedger,
) -> ParallelRoutingProposal:
    """Reconcile only after terminal child receipts agree with durable execution.

    The routing parent plan ID is also the durable execution plan ID, and every
    receipt-backed observation must resolve through provider/account receipt
    identity to an attempt whose action ID is the canonical proposal leg ID. The
    durable attempt state must agree with the routing effect. This function is
    read-only with respect to the ledger and never calls a provider or moves money.

    UNKNOWN without an external receipt remains admissible only because it blocks
    routing. No accepted-stake equality claim is made here; the public durable saga
    currently exposes receipt ownership and attempt state, not acknowledgement
    payload amounts.
    """
    venues = tuple(selected_venues)
    normalized = _validated_child_receipts(
        venues,
        observations,
        routing_request_id=routing_request_id,
        parent_plan_id=parent_plan_id,
    )
    _validate_durable_effect_receipts(
        normalized,
        ledger=ledger,
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
