"""Betfair Exchange/Sportsbook production-scope guard.

This module freezes provider-documented product-domain and Application-Key
semantics without granting provider access or Autosport execution authority.

Important boundaries:
- Exchange and Sportsbook are distinct provider/provenance domains.
- the Sportsbook API is read-only and affiliate-only; it cannot be an execution
  fallback for the Exchange;
- a Delayed Exchange Application Key operates against the live production
  Exchange and is technically bet-placement capable, so it is never a sandbox
  or dry-run signal;
- monitor-only Exchange use belongs on a Delayed key; read-only use of a Live
  key is not a supported Betfair usage mode;
- key-tier compatibility never enables Autosport provider writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BetfairProductScopeError(ValueError):
    """Raised when a scope projection is structurally invalid."""


class BetfairProductDomain(str, Enum):
    EXCHANGE = "EXCHANGE"
    SPORTSBOOK = "SPORTSBOOK"


class BetfairExchangeAppKeyTier(str, Enum):
    DELAYED = "DELAYED"
    LIVE = "LIVE"


class BetfairUsageIntent(str, Enum):
    MONITOR_ONLY = "MONITOR_ONLY"
    TRANSACTIONAL = "TRANSACTIONAL"


class BetfairOperation(str, Enum):
    MARKET_READ = "MARKET_READ"
    PLACE_BET = "PLACE_BET"


class BetfairScopeState(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    DENIED = "DENIED"


@dataclass(frozen=True, slots=True)
class BetfairProductScopeDecision:
    state: BetfairScopeState
    reason_codes: tuple[str, ...]
    domain: BetfairProductDomain
    provenance_domain: str
    production_environment: bool
    delayed_market_data: bool | None
    provider_exchange_bet_placement_available: bool
    sportsbook_read_only: bool
    requires_external_affiliate_entitlement: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state, BetfairScopeState):
            raise BetfairProductScopeError("state must be BetfairScopeState")
        if not isinstance(self.domain, BetfairProductDomain):
            raise BetfairProductScopeError("domain must be BetfairProductDomain")
        if self.provenance_domain not in {
            "betfair.exchange",
            "betfair.sportsbook",
        }:
            raise BetfairProductScopeError("invalid Betfair provenance domain")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason
            for reason in self.reason_codes
        ):
            raise BetfairProductScopeError(
                "reason_codes must be an immutable tuple of non-empty strings"
            )
        for field in (
            "production_environment",
            "provider_exchange_bet_placement_available",
            "sportsbook_read_only",
            "requires_external_affiliate_entitlement",
        ):
            if type(getattr(self, field)) is not bool:
                raise BetfairProductScopeError(f"{field} must be bool")
        if self.delayed_market_data is not None and type(
            self.delayed_market_data
        ) is not bool:
            raise BetfairProductScopeError(
                "delayed_market_data must be bool or None"
            )
        if self.state is BetfairScopeState.COMPATIBLE and self.reason_codes:
            raise BetfairProductScopeError(
                "COMPATIBLE decision cannot carry denial reasons"
            )
        if self.state is BetfairScopeState.DENIED and not self.reason_codes:
            raise BetfairProductScopeError(
                "DENIED decision requires at least one reason"
            )

    @property
    def scope_compatible(self) -> bool:
        return self.state is BetfairScopeState.COMPATIBLE

    @property
    def execution_authorized(self) -> bool:
        """Provider scope compatibility never authorizes an Autosport write."""
        return False

    @property
    def provider_entitlement_authorized(self) -> bool:
        """A caller projection never proves an external provider entitlement."""
        return False


def evaluate_betfair_product_scope(
    *,
    domain: BetfairProductDomain,
    operation: BetfairOperation,
    usage_intent: BetfairUsageIntent,
    exchange_app_key_tier: BetfairExchangeAppKeyTier | None = None,
    sportsbook_affiliate_entitled: bool | None = None,
) -> BetfairProductScopeDecision:
    """Evaluate provider product/key compatibility without granting authority.

    sportsbook_affiliate_entitled is a configuration projection only. A
    positive value can make the static scope compatible, but cannot prove a
    provider-issued affiliate entitlement; callers still need separate
    authoritative entitlement evidence and a separate Sportsbook adapter.
    """

    if not isinstance(domain, BetfairProductDomain):
        raise BetfairProductScopeError(
            "domain must be a BetfairProductDomain value"
        )
    if not isinstance(operation, BetfairOperation):
        raise BetfairProductScopeError(
            "operation must be a BetfairOperation value"
        )
    if not isinstance(usage_intent, BetfairUsageIntent):
        raise BetfairProductScopeError(
            "usage_intent must be a BetfairUsageIntent value"
        )
    if (
        exchange_app_key_tier is not None
        and not isinstance(exchange_app_key_tier, BetfairExchangeAppKeyTier)
    ):
        raise BetfairProductScopeError(
            "exchange_app_key_tier must be BetfairExchangeAppKeyTier or None"
        )
    if (
        sportsbook_affiliate_entitled is not None
        and type(sportsbook_affiliate_entitled) is not bool
    ):
        raise BetfairProductScopeError(
            "sportsbook_affiliate_entitled must be bool or None"
        )

    if domain is BetfairProductDomain.SPORTSBOOK:
        return _evaluate_sportsbook_scope(
            operation=operation,
            usage_intent=usage_intent,
            exchange_app_key_tier=exchange_app_key_tier,
            sportsbook_affiliate_entitled=sportsbook_affiliate_entitled,
        )

    return _evaluate_exchange_scope(
        operation=operation,
        usage_intent=usage_intent,
        exchange_app_key_tier=exchange_app_key_tier,
        sportsbook_affiliate_entitled=sportsbook_affiliate_entitled,
    )


def _evaluate_exchange_scope(
    *,
    operation: BetfairOperation,
    usage_intent: BetfairUsageIntent,
    exchange_app_key_tier: BetfairExchangeAppKeyTier | None,
    sportsbook_affiliate_entitled: bool | None,
) -> BetfairProductScopeDecision:
    reasons: list[str] = []

    if exchange_app_key_tier is None:
        reasons.append("EXCHANGE_APP_KEY_TIER_REQUIRED")
    if sportsbook_affiliate_entitled is not None:
        reasons.append("SPORTSBOOK_ENTITLEMENT_CANNOT_SCOPE_EXCHANGE")

    if (
        operation is BetfairOperation.PLACE_BET
        and usage_intent is not BetfairUsageIntent.TRANSACTIONAL
    ):
        reasons.append("PLACE_BET_REQUIRES_TRANSACTIONAL_INTENT")

    if (
        exchange_app_key_tier is BetfairExchangeAppKeyTier.LIVE
        and usage_intent is BetfairUsageIntent.MONITOR_ONLY
    ):
        reasons.append("LIVE_KEY_MONITOR_ONLY_NOT_PERMITTED")

    delayed = (
        None
        if exchange_app_key_tier is None
        else exchange_app_key_tier is BetfairExchangeAppKeyTier.DELAYED
    )

    return BetfairProductScopeDecision(
        BetfairScopeState.DENIED if reasons else BetfairScopeState.COMPATIBLE,
        tuple(reasons),
        BetfairProductDomain.EXCHANGE,
        "betfair.exchange",
        True,
        delayed,
        exchange_app_key_tier
        in {
            BetfairExchangeAppKeyTier.DELAYED,
            BetfairExchangeAppKeyTier.LIVE,
        },
        False,
        False,
    )


def _evaluate_sportsbook_scope(
    *,
    operation: BetfairOperation,
    usage_intent: BetfairUsageIntent,
    exchange_app_key_tier: BetfairExchangeAppKeyTier | None,
    sportsbook_affiliate_entitled: bool | None,
) -> BetfairProductScopeDecision:
    reasons: list[str] = []

    if exchange_app_key_tier is not None:
        reasons.append("EXCHANGE_APP_KEY_CANNOT_SCOPE_SPORTSBOOK")
    if operation is BetfairOperation.PLACE_BET:
        reasons.append("SPORTSBOOK_API_READ_ONLY")
    if usage_intent is BetfairUsageIntent.TRANSACTIONAL:
        reasons.append("SPORTSBOOK_API_HAS_NO_TRANSACTIONAL_SCOPE")
    if sportsbook_affiliate_entitled is not True:
        reasons.append("SPORTSBOOK_AFFILIATE_ENTITLEMENT_REQUIRED")

    return BetfairProductScopeDecision(
        BetfairScopeState.DENIED if reasons else BetfairScopeState.COMPATIBLE,
        tuple(reasons),
        BetfairProductDomain.SPORTSBOOK,
        "betfair.sportsbook",
        True,
        None,
        False,
        True,
        True,
    )
