"""Authenticated Betfair inputs needed for prospective execution-fee authority.

This module deliberately does not calculate or issue commission authority. It only
captures provider-native account and market inputs through the already-canonical
:class:`BetfairReadOnlyClient`, preserving the two causal provider payload hashes.
A downstream rule must remain UNKNOWN when tariff/regime applicability is not proven.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from types import MethodType, SimpleNamespace
from typing import Mapping

from .betfair_account_readonly import (
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    _GET_ACCOUNT_DETAILS,
    _LIST_MARKET_CATALOGUE,
)


_CLIENT_AUTHORITY_METHODS = frozenset(
    {
        "_rpc",
        "_next_request_id",
        "_observed_at",
        "_redact_provider_message",
    }
)


@dataclass(frozen=True, slots=True)
class BetfairExecutionFeeInputsObservation:
    """Exact authenticated provider inputs, not a prospective fee decision.

    ``discount_rate_percent`` and ``market_base_rate_percent`` preserve Betfair's
    provider percentage units. They are intentionally not converted to fractions
    here so this evidence layer cannot silently change tariff semantics.
    """

    venue_id: str
    account_id: str
    currency_code: str
    region: str | None
    market_id: str
    discount_rate_percent: Decimal
    market_base_rate_percent: Decimal
    discount_allowed: bool
    regulator: str | None
    account_evidence: BetfairEvidence
    market_evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _required_text(self.venue_id, "venue_id")
        _required_text(self.account_id, "account_id")
        currency = _required_text(self.currency_code, "currency_code")
        if currency != currency.upper() or not currency.isascii():
            raise BetfairReadOnlyError(
                "currency_code must be uppercase ASCII provider currency code"
            )
        _optional_text(self.region, "region")
        _required_text(self.market_id, "market_id")
        _provider_percent(self.discount_rate_percent, "discount_rate_percent")
        _provider_percent(self.market_base_rate_percent, "market_base_rate_percent")
        if not isinstance(self.discount_allowed, bool):
            raise BetfairReadOnlyError("discount_allowed must be bool")
        _optional_text(self.regulator, "regulator")
        if type(self.account_evidence) is not BetfairEvidence:
            raise BetfairReadOnlyError(
                "account_evidence must be exact canonical BetfairEvidence"
            )
        if type(self.market_evidence) is not BetfairEvidence:
            raise BetfairReadOnlyError(
                "market_evidence must be exact canonical BetfairEvidence"
            )


def _read_betfair_execution_fee_inputs(
    snapshot_client,
    rpc,
    next_request_id,
    observed_at,
    redact_provider_message,
    client: BetfairReadOnlyClient,
    *,
    market_id: str,
) -> BetfairExecutionFeeInputsObservation:
    """Read exact account discount and exact market commission inputs.

    Betfair exposes ``discountRate`` from ``getAccountDetails`` rather than
    ``getAccountFunds``. The function composes the existing authenticated,
    read-only client; it creates no second transport stack, credential source,
    provider adapter architecture, or store. Missing, ambiguous, mismatched, or
    malformed data fails closed.

    The public callable binds the canonical snapshot/RPC/helper function objects
    into the read-only ``functools.partial`` capability when this module is first
    constructed. ``partial.func`` and ``partial.args`` are read-only attributes,
    so later pre-call rebinding of writable module mirrors or matching
    ``BetfairReadOnlyClient`` class attributes cannot replace the executable set
    used by this observation. The caller's mutable client and one transport POST
    capability are then snapshotted before provider I/O.
    """

    pinned_client, venue_id, account_id = snapshot_client(
        client,
        next_request_id=next_request_id,
        observed_at=observed_at,
        redact_provider_message=redact_provider_message,
    )
    market = _required_text(market_id, "market_id")

    account_response = rpc(
        pinned_client,
        _GET_ACCOUNT_DETAILS,
        {},
    )
    account = _mapping(account_response.result, "getAccountDetails result")
    currency_code = _provider_text(account, "currencyCode", "currency_code")
    region = _provider_optional_text(account, "region", "region")
    discount_rate = _provider_number(
        account,
        "discountRate",
        "discount_rate_percent",
    )
    _provider_percent(discount_rate, "discount_rate_percent")

    market_response = rpc(
        pinned_client,
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
    regulator = _provider_optional_text(description, "regulator", "regulator")

    return BetfairExecutionFeeInputsObservation(
        venue_id=venue_id,
        account_id=account_id,
        currency_code=currency_code,
        region=region,
        market_id=returned_market,
        discount_rate_percent=discount_rate,
        market_base_rate_percent=market_base_rate,
        discount_allowed=discount_allowed,
        regulator=regulator,
        account_evidence=account_response.evidence,
        market_evidence=market_response.evidence,
    )


def _snapshot_canonical_client(
    client: object,
    *,
    next_request_id,
    observed_at,
    redact_provider_message,
) -> tuple[BetfairReadOnlyClient, str, str]:
    """Capture one private exact-client authority image before provider I/O."""

    if type(client) is not BetfairReadOnlyClient:
        raise TypeError("client must be exact BetfairReadOnlyClient")

    state = vars(client).copy()
    shadowed = sorted(
        name for name in _CLIENT_AUTHORITY_METHODS if name in state
    )
    if shadowed:
        raise BetfairReadOnlyError(
            "BetfairReadOnlyClient read capability is instance-shadowed: "
            + ", ".join(shadowed)
        )

    credentials = state.get("_credentials")
    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairReadOnlyError(
            "BetfairReadOnlyClient credentials are not exact canonical credentials"
        )
    venue_id = _required_text(state.get("_venue_id"), "venue_id")
    account_id = _required_text(state.get("_account_id"), "account_id")
    transport = state.get("_transport")
    timeout_seconds = state.get("_timeout_seconds")
    clock = state.get("_clock")
    if transport is None:
        raise BetfairReadOnlyError("BetfairReadOnlyClient transport is missing")
    transport_post = getattr(transport, "post", None)
    if not callable(transport_post):
        raise BetfairReadOnlyError("BetfairReadOnlyClient transport post capability is invalid")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise BetfairReadOnlyError("BetfairReadOnlyClient timeout is invalid")
    if timeout_seconds <= 0:
        raise BetfairReadOnlyError("BetfairReadOnlyClient timeout is invalid")
    if not callable(clock):
        raise BetfairReadOnlyError("BetfairReadOnlyClient clock is invalid")

    pinned_credentials = BetfairSessionCredentials(
        credentials.application_key,
        credentials.session_token,
    )
    pinned_transport = SimpleNamespace(post=transport_post)
    pinned_client = BetfairReadOnlyClient(
        pinned_credentials,
        transport=pinned_transport,
        timeout_seconds=float(timeout_seconds),
        clock=clock,
        venue_id=venue_id,
        account_id=account_id,
    )
    pinned_client._next_request_id = MethodType(
        next_request_id,
        pinned_client,
    )
    pinned_client._observed_at = MethodType(
        observed_at,
        pinned_client,
    )
    pinned_client._redact_provider_message = MethodType(
        redact_provider_message,
        pinned_client,
    )
    return pinned_client, venue_id, account_id


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BetfairReadOnlyError(f"{field} must be a JSON object")
    return value


def _provider_text(value: Mapping[str, object], key: str, field: str) -> str:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    return _required_text(value[key], field)


def _provider_optional_text(
    value: Mapping[str, object],
    key: str,
    field: str,
) -> str | None:
    raw = value.get(key)
    return None if raw is None else _required_text(raw, field)


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
        raise BetfairReadOnlyError(
            f"{field} must be between 0 and 100 provider percent"
        )
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairReadOnlyError(f"{field} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _required_text(value, field)


# Bind the complete provider-executable set into one non-rebindable callable before
# any caller can reach the public API. Unlike ordinary module aliases, partial's
# ``func`` and ``args`` attributes are read-only, so replacing module mirrors or
# matching class attributes later cannot change this capability. The private client
# receives the same captured helper implementations before provider callbacks run.
read_betfair_execution_fee_inputs = partial(
    _read_betfair_execution_fee_inputs,
    _snapshot_canonical_client,
    BetfairReadOnlyClient.__dict__["_rpc"],
    BetfairReadOnlyClient.__dict__["_next_request_id"],
    BetfairReadOnlyClient.__dict__["_observed_at"],
    BetfairReadOnlyClient.__dict__["_redact_provider_message"],
)
read_betfair_execution_fee_inputs.__name__ = "read_betfair_execution_fee_inputs"
read_betfair_execution_fee_inputs.__qualname__ = "read_betfair_execution_fee_inputs"
read_betfair_execution_fee_inputs.__doc__ = _read_betfair_execution_fee_inputs.__doc__
