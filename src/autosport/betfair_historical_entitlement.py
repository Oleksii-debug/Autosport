"""Fail-closed Betfair Historical Data acquisition provenance.

Consumes the canonical K07 authenticated session context and records the exact
GetMyData purchase snapshot, DownloadListOfFiles result, and DownloadFile bytes.
This authority is PERSONAL_NONCOMMERCIAL only.  It does not prove stable
cross-session account identity, commercial/redistribution rights, live freshness,
execution, settlement, or real-money truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
            }
        )


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

    def post_json(self, url: str, *, ssoid: str, body: bytes, timeout_seconds: float) -> bytes:
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "ssoid": ssoid},
            method="POST",
        )
        return self._request(request, timeout_seconds, self._json_limit)

    def get_file(self, url: str, *, ssoid: str, timeout_seconds: float) -> bytes:
        request = Request(url, headers={"ssoid": ssoid}, method="GET")
        return self._request(request, timeout_seconds, self._file_limit)

    def _request(self, request: Request, timeout_seconds: float, limit: int) -> bytes:
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
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
        self._issued: dict[tuple[str, int], tuple[object, str]] = {}

    def get_entitlement_snapshot(self) -> HistoricalEntitlementSnapshot:
        payload = self._post("GetMyData", b"{}")
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
        self._remember("snapshot", value, value.snapshot_sha256)
        return value

    def list_files(
        self,
        snapshot: HistoricalEntitlementSnapshot,
        download_filter: HistoricalDownloadFilter,
    ) -> HistoricalFileListing:
        self._require_snapshot(snapshot)
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
        payload = self._post("DownloadListOfFiles", body)
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
        self._remember("listing", value, value.listing_sha256)
        return value

    def download_file(
        self,
        snapshot: HistoricalEntitlementSnapshot,
        listing: HistoricalFileListing,
        provider_path: str,
    ) -> tuple[HistoricalDownloadedFile, bytes]:
        self._require_snapshot(snapshot)
        self._require_listing(listing, snapshot)
        path = _path(provider_path)
        if path not in listing.provider_paths:
            raise BetfairHistoricalEntitlementError(
                "requested provider path was not returned by this exact listing"
            )
        self._require_context()
        payload = self._transport.get_file(
            f"{HISTORICAL_API_BASE}/DownloadFile?filePath={quote(path, safe='')}",
            ssoid=self._session_token(),
            timeout_seconds=self._timeout,
        )
        self._require_context()
        value = HistoricalDownloadedFile(
            self._identity.session_context_id,
            snapshot.snapshot_sha256,
            listing.listing_sha256,
            path,
            self._now(),
            sha256(payload).hexdigest(),
            len(payload),
        )
        self._remember("download", value, value.file_identity_sha256)
        return value, payload

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
                "download bytes do not match exact provider acquisition evidence"
            )
        return evidence

    def _post(self, operation: str, body: bytes) -> bytes:
        self._require_context()
        payload = self._transport.post_json(
            f"{HISTORICAL_API_BASE}/{operation}",
            ssoid=self._session_token(),
            body=body,
            timeout_seconds=self._timeout,
        )
        self._require_context()
        return payload

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

    def _remember(self, kind: str, value: object, digest: str) -> None:
        self._issued[(kind, id(value))] = (value, digest)

    def _require_issued(self, kind: str, value: object, digest: str) -> None:
        record = self._issued.get((kind, id(value)))
        if record is None or record[0] is not value or record[1] != digest:
            raise BetfairHistoricalEntitlementError(
                f"{kind} was not issued unchanged by this canonical client"
            )

    def _require_snapshot(self, value: HistoricalEntitlementSnapshot) -> None:
        self._require_context()
        if type(value) is not HistoricalEntitlementSnapshot:
            raise BetfairHistoricalEntitlementError("entitlement snapshot is not canonical")
        self._require_issued("snapshot", value, value.snapshot_sha256)
        if value.session_context_id != self._identity.session_context_id:
            raise BetfairHistoricalEntitlementError("snapshot belongs to another context")

    def _require_listing(
        self, value: HistoricalFileListing, snapshot: HistoricalEntitlementSnapshot
    ) -> None:
        if type(value) is not HistoricalFileListing:
            raise BetfairHistoricalEntitlementError("listing is not canonical")
        self._require_issued("listing", value, value.listing_sha256)
        if value.session_context_id != self._identity.session_context_id:
            raise BetfairHistoricalEntitlementError("listing belongs to another context")
        if value.entitlement_snapshot_sha256 != snapshot.snapshot_sha256:
            raise BetfairHistoricalEntitlementError("listing binds another snapshot")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


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
