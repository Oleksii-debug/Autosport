"""Market-specific Betfair Exchange price-ladder admission authority.

This module reuses the canonical read-only Betfair JSON-RPC client. It does
not expose provider writes and does not claim liquidity, fill, funds,
execution, settlement, or real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
from hashlib import sha256
import json
from threading import Lock
from typing import Mapping
from weakref import ref

from .betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
)

_LIST_MARKET_CATALOGUE = "SportsAPING/v1.0/listMarketCatalogue"
_CONTRACT_VERSION = "1"
_SOURCE_KIND = "listMarketCatalogue:MARKET_DESCRIPTION"
_CANONICAL_RPC = BetfairReadOnlyClient._rpc

_CLASSIC_BANDS = (
    (Decimal("1.01"), Decimal("2.00"), Decimal("0.01")),
    (Decimal("2.00"), Decimal("3.00"), Decimal("0.02")),
    (Decimal("3.00"), Decimal("4.00"), Decimal("0.05")),
    (Decimal("4.00"), Decimal("6.00"), Decimal("0.10")),
    (Decimal("6.00"), Decimal("10.00"), Decimal("0.20")),
    (Decimal("10.00"), Decimal("20.00"), Decimal("0.50")),
    (Decimal("20.00"), Decimal("30.00"), Decimal("1")),
    (Decimal("30.00"), Decimal("50.00"), Decimal("2")),
    (Decimal("50.00"), Decimal("100.00"), Decimal("5")),
    (Decimal("100.00"), Decimal("1000.00"), Decimal("10")),
)


class BetfairPriceLadderError(BetfairReadOnlyError):
    """Raised when price-ladder evidence or admission cannot be trusted."""


class PriceLadderAdmissionState(str, Enum):
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"
    PRICE_LADDER_ADMISSIBLE = "PRICE_LADDER_ADMISSIBLE"
    PRICE_LADDER_INVALID = "PRICE_LADDER_INVALID"
    UNSUPPORTED_LADDER_SEMANTICS = "UNSUPPORTED_LADDER_SEMANTICS"


@dataclass(frozen=True, slots=True)
class BetfairLineRangeInfo:
    min_unit_value: Decimal
    max_unit_value: Decimal
    interval: Decimal
    market_unit: str

    def __post_init__(self) -> None:
        _finite_decimal(self.min_unit_value, "min_unit_value")
        _finite_decimal(self.max_unit_value, "max_unit_value")
        _finite_decimal(self.interval, "interval")
        if self.max_unit_value < self.min_unit_value:
            raise BetfairPriceLadderError(
                "line range max_unit_value precedes min_unit_value"
            )
        if self.interval <= 0:
            raise BetfairPriceLadderError("line range interval must be positive")
        _required_text(self.market_unit, "market_unit")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "min_unit_value": _decimal_text(self.min_unit_value),
            "max_unit_value": _decimal_text(self.max_unit_value),
            "interval": _decimal_text(self.interval),
            "market_unit": self.market_unit,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairPriceLadderObservation:
    market_id: str
    ladder_type: str | None
    line_range: BetfairLineRangeInfo | None
    observed_at: str
    source_payload_sha256: str
    request_scope_sha256: str
    evidence_sha256: str
    adapter_id: str = ADAPTER_ID
    adapter_version: str = ADAPTER_VERSION
    source_kind: str = _SOURCE_KIND

    def __post_init__(self) -> None:
        _required_text(self.market_id, "market_id")
        if self.ladder_type is not None:
            _provider_enum_text(self.ladder_type, "ladder_type")
        if self.line_range is not None and not isinstance(
            self.line_range, BetfairLineRangeInfo
        ):
            raise BetfairPriceLadderError("line_range must be canonical")
        _iso_timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")
        _sha256_hex(self.request_scope_sha256, "request_scope_sha256")
        _sha256_hex(self.evidence_sha256, "evidence_sha256")
        if self.adapter_id != ADAPTER_ID or self.adapter_version != ADAPTER_VERSION:
            raise BetfairPriceLadderError("Betfair adapter identity mismatch")
        if self.source_kind != _SOURCE_KIND:
            raise BetfairPriceLadderError("market-definition source kind mismatch")

    def _integrity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.betfair_price_ladder_observation",
            "schema_version": 1,
            "contract_version": _CONTRACT_VERSION,
            "provider": "betfair-exchange",
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_kind": self.source_kind,
            "market_id": self.market_id,
            "ladder_type": self.ladder_type,
            "line_range": (
                None
                if self.line_range is None
                else self.line_range.canonical_payload()
            ),
            "observed_at": self.observed_at,
            "source_payload_sha256": self.source_payload_sha256,
            "request_scope_sha256": self.request_scope_sha256,
        }

    def assert_authoritative(self) -> None:
        if self.evidence_sha256 != _canonical_sha256(self._integrity_payload()):
            raise BetfairPriceLadderError(
                "price-ladder observation digest mismatch"
            )

    def _authority_fingerprint(self) -> str:
        return sha256(
            repr(
                (
                    self.market_id,
                    self.ladder_type,
                    self.line_range,
                    self.observed_at,
                    self.source_payload_sha256,
                    self.request_scope_sha256,
                    self.evidence_sha256,
                    self.adapter_id,
                    self.adapter_version,
                    self.source_kind,
                )
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairPriceLadderAdmission:
    state: PriceLadderAdmissionState
    market_id: str
    ladder_type: str | None
    price: Decimal
    observation_sha256: str
    admission_sha256: str
    selection_id: int | None = None
    handicap: Decimal | None = None
    execution_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, PriceLadderAdmissionState):
            raise BetfairPriceLadderError(
                "state must be PriceLadderAdmissionState"
            )
        _required_text(self.market_id, "market_id")
        if self.ladder_type is not None:
            _provider_enum_text(self.ladder_type, "ladder_type")
        _finite_decimal(self.price, "price")
        _sha256_hex(self.observation_sha256, "observation_sha256")
        _sha256_hex(self.admission_sha256, "admission_sha256")
        if self.selection_id is not None:
            _positive_int(self.selection_id, "selection_id")
        if self.handicap is not None:
            _finite_decimal(self.handicap, "handicap")
        if self.execution_authorized:
            raise BetfairPriceLadderError(
                "price-ladder admission cannot authorize execution"
            )

    def _integrity_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.betfair_price_ladder_admission",
            "schema_version": 1,
            "contract_version": _CONTRACT_VERSION,
            "state": self.state.value,
            "market_id": self.market_id,
            "ladder_type": self.ladder_type,
            "price": _decimal_text(self.price),
            "observation_sha256": self.observation_sha256,
            "selection_id": self.selection_id,
            "handicap": (
                None
                if self.handicap is None
                else _decimal_text(self.handicap)
            ),
            "execution_authorized": False,
        }

    def assert_authoritative(self) -> None:
        if self.admission_sha256 != _canonical_sha256(self._integrity_payload()):
            raise BetfairPriceLadderError(
                "price-ladder admission digest mismatch"
            )

    def _authority_fingerprint(self) -> str:
        return sha256(
            repr(
                (
                    self.state,
                    self.market_id,
                    self.ladder_type,
                    self.price,
                    self.observation_sha256,
                    self.admission_sha256,
                    self.selection_id,
                    self.handicap,
                    self.execution_authorized,
                )
            ).encode("utf-8")
        ).hexdigest()


class BetfairPriceLadderAuthority:
    """Acquire exact market metadata and issue one-axis price-tick admissions."""

    def __init__(self, client: BetfairReadOnlyClient) -> None:
        if type(client) is not BetfairReadOnlyClient:
            raise BetfairPriceLadderError(
                "client must be exact canonical BetfairReadOnlyClient"
            )
        self._client = client

    def __repr__(self) -> str:
        return (
            "BetfairPriceLadderAuthority("
            f"adapter_id={ADAPTER_ID!r}, adapter_version={ADAPTER_VERSION!r})"
        )

    def acquire(self, market_id: str) -> BetfairPriceLadderObservation:
        market = _required_text(market_id, "market_id")
        params = {
            "filter": {"marketIds": [market]},
            "marketProjection": ["MARKET_DESCRIPTION"],
            "maxResults": 1,
        }
        response = _CANONICAL_RPC(
            self._client,
            _LIST_MARKET_CATALOGUE,
            params,
        )
        rows = response.result
        if not isinstance(rows, list):
            raise BetfairPriceLadderError(
                "listMarketCatalogue result must be a JSON array"
            )
        if len(rows) != 1:
            raise BetfairPriceLadderError(
                "exact market definition is unavailable from listMarketCatalogue"
            )
        row = _json_object(rows[0], "marketCatalogue[0]")
        returned_market = _provider_required_text(
            row,
            "marketId",
            "market_id",
        )
        if returned_market != market:
            raise BetfairPriceLadderError(
                "marketCatalogue returned a different market"
            )

        description_raw = row.get("description")
        ladder_type: str | None = None
        line_range: BetfairLineRangeInfo | None = None
        if description_raw is not None:
            description = _json_object(
                description_raw,
                "marketCatalogue[0].description",
            )
            ladder_raw = description.get("priceLadderDescription")
            if ladder_raw is not None:
                ladder = _json_object(
                    ladder_raw,
                    "marketCatalogue[0].description.priceLadderDescription",
                )
                ladder_type = _provider_required_text(
                    ladder,
                    "type",
                    "price_ladder_type",
                )
                _provider_enum_text(
                    ladder_type,
                    "price_ladder_type",
                )
            line_raw = description.get("lineRangeInfo")
            if line_raw is not None:
                line_range = _parse_line_range(line_raw)

        request_scope_sha256 = _canonical_sha256(
            {
                "schema": "autosport.betfair_price_ladder_request",
                "schema_version": 1,
                "contract_version": _CONTRACT_VERSION,
                "method": _LIST_MARKET_CATALOGUE,
                "params": params,
            }
        )
        payload = {
            "schema": "autosport.betfair_price_ladder_observation",
            "schema_version": 1,
            "contract_version": _CONTRACT_VERSION,
            "provider": "betfair-exchange",
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "source_kind": _SOURCE_KIND,
            "market_id": returned_market,
            "ladder_type": ladder_type,
            "line_range": (
                None
                if line_range is None
                else line_range.canonical_payload()
            ),
            "observed_at": response.evidence.observed_at,
            "source_payload_sha256": response.evidence.source_payload_sha256,
            "request_scope_sha256": request_scope_sha256,
        }
        return BetfairPriceLadderObservation(
            market_id=returned_market,
            ladder_type=ladder_type,
            line_range=line_range,
            observed_at=response.evidence.observed_at,
            source_payload_sha256=response.evidence.source_payload_sha256,
            request_scope_sha256=request_scope_sha256,
            evidence_sha256=_canonical_sha256(payload),
        )

    def resolve(
        self,
        *,
        observation: BetfairPriceLadderObservation,
        market_id: str,
        price: Decimal | str,
        selection_id: int | None = None,
        handicap: Decimal | str | None = None,
    ) -> BetfairPriceLadderAdmission:
        market = _required_text(market_id, "market_id")
        if observation.market_id != market:
            raise BetfairPriceLadderError(
                "price-ladder observation belongs to a different market"
            )
        exact_price = _input_decimal(price, "price")
        exact_handicap = (
            None
            if handicap is None
            else _input_decimal(handicap, "handicap")
        )
        if selection_id is not None:
            _positive_int(selection_id, "selection_id")

        if observation.ladder_type is None:
            state = PriceLadderAdmissionState.UNKNOWN_UNPROVEN
        elif observation.ladder_type == "CLASSIC":
            state = (
                PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
                if _classic_tick(exact_price)
                else PriceLadderAdmissionState.PRICE_LADDER_INVALID
            )
        elif observation.ladder_type == "FINEST":
            state = (
                PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
                if _finest_tick(exact_price)
                else PriceLadderAdmissionState.PRICE_LADDER_INVALID
            )
        else:
            # LINE_RANGE is intentionally not interpreted here. Current
            # canonical standard-LIMIT action semantics do not prove that the
            # submitted number is the provider line-position/handicap domain.
            state = (
                PriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
            )

        payload = {
            "schema": "autosport.betfair_price_ladder_admission",
            "schema_version": 1,
            "contract_version": _CONTRACT_VERSION,
            "state": state.value,
            "market_id": market,
            "ladder_type": observation.ladder_type,
            "price": _decimal_text(exact_price),
            "observation_sha256": observation.evidence_sha256,
            "selection_id": selection_id,
            "handicap": (
                None
                if exact_handicap is None
                else _decimal_text(exact_handicap)
            ),
            "execution_authorized": False,
        }
        return BetfairPriceLadderAdmission(
            state=state,
            market_id=market,
            ladder_type=observation.ladder_type,
            price=exact_price,
            observation_sha256=observation.evidence_sha256,
            admission_sha256=_canonical_sha256(payload),
            selection_id=selection_id,
            handicap=exact_handicap,
        )


def _parse_line_range(value: object) -> BetfairLineRangeInfo:
    raw = _json_object(
        value,
        "marketCatalogue[0].description.lineRangeInfo",
    )
    return BetfairLineRangeInfo(
        _provider_number(raw, "minUnitValue", "min_unit_value"),
        _provider_number(raw, "maxUnitValue", "max_unit_value"),
        _provider_number(raw, "interval", "interval"),
        _provider_required_text(raw, "marketUnit", "market_unit"),
    )


def _classic_tick(price: Decimal) -> bool:
    if price < Decimal("1.01") or price > Decimal("1000"):
        return False
    value = Fraction(price)
    for lower, upper, step in _CLASSIC_BANDS:
        if lower <= price <= upper:
            units = (value - Fraction(lower)) / Fraction(step)
            if units.denominator == 1:
                return True
    return False


def _finest_tick(price: Decimal) -> bool:
    if price < Decimal("1.01") or price > Decimal("1000"):
        return False
    units = (
        Fraction(price) - Fraction(Decimal("1.01"))
    ) / Fraction(Decimal("0.01"))
    return units.denominator == 1


def _json_object(
    value: object,
    field_name: str,
) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or any(not isinstance(key, str) for key in value)
    ):
        raise BetfairPriceLadderError(
            f"{field_name} must be a JSON object"
        )
    return value


def _provider_required_text(
    value: Mapping[str, object],
    key: str,
    field_name: str,
) -> str:
    if key not in value:
        raise BetfairPriceLadderError(
            f"{field_name} is missing from provider response"
        )
    return _required_text(value[key], field_name)


def _provider_number(
    value: Mapping[str, object],
    key: str,
    field_name: str,
) -> Decimal:
    if key not in value:
        raise BetfairPriceLadderError(
            f"{field_name} is missing from provider response"
        )
    raw = value[key]
    if isinstance(raw, Decimal):
        result = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        result = Decimal(raw)
    else:
        raise BetfairPriceLadderError(
            f"{field_name} must be provider JSON number without binary float"
        )
    return _finite_decimal(result, field_name)


def _provider_enum_text(
    value: object,
    field_name: str,
) -> str:
    text = _required_text(value, field_name)
    if not text.isascii() or text != text.upper():
        raise BetfairPriceLadderError(
            f"{field_name} must be uppercase ASCII provider enum text"
        )
    return text


def _required_text(
    value: object,
    field_name: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise BetfairPriceLadderError(
            f"{field_name} must be a non-empty trimmed string"
        )
    return value


def _positive_int(
    value: object,
    field_name: str,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise BetfairPriceLadderError(
            f"{field_name} must be a positive integer"
        )
    return value


def _finite_decimal(
    value: object,
    field_name: str,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetfairPriceLadderError(
            f"{field_name} must be a finite Decimal"
        )
    return value


def _input_decimal(
    value: object,
    field_name: str,
) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise BetfairPriceLadderError(
                f"{field_name} text must be non-empty and trimmed"
            )
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise BetfairPriceLadderError(
                f"{field_name} text is not an exact decimal"
            ) from None
    else:
        raise BetfairPriceLadderError(
            f"{field_name} must be Decimal or exact decimal text"
        )
    return _finite_decimal(result, field_name)


def _decimal_text(value: Decimal) -> str:
    _finite_decimal(value, "decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _iso_timestamp(
    value: object,
    field_name: str,
) -> str:
    from datetime import datetime

    text = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise BetfairPriceLadderError(
            f"{field_name} must be ISO-8601"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairPriceLadderError(
            f"{field_name} must include timezone offset"
        )
    return text


def _sha256_hex(
    value: object,
    field_name: str,
) -> str:
    text = _required_text(value, field_name)
    if (
        len(text) != 64
        or any(
            character not in "0123456789abcdef"
            for character in text
        )
    ):
        raise BetfairPriceLadderError(
            f"{field_name} must be lowercase 64-character SHA-256"
        )
    return text


def _canonical_sha256(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairPriceLadderError(
            "price-ladder evidence is not canonical JSON"
        ) from exc
    return sha256(raw).hexdigest()


def _install_price_ladder_authority() -> None:
    issued_observations: dict[
        int,
        tuple[object, str, object, str],
    ] = {}
    latest_by_market: dict[
        tuple[int, str],
        object,
    ] = {}
    issued_admissions: dict[
        int,
        tuple[object, str, object, object],
    ] = {}
    issuance_lock = Lock()

    raw_acquire = BetfairPriceLadderAuthority.acquire
    raw_resolve = BetfairPriceLadderAuthority.resolve
    validate_observation = (
        BetfairPriceLadderObservation.assert_authoritative
    )
    validate_admission = (
        BetfairPriceLadderAdmission.assert_authoritative
    )

    def authoritative_acquire(
        self: BetfairPriceLadderAuthority,
        market_id: str,
    ) -> BetfairPriceLadderObservation:
        market = _required_text(market_id, "market_id")
        # A refresh attempt is itself evidence that the caller needs a current
        # definition. Revoke the prior current witness before provider I/O so
        # any failure leaves historical evidence auditable but non-consumable.
        with issuance_lock:
            latest_by_market.pop((id(self), market), None)

        observation = raw_acquire(self, market)
        observation_id = id(observation)

        def forget_observation(
            _weakref: object,
            *,
            key: int = observation_id,
        ) -> None:
            with issuance_lock:
                issued_observations.pop(key, None)

        observation_ref = ref(
            observation,
            forget_observation,
        )
        authority_ref = ref(self)
        with issuance_lock:
            issued_observations[observation_id] = (
                observation_ref,
                observation._authority_fingerprint(),
                authority_ref,
                observation.market_id,
            )
            latest_by_market[
                (id(self), observation.market_id)
            ] = observation_ref
        return observation

    def assert_observation(
        self: BetfairPriceLadderObservation,
    ) -> None:
        validate_observation(self)
        with issuance_lock:
            record = issued_observations.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairPriceLadderError(
                "price-ladder observation was not issued "
                "by canonical authority"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairPriceLadderError(
                "price-ladder observation changed "
                "after canonical acquisition"
            )

    def authoritative_resolve(
        self: BetfairPriceLadderAuthority,
        *,
        observation: BetfairPriceLadderObservation,
        market_id: str,
        price: Decimal | str,
        selection_id: int | None = None,
        handicap: Decimal | str | None = None,
    ) -> BetfairPriceLadderAdmission:
        if type(observation) is not BetfairPriceLadderObservation:
            raise BetfairPriceLadderError(
                "observation must be exact canonical "
                "BetfairPriceLadderObservation"
            )
        observation.assert_authoritative()
        with issuance_lock:
            record = issued_observations.get(id(observation))
            latest = latest_by_market.get(
                (id(self), observation.market_id)
            )
        if (
            record is None
            or record[2]() is not self
            or latest is None
            or latest() is not observation
        ):
            raise BetfairPriceLadderError(
                "price-ladder observation is not the current "
                "acquisition for this authority"
            )

        admission = raw_resolve(
            self,
            observation=observation,
            market_id=market_id,
            price=price,
            selection_id=selection_id,
            handicap=handicap,
        )
        admission_id = id(admission)

        def forget_admission(
            _weakref: object,
            *,
            key: int = admission_id,
        ) -> None:
            with issuance_lock:
                issued_admissions.pop(key, None)

        with issuance_lock:
            issued_admissions[admission_id] = (
                ref(admission, forget_admission),
                admission._authority_fingerprint(),
                ref(self),
                ref(observation),
            )
        return admission

    def assert_admission(
        self: BetfairPriceLadderAdmission,
    ) -> None:
        validate_admission(self)
        with issuance_lock:
            record = issued_admissions.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairPriceLadderError(
                "price-ladder admission was not issued "
                "by canonical authority"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairPriceLadderError(
                "price-ladder admission changed "
                "after canonical resolution"
            )
        authority = record[2]()
        observation = record[3]()
        if authority is None or observation is None:
            raise BetfairPriceLadderError(
                "price-ladder admission authority is no longer live"
            )
        observation.assert_authoritative()
        with issuance_lock:
            latest = latest_by_market.get(
                (id(authority), observation.market_id)
            )
        if latest is None or latest() is not observation:
            raise BetfairPriceLadderError(
                "price-ladder admission was superseded "
                "by a newer market definition"
            )

    BetfairPriceLadderAuthority.acquire = authoritative_acquire
    BetfairPriceLadderAuthority.resolve = authoritative_resolve
    BetfairPriceLadderObservation.assert_authoritative = (
        assert_observation
    )
    BetfairPriceLadderAdmission.assert_authoritative = (
        assert_admission
    )


_install_price_ladder_authority()
del _install_price_ladder_authority
