from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping


_ORDER_TYPE = "LIMIT"
_SIDE = "BACK"
_PERSISTENCE = "LAPSE"
_TIME_IN_FORCE = "FILL_OR_KILL"
_HEX = frozenset("0123456789abcdef")
_MAX_FIXED_POINT_TEXT = 512
_MAX_TEXT_INPUT_CHARS = 512
_MAX_DECIMAL_INPUT_MAGNITUDE = 10 ** _MAX_FIXED_POINT_TEXT
_MAX_BETFAIR_SELECTION_ID = "9223372036854775807"
_BETFAIR_CLASSIC_MIN_ODDS = Decimal("1.01")
_BETFAIR_CLASSIC_MAX_ODDS = Decimal("1000")
_BETFAIR_CLASSIC_TICK_CENTS = (
    (200, 1),
    (300, 2),
    (400, 5),
    (600, 10),
    (1000, 20),
    (2000, 50),
    (3000, 100),
    (5000, 200),
    (10000, 500),
    (100000, 1000),
)


class BetfairFillOrKillError(RuntimeError):
    """Raised when bounded Betfair FILL_OR_KILL semantics are not proven structurally."""


class FillOrKillStructuralOutcome(str, Enum):
    KILLED_ZERO = "KILLED_ZERO"
    FULLY_MATCHED = "FULLY_MATCHED"
    MINIMUM_MATCHED_REMAINDER_CANCELLED = "MINIMUM_MATCHED_REMAINDER_CANCELLED"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise BetfairFillOrKillError(f"{name} must be non-empty canonical text")
    if len(value) > _MAX_TEXT_INPUT_CHARS:
        raise BetfairFillOrKillError(
            f"{name} text input exceeds resource bound"
        )
    if value != value.strip() or "\x00" in value:
        raise BetfairFillOrKillError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BetfairFillOrKillError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name).lower()
    if len(raw) != 64 or any(ch not in _HEX for ch in raw):
        raise BetfairFillOrKillError(f"{name} must be canonical SHA-256 hex")
    return raw


def _fixed_point_materialization_length(value: Decimal) -> int:
    """Conservative preflight for format(value, "f") without materializing it."""

    if type(value) is not Decimal or not value.is_finite():
        raise BetfairFillOrKillError(
            "fixed-point length preflight requires exact finite Decimal"
        )
    sign, digits, exponent = value.as_tuple()
    digit_count = len(digits)
    sign_length = 1 if sign else 0
    if exponent >= 0:
        return sign_length + digit_count + exponent
    integer_digits = digit_count + exponent
    if integer_digits > 0:
        return sign_length + digit_count + 1
    return sign_length + 2 + (-integer_digits) + digit_count


def _require_fixed_point_bound(value: Decimal, name: str) -> Decimal:
    if _fixed_point_materialization_length(value) > _MAX_FIXED_POINT_TEXT:
        raise BetfairFillOrKillError(
            f"{name} fixed-point representation exceeds resource bound"
        )
    return value


def _preflight_decimal_input(value: object, name: str) -> None:
    if type(value) is str:
        if len(value) > _MAX_FIXED_POINT_TEXT:
            raise BetfairFillOrKillError(
                f"{name} decimal input exceeds resource bound"
            )
        return
    if type(value) is int and (
        value >= _MAX_DECIMAL_INPUT_MAGNITUDE
        or value <= -_MAX_DECIMAL_INPUT_MAGNITUDE
    ):
        raise BetfairFillOrKillError(
            f"{name} integer input exceeds resource bound"
        )


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise BetfairFillOrKillError(f"{name} must not use bool/float coercion")
    if type(value) not in {Decimal, str, int}:
        raise BetfairFillOrKillError(
            f"{name} must be exact Decimal, canonical decimal text, or int"
        )
    if type(value) is str and (not value or value != value.strip()):
        raise BetfairFillOrKillError(f"{name} must be canonical decimal text")
    _preflight_decimal_input(value, name)
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BetfairFillOrKillError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BetfairFillOrKillError(f"{name} must be finite and > 0")
    return _require_fixed_point_bound(parsed, name)


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise BetfairFillOrKillError(f"{name} must not use bool/float coercion")
    if type(value) not in {Decimal, str, int}:
        raise BetfairFillOrKillError(
            f"{name} must be exact Decimal, canonical decimal text, or int"
        )
    if type(value) is str and (not value or value != value.strip()):
        raise BetfairFillOrKillError(f"{name} must be canonical decimal text")
    _preflight_decimal_input(value, name)
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BetfairFillOrKillError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise BetfairFillOrKillError(f"{name} must be finite and >= 0")
    return _require_fixed_point_bound(parsed, name)


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairFillOrKillError("internal decimal must be exact finite Decimal")
    _require_fixed_point_bound(value, "internal decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _decimal_digits_to_int(digits: tuple[int, ...]) -> int:
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return coefficient


def _exact_cents(value: Decimal) -> int | None:
    """Return exact hundredths when representable without Decimal context arithmetic."""

    sign, digits, exponent = value.as_tuple()
    if sign or type(exponent) is not int:
        return None
    coefficient = _decimal_digits_to_int(digits)
    if exponent >= -2:
        return coefficient * (10 ** (exponent + 2))
    divisor = 10 ** (-exponent - 2)
    if coefficient % divisor:
        return None
    return coefficient // divisor


def _betfair_classic_odds_price(value: Decimal) -> Decimal:
    """Require one exact price on Betfair's documented odds-market tick ladder."""

    if value < _BETFAIR_CLASSIC_MIN_ODDS or value > _BETFAIR_CLASSIC_MAX_ODDS:
        raise BetfairFillOrKillError(
            "limit_price must be on the Betfair Classic odds ladder from 1.01 to 1000"
        )
    cents = _exact_cents(value)
    if cents is None:
        raise BetfairFillOrKillError(
            "limit_price must be on the Betfair Classic odds ladder"
        )
    for upper_cents, tick_cents in _BETFAIR_CLASSIC_TICK_CENTS:
        if cents <= upper_cents:
            if cents % tick_cents:
                raise BetfairFillOrKillError(
                    "limit_price must be on the Betfair Classic odds ladder"
                )
            return value
    raise BetfairFillOrKillError(
        "limit_price must be on the Betfair Classic odds ladder"
    )


def _exact_nonnegative_difference(total: Decimal, part: Decimal) -> Decimal:
    """Subtract two bounded nonnegative Decimals without ambient-context rounding."""

    if (
        type(total) is not Decimal
        or type(part) is not Decimal
        or not total.is_finite()
        or not part.is_finite()
        or total < 0
        or part < 0
        or part > total
    ):
        raise BetfairFillOrKillError(
            "exact FOK remainder requires finite nonnegative Decimal operands"
        )
    if part.is_zero():
        return total
    if part == total:
        return Decimal("0")

    total_tuple = total.as_tuple()
    part_tuple = part.as_tuple()
    if type(total_tuple.exponent) is not int or type(part_tuple.exponent) is not int:
        raise BetfairFillOrKillError(
            "exact FOK remainder requires integer Decimal exponents"
        )
    common_exponent = min(total_tuple.exponent, part_tuple.exponent)
    total_coefficient = _decimal_digits_to_int(total_tuple.digits) * (
        10 ** (total_tuple.exponent - common_exponent)
    )
    part_coefficient = _decimal_digits_to_int(part_tuple.digits) * (
        10 ** (part_tuple.exponent - common_exponent)
    )
    remainder_coefficient = total_coefficient - part_coefficient
    if remainder_coefficient <= 0:
        raise BetfairFillOrKillError(
            "exact FOK remainder arithmetic produced a nonpositive remainder"
        )
    remainder_digits = tuple(
        int(character) for character in str(remainder_coefficient)
    )
    return Decimal((0, remainder_digits, common_exponent))


def _positive_selection(value: object) -> str:
    if type(value) is str and len(value) > len(_MAX_BETFAIR_SELECTION_ID):
        raise BetfairFillOrKillError(
            "selection_id exceeds Betfair signed-long domain"
        )
    raw = _text(value, "selection_id")
    if not raw.isascii() or not raw.isdigit() or raw.startswith("0"):
        raise BetfairFillOrKillError(
            "selection_id must be canonical positive integer text"
        )
    if (
        len(raw) > len(_MAX_BETFAIR_SELECTION_ID)
        or (
            len(raw) == len(_MAX_BETFAIR_SELECTION_ID)
            and raw > _MAX_BETFAIR_SELECTION_ID
        )
    ):
        raise BetfairFillOrKillError(
            "selection_id exceeds Betfair signed-long domain"
        )
    return raw


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairFillOrKillError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairFillOrKillRequest:
    """A bounded BACK/LIMIT/FOK request projection, with no provider-write capability."""

    market_id: str
    selection_id: str
    requested_size: Decimal | str | int
    limit_price: Decimal | str | int
    min_fill_size: Decimal | str | int | None = None
    side: str = _SIDE
    order_type: str = _ORDER_TYPE
    persistence_type: str = _PERSISTENCE
    time_in_force: str = _TIME_IN_FORCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", _text(self.market_id, "market_id"))
        object.__setattr__(self, "selection_id", _positive_selection(self.selection_id))
        if self.side != _SIDE:
            raise BetfairFillOrKillError(
                "bounded FOK contract currently supports BACK only"
            )
        if self.order_type != _ORDER_TYPE:
            raise BetfairFillOrKillError("FOK request must use LIMIT order_type")
        if self.persistence_type != _PERSISTENCE:
            raise BetfairFillOrKillError(
                "bounded FOK contract requires LAPSE persistence"
            )
        if self.time_in_force != _TIME_IN_FORCE:
            raise BetfairFillOrKillError(
                "FOK request must use time_in_force=FILL_OR_KILL"
            )

        size = _positive_decimal(self.requested_size, "requested_size")
        price = _betfair_classic_odds_price(
            _positive_decimal(self.limit_price, "limit_price")
        )
        object.__setattr__(self, "requested_size", size)
        object.__setattr__(self, "limit_price", price)

        if self.min_fill_size is not None:
            minimum = _positive_decimal(self.min_fill_size, "min_fill_size")
            if minimum > size:
                raise BetfairFillOrKillError(
                    "min_fill_size cannot exceed requested_size"
                )
            object.__setattr__(self, "min_fill_size", minimum)

    @property
    def provider_instruction(self) -> Mapping[str, object]:
        limit_order: dict[str, object] = {
            "size": _decimal_text(self.requested_size),
            "price": _decimal_text(self.limit_price),
            "persistenceType": _PERSISTENCE,
            "timeInForce": _TIME_IN_FORCE,
        }
        if self.min_fill_size is not None:
            limit_order["minFillSize"] = _decimal_text(self.min_fill_size)
        return {
            "selectionId": int(self.selection_id),
            "handicap": 0,
            "side": _SIDE,
            "orderType": _ORDER_TYPE,
            "limitOrder": limit_order,
        }

    @property
    def request_projection_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_fill_or_kill_request",
                "schema_version": 1,
                "marketId": self.market_id,
                "instruction": self.provider_instruction,
            }
        )


@dataclass(frozen=True, slots=True)
class BetfairFillOrKillImmediateReport:
    """Exact immediate response assertions; provider origin is not implied."""

    request_projection_sha256: str
    response_sha256: str
    top_status: str
    instruction_status: str
    size_matched: Decimal | str | int
    average_price_matched: Decimal | str | int
    bet_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_projection_sha256",
            _sha256(self.request_projection_sha256, "request_projection_sha256"),
        )
        object.__setattr__(
            self,
            "response_sha256",
            _sha256(self.response_sha256, "response_sha256"),
        )
        if self.top_status != "SUCCESS" or self.instruction_status != "SUCCESS":
            raise BetfairFillOrKillError(
                "bounded FOK structural classification requires exact SUCCESS/SUCCESS report"
            )
        matched = _nonnegative_decimal(self.size_matched, "size_matched")
        vwap = _nonnegative_decimal(
            self.average_price_matched,
            "average_price_matched",
        )
        if matched == 0 and vwap != 0:
            raise BetfairFillOrKillError(
                "zero matched size cannot claim a positive matched VWAP"
            )
        if matched > 0 and vwap <= 0:
            raise BetfairFillOrKillError(
                "positive matched size requires positive matched VWAP"
            )
        object.__setattr__(self, "size_matched", matched)
        object.__setattr__(self, "average_price_matched", vwap)
        if self.bet_id is not None:
            object.__setattr__(self, "bet_id", _text(self.bet_id, "bet_id"))


@dataclass(frozen=True, slots=True, init=False)
class BetfairFillOrKillStructuralEvidence:
    """Structural FOK semantics only; this object grants no execution authority."""

    request_projection_sha256: str
    response_sha256: str
    outcome: FillOrKillStructuralOutcome
    requested_size: Decimal
    min_fill_size: Decimal | None
    size_matched: Decimal
    matched_vwap: Decimal
    unmatched_remainder: Decimal
    unmatched_remainder_terminal_by_fok_contract: bool
    aggregate_vwap_limit_satisfied: bool | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise BetfairFillOrKillError(
            "structural FOK evidence must be produced by lifecycle inspection"
        )

    @property
    def per_fragment_price_floor_proven(self) -> bool:
        return False

    @property
    def provider_origin_verified(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_fill_or_kill_structural_evidence",
                "schema_version": 1,
                "request_projection_sha256": self.request_projection_sha256,
                "response_sha256": self.response_sha256,
                "outcome": self.outcome.value,
                "requested_size": _decimal_text(self.requested_size),
                "min_fill_size": (
                    None
                    if self.min_fill_size is None
                    else _decimal_text(self.min_fill_size)
                ),
                "size_matched": _decimal_text(self.size_matched),
                "matched_vwap": _decimal_text(self.matched_vwap),
                "unmatched_remainder": _decimal_text(self.unmatched_remainder),
                "unmatched_remainder_terminal_by_fok_contract": (
                    self.unmatched_remainder_terminal_by_fok_contract
                ),
                "aggregate_vwap_limit_satisfied": self.aggregate_vwap_limit_satisfied,
                "per_fragment_price_floor_proven": self.per_fragment_price_floor_proven,
                "provider_origin_verified": self.provider_origin_verified,
                "grants_execution_authority": self.grants_execution_authority,
                "grants_real_money_authority": self.grants_real_money_authority,
            }
        )


def _make_structural_evidence(
    *,
    request_projection_sha256: str,
    response_sha256: str,
    outcome: FillOrKillStructuralOutcome,
    requested_size: Decimal,
    min_fill_size: Decimal | None,
    size_matched: Decimal,
    matched_vwap: Decimal,
    unmatched_remainder: Decimal,
    unmatched_remainder_terminal_by_fok_contract: bool,
    aggregate_vwap_limit_satisfied: bool | None,
) -> BetfairFillOrKillStructuralEvidence:
    evidence = object.__new__(BetfairFillOrKillStructuralEvidence)
    for name, value in (
        ("request_projection_sha256", request_projection_sha256),
        ("response_sha256", response_sha256),
        ("outcome", outcome),
        ("requested_size", requested_size),
        ("min_fill_size", min_fill_size),
        ("size_matched", size_matched),
        ("matched_vwap", matched_vwap),
        ("unmatched_remainder", unmatched_remainder),
        (
            "unmatched_remainder_terminal_by_fok_contract",
            unmatched_remainder_terminal_by_fok_contract,
        ),
        ("aggregate_vwap_limit_satisfied", aggregate_vwap_limit_satisfied),
    ):
        object.__setattr__(evidence, name, value)
    return evidence


def inspect_betfair_fill_or_kill_lifecycle(
    request: BetfairFillOrKillRequest,
    report: BetfairFillOrKillImmediateReport,
) -> BetfairFillOrKillStructuralEvidence:
    """Classify exact documented FOK response semantics without minting provider authority."""

    if type(request) is not BetfairFillOrKillRequest:
        raise BetfairFillOrKillError(
            "request must be an exact BetfairFillOrKillRequest"
        )
    if type(report) is not BetfairFillOrKillImmediateReport:
        raise BetfairFillOrKillError(
            "report must be an exact BetfairFillOrKillImmediateReport"
        )
    if report.request_projection_sha256 != request.request_projection_sha256:
        raise BetfairFillOrKillError(
            "immediate report does not bind the exact FOK request projection"
        )
    if report.size_matched > request.requested_size:
        raise BetfairFillOrKillError(
            "provider report cannot match more than requested_size"
        )

    if report.size_matched == 0:
        outcome = FillOrKillStructuralOutcome.KILLED_ZERO
        # No matched volume means there is no matched VWAP to compare with the
        # FOK price floor.  Preserve this as not-applicable/unknown rather than
        # minting positive price-quality evidence from a lapsed zero-fill order.
        aggregate_vwap_satisfied = None
    else:
        if report.average_price_matched < request.limit_price:
            raise BetfairFillOrKillError(
                "matched FOK VWAP is below the BACK limit-price floor"
            )
        aggregate_vwap_satisfied = True
        if request.min_fill_size is None:
            if report.size_matched != request.requested_size:
                raise BetfairFillOrKillError(
                    "FOK without min_fill_size must fully match or match zero"
                )
            outcome = FillOrKillStructuralOutcome.FULLY_MATCHED
        else:
            if report.size_matched < request.min_fill_size:
                raise BetfairFillOrKillError(
                    "positive FOK match is below the declared min_fill_size"
                )
            outcome = (
                FillOrKillStructuralOutcome.FULLY_MATCHED
                if report.size_matched == request.requested_size
                else FillOrKillStructuralOutcome.MINIMUM_MATCHED_REMAINDER_CANCELLED
            )

    remainder = _exact_nonnegative_difference(
        request.requested_size,
        report.size_matched,
    )
    return _make_structural_evidence(
        request_projection_sha256=request.request_projection_sha256,
        response_sha256=report.response_sha256,
        outcome=outcome,
        requested_size=request.requested_size,
        min_fill_size=request.min_fill_size,
        size_matched=report.size_matched,
        matched_vwap=report.average_price_matched,
        unmatched_remainder=remainder,
        unmatched_remainder_terminal_by_fok_contract=True,
        aggregate_vwap_limit_satisfied=aggregate_vwap_satisfied,
    )


def resolve_betfair_fill_or_kill_lifecycle(
    request: BetfairFillOrKillRequest,
    report: BetfairFillOrKillImmediateReport,
) -> BetfairFillOrKillStructuralEvidence:
    """Fail closed until canonical product request/provider-response origin is composed."""

    inspect_betfair_fill_or_kill_lifecycle(request, report)
    raise BetfairFillOrKillError(
        "FOK structural assertions lack canonical product-issued request and provider-origin authority"
    )
