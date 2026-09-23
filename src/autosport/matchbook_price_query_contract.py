from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
ENDPOINT_FAMILY = "EVENTS"
ENDPOINT_TEMPLATE = (
    "/edge/rest/events/{event_id}/markets/{market_id}/"
    "runners/{runner_id}/prices"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "AUD", "CAD", "HKD"})
_INT32_MAX = (1 << 31) - 1
_INT64_MAX = (1 << 63) - 1
_MAX_DECIMAL_TEXT_CHARS = 512


class MatchbookPriceQueryError(ValueError):
    """Raised when Matchbook price-query provenance is ambiguous or malformed."""


class MatchbookExchangeType(StrEnum):
    BACK_LAY = "back-lay"
    BINARY = "binary"


class MatchbookOddsType(StrEnum):
    DECIMAL = "DECIMAL"
    US = "US"
    HK = "HK"
    MALAY = "MALAY"
    INDO = "INDO"
    PERCENT = "%"


class MatchbookPriceSide(StrEnum):
    BOTH = "both"
    BACK = "back"
    LAY = "lay"
    WIN = "win"
    LOSE = "lose"


class MatchbookPriceMode(StrEnum):
    EXPANDED = "expanded"
    AGGREGATED = "aggregated"


class MatchbookPriceRepresentation(StrEnum):
    EXPANDED_LEVELS = "EXPANDED_LEVELS"
    AGGREGATED_DISPLAY = "AGGREGATED_DISPLAY"


def _positive_int(value: int, name: str, *, maximum: int) -> int:
    if type(value) is not int or value <= 0:
        raise MatchbookPriceQueryError(f"{name} must be a positive integer")
    if value > maximum:
        raise MatchbookPriceQueryError(
            f"{name} exceeds the documented Matchbook integer domain"
        )
    return value


def _sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MatchbookPriceQueryError(f"{name} must be lowercase SHA-256 hex")
    return value


def _finite_non_negative_decimal(value: Decimal, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise MatchbookPriceQueryError(
            f"{name} must be a non-negative finite exact Decimal"
        )
    try:
        provider_double = float(value)
    except (OverflowError, ValueError) as exc:
        raise MatchbookPriceQueryError(
            f"{name} is outside the documented Matchbook double domain"
        ) from exc
    if (
        not math.isfinite(provider_double)
        or (value != 0 and provider_double == 0.0)
    ):
        raise MatchbookPriceQueryError(
            f"{name} is outside the documented Matchbook double domain"
        )
    _decimal_text(value)
    return value


def _decimal_text(value: Decimal) -> str:
    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    if not any(digits):
        return "0"

    while digits[-1] == 0:
        digits.pop()
        exponent += 1

    coefficient = "".join(str(digit) for digit in digits)
    sign_chars = 1 if sign else 0
    if exponent >= 0:
        projected_chars = sign_chars + len(coefficient) + exponent
        if projected_chars > _MAX_DECIMAL_TEXT_CHARS:
            raise MatchbookPriceQueryError(
                "serialized Decimal exceeds safety bound"
            )
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            projected_chars = sign_chars + len(coefficient) + 1
            if projected_chars > _MAX_DECIMAL_TEXT_CHARS:
                raise MatchbookPriceQueryError(
                    "serialized Decimal exceeds safety bound"
                )
            text = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            projected_chars = (
                sign_chars + 2 + (-point) + len(coefficient)
            )
            if projected_chars > _MAX_DECIMAL_TEXT_CHARS:
                raise MatchbookPriceQueryError(
                    "serialized Decimal exceeds safety bound"
                )
            text = f"0.{('0' * -point)}{coefficient}"

    return f"-{text}" if sign else text


def _utc_text(value: datetime, name: str) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MatchbookPriceQueryError(f"{name} must be timezone-aware")
    if value.utcoffset().total_seconds() != 0:
        raise MatchbookPriceQueryError(f"{name} must be UTC")
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MatchbookPriceQueryError(
            f"{name} must be an ISO-8601 UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookPriceQueryError(
            f"{name} must be an ISO-8601 UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchbookPriceQueryError(f"{name} must be timezone-aware")
    if parsed.utcoffset().total_seconds() != 0:
        raise MatchbookPriceQueryError(f"{name} must be UTC")
    return parsed.astimezone(timezone.utc)


def _digest(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MatchbookPriceQueryError(
            "price-query payload is not canonical JSON"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatchbookPriceQueryError(f"{name} must be a mapping")
    return value


def _exact_keys(
    raw: Mapping[str, Any], expected: set[str], name: str
) -> None:
    if set(raw) != expected:
        raise MatchbookPriceQueryError(f"{name} fields are not canonical")


def _require_canonical_false_fields(
    raw: Mapping[str, Any],
    fields: tuple[str, ...],
    name: str,
) -> None:
    for field in fields:
        if type(raw[field]) is not bool or raw[field] is not False:
            raise MatchbookPriceQueryError(
                f"{name}.{field} must be canonical false"
            )


@dataclass(frozen=True, slots=True)
class MatchbookPriceQueryContract:
    """Immutable semantics for one Matchbook Get Prices acquisition.

    Matchbook exposes defaults for several economically material query
    parameters. This contract requires every such semantic to be chosen
    explicitly by Autosport. It contains no transport, authentication,
    provider-origin, execution, or real-money authority.
    """

    event_id: int
    market_id: int
    runner_id: int
    exchange_type: MatchbookExchangeType
    odds_type: MatchbookOddsType
    currency: str
    side: MatchbookPriceSide
    depth: int
    price_mode: MatchbookPriceMode
    minimum_liquidity: Decimal
    exclude_mirrored_prices: bool

    def __post_init__(self) -> None:
        _positive_int(self.event_id, "event_id", maximum=_INT64_MAX)
        _positive_int(self.market_id, "market_id", maximum=_INT64_MAX)
        _positive_int(self.runner_id, "runner_id", maximum=_INT64_MAX)
        if not isinstance(self.exchange_type, MatchbookExchangeType):
            raise MatchbookPriceQueryError(
                "exchange_type must be MatchbookExchangeType"
            )
        if not isinstance(self.odds_type, MatchbookOddsType):
            raise MatchbookPriceQueryError(
                "odds_type must be MatchbookOddsType"
            )
        if not isinstance(self.side, MatchbookPriceSide):
            raise MatchbookPriceQueryError(
                "side must be MatchbookPriceSide"
            )
        if not isinstance(self.price_mode, MatchbookPriceMode):
            raise MatchbookPriceQueryError(
                "price_mode must be MatchbookPriceMode"
            )
        if (
            not isinstance(self.currency, str)
            or self.currency not in _SUPPORTED_CURRENCIES
        ):
            raise MatchbookPriceQueryError(
                "currency is not a supported explicit Matchbook currency"
            )
        _positive_int(self.depth, "depth", maximum=_INT32_MAX)
        _finite_non_negative_decimal(
            self.minimum_liquidity, "minimum_liquidity"
        )
        if type(self.exclude_mirrored_prices) is not bool:
            raise MatchbookPriceQueryError(
                "exclude_mirrored_prices must be bool"
            )

        back_lay_sides = {
            MatchbookPriceSide.BOTH,
            MatchbookPriceSide.BACK,
            MatchbookPriceSide.LAY,
        }
        binary_sides = {
            MatchbookPriceSide.BOTH,
            MatchbookPriceSide.WIN,
            MatchbookPriceSide.LOSE,
        }
        allowed = (
            back_lay_sides
            if self.exchange_type is MatchbookExchangeType.BACK_LAY
            else binary_sides
        )
        if self.side not in allowed:
            raise MatchbookPriceQueryError(
                "side is incompatible with the selected Matchbook exchange_type"
            )

    @property
    def endpoint_family(self) -> str:
        return ENDPOINT_FAMILY

    @property
    def request_path(self) -> str:
        return ENDPOINT_TEMPLATE.format(
            event_id=self.event_id,
            market_id=self.market_id,
            runner_id=self.runner_id,
        )

    @property
    def provider_price_representation(
        self,
    ) -> MatchbookPriceRepresentation:
        if self.price_mode is MatchbookPriceMode.EXPANDED:
            return MatchbookPriceRepresentation.EXPANDED_LEVELS
        return MatchbookPriceRepresentation.AGGREGATED_DISPLAY

    @property
    def provider_defaults_used(self) -> bool:
        return False

    @property
    def absence_beyond_depth_proven(self) -> bool:
        return False

    @property
    def omitted_side_absence_proven(self) -> bool:
        return False

    @property
    def execution_liquidity_reserved(self) -> bool:
        return False

    def query_params(self) -> tuple[tuple[str, str], ...]:
        """Return the exact provider query projection.

        Matchbook represents BOTH by omitting side. The product contract still
        carries BOTH explicitly, so provider omission cannot be mistaken for an
        unspecified caller default.
        """

        params: list[tuple[str, str]] = [
            ("exchange-type", self.exchange_type.value),
            ("odds-type", self.odds_type.value),
            ("depth", str(self.depth)),
            ("currency", self.currency),
            (
                "minimum-liquidity",
                _decimal_text(self.minimum_liquidity),
            ),
            ("price-mode", self.price_mode.value),
            (
                "exclude-mirrored-prices",
                "true" if self.exclude_mirrored_prices else "false",
            ),
        ]
        if self.side is not MatchbookPriceSide.BOTH:
            params.append(("side", self.side.value))
        return tuple(params)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "endpoint_family": ENDPOINT_FAMILY,
            "request_path": self.request_path,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "runner_id": self.runner_id,
            "exchange_type": self.exchange_type.value,
            "odds_type": self.odds_type.value,
            "currency": self.currency,
            "side_scope": self.side.value,
            "depth": self.depth,
            "price_mode": self.price_mode.value,
            "minimum_liquidity": _decimal_text(
                self.minimum_liquidity
            ),
            "exclude_mirrored_prices": self.exclude_mirrored_prices,
            "query_params": [
                list(item) for item in self.query_params()
            ],
            "provider_price_representation":
                self.provider_price_representation.value,
            "provider_defaults_used": False,
            "absence_beyond_depth_proven": False,
            "omitted_side_absence_proven": False,
            "execution_liquidity_reserved": False,
        }

    @property
    def contract_sha256(self) -> str:
        return _digest(self.payload())

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["contract_sha256"] = self.contract_sha256
        return raw

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "MatchbookPriceQueryContract":
        expected = {
            "schema_version",
            "endpoint_family",
            "request_path",
            "event_id",
            "market_id",
            "runner_id",
            "exchange_type",
            "odds_type",
            "currency",
            "side_scope",
            "depth",
            "price_mode",
            "minimum_liquidity",
            "exclude_mirrored_prices",
            "query_params",
            "provider_price_representation",
            "provider_defaults_used",
            "absence_beyond_depth_proven",
            "omitted_side_absence_proven",
            "execution_liquidity_reserved",
            "contract_sha256",
        }
        raw = _mapping(raw, "MatchbookPriceQueryContract")
        _exact_keys(
            raw, expected, "MatchbookPriceQueryContract"
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != SCHEMA_VERSION
            or raw["endpoint_family"] != ENDPOINT_FAMILY
        ):
            raise MatchbookPriceQueryError(
                "unsupported price-query contract schema"
            )
        _require_canonical_false_fields(
            raw,
            (
                "provider_defaults_used",
                "absence_beyond_depth_proven",
                "omitted_side_absence_proven",
                "execution_liquidity_reserved",
            ),
            "MatchbookPriceQueryContract",
        )
        minimum = raw["minimum_liquidity"]
        if not isinstance(minimum, str):
            raise MatchbookPriceQueryError(
                "minimum_liquidity must use exact serialized Decimal text"
            )
        try:
            minimum_decimal = Decimal(minimum)
        except Exception as exc:
            raise MatchbookPriceQueryError(
                "minimum_liquidity is not a Decimal"
            ) from exc
        item = cls(
            event_id=raw["event_id"],
            market_id=raw["market_id"],
            runner_id=raw["runner_id"],
            exchange_type=MatchbookExchangeType(
                raw["exchange_type"]
            ),
            odds_type=MatchbookOddsType(raw["odds_type"]),
            currency=raw["currency"],
            side=MatchbookPriceSide(raw["side_scope"]),
            depth=raw["depth"],
            price_mode=MatchbookPriceMode(raw["price_mode"]),
            minimum_liquidity=minimum_decimal,
            exclude_mirrored_prices=raw[
                "exclude_mirrored_prices"
            ],
        )
        if raw != item.to_dict():
            raise MatchbookPriceQueryError(
                "price-query contract payload is not canonical"
            )
        return item


@dataclass(frozen=True, slots=True)
class MatchbookPriceObservationEvidence:
    """Request/response provenance only; never provider or execution authority."""

    query: MatchbookPriceQueryContract
    observed_at: datetime
    raw_response_sha256: str

    def __post_init__(self) -> None:
        if type(self.query) is not MatchbookPriceQueryContract:
            raise MatchbookPriceQueryError(
                "observation requires an exact MatchbookPriceQueryContract"
            )
        _utc_text(self.observed_at, "observed_at")
        _sha256(
            self.raw_response_sha256, "raw_response_sha256"
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "query_contract_sha256": self.query.contract_sha256,
            "query": self.query.to_dict(),
            "observed_at": _utc_text(
                self.observed_at, "observed_at"
            ),
            "raw_response_sha256": self.raw_response_sha256,
            "provider_price_representation":
                self.query.provider_price_representation.value,
            "provider_origin_proven": False,
            "provider_authentication_proven": False,
            "absence_beyond_depth_proven": False,
            "omitted_side_absence_proven": False,
            "execution_liquidity_reserved": False,
            "grants_execution_authority": False,
            "grants_real_money_authority": False,
        }

    @property
    def evidence_id(self) -> str:
        return _digest(self.payload())

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["evidence_id"] = self.evidence_id
        return raw

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "MatchbookPriceObservationEvidence":
        expected = {
            "schema_version",
            "query_contract_sha256",
            "query",
            "observed_at",
            "raw_response_sha256",
            "provider_price_representation",
            "provider_origin_proven",
            "provider_authentication_proven",
            "absence_beyond_depth_proven",
            "omitted_side_absence_proven",
            "execution_liquidity_reserved",
            "grants_execution_authority",
            "grants_real_money_authority",
            "evidence_id",
        }
        raw = _mapping(
            raw, "MatchbookPriceObservationEvidence"
        )
        _exact_keys(
            raw, expected, "MatchbookPriceObservationEvidence"
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != SCHEMA_VERSION
        ):
            raise MatchbookPriceQueryError(
                "unsupported observation evidence schema"
            )
        _require_canonical_false_fields(
            raw,
            (
                "provider_origin_proven",
                "provider_authentication_proven",
                "absence_beyond_depth_proven",
                "omitted_side_absence_proven",
                "execution_liquidity_reserved",
                "grants_execution_authority",
                "grants_real_money_authority",
            ),
            "MatchbookPriceObservationEvidence",
        )
        query = MatchbookPriceQueryContract.from_dict(
            _mapping(raw["query"], "query")
        )
        if (
            raw["query_contract_sha256"]
            != query.contract_sha256
        ):
            raise MatchbookPriceQueryError(
                "observation/query digest mismatch"
            )
        item = cls(
            query=query,
            observed_at=_parse_utc(
                raw["observed_at"], "observed_at"
            ),
            raw_response_sha256=_sha256(
                raw["raw_response_sha256"],
                "raw_response_sha256",
            ),
        )
        if raw != item.to_dict():
            raise MatchbookPriceQueryError(
                "observation evidence payload is not canonical"
            )
        return item
