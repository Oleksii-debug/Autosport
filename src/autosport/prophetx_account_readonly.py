"""Strict sandbox-only ProphetX wallet evidence adapter.

This module intentionally implements only the documented read-only Trading API balance
surface. It never authenticates with access/secret keys, places/cancels orders, infers a
production host, or turns ProphetX exposure-credit / locked-funds fields into spendable cash.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from http.client import HTTPException
import json
import math
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from weakref import WeakKeyDictionary

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


SANDBOX_BASE_URL = "https://api.sandbox.prophetx.dev/partner"
BALANCE_URL = f"{SANDBOX_BASE_URL}/v4/mm/get_balance"
ADAPTER_ID = "prophetx-trading-api-readonly-sandbox"
ADAPTER_VERSION = "1"
PROVIDER_CURRENCY = "USD"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProphetXReadOnlyError(RuntimeError):
    """Raised when ProphetX read-only evidence cannot be accepted safely."""


@dataclass(frozen=True, slots=True, repr=False)
class ProphetXSessionToken:
    access_token: str

    def __post_init__(self) -> None:
        _session_token(self.access_token)

    def __repr__(self) -> str:
        return "ProphetXSessionToken(access_token=<redacted>)"


@dataclass(frozen=True, slots=True)
class ProphetXHttpResponse:
    status: int
    final_url: str
    content_type: str | None
    content_encoding: str | None
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise TypeError("status must be an integer")
        _required_text(self.final_url, "final_url")
        if self.content_type is not None and not isinstance(self.content_type, str):
            raise TypeError("content_type must be str or None")
        if self.content_encoding is not None and not isinstance(self.content_encoding, str):
            raise TypeError("content_encoding must be str or None")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")


class ProphetXHttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProphetXHttpResponse: ...


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ProphetXReadOnlyError("ProphetX HTTP redirect refused")


def _make_provider_fetch(
    opener: object,
    max_response_bytes: int,
):
    """Capture one construction-time network primitive for positive wallet authority."""

    open_response = opener.open
    request_type = Request

    def fetch(
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProphetXHttpResponse:
        if url != BALANCE_URL:
            raise ProphetXReadOnlyError(
                "ProphetX transport target is outside the fixed balance origin"
            )
        request = request_type(url, headers=dict(headers), method="GET")
        try:
            with open_response(request, timeout=timeout_seconds) as response:
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise ProphetXReadOnlyError(
                        "ProphetX response exceeded the size limit"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    stripped = content_length.strip()
                    if (
                        not stripped
                        or not stripped.isascii()
                        or not stripped.isdigit()
                    ):
                        raise ProphetXReadOnlyError(
                            "ProphetX response Content-Length is invalid"
                        )
                    if int(stripped) != len(body):
                        raise ProphetXReadOnlyError(
                            "ProphetX response Content-Length does not match body"
                        )
                return ProphetXHttpResponse(
                    status=int(response.getcode()),
                    final_url=str(response.geturl()),
                    content_type=response.headers.get("Content-Type"),
                    content_encoding=response.headers.get("Content-Encoding"),
                    body=body,
                )
        except ProphetXReadOnlyError:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProphetXReadOnlyError(
                f"ProphetX HTTP request failed with status {status}"
            ) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ProphetXReadOnlyError("ProphetX network request failed") from None

    return fetch


class UrllibProphetXHttpTransport:
    def __init__(self, *, max_response_bytes: int = _MAX_RESPONSE_BYTES) -> None:
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _RejectRedirectHandler())
        self._provider_fetch = _make_provider_fetch(
            self._opener,
            max_response_bytes,
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProphetXHttpResponse:
        if url != BALANCE_URL:
            raise ProphetXReadOnlyError(
                "ProphetX transport target is outside the fixed balance origin"
            )
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(self._max_response_bytes + 1)
                if len(body) > self._max_response_bytes:
                    raise ProphetXReadOnlyError(
                        "ProphetX response exceeded the size limit"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    stripped = content_length.strip()
                    if (
                        not stripped
                        or not stripped.isascii()
                        or not stripped.isdigit()
                    ):
                        raise ProphetXReadOnlyError(
                            "ProphetX response Content-Length is invalid"
                        )
                    if int(stripped) != len(body):
                        raise ProphetXReadOnlyError(
                            "ProphetX response Content-Length does not match body"
                        )
                return ProphetXHttpResponse(
                    status=int(response.getcode()),
                    final_url=str(response.geturl()),
                    content_type=response.headers.get("Content-Type"),
                    content_encoding=response.headers.get("Content-Encoding"),
                    body=body,
                )
        except ProphetXReadOnlyError:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProphetXReadOnlyError(
                f"ProphetX HTTP request failed with status {status}"
            ) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ProphetXReadOnlyError("ProphetX network request failed") from None


_CANONICAL_WALLET_GET = UrllibProphetXHttpTransport.get
_PROVIDER_TRANSPORTS: WeakKeyDictionary[
    object, UrllibProphetXHttpTransport
] = WeakKeyDictionary()
_PROVIDER_FETCHES: WeakKeyDictionary[object, object] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class ProphetXEvidence:
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _iso_timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")


@dataclass(frozen=True, slots=True)
class ProphetXWalletObservation:
    """Provider-native wallet evidence from one exact get_balance response.

    Only 'balance' maps to canonical available cash. GEC, matched funds and unmatched
    funds remain provider-specific evidence because they have distinct economic semantics.
    """

    balance: Decimal
    gec_balance: Decimal
    matched_order_balance: Decimal
    unmatched_order_balance: Decimal
    unmatched_order_balance_status: str
    unmatched_order_last_synced_at: str | None
    evidence: ProphetXEvidence

    def __post_init__(self) -> None:
        _nonnegative_decimal(self.balance, "balance")
        _nonnegative_decimal(self.gec_balance, "gec_balance")
        _nonnegative_decimal(self.matched_order_balance, "matched_order_balance")
        _nonnegative_decimal(self.unmatched_order_balance, "unmatched_order_balance")
        if self.unmatched_order_balance_status not in {"succeed", "failed"}:
            raise ProphetXReadOnlyError(
                "unmatched_order_balance_status must be 'succeed' or 'failed'"
            )
        if self.unmatched_order_last_synced_at is not None:
            _iso_timestamp(
                self.unmatched_order_last_synced_at,
                "unmatched_order_last_synced_at",
            )
        if (
            self.unmatched_order_balance_status == "succeed"
            and self.unmatched_order_last_synced_at is None
        ):
            raise ProphetXReadOnlyError(
                "successful unmatched-order balance requires last-synced timestamp"
            )
        if not isinstance(self.evidence, ProphetXEvidence):
            raise ProphetXReadOnlyError("wallet evidence must be ProphetXEvidence")


class ProphetXReadOnlyClient:
    """One fixed-origin ProphetX sandbox balance reader.

    The caller supplies an already-issued bearer session token. Login/session renewal is a
    separate credential-plane concern and is intentionally outside this source slice.

    Caller-supplied transports remain useful for deterministic structural parsing tests, but
    they cannot mint canonical provider/account authority. Positive BALANCE_READ authority is
    reserved for the exact fixed-origin transport instance constructed internally by this
    client and is invalidated if that transport is replaced after construction.
    """

    def __init__(
        self,
        session: ProphetXSessionToken,
        *,
        transport: ProphetXHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        venue_id: str = "prophetx",
        account_id: str = "default-account",
    ) -> None:
        if not isinstance(session, ProphetXSessionToken):
            raise TypeError("session must be ProphetXSessionToken")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        self._session = session
        if transport is None:
            canonical_transport = UrllibProphetXHttpTransport()
            self._transport: ProphetXHttpTransport = canonical_transport
            if type(self) is ProphetXReadOnlyClient:
                _PROVIDER_TRANSPORTS[self] = canonical_transport
                _PROVIDER_FETCHES[self] = canonical_transport._provider_fetch
        else:
            self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._venue_id = _required_text(venue_id, "venue_id")
        self._account_id = _required_text(account_id, "account_id")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, "
            f"adapter_version={ADAPTER_VERSION!r}, environment='sandbox')"
        )

    def read_wallet(self) -> ProphetXWalletObservation:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._session.access_token}",
        }
        canonical_transport = _PROVIDER_TRANSPORTS.get(self)
        authoritative_fetch = None
        if canonical_transport is not None:
            authoritative_fetch = _require_canonical_network_authority(self)
            response = authoritative_fetch(
                BALANCE_URL,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
            )
        else:
            response = self._transport.get(
                BALANCE_URL,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
            )
        self._validate_http_response(response)
        if (
            authoritative_fetch is not None
            and _require_canonical_network_authority(self)
            is not authoritative_fetch
        ):
            raise ProphetXReadOnlyError(
                "canonical ProphetX account network authority changed during acquisition"
            )
        payload_sha256 = sha256(response.body).hexdigest()
        decoded = _decode_json(response.body)
        envelope = _mapping(decoded, "ProphetX balance response")
        data = _mapping(envelope.get("data"), "ProphetX balance response.data")
        balance = _provider_money(data, "balance")
        gec_balance = _provider_money(data, "gec_balance")
        matched_order_balance = _provider_money(data, "matched_order_balance")
        unmatched_order_balance = _provider_money(data, "unmatched_order_balance")
        status = _provider_text(
            data,
            "unmatched_order_balance_status",
            "unmatched_order_balance_status",
        )
        last_synced_raw = data.get("unmatched_order_last_synced_at")
        if last_synced_raw is not None:
            last_synced = _required_text(
                last_synced_raw,
                "unmatched_order_last_synced_at",
            )
        else:
            last_synced = None

        # This is product availability time, not a provider-native timestamp.
        # Capture it only after complete-body, representation and field parsing
        # have succeeded so accepted evidence cannot be backdated by parse time.
        observed_at = self._observed_at()
        return ProphetXWalletObservation(
            balance=balance,
            gec_balance=gec_balance,
            matched_order_balance=matched_order_balance,
            unmatched_order_balance=unmatched_order_balance,
            unmatched_order_balance_status=status,
            unmatched_order_last_synced_at=last_synced,
            evidence=ProphetXEvidence(observed_at, payload_sha256),
        )

    def capability_profile(self) -> BookmakerCapabilityProfile:
        wallet = self.read_wallet()
        self._require_synchronized_wallet(wallet)
        self._require_provider_origin_authority()
        return self._profile_for(wallet)

    def read_account_snapshot(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
        /,
    ) -> BookmakerAccountSnapshot:
        if not isinstance(requested_capabilities, frozenset):
            raise TypeError("requested_capabilities must be a frozenset")
        if not requested_capabilities:
            raise ProphetXReadOnlyError(
                "at least one account capability must be requested"
            )
        if requested_capabilities != frozenset({BookmakerCapability.BALANCE_READ}):
            raise ProphetXReadOnlyError(
                "ProphetX wallet adapter implements only balance_read"
            )

        wallet = self.read_wallet()
        self._require_synchronized_wallet(wallet)
        self._require_provider_origin_authority()
        profile = self._profile_for(wallet)
        balance = BookmakerBalanceObservation(
            venue_id=self._venue_id,
            account_id=self._account_id,
            adapter_id=ADAPTER_ID,
            observation_id=f"wallet:{wallet.evidence.source_payload_sha256}",
            currency=PROVIDER_CURRENCY,
            available_balance=wallet.balance,
            observed_at=wallet.evidence.observed_at,
            source_payload_sha256=wallet.evidence.source_payload_sha256,
            total_balance=None,
            exposure=None,
            retained_commission=None,
            exposure_limit=None,
        )
        return BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
            observed_at=wallet.evidence.observed_at,
            balance=balance,
        )

    def _profile_for(
        self, wallet: ProphetXWalletObservation
    ) -> BookmakerCapabilityProfile:
        digest = wallet.evidence.source_payload_sha256
        return BookmakerCapabilityProfile(
            venue_id=self._venue_id,
            account_id=self._account_id,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            profile_version=1,
            facts=(
                BookmakerCapabilityFact(
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapabilityState.SUPPORTED,
                ),
            ),
            observed_at=wallet.evidence.observed_at,
            source_ref=f"prophetx://sandbox/wallet/{digest}",
            source_payload_sha256=digest,
        )

    def _require_provider_origin_authority(self) -> None:
        _require_canonical_network_authority(self)

    @staticmethod
    def _require_synchronized_wallet(wallet: ProphetXWalletObservation) -> None:
        if wallet.unmatched_order_balance_status != "succeed":
            raise ProphetXReadOnlyError(
                "ProphetX unmatched-order balance synchronization is not successful"
            )

    @staticmethod
    def _validate_http_response(response: ProphetXHttpResponse) -> None:
        if not isinstance(response, ProphetXHttpResponse):
            raise ProphetXReadOnlyError(
                "ProphetX transport must return ProphetXHttpResponse"
            )
        if response.status != 200:
            raise ProphetXReadOnlyError(
                f"ProphetX HTTP request failed with status {response.status}"
            )
        if response.final_url != BALANCE_URL:
            raise ProphetXReadOnlyError(
                "ProphetX response origin changed unexpectedly"
            )
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise ProphetXReadOnlyError(
                "ProphetX response exceeded the size limit"
            )
        if response.content_encoding is not None:
            encoding = response.content_encoding.strip().lower()
            if encoding and encoding != "identity":
                raise ProphetXReadOnlyError(
                    "ProphetX response used unsupported content encoding"
                )
        if response.content_type is None:
            raise ProphetXReadOnlyError(
                "ProphetX response is missing Content-Type"
            )
        media_type = response.content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ProphetXReadOnlyError(
                "ProphetX response Content-Type is not application/json"
            )

    def _observed_at(self) -> str:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ProphetXReadOnlyError(
                "clock must return timezone-aware datetime"
            )
        return value.isoformat()


def _require_canonical_network_authority(
    client: ProphetXReadOnlyClient,
):
    """Return the exact construction-time wallet fetch or fail closed on drift."""

    canonical = _PROVIDER_TRANSPORTS.get(client)
    expected_fetch = _PROVIDER_FETCHES.get(client)
    if (
        type(client) is not ProphetXReadOnlyClient
        or canonical is None
        or type(canonical) is not UrllibProphetXHttpTransport
        or client._transport is not canonical
        or type(canonical).get is not _CANONICAL_WALLET_GET
        or expected_fetch is None
        or canonical._provider_fetch is not expected_fetch
    ):
        raise ProphetXReadOnlyError(
            "canonical ProphetX account authority requires product-owned transport"
        )
    return expected_fetch


def _decode_json(payload: bytes) -> object:
    def reject_duplicate_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProphetXReadOnlyError(
                    "ProphetX JSON contains duplicate object key"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ProphetXReadOnlyError(
            "ProphetX JSON contains non-standard numeric constant"
        )

    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except ProphetXReadOnlyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProphetXReadOnlyError(
            "ProphetX response is not valid UTF-8 JSON"
        ) from None


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ProphetXReadOnlyError(f"{field} must be a JSON object")
    return value


def _provider_text(
    value: Mapping[str, object], key: str, field: str
) -> str:
    if key not in value:
        raise ProphetXReadOnlyError(
            f"{field} is missing from provider response"
        )
    return _required_text(value[key], field)


def _provider_money(value: Mapping[str, object], key: str) -> Decimal:
    if key not in value:
        raise ProphetXReadOnlyError(
            f"{key} is missing from provider response"
        )
    raw = value[key]
    if not isinstance(raw, Decimal):
        raise ProphetXReadOnlyError(
            f"{key} must be a JSON number decoded without binary float"
        )
    return _nonnegative_decimal(raw, key)


def _session_token(value: object) -> str:
    text = _required_text(value, "access_token")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text):
        raise ProphetXReadOnlyError(
            "access_token must not contain whitespace or control characters"
        )
    return text


def _required_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ProphetXReadOnlyError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ProphetXReadOnlyError(
            f"{field} must be a finite Decimal"
        )
    if value < 0:
        raise ProphetXReadOnlyError(
            f"{field} must be non-negative"
        )
    return value


def _iso_timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        raise ProphetXReadOnlyError(
            f"{field} must be ISO-8601"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXReadOnlyError(
            f"{field} must include timezone offset"
        )
    return parsed


def _sha256_hex(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise ProphetXReadOnlyError(
            f"{field} must be lowercase 64-character SHA-256"
        )
    return text
