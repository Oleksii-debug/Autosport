from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

BETFAIR_STREAM_SOURCE_ID = "betfair_exchange_stream"
_MAX_FRAME_BYTES = 4 * 1024 * 1024


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be UTF-8 encodable") from exc
    return value


def _clock(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _optional_int32(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < -(2**31) or value > (2**31 - 1):
        raise ValueError(f"{field} must be a signed int32 or null")
    return value


def _optional_stream_interval(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    parsed = _int(value, field, minimum=minimum)
    if maximum is not None and parsed > maximum:
        raise ValueError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
    return parsed


def _decimal(value: object, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise ValueError(f"{field} must be a finite decimal >= {minimum}")
    return parsed


def _odds(value: object, field: str) -> Decimal:
    parsed = _decimal(value, field)
    if parsed <= 1:
        raise ValueError(f"{field} must be finite decimal odds > 1")
    return parsed


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


class BetfairCrlfJsonDecoder:
    """Incremental strict UTF-8 and CRLF framing for Betfair stream JSON."""

    def __init__(self, *, max_frame_bytes: int = _MAX_FRAME_BYTES) -> None:
        if type(max_frame_bytes) is not int or max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be a positive integer")
        self._max = max_frame_bytes
        self._buffer = bytearray()
        self._failed = False

    def _require_usable(self) -> None:
        if self._failed:
            raise ValueError(
                "Betfair stream decoder failed; reconnect with a new decoder"
            )

    def feed(self, chunk: bytes) -> tuple[dict[str, Any], ...]:
        if type(chunk) is not bytes:
            raise TypeError("chunk must be bytes")
        self._require_usable()
        if not chunk:
            return ()
        try:
            prior_buffer_len = len(self._buffer)
            self._buffer.extend(chunk)
            decoded: list[dict[str, Any]] = []
            # A successful prior feed cannot leave a complete CRLF delimiter in
            # the buffer. Search only the newly appended suffix, retaining one
            # byte of overlap so a CR/LF pair split across transport reads is
            # still found. This keeps highly fragmented input linear instead of
            # rescanning the complete accumulated frame on every feed().
            search_start = max(0, prior_buffer_len - 1)
            while True:
                end = self._buffer.find(b"\r\n", search_start)
                if end < 0:
                    break
                frame = bytes(self._buffer[:end])
                del self._buffer[: end + 2]
                search_start = 0
                if not frame:
                    raise ValueError("empty Betfair stream frame is not allowed")
                if len(frame) > self._max:
                    raise ValueError("Betfair stream frame exceeds configured maximum size")
                if b"\n" in frame or b"\r" in frame:
                    raise ValueError("Betfair stream frames must use CRLF delimiters")
                try:
                    text = frame.decode("utf-8", errors="strict")
                    raw = json.loads(
                        text,
                        parse_constant=_reject_constant,
                        object_pairs_hook=_strict_object,
                    )
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    RecursionError,
                ) as exc:
                    raise ValueError(
                        "Betfair stream frame must be strict UTF-8 JSON"
                    ) from exc
                if type(raw) is not dict:
                    raise ValueError("Betfair stream frame must contain a JSON object")
                decoded.append(raw)
            if len(self._buffer) > self._max:
                # max_frame_bytes bounds frame payload bytes, not the CRLF
                # delimiter. A terminal CR may therefore be retained one byte
                # beyond the payload limit until the next transport read.
                if not (
                    len(self._buffer) == self._max + 1
                    and self._buffer[-1] == 0x0D
                ):
                    raise ValueError("Betfair stream frame exceeds configured maximum size")
            return tuple(decoded)
        except ValueError:
            self._failed = True
            raise

    def finish(self) -> None:
        self._require_usable()
        if self._buffer:
            self._failed = True
            raise ValueError("truncated Betfair stream frame without CRLF terminator")

@dataclass(frozen=True, slots=True)
class BetfairSegmentBudget:
    """Finite in-memory bounds for one segmented market ChangeMessage."""

    max_segments: int
    max_canonical_bytes: int

    def __post_init__(self) -> None:
        _int(self.max_segments, "max_segments", minimum=1)
        _int(self.max_canonical_bytes, "max_canonical_bytes", minimum=1)


class BetfairMarketChangeSegmentReassembler:
    """Reassemble Betfair market segments atomically before typed decoding.

    Betfair's reference clients accumulate each segment's market-change items and,
    on SEG_END, publish the final segment metadata with the accumulated item list.
    Any sequencing/budget failure poisons this instance so callers must reconnect
    and start with a fresh reassembler instead of guessing at a partial image.
    """

    _SEGMENT_TYPES = frozenset({"SEG_START", "SEG", "SEG_END"})

    def __init__(self, *, budget: BetfairSegmentBudget) -> None:
        if type(budget) is not BetfairSegmentBudget:
            raise TypeError("budget must be BetfairSegmentBudget")
        self._budget = budget
        self._items: list[object] | None = None
        self._segment_count = 0
        self._canonical_bytes = 0
        self._failed = False

    @property
    def pending_segments(self) -> int:
        return self._segment_count

    @property
    def pending_canonical_bytes(self) -> int:
        return self._canonical_bytes

    @property
    def failed(self) -> bool:
        return self._failed

    def _clear_pending(self) -> None:
        self._items = None
        self._segment_count = 0
        self._canonical_bytes = 0

    def _fail(self, message: str) -> None:
        self._clear_pending()
        self._failed = True
        raise ValueError(message)

    def _require_usable(self) -> None:
        if self._failed:
            raise ValueError(
                "Betfair segment reassembler failed; reconnect with a new reassembler"
            )

    @staticmethod
    def _sealed_copy(raw: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            payload = json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            sealed = json.loads(
                payload.decode("utf-8"),
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Betfair segment must contain canonical JSON-compatible values"
            ) from exc
        if type(sealed) is not dict:
            raise ValueError("Betfair segment must contain a JSON object")
        return sealed, len(payload)

    def push(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if type(raw) is not dict:
            raise TypeError("raw must be a dict")
        self._require_usable()

        segment_type = raw.get("segmentType")
        if segment_type is None:
            if self._items is not None:
                self._fail(
                    "non-segmented message arrived before segmented message completed"
                )
            return raw
        if type(segment_type) is not str or segment_type not in self._SEGMENT_TYPES:
            self._fail(f"unsupported Betfair segment type {segment_type!r}")
        if raw.get("op") != "mcm":
            self._fail("market segment reassembler only accepts op='mcm'")

        if segment_type == "SEG_START":
            if self._items is not None:
                self._fail("SEG_START arrived before prior segmented message completed")
            self._items = []
            self._segment_count = 0
            self._canonical_bytes = 0
        elif self._items is None:
            self._fail(f"{segment_type} arrived without SEG_START")

        try:
            sealed, encoded_size = self._sealed_copy(raw)
        except ValueError as exc:
            self._fail(str(exc))
        items = sealed.get("mc", [])
        if type(items) is not list:
            self._fail("segmented market mc must be a list")

        next_count = self._segment_count + 1
        next_bytes = self._canonical_bytes + encoded_size
        if next_count > self._budget.max_segments:
            self._fail("Betfair segmented message exceeds max_segments budget")
        if next_bytes > self._budget.max_canonical_bytes:
            self._fail("Betfair segmented message exceeds max_canonical_bytes budget")

        assert self._items is not None
        self._items.extend(items)
        self._segment_count = next_count
        self._canonical_bytes = next_bytes

        if segment_type != "SEG_END":
            return None

        completed = sealed
        completed.pop("segmentType", None)
        completed["mc"] = self._items
        self._clear_pending()
        return completed


class BetfairQuoteSide(str, Enum):
    BACK = "back"
    LAY = "lay"
    LAST_TRADED = "last_traded"


class BetfairFrameKind(str, Enum):
    SUB_IMAGE = "sub_image"
    DELTA = "delta"
    RESUB_DELTA = "resub_delta"
    HEARTBEAT = "heartbeat"


class BetfairApplyStatus(str, Enum):
    APPLIED = "applied"
    HEARTBEAT = "heartbeat"
    DUPLICATE = "duplicate"


class BetfairProviderStreamHealth(str, Enum):
    UP_TO_DATE = "up_to_date"
    UNRELIABLE = "unreliable_503"


def _provider_stream_health(value: object) -> BetfairProviderStreamHealth:
    if value is None:
        return BetfairProviderStreamHealth.UP_TO_DATE
    if type(value) is int and value == 503:
        return BetfairProviderStreamHealth.UNRELIABLE
    raise ValueError(f"unsupported Betfair stream status {value!r}")


@dataclass(frozen=True, slots=True)
class BetfairQuoteIdentity:
    source_id: str
    market_id: str
    selection_id: int
    handicap: Decimal
    side: BetfairQuoteSide
    price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.source_id != BETFAIR_STREAM_SOURCE_ID:
            raise ValueError("source_id must be the canonical Betfair stream source")
        _text(self.market_id, "market_id")
        _int(self.selection_id, "selection_id", minimum=1)
        if not isinstance(self.handicap, Decimal) or not self.handicap.is_finite():
            raise ValueError("handicap must be a finite Decimal")
        if not isinstance(self.side, BetfairQuoteSide):
            raise TypeError("side must be BetfairQuoteSide")
        if self.side is BetfairQuoteSide.LAST_TRADED:
            if self.price is not None:
                raise ValueError("last-traded identity must not include ladder price")
        elif (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or self.price <= 1
        ):
            raise ValueError("BACK/LAY identity requires finite Decimal odds > 1")


@dataclass(frozen=True, slots=True)
class BetfairPriceSizeDelta:
    price: Decimal
    size: Decimal
    level: int | None = None

    def __post_init__(self) -> None:
        if not self.price.is_finite() or self.price <= 1:
            raise ValueError("price must be finite Decimal odds > 1")
        if not self.size.is_finite() or self.size < 0:
            raise ValueError("size must be a finite non-negative Decimal")
        if self.level is not None and (type(self.level) is not int or self.level < 0):
            raise ValueError("level must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class BetfairRunnerChange:
    selection_id: int
    handicap: Decimal
    last_traded_price: Decimal | None
    available_to_back: tuple[BetfairPriceSizeDelta, ...]
    available_to_lay: tuple[BetfairPriceSizeDelta, ...]


@dataclass(frozen=True, slots=True)
class BetfairMarketChange:
    market_id: str
    image: bool
    runner_changes: tuple[BetfairRunnerChange, ...]
    conflated: bool = False


@dataclass(frozen=True, slots=True)
class BetfairMarketChangeFrame:
    kind: BetfairFrameKind
    initial_clk: str | None
    clk: str | None
    publish_time_ms: int
    conflated: bool
    provider_health: BetfairProviderStreamHealth
    market_changes: tuple[BetfairMarketChange, ...]
    frame_sha256: str
    conflate_ms: int | None = None
    heartbeat_ms: int | None = None
    request_id: int | None = None


@dataclass(frozen=True, slots=True)
class BetfairStreamCursor:
    initial_clk: str
    clk: str


@dataclass(frozen=True, slots=True)
class BetfairQuoteState:
    identity: BetfairQuoteIdentity
    price: Decimal
    size: Decimal | None


@dataclass(frozen=True, slots=True)
class BetfairStreamApplyResult:
    status: BetfairApplyStatus
    cursor: BetfairStreamCursor | None
    frame_kind: BetfairFrameKind
    conflated: bool
    provider_health: BetfairProviderStreamHealth
    publish_time_ms: int
    changed: tuple[BetfairQuoteState, ...]
    removed: tuple[BetfairQuoteIdentity, ...]
    image_replaced_markets: tuple[str, ...]
    conflate_ms: int | None = None
    heartbeat_ms: int | None = None
    request_id: int | None = None


def _frame_hash(raw: dict[str, Any]) -> str:
    try:
        payload = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Betfair frame contains non-canonical JSON values") from exc
    return hashlib.sha256(payload).hexdigest()


def _ladder(
    runner: dict[str, Any], *, direct_field: str, best_field: str
) -> tuple[BetfairPriceSizeDelta, ...]:
    direct = direct_field in runner
    best = best_field in runner
    if direct and best:
        raise ValueError(f"runner cannot contain both {direct_field} and {best_field}")
    if not direct and not best:
        return ()
    field = direct_field if direct else best_field
    rows = runner[field]
    if type(rows) is not list:
        raise ValueError(f"{field} must be a list")
    result: list[BetfairPriceSizeDelta] = []
    seen: set[object] = set()
    for index, row in enumerate(rows):
        if type(row) is not list:
            raise ValueError(f"{field}[{index}] must be a list")
        if direct:
            if len(row) != 2:
                raise ValueError(f"{field}[{index}] must be [price,size]")
            price = _odds(row[0], f"{field}[{index}].price")
            size = _decimal(row[1], f"{field}[{index}].size", minimum=Decimal("0"))
            level = None
            key: object = price
        else:
            if len(row) != 3:
                raise ValueError(f"{field}[{index}] must be [level,price,size]")
            level = _int(row[0], f"{field}[{index}].level")
            size = _decimal(row[2], f"{field}[{index}].size", minimum=Decimal("0"))
            price = Decimal("2") if size == 0 else _odds(row[1], f"{field}[{index}].price")
            key = level
        if key in seen:
            raise ValueError(f"{field} contains duplicate quote identity")
        seen.add(key)
        result.append(BetfairPriceSizeDelta(price, size, level))
    return tuple(result)


def _runner(raw: object) -> BetfairRunnerChange:
    if type(raw) is not dict:
        raise ValueError("runner change must be a JSON object")
    unsupported_display_ladders = sorted(
        field for field in ("bdatb", "bdatl") if field in raw
    )
    if unsupported_display_ladders:
        raise ValueError(
            "virtual display ladder semantics are not supported by this codec: "
            + ",".join(unsupported_display_ladders)
        )
    selection = _int(raw.get("id"), "runner.id", minimum=1)
    handicap = _decimal(raw.get("hc", 0), "runner.hc")
    ltp = _odds(raw["ltp"], "runner.ltp") if "ltp" in raw else None
    return BetfairRunnerChange(
        selection,
        handicap,
        ltp,
        _ladder(raw, direct_field="atb", best_field="batb"),
        _ladder(raw, direct_field="atl", best_field="batl"),
    )


def _market(raw: object) -> BetfairMarketChange:
    if type(raw) is not dict:
        raise ValueError("market change must be a JSON object")
    market_id = _text(raw.get("id"), "market.id")
    image = raw.get("img", False)
    if type(image) is not bool:
        raise ValueError("market.img must be bool")
    conflated = raw.get("con", False)
    if conflated is None:
        conflated = False
    elif type(conflated) is not bool:
        raise ValueError("market.con must be bool or null")
    runners_raw = raw.get("rc", [])
    if type(runners_raw) is not list:
        raise ValueError("market.rc must be a list")
    runners = tuple(_runner(item) for item in runners_raw)
    identities = [(item.selection_id, item.handicap) for item in runners]
    if len(set(identities)) != len(identities):
        raise ValueError("market.rc contains duplicate selection_id/handicap identity")
    return BetfairMarketChange(market_id, image, runners, conflated)


def decode_market_change_message(raw: dict[str, Any]) -> BetfairMarketChangeFrame:
    """Decode one market-change message into Autosport-owned typed values."""

    if type(raw) is not dict:
        raise TypeError("raw must be a dict")
    if raw.get("op") != "mcm":
        raise ValueError("only Betfair market-change messages are supported")
    if raw.get("segmentType") is not None:
        raise ValueError(
            "segmented Betfair change messages require reassembly before codec application"
        )
    ct = raw.get("ct")
    kinds = {
        None: BetfairFrameKind.DELTA,
        "SUB_IMAGE": BetfairFrameKind.SUB_IMAGE,
        "RESUB_DELTA": BetfairFrameKind.RESUB_DELTA,
        "HEARTBEAT": BetfairFrameKind.HEARTBEAT,
    }
    if ct not in kinds:
        raise ValueError(f"unsupported Betfair change type {ct!r}")
    kind = kinds[ct]
    pt = _int(raw.get("pt"), "pt")
    initial = _clock(raw.get("initialClk"), "initialClk")
    clk = _clock(raw.get("clk"), "clk")
    provider_health = _provider_stream_health(raw.get("status"))
    request_id = _optional_int32(raw.get("id"), "id")
    conflate_ms = _optional_stream_interval(raw.get("conflateMs"), "conflateMs")
    heartbeat_ms = _optional_stream_interval(
        raw.get("heartbeatMs"),
        "heartbeatMs",
        minimum=500,
        maximum=30000,
    )
    mc_raw = raw.get("mc", [])
    if type(mc_raw) is not list:
        raise ValueError("mc must be a list")
    if kind is BetfairFrameKind.HEARTBEAT and mc_raw:
        raise ValueError("heartbeat must not contain market changes")
    changes = tuple(_market(item) for item in mc_raw)
    conflated = any(item.conflated for item in changes)
    ids = [item.market_id for item in changes]
    if len(set(ids)) != len(ids):
        raise ValueError("frame contains duplicate market ids")
    if kind is BetfairFrameKind.SUB_IMAGE:
        if initial is None or clk is None:
            raise ValueError("SUB_IMAGE requires initialClk and clk")
        if any(not item.image for item in changes):
            raise ValueError("SUB_IMAGE market changes must declare img=true")
    elif clk is None:
        raise ValueError(f"{kind.value} requires clk")
    return BetfairMarketChangeFrame(
        kind,
        initial,
        clk,
        pt,
        conflated,
        provider_health,
        changes,
        _frame_hash(raw),
        conflate_ms,
        heartbeat_ms,
        request_id,
    )


class BetfairMarketStreamState:
    """Deterministic provider state; Betfair clocks remain opaque resume tokens."""

    def __init__(self, *, source_id: str = BETFAIR_STREAM_SOURCE_ID) -> None:
        if source_id != BETFAIR_STREAM_SOURCE_ID:
            raise ValueError("source_id must be the canonical Betfair stream source")
        self._source = source_id
        self._initial: str | None = None
        self._clk: str | None = None
        self._pt: int | None = None
        self._hash: str | None = None
        self._provider_health: BetfairProviderStreamHealth | None = None
        self._last_unreliable_publish_time_ms: int | None = None
        self._initialized = False
        self._quotes: dict[BetfairQuoteIdentity, BetfairQuoteState] = {}
        self._levels: dict[tuple[str, int, Decimal, BetfairQuoteSide, int], Decimal] = {}

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def provider_health(self) -> BetfairProviderStreamHealth | None:
        return self._provider_health

    @property
    def last_unreliable_publish_time_ms(self) -> int | None:
        return self._last_unreliable_publish_time_ms

    def reconnect_cursor(self) -> BetfairStreamCursor | None:
        if self._initial is None or self._clk is None:
            return None
        return BetfairStreamCursor(self._initial, self._clk)

    def snapshot(self) -> tuple[BetfairQuoteState, ...]:
        def key(item: BetfairQuoteIdentity) -> tuple[str, int, str, str, str]:
            return (
                item.market_id,
                item.selection_id,
                str(item.handicap),
                item.side.value,
                "" if item.price is None else str(item.price),
            )
        return tuple(self._quotes[item] for item in sorted(self._quotes, key=key))

    def _identity(
        self,
        market_id: str,
        runner: BetfairRunnerChange,
        side: BetfairQuoteSide,
        price: Decimal | None = None,
    ) -> BetfairQuoteIdentity:
        return BetfairQuoteIdentity(
            self._source, market_id, runner.selection_id, runner.handicap, side, price
        )

    def _clear_market(self, market_id: str) -> list[BetfairQuoteIdentity]:
        removed = [item for item in self._quotes if item.market_id == market_id]
        for item in removed:
            del self._quotes[item]
        for key in [item for item in self._levels if item[0] == market_id]:
            del self._levels[key]
        return removed

    def _apply_ladder(
        self,
        market_id: str,
        runner: BetfairRunnerChange,
        side: BetfairQuoteSide,
        deltas: tuple[BetfairPriceSizeDelta, ...],
    ) -> tuple[list[BetfairQuoteState], list[BetfairQuoteIdentity]]:
        changed: list[BetfairQuoteState] = []
        removed: list[BetfairQuoteIdentity] = []
        for delta in deltas:
            if delta.level is not None:
                level_key = (
                    market_id, runner.selection_id, runner.handicap, side, delta.level
                )
                prior_price = self._levels.get(level_key)
                if delta.size == 0:
                    if prior_price is not None:
                        prior = self._identity(market_id, runner, side, prior_price)
                        if prior in self._quotes:
                            del self._quotes[prior]
                            removed.append(prior)
                    self._levels.pop(level_key, None)
                    continue
                if prior_price is not None and prior_price != delta.price:
                    prior = self._identity(market_id, runner, side, prior_price)
                    if prior in self._quotes:
                        del self._quotes[prior]
                        removed.append(prior)
                self._levels[level_key] = delta.price
            identity = self._identity(market_id, runner, side, delta.price)
            if delta.size == 0:
                if identity in self._quotes:
                    del self._quotes[identity]
                    removed.append(identity)
                continue
            state = BetfairQuoteState(identity, delta.price, delta.size)
            self._quotes[identity] = state
            changed.append(state)
        return changed, removed

    def apply(self, frame: BetfairMarketChangeFrame) -> BetfairStreamApplyResult:
        if not isinstance(frame, BetfairMarketChangeFrame):
            raise TypeError("frame must be BetfairMarketChangeFrame")
        if self._pt is not None and frame.publish_time_ms < self._pt:
            raise ValueError("Betfair publish time moved backwards")
        if frame.clk is not None and frame.clk == self._clk:
            if frame.frame_sha256 == self._hash:
                return BetfairStreamApplyResult(
                    BetfairApplyStatus.DUPLICATE,
                    self.reconnect_cursor(),
                    frame.kind,
                    frame.conflated,
                    frame.provider_health,
                    frame.publish_time_ms,
                    (),
                    (),
                    (),
                    frame.conflate_ms,
                    frame.heartbeat_ms,
                    frame.request_id,
                )
            raise ValueError("same Betfair clk arrived with different frame content")

        if frame.kind is BetfairFrameKind.HEARTBEAT:
            if frame.initial_clk is not None:
                self._initial = frame.initial_clk
            self._provider_health = frame.provider_health
            if frame.provider_health is BetfairProviderStreamHealth.UNRELIABLE:
                self._last_unreliable_publish_time_ms = frame.publish_time_ms
            self._clk, self._pt, self._hash = (
                frame.clk, frame.publish_time_ms, frame.frame_sha256
            )
            return BetfairStreamApplyResult(
                BetfairApplyStatus.HEARTBEAT,
                self.reconnect_cursor(),
                frame.kind,
                frame.conflated,
                frame.provider_health,
                frame.publish_time_ms,
                (),
                (),
                (),
                frame.conflate_ms,
                frame.heartbeat_ms,
                frame.request_id,
            )

        reset_removed: list[BetfairQuoteIdentity] = []
        if frame.kind is BetfairFrameKind.SUB_IMAGE:
            reset_removed = list(self._quotes)
            self._quotes.clear()
            self._levels.clear()
            self._initial = frame.initial_clk
            self._initialized = True
        else:
            if not self._initialized:
                raise ValueError("Betfair delta cannot be applied before SUB_IMAGE")
            if frame.kind is BetfairFrameKind.RESUB_DELTA and frame.initial_clk is None:
                raise ValueError("RESUB_DELTA requires initialClk for resume binding")

        changed: list[BetfairQuoteState] = []
        removed = reset_removed
        images: list[str] = []
        for market in frame.market_changes:
            if market.image:
                images.append(market.market_id)
                removed.extend(self._clear_market(market.market_id))
            for runner in market.runner_changes:
                if runner.last_traded_price is not None:
                    identity = self._identity(
                        market.market_id, runner, BetfairQuoteSide.LAST_TRADED
                    )
                    state = BetfairQuoteState(identity, runner.last_traded_price, None)
                    self._quotes[identity] = state
                    changed.append(state)
                for side, deltas in (
                    (BetfairQuoteSide.BACK, runner.available_to_back),
                    (BetfairQuoteSide.LAY, runner.available_to_lay),
                ):
                    c, r = self._apply_ladder(market.market_id, runner, side, deltas)
                    changed.extend(c)
                    removed.extend(r)

        if frame.initial_clk is not None:
            self._initial = frame.initial_clk
        self._provider_health = frame.provider_health
        if frame.provider_health is BetfairProviderStreamHealth.UNRELIABLE:
            self._last_unreliable_publish_time_ms = frame.publish_time_ms
        self._clk, self._pt, self._hash = frame.clk, frame.publish_time_ms, frame.frame_sha256
        return BetfairStreamApplyResult(
            BetfairApplyStatus.APPLIED,
            self.reconnect_cursor(),
            frame.kind,
            frame.conflated,
            frame.provider_health,
            frame.publish_time_ms,
            tuple(changed),
            tuple(dict.fromkeys(removed)),
            tuple(images),
            frame.conflate_ms,
            frame.heartbeat_ms,
            frame.request_id,
        )
