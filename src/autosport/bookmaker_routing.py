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
    """Externally reconciled effect; this is evidence input, not a receipt ledger."""

    venue_id: str
    account_id: str
    effect: ExternalEffect
    confirmed_accepted: Decimal = Decimal("0")
    observation_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.venue_id, "observation venue_id")
        _text(self.account_id, "observation account_id")
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


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    state: RoutingState
    residual: Decimal
    next_venue: VenueQuote | None
    confirmed_total: Decimal
    proposed_stake: Decimal = Decimal("0")


def route_residual(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation] = (),
) -> RoutingDecision:
    """Choose the next preselected venue without creating execution/receipt authority.

    Only externally confirmed ACCEPTED observations reduce the residual. Any UNKNOWN
    external effect blocks retry/reroute even when arithmetic would otherwise look
    complete, because the missing acknowledgement may represent a real placement.
    MARKET_REFUSED is scoped to that venue/market request and never disables the
    account globally. Acceptance ceilings cap each proposed routing amount.
    """

    requested = _amount(requested_stake, "requested_stake", positive=True)
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
    obs = tuple(observations)
    if any(not isinstance(item, VenueObservation) for item in obs):
        raise RoutingContractError(
            "observations must contain VenueObservation values"
        )

    observation_identities = [
        (item.venue_id, item.account_id) for item in obs
    ]
    counts = Counter(observation_identities)
    observation_ids: set[str] = set()
    for item, identity in zip(obs, observation_identities, strict=True):
        if identity not in by_identity:
            raise RoutingContractError(
                "observation references an unselected venue/account"
            )
        if counts[identity] > 1 and item.observation_id is None:
            raise RoutingContractError(
                "repeated venue/account observations require unique observation_id"
            )
        if item.observation_id is not None:
            if item.observation_id in observation_ids:
                raise RoutingContractError(
                    "duplicate observation_id is ambiguous"
                )
            observation_ids.add(item.observation_id)

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
