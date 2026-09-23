"""Explicit, non-authorizing Betfair Betting/Accounts API routing contract.

Betfair login origin and Betting/Accounts API origin are separate provider facts.
In particular, Spain and Italy use jurisdiction-specific login hosts while documented
Betting/Accounts JSON-RPC calls remain on the global ``api.betfair.com`` host.

The AUSTRALIA_NEW_ZEALAND login origin intentionally does not prove that a customer
is New Zealand based.  Therefore this module never selects the New Zealand
``api.betfair.com.au`` API route from that login origin alone.  The documented New
Zealand route is exposed as an inert contract for later composition with a
product-owned country authority.

Objects in this module are descriptive contracts only.  They are freely
constructible/copyable and never authorize provider I/O, writes, execution, funds,
settlement, or real-money activity.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from .betfair_session_origin import BetfairLoginJurisdiction


ROUTING_SCHEMA = "autosport.betfair_api_routing_contract"
ROUTING_SCHEMA_VERSION = 1

GLOBAL_BETTING_JSON_RPC_ENDPOINT = (
    "https://api.betfair.com/exchange/betting/json-rpc/v1"
)
GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT = (
    "https://api.betfair.com/exchange/account/json-rpc/v1"
)
NEW_ZEALAND_BETTING_JSON_RPC_ENDPOINT = (
    "https://api.betfair.com.au/exchange/betting/json-rpc/v1"
)
NEW_ZEALAND_ACCOUNTS_JSON_RPC_ENDPOINT = (
    "https://api.betfair.com.au/exchange/account/json-rpc/v1"
)


class BetfairApiRoutingError(RuntimeError):
    """Raised when a routing contract is malformed or used inconsistently."""


class BetfairApiRouteClass(str, Enum):
    GLOBAL = "GLOBAL"
    NEW_ZEALAND = "NEW_ZEALAND"


class BetfairApiService(str, Enum):
    BETTING = "BETTING"
    ACCOUNTS = "ACCOUNTS"


class BetfairApiRouteResolutionStatus(str, Enum):
    RESOLVED_CONTRACT = "RESOLVED_CONTRACT"
    COUNTRY_AUTHORITY_REQUIRED = "COUNTRY_AUTHORITY_REQUIRED"


_ROUTE_ENDPOINTS: Mapping[
    BetfairApiRouteClass, tuple[str, str]
] = MappingProxyType(
    {
        BetfairApiRouteClass.GLOBAL: (
            GLOBAL_BETTING_JSON_RPC_ENDPOINT,
            GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT,
        ),
        BetfairApiRouteClass.NEW_ZEALAND: (
            NEW_ZEALAND_BETTING_JSON_RPC_ENDPOINT,
            NEW_ZEALAND_ACCOUNTS_JSON_RPC_ENDPOINT,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class BetfairApiRouteContract:
    """Versioned endpoint-pair contract.

    This value is deliberately not an authority object.  A caller can copy or
    reconstruct it, but doing so never grants permission to send provider I/O.
    """

    route_class: BetfairApiRouteClass
    betting_json_rpc_endpoint: str
    accounts_json_rpc_endpoint: str

    def __post_init__(self) -> None:
        if type(self.route_class) is not BetfairApiRouteClass:
            raise BetfairApiRoutingError(
                "route_class must be exact BetfairApiRouteClass"
            )
        expected = _ROUTE_ENDPOINTS[self.route_class]
        if (
            self.betting_json_rpc_endpoint,
            self.accounts_json_rpc_endpoint,
        ) != expected:
            raise BetfairApiRoutingError(
                "Betfair API endpoint pair does not match explicit route table"
            )

    @property
    def route_id(self) -> str:
        payload = {
            "schema": ROUTING_SCHEMA,
            "schema_version": ROUTING_SCHEMA_VERSION,
            "route_class": self.route_class.value,
            "betting_json_rpc_endpoint": self.betting_json_rpc_endpoint,
            "accounts_json_rpc_endpoint": self.accounts_json_rpc_endpoint,
        }
        return sha256(_canonical_json(payload)).hexdigest()

    @property
    def authoritative(self) -> bool:
        return False

    @property
    def provider_io_authorized(self) -> bool:
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    def endpoint_for(self, service: BetfairApiService) -> str:
        if type(service) is not BetfairApiService:
            raise BetfairApiRoutingError(
                "service must be exact BetfairApiService"
            )
        if service is BetfairApiService.BETTING:
            return self.betting_json_rpc_endpoint
        return self.accounts_json_rpc_endpoint


@dataclass(frozen=True, slots=True)
class BetfairApiRouteResolution:
    """Descriptive result from login-jurisdiction routing analysis."""

    login_jurisdiction: BetfairLoginJurisdiction
    status: BetfairApiRouteResolutionStatus
    contract: BetfairApiRouteContract | None

    def __post_init__(self) -> None:
        if type(self.login_jurisdiction) is not BetfairLoginJurisdiction:
            raise BetfairApiRoutingError(
                "login_jurisdiction must be exact BetfairLoginJurisdiction"
            )
        if type(self.status) is not BetfairApiRouteResolutionStatus:
            raise BetfairApiRoutingError(
                "status must be exact BetfairApiRouteResolutionStatus"
            )
        if self.status is BetfairApiRouteResolutionStatus.RESOLVED_CONTRACT:
            if (
                type(self.contract) is not BetfairApiRouteContract
                or self.contract.route_class is not BetfairApiRouteClass.GLOBAL
                or self.login_jurisdiction
                is BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND
            ):
                raise BetfairApiRoutingError(
                    "resolved login jurisdiction has inconsistent API contract"
                )
        elif (
            self.login_jurisdiction
            is not BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND
            or self.contract is not None
        ):
            raise BetfairApiRoutingError(
                "country-authority-required status is valid only for AU/NZ login origin"
            )

    @property
    def authoritative(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False


def documented_api_contract(
    route_class: BetfairApiRouteClass,
) -> BetfairApiRouteContract:
    """Return the exact versioned provider endpoint pair for one route class.

    The returned object is reference data, not provider-I/O authority.
    """
    if type(route_class) is not BetfairApiRouteClass:
        raise BetfairApiRoutingError(
            "route_class must be exact BetfairApiRouteClass"
        )
    betting, accounts = _ROUTE_ENDPOINTS[route_class]
    return BetfairApiRouteContract(
        route_class=route_class,
        betting_json_rpc_endpoint=betting,
        accounts_json_rpc_endpoint=accounts,
    )


def routing_contract_for_login_jurisdiction(
    login_jurisdiction: BetfairLoginJurisdiction,
) -> BetfairApiRouteResolution:
    """Resolve only what the authenticated login jurisdiction itself proves.

    GLOBAL_COM, ITALY, SPAIN, and ROMANIA all use the documented global
    Betting/Accounts API contract.  AUSTRALIA_NEW_ZEALAND remains unresolved:
    that login host is insufficient to prove New Zealand country authority.
    """
    if type(login_jurisdiction) is not BetfairLoginJurisdiction:
        raise BetfairApiRoutingError(
            "login_jurisdiction must be exact BetfairLoginJurisdiction"
        )

    if login_jurisdiction is BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND:
        return BetfairApiRouteResolution(
            login_jurisdiction=login_jurisdiction,
            status=BetfairApiRouteResolutionStatus.COUNTRY_AUTHORITY_REQUIRED,
            contract=None,
        )

    return BetfairApiRouteResolution(
        login_jurisdiction=login_jurisdiction,
        status=BetfairApiRouteResolutionStatus.RESOLVED_CONTRACT,
        contract=documented_api_contract(BetfairApiRouteClass.GLOBAL),
    )


def require_endpoint_matches_contract(
    contract: BetfairApiRouteContract,
    service: BetfairApiService,
    endpoint: str,
) -> str:
    """Validate endpoint identity against a descriptive contract.

    This is a consistency check only.  It deliberately does not turn the contract
    into provider-I/O or execution authority.
    """
    if type(contract) is not BetfairApiRouteContract:
        raise BetfairApiRoutingError(
            "contract must be exact BetfairApiRouteContract"
        )
    if type(endpoint) is not str or endpoint != endpoint.strip() or not endpoint:
        raise BetfairApiRoutingError("endpoint must be canonical non-empty text")
    expected = contract.endpoint_for(service)
    if endpoint != expected:
        raise BetfairApiRoutingError(
            "endpoint does not match explicit Betfair API routing contract"
        )
    return endpoint


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairApiRoutingError(
            "routing contract is not canonical JSON"
        ) from exc
