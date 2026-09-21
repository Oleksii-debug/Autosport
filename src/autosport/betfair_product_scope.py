"""Fail-closed Betfair Exchange versus Sportsbook product-family boundary.

This module owns only the provider product-family distinction. Application-key
class, market-data freshness, credentials, provider entitlement acquisition and
execution authority are owned by separate lineages.

Provider facts represented here:
- Exchange API market/order operations belong to Betfair Exchange.
- Exchange API does not expose Betfair Sportsbook odds and cannot place
  Sportsbook bets.
- the separate Sportsbook API is read-only and available only to licensed
  Affiliate partners.

A compatible result is only a static product-family routing fact. It never
proves provider entitlement, fresh/live data, provider write permission, or
Autosport execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BetfairProductScopeError(ValueError):
    """Raised when a product-family projection is structurally invalid."""


class BetfairProductDomain(str, Enum):
    EXCHANGE = "BETFAIR_EXCHANGE"
    SPORTSBOOK = "BETFAIR_SPORTSBOOK"


class BetfairProductOperation(str, Enum):
    MARKET_READ = "MARKET_READ"
    PLACE_BET = "PLACE_BET"


class BetfairProductScopeState(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    DENIED = "DENIED"


@dataclass(frozen=True, slots=True)
class BetfairProductIdentity:
    """Product-namespaced external identity; cross-product dedupe is impossible."""

    domain: BetfairProductDomain
    external_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.domain, BetfairProductDomain):
            raise BetfairProductScopeError(
                "domain must be a BetfairProductDomain value"
            )
        if (
            not isinstance(self.external_id, str)
            or not self.external_id
            or self.external_id != self.external_id.strip()
            or "\x00" in self.external_id
        ):
            raise BetfairProductScopeError(
                "external_id must be non-empty canonical text"
            )

    @property
    def provenance_key(self) -> str:
        product = (
            "exchange"
            if self.domain is BetfairProductDomain.EXCHANGE
            else "sportsbook"
        )
        return f"betfair.{product}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class BetfairProductScopeDecision:
    state: BetfairProductScopeState
    reason_codes: tuple[str, ...]
    domain: BetfairProductDomain
    provenance_domain: str
    product_bet_placement_surface_available: bool
    sportsbook_read_only: bool
    requires_external_affiliate_entitlement: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state, BetfairProductScopeState):
            raise BetfairProductScopeError(
                "state must be BetfairProductScopeState"
            )
        if not isinstance(self.domain, BetfairProductDomain):
            raise BetfairProductScopeError(
                "domain must be BetfairProductDomain"
            )
        expected_provenance = (
            "betfair.exchange"
            if self.domain is BetfairProductDomain.EXCHANGE
            else "betfair.sportsbook"
        )
        if self.provenance_domain != expected_provenance:
            raise BetfairProductScopeError(
                "provenance_domain does not match product domain"
            )
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason
            for reason in self.reason_codes
        ):
            raise BetfairProductScopeError(
                "reason_codes must be an immutable tuple of non-empty strings"
            )
        for field in (
            "product_bet_placement_surface_available",
            "sportsbook_read_only",
            "requires_external_affiliate_entitlement",
        ):
            if type(getattr(self, field)) is not bool:
                raise BetfairProductScopeError(f"{field} must be bool")
        if (
            self.state is BetfairProductScopeState.COMPATIBLE
            and self.reason_codes
        ):
            raise BetfairProductScopeError(
                "COMPATIBLE decision cannot carry denial reasons"
            )
        if (
            self.state is BetfairProductScopeState.DENIED
            and not self.reason_codes
        ):
            raise BetfairProductScopeError(
                "DENIED decision requires at least one reason"
            )

    @property
    def scope_compatible(self) -> bool:
        return self.state is BetfairProductScopeState.COMPATIBLE

    @property
    def provider_entitlement_authorized(self) -> bool:
        """Static caller configuration cannot prove provider-issued entitlement."""
        return False

    @property
    def execution_authorized(self) -> bool:
        """Product-family routing never authorizes an Autosport provider write."""
        return False


def evaluate_betfair_product_scope(
    *,
    domain: BetfairProductDomain,
    operation: BetfairProductOperation,
    sportsbook_affiliate_entitled: bool | None = None,
) -> BetfairProductScopeDecision:
    """Evaluate product-family routing without duplicating key/freshness authority."""

    if not isinstance(domain, BetfairProductDomain):
        raise BetfairProductScopeError(
            "domain must be a BetfairProductDomain value"
        )
    if not isinstance(operation, BetfairProductOperation):
        raise BetfairProductScopeError(
            "operation must be a BetfairProductOperation value"
        )
    if (
        sportsbook_affiliate_entitled is not None
        and type(sportsbook_affiliate_entitled) is not bool
    ):
        raise BetfairProductScopeError(
            "sportsbook_affiliate_entitled must be bool or None"
        )

    if domain is BetfairProductDomain.EXCHANGE:
        reasons = (
            ("SPORTSBOOK_ENTITLEMENT_CANNOT_SCOPE_EXCHANGE",)
            if sportsbook_affiliate_entitled is not None
            else ()
        )
        return BetfairProductScopeDecision(
            BetfairProductScopeState.DENIED
            if reasons
            else BetfairProductScopeState.COMPATIBLE,
            reasons,
            BetfairProductDomain.EXCHANGE,
            "betfair.exchange",
            True,
            False,
            False,
        )

    reasons_list: list[str] = []
    if operation is BetfairProductOperation.PLACE_BET:
        reasons_list.append("SPORTSBOOK_API_READ_ONLY")
    if sportsbook_affiliate_entitled is not True:
        reasons_list.append("SPORTSBOOK_AFFILIATE_ENTITLEMENT_REQUIRED")

    return BetfairProductScopeDecision(
        BetfairProductScopeState.DENIED
        if reasons_list
        else BetfairProductScopeState.COMPATIBLE,
        tuple(reasons_list),
        BetfairProductDomain.SPORTSBOOK,
        "betfair.sportsbook",
        False,
        True,
        True,
    )
