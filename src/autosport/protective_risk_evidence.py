from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    ROUND_HALF_EVEN,
    Rounded,
    localcontext,
)
from enum import StrEnum
import hashlib
from itertools import islice
import json
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
METHOD = "EXTERNAL_FLOW_NEUTRALIZED_EQUITY_V1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# G13 is evidence-only, but the evidence itself is scientific/financial truth and
# therefore must be reproducible under a declared finite resource domain. Event
# money is deliberately much narrower than Decimal's implementation limits. With
# <=10k events, <=48 coefficient digits and exponents in [-18, 18], aligning every
# value to the smallest allowed scale and summing every flow needs <90 significant
# digits. The 128-digit exact-money context therefore has explicit headroom; any
# unexpected rounding is trapped rather than silently becoming evidence.
_MAX_EVENTS = 10_000
_MAX_EVENT_DECIMAL_DIGITS = 48
_MIN_EVENT_DECIMAL_EXPONENT = -18
_MAX_EVENT_DECIMAL_EXPONENT = 18
_MAX_EVENT_DECIMAL_TEXT_CHARS = 80
_MAX_DERIVED_DECIMAL_TEXT_CHARS = 192
_MONEY_CONTEXT = Context(
    prec=128,
    rounding=ROUND_HALF_EVEN,
    Emin=-256,
    Emax=256,
    capitals=1,
    clamp=0,
)
# Percentage evidence is intentionally rounded only here, under one canonical
# context. Money add/sub never uses this rounding context.
_DECIMAL_CONTEXT = Context(
    prec=60,
    rounding=ROUND_HALF_EVEN,
    Emin=-256,
    Emax=256,
    capitals=1,
    clamp=0,
)


class ProtectiveRiskEvidenceError(ValueError):
    """Fail-closed error for malformed or causally ambiguous G13 evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RiskEvidenceEventKind(StrEnum):
    VALUATION = "VALUATION"
    CAPITAL_FLOW = "CAPITAL_FLOW"


class PercentageEvidenceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TERMINATED_NONPOSITIVE_EQUITY = "TERMINATED_NONPOSITIVE_EQUITY"


def _error(code: str, message: str) -> ProtectiveRiskEvidenceError:
    return ProtectiveRiskEvidenceError(code, message)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise _error("INVALID_EVENT", f"{name} must be a non-empty trimmed string")
    if "\x00" in value:
        raise _error("INVALID_EVENT", f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _error("INVALID_EVENT", f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise _error("INVALID_EVENT", f"{name} must be lowercase SHA-256 hex")
    return text


def _currency(value: object) -> str:
    text = _text(value, "currency")
    if _CURRENCY_RE.fullmatch(text) is None:
        raise _error("INVALID_EVENT", "currency must be an uppercase three-letter code")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("INVALID_EVENT", f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error("INVALID_EVENT", f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _finite_decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    # Economic evidence must not dispatch validation/arithmetic through caller-defined
    # Decimal subclasses. A subclass can override is_finite/as_tuple/comparison or
    # copy_negate and otherwise forge the canonical money path while still passing
    # isinstance(value, Decimal).
    if type(value) is not Decimal:
        raise _error("INVALID_EVENT", f"{name} must be an exact Decimal")
    if not value.is_finite():
        raise _error("INVALID_EVENT", f"{name} must be finite")
    if positive and value <= 0:
        raise _error("INVALID_EVENT", f"{name} must be positive")
    return value


def _fixed_decimal_text_length(value: Decimal) -> int:
    """Return fixed-point render length without rendering/expanding the Decimal."""

    finite = _finite_decimal(value, "decimal")
    if finite.is_zero():
        return 1
    sign, digits, exponent = finite.as_tuple()
    digit_count = max(1, len(digits))
    if not isinstance(exponent, int):
        raise _error("ARITHMETIC_UNREPRESENTABLE", "Decimal exponent is not finite")
    if exponent >= 0:
        body = digit_count + exponent
    elif digit_count + exponent > 0:
        body = digit_count + 1  # decimal point inside the coefficient
    else:
        body = 2 - exponent  # '0.' + leading fractional zeroes + coefficient
    return body + int(bool(sign))


def _event_money_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
) -> Decimal:
    decimal_value = _finite_decimal(value, name, positive=positive)
    _, digits, exponent = decimal_value.as_tuple()
    if not isinstance(exponent, int):
        raise _error(
            "ARITHMETIC_UNREPRESENTABLE",
            f"{name} Decimal exponent is outside the supported event-money domain",
        )
    if (
        len(digits) > _MAX_EVENT_DECIMAL_DIGITS
        or exponent < _MIN_EVENT_DECIMAL_EXPONENT
        or exponent > _MAX_EVENT_DECIMAL_EXPONENT
        or _fixed_decimal_text_length(decimal_value) > _MAX_EVENT_DECIMAL_TEXT_CHARS
    ):
        raise _error(
            "ARITHMETIC_UNREPRESENTABLE",
            f"{name} exceeds the supported event-money Decimal shape",
        )
    return decimal_value


def _decimal_text(value: Decimal) -> str:
    finite = _finite_decimal(value, "decimal")
    if finite.is_zero():
        return "0"
    if _fixed_decimal_text_length(finite) > _MAX_DERIVED_DECIMAL_TEXT_CHARS:
        raise _error(
            "ARITHMETIC_UNREPRESENTABLE",
            "derived Decimal exceeds canonical serialization bounds",
        )
    text = format(finite, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def _parse_decimal(value: object, name: str) -> Decimal:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_EVENT_DECIMAL_TEXT_CHARS
    ):
        raise _error("EVIDENCE_INTEGRITY", f"{name} must be a bounded canonical decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise _error("EVIDENCE_INTEGRITY", f"{name} is not a Decimal") from exc
    try:
        _event_money_decimal(parsed, name)
    except ProtectiveRiskEvidenceError as exc:
        raise _error(
            "EVIDENCE_INTEGRITY",
            f"{name} exceeds the supported event-money Decimal shape",
        ) from exc
    if _decimal_text(parsed) != value:
        raise _error("EVIDENCE_INTEGRITY", f"{name} is not canonical finite Decimal text")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(raw: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(raw) != expected:
        raise _error("EVIDENCE_INTEGRITY", f"{name} keys do not match schema")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("EVIDENCE_INTEGRITY", f"{name} must be an object")
    return value


@dataclass(frozen=True, slots=True)
class ProtectiveRiskEvent:
    sequence: int
    event_id: str
    occurred_at: str
    kind: RiskEvidenceEventKind
    raw_equity: Decimal | None = None
    amount: Decimal | None = None
    source_scope_id: str | None = None
    destination_scope_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise _error("INVALID_EVENT", "sequence must be a positive integer")
        _text(self.event_id, "event_id")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        if type(self.kind) is not RiskEvidenceEventKind:
            raise _error("INVALID_EVENT", "kind must be RiskEvidenceEventKind")

        if self.kind is RiskEvidenceEventKind.VALUATION:
            if self.raw_equity is None:
                raise _error("INVALID_EVENT", "VALUATION requires raw_equity")
            _event_money_decimal(self.raw_equity, "raw_equity")
            if any(
                value is not None
                for value in (self.amount, self.source_scope_id, self.destination_scope_id)
            ):
                raise _error(
                    "INVALID_EVENT",
                    "VALUATION must not carry capital-flow fields",
                )
        else:
            if self.raw_equity is not None:
                raise _error("INVALID_EVENT", "CAPITAL_FLOW must not carry raw_equity")
            if self.amount is None:
                raise _error("INVALID_EVENT", "CAPITAL_FLOW requires amount")
            _event_money_decimal(self.amount, "amount", positive=True)
            if self.source_scope_id is None or self.destination_scope_id is None:
                raise _error(
                    "FLOW_SCOPE_MISMATCH",
                    "CAPITAL_FLOW requires source and destination scope identity",
                )
            _text(self.source_scope_id, "source_scope_id")
            _text(self.destination_scope_id, "destination_scope_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "kind": self.kind.value,
            "raw_equity": None if self.raw_equity is None else _decimal_text(self.raw_equity),
            "amount": None if self.amount is None else _decimal_text(self.amount),
            "source_scope_id": self.source_scope_id,
            "destination_scope_id": self.destination_scope_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProtectiveRiskEvent":
        _exact_keys(
            raw,
            {
                "sequence",
                "event_id",
                "occurred_at",
                "kind",
                "raw_equity",
                "amount",
                "source_scope_id",
                "destination_scope_id",
            },
            "ProtectiveRiskEvent",
        )
        if type(raw["sequence"]) is not int:
            raise _error("EVIDENCE_INTEGRITY", "event sequence must be an integer")
        try:
            kind = RiskEvidenceEventKind(_text(raw["kind"], "kind"))
        except ValueError as exc:
            raise _error("EVIDENCE_INTEGRITY", "unsupported event kind") from exc
        raw_equity = raw["raw_equity"]
        amount = raw["amount"]
        return cls(
            sequence=raw["sequence"],
            event_id=_text(raw["event_id"], "event_id"),
            occurred_at=_text(raw["occurred_at"], "occurred_at"),
            kind=kind,
            raw_equity=(
                None
                if raw_equity is None
                else _parse_decimal(raw_equity, "raw_equity")
            ),
            amount=None if amount is None else _parse_decimal(amount, "amount"),
            source_scope_id=(
                None
                if raw["source_scope_id"] is None
                else _text(raw["source_scope_id"], "source_scope_id")
            ),
            destination_scope_id=(
                None
                if raw["destination_scope_id"] is None
                else _text(raw["destination_scope_id"], "destination_scope_id")
            ),
        )


@dataclass(frozen=True, slots=True)
class FlowAdjustedEquityPoint:
    sequence: int
    event_id: str
    occurred_at: str
    raw_equity: Decimal
    cumulative_external_flow: Decimal
    flow_adjusted_equity: Decimal
    high_water_mark: Decimal
    drawdown_absolute: Decimal
    drawdown_fraction: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "raw_equity": _decimal_text(self.raw_equity),
            "cumulative_external_flow": _decimal_text(self.cumulative_external_flow),
            "flow_adjusted_equity": _decimal_text(self.flow_adjusted_equity),
            "high_water_mark": _decimal_text(self.high_water_mark),
            "drawdown_absolute": _decimal_text(self.drawdown_absolute),
            "drawdown_fraction": (
                None
                if self.drawdown_fraction is None
                else _decimal_text(self.drawdown_fraction)
            ),
        }


@dataclass(frozen=True, slots=True)
class ProtectiveRiskFlowEvidence:
    campaign_id: str
    protocol_sha256: str
    source_sha256: str
    capital_scope_id: str
    currency: str
    events: tuple[ProtectiveRiskEvent, ...]
    points: tuple[FlowAdjustedEquityPoint, ...]
    percentage_status: PercentageEvidenceStatus
    ruin_event_id: str | None
    cumulative_external_flow: Decimal
    maximum_drawdown_absolute: Decimal
    maximum_drawdown_fraction: Decimal | None
    minimum_flow_adjusted_equity: Decimal
    evidence_sha256: str

    @property
    def source_resolved(self) -> bool:
        return False

    @property
    def risk_policy_authority(self) -> bool:
        return False

    def payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "method": METHOD,
            "campaign_id": self.campaign_id,
            "protocol_sha256": self.protocol_sha256,
            "source_sha256": self.source_sha256,
            "capital_scope_id": self.capital_scope_id,
            "currency": self.currency,
            "events": [event.to_dict() for event in self.events],
            "points": [point.to_dict() for point in self.points],
            "percentage_status": self.percentage_status.value,
            "ruin_event_id": self.ruin_event_id,
            "cumulative_external_flow": _decimal_text(self.cumulative_external_flow),
            "maximum_drawdown_absolute": _decimal_text(self.maximum_drawdown_absolute),
            "maximum_drawdown_fraction": (
                None
                if self.maximum_drawdown_fraction is None
                else _decimal_text(self.maximum_drawdown_fraction)
            ),
            "minimum_flow_adjusted_equity": _decimal_text(
                self.minimum_flow_adjusted_equity
            ),
            "source_resolved": False,
            "risk_policy_authority": False,
        }

    def to_dict(self) -> dict[str, object]:
        raw = self.payload_without_digest()
        raw["evidence_sha256"] = self.evidence_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProtectiveRiskFlowEvidence":
        expected = {
            "schema_version",
            "method",
            "campaign_id",
            "protocol_sha256",
            "source_sha256",
            "capital_scope_id",
            "currency",
            "events",
            "points",
            "percentage_status",
            "ruin_event_id",
            "cumulative_external_flow",
            "maximum_drawdown_absolute",
            "maximum_drawdown_fraction",
            "minimum_flow_adjusted_equity",
            "source_resolved",
            "risk_policy_authority",
            "evidence_sha256",
        }
        _exact_keys(raw, expected, "ProtectiveRiskFlowEvidence")
        if raw["schema_version"] != SCHEMA_VERSION or raw["method"] != METHOD:
            raise _error("EVIDENCE_INTEGRITY", "unsupported protective-risk schema/method")
        if raw["source_resolved"] is not False or raw["risk_policy_authority"] is not False:
            raise _error(
                "EVIDENCE_INTEGRITY",
                "G13 flow evidence cannot carry source/risk-policy authority",
            )
        event_values = raw["events"]
        if type(event_values) is not list:
            raise _error("EVIDENCE_INTEGRITY", "events must be a list")
        if len(event_values) > _MAX_EVENTS:
            raise _error(
                "EVIDENCE_RESOURCE_LIMIT",
                "serialized event count exceeds the supported evidence domain",
            )
        rebuilt = build_protective_risk_flow_evidence(
            campaign_id=_text(raw["campaign_id"], "campaign_id"),
            protocol_sha256=_text(raw["protocol_sha256"], "protocol_sha256"),
            source_sha256=_text(raw["source_sha256"], "source_sha256"),
            capital_scope_id=_text(raw["capital_scope_id"], "capital_scope_id"),
            currency=_text(raw["currency"], "currency"),
            events=tuple(
                ProtectiveRiskEvent.from_dict(_mapping(item, "event"))
                for item in event_values
            ),
        )
        if rebuilt.to_dict() != dict(raw):
            raise _error(
                "EVIDENCE_INTEGRITY",
                "serialized protective-risk evidence does not match deterministic replay",
            )
        return rebuilt


def _classify_external_flow(
    event: ProtectiveRiskEvent,
    capital_scope_id: str,
) -> Decimal:
    assert event.kind is RiskEvidenceEventKind.CAPITAL_FLOW
    assert event.amount is not None
    source_inside = event.source_scope_id == capital_scope_id
    destination_inside = event.destination_scope_id == capital_scope_id
    if source_inside and destination_inside:
        return Decimal("0")
    if source_inside:
        return event.amount.copy_negate()
    if destination_inside:
        return event.amount
    raise _error(
        "FLOW_SCOPE_MISMATCH",
        "capital flow does not cross or remain within the declared capital scope",
    )


def build_protective_risk_flow_evidence(
    *,
    campaign_id: str,
    protocol_sha256: str,
    source_sha256: str,
    capital_scope_id: str,
    currency: str,
    events: Sequence[ProtectiveRiskEvent],
) -> ProtectiveRiskFlowEvidence:
    """Reconstruct one ordered, external-flow-neutralized campaign equity path.

    This function is evidence-only. It does not size stakes, mutate PaperRiskPolicy,
    change exposure limits, or grant execution authority.
    """

    campaign_id = _text(campaign_id, "campaign_id")
    protocol_sha256 = _sha256(protocol_sha256, "protocol_sha256")
    source_sha256 = _sha256(source_sha256, "source_sha256")
    capital_scope_id = _text(capital_scope_id, "capital_scope_id")
    currency = _currency(currency)

    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise _error("INVALID_EVENT", "events must be an ordered sequence")
    canonical_events = tuple(islice(events, _MAX_EVENTS + 1))
    if len(canonical_events) > _MAX_EVENTS:
        raise _error(
            "EVIDENCE_RESOURCE_LIMIT",
            "event count exceeds the supported evidence domain",
        )
    if not canonical_events:
        raise _error("FLOW_ORDER_AMBIGUOUS", "at least one valuation is required")
    if any(type(event) is not ProtectiveRiskEvent for event in canonical_events):
        raise _error("INVALID_EVENT", "events must contain ProtectiveRiskEvent values")
    if canonical_events[0].kind is not RiskEvidenceEventKind.VALUATION:
        raise _error(
            "FLOW_ORDER_AMBIGUOUS",
            "campaign evidence must begin with a pre-flow valuation",
        )

    event_ids: set[str] = set()
    previous_time: datetime | None = None
    last_valuation_index = max(
        (
            index
            for index, event in enumerate(canonical_events)
            if event.kind is RiskEvidenceEventKind.VALUATION
        ),
        default=-1,
    )
    for index, event in enumerate(canonical_events):
        expected_sequence = index + 1
        if event.sequence != expected_sequence:
            raise _error(
                "FLOW_ORDER_AMBIGUOUS",
                "event sequence must be contiguous and start at one",
            )
        if event.event_id in event_ids:
            raise _error("FLOW_ORDER_AMBIGUOUS", "event_id must be unique")
        event_ids.add(event.event_id)
        current_time = _instant(event.occurred_at, "occurred_at")
        if previous_time is not None and current_time < previous_time:
            raise _error(
                "FLOW_ORDER_AMBIGUOUS",
                "occurred_at may tie but must not move backward relative to sequence",
            )
        previous_time = current_time
        if (
            event.kind is RiskEvidenceEventKind.CAPITAL_FLOW
            and index >= last_valuation_index
        ):
            raise _error(
                "FLOW_ORDER_AMBIGUOUS",
                "every capital flow requires a later post-flow valuation",
            )

    cumulative_external_flow = Decimal("0")
    points: list[FlowAdjustedEquityPoint] = []
    high_water: Decimal | None = None
    maximum_drawdown_absolute = Decimal("0")
    maximum_drawdown_fraction_seen = Decimal("0")
    minimum_equity: Decimal | None = None
    ruin_event_id: str | None = None
    percentage_status = PercentageEvidenceStatus.ACTIVE

    try:
        with localcontext(_MONEY_CONTEXT) as money_context:
            # Any accidental loss of exact money information is a correctness error.
            money_context.traps[Inexact] = True
            money_context.traps[Rounded] = True
            for event in canonical_events:
                if event.kind is RiskEvidenceEventKind.CAPITAL_FLOW:
                    net_flow = _classify_external_flow(event, capital_scope_id)
                    if ruin_event_id is not None and net_flow > 0:
                        raise _error(
                            "RECAPITALIZATION_AFTER_RUIN",
                            "external inflow after nonpositive equity requires a new campaign",
                        )
                    cumulative_external_flow = cumulative_external_flow + net_flow
                    continue

                assert event.raw_equity is not None
                adjusted = event.raw_equity - cumulative_external_flow
                if high_water is None or adjusted > high_water:
                    high_water = adjusted
                drawdown_absolute = max(Decimal("0"), high_water - adjusted)
                maximum_drawdown_absolute = max(
                    maximum_drawdown_absolute,
                    drawdown_absolute,
                )
                minimum_equity = (
                    adjusted if minimum_equity is None else min(minimum_equity, adjusted)
                )

                fraction: Decimal | None
                if (
                    percentage_status is PercentageEvidenceStatus.ACTIVE
                    and high_water > 0
                    and adjusted > 0
                ):
                    with localcontext(_DECIMAL_CONTEXT):
                        fraction = drawdown_absolute / high_water
                    maximum_drawdown_fraction_seen = max(
                        maximum_drawdown_fraction_seen,
                        fraction,
                    )
                else:
                    fraction = None

                if adjusted <= 0 and ruin_event_id is None:
                    ruin_event_id = event.event_id
                    percentage_status = (
                        PercentageEvidenceStatus.TERMINATED_NONPOSITIVE_EQUITY
                    )
                    fraction = None

                points.append(
                    FlowAdjustedEquityPoint(
                        sequence=event.sequence,
                        event_id=event.event_id,
                        occurred_at=event.occurred_at,
                        raw_equity=event.raw_equity,
                        cumulative_external_flow=cumulative_external_flow,
                        flow_adjusted_equity=adjusted,
                        high_water_mark=high_water,
                        drawdown_absolute=drawdown_absolute,
                        drawdown_fraction=fraction,
                    )
                )
    except DecimalException as exc:
        raise _error(
            "ARITHMETIC_UNREPRESENTABLE",
            "protective-risk arithmetic exceeds the canonical Decimal domain",
        ) from exc
    if not points:
        raise _error("FLOW_ORDER_AMBIGUOUS", "at least one valuation is required")
    assert minimum_equity is not None

    maximum_drawdown_fraction = (
        maximum_drawdown_fraction_seen
        if percentage_status is PercentageEvidenceStatus.ACTIVE
        else None
    )

    provisional = ProtectiveRiskFlowEvidence(
        campaign_id=campaign_id,
        protocol_sha256=protocol_sha256,
        source_sha256=source_sha256,
        capital_scope_id=capital_scope_id,
        currency=currency,
        events=canonical_events,
        points=tuple(points),
        percentage_status=percentage_status,
        ruin_event_id=ruin_event_id,
        cumulative_external_flow=cumulative_external_flow,
        maximum_drawdown_absolute=maximum_drawdown_absolute,
        maximum_drawdown_fraction=maximum_drawdown_fraction,
        minimum_flow_adjusted_equity=minimum_equity,
        evidence_sha256="0" * 64,
    )
    digest = _digest(provisional.payload_without_digest())
    return ProtectiveRiskFlowEvidence(
        campaign_id=provisional.campaign_id,
        protocol_sha256=provisional.protocol_sha256,
        source_sha256=provisional.source_sha256,
        capital_scope_id=provisional.capital_scope_id,
        currency=provisional.currency,
        events=provisional.events,
        points=provisional.points,
        percentage_status=provisional.percentage_status,
        ruin_event_id=provisional.ruin_event_id,
        cumulative_external_flow=provisional.cumulative_external_flow,
        maximum_drawdown_absolute=provisional.maximum_drawdown_absolute,
        maximum_drawdown_fraction=provisional.maximum_drawdown_fraction,
        minimum_flow_adjusted_equity=provisional.minimum_flow_adjusted_equity,
        evidence_sha256=digest,
    )