from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Any, Iterable

SCHEMA_VERSION = 1


class ProphetXReplaceError(ValueError):
    pass


class ProphetXReplaceConflict(ProphetXReplaceError):
    pass


class ProphetXReplaceUnsupported(ProphetXReplaceError):
    pass


class Transport(str, Enum):
    FIX = "FIX_ORDER_ENTRY"
    REST = "REST_DIRECT_LINK"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReplaceOutcome(str, Enum):
    PENDING = "PENDING"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"


class ReplaceRejectResponseTo(str, Enum):
    CANCEL = "1"
    REPLACE = "2"


def _text(v: object, name: str) -> str:
    if not isinstance(v, str) or not v or v != v.strip():
        raise ProphetXReplaceError(f"{name} must be non-empty trimmed text")
    try:
        v.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProphetXReplaceError(f"{name} must be valid UTF-8") from exc
    return v


def _time(v: object, name: str) -> str:
    s = _text(v, name)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProphetXReplaceError(f"{name} must be ISO-8601") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ProphetXReplaceError(f"{name} must be timezone-aware")
    return s


def _seq(v: object, name: str) -> int:
    if type(v) is not int or v <= 0:
        raise ProphetXReplaceError(f"{name} must be a positive non-boolean int")
    return v


def _dec(
    v: Decimal | str | int,
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise ProphetXReplaceError(f"{name} must be Decimal") from exc
    if not d.is_finite() or positive and d <= 0 or nonnegative and d < 0:
        raise ProphetXReplaceError(f"{name} has invalid Decimal value")
    return d


def _dtext(d: Decimal) -> str:
    if not d.is_finite():
        raise ProphetXReplaceError("non-finite Decimal")
    if d == 0:
        return "0"
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _canon(v: Any) -> str:
    return json.dumps(
        v,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(v: Any) -> str:
    return hashlib.sha256(_canon(v).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ProphetXReplaceConflict(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class ReplaceRequest:
    canonical_order_id: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    original_cl_ord_id: str
    replace_cl_ord_id: str
    new_price: Decimal | str | int
    new_quantity: Decimal | str | int
    requested_at: str
    transport: Transport = Transport.FIX
    order_type: OrderType = OrderType.LIMIT

    def __post_init__(self) -> None:
        for n in (
            "canonical_order_id",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "original_cl_ord_id",
            "replace_cl_ord_id",
        ):
            _text(getattr(self, n), n)
        if self.original_cl_ord_id == self.replace_cl_ord_id:
            raise ProphetXReplaceError("replacement needs a new ClOrdID")
        if not isinstance(self.transport, Transport) or not isinstance(
            self.order_type, OrderType
        ):
            raise ProphetXReplaceError("invalid transport/order type")
        _time(self.requested_at, "requested_at")
        object.__setattr__(
            self, "new_price", _dec(self.new_price, "new_price", positive=True)
        )
        object.__setattr__(
            self,
            "new_quantity",
            _dec(self.new_quantity, "new_quantity", positive=True),
        )

    @property
    def fingerprint(self) -> str:
        return _digest(
            {
                "canonical_order_id": self.canonical_order_id,
                "account_id": self.account_id,
                "symbol": self.symbol,
                "side": self.side,
                "provider_order_id": self.provider_order_id,
                "original_cl_ord_id": self.original_cl_ord_id,
                "replace_cl_ord_id": self.replace_cl_ord_id,
                "new_price": _dtext(self.new_price),
                "new_quantity": _dtext(self.new_quantity),
                "requested_at": self.requested_at,
                "transport": self.transport.value,
                "order_type": self.order_type.value,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    cl_ord_id: str
    status: OrderStatus
    order_quantity: Decimal | str | int
    cumulative_filled: Decimal | str | int
    leaves_quantity: Decimal | str | int
    average_fill_price: Decimal | str | int | None
    observed_at: str
    fix_session_id: str
    fix_sequence: int

    def __post_init__(self) -> None:
        for n in (
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        if not isinstance(self.status, OrderStatus):
            raise ProphetXReplaceError("invalid order status")
        _time(self.observed_at, "observed_at")
        _seq(self.fix_sequence, "fix_sequence")
        q = _dec(self.order_quantity, "order_quantity", positive=True)
        f = _dec(self.cumulative_filled, "cumulative_filled", nonnegative=True)
        l = _dec(self.leaves_quantity, "leaves_quantity", nonnegative=True)
        if f + l != q:
            raise ProphetXReplaceError(
                "order_quantity must equal cumulative_filled + leaves_quantity"
            )
        if self.status is OrderStatus.NEW and f != 0:
            raise ProphetXReplaceError("NEW working evidence cannot already contain fills")
        if self.status is OrderStatus.PARTIALLY_FILLED and (f <= 0 or l <= 0):
            raise ProphetXReplaceError(
                "PARTIALLY_FILLED working evidence requires filled and open quantity"
            )
        avg = self.average_fill_price
        if f == 0 and avg is not None:
            raise ProphetXReplaceError(
                "zero fill cannot have average_fill_price"
            )
        if f > 0 and avg is None:
            raise ProphetXReplaceError(
                "positive fill requires average_fill_price"
            )
        if avg is not None:
            avg = _dec(avg, "average_fill_price", positive=True)
        object.__setattr__(self, "order_quantity", q)
        object.__setattr__(self, "cumulative_filled", f)
        object.__setattr__(self, "leaves_quantity", l)
        object.__setattr__(self, "average_fill_price", avg)


@dataclass(frozen=True, slots=True)
class Fill:
    exec_id: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    cl_ord_id: str
    last_quantity: Decimal | str | int
    last_price: Decimal | str | int
    cumulative_quantity: Decimal | str | int
    leaves_quantity: Decimal | str | int
    transact_time: str
    fix_session_id: str
    fix_sequence: int

    def __post_init__(self) -> None:
        for n in (
            "exec_id",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        for n, positive in (
            ("last_quantity", True),
            ("last_price", True),
            ("cumulative_quantity", True),
            ("leaves_quantity", False),
        ):
            object.__setattr__(
                self,
                n,
                _dec(
                    getattr(self, n),
                    n,
                    positive=positive,
                    nonnegative=not positive,
                ),
            )
        _time(self.transact_time, "transact_time")
        _seq(self.fix_sequence, "fix_sequence")

    @property
    def evidence_id(self) -> str:
        return self.exec_id

    def wire(self) -> dict[str, Any]:
        return {
            "kind": "FILL",
            "exec_id": self.exec_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side,
            "provider_order_id": self.provider_order_id,
            "cl_ord_id": self.cl_ord_id,
            "last_quantity": _dtext(self.last_quantity),
            "last_price": _dtext(self.last_price),
            "cumulative_quantity": _dtext(self.cumulative_quantity),
            "leaves_quantity": _dtext(self.leaves_quantity),
            "transact_time": self.transact_time,
            "fix_session_id": self.fix_session_id,
            "fix_sequence": self.fix_sequence,
        }


@dataclass(frozen=True, slots=True)
class Replaced:
    exec_id: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    cl_ord_id: str
    orig_cl_ord_id: str
    price: Decimal | str | int
    order_quantity: Decimal | str | int
    cumulative_quantity: Decimal | str | int
    leaves_quantity: Decimal | str | int
    transact_time: str
    fix_session_id: str
    fix_sequence: int

    def __post_init__(self) -> None:
        for n in (
            "exec_id",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cl_ord_id",
            "orig_cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        object.__setattr__(
            self, "price", _dec(self.price, "price", positive=True)
        )
        object.__setattr__(
            self,
            "order_quantity",
            _dec(self.order_quantity, "order_quantity", positive=True),
        )
        object.__setattr__(
            self,
            "cumulative_quantity",
            _dec(
                self.cumulative_quantity,
                "cumulative_quantity",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "leaves_quantity",
            _dec(self.leaves_quantity, "leaves_quantity", nonnegative=True),
        )
        _time(self.transact_time, "transact_time")
        _seq(self.fix_sequence, "fix_sequence")

    @property
    def evidence_id(self) -> str:
        return self.exec_id

    def wire(self) -> dict[str, Any]:
        return {
            "kind": "REPLACED",
            "exec_id": self.exec_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side,
            "provider_order_id": self.provider_order_id,
            "cl_ord_id": self.cl_ord_id,
            "orig_cl_ord_id": self.orig_cl_ord_id,
            "price": _dtext(self.price),
            "order_quantity": _dtext(self.order_quantity),
            "cumulative_quantity": _dtext(self.cumulative_quantity),
            "leaves_quantity": _dtext(self.leaves_quantity),
            "transact_time": self.transact_time,
            "fix_session_id": self.fix_session_id,
            "fix_sequence": self.fix_sequence,
        }


@dataclass(frozen=True, slots=True)
class ReplaceReject:
    reject_id: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    replace_cl_ord_id: str
    orig_cl_ord_id: str
    reason: str
    response_to: ReplaceRejectResponseTo
    order_status: OrderStatus
    transact_time: str
    fix_session_id: str
    fix_sequence: int

    def __post_init__(self) -> None:
        for n in (
            "reject_id",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "replace_cl_ord_id",
            "orig_cl_ord_id",
            "reason",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        if not isinstance(self.response_to, ReplaceRejectResponseTo):
            raise ProphetXReplaceError("invalid CxlRejResponseTo")
        if not isinstance(self.order_status, OrderStatus):
            raise ProphetXReplaceError("invalid order_status")
        _time(self.transact_time, "transact_time")
        _seq(self.fix_sequence, "fix_sequence")

    @property
    def evidence_id(self) -> str:
        return self.reject_id

    def wire(self) -> dict[str, Any]:
        return {
            "kind": "REPLACE_REJECT",
            "reject_id": self.reject_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side,
            "provider_order_id": self.provider_order_id,
            "replace_cl_ord_id": self.replace_cl_ord_id,
            "orig_cl_ord_id": self.orig_cl_ord_id,
            "reason": self.reason,
            "response_to": self.response_to.value,
            "order_status": self.order_status.value,
            "transact_time": self.transact_time,
            "fix_session_id": self.fix_session_id,
            "fix_sequence": self.fix_sequence,
        }


Evidence = Fill | Replaced | ReplaceReject


@dataclass(frozen=True, slots=True)
class Projection:
    request_fingerprint: str
    outcome: ReplaceOutcome
    original_status: OrderStatus
    original_filled_quantity: Decimal
    original_average_fill_price: Decimal | None
    replacement_filled_quantity: Decimal
    replacement_average_fill_price: Decimal | None
    replacement_open_quantity: Decimal
    active_cl_ord_id: str
    active_price: Decimal | None
    requires_readback: bool
    evidence_digest: str

    @property
    def total_historical_filled_quantity(self) -> Decimal:
        return self.original_filled_quantity + self.replacement_filled_quantity

    def wire(self) -> dict[str, Any]:
        d = lambda x: None if x is None else _dtext(x)
        return {
            "request_fingerprint": self.request_fingerprint,
            "outcome": self.outcome.value,
            "original_status": self.original_status.value,
            "original_filled_quantity": d(self.original_filled_quantity),
            "original_average_fill_price": d(
                self.original_average_fill_price
            ),
            "replacement_filled_quantity": d(
                self.replacement_filled_quantity
            ),
            "replacement_average_fill_price": d(
                self.replacement_average_fill_price
            ),
            "replacement_open_quantity": d(self.replacement_open_quantity),
            "active_cl_ord_id": self.active_cl_ord_id,
            "active_price": d(self.active_price),
            "requires_readback": self.requires_readback,
            "evidence_digest": self.evidence_digest,
        }


def _avg(
    q: Decimal,
    avg: Decimal | None,
    add: Decimal,
    px: Decimal,
) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = 50
        if q == 0:
            return add, px
        assert avg is not None
        nq = q + add
        return nq, +((q * avg + add * px) / nq)


def reconcile(
    request: ReplaceRequest,
    working: WorkingOrder,
    evidence: Iterable[Evidence],
) -> Projection:
    if request.transport is Transport.REST:
        raise ProphetXReplaceUnsupported(
            "REST atomic replace is unqualified; use separate "
            "cancel/reconcile/new workflow"
        )
    if request.order_type is not OrderType.LIMIT:
        raise ProphetXReplaceUnsupported(
            "FIX replace is limited to working LIMIT orders"
        )
    if (
        working.status
        not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
        or working.leaves_quantity <= 0
    ):
        raise ProphetXReplaceError(
            "provider evidence must prove a working order before replace"
        )
    if (
        working.account_id,
        working.symbol,
        working.side,
        working.provider_order_id,
        working.cl_ord_id,
    ) != (
        request.account_id,
        request.symbol,
        request.side,
        request.provider_order_id,
        request.original_cl_ord_id,
    ):
        raise ProphetXReplaceConflict("working-order identity mismatch")
    request_dt = datetime.fromisoformat(request.requested_at.replace("Z", "+00:00"))
    working_dt = datetime.fromisoformat(working.observed_at.replace("Z", "+00:00"))
    if working_dt > request_dt:
        raise ProphetXReplaceError(
            "working-order evidence must be available no later than the replace request"
        )

    by_id: dict[str, Evidence] = {}
    by_seq: dict[int, Evidence] = {}
    for item in tuple(evidence):
        if not isinstance(item, (Fill, Replaced, ReplaceReject)):
            raise ProphetXReplaceError("unsupported evidence")
        old = by_id.get(item.evidence_id)
        if old is not None:
            if old != item:
                raise ProphetXReplaceConflict(
                    "conflicting replay of provider evidence id"
                )
            continue
        seq_old = by_seq.get(item.fix_sequence)
        if seq_old is not None and seq_old != item:
            raise ProphetXReplaceConflict(
                "conflicting evidence at FIX sequence"
            )
        by_id[item.evidence_id] = item
        by_seq[item.fix_sequence] = item

    ordered = sorted(by_id.values(), key=lambda x: x.fix_sequence)
    for item in ordered:
        if (
            item.fix_session_id != working.fix_session_id
            or item.fix_sequence <= working.fix_sequence
        ):
            raise ProphetXReplaceConflict("FIX session/sequence mismatch")
        if (
            item.account_id,
            item.symbol,
            item.side,
            item.provider_order_id,
        ) != (
            request.account_id,
            request.symbol,
            request.side,
            request.provider_order_id,
        ):
            raise ProphetXReplaceConflict("provider evidence identity mismatch")

    oq = working.cumulative_filled
    oa = working.average_fill_price
    rq: Decimal = Decimal("0")
    ra: Decimal | None = None
    ro = Decimal("0")
    outcome = ReplaceOutcome.PENDING
    status = working.status
    active_id = request.original_cl_ord_id
    active_price: Decimal | None = None
    readback = False

    for item in ordered:
        if isinstance(item, Fill):
            if item.cl_ord_id == request.original_cl_ord_id:
                if outcome is ReplaceOutcome.REPLACED:
                    raise ProphetXReplaceConflict(
                        "old-order fill appears after Replaced"
                    )
                if (
                    item.cumulative_quantity
                    != oq + item.last_quantity
                    or item.cumulative_quantity + item.leaves_quantity
                    != working.order_quantity
                ):
                    raise ProphetXReplaceConflict(
                        "old-order fill conservation mismatch"
                    )
                oq, oa = _avg(
                    oq,
                    oa,
                    item.last_quantity,
                    item.last_price,
                )
                status = (
                    OrderStatus.FILLED
                    if item.leaves_quantity == 0
                    else OrderStatus.PARTIALLY_FILLED
                )
            elif item.cl_ord_id == request.replace_cl_ord_id:
                if outcome is not ReplaceOutcome.REPLACED:
                    raise ProphetXReplaceConflict(
                        "replacement fill precedes Replaced"
                    )
                if (
                    item.cumulative_quantity
                    != rq + item.last_quantity
                    or item.cumulative_quantity + item.leaves_quantity
                    != request.new_quantity
                ):
                    raise ProphetXReplaceConflict(
                        "replacement fill conservation mismatch"
                    )
                rq, ra = _avg(
                    rq,
                    ra,
                    item.last_quantity,
                    item.last_price,
                )
                ro = item.leaves_quantity
            else:
                raise ProphetXReplaceConflict("fill ClOrdID is unrelated")
        elif isinstance(item, Replaced):
            if (
                item.cl_ord_id != request.replace_cl_ord_id
                or item.orig_cl_ord_id != request.original_cl_ord_id
            ):
                raise ProphetXReplaceConflict("Replaced identity mismatch")
            if (
                item.price != request.new_price
                or item.order_quantity != request.new_quantity
            ):
                raise ProphetXReplaceConflict("Replaced economics mismatch")
            if (
                item.cumulative_quantity != 0
                or item.leaves_quantity != request.new_quantity
            ):
                raise ProphetXReplaceConflict(
                    "ProphetX replacement must begin CumQty=0 "
                    "with full new leaves"
                )
            if outcome is not ReplaceOutcome.PENDING:
                raise ProphetXReplaceConflict(
                    "incompatible terminal replace evidence"
                )
            if oq >= working.order_quantity:
                raise ProphetXReplaceConflict(
                    "old order became fully filled before provider replacement"
                )
            outcome = ReplaceOutcome.REPLACED
            status = OrderStatus.REPLACED
            active_id = request.replace_cl_ord_id
            active_price = request.new_price
            ro = request.new_quantity
        else:
            if item.response_to is not ReplaceRejectResponseTo.REPLACE:
                raise ProphetXReplaceConflict(
                    "OrderCancelReject CxlRejResponseTo does not identify replace"
                )
            if (
                item.replace_cl_ord_id != request.replace_cl_ord_id
                or item.orig_cl_ord_id != request.original_cl_ord_id
            ):
                raise ProphetXReplaceConflict(
                    "replace-reject identity mismatch"
                )
            if outcome is not ReplaceOutcome.PENDING:
                raise ProphetXReplaceConflict(
                    "incompatible terminal replace evidence"
                )
            outcome = ReplaceOutcome.REJECTED
            status = item.order_status
            active_id = request.original_cl_ord_id
            active_price = None
            readback = True

    return Projection(
        request.fingerprint,
        outcome,
        status,
        oq,
        oa,
        rq,
        ra,
        ro,
        active_id,
        active_price,
        readback,
        _digest([item.wire() for item in ordered]),
    )


def encode_checkpoint(projection: Projection) -> str:
    payload = projection.wire()
    return _canon(
        {
            "schema_version": SCHEMA_VERSION,
            "authority": "CACHE_ONLY_RE_RESOLVE_PROVIDER_EVIDENCE",
            "payload": payload,
            "payload_sha256": _digest(payload),
        }
    )


def decode_checkpoint(raw: str) -> Projection:
    _text(raw, "raw")
    try:
        obj = json.loads(raw, object_pairs_hook=_pairs)
    except json.JSONDecodeError as exc:
        raise ProphetXReplaceConflict(
            "checkpoint is not strict JSON"
        ) from exc
    if not isinstance(obj, dict) or set(obj) != {
        "schema_version",
        "authority",
        "payload",
        "payload_sha256",
    }:
        raise ProphetXReplaceConflict("checkpoint envelope mismatch")
    if (
        obj["schema_version"] != SCHEMA_VERSION
        or obj["authority"]
        != "CACHE_ONLY_RE_RESOLVE_PROVIDER_EVIDENCE"
    ):
        raise ProphetXReplaceConflict(
            "checkpoint cannot grant provider authority"
        )
    payload = obj["payload"]
    if (
        not isinstance(payload, dict)
        or obj["payload_sha256"] != _digest(payload)
    ):
        raise ProphetXReplaceConflict("checkpoint digest mismatch")
    expected = {
        "request_fingerprint",
        "outcome",
        "original_status",
        "original_filled_quantity",
        "original_average_fill_price",
        "replacement_filled_quantity",
        "replacement_average_fill_price",
        "replacement_open_quantity",
        "active_cl_ord_id",
        "active_price",
        "requires_readback",
        "evidence_digest",
    }
    if (
        set(payload) != expected
        or type(payload["requires_readback"]) is not bool
    ):
        raise ProphetXReplaceConflict("checkpoint payload mismatch")

    def od(name: str) -> Decimal | None:
        return (
            None
            if payload[name] is None
            else _dec(payload[name], name, positive=True)
        )

    return Projection(
        _text(
            payload["request_fingerprint"],
            "request_fingerprint",
        ),
        ReplaceOutcome(payload["outcome"]),
        OrderStatus(payload["original_status"]),
        _dec(
            payload["original_filled_quantity"],
            "original_filled_quantity",
            nonnegative=True,
        ),
        od("original_average_fill_price"),
        _dec(
            payload["replacement_filled_quantity"],
            "replacement_filled_quantity",
            nonnegative=True,
        ),
        od("replacement_average_fill_price"),
        _dec(
            payload["replacement_open_quantity"],
            "replacement_open_quantity",
            nonnegative=True,
        ),
        _text(
            payload["active_cl_ord_id"],
            "active_cl_ord_id",
        ),
        od("active_price"),
        payload["requires_readback"],
        _text(payload["evidence_digest"], "evidence_digest"),
    )
