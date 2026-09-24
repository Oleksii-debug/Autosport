"""Read-only Betfair Order Stream (OCM) codec and deterministic state cache.

This module preserves provider-side private order truth only. It never submits or
cancels orders, mutates ``RealExecutionLedger``, or grants execution/settlement
authority. Order-change ``uo`` records are full provider replacements keyed by
``betId``. ``customerOrderRef`` is only a non-unique correlation attribute.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .betfair_stream_codec import (
    BetfairApplyStatus,
    BetfairFrameKind,
    BetfairProviderStreamHealth,
)

ORDER_STREAM_FINAL_SETTLEMENT_AUTHORITY = False
_HEX = frozenset("0123456789abcdef")
_REQUIRED_ORDER_FIELDS = frozenset(
    {"id", "p", "s", "side", "status", "ot", "pd", "sm", "sr", "sl", "sc", "sv"}
)


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


def _optional_reference(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, field)


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return text


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _decimal(value: object, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        suffix = "" if minimum is None else f" >= {minimum}"
        raise ValueError(f"{field} must be a finite decimal{suffix}")
    return parsed


def _optional_decimal(
    value: object, field: str, *, minimum: Decimal | None = None
) -> Decimal | None:
    return None if value is None else _decimal(value, field, minimum=minimum)


def _bool_or_false(value: object, field: str) -> bool:
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError(f"{field} must be bool or null")
    return value


def _provider_health(value: object) -> BetfairProviderStreamHealth:
    if value is None:
        return BetfairProviderStreamHealth.UP_TO_DATE
    if type(value) is int and value == 503:
        return BetfairProviderStreamHealth.UNRELIABLE
    raise ValueError(f"unsupported Betfair order-stream status {value!r}")


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
    side: str
    status: str
    persistence_type: str | None
    order_type: str
    price: Decimal
    size: Decimal
    average_price_matched: Decimal | None
    size_matched: Decimal
    size_remaining: Decimal
    size_lapsed: Decimal
    size_cancelled: Decimal
    size_voided: Decimal
    placed_at_ms: int


@dataclass(frozen=True, slots=True)
class BetfairOrderRunnerChange:
    selection_id: int
    handicap: Decimal
    unmatched_orders: tuple[BetfairUnmatchedOrderDelta, ...]
    full_image: bool = False


@dataclass(frozen=True, slots=True)
class BetfairOrderMarketChange:
    market_id: str
    image: bool
    runner_changes: tuple[BetfairOrderRunnerChange, ...]

    @property
    def full_image(self) -> bool:
        return self.image


@dataclass(frozen=True, slots=True)
class BetfairOrderChangeFrame:
    kind: BetfairFrameKind
    initial_clk: str | None
    clk: str
    publish_time_ms: int
    conflated: bool
    market_changes: tuple[BetfairOrderMarketChange, ...]
    frame_sha256: str
    provider_health: BetfairProviderStreamHealth = BetfairProviderStreamHealth.UP_TO_DATE


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
    order_type: str
    price: Decimal
    size: Decimal
    average_price_matched: Decimal | None
    size_matched: Decimal
    size_remaining: Decimal
    size_lapsed: Decimal
    size_cancelled: Decimal
    size_voided: Decimal
    placed_at_ms: int
    last_publish_time_ms: int

    @property
    def accepted_average_price(self) -> Decimal | None:
        if (
            self.average_price_matched is None
            or self.average_price_matched <= 1
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
    provider_health: BetfairProviderStreamHealth = BetfairProviderStreamHealth.UP_TO_DATE


def _order(raw: object) -> BetfairUnmatchedOrderDelta:
    if type(raw) is not dict:
        raise ValueError("unmatched order change must be a JSON object")
    missing = sorted(_REQUIRED_ORDER_FIELDS.difference(raw))
    if missing:
        raise ValueError("full Betfair unmatched order missing fields: " + ",".join(missing))

    bet_id = _text(raw.get("id"), "order.id")
    customer_order_ref = _optional_reference(raw.get("rfo"), "order.rfo")
    customer_strategy_ref = _optional_reference(raw.get("rfs"), "order.rfs")
    side = _text(raw.get("side"), "order.side")
    if side not in {"B", "L"}:
        raise ValueError("order.side must be B or L")
    status = _text(raw.get("status"), "order.status")
    if status not in {"E", "EC"}:
        raise ValueError("order.status must be E or EC")
    persistence_type = _optional_text(raw.get("pt"), "order.pt")
    if persistence_type is not None and persistence_type not in {"L", "P", "MOC"}:
        raise ValueError("order.pt is unsupported")
    order_type = _text(raw.get("ot"), "order.ot")
    if order_type not in {"L", "LOC", "MOC"}:
        raise ValueError("order.ot is unsupported")

    return BetfairUnmatchedOrderDelta(
        bet_id=bet_id,
        customer_order_ref=customer_order_ref,
        customer_strategy_ref=customer_strategy_ref,
        side=side,
        status=status,
        persistence_type=persistence_type,
        order_type=order_type,
        price=_decimal(raw.get("p"), "order.p"),
        size=_decimal(raw.get("s"), "order.s", minimum=Decimal("0")),
        average_price_matched=_optional_decimal(
            raw.get("avp"), "order.avp", minimum=Decimal("0")
        ),
        size_matched=_decimal(raw.get("sm"), "order.sm", minimum=Decimal("0")),
        size_remaining=_decimal(raw.get("sr"), "order.sr", minimum=Decimal("0")),
        size_lapsed=_decimal(raw.get("sl"), "order.sl", minimum=Decimal("0")),
        size_cancelled=_decimal(raw.get("sc"), "order.sc", minimum=Decimal("0")),
        size_voided=_decimal(raw.get("sv"), "order.sv", minimum=Decimal("0")),
        placed_at_ms=_integer(raw.get("pd"), "order.pd"),
    )


def _runner(raw: object) -> BetfairOrderRunnerChange:
    if type(raw) is not dict:
        raise ValueError("order runner change must be a JSON object")
    selection_id = _integer(raw.get("id"), "order_runner.id", minimum=1)
    handicap = _decimal(raw.get("hc", 0), "order_runner.hc")
    full_image = _bool_or_false(raw.get("fullImage"), "order_runner.fullImage")
    orders_raw = raw.get("uo", [])
    if type(orders_raw) is not list:
        raise ValueError("order_runner.uo must be a list")
    orders = tuple(_order(item) for item in orders_raw)
    ids = [item.bet_id for item in orders]
    if len(set(ids)) != len(ids):
        raise ValueError("order_runner.uo contains duplicate betId")
    return BetfairOrderRunnerChange(selection_id, handicap, orders, full_image)


def _market(raw: object) -> BetfairOrderMarketChange:
    if type(raw) is not dict:
        raise ValueError("order market change must be a JSON object")
    market_id = _text(raw.get("id"), "order_market.id")
    full_image = _bool_or_false(raw.get("fullImage"), "order_market.fullImage")
    runners_raw = raw.get("orc", [])
    if type(runners_raw) is not list:
        raise ValueError("order_market.orc must be a list")
    runners = tuple(_runner(item) for item in runners_raw)
    identities = [(item.selection_id, item.handicap) for item in runners]
    if len(set(identities)) != len(identities):
        raise ValueError("order_market.orc contains duplicate runner identity")
    return BetfairOrderMarketChange(market_id, full_image, runners)


def decode_order_change_message(raw: dict[str, Any]) -> BetfairOrderChangeFrame:
    """Decode one private OrderChangeMessage without granting execution authority."""
    if type(raw) is not dict:
        raise TypeError("raw must be a dict")
    if raw.get("op") != "ocm":
        raise ValueError("only Betfair order-change messages are supported")
    if raw.get("segmentType") is not None:
        raise ValueError("segmented Betfair order messages require reassembly before application")

    kinds = {
        None: BetfairFrameKind.DELTA,
        "SUB_IMAGE": BetfairFrameKind.SUB_IMAGE,
        "RESUB_DELTA": BetfairFrameKind.RESUB_DELTA,
        "HEARTBEAT": BetfairFrameKind.HEARTBEAT,
    }
    change_type = raw.get("ct")
    if change_type not in kinds:
        raise ValueError(f"unsupported Betfair order change type {change_type!r}")
    kind = kinds[change_type]
    initial_clk = _optional_text(raw.get("initialClk"), "initialClk")
    clk = _text(raw.get("clk"), "clk")
    publish_time_ms = _integer(raw.get("pt"), "pt")
    conflated = _bool_or_false(raw.get("con"), "con")
    provider_health = _provider_health(raw.get("status"))

    changes_raw = raw.get("oc", [])
    if type(changes_raw) is not list:
        raise ValueError("oc must be a list")
    if kind is BetfairFrameKind.HEARTBEAT and changes_raw:
        raise ValueError("order-stream heartbeat must not contain order changes")
    changes = tuple(_market(item) for item in changes_raw)
    market_ids = [item.market_id for item in changes]
    if len(set(market_ids)) != len(market_ids):
        raise ValueError("order frame contains duplicate market ids")
    if kind in {BetfairFrameKind.SUB_IMAGE, BetfairFrameKind.RESUB_DELTA} and initial_clk is None:
        raise ValueError(f"{kind.value} requires initialClk")

    return BetfairOrderChangeFrame(
        kind=kind,
        initial_clk=initial_clk,
        clk=clk,
        publish_time_ms=publish_time_ms,
        conflated=conflated,
        market_changes=changes,
        frame_sha256=_frame_hash(raw),
        provider_health=provider_health,
    )


class BetfairOrderStreamState:
    """Deterministic trusted OCM cache bound to one private-order subscription."""

    def __init__(self, *, subscription_sha256: str) -> None:
        self._subscription = _sha256(subscription_sha256, "subscription_sha256")
        self._initial_clk: str | None = None
        self._clk: str | None = None
        self._publish_time_ms: int | None = None
        self._frame_sha256: str | None = None
        self._initialized = False
        self._orders: dict[BetfairOrderIdentity, BetfairOrderState] = {}
        self._bet_locations: dict[str, BetfairOrderIdentity] = {}
        self._ref_to_bets: dict[str, set[str]] = {}
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
        return BetfairOrderStreamCursor(self._subscription, self._initial_clk, self._clk)

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

    def bet_ids_for_customer_order_ref(self, customer_order_ref: str) -> tuple[str, ...]:
        customer_order_ref = _text(customer_order_ref, "customer_order_ref")
        return tuple(sorted(self._ref_to_bets.get(customer_order_ref, ())))

    def bet_id_for_customer_order_ref(self, customer_order_ref: str) -> str | None:
        """Return a betId only when this non-unique correlation is unambiguous."""
        bet_ids = self.bet_ids_for_customer_order_ref(customer_order_ref)
        return bet_ids[0] if len(bet_ids) == 1 else None

    def _set_reference(self, bet_id: str, customer_order_ref: str | None) -> None:
        previous = self._bet_to_ref.get(bet_id)
        if previous == customer_order_ref:
            return
        if previous is not None:
            prior_bets = self._ref_to_bets.get(previous)
            if prior_bets is not None:
                prior_bets.discard(bet_id)
                if not prior_bets:
                    del self._ref_to_bets[previous]
            self._bet_to_ref.pop(bet_id, None)
        if customer_order_ref is not None:
            self._ref_to_bets.setdefault(customer_order_ref, set()).add(bet_id)
            self._bet_to_ref[bet_id] = customer_order_ref

    def _remove_identity(self, identity: BetfairOrderIdentity) -> None:
        self._orders.pop(identity, None)
        if self._bet_locations.get(identity.bet_id) == identity:
            self._bet_locations.pop(identity.bet_id, None)
        self._set_reference(identity.bet_id, None)

    def _clear_all(self) -> tuple[BetfairOrderIdentity, ...]:
        identities = tuple(self._orders)
        for identity in identities:
            self._remove_identity(identity)
        return identities

    def _clear_market(self, market_id: str) -> tuple[BetfairOrderIdentity, ...]:
        identities = tuple(identity for identity in self._orders if identity.market_id == market_id)
        for identity in identities:
            self._remove_identity(identity)
        return identities

    def _clear_runner(
        self, market_id: str, selection_id: int, handicap: Decimal
    ) -> tuple[BetfairOrderIdentity, ...]:
        identities = tuple(
            identity
            for identity in self._orders
            if identity.market_id == market_id
            and identity.selection_id == selection_id
            and identity.handicap == handicap
        )
        for identity in identities:
            self._remove_identity(identity)
        return identities

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
        previous_location = self._bet_locations.get(delta.bet_id)
        if previous_location is not None and previous_location != identity:
            raise ValueError("Betfair betId moved to a conflicting market/runner identity")
        previous = self._orders.get(identity)
        if previous is not None and previous.status == "EC" and delta.status == "E":
            raise ValueError("execution-complete Betfair order cannot become executable")

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
        self._bet_locations[delta.bet_id] = identity
        self._set_reference(delta.bet_id, delta.customer_order_ref)
        return state

    def _apply_change(self, frame: BetfairOrderChangeFrame) -> BetfairOrderStreamApplyResult:
        removed: list[BetfairOrderIdentity] = []
        image_markets: list[str] = []
        if frame.kind is BetfairFrameKind.SUB_IMAGE:
            removed.extend(self._clear_all())
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
                if runner.full_image:
                    removed.extend(
                        self._clear_runner(
                            market.market_id, runner.selection_id, runner.handicap
                        )
                    )
                for delta in runner.unmatched_orders:
                    if delta.bet_id in frame_bet_ids:
                        raise ValueError("order frame contains duplicate betId")
                    frame_bet_ids.add(delta.bet_id)
                    changed.append(
                        self._apply_order(
                            market.market_id, runner, delta, frame.publish_time_ms
                        )
                    )

        if frame.initial_clk is not None:
            self._initial_clk = frame.initial_clk
        self._clk = frame.clk
        self._publish_time_ms = frame.publish_time_ms
        self._frame_sha256 = frame.frame_sha256
        return BetfairOrderStreamApplyResult(
            status=BetfairApplyStatus.APPLIED,
            cursor=self.reconnect_cursor(),
            frame_kind=frame.kind,
            publish_time_ms=frame.publish_time_ms,
            changed=tuple(changed),
            removed=tuple(dict.fromkeys(removed)),
            image_replaced_markets=tuple(dict.fromkeys(image_markets)),
            provider_health=frame.provider_health,
        )

    def apply(self, frame: BetfairOrderChangeFrame) -> BetfairOrderStreamApplyResult:
        if not isinstance(frame, BetfairOrderChangeFrame):
            raise TypeError("frame must be BetfairOrderChangeFrame")
        if frame.provider_health is BetfairProviderStreamHealth.UNRELIABLE:
            raise ValueError("Betfair order stream status=503 is non-authoritative")
        if self._publish_time_ms is not None and frame.publish_time_ms < self._publish_time_ms:
            raise ValueError("Betfair order-stream publish time moved backwards")
        if frame.clk == self._clk:
            if frame.frame_sha256 == self._frame_sha256:
                return BetfairOrderStreamApplyResult(
                    status=BetfairApplyStatus.DUPLICATE,
                    cursor=self.reconnect_cursor(),
                    frame_kind=frame.kind,
                    publish_time_ms=frame.publish_time_ms,
                    changed=(),
                    removed=(),
                    image_replaced_markets=(),
                    provider_health=frame.provider_health,
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
                status=BetfairApplyStatus.HEARTBEAT,
                cursor=self.reconnect_cursor(),
                frame_kind=frame.kind,
                publish_time_ms=frame.publish_time_ms,
                changed=(),
                removed=(),
                image_replaced_markets=(),
                provider_health=frame.provider_health,
            )

        before = (
            self._initial_clk,
            self._clk,
            self._publish_time_ms,
            self._frame_sha256,
            self._initialized,
            self._orders.copy(),
            self._bet_locations.copy(),
            {key: set(value) for key, value in self._ref_to_bets.items()},
            self._bet_to_ref.copy(),
        )
        try:
            return self._apply_change(frame)
        except Exception:
            (
                self._initial_clk,
                self._clk,
                self._publish_time_ms,
                self._frame_sha256,
                self._initialized,
                self._orders,
                self._bet_locations,
                self._ref_to_bets,
                self._bet_to_ref,
            ) = before
            raise


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
