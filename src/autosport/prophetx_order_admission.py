from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable


class ProphetXOrderShapeError(ValueError):
    """Frozen order input cannot be represented without guessing provider semantics."""


class ProphetXTransportProfile(str, Enum):
    FIX = "FIX"
    DIRECT_LINK_REST = "DIRECT_LINK_REST"


class ProphetXEnvironment(str, Enum):
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class ProphetXOrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class ProphetXTimeInForce(str, Enum):
    GTC = "GTC"
    FOK = "FOK"


class AdmissionState(str, Enum):
    PROVEN = "PROVEN"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProphetXOrderShape:
    """Immutable pre-transport order shape.

    This value deliberately contains no credential, provider-write capability, or
    account-limit assertion. It freezes only action fields that Autosport can
    inspect before transport.
    """

    transport: ProphetXTransportProfile
    environment: ProphetXEnvironment
    account_id: str
    strike_id: str
    quantity: Decimal | str | int
    order_type: ProphetXOrderType = ProphetXOrderType.LIMIT
    side: str = "BUY"
    american_price: int | str | Decimal | None = None
    time_in_force: ProphetXTimeInForce | str | None = None
    fill_or_kill: bool | None = None
    quote_id: str | None = None
    require_immediate_fill: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.transport, ProphetXTransportProfile):
            raise ProphetXOrderShapeError("transport must be a ProphetXTransportProfile")
        if not isinstance(self.environment, ProphetXEnvironment):
            raise ProphetXOrderShapeError("environment must be a ProphetXEnvironment")
        if not isinstance(self.order_type, ProphetXOrderType):
            raise ProphetXOrderShapeError("order_type must be a ProphetXOrderType")
        for field, value in (("account_id", self.account_id), ("strike_id", self.strike_id)):
            _trimmed_text(value, field)
        if not isinstance(self.side, str) or not self.side or self.side != self.side.strip():
            raise ProphetXOrderShapeError("side must be a non-empty trimmed string")
        if type(self.require_immediate_fill) is not bool:
            raise ProphetXOrderShapeError("require_immediate_fill must be bool")
        if self.fill_or_kill is not None and type(self.fill_or_kill) is not bool:
            raise ProphetXOrderShapeError("fill_or_kill must be bool when present")

        quantity = _positive_decimal(self.quantity, "quantity")
        object.__setattr__(self, "quantity", quantity)

        tif = self.time_in_force
        if isinstance(tif, str) and not isinstance(tif, ProphetXTimeInForce):
            try:
                tif = ProphetXTimeInForce(tif)
            except ValueError:
                # Preserve unsupported provider spelling as a structural blocker in
                # assessment instead of silently normalizing it.
                _trimmed_text(tif, "time_in_force")
        elif tif is not None and not isinstance(tif, ProphetXTimeInForce):
            raise ProphetXOrderShapeError(
                "time_in_force must be text or ProphetXTimeInForce"
            )
        object.__setattr__(self, "time_in_force", tif)

        if self.american_price is not None:
            object.__setattr__(
                self,
                "american_price",
                _american_price(self.american_price),
            )
        if self.quote_id is not None:
            _trimmed_text(self.quote_id, "quote_id")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.to_canonical_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        tif = self.time_in_force
        return {
            "transport": self.transport.value,
            "environment": self.environment.value,
            "account_id": self.account_id,
            "strike_id": self.strike_id,
            "quantity": _decimal_text(self.quantity),
            "order_type": self.order_type.value,
            "side": self.side,
            "american_price": self.american_price,
            "time_in_force": tif.value if isinstance(tif, ProphetXTimeInForce) else tif,
            "fill_or_kill": self.fill_or_kill,
            "quote_id": self.quote_id,
            "require_immediate_fill": self.require_immediate_fill,
        }


@dataclass(frozen=True, slots=True)
class ProphetXOrderAdmissionAssessment:
    """Fail-closed pre-transport assessment.

    PROVEN means only that this module can prove the named local/provider-contract
    shape invariant. UNKNOWN dimensions still require separate product-issued
    provider/account evidence. This object is never provider execution authority.
    """

    order_fingerprint: str
    instrument_identity: AdmissionState
    price_ladder: AdmissionState
    quantity_format: AdmissionState
    min_max_stake: AdmissionState
    exposure_limit: AdmissionState
    order_type_tif: AdmissionState
    batch_shape: AdmissionState
    reasons: tuple[str, ...]

    @property
    def structurally_blocked(self) -> bool:
        return AdmissionState.BLOCKED in (
            self.instrument_identity,
            self.price_ladder,
            self.quantity_format,
            self.min_max_stake,
            self.exposure_limit,
            self.order_type_tif,
            self.batch_shape,
        )

    @property
    def provider_execution_admissible(self) -> bool:
        """True only when every required dimension is product-proven.

        The current bounded implementation intentionally cannot return True because
        exact current ladder membership, provider min/max stake, exposure headroom,
        and current strike identity require separate provider-origin authorities.
        """

        # This bounded DTO is structural evidence only. Even a caller that
        # manually reconstructs every field as PROVEN cannot mint provider-write
        # authority from it.
        return False


@dataclass(frozen=True, slots=True)
class ProphetXBatchAssessment:
    order_fingerprints: tuple[str, ...]
    batch_shape: AdmissionState
    reasons: tuple[str, ...]


def assess_order_shape(order: ProphetXOrderShape) -> ProphetXOrderAdmissionAssessment:
    if not isinstance(order, ProphetXOrderShape):
        raise TypeError("order must be a ProphetXOrderShape")

    reasons: list[str] = []
    order_type_tif = _order_type_tif_state(order, reasons)

    # Presence/format is validated by ProphetXOrderShape, but current provider
    # membership is intentionally unresolved here. A display label or caller scalar
    # must never turn a strike into current provider execution authority.
    instrument_identity = AdmissionState.UNKNOWN
    reasons.append(
        "current provider/environment strike identity requires product-issued provider evidence"
    )

    # A syntactically valid American price is not proof that the current provider
    # ladder contains it. Market orders additionally require current QuoteID
    # authority that this structural module does not issue.
    price_ladder = AdmissionState.UNKNOWN
    reasons.append("current ProphetX price-ladder/QuoteID authority is not present")

    min_max = AdmissionState.UNKNOWN
    reasons.append("current provider/account min-max stake evidence is not present")
    exposure = AdmissionState.UNKNOWN
    reasons.append("current provider/account exposure-headroom evidence is not present")

    return ProphetXOrderAdmissionAssessment(
        order_fingerprint=order.fingerprint,
        instrument_identity=instrument_identity,
        price_ladder=price_ladder,
        quantity_format=AdmissionState.PROVEN,
        min_max_stake=min_max,
        exposure_limit=exposure,
        order_type_tif=order_type_tif,
        batch_shape=AdmissionState.PROVEN,
        reasons=tuple(reasons),
    )


def assess_batch(orders: Iterable[ProphetXOrderShape]) -> ProphetXBatchAssessment:
    if isinstance(orders, (str, bytes)):
        raise TypeError("orders must be an iterable of ProphetXOrderShape values")
    values = tuple(orders)
    if not values:
        return ProphetXBatchAssessment(
            (),
            AdmissionState.BLOCKED,
            ("batch is empty",),
        )
    if any(not isinstance(order, ProphetXOrderShape) for order in values):
        raise TypeError("orders must contain only ProphetXOrderShape values")

    fingerprints = tuple(order.fingerprint for order in values)
    reasons: list[str] = []
    transports = {order.transport for order in values}
    environments = {order.environment for order in values}
    accounts = {order.account_id for order in values}

    if len(transports) != 1:
        reasons.append("batch mixes transport profiles")
    if len(environments) != 1:
        reasons.append("batch mixes provider environments")
    if len(accounts) != 1:
        reasons.append("batch mixes provider accounts")
    if reasons:
        return ProphetXBatchAssessment(
            fingerprints,
            AdmissionState.BLOCKED,
            tuple(reasons),
        )

    transport = values[0].transport
    if transport is ProphetXTransportProfile.FIX:
        if len(values) > 20:
            return ProphetXBatchAssessment(
                fingerprints,
                AdmissionState.BLOCKED,
                ("ProphetX FIX NewOrderList maximum is 20 orders",),
            )
        return ProphetXBatchAssessment(fingerprints, AdmissionState.PROVEN, ())

    if len(values) == 1:
        return ProphetXBatchAssessment(fingerprints, AdmissionState.PROVEN, ())
    return ProphetXBatchAssessment(
        fingerprints,
        AdmissionState.UNKNOWN,
        ("Direct Link REST multi-order batch semantics are not qualified here",),
    )


def _order_type_tif_state(
    order: ProphetXOrderShape,
    reasons: list[str],
) -> AdmissionState:
    if order.side != "BUY":
        reasons.append("ProphetX order-entry contract is buy-only")
        return AdmissionState.BLOCKED

    if order.transport is ProphetXTransportProfile.FIX:
        if order.fill_or_kill is not None:
            reasons.append("FIX order shape must use TimeInForce, not REST fill_or_kill")
            return AdmissionState.BLOCKED

        tif = order.time_in_force
        if isinstance(tif, str) and not isinstance(tif, ProphetXTimeInForce):
            reasons.append(f"unsupported ProphetX FIX TimeInForce {tif!r}")
            return AdmissionState.BLOCKED

        effective_tif = tif or ProphetXTimeInForce.GTC
        if (
            order.require_immediate_fill
            and effective_tif is not ProphetXTimeInForce.FOK
        ):
            reasons.append("immediate-fill intent requires explicit FIX FOK")
            return AdmissionState.BLOCKED

        if order.order_type is ProphetXOrderType.LIMIT:
            if order.american_price is None:
                reasons.append("FIX limit order requires an explicit price")
                return AdmissionState.BLOCKED
            if order.quote_id is not None:
                reasons.append("FIX limit order must not substitute QuoteID for limit price")
                return AdmissionState.BLOCKED
            return AdmissionState.PROVEN

        if order.american_price is not None:
            reasons.append("FIX market order must not carry explicit Price")
            return AdmissionState.BLOCKED
        if order.quote_id is None:
            reasons.append("FIX market order requires provider QuoteID")
            return AdmissionState.BLOCKED
        return AdmissionState.PROVEN

    # Direct Link REST: the currently frozen public contract in this packet proves
    # exact strike/price/quantity plus fill_or_kill for limit-style submission. It
    # does not qualify FIX-style Market/QuoteID/TIF semantics on REST.
    if order.time_in_force is not None:
        reasons.append("Direct Link REST order must not import FIX TimeInForce semantics")
        return AdmissionState.BLOCKED
    if order.quote_id is not None:
        reasons.append("Direct Link REST order must not import FIX QuoteID market semantics")
        return AdmissionState.BLOCKED
    if order.order_type is not ProphetXOrderType.LIMIT:
        reasons.append("Direct Link REST market-order semantics are not qualified")
        return AdmissionState.BLOCKED
    if order.american_price is None:
        reasons.append("Direct Link REST limit order requires an explicit price")
        return AdmissionState.BLOCKED
    if order.fill_or_kill is None:
        reasons.append("Direct Link REST fill_or_kill must be explicit")
        return AdmissionState.BLOCKED
    if order.require_immediate_fill and not order.fill_or_kill:
        reasons.append("immediate-fill intent requires Direct Link fill_or_kill=true")
        return AdmissionState.BLOCKED
    return AdmissionState.PROVEN


def _trimmed_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXOrderShapeError(f"{field} must be a non-empty trimmed string")
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise ProphetXOrderShapeError(
            f"{field} must not contain control line breaks or NUL"
        )
    return value


def _positive_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProphetXOrderShapeError(
            f"{field} must use exact Decimal-compatible ingress"
        )
    if not isinstance(value, (str, int, Decimal)):
        raise ProphetXOrderShapeError(
            f"{field} must use exact Decimal-compatible ingress"
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProphetXOrderShapeError(
            f"{field} must be a finite positive Decimal"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ProphetXOrderShapeError(f"{field} must be a finite positive Decimal")
    return parsed


def _american_price(value: object) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProphetXOrderShapeError("american_price must use exact integer ingress")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ProphetXOrderShapeError("american_price must be an exact integer")
        parsed = int(value)
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise ProphetXOrderShapeError("american_price must be an exact integer")
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise ProphetXOrderShapeError(
                "american_price must be an exact integer"
            ) from exc
        if not decimal.is_finite() or decimal != decimal.to_integral_value():
            raise ProphetXOrderShapeError("american_price must be an exact integer")
        parsed = int(decimal)
    elif type(value) is int:
        parsed = value
    else:
        raise ProphetXOrderShapeError("american_price must be an exact integer")
    if parsed == 0 or abs(parsed) < 100:
        raise ProphetXOrderShapeError(
            "american_price absolute value must be at least 100"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
