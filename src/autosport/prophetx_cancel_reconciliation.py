from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Any, Iterable

SCHEMA_VERSION = 2
PROJECTION_AUTHORITY = "STRUCTURAL_ONLY_UNVERIFIED_PROVIDER_ORIGIN"


class ProphetXCancelError(ValueError):
    pass


class ProphetXCancelConflict(ProphetXCancelError):
    pass


class CancelTransport(str, Enum):
    FIX = "FIX_ORDER_ENTRY"
    REST = "REST_DIRECT_LINK"


class CancelScope(str, Enum):
    WHOLE_REMAINDER = "WHOLE_REMAINDER"


class RestTransportState(str, Enum):
    TIMEOUT_AFTER_POSSIBLE_SEND = "TIMEOUT_AFTER_POSSIBLE_SEND"
    HTTP_200_UNQUALIFIED = "HTTP_200_UNQUALIFIED"
    HTTP_404_SAMPLE_AMBIGUOUS = "HTTP_404_SAMPLE_AMBIGUOUS"
    MALFORMED_OR_UNQUALIFIED_RESPONSE = "MALFORMED_OR_UNQUALIFIED_RESPONSE"
    NETWORK_ERROR_AFTER_POSSIBLE_SEND = "NETWORK_ERROR_AFTER_POSSIBLE_SEND"


class CancelRejectReason(str, Enum):
    TOO_LATE = "0"
    UNKNOWN_ORDER = "1"
    OTHER = "2"


class CancelRejectResponseTo(str, Enum):
    CANCEL = "1"
    REPLACE = "2"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class CancelOutcome(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    TERMINAL_OBSERVED = "TERMINAL_OBSERVED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class CancelCause(str, Enum):
    NONE = "NONE"
    PROVIDER_TERMINAL_UNSPECIFIED = "PROVIDER_TERMINAL_UNSPECIFIED"
    CANCEL_REJECT = "CANCEL_REJECT"
    TRANSPORT_AMBIGUOUS = "TRANSPORT_AMBIGUOUS"


def _text(v: object, name: str) -> str:
    if not isinstance(v, str) or not v or v != v.strip():
        raise ProphetXCancelError(f"{name} must be non-empty trimmed text")
    try:
        v.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProphetXCancelError(f"{name} must be valid UTF-8") from exc
    return v


def _time(v: object, name: str) -> str:
    s = _text(v, name)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProphetXCancelError(f"{name} must be ISO-8601") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ProphetXCancelError(f"{name} must be timezone-aware")
    return s


def _seq(v: object, name: str) -> int:
    if type(v) is not int or v <= 0:
        raise ProphetXCancelError(
            f"{name} must be a positive non-boolean int"
        )
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
        raise ProphetXCancelError(f"{name} must be Decimal") from exc
    if not d.is_finite() or positive and d <= 0 or nonnegative and d < 0:
        raise ProphetXCancelError(f"{name} has invalid Decimal value")
    return d


def _dtext(d: Decimal) -> str:
    if not d.is_finite():
        raise ProphetXCancelError("non-finite Decimal")
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
            raise ProphetXCancelConflict(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class CancelRequest:
    canonical_order_id: str
    environment: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    original_cl_ord_id: str
    cancel_cl_ord_id: str | None
    requested_at: str
    transport: CancelTransport = CancelTransport.FIX
    scope: CancelScope = CancelScope.WHOLE_REMAINDER

    def __post_init__(self) -> None:
        for n in (
            "canonical_order_id",
            "environment",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "original_cl_ord_id",
        ):
            _text(getattr(self, n), n)
        if not isinstance(self.transport, CancelTransport):
            raise ProphetXCancelError("invalid cancel transport")
        if not isinstance(self.scope, CancelScope):
            raise ProphetXCancelError("invalid cancel scope")
        if self.scope is not CancelScope.WHOLE_REMAINDER:
            raise ProphetXCancelError("unsupported cancel scope")
        if self.transport is CancelTransport.FIX:
            cancel_id = _text(self.cancel_cl_ord_id, "cancel_cl_ord_id")
            if self.original_cl_ord_id == cancel_id:
                raise ProphetXCancelError(
                    "cancel request ClOrdID must differ from original order ClOrdID"
                )
        elif self.cancel_cl_ord_id is not None:
            raise ProphetXCancelError(
                "REST cancel has no FIX cancel ClOrdID; bind original external_id instead"
            )
        _time(self.requested_at, "requested_at")

    @property
    def fingerprint(self) -> str:
        return _digest(
            {
                "canonical_order_id": self.canonical_order_id,
                "environment": self.environment,
                "account_id": self.account_id,
                "symbol": self.symbol,
                "side": self.side,
                "provider_order_id": self.provider_order_id,
                "original_cl_ord_id": self.original_cl_ord_id,
                "cancel_cl_ord_id": self.cancel_cl_ord_id,
                "requested_at": self.requested_at,
                "transport": self.transport.value,
                "scope": self.scope.value,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    environment: str
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
            "environment",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        if self.status not in {
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise ProphetXCancelError(
                "cancel baseline must be a working order"
            )
        _time(self.observed_at, "observed_at")
        _seq(self.fix_sequence, "fix_sequence")
        q = _dec(self.order_quantity, "order_quantity", positive=True)
        f = _dec(
            self.cumulative_filled,
            "cumulative_filled",
            nonnegative=True,
        )
        l = _dec(
            self.leaves_quantity,
            "leaves_quantity",
            positive=True,
        )
        if f + l != q:
            raise ProphetXCancelError(
                "order_quantity must equal cumulative_filled + leaves_quantity"
            )
        if self.status is OrderStatus.NEW and f != 0:
            raise ProphetXCancelError(
                "NEW working order cannot already contain fills"
            )
        if self.status is OrderStatus.PARTIALLY_FILLED and f <= 0:
            raise ProphetXCancelError(
                "PARTIALLY_FILLED requires positive cumulative fill"
            )
        avg = self.average_fill_price
        if f == 0 and avg is not None:
            raise ProphetXCancelError(
                "zero fill cannot have average_fill_price"
            )
        if f > 0 and avg is None:
            raise ProphetXCancelError(
                "positive fill requires average_fill_price"
            )
        if avg is not None:
            avg = _dec(
                avg,
                "average_fill_price",
                positive=True,
            )
        object.__setattr__(self, "order_quantity", q)
        object.__setattr__(self, "cumulative_filled", f)
        object.__setattr__(self, "leaves_quantity", l)
        object.__setattr__(self, "average_fill_price", avg)


@dataclass(frozen=True, slots=True)
class Fill:
    exec_id: str
    environment: str
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
            "environment",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        object.__setattr__(
            self,
            "last_quantity",
            _dec(
                self.last_quantity,
                "last_quantity",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "last_price",
            _dec(
                self.last_price,
                "last_price",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "cumulative_quantity",
            _dec(
                self.cumulative_quantity,
                "cumulative_quantity",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "leaves_quantity",
            _dec(
                self.leaves_quantity,
                "leaves_quantity",
                nonnegative=True,
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
            "environment": self.environment,
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
class CanceledReport:
    exec_id: str
    environment: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    cl_ord_id: str
    cumulative_quantity: Decimal | str | int
    leaves_quantity: Decimal | str | int
    transact_time: str
    fix_session_id: str
    fix_sequence: int

    def __post_init__(self) -> None:
        for n in (
            "exec_id",
            "environment",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
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
            _dec(
                self.leaves_quantity,
                "leaves_quantity",
                nonnegative=True,
            ),
        )
        _time(self.transact_time, "transact_time")
        _seq(self.fix_sequence, "fix_sequence")

    @property
    def evidence_id(self) -> str:
        return self.exec_id

    def wire(self) -> dict[str, Any]:
        return {
            "kind": "CANCELED",
            "exec_id": self.exec_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side,
            "provider_order_id": self.provider_order_id,
            "cl_ord_id": self.cl_ord_id,
            "cumulative_quantity": _dtext(self.cumulative_quantity),
            "leaves_quantity": _dtext(self.leaves_quantity),
            "transact_time": self.transact_time,
            "fix_session_id": self.fix_session_id,
            "fix_sequence": self.fix_sequence,
        }


@dataclass(frozen=True, slots=True)
class CancelReject:
    reject_id: str
    environment: str
    account_id: str
    symbol: str
    side: str
    provider_order_id: str
    cancel_cl_ord_id: str
    orig_cl_ord_id: str
    reason: CancelRejectReason
    response_to: CancelRejectResponseTo
    order_status: OrderStatus
    transact_time: str
    fix_session_id: str
    fix_sequence: int

    def __post_init__(self) -> None:
        for n in (
            "reject_id",
            "environment",
            "account_id",
            "symbol",
            "side",
            "provider_order_id",
            "cancel_cl_ord_id",
            "orig_cl_ord_id",
            "fix_session_id",
        ):
            _text(getattr(self, n), n)
        if not isinstance(self.reason, CancelRejectReason):
            raise ProphetXCancelError("invalid CxlRejReason")
        if not isinstance(self.response_to, CancelRejectResponseTo):
            raise ProphetXCancelError("invalid CxlRejResponseTo")
        if not isinstance(self.order_status, OrderStatus):
            raise ProphetXCancelError("invalid order_status")
        _time(self.transact_time, "transact_time")
        _seq(self.fix_sequence, "fix_sequence")

    @property
    def evidence_id(self) -> str:
        return self.reject_id

    def wire(self) -> dict[str, Any]:
        return {
            "kind": "CANCEL_REJECT",
            "reject_id": self.reject_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side,
            "provider_order_id": self.provider_order_id,
            "cancel_cl_ord_id": self.cancel_cl_ord_id,
            "orig_cl_ord_id": self.orig_cl_ord_id,
            "reason": self.reason.value,
            "response_to": self.response_to.value,
            "order_status": self.order_status.value,
            "transact_time": self.transact_time,
            "fix_session_id": self.fix_session_id,
            "fix_sequence": self.fix_sequence,
        }


@dataclass(frozen=True, slots=True)
class RestTransportObservation:
    observation_id: str
    environment: str
    account_id: str
    provider_order_id: str
    http_status: int | None
    state: RestTransportState
    observed_at: str

    def __post_init__(self) -> None:
        for n in (
            "observation_id",
            "environment",
            "account_id",
            "provider_order_id",
        ):
            _text(getattr(self, n), n)
        if not isinstance(self.state, RestTransportState):
            raise ProphetXCancelError("invalid REST transport state")
        if self.http_status is not None and (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ProphetXCancelError(
                "http_status must be a valid non-boolean HTTP status"
            )
        if (
            self.state
            in {
                RestTransportState.TIMEOUT_AFTER_POSSIBLE_SEND,
                RestTransportState.NETWORK_ERROR_AFTER_POSSIBLE_SEND,
            }
            and self.http_status is not None
        ):
            raise ProphetXCancelError(
                "network/timeout state cannot carry an HTTP status"
            )
        if (
            self.state is RestTransportState.HTTP_200_UNQUALIFIED
            and self.http_status != 200
        ):
            raise ProphetXCancelError(
                "HTTP_200_UNQUALIFIED requires http_status=200"
            )
        if (
            self.state is RestTransportState.HTTP_404_SAMPLE_AMBIGUOUS
            and self.http_status != 404
        ):
            raise ProphetXCancelError(
                "HTTP_404_SAMPLE_AMBIGUOUS requires http_status=404"
            )
        _time(self.observed_at, "observed_at")

    @property
    def evidence_id(self) -> str:
        return self.observation_id

    def wire(self) -> dict[str, Any]:
        return {
            "kind": "REST_TRANSPORT",
            "observation_id": self.observation_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "provider_order_id": self.provider_order_id,
            "http_status": self.http_status,
            "state": self.state.value,
            "observed_at": self.observed_at,
        }


Evidence = Fill | CanceledReport | CancelReject | RestTransportObservation


@dataclass(frozen=True, slots=True)
class Projection:
    request_fingerprint: str | None
    outcome: CancelOutcome
    cause: CancelCause
    provider_status: OrderStatus
    known_filled_quantity: Decimal
    known_average_fill_price: Decimal | None
    known_open_quantity: Decimal | None
    released_quantity: Decimal
    requires_readback: bool
    active_order_identity: str
    evidence_digest: str

    @property
    def provider_origin_authoritative(self) -> bool:
        """Structural provider event DTOs are not product-issued origin authority."""
        return False

    @property
    def exposure_release_authorized(self) -> bool:
        """A structural release quantity cannot authorize capital/exposure release."""
        return False

    def assert_provider_origin_authoritative(self) -> None:
        raise ProphetXCancelError(
            "structural cancel projection has no provider-origin authority; "
            "compose canonical product-issued ProphetX readback evidence first"
        )

    def wire(self) -> dict[str, Any]:
        d = lambda x: None if x is None else _dtext(x)
        return {
            "request_fingerprint": self.request_fingerprint,
            "outcome": self.outcome.value,
            "cause": self.cause.value,
            "provider_status": self.provider_status.value,
            "known_filled_quantity": d(self.known_filled_quantity),
            "known_average_fill_price": d(
                self.known_average_fill_price
            ),
            "known_open_quantity": d(self.known_open_quantity),
            "released_quantity": d(self.released_quantity),
            "requires_readback": self.requires_readback,
            "active_order_identity": self.active_order_identity,
            "evidence_digest": self.evidence_digest,
            "provider_origin_authority": PROJECTION_AUTHORITY,
            "provider_origin_authoritative": False,
            "exposure_release_authorized": False,
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


def reconcile_cancel(
    working: WorkingOrder,
    evidence: Iterable[Evidence],
    *,
    request: CancelRequest | None = None,
) -> Projection:
    if request is not None:
        if not isinstance(request, CancelRequest):
            raise ProphetXCancelError(
                "request must be CancelRequest or None"
            )
        if (
            request.environment,
            request.account_id,
            request.symbol,
            request.side,
            request.provider_order_id,
            request.original_cl_ord_id,
        ) != (
            working.environment,
            working.account_id,
            working.symbol,
            working.side,
            working.provider_order_id,
            working.cl_ord_id,
        ):
            raise ProphetXCancelConflict(
                "cancel request does not match working order"
            )
        req_dt = datetime.fromisoformat(
            request.requested_at.replace("Z", "+00:00")
        )
        work_dt = datetime.fromisoformat(
            working.observed_at.replace("Z", "+00:00")
        )
        if work_dt > req_dt:
            raise ProphetXCancelError(
                "working-order evidence must exist before cancel request"
            )

    raw = tuple(evidence)
    by_id: dict[str, Evidence] = {}
    fix_seq: dict[int, Evidence] = {}
    for item in raw:
        if not isinstance(
            item,
            (
                Fill,
                CanceledReport,
                CancelReject,
                RestTransportObservation,
            ),
        ):
            raise ProphetXCancelError("unsupported cancel evidence")
        old = by_id.get(item.evidence_id)
        if old is not None:
            if old != item:
                raise ProphetXCancelConflict(
                    "conflicting provider evidence replay"
                )
            continue
        by_id[item.evidence_id] = item
        if isinstance(item, (Fill, CanceledReport, CancelReject)):
            prior = fix_seq.get(item.fix_sequence)
            if prior is not None and prior != item:
                raise ProphetXCancelConflict(
                    "conflicting evidence at FIX sequence"
                )
            fix_seq[item.fix_sequence] = item

    fix_events = sorted(
        (
            x
            for x in by_id.values()
            if isinstance(x, (Fill, CanceledReport, CancelReject))
        ),
        key=lambda x: x.fix_sequence,
    )
    rest_events = [
        x
        for x in by_id.values()
        if isinstance(x, RestTransportObservation)
    ]
    if len(rest_events) > 1:
        raise ProphetXCancelConflict(
            "multiple distinct REST transport observations imply an unmodeled retry"
        )
    if rest_events and (
        request is None
        or request.transport is not CancelTransport.REST
    ):
        raise ProphetXCancelConflict(
            "REST transport evidence requires the matching REST cancel request"
        )
    if (
        request is not None
        and request.transport is CancelTransport.FIX
        and rest_events
    ):
        raise ProphetXCancelConflict(
            "REST observation cannot satisfy FIX cancel attempt"
        )
    if (
        request is not None
        and request.transport is CancelTransport.REST
        and fix_events
    ):
        # Provider FIX reports may still be readback evidence for the same
        # order; they do not retroactively make the REST transport result
        # itself authoritative.
        pass

    for item in fix_events:
        if (
            item.fix_session_id != working.fix_session_id
            or item.fix_sequence <= working.fix_sequence
        ):
            raise ProphetXCancelConflict(
                "FIX session/sequence mismatch"
            )
        if (
            item.environment,
            item.account_id,
            item.symbol,
            item.side,
            item.provider_order_id,
        ) != (
            working.environment,
            working.account_id,
            working.symbol,
            working.side,
            working.provider_order_id,
        ):
            raise ProphetXCancelConflict(
                "provider evidence identity mismatch"
            )

    for item in rest_events:
        if (
            item.environment,
            item.account_id,
            item.provider_order_id,
        ) != (
            working.environment,
            working.account_id,
            working.provider_order_id,
        ):
            raise ProphetXCancelConflict(
                "REST transport evidence identity mismatch"
            )
        if request is not None:
            observed_dt = datetime.fromisoformat(
                item.observed_at.replace("Z", "+00:00")
            )
            request_dt = datetime.fromisoformat(
                request.requested_at.replace("Z", "+00:00")
            )
            if observed_dt < request_dt:
                raise ProphetXCancelConflict(
                    "REST transport observation cannot predate cancel request"
                )

    filled = working.cumulative_filled
    avg = working.average_fill_price
    open_qty: Decimal | None = working.leaves_quantity
    released = Decimal("0")
    status = working.status
    outcome = (
        CancelOutcome.NOT_REQUESTED
        if request is None
        else CancelOutcome.PENDING
    )
    cause = CancelCause.NONE
    readback = False
    terminal_seen = False
    reject_seen = False

    # REST transport semantics are intentionally weak. HTTP 200/404,
    # timeout and malformed response all require provider-origin state
    # reconciliation.
    if rest_events:
        outcome = CancelOutcome.UNKNOWN
        cause = CancelCause.TRANSPORT_AMBIGUOUS
        readback = True

    for item in fix_events:
        if isinstance(item, Fill):
            if terminal_seen:
                raise ProphetXCancelConflict(
                    "fill appears after terminal Canceled report"
                )
            if item.cl_ord_id != working.cl_ord_id:
                raise ProphetXCancelConflict(
                    "fill does not belong to original order"
                )
            if (
                item.cumulative_quantity
                != filled + item.last_quantity
            ):
                raise ProphetXCancelConflict(
                    "fill cumulative quantity mismatch"
                )
            if (
                item.cumulative_quantity + item.leaves_quantity
                != working.order_quantity
            ):
                raise ProphetXCancelConflict(
                    "fill conservation mismatch"
                )
            filled, avg = _avg(
                filled,
                avg,
                item.last_quantity,
                item.last_price,
            )
            open_qty = item.leaves_quantity
            status = (
                OrderStatus.FILLED
                if item.leaves_quantity == 0
                else OrderStatus.PARTIALLY_FILLED
            )
            continue

        if isinstance(item, CanceledReport):
            if item.cl_ord_id != working.cl_ord_id:
                raise ProphetXCancelConflict(
                    "ProphetX Canceled report must correlate to original order ClOrdID"
                )
            if item.leaves_quantity != 0:
                raise ProphetXCancelConflict(
                    "terminal Canceled report must have LeavesQty=0"
                )
            if item.cumulative_quantity != filled:
                raise ProphetXCancelConflict(
                    "Canceled report CumQty must equal provider-authoritative fills already observed"
                )
            if open_qty == 0:
                raise ProphetXCancelConflict(
                    "fully filled order cannot later become Canceled on this lifecycle"
                )
            if terminal_seen:
                raise ProphetXCancelConflict(
                    "multiple distinct terminal cancel reports"
                )
            terminal_seen = True
            released = (
                open_qty
                if open_qty is not None
                else Decimal("0")
            )
            open_qty = Decimal("0")
            status = OrderStatus.CANCELED
            if (
                request is not None
                and outcome is not CancelOutcome.REJECTED
            ):
                outcome = CancelOutcome.TERMINAL_OBSERVED
            # Current ProphetX Canceled report uses the order ClOrdID and
            # does not mechanically prove whether a nearby local cancel
            # caused it.
            cause = CancelCause.PROVIDER_TERMINAL_UNSPECIFIED
            readback = False
            continue

        assert isinstance(item, CancelReject)
        if item.response_to is not CancelRejectResponseTo.CANCEL:
            raise ProphetXCancelConflict(
                "OrderCancelReject CxlRejResponseTo does not identify cancel"
            )
        if reject_seen:
            raise ProphetXCancelConflict(
                "multiple distinct CancelReject reports for one cancel request"
            )
        reject_seen = True
        if request is None:
            raise ProphetXCancelConflict(
                "CancelReject cannot exist without a local cancel request"
            )
        if request.transport is not CancelTransport.FIX:
            raise ProphetXCancelConflict(
                "FIX CancelReject cannot reconcile a REST cancel request"
            )
        reject_dt = datetime.fromisoformat(
            item.transact_time.replace("Z", "+00:00")
        )
        request_dt = datetime.fromisoformat(
            request.requested_at.replace("Z", "+00:00")
        )
        if reject_dt < request_dt:
            raise ProphetXCancelConflict(
                "CancelReject cannot predate cancel request"
            )
        if (
            item.cancel_cl_ord_id != request.cancel_cl_ord_id
            or item.orig_cl_ord_id != request.original_cl_ord_id
        ):
            raise ProphetXCancelConflict(
                "CancelReject identity mismatch"
            )
        if (
            terminal_seen
            and item.order_status is not OrderStatus.CANCELED
        ):
            raise ProphetXCancelConflict(
                "CancelReject after terminal Canceled must report current CANCELED status"
            )
        outcome = CancelOutcome.REJECTED
        if terminal_seen:
            # Request was rejected/not applied, while the order is
            # independently provider-terminal. Preserve that distinction
            # and never relabel cause.
            cause = CancelCause.PROVIDER_TERMINAL_UNSPECIFIED
            status = OrderStatus.CANCELED
            readback = False
            open_qty = Decimal("0")
        else:
            cause = CancelCause.CANCEL_REJECT
            status = item.order_status
            released = Decimal("0")
            readback = True
            if status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.UNKNOWN,
            }:
                open_qty = None

    return Projection(
        None if request is None else request.fingerprint,
        outcome,
        cause,
        status,
        filled,
        avg,
        open_qty,
        released,
        readback,
        working.cl_ord_id,
        _digest(
            [
                x.wire()
                for x in sorted(
                    by_id.values(),
                    key=lambda x: (
                        getattr(x, "fix_sequence", 1 << 62),
                        x.evidence_id,
                    ),
                )
            ]
        ),
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
        obj = json.loads(
            raw,
            object_pairs_hook=_pairs,
        )
    except json.JSONDecodeError as exc:
        raise ProphetXCancelConflict(
            "checkpoint is not strict JSON"
        ) from exc
    if not isinstance(obj, dict) or set(obj) != {
        "schema_version",
        "authority",
        "payload",
        "payload_sha256",
    }:
        raise ProphetXCancelConflict(
            "checkpoint envelope mismatch"
        )
    if (
        obj["schema_version"] != SCHEMA_VERSION
        or obj["authority"]
        != "CACHE_ONLY_RE_RESOLVE_PROVIDER_EVIDENCE"
    ):
        raise ProphetXCancelConflict(
            "checkpoint cannot grant provider authority"
        )
    payload = obj["payload"]
    if (
        not isinstance(payload, dict)
        or obj["payload_sha256"] != _digest(payload)
    ):
        raise ProphetXCancelConflict(
            "checkpoint digest mismatch"
        )
    expected = {
        "request_fingerprint",
        "outcome",
        "cause",
        "provider_status",
        "known_filled_quantity",
        "known_average_fill_price",
        "known_open_quantity",
        "released_quantity",
        "requires_readback",
        "active_order_identity",
        "evidence_digest",
        "provider_origin_authority",
        "provider_origin_authoritative",
        "exposure_release_authorized",
    }
    if (
        set(payload) != expected
        or type(payload["requires_readback"]) is not bool
        or payload["provider_origin_authority"] != PROJECTION_AUTHORITY
        or payload["provider_origin_authoritative"] is not False
        or payload["exposure_release_authorized"] is not False
    ):
        raise ProphetXCancelConflict(
            "checkpoint payload mismatch"
        )

    def od(
        name: str,
        *,
        nonnegative: bool = False,
    ) -> Decimal | None:
        return (
            None
            if payload[name] is None
            else _dec(
                payload[name],
                name,
                nonnegative=nonnegative,
                positive=not nonnegative,
            )
        )

    fp = payload["request_fingerprint"]
    if fp is not None:
        _text(fp, "request_fingerprint")
    return Projection(
        fp,
        CancelOutcome(payload["outcome"]),
        CancelCause(payload["cause"]),
        OrderStatus(payload["provider_status"]),
        _dec(
            payload["known_filled_quantity"],
            "known_filled_quantity",
            nonnegative=True,
        ),
        od("known_average_fill_price"),
        od(
            "known_open_quantity",
            nonnegative=True,
        ),
        _dec(
            payload["released_quantity"],
            "released_quantity",
            nonnegative=True,
        ),
        payload["requires_readback"],
        _text(
            payload["active_order_identity"],
            "active_order_identity",
        ),
        _text(
            payload["evidence_digest"],
            "evidence_digest",
        ),
    )
