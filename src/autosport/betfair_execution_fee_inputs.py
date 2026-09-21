"""Authenticated Betfair inputs needed for prospective execution-fee authority.

This module deliberately does not calculate or issue commission authority.  It only
captures provider-native account and market inputs through the already-canonical
:class:`BetfairReadOnlyClient`, preserving the two causal provider payload hashes.
A downstream rule may remain UNKNOWN when tariff/regime applicability is not proven.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .betfair_account_readonly import (
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    _GET_ACCOUNT_FUNDS,
    _LIST_MARKET_CATALOGUE,
)


@dataclass(frozen=True, slots=True)
class BetfairExecutionFeeInputsObservation:
    """Exact authenticated provider inputs, not a prospective fee decision.

    ``discount_rate_percent`` and ``market_base_rate_percent`` preserve Betfair's
    provider percentage units.  They are intentionally not converted to fractions
    here so this evidence layer cannot silently change tariff semantics.
    """

    venue_id: str
    account_id: str
    market_id: str
    discount_rate_percent: Decimal
    market_base_rate_percent: Decimal
    discount_allowed: bool
    account_evidence: BetfairEvidence
    market_evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _required_text(self.venue_id, "venue_id")
        _required_text(self.account_id, "account_id")
        _required_text(self.market_id, "market_id")
        _provider_percent(self.discount_rate_percent, "discount_rate_percent")
        _provider_percent(self.market_base_rate_percent, "market_base_rate_percent")
        if not isinstance(self.discount_allowed, bool):
            raise BetfairReadOnlyError("discount_allowed must be bool")
        if not isinstance(self.account_evidence, BetfairEvidence):
            raise BetfairReadOnlyError("account_evidence must be canonical BetfairEvidence")
        if not isinstance(self.market_evidence, BetfairEvidence):
            raise BetfairReadOnlyError("market_evidence must be canonical BetfairEvidence")


def read_betfair_execution_fee_inputs(
    client: BetfairReadOnlyClient,
    *,
    market_id: str,
) -> BetfairExecutionFeeInputsObservation:
    """Read exact account discount and exact market commission inputs.

    The function composes the existing authenticated, read-only Betfair client; it
    does not create a second transport, credential path, provider client, or store.
    Missing, ambiguous, mismatched, or malformed provider data fails closed.
    """

    if not isinstance(client, BetfairReadOnlyClient):
        raise TypeError("client must be BetfairReadOnlyClient")
    market = _required_text(market_id, "market_id")

    account_response = client._rpc(_GET_ACCOUNT_FUNDS, {})
    account = _mapping(account_response.result, "getAccountFunds result")
    discount_rate = _provider_number(account, "discountRate", "discount_rate_percent")
    _provider_percent(discount_rate, "discount_rate_percent")

    market_response = client._rpc(
        _LIST_MARKET_CATALOGUE,
        {
            "filter": {"marketIds": [market]},
            "marketProjection": ["MARKET_DESCRIPTION"],
            "maxResults": 1,
        },
    )
    rows = market_response.result
    if not isinstance(rows, list):
        raise BetfairReadOnlyError("listMarketCatalogue result must be a JSON array")
    if len(rows) != 1:
        raise BetfairReadOnlyError(
            "exact market commission inputs are unavailable from listMarketCatalogue"
        )
    row = _mapping(rows[0], "marketCatalogue[0]")
    returned_market = _provider_text(row, "marketId", "market_id")
    if returned_market != market:
        raise BetfairReadOnlyError("marketCatalogue returned a different market")
    description = _mapping(row.get("description"), "marketCatalogue[0].description")
    market_base_rate = _provider_number(
        description,
        "marketBaseRate",
        "market_base_rate_percent",
    )
    _provider_percent(market_base_rate, "market_base_rate_percent")
    discount_allowed = description.get("discountAllowed")
    if not isinstance(discount_allowed, bool):
        raise BetfairReadOnlyError("discountAllowed must be bool")

    venue_id = _required_text(getattr(client, "_venue_id", None), "venue_id")
    account_id = _required_text(getattr(client, "_account_id", None), "account_id")
    return BetfairExecutionFeeInputsObservation(
        venue_id=venue_id,
        account_id=account_id,
        market_id=returned_market,
        discount_rate_percent=discount_rate,
        market_base_rate_percent=market_base_rate,
        discount_allowed=discount_allowed,
        account_evidence=account_response.evidence,
        market_evidence=market_response.evidence,
    )


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BetfairReadOnlyError(f"{field} must be a JSON object")
    return value


def _provider_text(value: Mapping[str, object], key: str, field: str) -> str:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    return _required_text(value[key], field)


def _provider_number(
    value: Mapping[str, object],
    key: str,
    field: str,
) -> Decimal:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    raw = value[key]
    if isinstance(raw, Decimal):
        result = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        result = Decimal(raw)
    else:
        raise BetfairReadOnlyError(
            f"{field} must be a JSON number decoded without binary float"
        )
    if not result.is_finite():
        raise BetfairReadOnlyError(f"{field} must be finite")
    return result


def _provider_percent(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetfairReadOnlyError(f"{field} must be a finite Decimal")
    if value < 0 or value > 100:
        raise BetfairReadOnlyError(f"{field} must be between 0 and 100 provider percent")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairReadOnlyError(f"{field} must be a non-empty trimmed string")
    return value
