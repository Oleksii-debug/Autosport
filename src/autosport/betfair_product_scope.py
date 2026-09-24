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
    REQUIRES_EXTERNAL_ENTITLEMENT = "REQUIRES_EXTERNAL_ENTITLEMENT"
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
    requires_separate_sportsbook_adapter: bool

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
            "requires_separate_sportsbook_adapter",
        ):
            if type(getattr(self, field)) is not bool:
                raise BetfairProductScopeError(f"{field} must be bool")

        if self.state is BetfairProductScopeState.COMPATIBLE:
            if self.reason_codes:
                raise BetfairProductScopeError(
                    "COMPATIBLE decision cannot carry reasons"
                )
        elif self.state is BetfairProductScopeState.REQUIRES_EXTERNAL_ENTITLEMENT:
            if self.reason_codes != (
                "SPORTSBOOK_AFFILIATE_ENTITLEMENT_REQUIRED",
            ):
                raise BetfairProductScopeError(
                    "external-entitlement state requires canonical reason"
                )
        elif not self.reason_codes:
            raise BetfairProductScopeError(
                "DENIED decision requires at least one reason"
            )

    @property
    def scope_compatible(self) -> bool:
        return self.state is BetfairProductScopeState.COMPATIBLE

    @property
    def provider_entitlement_authorized(self) -> bool:
        """This static boundary never proves provider-issued entitlement."""
        return False

    @property
    def execution_authorized(self) -> bool:
        """Product-family routing never authorizes an Autosport provider write."""
        return False


def evaluate_betfair_product_scope(
    *,
    domain: BetfairProductDomain,
    operation: BetfairProductOperation,
) -> BetfairProductScopeDecision:
    """Evaluate product-family routing without minting external entitlement."""

    if not isinstance(domain, BetfairProductDomain):
        raise BetfairProductScopeError(
            "domain must be a BetfairProductDomain value"
        )
    if not isinstance(operation, BetfairProductOperation):
        raise BetfairProductScopeError(
            "operation must be a BetfairProductOperation value"
        )

    if domain is BetfairProductDomain.EXCHANGE:
        return BetfairProductScopeDecision(
            BetfairProductScopeState.COMPATIBLE,
            (),
            BetfairProductDomain.EXCHANGE,
            "betfair.exchange",
            True,
            False,
            False,
            False,
        )

    if operation is BetfairProductOperation.PLACE_BET:
        return BetfairProductScopeDecision(
            BetfairProductScopeState.DENIED,
            ("SPORTSBOOK_API_READ_ONLY",),
            BetfairProductDomain.SPORTSBOOK,
            "betfair.sportsbook",
            False,
            True,
            True,
            True,
        )

    return BetfairProductScopeDecision(
        BetfairProductScopeState.REQUIRES_EXTERNAL_ENTITLEMENT,
        ("SPORTSBOOK_AFFILIATE_ENTITLEMENT_REQUIRED",),
        BetfairProductDomain.SPORTSBOOK,
        "betfair.sportsbook",
        False,
        True,
        True,
        True,
    )
