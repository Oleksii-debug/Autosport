from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable

from .bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
    _dedupe_external_receipts,
    _exact_decimal_sum,
)
from .bookmaker_routing_plan import (
    ParallelRoutingProposal,
    VenueLegProposal,
    plan_equal_split_residual,
)
from .real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    EventType,
    ExecutionLedgerError,
    RealExecutionLedger,
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


def _verified_ledger_events(
    ledger: RealExecutionLedger,
) -> tuple[dict[str, object], ...]:
    """Read one snapshot through the canonical ledger implementation only.

    Positive routing authority must not depend on caller-dispatched instance
    methods. Exact-type admission blocks subclass overrides, and the unbound
    canonical parser prevents instance method rebinding from substituting bytes
    or parser semantics from another ledger.
    """

    if type(ledger) is not RealExecutionLedger:
        raise RoutingContractError("ledger must be an exact RealExecutionLedger")
    try:
        raw = ledger.path.read_bytes() if ledger.path.exists() else b""
        events = RealExecutionLedger._parse(raw)
    except (ExecutionLedgerError, OSError) as exc:
        raise RoutingContractError(
            "durable execution ledger could not be verified"
        ) from exc
    return tuple(events)


def _durable_plan_action(
    events: tuple[dict[str, object], ...],
    *,
    parent_plan_id: str,
    action_id: str,
) -> dict[str, object]:
    plans = [
        event
        for event in events
        if event.get("plan_id") == parent_plan_id
        and event.get("event_type") == EventType.PLAN_RESERVED.value
    ]
    if len(plans) != 1:
        raise RoutingContractError(
            "parent plan is absent or ambiguous in durable execution ledger"
        )
    try:
        plan = plans[0]["payload"]["plan"]  # type: ignore[index]
        if not isinstance(plan, dict) or plan.get("plan_id") != parent_plan_id:
            raise TypeError("stored plan payload is invalid")
        actions = plan["actions"]
        if not isinstance(actions, list):
            raise TypeError("stored plan actions are invalid")
    except (KeyError, TypeError) as exc:
        raise RoutingContractError(
            "durable execution plan payload is invalid"
        ) from exc

    matches = [
        action
        for action in actions
        if isinstance(action, dict) and action.get("action_id") == action_id
    ]
    if len(matches) != 1:
        raise RoutingContractError(
            "durable receipt attempt does not own canonical proposal child"
        )
    return matches[0]


def _durable_decimal(value: object, name: str) -> Decimal:
    if type(value) is not str:
        raise RoutingContractError(f"durable {name} is not canonical Decimal text")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise RoutingContractError(f"durable {name} is not a valid Decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise RoutingContractError(f"durable {name} must be finite and positive")
    return parsed


def _require_durable_action_matches_child(
    action: dict[str, object],
    item: VenueObservation,
) -> Decimal:
    exact_text = {
        "bookmaker_id": item.venue_id,
        "account_id": item.account_id,
        "event_id": item.quote.event_id,
        "market_id": item.quote.market_id,
        "selection_id": item.quote.selection_id,
        "quote_id": item.quote.market_event_hash,
        "quote_observed_at": item.quote.observed_ts,
    }
    for field, expected in exact_text.items():
        if action.get(field) != expected:
            raise RoutingContractError(
                f"durable execution action {field} mismatches canonical routing child"
            )

    # This routing contract has no LAY-liability representation. The canonical
    # supervised bridge currently emits BACK only; anything else must fail closed.
    if action.get("side") != "BACK":
        raise RoutingContractError(
            "durable execution action side is outside routing child authority"
        )

    durable_odds = _durable_decimal(
        action.get("requested_odds"), "requested_odds"
    )
    if durable_odds != item.quote.decimal_odds:
        raise RoutingContractError(
            "durable execution action odds mismatch canonical routing child"
        )
    durable_requested = _durable_decimal(
        action.get("requested_stake"), "requested_stake"
    )
    if item.proposed_stake is None or durable_requested != item.proposed_stake:
        raise RoutingContractError(
            "durable execution action stake mismatch canonical routing child"
        )
    return durable_requested


def _matching_receipt_events(
    events: tuple[dict[str, object], ...],
    *,
    parent_plan_id: str,
    action_id: str,
    external_receipt_id: str,
    event_type: EventType,
) -> tuple[dict[str, object], ...]:
    matches: list[dict[str, object]] = []
    for event in events:
        if (
            event.get("plan_id") != parent_plan_id
            or event.get("action_id") != action_id
            or event.get("event_type") != event_type.value
        ):
            continue
        payload = event.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("external_receipt_id") == external_receipt_id
        ):
            matches.append(event)
    return tuple(matches)


def _has_unresolved_partial_acceptance(
    observations: tuple[VenueObservation, ...],
) -> bool:
    """Return whether a child has confirmed stake but no child-bound closure."""

    closed_children = {
        item.proposal_leg_id
        for item in observations
        if item.effect is ExternalEffect.MARKET_REFUSED
    }
    return any(
        item.effect is ExternalEffect.ACCEPTED
        and item.proposal_leg_id not in closed_children
        and item.proposed_stake is not None
        and item.confirmed_accepted < item.proposed_stake
        for item in observations
    )


def _block_positive_reroute(
    proposal: ParallelRoutingProposal,
) -> ParallelRoutingProposal:
    if (
        proposal.state is RoutingState.BLOCKED_UNKNOWN
        and proposal.proposed_total == Decimal("0")
        and not proposal.legs
    ):
        return proposal
    return ParallelRoutingProposal(
        state=RoutingState.BLOCKED_UNKNOWN,
        parent_plan_id=proposal.parent_plan_id,
        routing_request_id=proposal.routing_request_id,
        residual_before=proposal.residual_before,
        confirmed_total=proposal.confirmed_total,
        proposed_total=Decimal("0"),
        stake_quantum=proposal.stake_quantum,
        legs=(),
    )


def _durable_attempt_states(
    events: tuple[dict[str, object], ...],
    *,
    parent_plan_id: str,
) -> dict[str, AttemptState]:
    """Project attempt states from the same frozen, already-verified snapshot."""

    states: dict[str, AttemptState] = {}
    for event in events:
        if event.get("plan_id") != parent_plan_id:
            continue
        attempt_id = event.get("attempt_id")
        if type(attempt_id) is not str:
            continue
        kind = event.get("event_type")
        if kind == EventType.ATTEMPT_RESERVED.value:
            states[attempt_id] = AttemptState.RESERVED
        elif kind == EventType.ATTEMPT_SUBMITTED.value:
            states[attempt_id] = AttemptState.SUBMITTED
        elif kind == EventType.ATTEMPT_UNKNOWN.value:
            states[attempt_id] = AttemptState.UNKNOWN
        elif kind == EventType.RECONCILED_NOT_FOUND.value:
            states[attempt_id] = AttemptState.RECONCILED_NOT_FOUND
        elif kind == EventType.EXTERNAL_ACKNOWLEDGEMENT.value:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise RoutingContractError(
                    "durable acknowledgement payload is invalid"
                )
            try:
                states[attempt_id] = AttemptState(payload["status"])
            except (KeyError, ValueError) as exc:
                raise RoutingContractError(
                    "durable acknowledgement status is invalid"
                ) from exc
    return states


def _validate_durable_effect_receipts(
    observations: tuple[VenueObservation, ...],
    *,
    ledger: RealExecutionLedger,
    parent_plan_id: str,
    requested_stake: Decimal,
) -> bool:
    """Bind routing claims to one fenced durable snapshot.

    Returns True when durable evidence still represents a potentially-live partial
    remainder and therefore cannot authorize a positive residual reroute.
    """

    events = _verified_ledger_events(ledger)
    plan_events = tuple(
        event
        for event in events
        if event.get("plan_id") == parent_plan_id
        and event.get("event_type") == EventType.PLAN_RESERVED.value
    )
    if len(plan_events) != 1:
        raise RoutingContractError(
            "parent plan is absent or ambiguous in durable execution ledger"
        )
    try:
        actions = plan_events[0]["payload"]["plan"]["actions"]  # type: ignore[index]
        if not isinstance(actions, list) or not all(
            isinstance(action, dict)
            and type(action.get("action_id")) is str
            and bool(action["action_id"].strip())
            for action in actions
        ):
            raise TypeError("stored plan actions are invalid")
    except (KeyError, TypeError) as exc:
        raise RoutingContractError(
            "durable execution plan payload is invalid"
        ) from exc

    if (
        not isinstance(requested_stake, Decimal)
        or not requested_stake.is_finite()
        or requested_stake <= 0
    ):
        raise RoutingContractError(
            "requested_stake must be an exact positive finite Decimal"
        )
    durable_parent_amount = _exact_decimal_sum(
        _durable_decimal(action.get("requested_stake"), "requested_stake")
        for action in actions
    )
    if requested_stake != durable_parent_amount:
        raise RoutingContractError(
            "requested_stake does not match durable parent execution amount"
        )

    attempted_action_ids = {
        event.get("action_id")
        for event in events
        if event.get("plan_id") == parent_plan_id
        and event.get("event_type") == EventType.ATTEMPT_RESERVED.value
    }
    unattempted_action_exists = any(
        action["action_id"] not in attempted_action_ids
        for action in actions
    )

    attempt_states = _durable_attempt_states(
        events,
        parent_plan_id=parent_plan_id,
    )
    unresolved_partial = unattempted_action_exists or any(
        state in {
            AttemptState.RESERVED,
            AttemptState.SUBMITTED,
            AttemptState.UNKNOWN,
            AttemptState.PARTIAL,
            # Persistence alone is not trusted provider-absence provenance.
            # Until a product-issued absence authority is integrated, NOT_FOUND
            # remains fail-closed for positive residual routing.
            AttemptState.RECONCILED_NOT_FOUND,
        }
        for state in attempt_states.values()
    )
    for item in observations:
        if item.external_receipt_id is None:
            if item.effect is ExternalEffect.UNKNOWN:
                continue
            raise RoutingContractError(
                "ledger-backed terminal reconciliation requires external_receipt_id"
            )
        if item.proposal_leg_id is None:
            raise RoutingContractError(
                "ledger-backed reconciliation requires canonical proposal child"
            )

        action = _durable_plan_action(
            events,
            parent_plan_id=parent_plan_id,
            action_id=item.proposal_leg_id,
        )
        durable_requested = _require_durable_action_matches_child(action, item)

        acknowledgements = _matching_receipt_events(
            events,
            parent_plan_id=parent_plan_id,
            action_id=item.proposal_leg_id,
            external_receipt_id=item.external_receipt_id,
            event_type=EventType.EXTERNAL_ACKNOWLEDGEMENT,
        )
        found_reconciliations = _matching_receipt_events(
            events,
            parent_plan_id=parent_plan_id,
            action_id=item.proposal_leg_id,
            external_receipt_id=item.external_receipt_id,
            event_type=EventType.RECONCILED_FOUND,
        )

        if item.effect is ExternalEffect.UNKNOWN:
            if acknowledgements or not found_reconciliations:
                raise RoutingContractError(
                    "routing receipt effect disagrees with durable execution state"
                )
            continue

        if len(acknowledgements) != 1:
            raise RoutingContractError(
                "routing receipt is absent from durable execution ledger or is ambiguous"
            )
        payload = acknowledgements[0].get("payload")
        if not isinstance(payload, dict):
            raise RoutingContractError(
                "durable acknowledgement payload is invalid"
            )
        try:
            status = AcknowledgementStatus(payload["status"])
        except (KeyError, ValueError) as exc:
            raise RoutingContractError(
                "durable acknowledgement status is invalid"
            ) from exc

        if item.effect is ExternalEffect.MARKET_REFUSED:
            if status is not AcknowledgementStatus.REJECTED:
                raise RoutingContractError(
                    "routing receipt effect disagrees with durable execution state"
                )
            continue

        if item.effect is not ExternalEffect.ACCEPTED or status not in {
            AcknowledgementStatus.ACCEPTED,
            AcknowledgementStatus.PARTIAL,
        }:
            raise RoutingContractError(
                "routing receipt effect disagrees with durable execution state"
            )

        durable_accepted = _durable_decimal(
            payload.get("accepted_stake"), "accepted_stake"
        )
        if item.confirmed_accepted != durable_accepted:
            raise RoutingContractError(
                "routing accepted amount disagrees with durable acknowledgement"
            )

        # PARTIAL is not terminality proof: the unmatched provider order remainder
        # may still execute. Even a provider-labelled ACCEPTED record is not enough
        # for residual authority if its durable amount is below the bound request.
        if (
            status is AcknowledgementStatus.PARTIAL
            or durable_accepted < durable_requested
        ):
            unresolved_partial = True

    provided_receipts = {
        (item.proposal_leg_id, item.external_receipt_id)
        for item in observations
        if item.external_receipt_id is not None
    }
    durable_terminal_receipts: set[tuple[object, object]] = set()
    for event in events:
        if (
            event.get("plan_id") != parent_plan_id
            or event.get("event_type") != EventType.EXTERNAL_ACKNOWLEDGEMENT.value
        ):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise RoutingContractError(
                "durable acknowledgement payload is invalid"
            )
        durable_terminal_receipts.add(
            (event.get("action_id"), payload.get("external_receipt_id"))
        )
    if durable_terminal_receipts.difference(provided_receipts):
        raise RoutingContractError(
            "reconciliation observations omit durable terminal receipt"
        )

    return unresolved_partial


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
    proposal = plan_equal_split_residual(
        requested_stake,
        venues,
        normalized,
        routing_request_id=routing_request_id,
        parent_plan_id=parent_plan_id,
        stake_quantum=stake_quantum,
    )
    if _has_unresolved_partial_acceptance(normalized):
        return _block_positive_reroute(proposal)
    return proposal


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

    The routing parent plan ID is also the durable execution plan ID. An optimistic
    integrity-verified preflight may reject invalid evidence early, but no positive
    routing result is authorized from that read. The durable facts are re-resolved
    under the ledger's existing writer-serialization boundary and that boundary is
    held through routing-result construction, so a concurrent execution mutation
    cannot race a stale snapshot into positive residual authority.

    Omitting any durable terminal receipt fails closed. Any durable RESERVED,
    SUBMITTED, UNKNOWN, or PARTIAL attempt keeps positive reroute authority blocked
    until the external effect is conclusively resolved. UNKNOWN without an external
    receipt remains admissible only because it blocks routing.

    Positive reroute also requires the caller's parent amount to equal the exact
    sum of requested stakes in the durable execution plan. The ledger schema has no
    separate original routing-request amount, so a partially covered parent cannot
    safely mint a larger residual from caller input; it remains fail-closed until a
    product-owned parent-amount authority exists.

    This function does not append ledger events, call a provider, or move money.
    It reuses the ledger writer boundary as a short read-authority lease so reads
    and writes share one serialization domain.
    """
    venues = tuple(selected_venues)
    normalized = _validated_child_receipts(
        venues,
        observations,
        routing_request_id=routing_request_id,
        parent_plan_id=parent_plan_id,
    )

    # Optimistic fail-fast validation only. A writer may legitimately advance the
    # ledger after this snapshot; therefore this result is never returned as
    # positive authority without the serialized re-resolution below.
    _validate_durable_effect_receipts(
        normalized,
        ledger=ledger,
        parent_plan_id=parent_plan_id,
        requested_stake=requested_stake,
    )

    def resolve_under_ledger_serialization() -> ParallelRoutingProposal:
        durable_unresolved_partial = _validate_durable_effect_receipts(
            normalized,
            ledger=ledger,
            parent_plan_id=parent_plan_id,
            requested_stake=requested_stake,
        )
        proposal = plan_equal_split_residual(
            requested_stake,
            venues,
            normalized,
            routing_request_id=routing_request_id,
            parent_plan_id=parent_plan_id,
            stake_quantum=stake_quantum,
        )
        if (
            durable_unresolved_partial
            or _has_unresolved_partial_acceptance(normalized)
        ):
            return _block_positive_reroute(proposal)
        return proposal

    try:
        # Use the product-owned implementation, not a caller-rebound instance
        # method. _mutate holds the existing per-instance RLock plus exact
        # cross-instance writer-lock path until the callback returns. The callback
        # is read-only, so this is a serialization lease rather than a ledger
        # mutation or a second lock authority.
        return RealExecutionLedger._mutate(
            ledger,
            resolve_under_ledger_serialization,
        )
    except (ExecutionLedgerError, OSError) as exc:
        raise RoutingContractError(
            "durable execution ledger could not be serialized for reconciliation"
        ) from exc
