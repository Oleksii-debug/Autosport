from __future__ import annotations

"""Read-only Betfair Order Stream (OCM) codec and deterministic state cache.

This module owns only provider-side private order-stream reconstruction. It does not
submit/cancel orders, mutate ``RealExecutionLedger``, infer Market Stream ordering,
or claim final settlement/P&L authority. Betfair ``initialClk``/``clk`` values remain
opaque resume tokens and the caller-provided subscription SHA-256 binds a cache to the
exact private-order subscription it reconstructs.

Order fields in OCM are absolute provider values. Replayed frames are therefore
idempotent and matched/cancelled/lapsed/voided sizes are replaced, never added.
``customerOrderRef`` (lightweight field ``rfo``) may bind one provider ``betId`` only;
a conflicting re-bind fails closed even after an image removes the order from the
current cache.
"""

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from .betfair_stream_codec import BetfairApplyStatus, BetfairFrameKind

ORDER_STREAM_FINAL_SETTLEMENT_AUTHORITY = False
_HEX = frozenset("0123456789abcdef")
_T = TypeVar("_T")


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be UTF-8 encodable") from exc
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return text


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _optional_integer(value: object, field: str, *, minimum: int = 0) -> int | None:
    return None if value is None else _integer(value, field, minimum=minimum)


def _decimal(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise ValueError(f"{field} must be a finite decimal >= {minimum}")
    return parsed


def _optional_decimal(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field, minimum=minimum)


def _optional_odds(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    parsed = _decimal(value, field)
    if parsed <= 1:
        raise ValueError(f"{field} must be decimal odds > 1")
    return parsed


def _optional_average_price(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    parsed = _decimal(value, field, minimum=Decimal("0"))
    if parsed != 0 and parsed <= 1:
        raise ValueError(f"{field} must be zero or decimal odds > 1")
    return parsed


def _frame_hash(raw: dict[str, Any]) -> str:
    try:
        payload = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Betfair order frame contains non-canonical JSON values") from exc
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairOrderIdentity:
    market_id: str
    selection_id: int
    handicap: Decimal
    bet_id: str

    def __post_init__(self) -> None:
        _text(self.market_id, "market_id")
        _integer(self.selection_id, "selection_id", minimum=1)
        if not isinstance(self.handicap, Decimal) or not self.handicap.is_finite():
            raise ValueError("handicap must be a finite Decimal")
        _text(self.bet_id, "bet_id")


@dataclass(frozen=True, slots=True)
class BetfairUnmatchedOrderDelta:
    bet_id: str
    customer_order_ref: str | None
    customer_strategy_ref: str | None
    side: str | None
    status: str | None
    persistence_type: str | None
    order_type: str | None
    price: Decimal | None
    size: Decimal | None
    average_price_matched: Decimal | None
    size_matched: Decimal | None
    size_remaining: Decimal | None
    size_lapsed: Decimal | None
    size_cancelled: Decimal | None
    size_voided: Decimal | None
    placed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class BetfairOrderRunnerChange:
    selection_id: int
    handicap: Decimal
    unmatched_orders: tuple[BetfairUnmatchedOrderDelta, ...]


@dataclass(frozen=True, slots=True)
class BetfairOrderMarketChange:
    market_id: str
    image: bool
    runner_changes: tuple[BetfairOrderRunnerChange, ...]


@dataclass(frozen=True, slots=True)
class BetfairOrderChangeFrame:
    kind: BetfairFrameKind
    initial_clk: str | None
    clk: str
    publish_time_ms: int
    conflated: bool
    market_changes: tuple[BetfairOrderMarketChange, ...]
    frame_sha256: str


@dataclass(frozen=True, slots=True)
class BetfairOrderStreamCursor:
    subscription_sha256: str
    initial_clk: str
    clk: str

    def __post_init__(self) -> None:
        _sha256(self.subscription_sha256, "subscription_sha256")
        _text(self.initial_clk, "initial_clk")
        _text(self.clk, "clk")


@dataclass(frozen=True, slots=True)
class BetfairOrderState:
    identity: BetfairOrderIdentity
    customer_order_ref: str | None
    customer_strategy_ref: str | None
    side: str
    status: str
    persistence_type: str | None
    order_type: str | None
    price: Decimal | None
    size: Decimal | None
    average_price_matched: Decimal | None
    size_matched: Decimal | None
    size_remaining: Decimal | None
    size_lapsed: Decimal | None
    size_cancelled: Decimal | None
    size_voided: Decimal | None
    placed_at_ms: int | None
    last_publish_time_ms: int

    @property
    def accepted_average_price(self) -> Decimal | None:
        if (
            self.average_price_matched is None
            or self.average_price_matched <= 1
            or self.size_matched is None
            or self.size_matched <= 0
        ):
            return None
        return self.average_price_matched


@dataclass(frozen=True, slots=True)
class BetfairOrderStreamApplyResult:
    status: BetfairApplyStatus
    cursor: BetfairOrderStreamCursor | None
    frame_kind: BetfairFrameKind
    publish_time_ms: int
    changed: tuple[BetfairOrderState, ...]
    removed: tuple[BetfairOrderIdentity, ...]
    image_replaced_markets: tuple[str, ...]


def _order(raw: object) -> BetfairUnmatchedOrderDelta:
    if type(raw) is not dict:
        raise ValueError("unmatched order change must be a JSON object")
    bet_id = _text(raw.get("id"), "order.id")
    customer_order_ref = _optional_text(raw.get("rfo"), "order.rfo")
    customer_strategy_ref = _optional_text(raw.get("rfs"), "order.rfs")
    side = _optional_text(raw.get("side"), "order.side")
    if side is not None and side not in {"B", "L"}:
        raise ValueError("order.side must be B or L")
    status = _optional_text(raw.get("status"), "order.status")
    if status is not None and status not in {"E", "EC"}:
        raise ValueError("order.status must be E or EC")
    persistence_type = _optional_text(raw.get("pt"), "order.pt")
    if persistence_type is not None and persistence_type not in {"L", "P", "MOC"}:
        raise ValueError("order.pt is unsupported")
    order_type = _optional_text(raw.get("ot"), "order.ot")
    if order_type is not None and order_type not in {"L", "LOC", "MOC"}:
        raise ValueError("order.ot is unsupported")
    size_fields = {
        name: _optional_decimal(raw.get(field), f"order.{field}", minimum=Decimal("0"))
        for name, field in (
            ("matched", "sm"),
            ("remaining", "sr"),
            ("lapsed", "sl"),
            ("cancelled", "sc"),
            ("voided", "sv"),
        )
    }
    return BetfairUnmatchedOrderDelta(
        bet_id=bet_id,
        customer_order_ref=customer_order_ref,
        customer_strategy_ref=customer_strategy_ref,
        side=side,
        status=status,
        persistence_type=persistence_type,
        order_type=order_type,
        price=_optional_odds(raw.get("p"), "order.p"),
        size=_optional_decimal(raw.get("s"), "order.s", minimum=Decimal("0")),
        average_price_matched=_optional_average_price(raw.get("avp"), "order.avp"),
        size_matched=size_fields["matched"],
        size_remaining=size_fields["remaining"],
        size_lapsed=size_fields["lapsed"],
        size_cancelled=size_fields["cancelled"],
        size_voided=size_fields["voided"],
        placed_at_ms=_optional_integer(raw.get("pd"), "order.pd"),
    )


def _runner(raw: object) -> BetfairOrderRunnerChange:
    if type(raw) is not dict:
        raise ValueError("order runner change must be a JSON object")
    selection_id = _integer(raw.get("id"), "order_runner.id", minimum=1)
    handicap = _decimal(raw.get("hc", 0), "order_runner.hc")
    orders_raw = raw.get("uo", [])
    if type(orders_raw) is not list:
        raise ValueError("order_runner.uo must be a list")
    orders = tuple(_order(item) for item in orders_raw)
    ids = [item.bet_id for item in orders]
    if len(set(ids)) != len(ids):
        raise ValueError("order_runner.uo contains duplicate betId")
    return BetfairOrderRunnerChange(selection_id, handicap, orders)


def _market(raw: object) -> BetfairOrderMarketChange:
    if type(raw) is not dict:
        raise ValueError("order market change must be a JSON object")
    market_id = _text(raw.get("id"), "order_market.id")
    image = raw.get("img", False)
    if type(image) is not bool:
        raise ValueError("order_market.img must be bool")
    runners_raw = raw.get("orc", [])
    if type(runners_raw) is not list:
        raise ValueError("order_market.orc must be a list")
    runners = tuple(_runner(item) for item in runners_raw)
    identities = [(item.selection_id, item.handicap) for item in runners]
    if len(set(identities)) != len(identities):
        raise ValueError("order_market.orc contains duplicate runner identity")
    return BetfairOrderMarketChange(market_id, image, runners)


def decode_order_change_message(raw: dict[str, Any]) -> BetfairOrderChangeFrame:
    """Decode one private OrderChangeMessage without granting execution authority."""

    if type(raw) is not dict:
        raise TypeError("raw must be a dict")
    if raw.get("op") != "ocm":
        raise ValueError("only Betfair order-change messages are supported")
    if raw.get("segmentType") is not None:
        raise ValueError(
            "segmented Betfair order messages require reassembly before application"
        )
    kinds = {
        None: BetfairFrameKind.DELTA,
        "SUB_IMAGE": BetfairFrameKind.SUB_IMAGE,
        "RESUB_DELTA": BetfairFrameKind.RESUB_DELTA,
        "HEARTBEAT": BetfairFrameKind.HEARTBEAT,
    }
    ct = raw.get("ct")
    if ct not in kinds:
        raise ValueError(f"unsupported Betfair order change type {ct!r}")
    kind = kinds[ct]
    initial_clk = _optional_text(raw.get("initialClk"), "initialClk")
    clk = _text(raw.get("clk"), "clk")
    publish_time_ms = _integer(raw.get("pt"), "pt")
    conflated = raw.get("con", False)
    if type(conflated) is not bool:
        raise ValueError("con must be bool")
    changes_raw = raw.get("oc", [])
    if type(changes_raw) is not list:
        raise ValueError("oc must be a list")
    if kind is BetfairFrameKind.HEARTBEAT and changes_raw:
        raise ValueError("order-stream heartbeat must not contain order changes")
    changes = tuple(_market(item) for item in changes_raw)
    market_ids = [item.market_id for item in changes]
    if len(set(market_ids)) != len(market_ids):
        raise ValueError("order frame contains duplicate market ids")
    if kind in {BetfairFrameKind.SUB_IMAGE, BetfairFrameKind.RESUB_DELTA}:
        if initial_clk is None:
            raise ValueError(f"{kind.value} requires initialClk")
    return BetfairOrderChangeFrame(
        kind=kind,
        initial_clk=initial_clk,
        clk=clk,
        publish_time_ms=publish_time_ms,
        conflated=conflated,
        market_changes=changes,
        frame_sha256=_frame_hash(raw),
    )


class BetfairOrderStreamState:
    """Deterministic OCM cache bound to one exact private-order subscription."""

    def __init__(self, *, subscription_sha256: str) -> None:
        self._subscription = _sha256(subscription_sha256, "subscription_sha256")
        self._initial_clk: str | None = None
        self._clk: str | None = None
        self._publish_time_ms: int | None = None
        self._frame_sha256: str | None = None
        self._initialized = False
        self._orders: dict[BetfairOrderIdentity, BetfairOrderState] = {}
        self._bet_locations: dict[str, BetfairOrderIdentity] = {}
        self._ref_to_bet: dict[str, str] = {}
        self._bet_to_ref: dict[str, str] = {}

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def subscription_sha256(self) -> str:
        return self._subscription

    def reconnect_cursor(self) -> BetfairOrderStreamCursor | None:
        if self._initial_clk is None or self._clk is None:
            return None
        return BetfairOrderStreamCursor(
            self._subscription,
            self._initial_clk,
            self._clk,
        )

    def snapshot(self) -> tuple[BetfairOrderState, ...]:
        def key(state: BetfairOrderState) -> tuple[str, int, str, str]:
            identity = state.identity
            return (
                identity.market_id,
                identity.selection_id,
                str(identity.handicap),
                identity.bet_id,
            )

        return tuple(sorted(self._orders.values(), key=key))

    def state_for_bet_id(self, bet_id: str) -> BetfairOrderState | None:
        bet_id = _text(bet_id, "bet_id")
        identity = self._bet_locations.get(bet_id)
        return None if identity is None else self._orders.get(identity)

    def bet_id_for_customer_order_ref(self, customer_order_ref: str) -> str | None:
        customer_order_ref = _text(customer_order_ref, "customer_order_ref")
        return self._ref_to_bet.get(customer_order_ref)

    def _bind_identity(
        self,
        identity: BetfairOrderIdentity,
        customer_order_ref: str | None,
    ) -> None:
        previous_location = self._bet_locations.get(identity.bet_id)
        if previous_location is not None and previous_location != identity:
            raise ValueError("Betfair betId moved to a conflicting market/runner identity")
        self._bet_locations[identity.bet_id] = identity
        if customer_order_ref is None:
            return
        previous_bet = self._ref_to_bet.get(customer_order_ref)
        if previous_bet is not None and previous_bet != identity.bet_id:
            raise ValueError("customerOrderRef rebound to a conflicting Betfair betId")
        previous_ref = self._bet_to_ref.get(identity.bet_id)
        if previous_ref is not None and previous_ref != customer_order_ref:
            raise ValueError("Betfair betId rebound to a conflicting customerOrderRef")
        self._ref_to_bet[customer_order_ref] = identity.bet_id
        self._bet_to_ref[identity.bet_id] = customer_order_ref

    @staticmethod
    def _merge_immutable(previous: _T | None, incoming: _T | None, field: str) -> _T | None:
        if incoming is None:
            return previous
        if previous is not None and previous != incoming:
            raise ValueError(f"Betfair order immutable field {field} changed")
        return incoming

    def _apply_order(
        self,
        market_id: str,
        runner: BetfairOrderRunnerChange,
        delta: BetfairUnmatchedOrderDelta,
        publish_time_ms: int,
    ) -> BetfairOrderState:
        identity = BetfairOrderIdentity(
            market_id=market_id,
            selection_id=runner.selection_id,
            handicap=runner.handicap,
            bet_id=delta.bet_id,
        )
        self._bind_identity(identity, delta.customer_order_ref)
        previous = self._orders.get(identity)
        if previous is None:
            if delta.side is None or delta.status is None:
                raise ValueError(
                    "first observation of a Betfair order requires side and status"
                )
            state = BetfairOrderState(
                identity=identity,
                customer_order_ref=delta.customer_order_ref,
                customer_strategy_ref=delta.customer_strategy_ref,
                side=delta.side,
                status=delta.status,
                persistence_type=delta.persistence_type,
                order_type=delta.order_type,
                price=delta.price,
                size=delta.size,
                average_price_matched=delta.average_price_matched,
                size_matched=delta.size_matched,
                size_remaining=delta.size_remaining,
                size_lapsed=delta.size_lapsed,
                size_cancelled=delta.size_cancelled,
                size_voided=delta.size_voided,
                placed_at_ms=delta.placed_at_ms,
                last_publish_time_ms=publish_time_ms,
            )
            self._orders[identity] = state
            return state

        if previous.status == "EC" and delta.status == "E":
            raise ValueError("execution-complete Betfair order cannot become executable")
        state = replace(
            previous,
            customer_order_ref=self._merge_immutable(
                previous.customer_order_ref, delta.customer_order_ref, "customerOrderRef"
            ),
            customer_strategy_ref=self._merge_immutable(
                previous.customer_strategy_ref,
                delta.customer_strategy_ref,
                "customerStrategyRef",
            ),
            side=self._merge_immutable(previous.side, delta.side, "side"),
            status=previous.status if delta.status is None else delta.status,
            persistence_type=self._merge_immutable(
                previous.persistence_type,
                delta.persistence_type,
                "persistenceType",
            ),
            order_type=self._merge_immutable(
                previous.order_type,
                delta.order_type,
                "orderType",
            ),
            price=self._merge_immutable(previous.price, delta.price, "price"),
            size=self._merge_immutable(previous.size, delta.size, "size"),
            average_price_matched=(
                previous.average_price_matched
                if delta.average_price_matched is None
                else delta.average_price_matched
            ),
            size_matched=(
                previous.size_matched if delta.size_matched is None else delta.size_matched
            ),
            size_remaining=(
                previous.size_remaining
                if delta.size_remaining is None
                else delta.size_remaining
            ),
            size_lapsed=(
                previous.size_lapsed if delta.size_lapsed is None else delta.size_lapsed
            ),
            size_cancelled=(
                previous.size_cancelled
                if delta.size_cancelled is None
                else delta.size_cancelled
            ),
            size_voided=(
                previous.size_voided if delta.size_voided is None else delta.size_voided
            ),
            placed_at_ms=self._merge_immutable(
                previous.placed_at_ms, delta.placed_at_ms, "placedDate"
            ),
            last_publish_time_ms=publish_time_ms,
        )
        self._orders[identity] = state
        return state

    def _clear_market(self, market_id: str) -> tuple[BetfairOrderIdentity, ...]:
        identities = tuple(
            identity for identity in self._orders if identity.market_id == market_id
        )
        for identity in identities:
            del self._orders[identity]
        return identities

    def apply(self, frame: BetfairOrderChangeFrame) -> BetfairOrderStreamApplyResult:
        if not isinstance(frame, BetfairOrderChangeFrame):
            raise TypeError("frame must be BetfairOrderChangeFrame")
        if (
            self._publish_time_ms is not None
            and frame.publish_time_ms < self._publish_time_ms
        ):
            raise ValueError("Betfair order-stream publish time moved backwards")
        if frame.clk == self._clk:
            if frame.frame_sha256 == self._frame_sha256:
                return BetfairOrderStreamApplyResult(
                    BetfairApplyStatus.DUPLICATE,
                    self.reconnect_cursor(),
                    frame.kind,
                    frame.publish_time_ms,
                    (),
                    (),
                    (),
                )
            raise ValueError("same Betfair order-stream clk has different content")

        if frame.kind is BetfairFrameKind.HEARTBEAT:
            if (
                frame.initial_clk is not None
                and self._initial_clk is not None
                and frame.initial_clk != self._initial_clk
            ):
                raise ValueError("Betfair order-stream initialClk changed on heartbeat")
            if frame.initial_clk is not None:
                self._initial_clk = frame.initial_clk
            self._clk = frame.clk
            self._publish_time_ms = frame.publish_time_ms
            self._frame_sha256 = frame.frame_sha256
            return BetfairOrderStreamApplyResult(
                BetfairApplyStatus.HEARTBEAT,
                self.reconnect_cursor(),
                frame.kind,
                frame.publish_time_ms,
                (),
                (),
                (),
            )

        removed: list[BetfairOrderIdentity] = []
        image_markets: list[str] = []
        if frame.kind is BetfairFrameKind.SUB_IMAGE:
            removed.extend(self._orders)
            self._orders.clear()
            self._initial_clk = frame.initial_clk
            self._initialized = True
        else:
            if not self._initialized:
                raise ValueError("Betfair order delta cannot be applied before SUB_IMAGE")
            if (
                frame.initial_clk is not None
                and self._initial_clk is not None
                and frame.initial_clk != self._initial_clk
            ):
                raise ValueError("Betfair order-stream initialClk conflicts with cache")

        changed: list[BetfairOrderState] = []
        frame_bet_ids: set[str] = set()
        for market in frame.market_changes:
            if market.image:
                image_markets.append(market.market_id)
                removed.extend(self._clear_market(market.market_id))
            for runner in market.runner_changes:
                for delta in runner.unmatched_orders:
                    if delta.bet_id in frame_bet_ids:
                        raise ValueError("order frame contains duplicate betId")
                    frame_bet_ids.add(delta.bet_id)
                    changed.append(
                        self._apply_order(
                            market.market_id,
                            runner,
                            delta,
                            frame.publish_time_ms,
                        )
                    )

        if frame.initial_clk is not None:
            self._initial_clk = frame.initial_clk
        self._clk = frame.clk
        self._publish_time_ms = frame.publish_time_ms
        self._frame_sha256 = frame.frame_sha256
        return BetfairOrderStreamApplyResult(
            BetfairApplyStatus.APPLIED,
            self.reconnect_cursor(),
            frame.kind,
            frame.publish_time_ms,
            tuple(changed),
            tuple(dict.fromkeys(removed)),
            tuple(image_markets),
        )


__all__ = [
    "ORDER_STREAM_FINAL_SETTLEMENT_AUTHORITY",
    "BetfairOrderChangeFrame",
    "BetfairOrderIdentity",
    "BetfairOrderMarketChange",
    "BetfairOrderRunnerChange",
    "BetfairOrderState",
    "BetfairOrderStreamApplyResult",
    "BetfairOrderStreamCursor",
    "BetfairOrderStreamState",
    "BetfairUnmatchedOrderDelta",
    "decode_order_change_message",
]
