"""Fail-closed Betfair Historical Data acquisition provenance.

Consumes the canonical K07 authenticated session context and records the exact
GetMyData purchase snapshot, DownloadListOfFiles result, and DownloadFile bytes.
The captured DTO values preserve purchase/list/file structure and exact byte identities
without self-attesting provider origin. A separate process-local capability can prove
that one exact file traversed the still-live canonical authenticated transport chain;
current usage rights remain separately unverified. PERSONAL_NONCOMMERCIAL is a dated
reference classification only;
rights must be revalidated by a separate authority.  This module does not prove stable
cross-session account identity, commercial/redistribution rights, live freshness,
execution, settlement, or real-money truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
import urllib.request as _urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener
from weakref import ref

from .betfair_account_identity import (
    BetfairAuthenticatedAccountIdentity,
    require_authoritative_betfair_account_identity,
)
from .betfair_account_readonly import BetfairReadOnlyClient

HISTORICAL_API_BASE = "https://historicdata.betfair.com/api"
TERMS_REFERENCE = "https://www.betfair.com/aboutUs/Terms.and.Conditions/"
TERMS_AS_OF = "2026-09-22"
_SCHEMA_VERSION = 1
_SCOPE = "PERSONAL_NONCOMMERCIAL"


class BetfairHistoricalEntitlementError(RuntimeError):
    pass


class BetfairHistoricalBackpressure(BetfairHistoricalEntitlementError):
    """HTTP 429 is retryable backpressure, never proof of no entitlement."""


@dataclass(frozen=True, slots=True)
class PurchasedHistoricalPackage:
    sport: str
    plan: str
    for_date: str
    purchase_item_id: int

    def __post_init__(self) -> None:
        _text(self.sport, "sport")
        _text(self.plan, "plan")
        _provider_month(self.for_date)
        _uint(self.purchase_item_id, "purchaseItemId")

    @property
    def month(self) -> tuple[int, int]:
        value = _provider_month(self.for_date)
        return value.year, value.month


@dataclass(frozen=True, slots=True)
class HistoricalEntitlementSnapshot:
    session_context_id: str
    observed_at: str
    response_sha256: str
    packages: tuple[PurchasedHistoricalPackage, ...]
    usage_scope: str = _SCOPE
    redistributable: bool = False
    terms_reference: str = TERMS_REFERENCE
    terms_as_of: str = TERMS_AS_OF

    def __post_init__(self) -> None:
        _context_id(self.session_context_id)
        _timestamp(self.observed_at, "observed_at")
        _sha(self.response_sha256, "response_sha256")
        if not isinstance(self.packages, tuple) or any(
            type(value) is not PurchasedHistoricalPackage for value in self.packages
        ):
            raise BetfairHistoricalEntitlementError("packages are not canonical")
        ids = [value.purchase_item_id for value in self.packages]
        if len(ids) != len(set(ids)):
            raise BetfairHistoricalEntitlementError(
                "GetMyData purchaseItemId must be unique within one snapshot"
            )
        if self.usage_scope != _SCOPE:
            raise BetfairHistoricalEntitlementError(
                "this authority cannot mint commercial-use approval"
            )
        if self.redistributable is not False:
            raise BetfairHistoricalEntitlementError(
                "this authority cannot mint redistribution permission"
            )
        if self.terms_reference != TERMS_REFERENCE or self.terms_as_of != TERMS_AS_OF:
            raise BetfairHistoricalEntitlementError("terms provenance is product-owned")

    @property
    def provider_acquisition_verified(self) -> bool:
        """Current transport evidence is structural, not executable-origin proof."""
        return False

    @property
    def usage_rights_verified(self) -> bool:
        """Dated Terms metadata is not a current lawful-use decision."""
        return False

    @property
    def rights_revalidation_required(self) -> bool:
        return True

    @property
    def snapshot_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_historical_entitlement_snapshot",
                "schema_version": _SCHEMA_VERSION,
                "session_context_id": self.session_context_id,
                "observed_at": self.observed_at,
                "response_sha256": self.response_sha256,
                "packages": [
                    [p.sport, p.plan, p.for_date, p.purchase_item_id] for p in self.packages
                ],
                "usage_scope": _SCOPE,
                "redistributable": False,
                "terms_reference": TERMS_REFERENCE,
                "terms_as_of": TERMS_AS_OF,
                "provider_acquisition_verified": False,
                "usage_rights_verified": False,
                "rights_revalidation_required": True,
            }
        )


@dataclass(frozen=True, slots=True)
class HistoricalDownloadFilter:
    sport: str
    plan: str
    from_day: int
    from_month: int
    from_year: int
    to_day: int
    to_month: int
    to_year: int
    event_id: int | None = None
    event_name: str | None = None
    market_types: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ("M",)

    def __post_init__(self) -> None:
        _text(self.sport, "sport")
        _text(self.plan, "plan")
        start = _day(self.from_year, self.from_month, self.from_day, "from")
        end = _day(self.to_year, self.to_month, self.to_day, "to")
        if end < start:
            raise BetfairHistoricalEntitlementError("download filter end precedes start")
        if self.event_id is not None:
            _uint(self.event_id, "event_id")
        if self.event_name is not None:
            _text(self.event_name, "event_name")
        _texts(self.market_types, "market_types")
        _texts(self.countries, "countries")
        file_types = _texts(self.file_types, "file_types")
        if not file_types or any(value not in {"M", "E"} for value in file_types):
            raise BetfairHistoricalEntitlementError("file_types must contain M or E")
        if set(file_types) == {"M", "E"}:
            raise BetfairHistoricalEntitlementError(
                "M and E duplicate the same data; choose one representation"
            )

    @property
    def required_months(self) -> frozenset[tuple[int, int]]:
        current = (self.from_year, self.from_month)
        end = (self.to_year, self.to_month)
        values: set[tuple[int, int]] = set()
        while True:
            values.add(current)
            if current == end:
                return frozenset(values)
            year, month = current
            current = (year + 1, 1) if month == 12 else (year, month + 1)

    def provider_payload(self) -> dict[str, object]:
        return {
            "sport": self.sport,
            "plan": self.plan,
            "fromDay": self.from_day,
            "fromMonth": self.from_month,
            "fromYear": self.from_year,
            "toDay": self.to_day,
            "toMonth": self.to_month,
            "toYear": self.to_year,
            "eventId": self.event_id,
            "eventName": self.event_name,
            "marketTypesCollection": list(self.market_types),
            "countriesCollection": list(self.countries),
            "fileTypeCollection": list(self.file_types),
        }

    @property
    def filter_sha256(self) -> str:
        return _digest(self.provider_payload())


@dataclass(frozen=True, slots=True)
class HistoricalFileListing:
    session_context_id: str
    entitlement_snapshot_sha256: str
    download_filter_sha256: str
    observed_at: str
    response_sha256: str
    provider_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _context_id(self.session_context_id)
        _sha(self.entitlement_snapshot_sha256, "entitlement_snapshot_sha256")
        _sha(self.download_filter_sha256, "download_filter_sha256")
        _timestamp(self.observed_at, "observed_at")
        _sha(self.response_sha256, "response_sha256")
        if not isinstance(self.provider_paths, tuple):
            raise BetfairHistoricalEntitlementError("provider_paths must be a tuple")
        if len(self.provider_paths) != len(set(self.provider_paths)):
            raise BetfairHistoricalEntitlementError("provider_paths contain duplicates")
        for path in self.provider_paths:
            _path(path)

    @property
    def provider_acquisition_verified(self) -> bool:
        return False

    @property
    def usage_rights_verified(self) -> bool:
        return False

    @property
    def rights_revalidation_required(self) -> bool:
        return True

    @property
    def listing_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_historical_file_listing",
                "schema_version": _SCHEMA_VERSION,
                "session_context_id": self.session_context_id,
                "entitlement_snapshot_sha256": self.entitlement_snapshot_sha256,
                "download_filter_sha256": self.download_filter_sha256,
                "observed_at": self.observed_at,
                "response_sha256": self.response_sha256,
                "provider_paths": list(self.provider_paths),
                "provider_acquisition_verified": False,
                "usage_rights_verified": False,
                "rights_revalidation_required": True,
            }
        )


@dataclass(frozen=True, slots=True)
class HistoricalDownloadedFile:
    session_context_id: str
    entitlement_snapshot_sha256: str
    listing_sha256: str
    provider_path: str
    retrieved_at: str
    raw_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        _context_id(self.session_context_id)
        _sha(self.entitlement_snapshot_sha256, "entitlement_snapshot_sha256")
        _sha(self.listing_sha256, "listing_sha256")
        _path(self.provider_path)
        _timestamp(self.retrieved_at, "retrieved_at")
        _sha(self.raw_sha256, "raw_sha256")
        _uint(self.byte_length, "byte_length")

    @property
    def provider_acquisition_verified(self) -> bool:
        return False

    @property
    def usage_rights_verified(self) -> bool:
        return False

    @property
    def rights_revalidation_required(self) -> bool:
        return True

    @property
    def file_identity_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_historical_downloaded_file",
                "schema_version": _SCHEMA_VERSION,
                "session_context_id": self.session_context_id,
                "entitlement_snapshot_sha256": self.entitlement_snapshot_sha256,
                "listing_sha256": self.listing_sha256,
                "provider_path": self.provider_path,
                "retrieved_at": self.retrieved_at,
                "raw_sha256": self.raw_sha256,
                "byte_length": self.byte_length,
                "provider_acquisition_verified": False,
                "usage_rights_verified": False,
                "rights_revalidation_required": True,
            }
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class HistoricalProviderOriginWitness:
    """Process-local capability proving one exact canonical Betfair download path.

    The witness is deliberately not durable authority. It proves only that the exact
    file bytes were acquired through this process's still-live canonical authenticated
    Historical Data transport chain. Usage/retention rights remain a separate authority.
    """

    session_context_id: str
    entitlement_snapshot_sha256: str
    listing_sha256: str
    download_file_identity_sha256: str
    provider_path: str
    retrieved_at: str
    raw_sha256: str
    byte_length: int
    transport_contract_sha256: str

    def __post_init__(self) -> None:
        _context_id(self.session_context_id)
        _sha(self.entitlement_snapshot_sha256, "entitlement_snapshot_sha256")
        _sha(self.listing_sha256, "listing_sha256")
        _sha(self.download_file_identity_sha256, "download_file_identity_sha256")
        _path(self.provider_path)
        _timestamp(self.retrieved_at, "retrieved_at")
        _sha(self.raw_sha256, "raw_sha256")
        _uint(self.byte_length, "byte_length")
        _sha(self.transport_contract_sha256, "transport_contract_sha256")

    @property
    def witness_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_historical_provider_origin_witness",
                "schema_version": 1,
                "session_context_id": self.session_context_id,
                "entitlement_snapshot_sha256": self.entitlement_snapshot_sha256,
                "listing_sha256": self.listing_sha256,
                "download_file_identity_sha256": self.download_file_identity_sha256,
                "provider_path": self.provider_path,
                "retrieved_at": self.retrieved_at,
                "raw_sha256": self.raw_sha256,
                "byte_length": self.byte_length,
                "transport_contract_sha256": self.transport_contract_sha256,
                "usage_rights_verified": False,
                "rights_revalidation_required": True,
            }
        )

    def _authority_fingerprint(self) -> str:
        return self.witness_sha256

    def assert_authoritative(self) -> None:
        raise BetfairHistoricalEntitlementError(
            "historical provider-origin witness was not issued by the canonical client"
        )

    @property
    def provider_origin_verified(self) -> bool:
        try:
            self.assert_authoritative()
        except BetfairHistoricalEntitlementError:
            return False
        return True

    @property
    def usage_rights_verified(self) -> bool:
        return False

    @property
    def rights_revalidation_required(self) -> bool:
        return True


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        resolved = urljoin(req.full_url, newurl)
        if _origin(req.full_url) != _origin(resolved):
            raise BetfairHistoricalEntitlementError(
                "Betfair Historical Data cross-origin redirect blocked"
            )
        return super().redirect_request(req, fp, code, msg, headers, resolved)


class UrllibBetfairHistoricalTransport:
    def __init__(
        self,
        *,
        max_json_bytes: int = 8 * 1024 * 1024,
        max_file_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        _positive(max_json_bytes, "max_json_bytes")
        _positive(max_file_bytes, "max_file_bytes")
        self._json_limit = max_json_bytes
        self._file_limit = max_file_bytes
        self._opener = build_opener(_SameOriginRedirectHandler())
        self._opener_origin = self._opener
        self._opener_open = self._opener.open
        self._opener_handler_ids = tuple(id(handler) for handler in self._opener.handlers)
        self._construction_origin_verified = (
            build_opener is _CANONICAL_BUILD_OPENER
            and Request is _CANONICAL_REQUEST_TYPE
            and type(self._opener) is OpenerDirector
            and type(self._opener).open is _CANONICAL_OPENER_OPEN
            and _SameOriginRedirectHandler.redirect_request
            is _CANONICAL_REDIRECT_REQUEST
        )

    def post_json(self, url: str, *, ssoid: str, body: bytes, timeout_seconds: float) -> bytes:
        request = _CANONICAL_REQUEST_TYPE(
            url,
            data=body,
            headers={"Content-Type": "application/json", "ssoid": ssoid},
            method="POST",
        )
        return self._request(request, timeout_seconds, self._json_limit)

    def get_file(self, url: str, *, ssoid: str, timeout_seconds: float) -> bytes:
        request = _CANONICAL_REQUEST_TYPE(
            url, headers={"ssoid": ssoid}, method="GET"
        )
        return self._request(request, timeout_seconds, self._file_limit)

    def _request(self, request: Request, timeout_seconds: float, limit: int) -> bytes:
        try:
            with self._opener_open(request, timeout=timeout_seconds) as response:
                payload = response.read(limit + 1)
        except BetfairHistoricalEntitlementError:
            raise
        except HTTPError as exc:
            if exc.code == 429:
                raise BetfairHistoricalBackpressure(
                    "Betfair Historical Data API rate limit/backpressure"
                ) from None
            raise BetfairHistoricalEntitlementError(
                f"Betfair Historical Data HTTP request failed with status {exc.code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise BetfairHistoricalEntitlementError(
                "Betfair Historical Data network request failed"
            ) from None
        if len(payload) > limit:
            raise BetfairHistoricalEntitlementError(
                "Betfair Historical Data response exceeded size limit"
            )
        return payload


_CANONICAL_BUILD_OPENER = build_opener
_CANONICAL_REQUEST_TYPE = Request
_CANONICAL_OPENER_OPEN = OpenerDirector.open
_CANONICAL_OPENER_INTERNAL_OPEN = OpenerDirector._open
_CANONICAL_OPENER_CALL_CHAIN = OpenerDirector._call_chain
_CANONICAL_OPENER_ERROR = OpenerDirector.error
_CANONICAL_REDIRECT_REQUEST = _SameOriginRedirectHandler.redirect_request
_CANONICAL_STDLIB_REDIRECT_HANDLER = _urllib_request.HTTPRedirectHandler
_CANONICAL_HTTPS_HANDLER = _urllib_request.HTTPSHandler
_CANONICAL_HTTPS_OPEN = _urllib_request.HTTPSHandler.https_open
_CANONICAL_HTTPS_REQUEST = _urllib_request.HTTPSHandler.https_request
_CANONICAL_ABSTRACT_HTTP_HANDLER = _urllib_request.AbstractHTTPHandler
_CANONICAL_HTTP_DO_OPEN = _urllib_request.AbstractHTTPHandler.do_open
_CANONICAL_HTTP_ERROR_PROCESSOR = _urllib_request.HTTPErrorProcessor
_CANONICAL_HTTPS_RESPONSE = _urllib_request.HTTPErrorProcessor.https_response
_CANONICAL_HISTORICAL_POST_JSON = UrllibBetfairHistoricalTransport.post_json
_CANONICAL_HISTORICAL_GET_FILE = UrllibBetfairHistoricalTransport.get_file
_CANONICAL_HISTORICAL_REQUEST = UrllibBetfairHistoricalTransport._request
_CANONICAL_QUOTE = quote
_HISTORICAL_TRANSPORT_CONTRACT_SHA256 = sha256(
    json.dumps(
        {
            "schema": "autosport.betfair_historical_transport_contract",
            "schema_version": 1,
            "api_base": HISTORICAL_API_BASE,
            "transport": "urllib_verified_https",
            "redirect_policy": "same_origin_only",
            "download_payload_fence": "bzip2_header",
            "canonical_clock": "datetime.now(timezone.utc)",
            "secrets_persisted": False,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()


def _opener_dispatch_snapshot(
    opener: object,
) -> tuple[tuple[str, object, tuple[object, ...]], ...] | None:
    records: list[tuple[str, object, tuple[object, ...]]] = []
    for map_name in ("handle_open", "process_request", "process_response"):
        mapping = getattr(opener, map_name, None)
        if type(mapping) is not dict:
            return None
        for key, handlers in mapping.items():
            if type(key) not in (str, int) or type(handlers) is not list:
                return None
            records.append((map_name, key, tuple(handlers)))
    error_mapping = getattr(opener, "handle_error", None)
    if type(error_mapping) is not dict:
        return None
    for protocol, by_code in error_mapping.items():
        if type(protocol) not in (str, int) or type(by_code) is not dict:
            return None
        for code, handlers in by_code.items():
            if type(code) not in (str, int) or type(handlers) is not list:
                return None
            records.append((f"handle_error:{protocol}", code, tuple(handlers)))
    records.sort(key=lambda item: (item[0], type(item[1]).__name__, str(item[1])))
    return tuple(records)


def _canonical_historical_network_transport(
    transport: object,
) -> bool:
    """Recognize the still-unmodified built-in Historical Data transport.

    This is a trusted-process executable-surface fence, not provider-signed proof.
    Structural/test transports remain usable but cannot obtain provider-origin capability.
    """

    if type(transport) is not UrllibBetfairHistoricalTransport:
        return False
    if (
        type(transport).post_json is not _CANONICAL_HISTORICAL_POST_JSON
        or type(transport).get_file is not _CANONICAL_HISTORICAL_GET_FILE
        or type(transport)._request is not _CANONICAL_HISTORICAL_REQUEST
        or build_opener is not _CANONICAL_BUILD_OPENER
        or Request is not _CANONICAL_REQUEST_TYPE
        or quote is not _CANONICAL_QUOTE
        or _SameOriginRedirectHandler.redirect_request
        is not _CANONICAL_REDIRECT_REQUEST
        or _urllib_request.OpenerDirector is not OpenerDirector
        or OpenerDirector.open is not _CANONICAL_OPENER_OPEN
        or OpenerDirector._open is not _CANONICAL_OPENER_INTERNAL_OPEN
        or OpenerDirector._call_chain is not _CANONICAL_OPENER_CALL_CHAIN
        or OpenerDirector.error is not _CANONICAL_OPENER_ERROR
        or _urllib_request.HTTPRedirectHandler is not _CANONICAL_STDLIB_REDIRECT_HANDLER
        or _urllib_request.HTTPSHandler is not _CANONICAL_HTTPS_HANDLER
        or _CANONICAL_HTTPS_HANDLER.https_open is not _CANONICAL_HTTPS_OPEN
        or _CANONICAL_HTTPS_HANDLER.https_request is not _CANONICAL_HTTPS_REQUEST
        or _urllib_request.AbstractHTTPHandler is not _CANONICAL_ABSTRACT_HTTP_HANDLER
        or _CANONICAL_ABSTRACT_HTTP_HANDLER.do_open is not _CANONICAL_HTTP_DO_OPEN
        or _urllib_request.HTTPErrorProcessor is not _CANONICAL_HTTP_ERROR_PROCESSOR
        or _CANONICAL_HTTP_ERROR_PROCESSOR.https_response is not _CANONICAL_HTTPS_RESPONSE
    ):
        return False
    state = getattr(transport, "__dict__", None)
    if type(state) is not dict or set(state) != {
        "_json_limit",
        "_file_limit",
        "_opener",
        "_opener_origin",
        "_opener_open",
        "_opener_handler_ids",
        "_construction_origin_verified",
    }:
        return False
    if state["_construction_origin_verified"] is not True:
        return False
    opener = state["_opener"]
    if (
        opener is not state["_opener_origin"]
        or type(opener) is not OpenerDirector
        or type(opener).open is not _CANONICAL_OPENER_OPEN
    ):
        return False
    opener_state = getattr(opener, "__dict__", None)
    if type(opener_state) is not dict or any(
        name in opener_state for name in ("open", "_open", "_call_chain", "error")
    ):
        return False
    open_call = state["_opener_open"]
    if (
        getattr(open_call, "__self__", None) is not opener
        or getattr(open_call, "__func__", None) is not _CANONICAL_OPENER_OPEN
    ):
        return False
    handlers = getattr(opener, "handlers", None)
    if type(handlers) is not list:
        return False
    if tuple(id(handler) for handler in handlers) != state["_opener_handler_ids"]:
        return False

    redirect_handlers = tuple(
        handler
        for handler in handlers
        if isinstance(handler, _CANONICAL_STDLIB_REDIRECT_HANDLER)
    )
    if (
        len(redirect_handlers) != 1
        or type(redirect_handlers[0]) is not _SameOriginRedirectHandler
        or "redirect_request" in vars(redirect_handlers[0])
    ):
        return False

    https_handlers = tuple(
        handler for handler in handlers if isinstance(handler, _CANONICAL_HTTPS_HANDLER)
    )
    if len(https_handlers) != 1 or type(https_handlers[0]) is not _CANONICAL_HTTPS_HANDLER:
        return False
    https_handler = https_handlers[0]
    if any(
        name in vars(https_handler) for name in ("https_open", "https_request", "do_open")
    ):
        return False

    dispatch = _opener_dispatch_snapshot(opener)
    if dispatch is None:
        return False
    https_open_handlers = tuple(
        values
        for map_name, key, values in dispatch
        if map_name == "handle_open" and key == "https"
    )
    https_request_handlers = tuple(
        values
        for map_name, key, values in dispatch
        if map_name == "process_request" and key == "https"
    )
    https_response_handlers = tuple(
        values
        for map_name, key, values in dispatch
        if map_name == "process_response" and key == "https"
    )
    redirect_error_handlers = tuple(
        values
        for map_name, key, values in dispatch
        if map_name == "handle_error:http" and key in {301, 302, 303, 307, 308}
    )
    if (
        len(https_open_handlers) != 1
        or len(https_open_handlers[0]) != 1
        or https_open_handlers[0][0] is not https_handler
        or len(https_request_handlers) != 1
        or len(https_request_handlers[0]) != 1
        or https_request_handlers[0][0] is not https_handler
        or len(https_response_handlers) != 1
        or len(https_response_handlers[0]) != 1
        or type(https_response_handlers[0][0]) is not _CANONICAL_HTTP_ERROR_PROCESSOR
        or "https_response" in vars(https_response_handlers[0][0])
        or len(redirect_error_handlers) != 5
        or any(
            len(values) != 1 or values[0] is not redirect_handlers[0]
            for values in redirect_error_handlers
        )
        or any(
            not any(handler is registered for registered in handlers)
            for _map_name, _key, values in dispatch
            for handler in values
        )
    ):
        return False
    return True


class BetfairHistoricalEntitlementClient:
    def __init__(
        self,
        client: BetfairReadOnlyClient,
        account_identity: BetfairAuthenticatedAccountIdentity,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if type(client) is not BetfairReadOnlyClient:
            raise BetfairHistoricalEntitlementError(
                "historical provenance requires exact canonical BetfairReadOnlyClient"
            )
        self._client = client
        try:
            self._identity = require_authoritative_betfair_account_identity(
                account_identity, client=client
            )
        except Exception as exc:
            raise BetfairHistoricalEntitlementError(
                "historical provenance requires current K07 authenticated context"
            ) from exc
        self._transport = UrllibBetfairHistoricalTransport()
        self._transport_origin = self._transport
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise BetfairHistoricalEntitlementError("timeout_seconds must be positive")
        self._timeout = float(timeout_seconds)
        self._issued: dict[tuple[str, int], tuple[object, str, bool]] = {}

    def get_entitlement_snapshot(self) -> HistoricalEntitlementSnapshot:
        payload, provider_origin = self._post("GetMyData", b"{}")
        decoded = _strict_json(payload)
        if not isinstance(decoded, list):
            raise BetfairHistoricalEntitlementError("GetMyData response must be a JSON array")
        packages = tuple(_package(value, index) for index, value in enumerate(decoded))
        ids = [value.purchase_item_id for value in packages]
        if len(ids) != len(set(ids)):
            raise BetfairHistoricalEntitlementError(
                "GetMyData returned duplicate/conflicting purchaseItemId identity"
            )
        value = HistoricalEntitlementSnapshot(
            self._identity.session_context_id,
            self._now(),
            sha256(payload).hexdigest(),
            packages,
        )
        self._remember(
            "snapshot",
            value,
            value.snapshot_sha256,
            provider_origin=provider_origin,
        )
        return value

    def list_files(
        self,
        snapshot: HistoricalEntitlementSnapshot,
        download_filter: HistoricalDownloadFilter,
    ) -> HistoricalFileListing:
        snapshot_origin = self._require_snapshot(snapshot)
        if type(download_filter) is not HistoricalDownloadFilter:
            raise BetfairHistoricalEntitlementError("download_filter is not canonical")
        months = {
            value.month
            for value in snapshot.packages
            if value.sport == download_filter.sport and value.plan == download_filter.plan
        }
        if not download_filter.required_months.issubset(months):
            raise BetfairHistoricalEntitlementError(
                "download filter month range is not covered by authenticated purchases"
            )
        body = _json_bytes(download_filter.provider_payload())
        payload, provider_origin = self._post("DownloadListOfFiles", body)
        decoded = _strict_json(payload)
        if not isinstance(decoded, list):
            raise BetfairHistoricalEntitlementError(
                "DownloadListOfFiles response must be a JSON array"
            )
        paths = tuple(_path(value) for value in decoded)
        if len(paths) != len(set(paths)):
            raise BetfairHistoricalEntitlementError(
                "DownloadListOfFiles returned duplicate provider paths"
            )
        value = HistoricalFileListing(
            self._identity.session_context_id,
            snapshot.snapshot_sha256,
            download_filter.filter_sha256,
            self._now(),
            sha256(payload).hexdigest(),
            paths,
        )
        self._remember(
            "listing",
            value,
            value.listing_sha256,
            provider_origin=snapshot_origin and provider_origin,
        )
        return value

    def download_file(
        self,
        snapshot: HistoricalEntitlementSnapshot,
        listing: HistoricalFileListing,
        provider_path: str,
    ) -> tuple[HistoricalDownloadedFile, bytes]:
        snapshot_origin = self._require_snapshot(snapshot)
        listing_origin = self._require_listing(listing, snapshot)
        path = _path(provider_path)
        if path not in listing.provider_paths:
            raise BetfairHistoricalEntitlementError(
                "requested provider path was not returned by this exact listing"
            )
        self._require_context()
        origin_before = _canonical_historical_network_transport(self._transport)
        payload = self._transport.get_file(
            (
                f"{HISTORICAL_API_BASE}/DownloadFile?filePath="
                f"{_CANONICAL_QUOTE(path, safe='')}"
            ),
            ssoid=self._session_token(),
            timeout_seconds=self._timeout,
        )
        self._require_context()
        origin_after = _canonical_historical_network_transport(self._transport)
        if len(payload) < 4 or payload[:3] != b"BZh" or payload[3:4] not in b"123456789":
            raise BetfairHistoricalEntitlementError(
                "DownloadFile response is not a canonical bzip2 historical payload"
            )
        value = HistoricalDownloadedFile(
            self._identity.session_context_id,
            snapshot.snapshot_sha256,
            listing.listing_sha256,
            path,
            self._now(),
            sha256(payload).hexdigest(),
            len(payload),
        )
        self._remember(
            "download",
            value,
            value.file_identity_sha256,
            provider_origin=(
                snapshot_origin
                and listing_origin
                and origin_before
                and origin_after
            ),
        )
        return value, payload

    def issue_provider_origin_witness(
        self, evidence: HistoricalDownloadedFile, raw_bytes: bytes
    ) -> HistoricalProviderOriginWitness:
        raise BetfairHistoricalEntitlementError(
            "historical provider-origin witness issuer is not installed"
        )

    def require_authoritative_download(
        self, evidence: HistoricalDownloadedFile, raw_bytes: bytes
    ) -> HistoricalDownloadedFile:
        self._require_context()
        if type(evidence) is not HistoricalDownloadedFile:
            raise BetfairHistoricalEntitlementError("download evidence is not canonical")
        self._require_issued("download", evidence, evidence.file_identity_sha256)
        if not isinstance(raw_bytes, bytes) or (
            len(raw_bytes) != evidence.byte_length
            or sha256(raw_bytes).hexdigest() != evidence.raw_sha256
        ):
            raise BetfairHistoricalEntitlementError(
                "download bytes do not match exact captured file evidence"
            )
        if not evidence.provider_acquisition_verified:
            raise BetfairHistoricalEntitlementError(
                "provider acquisition provenance is not mechanically proven"
            )
        if (
            not evidence.usage_rights_verified
            or evidence.rights_revalidation_required
        ):
            raise BetfairHistoricalEntitlementError(
                "current usage rights require separate revalidation"
            )
        return evidence

    def _post(self, operation: str, body: bytes) -> tuple[bytes, bool]:
        self._require_context()
        origin_before = _canonical_historical_network_transport(self._transport)
        payload = self._transport.post_json(
            f"{HISTORICAL_API_BASE}/{operation}",
            ssoid=self._session_token(),
            body=body,
            timeout_seconds=self._timeout,
        )
        self._require_context()
        origin_after = _canonical_historical_network_transport(self._transport)
        return payload, origin_before and origin_after

    def _session_token(self) -> str:
        self._require_context()
        try:
            return _text(self._client._credentials.session_token, "session token")
        except AttributeError as exc:
            raise BetfairHistoricalEntitlementError(
                "canonical Betfair client lost its session context"
            ) from exc

    def _require_context(self) -> None:
        if (
            type(self._transport) is not UrllibBetfairHistoricalTransport
            or self._transport is not self._transport_origin
        ):
            raise BetfairHistoricalEntitlementError(
                "historical transport origin is no longer canonical"
            )
        try:
            require_authoritative_betfair_account_identity(
                self._identity, client=self._client
            )
        except Exception as exc:
            raise BetfairHistoricalEntitlementError(
                "authenticated Betfair session context is no longer authoritative"
            ) from exc

    def _remember(
        self,
        kind: str,
        value: object,
        digest: str,
        *,
        provider_origin: bool = False,
    ) -> None:
        self._issued[(kind, id(value))] = (value, digest, provider_origin)

    def _require_issued(self, kind: str, value: object, digest: str) -> bool:
        record = self._issued.get((kind, id(value)))
        if record is None or record[0] is not value or record[1] != digest:
            raise BetfairHistoricalEntitlementError(
                f"{kind} was not issued unchanged by this canonical client"
            )
        return record[2]

    def _require_snapshot(self, value: HistoricalEntitlementSnapshot) -> bool:
        self._require_context()
        if type(value) is not HistoricalEntitlementSnapshot:
            raise BetfairHistoricalEntitlementError("entitlement snapshot is not canonical")
        provider_origin = self._require_issued(
            "snapshot", value, value.snapshot_sha256
        )
        if value.session_context_id != self._identity.session_context_id:
            raise BetfairHistoricalEntitlementError("snapshot belongs to another context")
        return provider_origin

    def _require_listing(
        self, value: HistoricalFileListing, snapshot: HistoricalEntitlementSnapshot
    ) -> bool:
        if type(value) is not HistoricalFileListing:
            raise BetfairHistoricalEntitlementError("listing is not canonical")
        provider_origin = self._require_issued(
            "listing", value, value.listing_sha256
        )
        if value.session_context_id != self._identity.session_context_id:
            raise BetfairHistoricalEntitlementError("listing belongs to another context")
        if value.entitlement_snapshot_sha256 != snapshot.snapshot_sha256:
            raise BetfairHistoricalEntitlementError("listing binds another snapshot")
        return provider_origin

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


def _install_provider_origin_authority() -> None:
    issued: dict[
        int,
        tuple[
            object,
            str,
            BetfairHistoricalEntitlementClient,
            HistoricalDownloadedFile,
        ],
    ] = {}
    validate = HistoricalProviderOriginWitness.__post_init__

    def issue_provider_origin_witness(
        self: BetfairHistoricalEntitlementClient,
        evidence: HistoricalDownloadedFile,
        raw_bytes: bytes,
    ) -> HistoricalProviderOriginWitness:
        self._require_context()
        if type(evidence) is not HistoricalDownloadedFile:
            raise BetfairHistoricalEntitlementError(
                "download evidence is not canonical"
            )
        provider_origin = self._require_issued(
            "download", evidence, evidence.file_identity_sha256
        )
        if not isinstance(raw_bytes, bytes) or (
            len(raw_bytes) != evidence.byte_length
            or sha256(raw_bytes).hexdigest() != evidence.raw_sha256
        ):
            raise BetfairHistoricalEntitlementError(
                "download bytes do not match exact captured file evidence"
            )
        if not provider_origin:
            raise BetfairHistoricalEntitlementError(
                "download did not traverse the canonical Historical Data "
                "provider-origin transport chain"
            )
        if not _canonical_historical_network_transport(self._transport):
            raise BetfairHistoricalEntitlementError(
                "historical transport executable origin is no longer canonical"
            )
        witness = HistoricalProviderOriginWitness(
            session_context_id=evidence.session_context_id,
            entitlement_snapshot_sha256=evidence.entitlement_snapshot_sha256,
            listing_sha256=evidence.listing_sha256,
            download_file_identity_sha256=evidence.file_identity_sha256,
            provider_path=evidence.provider_path,
            retrieved_at=evidence.retrieved_at,
            raw_sha256=evidence.raw_sha256,
            byte_length=evidence.byte_length,
            transport_contract_sha256=_HISTORICAL_TRANSPORT_CONTRACT_SHA256,
        )
        key = id(witness)

        def forget(_weakref: object, *, witness_id: int = key) -> None:
            issued.pop(witness_id, None)

        issued[key] = (
            ref(witness, forget),
            witness._authority_fingerprint(),
            self,
            evidence,
        )
        return witness

    def assert_authoritative(self: HistoricalProviderOriginWitness) -> None:
        validate(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairHistoricalEntitlementError(
                "historical provider-origin witness was not issued by the canonical client"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairHistoricalEntitlementError(
                "historical provider-origin witness changed after issuance"
            )
        client = record[2]
        evidence = record[3]
        client._require_context()
        if not _canonical_historical_network_transport(client._transport):
            raise BetfairHistoricalEntitlementError(
                "historical transport executable origin changed after acquisition"
            )
        if not client._require_issued(
            "download", evidence, evidence.file_identity_sha256
        ):
            raise BetfairHistoricalEntitlementError(
                "download chain no longer carries provider-origin capability"
            )
        if (
            self.session_context_id != evidence.session_context_id
            or self.entitlement_snapshot_sha256
            != evidence.entitlement_snapshot_sha256
            or self.listing_sha256 != evidence.listing_sha256
            or self.download_file_identity_sha256
            != evidence.file_identity_sha256
            or self.provider_path != evidence.provider_path
            or self.retrieved_at != evidence.retrieved_at
            or self.raw_sha256 != evidence.raw_sha256
            or self.byte_length != evidence.byte_length
            or self.transport_contract_sha256
            != _HISTORICAL_TRANSPORT_CONTRACT_SHA256
        ):
            raise BetfairHistoricalEntitlementError(
                "historical provider-origin witness no longer matches captured download"
            )

    BetfairHistoricalEntitlementClient.issue_provider_origin_witness = (
        issue_provider_origin_witness
    )
    HistoricalProviderOriginWitness.assert_authoritative = assert_authoritative


_install_provider_origin_authority()
del _install_provider_origin_authority


def _package(value: object, index: int) -> PurchasedHistoricalPackage:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise BetfairHistoricalEntitlementError(f"GetMyData[{index}] must be a JSON object")
    if set(value) != {"sport", "plan", "forDate", "purchaseItemId"}:
        raise BetfairHistoricalEntitlementError(
            f"GetMyData[{index}] has unexpected or missing fields"
        )
    return PurchasedHistoricalPackage(
        _text(value["sport"], "sport"),
        _text(value["plan"], "plan"),
        _text(value["forDate"], "forDate"),
        _uint(value["purchaseItemId"], "purchaseItemId"),
    )


def _strict_json(payload: bytes) -> object:
    if not isinstance(payload, bytes):
        raise BetfairHistoricalEntitlementError("provider response must be bytes")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise BetfairHistoricalEntitlementError(
                    "provider JSON contains duplicate object key"
                )
            result[key] = value
        return result

    def constant(_: str) -> object:
        raise BetfairHistoricalEntitlementError(
            "provider JSON contains non-standard numeric constant"
        )

    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except BetfairHistoricalEntitlementError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BetfairHistoricalEntitlementError("provider response is not valid JSON") from None


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairHistoricalEntitlementError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _path(value: object) -> str:
    path = _text(value, "provider_path")
    if not path.startswith("/data/") or "\\" in path or "\x00" in path:
        raise BetfairHistoricalEntitlementError("provider_path is not canonical /data path")
    if any(part in {".", ".."} for part in path.split("/")):
        raise BetfairHistoricalEntitlementError("provider_path contains traversal")
    return path


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        raise BetfairHistoricalEntitlementError("redirect URL has invalid authority")
    try:
        port = parsed.port
    except ValueError:
        raise BetfairHistoricalEntitlementError("redirect URL has invalid port") from None
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _context_id(value: object) -> str:
    text = _text(value, "session_context_id")
    prefix = "betfair-session-context:"
    if not text.startswith(prefix):
        raise BetfairHistoricalEntitlementError("session_context_id is not canonical")
    _sha(text.removeprefix(prefix), "session_context_id")
    return text


def _provider_month(value: object) -> datetime:
    text = _text(value, "forDate")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BetfairHistoricalEntitlementError("forDate must be ISO-8601") from None


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BetfairHistoricalEntitlementError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairHistoricalEntitlementError(f"{field} must include timezone offset")
    return text


def _day(year: int, month: int, day: int, field: str) -> date:
    if any(not isinstance(v, int) or isinstance(v, bool) for v in (year, month, day)):
        raise BetfairHistoricalEntitlementError(f"{field} date parts must be integers")
    try:
        return date(year, month, day)
    except ValueError:
        raise BetfairHistoricalEntitlementError(f"{field} date is invalid") from None


def _texts(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise BetfairHistoricalEntitlementError(f"{field} must be a tuple")
    result = tuple(_text(item, field) for item in value)
    if len(result) != len(set(result)):
        raise BetfairHistoricalEntitlementError(f"{field} must not contain duplicates")
    return result


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairHistoricalEntitlementError(f"{field} must be non-empty trimmed text")
    return value


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BetfairHistoricalEntitlementError(f"{field} must be lowercase SHA-256")
    return text


def _uint(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BetfairHistoricalEntitlementError(f"{field} must be a non-negative integer")
    return value


def _positive(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BetfairHistoricalEntitlementError(f"{field} must be a positive integer")
    return value
