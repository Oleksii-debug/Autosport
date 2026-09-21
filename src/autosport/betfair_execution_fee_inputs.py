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
    error_type,
    mapping,
    provider_text,
    provider_optional_text,
    provider_number,
    provider_percent,
    required_text,
    build_observation,
    get_account_details_method,
    list_market_catalogue_method,
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

    The exported callable binds this complete evidence-producing graph into one
    read-only ``functools.partial`` capability when the module is first loaded.
    Writable module mirrors are therefore diagnostics/compatibility only: parser,
    DTO, constant, client-class, and snapshot rebinding before or during provider
    callbacks cannot replace the executable graph used by this observation.
    """

    pinned_client, venue_id, account_id = snapshot_client(
        client,
        next_request_id=next_request_id,
        observed_at=observed_at,
        redact_provider_message=redact_provider_message,
    )
    market = required_text(market_id, "market_id")

    account_response = rpc(
        pinned_client,
        get_account_details_method,
        {},
    )
    account = mapping(account_response.result, "getAccountDetails result")
    currency_code = provider_text(account, "currencyCode", "currency_code")
    region = provider_optional_text(account, "region", "region")
    discount_rate = provider_number(
        account,
        "discountRate",
        "discount_rate_percent",
    )
    provider_percent(discount_rate, "discount_rate_percent")

    market_response = rpc(
        pinned_client,
        list_market_catalogue_method,
        {
            "filter": {"marketIds": [market]},
            "marketProjection": ["MARKET_DESCRIPTION"],
            "maxResults": 1,
        },
    )
    rows = market_response.result
    if not isinstance(rows, list):
        raise error_type("listMarketCatalogue result must be a JSON array")
    if len(rows) != 1:
        raise error_type(
            "exact market commission inputs are unavailable from listMarketCatalogue"
        )
    row = mapping(rows[0], "marketCatalogue[0]")
    returned_market = provider_text(row, "marketId", "market_id")
    if returned_market != market:
        raise error_type("marketCatalogue returned a different market")
    description = mapping(row.get("description"), "marketCatalogue[0].description")
    market_base_rate = provider_number(
        description,
        "marketBaseRate",
        "market_base_rate_percent",
    )
    provider_percent(market_base_rate, "market_base_rate_percent")
    discount_allowed = description.get("discountAllowed")
    if not isinstance(discount_allowed, bool):
        raise error_type("discountAllowed must be bool")
    regulator = provider_optional_text(description, "regulator", "regulator")

    return build_observation(
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
    error_type,
    client_type,
    credentials_type,
    simple_namespace_type,
    method_type,
    authority_methods,
    required_text,
    client: object,
    *,
    next_request_id,
    observed_at,
    redact_provider_message,
):
    """Capture one private exact-client authority image before provider I/O."""

    if type(client) is not client_type:
        raise TypeError("client must be exact BetfairReadOnlyClient")

    state = vars(client).copy()
    shadowed = sorted(name for name in authority_methods if name in state)
    if shadowed:
        raise error_type(
            "BetfairReadOnlyClient read capability is instance-shadowed: "
            + ", ".join(shadowed)
        )

    credentials = state.get("_credentials")
    if type(credentials) is not credentials_type:
        raise error_type(
            "BetfairReadOnlyClient credentials are not exact canonical credentials"
        )
    venue_id = required_text(state.get("_venue_id"), "venue_id")
    account_id = required_text(state.get("_account_id"), "account_id")
    transport = state.get("_transport")
    timeout_seconds = state.get("_timeout_seconds")
    clock = state.get("_clock")
    if transport is None:
        raise error_type("BetfairReadOnlyClient transport is missing")
    transport_post = getattr(transport, "post", None)
    if not callable(transport_post):
        raise error_type("BetfairReadOnlyClient transport post capability is invalid")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise error_type("BetfairReadOnlyClient timeout is invalid")
    if timeout_seconds <= 0:
        raise error_type("BetfairReadOnlyClient timeout is invalid")
    if not callable(clock):
        raise error_type("BetfairReadOnlyClient clock is invalid")

    pinned_credentials = credentials_type(
        credentials.application_key,
        credentials.session_token,
    )
    pinned_transport = simple_namespace_type(post=transport_post)
    pinned_client = client_type(
        pinned_credentials,
        transport=pinned_transport,
        timeout_seconds=float(timeout_seconds),
        clock=clock,
        venue_id=venue_id,
        account_id=account_id,
    )
    pinned_client._next_request_id = method_type(
        next_request_id,
        pinned_client,
    )
    pinned_client._observed_at = method_type(
        observed_at,
        pinned_client,
    )
    pinned_client._redact_provider_message = method_type(
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


# The sealed helpers below intentionally receive every non-builtin dependency as a
# positional argument. The exported reader captures the resulting partials once;
# no authority-bearing parser/snapshot/DTO decision subsequently re-enters this
# module dictionary.
def _sealed_required_text(error_type, value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise error_type(f"{field} must be a non-empty trimmed string")
    return value


def _sealed_mapping(error_type, mapping_type, value: object, field: str):
    if not isinstance(value, mapping_type) or any(
        not isinstance(key, str) for key in value
    ):
        raise error_type(f"{field} must be a JSON object")
    return value


def _sealed_provider_text(error_type, required_text, value, key: str, field: str) -> str:
    if key not in value:
        raise error_type(f"{field} is missing from provider response")
    return required_text(value[key], field)


def _sealed_provider_optional_text(required_text, value, key: str, field: str):
    raw = value.get(key)
    return None if raw is None else required_text(raw, field)


def _sealed_provider_number(error_type, decimal_type, value, key: str, field: str):
    if key not in value:
        raise error_type(f"{field} is missing from provider response")
    raw = value[key]
    if isinstance(raw, decimal_type):
        result = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        result = decimal_type(raw)
    else:
        raise error_type(
            f"{field} must be a JSON number decoded without binary float"
        )
    if not result.is_finite():
        raise error_type(f"{field} must be finite")
    return result


def _sealed_provider_percent(error_type, decimal_type, value, field: str):
    if not isinstance(value, decimal_type) or not value.is_finite():
        raise error_type(f"{field} must be a finite Decimal")
    if value < 0 or value > 100:
        raise error_type(f"{field} must be between 0 and 100 provider percent")
    return value


def _build_observation_without_module_dispatch(
    error_type,
    evidence_type,
    observation_type,
    decimal_type,
    required_text,
    provider_percent,
    *,
    venue_id,
    account_id,
    currency_code,
    region,
    market_id,
    discount_rate_percent,
    market_base_rate_percent,
    discount_allowed,
    regulator,
    account_evidence,
    market_evidence,
):
    venue = required_text(venue_id, "venue_id")
    account = required_text(account_id, "account_id")
    currency = required_text(currency_code, "currency_code")
    if currency != currency.upper() or not currency.isascii():
        raise error_type("currency_code must be uppercase ASCII provider currency code")
    if region is not None:
        required_text(region, "region")
    market = required_text(market_id, "market_id")
    discount = provider_percent(discount_rate_percent, "discount_rate_percent")
    base_rate = provider_percent(
        market_base_rate_percent,
        "market_base_rate_percent",
    )
    if not isinstance(discount_allowed, bool):
        raise error_type("discount_allowed must be bool")
    if regulator is not None:
        required_text(regulator, "regulator")
    if type(account_evidence) is not evidence_type:
        raise error_type("account_evidence must be exact canonical BetfairEvidence")
    if type(market_evidence) is not evidence_type:
        raise error_type("market_evidence must be exact canonical BetfairEvidence")
    if not isinstance(discount, decimal_type) or not isinstance(base_rate, decimal_type):
        raise error_type("provider percentage validation returned a non-Decimal value")

    # Bypass the public dataclass constructor deliberately: __post_init__ retains
    # compatibility helpers for direct callers, while this authenticated path has
    # already validated every field through the sealed graph above.
    observation = object.__new__(observation_type)
    object.__setattr__(observation, "venue_id", venue)
    object.__setattr__(observation, "account_id", account)
    object.__setattr__(observation, "currency_code", currency)
    object.__setattr__(observation, "region", region)
    object.__setattr__(observation, "market_id", market)
    object.__setattr__(observation, "discount_rate_percent", discount)
    object.__setattr__(observation, "market_base_rate_percent", base_rate)
    object.__setattr__(observation, "discount_allowed", discount_allowed)
    object.__setattr__(observation, "regulator", regulator)
    object.__setattr__(observation, "account_evidence", account_evidence)
    object.__setattr__(observation, "market_evidence", market_evidence)
    return observation


_REQUIRED_TEXT_CAPABILITY = partial(_sealed_required_text, BetfairReadOnlyError)
_MAPPING_CAPABILITY = partial(_sealed_mapping, BetfairReadOnlyError, Mapping)
_PROVIDER_TEXT_CAPABILITY = partial(
    _sealed_provider_text,
    BetfairReadOnlyError,
    _REQUIRED_TEXT_CAPABILITY,
)
_PROVIDER_OPTIONAL_TEXT_CAPABILITY = partial(
    _sealed_provider_optional_text,
    _REQUIRED_TEXT_CAPABILITY,
)
_PROVIDER_NUMBER_CAPABILITY = partial(
    _sealed_provider_number,
    BetfairReadOnlyError,
    Decimal,
)
_PROVIDER_PERCENT_CAPABILITY = partial(
    _sealed_provider_percent,
    BetfairReadOnlyError,
    Decimal,
)
_SNAPSHOT_CLIENT_CAPABILITY = partial(
    _snapshot_canonical_client,
    BetfairReadOnlyError,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
    SimpleNamespace,
    MethodType,
    frozenset(_CLIENT_AUTHORITY_METHODS),
    _REQUIRED_TEXT_CAPABILITY,
)
_BUILD_OBSERVATION_CAPABILITY = partial(
    _build_observation_without_module_dispatch,
    BetfairReadOnlyError,
    BetfairEvidence,
    BetfairExecutionFeeInputsObservation,
    Decimal,
    _REQUIRED_TEXT_CAPABILITY,
    _PROVIDER_PERCENT_CAPABILITY,
)


# Bind the complete evidence-producing executable graph into one non-rebindable
# callable before any caller can reach the public API. ``partial.func`` and
# ``partial.args`` are read-only, so later module/class/parser/constant rebinding
# cannot change this capture authority.
read_betfair_execution_fee_inputs = partial(
    _read_betfair_execution_fee_inputs,
    _SNAPSHOT_CLIENT_CAPABILITY,
    BetfairReadOnlyClient.__dict__["_rpc"],
    BetfairReadOnlyClient.__dict__["_next_request_id"],
    BetfairReadOnlyClient.__dict__["_observed_at"],
    BetfairReadOnlyClient.__dict__["_redact_provider_message"],
    BetfairReadOnlyError,
    _MAPPING_CAPABILITY,
    _PROVIDER_TEXT_CAPABILITY,
    _PROVIDER_OPTIONAL_TEXT_CAPABILITY,
    _PROVIDER_NUMBER_CAPABILITY,
    _PROVIDER_PERCENT_CAPABILITY,
    _REQUIRED_TEXT_CAPABILITY,
    _BUILD_OBSERVATION_CAPABILITY,
    _GET_ACCOUNT_DETAILS,
    _LIST_MARKET_CATALOGUE,
)
read_betfair_execution_fee_inputs.__name__ = "read_betfair_execution_fee_inputs"
read_betfair_execution_fee_inputs.__qualname__ = "read_betfair_execution_fee_inputs"
read_betfair_execution_fee_inputs.__doc__ = _read_betfair_execution_fee_inputs.__doc__
