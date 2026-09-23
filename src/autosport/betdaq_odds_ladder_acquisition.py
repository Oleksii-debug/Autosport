from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from secrets import token_hex
from typing import Callable, Protocol
import xml.etree.ElementTree as ET

from . import betdaq_account_readonly as _account_readonly
from .betdaq_account_readonly import BetdaqCredentials, UrllibBetdaqSoapTransport
from .betdaq_odds_ladder_wire import BetdaqOddsLadderEntry, parse_get_odds_ladder_response
from .betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS


BETDAQ_ODDS_LADDER_ENDPOINT = "https://api.betdaq.com/v2.0/ReadOnlyService.asmx"
BETDAQ_ODDS_LADDER_SOAP_ACTION = (
    "http://www.GlobalBettingExchange.com/ExternalAPI/GetOddsLadder"
)
BETDAQ_DECIMAL_PRICE_FORMAT = 1
_SCHEMA = "autosport.betdaq-odds-ladder-acquisition-v1"
_ACQ_PREFIX = "betdaq-ladder-acq:"
_CANONICAL_POST = UrllibBetdaqSoapTransport.post
_CANONICAL_URLOPEN = _account_readonly.urlopen
_ORIGIN_WITNESS = object()
_CLOCK_WITNESS = object()


class BetdaqOddsLadderAcquisitionError(RuntimeError):
    pass


class BetdaqOddsLadderUseError(BetdaqOddsLadderAcquisitionError):
    pass


class BetdaqOddsLadderTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes: ...


Clock = Callable[[], str]
IdFactory = Callable[[], str]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    return token_hex(16)


@dataclass(frozen=True, slots=True)
class BetdaqOddsLadderRequest:
    price_format: int = BETDAQ_DECIMAL_PRICE_FORMAT

    def __post_init__(self) -> None:
        if type(self.price_format) is not int:
            raise TypeError("price_format must be an integer provider enum")
        if self.price_format != BETDAQ_DECIMAL_PRICE_FORMAT:
            raise ValueError(
                "Autosport BETDAQ ladder acquisition requires Decimal PriceFormat=1"
            )


@dataclass(frozen=True, slots=True)
class BetdaqOddsLadderObservation:
    acquisition_id: str
    observed_at: str
    price_format: int
    request_fingerprint_sha256: str
    response_sha256: str
    response_byte_count: int
    content_sha256: str
    entries: tuple[BetdaqOddsLadderEntry, ...]
    provider_message_created_at: str | None
    call_id: str | None
    evidence_sha256: str
    _origin_witness: object
    _clock_witness: object

    def __post_init__(self) -> None:
        if _canonical_acq_id(self.acquisition_id) != self.acquisition_id:
            raise BetdaqOddsLadderAcquisitionError("non-canonical acquisition_id")
        _time(self.observed_at, "observed_at")
        if type(self.price_format) is not int or self.price_format != 1:
            raise BetdaqOddsLadderAcquisitionError("evidence must use PriceFormat=1")
        for value, name in (
            (self.request_fingerprint_sha256, "request_fingerprint_sha256"),
            (self.response_sha256, "response_sha256"),
            (self.content_sha256, "content_sha256"),
            (self.evidence_sha256, "evidence_sha256"),
        ):
            _sha(value, name)
        if type(self.response_byte_count) is not int or self.response_byte_count <= 0:
            raise BetdaqOddsLadderAcquisitionError(
                "response_byte_count must be positive"
            )
        if not self.entries or any(
            not isinstance(item, BetdaqOddsLadderEntry) for item in self.entries
        ):
            raise BetdaqOddsLadderAcquisitionError("validated ladder entries required")
        if len({item.price for item in self.entries}) != len(self.entries):
            raise BetdaqOddsLadderAcquisitionError("duplicate numeric ladder price")
        if self.provider_message_created_at is not None:
            _time(self.provider_message_created_at, "provider_message_created_at")
        if self.call_id is not None:
            _text(self.call_id, "call_id")
        expected = _evidence_sha(
            self.acquisition_id,
            self.observed_at,
            self.price_format,
            self.request_fingerprint_sha256,
            self.response_sha256,
            self.response_byte_count,
            self.content_sha256,
            self.provider_message_created_at,
            self.call_id,
        )
        if self.evidence_sha256 != expected:
            raise BetdaqOddsLadderAcquisitionError("evidence digest mismatch")

    @property
    def provider_origin_verified(self) -> bool:
        return self._origin_witness is _ORIGIN_WITNESS

    @property
    def receipt_clock_verified(self) -> bool:
        return self._clock_witness is _CLOCK_WITNESS

    @property
    def provider_freshness_proven(self) -> bool:
        return False

    @property
    def live_entitlement_verified(self) -> bool:
        return False

    @property
    def write_permission_proven(self) -> bool:
        return False

    def exact_entry(self, price: Decimal) -> BetdaqOddsLadderEntry:
        price = _price(price)
        for item in self.entries:
            if item.price == price:
                return item
        raise BetdaqOddsLadderUseError(
            "requested price is not present on the acquired BETDAQ odds ladder"
        )

    def to_safe_record(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "acquisition_id": self.acquisition_id,
            "observed_at": self.observed_at,
            "price_format": self.price_format,
            "request_fingerprint_sha256": self.request_fingerprint_sha256,
            "response_sha256": self.response_sha256,
            "response_byte_count": self.response_byte_count,
            "content_sha256": self.content_sha256,
            "entries": [
                {"price_text": x.price_text, "representation": x.representation}
                for x in self.entries
            ],
            "provider_message_created_at": self.provider_message_created_at,
            "call_id": self.call_id,
            "evidence_sha256": self.evidence_sha256,
            "historical_provider_origin_verified": self.provider_origin_verified,
            "historical_receipt_clock_verified": self.receipt_clock_verified,
            "provider_freshness_proven": False,
            "write_permission_proven": False,
        }


@dataclass(frozen=True, slots=True)
class BetdaqLadderUseEvidence:
    ladder_evidence_sha256: str
    price: Decimal
    price_text: str
    ladder_observed_at: str
    evaluated_at: str
    max_age_seconds: int
    _origin_witness: object
    _clock_witness: object

    def __post_init__(self) -> None:
        _sha(self.ladder_evidence_sha256, "ladder_evidence_sha256")
        _price(self.price)
        _text(self.price_text, "price_text")
        _time(self.ladder_observed_at, "ladder_observed_at")
        _time(self.evaluated_at, "evaluated_at")
        if type(self.max_age_seconds) is not int or self.max_age_seconds <= 0:
            raise BetdaqOddsLadderUseError("max_age_seconds must be positive")
        if self._origin_witness is not _ORIGIN_WITNESS:
            raise BetdaqOddsLadderUseError("verified provider origin required")
        if self._clock_witness is not _CLOCK_WITNESS:
            raise BetdaqOddsLadderUseError("verified receipt clock required")

    @property
    def provider_origin_verified(self) -> bool:
        return self._origin_witness is _ORIGIN_WITNESS

    @property
    def receipt_clock_verified(self) -> bool:
        return self._clock_witness is _CLOCK_WITNESS

    @property
    def provider_freshness_proven(self) -> bool:
        return False

    @property
    def write_permission_proven(self) -> bool:
        return False


class BetdaqOddsLadderAcquirer:
    """Read-only live GetOddsLadder acquisition. No provider write surface."""

    def __init__(
        self,
        *,
        credentials: BetdaqCredentials,
        transport: BetdaqOddsLadderTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Clock = utc_now_iso,
        acquisition_id_factory: IdFactory = _new_id,
    ) -> None:
        if type(credentials) is not BetdaqCredentials:
            raise TypeError("credentials must be canonical BetdaqCredentials")
        selected: object = UrllibBetdaqSoapTransport() if transport is None else transport
        if not callable(getattr(selected, "post", None)):
            raise TypeError("transport must expose post")
        if (
            isinstance(timeout_seconds, bool)
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive finite")
        if not callable(clock) or not callable(acquisition_id_factory):
            raise TypeError("clock and acquisition_id_factory must be callable")
        self._credentials = credentials
        self._transport = selected
        self._timeout = float(timeout_seconds)
        self._clock = clock
        self._id_factory = acquisition_id_factory
        self._clock_verified = clock is utc_now_iso

    def acquire(
        self,
        request: BetdaqOddsLadderRequest = BetdaqOddsLadderRequest(),
    ) -> BetdaqOddsLadderObservation:
        if type(request) is not BetdaqOddsLadderRequest:
            raise TypeError("request must be BetdaqOddsLadderRequest")
        body = _soap_request(self._credentials, request)
        try:
            payload = self._transport.post(
                BETDAQ_ODDS_LADDER_ENDPOINT,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{BETDAQ_ODDS_LADDER_SOAP_ACTION}"',
                },
                body=body,
                timeout_seconds=self._timeout,
            )
        except Exception:
            raise BetdaqOddsLadderAcquisitionError(
                "BETDAQ odds-ladder transport failed"
            ) from None
        if type(payload) is not bytes:
            raise BetdaqOddsLadderAcquisitionError(
                "BETDAQ odds-ladder transport returned non-bytes payload"
            )

        wire = parse_get_odds_ladder_response(payload)
        observed_at = _clock_text(self._clock)
        acquisition_id = _canonical_acq_id(self._id_factory())
        request_fp = _request_fingerprint(request, self._credentials)
        response_sha = sha256(payload).hexdigest()
        evidence_sha = _evidence_sha(
            acquisition_id,
            observed_at,
            request.price_format,
            request_fp,
            response_sha,
            len(payload),
            wire.content_sha256,
            wire.provider_created_at_text,
            wire.call_id,
        )
        return BetdaqOddsLadderObservation(
            acquisition_id=acquisition_id,
            observed_at=observed_at,
            price_format=request.price_format,
            request_fingerprint_sha256=request_fp,
            response_sha256=response_sha,
            response_byte_count=len(payload),
            content_sha256=wire.content_sha256,
            entries=wire.entries,
            provider_message_created_at=wire.provider_created_at_text,
            call_id=wire.call_id,
            evidence_sha256=evidence_sha,
            _origin_witness=(
                _ORIGIN_WITNESS if _canonical_transport(self._transport) else object()
            ),
            _clock_witness=_CLOCK_WITNESS if self._clock_verified else object(),
        )


def qualify_exact_price_for_local_use(
    observation: BetdaqOddsLadderObservation,
    price: Decimal,
    *,
    max_age_seconds: int,
    clock: Clock = utc_now_iso,
) -> BetdaqLadderUseEvidence:
    """Exact membership + local age only; never provider freshness/write authority."""

    if not isinstance(observation, BetdaqOddsLadderObservation):
        raise TypeError("observation must be BetdaqOddsLadderObservation")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise BetdaqOddsLadderUseError("max_age_seconds must be positive")
    if clock is not utc_now_iso:
        raise BetdaqOddsLadderUseError("canonical product clock required")
    if not observation.provider_origin_verified:
        raise BetdaqOddsLadderUseError("verified BETDAQ provider origin required")
    if not observation.receipt_clock_verified:
        raise BetdaqOddsLadderUseError("verified product receipt clock required")
    entry = observation.exact_entry(price)
    observed = _time(observation.observed_at, "observed_at")
    evaluated_at = _clock_text(clock)
    age = (_time(evaluated_at, "evaluated_at") - observed).total_seconds()
    if age < 0:
        raise BetdaqOddsLadderUseError("product clock moved behind ladder observation")
    if age > max_age_seconds:
        raise BetdaqOddsLadderUseError("odds-ladder local use horizon exceeded")
    return BetdaqLadderUseEvidence(
        observation.evidence_sha256,
        entry.price,
        entry.price_text,
        observation.observed_at,
        evaluated_at,
        max_age_seconds,
        _ORIGIN_WITNESS,
        _CLOCK_WITNESS,
    )


def restore_structural_odds_ladder_observation(
    record: dict[str, object],
) -> BetdaqOddsLadderObservation:
    """Restore audit structure only; restart never restores live-use witnesses."""

    if type(record) is not dict or record.get("schema") != _SCHEMA:
        raise BetdaqOddsLadderAcquisitionError("unsupported odds-ladder record")
    if record.get("provider_freshness_proven") is not False:
        raise BetdaqOddsLadderAcquisitionError("record cannot claim provider freshness")
    if record.get("write_permission_proven") is not False:
        raise BetdaqOddsLadderAcquisitionError("record cannot claim write permission")
    raw_entries = record.get("entries")
    if type(raw_entries) is not list or not raw_entries:
        raise BetdaqOddsLadderAcquisitionError("record entries are invalid")
    entries: list[BetdaqOddsLadderEntry] = []
    seen: set[Decimal] = set()
    for raw in raw_entries:
        if type(raw) is not dict or set(raw) != {"price_text", "representation"}:
            raise BetdaqOddsLadderAcquisitionError("record entry shape is invalid")
        price_text = raw["price_text"]
        representation = raw["representation"]
        if type(price_text) is not str or price_text != price_text.strip():
            raise BetdaqOddsLadderAcquisitionError("record price text is invalid")
        try:
            price = Decimal(price_text)
        except InvalidOperation:
            raise BetdaqOddsLadderAcquisitionError("record price is invalid") from None
        _price(price)
        _text(representation, "representation")
        if price in seen:
            raise BetdaqOddsLadderAcquisitionError("record has duplicate price")
        seen.add(price)
        entries.append(BetdaqOddsLadderEntry(price, price_text, representation))

    content_sha = record.get("content_sha256")
    _sha(content_sha, "content_sha256")
    if _content_sha(tuple(entries)) != content_sha:
        raise BetdaqOddsLadderAcquisitionError("record content_sha256 mismatch")

    try:
        return BetdaqOddsLadderObservation(
            acquisition_id=record["acquisition_id"],
            observed_at=record["observed_at"],
            price_format=record["price_format"],
            request_fingerprint_sha256=record["request_fingerprint_sha256"],
            response_sha256=record["response_sha256"],
            response_byte_count=record["response_byte_count"],
            content_sha256=content_sha,
            entries=tuple(entries),
            provider_message_created_at=record["provider_message_created_at"],
            call_id=record["call_id"],
            evidence_sha256=record["evidence_sha256"],
            _origin_witness=object(),
            _clock_witness=object(),
        )
    except (KeyError, TypeError):
        raise BetdaqOddsLadderAcquisitionError(
            "odds-ladder record fields are invalid"
        ) from None


def _canonical_transport(transport: object) -> bool:
    if type(transport) is not UrllibBetdaqSoapTransport:
        return False
    post = getattr(transport, "post", None)
    return (
        getattr(post, "__self__", None) is transport
        and getattr(post, "__func__", None) is _CANONICAL_POST
        and _account_readonly.urlopen is _CANONICAL_URLOPEN
    )


def _soap_request(
    credentials: BetdaqCredentials,
    request: BetdaqOddsLadderRequest,
) -> bytes:
    envelope = ET.Element(f"{{{SOAP11_NS}}}Envelope")
    header = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Header")
    ET.SubElement(
        header,
        f"{{{EXTERNAL_API_NS}}}ExternalApiHeader",
        {
            "version": credentials.version,
            "languageCode": credentials.language_code,
            "username": credentials.username,
            "password": credentials.password,
            "applicationIdentifier": credentials.application_identifier,
        },
    )
    body = ET.SubElement(envelope, f"{{{SOAP11_NS}}}Body")
    method = ET.SubElement(body, f"{{{EXTERNAL_API_NS}}}GetOddsLadder")
    ET.SubElement(
        method,
        f"{{{EXTERNAL_API_NS}}}getOddsLadderRequest",
        {"PriceFormat": str(request.price_format)},
    )
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _request_fingerprint(
    request: BetdaqOddsLadderRequest,
    credentials: BetdaqCredentials,
) -> str:
    # No username/password/application identifier is persisted or hashed here.
    return _json_sha(
        {
            "schema": _SCHEMA,
            "endpoint": BETDAQ_ODDS_LADDER_ENDPOINT,
            "soap_action": BETDAQ_ODDS_LADDER_SOAP_ACTION,
            "price_format": request.price_format,
            "api_version": credentials.version,
            "language_code": credentials.language_code,
        }
    )


def _evidence_sha(
    acquisition_id: str,
    observed_at: str,
    price_format: int,
    request_sha: str,
    response_sha: str,
    response_bytes: int,
    content_sha: str,
    provider_created_at: str | None,
    call_id: str | None,
) -> str:
    return _json_sha(
        {
            "schema": _SCHEMA,
            "endpoint": BETDAQ_ODDS_LADDER_ENDPOINT,
            "acquisition_id": acquisition_id,
            "observed_at": observed_at,
            "price_format": price_format,
            "request_fingerprint_sha256": request_sha,
            "response_sha256": response_sha,
            "response_byte_count": response_bytes,
            "content_sha256": content_sha,
            "provider_message_created_at": provider_created_at,
            "call_id": call_id,
        }
    )


def _content_sha(entries: tuple[BetdaqOddsLadderEntry, ...]) -> str:
    payload = json.dumps(
        [{"price": x.price_text, "representation": x.representation} for x in entries],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _json_sha(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _clock_text(clock: Clock) -> str:
    try:
        value = clock()
    except Exception:
        raise BetdaqOddsLadderAcquisitionError("observation clock failed") from None
    if type(value) is not str:
        raise BetdaqOddsLadderAcquisitionError("clock must return ISO-8601 text")
    return _time(value, "clock").isoformat().replace("+00:00", "Z")


def _time(value: str, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetdaqOddsLadderAcquisitionError(f"{field} must be trimmed ISO-8601")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        raise BetdaqOddsLadderAcquisitionError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqOddsLadderAcquisitionError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_acq_id(value: str) -> str:
    if type(value) is not str:
        raise BetdaqOddsLadderAcquisitionError("acquisition id must be text")
    raw = value.removeprefix(_ACQ_PREFIX)
    if len(raw) != 32 or any(c not in "0123456789abcdef" for c in raw):
        raise BetdaqOddsLadderAcquisitionError("acquisition id must be 128-bit hex")
    return _ACQ_PREFIX + raw


def _sha(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise BetdaqOddsLadderAcquisitionError(f"{field} must be SHA-256 hex")
    return value


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(not c.isprintable() for c in value)
    ):
        raise BetdaqOddsLadderAcquisitionError(f"{field} must be safe text")
    return value


def _price(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 1:
        raise BetdaqOddsLadderUseError(
            "ladder price must be finite Decimal odds greater than 1"
        )
    return value
