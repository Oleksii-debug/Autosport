from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
    route_residual,
)


_SHA256_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise RoutingContractError(f"{name} must be a non-empty trimmed string")
    if "\x00" in value:
        raise RoutingContractError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RoutingContractError(f"{name} must be UTF-8 encodable") from exc
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise RoutingContractError(f"{name} must be an exact positive finite Decimal")
    return value


def _canonical_hash(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _SHA256_HEX for character in text):
        raise RoutingContractError(f"{name} must be a canonical lowercase SHA-256 digest")
    return text


def _leg_id(
    *,
    parent_plan_id: str,
    routing_request_id: str,
    venue: VenueQuote,
    proposed_stake: Decimal,
) -> str:
    payload = {
        "account_id": venue.account_id,
        "parent_plan_id": parent_plan_id,
        "proposed_stake": str(proposed_stake),
        "quote": venue.quote.to_dict(),
        "routing_request_id": routing_request_id,
        "schema": "autosport-routing-proposal-leg-v1",
        "venue_id": venue.venue_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class VenueLegProposal:
    """One non-money-moving child proposal bound to a parent plan and exact quote."""

    parent_plan_id: str
    routing_request_id: str
    leg_id: str
    venue: VenueQuote
    proposed_stake: Decimal

    def __post_init__(self) -> None:
        _text(self.parent_plan_id, "parent_plan_id")
        _text(self.routing_request_id, "routing_request_id")
        _canonical_hash(self.leg_id, "leg_id")
        if not isinstance(self.venue, VenueQuote):
            raise RoutingContractError("venue must be VenueQuote")
        _positive_decimal(self.proposed_stake, "proposed_stake")
        expected = _leg_id(
            parent_plan_id=self.parent_plan_id,
            routing_request_id=self.routing_request_id,
            venue=self.venue,
            proposed_stake=self.proposed_stake,
        )
        if self.leg_id != expected:
            raise RoutingContractError("leg_id does not match canonical proposal identity")


@dataclass(frozen=True, slots=True)
class ParallelRoutingProposal:
    """Deterministic proposal only; it grants no execution or acknowledgement authority."""

    state: RoutingState
    parent_plan_id: str
    routing_request_id: str
    residual_before: Decimal
    confirmed_total: Decimal
    proposed_total: Decimal
    stake_quantum: Decimal
    legs: tuple[VenueLegProposal, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, RoutingState):
            raise RoutingContractError("state must be RoutingState")
        _text(self.parent_plan_id, "parent_plan_id")
        _text(self.routing_request_id, "routing_request_id")
        residual = self.residual_before
        confirmed = self.confirmed_total
        proposed = self.proposed_total
        if not isinstance(residual, Decimal) or not residual.is_finite() or residual < 0:
            raise RoutingContractError("residual_before must be an exact non-negative Decimal")
        if not isinstance(confirmed, Decimal) or not confirmed.is_finite() or confirmed < 0:
            raise RoutingContractError("confirmed_total must be an exact non-negative Decimal")
        if not isinstance(proposed, Decimal) or not proposed.is_finite() or proposed < 0:
            raise RoutingContractError("proposed_total must be an exact non-negative Decimal")
        quantum = _positive_decimal(self.stake_quantum, "stake_quantum")
        if proposed > residual:
            raise RoutingContractError("proposed_total cannot exceed residual_before")
        if any(not isinstance(leg, VenueLegProposal) for leg in self.legs):
            raise RoutingContractError("legs must contain VenueLegProposal values")
        if any(
            leg.parent_plan_id != self.parent_plan_id
            or leg.routing_request_id != self.routing_request_id
            for leg in self.legs
        ):
            raise RoutingContractError("all legs must belong to the same parent/request")
        if len({leg.leg_id for leg in self.legs}) != len(self.legs):
            raise RoutingContractError("proposal leg identities must be unique")
        if sum((leg.proposed_stake for leg in self.legs), Decimal("0")) != proposed:
            raise RoutingContractError("proposal leg stakes must sum to proposed_total")
        if any(divmod(leg.proposed_stake, quantum)[1] != 0 for leg in self.legs):
            raise RoutingContractError("every proposal leg must respect stake_quantum")
        if self.state is RoutingState.BLOCKED_UNKNOWN and self.legs:
            raise RoutingContractError("unknown external effect cannot have proposal legs")
        if self.state is RoutingState.COMPLETE and (self.legs or proposed != 0 or residual != 0):
            raise RoutingContractError("complete routing cannot have residual proposal")


def _equal_split_units(total_units: int, capacity_units: tuple[int, ...]) -> tuple[int, ...]:
    """Water-fill integer units, preserving selected-venue order for one-unit remainder."""

    allocations = [0] * len(capacity_units)
    remaining = total_units
    active = [index for index, capacity in enumerate(capacity_units) if capacity > 0]

    while remaining > 0 and active:
        share, _ = divmod(remaining, len(active))
        saturated = [
            index
            for index in active
            if capacity_units[index] - allocations[index] < share
        ]
        if saturated:
            for index in saturated:
                room = capacity_units[index] - allocations[index]
                allocations[index] += room
                remaining -= room
            active = [
                index
                for index in active
                if allocations[index] < capacity_units[index]
            ]
            continue

        if share:
            for index in active:
                allocations[index] += share
            remaining -= share * len(active)

        if remaining:
            for index in active:
                if remaining == 0:
                    break
                if allocations[index] < capacity_units[index]:
                    allocations[index] += 1
                    remaining -= 1

        active = [
            index
            for index in active
            if allocations[index] < capacity_units[index]
        ]

    return tuple(allocations)


def plan_equal_split_residual(
    requested_stake: Decimal,
    selected_venues: Iterable[VenueQuote],
    observations: Iterable[VenueObservation] = (),
    *,
    routing_request_id: str,
    parent_plan_id: str,
    stake_quantum: Decimal,
) -> ParallelRoutingProposal:
    """Propose a parallel equal split across already-selected eligible venues.

    The function is deliberately non-money-moving. It reuses ``route_residual`` as
    the fail-closed authority for request/quote/observation validation, UNKNOWN
    handling and confirmed residual arithmetic. The caller supplies the exact stake
    quantum; no bookmaker granularity is invented here. Remaining stake is allocated
    as integer quantum units with deterministic water-filling. A one-unit remainder
    follows the caller's selected-venue order, so identical evidence yields identical
    child proposal identities.

    ``RoutingState.ROUTE`` means the full residual is covered by proposal legs.
    ``RoutingState.PARTIAL`` means only a strict subset can be proposed with current
    selected capacity. ``RoutingState.UNEXECUTABLE`` means none can be proposed.
    Neither state is an acknowledgement, receipt or real-execution result.
    """

    venues = tuple(selected_venues)
    obs = tuple(observations)
    parent_id = _text(parent_plan_id, "parent_plan_id")
    request_id = _text(routing_request_id, "routing_request_id")
    quantum = _positive_decimal(stake_quantum, "stake_quantum")

    base = route_residual(
        requested_stake,
        venues,
        obs,
        routing_request_id=request_id,
    )
    if base.state is RoutingState.BLOCKED_UNKNOWN:
        return ParallelRoutingProposal(
            state=RoutingState.BLOCKED_UNKNOWN,
            parent_plan_id=parent_id,
            routing_request_id=request_id,
            residual_before=base.residual,
            confirmed_total=base.confirmed_total,
            proposed_total=Decimal("0"),
            stake_quantum=quantum,
            legs=(),
        )
    if base.state is RoutingState.COMPLETE:
        return ParallelRoutingProposal(
            state=RoutingState.COMPLETE,
            parent_plan_id=parent_id,
            routing_request_id=request_id,
            residual_before=Decimal("0"),
            confirmed_total=base.confirmed_total,
            proposed_total=Decimal("0"),
            stake_quantum=quantum,
            legs=(),
        )

    residual_units, residual_remainder = divmod(base.residual, quantum)
    if residual_remainder != 0:
        raise RoutingContractError(
            "residual stake must be an exact multiple of stake_quantum"
        )
    target_units = int(residual_units)

    refused: set[tuple[str, str]] = set()
    accepted_by_identity: dict[tuple[str, str], Decimal] = {}
    for item in obs:
        identity = (item.venue_id, item.account_id)
        if item.effect is ExternalEffect.MARKET_REFUSED:
            refused.add(identity)
        elif item.effect is ExternalEffect.ACCEPTED:
            accepted_by_identity[identity] = (
                accepted_by_identity.get(identity, Decimal("0"))
                + item.confirmed_accepted
            )

    eligible: list[VenueQuote] = []
    capacity_units: list[int] = []
    for venue in venues:
        identity = (venue.venue_id, venue.account_id)
        if identity in refused or not venue.account_enabled:
            continue
        remaining_capacity = venue.acceptance_ceiling - accepted_by_identity.get(
            identity,
            Decimal("0"),
        )
        units, _ = divmod(remaining_capacity, quantum)
        available_units = int(units)
        if available_units <= 0:
            continue
        eligible.append(venue)
        capacity_units.append(available_units)

    allocations = _equal_split_units(target_units, tuple(capacity_units))
    legs: list[VenueLegProposal] = []
    for venue, units in zip(eligible, allocations, strict=True):
        if units <= 0:
            continue
        proposed_stake = quantum * units
        legs.append(
            VenueLegProposal(
                parent_plan_id=parent_id,
                routing_request_id=request_id,
                leg_id=_leg_id(
                    parent_plan_id=parent_id,
                    routing_request_id=request_id,
                    venue=venue,
                    proposed_stake=proposed_stake,
                ),
                venue=venue,
                proposed_stake=proposed_stake,
            )
        )

    proposed_total = sum(
        (leg.proposed_stake for leg in legs),
        Decimal("0"),
    )
    if proposed_total == base.residual:
        state = RoutingState.ROUTE
    elif proposed_total > 0 or base.confirmed_total > 0:
        state = RoutingState.PARTIAL
    else:
        state = RoutingState.UNEXECUTABLE

    return ParallelRoutingProposal(
        state=state,
        parent_plan_id=parent_id,
        routing_request_id=request_id,
        residual_before=base.residual,
        confirmed_total=base.confirmed_total,
        proposed_total=proposed_total,
        stake_quantum=quantum,
        legs=tuple(legs),
    )
