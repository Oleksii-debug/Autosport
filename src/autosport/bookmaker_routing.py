from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .opportunity import QuoteRef


class RoutingContractError(ValueError):
    """Raised when multi-venue routing authority is ambiguous or unsafe."""


class ExternalEffect(str, Enum):
    ACCEPTED = "accepted"
    MARKET_REFUSED = "market_refused"
    UNKNOWN = "unknown"


class RoutingState(str, Enum):
    ROUTE = "route"
    PARTIAL = "partial"
    COMPLETE = "complete"
    BLOCKED_UNKNOWN = "blocked_unknown"
    UNEXECUTABLE = "unexecutable"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise RoutingContractError(f"{name} must be a non-empty trimmed string")
    return value


def _amount(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RoutingContractError(f"{name} must be an exact finite Decimal")
    if value < 0 or (positive and value <= 0):
        raise RoutingContractError(
            f"{name} must be {'positive' if positive else 'non-negative'}"
        )
    return value


@dataclass(frozen=True, slots=True)
class VenueQuote:
    """One preselected execution venue bound to its own immutable quote evidence."""

    venue_id: str
    account_id: str
    quote: QuoteRef
    acceptance_ceiling: Decimal
    account_enabled: bool = True

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        if not isinstance(self.quote, QuoteRef):
            raise RoutingContractError("quote must be QuoteRef")
        if self.quote.source_id != self.venue_id:
            raise RoutingContractError("venue_id must match quote source_id")
        _amount(self.acceptance_ceiling, "acceptance_ceiling")
        if type(self.account_enabled) is not bool:
            raise RoutingContractError("account_enabled must be a boolean")


@dataclass(frozen=True, slots=True)
class VenueObservation:
    """Externally reconciled effect bound to one routing request and exact quote.

    The optional child identity fields bind evidence to one deterministic
    non-money-moving proposal leg. An external receipt is separate evidence:
    child-bound ACCEPTED effects require it, while UNKNOWN/refusal may truthfully
    have no provider receipt yet. This does not create a durable execution ledger;
    that future authority remains owned by canonical #353.
    """

    venue_id: str
    account_id: str
    effect: ExternalEffect
    routing_request_id: str
    quote: QuoteRef
    confirmed_accepted: Decimal = Decimal("0")
    observation_id: str | None = None
    parent_plan_id: str | None = None
    proposal_leg_id: str | None = None
    proposed_stake: Decimal | None = None
    external_receipt_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.venue_id, "observation venue_id")
        _text(self.account_id, "observation account_id")
        _text(self.routing_request_id, "observation routing_request_id")
        if not isinstance(self.quote, QuoteRef):
            raise RoutingContractError("observation quote must be QuoteRef")
        if self.quote.source_id != self.venue_id:
            raise RoutingContractError(
                "observation venue_id must match observation quote source_id"
            )
        if not isinstance(self.effect, ExternalEffect):
            raise RoutingContractError("effect must be ExternalEffect")
        accepted = _amount(self.confirmed_accepted, "confirmed_accepted")
        if self.effect is not ExternalEffect.ACCEPTED and accepted != 0:
            raise RoutingContractError(
                "only ACCEPTED evidence may carry confirmed accepted stake"
            )
        if self.effect is ExternalEffect.ACCEPTED and accepted <= 0:
            raise RoutingContractError(
                "ACCEPTED evidence requires positive confirmed stake"
            )
        if self.observation_id is not None:
            _text(self.observation_id, "observation_id")

        child_fields = (
            self.parent_plan_id,
            self.proposal_leg_id,
            self.proposed_stake,
        )
        child_bound = any(value is not None for value in child_fields)
        if child_bound:
            if any(value is None for value in child_fields):
                raise RoutingContractError(
                    "child identity requires parent_plan_id, proposal_leg_id and "
                    "proposed_stake together"
                )
            _text(self.parent_plan_id, "parent_plan_id")
            _text(self.proposal_leg_id, "proposal_leg_id")
            bound_stake = _amount(
                self.proposed_stake,
                "proposed_stake",
                positive=True,
            )
            if accepted > bound_stake:
                raise RoutingContractError(
                    "confirmed accepted stake exceeds bound proposal stake"
                )
        elif self.external_receipt_id is not None:
            raise RoutingContractError(
                "external_receipt_id requires deterministic child identity"
            )

        if self.external_receipt_id is not None:
            _text(self.external_receipt_id, "external_receipt_id")
        if (
            child_bound
            and self.effect is ExternalEffect.ACCEPTED
            and self.external_receipt_id is None
        ):
            raise RoutingContractError(
                "child-bound ACCEPTED evidence requires external_receipt_id"
            )


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    state: RoutingState
    residual: Decimal
    next_venue: VenueQuote | None
    confirmed_total: Decimal
    proposed_stake: Decimal = Decimal("0")


def _dedupe_external_receipts(
    observations: Iterable[VenueObservation],
) -> tuple[VenueObservation, ...]:
    """Deduplicate exact receipt replays and fail closed on conflicting reuse.

    External receipt identifiers are provider/account scoped because independent
    bookmakers may legitimately issue the same textual identifier. Re-observing
    the exact same immutable receipt is idempotent; changing any bound evidence for
    the same receipt identity is ambiguous and therefore rejected.
    """

    normalized: list[VenueObservation] = []
    receipts: dict[tuple[str, str, str], VenueObservation] = {}
    for item in observations:
        if item.external_receipt_id is None:
            normalized.append(item)
            continue
        key = (item.venue_id, item.account_id, item.external_receipt_id)
        previous = receipts.get(key)
        if previous is None:
            receipts[key] = item
            normalized.append(item)
            continue
        if previous != item:
            raise RoutingContractError(
                "conflicting external receipt identity is ambiguous"
            )
    return tuple(normalized)


def route_residual(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation] = (),
    *,
    routing_request_id: str,
) -> RoutingDecision:
    """Choose the next preselected venue without creating execution/receipt authority.

    Only externally confirmed ACCEPTED observations bound to this exact routing
    request and selected QuoteRef reduce the residual. Any UNKNOWN external effect
    blocks retry/reroute even when arithmetic would otherwise look complete, because
    the missing acknowledgement may represent a real placement. MARKET_REFUSED is
    scoped to this request's selected quote and never disables the account globally.
    Acceptance ceilings cap each proposed routing amount.
    """

    requested = _amount(requested_stake, "requested_stake", positive=True)
    request_id = _text(routing_request_id, "routing_request_id")
    venues = tuple(selected_venues)
    if not venues:
        raise RoutingContractError("at least one selected venue is required")
    if any(not isinstance(item, VenueQuote) for item in venues):
        raise RoutingContractError(
            "selected_venues must contain VenueQuote values"
        )

    identities = [(v.venue_id, v.account_id) for v in venues]
    if len(set(identities)) != len(identities):
        raise RoutingContractError(
            "selected venue/account identities must be unique"
        )

    quote_keys = {v.quote.quote_key for v in venues}
    if len(quote_keys) != 1:
        raise RoutingContractError(
            "selected venue quotes must refer to the same market selection"
        )

    by_identity = {(v.venue_id, v.account_id): v for v in venues}
    raw_obs = tuple(observations)
    if any(not isinstance(item, VenueObservation) for item in raw_obs):
        raise RoutingContractError(
            "observations must contain VenueObservation values"
        )
    obs = _dedupe_external_receipts(raw_obs)

    observation_identities = [
        (item.venue_id, item.account_id) for item in obs
    ]
    counts = Counter(observation_identities)
    observation_ids: set[str] = set()
    child_leg_bindings: dict[
        tuple[str, str],
        tuple[str, str, str, QuoteRef, Decimal],
    ] = {}
    accepted_receipt_by_child: dict[tuple[str, str], str] = {}
    for item, identity in zip(obs, observation_identities, strict=True):
        if identity not in by_identity:
            raise RoutingContractError(
                "observation references an unselected venue/account"
            )
        if item.routing_request_id != request_id:
            raise RoutingContractError(
                "observation routing_request_id does not match routing request"
            )
        selected_quote = by_identity[identity].quote
        if item.quote != selected_quote:
            raise RoutingContractError(
                "observation quote does not exactly match selected venue quote"
            )
        if (
            counts[identity] > 1
            and item.observation_id is None
            and item.external_receipt_id is None
        ):
            raise RoutingContractError(
                "repeated venue/account observations require unique observation_id "
                "or external_receipt_id"
            )
        if item.observation_id is not None:
            if item.observation_id in observation_ids:
                raise RoutingContractError(
                    "duplicate observation_id is ambiguous"
                )
            observation_ids.add(item.observation_id)
        if item.proposal_leg_id is not None:
            child_key = (item.parent_plan_id, item.proposal_leg_id)
            child_binding = (
                item.routing_request_id,
                item.venue_id,
                item.account_id,
                item.quote,
                item.proposed_stake,
            )
            previous = child_leg_bindings.get(child_key)
            if previous is not None and previous != child_binding:
                raise RoutingContractError(
                    "proposal leg identity has conflicting bound evidence"
                )
            child_leg_bindings[child_key] = child_binding
            if item.effect is ExternalEffect.ACCEPTED:
                if item.external_receipt_id is None:
                    raise RoutingContractError(
                        "child-bound ACCEPTED evidence requires external_receipt_id"
                    )
                previous_receipt = accepted_receipt_by_child.get(child_key)
                if (
                    previous_receipt is not None
                    and previous_receipt != item.external_receipt_id
                ):
                    raise RoutingContractError(
                        "multiple external receipts for one child are ambiguous"
                    )
                accepted_receipt_by_child[child_key] = item.external_receipt_id

    refused: set[tuple[str, str]] = set()
    accepted_by_identity: dict[tuple[str, str], Decimal] = {}
    confirmed = Decimal("0")
    has_unknown = False
    for item, identity in zip(obs, observation_identities, strict=True):
        if item.effect is ExternalEffect.UNKNOWN:
            has_unknown = True
            continue
        if item.effect is ExternalEffect.MARKET_REFUSED:
            refused.add(identity)
            continue

        accepted = accepted_by_identity.get(identity, Decimal("0"))
        accepted += item.confirmed_accepted
        venue = by_identity[identity]
        if accepted > venue.acceptance_ceiling:
            raise RoutingContractError(
                "confirmed accepted stake exceeds venue acceptance ceiling"
            )
        accepted_by_identity[identity] = accepted
        confirmed += item.confirmed_accepted
        if confirmed > requested:
            raise RoutingContractError(
                "confirmed accepted stake exceeds requested stake"
            )

    residual = requested - confirmed
    if has_unknown:
        return RoutingDecision(
            RoutingState.BLOCKED_UNKNOWN,
            residual,
            None,
            confirmed,
        )
    if residual == 0:
        return RoutingDecision(
            RoutingState.COMPLETE,
            residual,
            None,
            confirmed,
        )

    for venue in venues:
        identity = (venue.venue_id, venue.account_id)
        if identity in refused or not venue.account_enabled:
            continue
        accepted = accepted_by_identity.get(identity, Decimal("0"))
        remaining_capacity = venue.acceptance_ceiling - accepted
        if remaining_capacity <= 0:
            continue
        proposed = min(residual, remaining_capacity)
        return RoutingDecision(
            RoutingState.ROUTE,
            residual,
            venue,
            confirmed,
            proposed,
        )

    state = RoutingState.PARTIAL if confirmed > 0 else RoutingState.UNEXECUTABLE
    return RoutingDecision(state, residual, None, confirmed)
