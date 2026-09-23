from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from .betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairApplyStatus,
    BetfairFrameKind,
    BetfairMarketStreamState,
    BetfairProviderStreamHealth,
    BetfairQuoteIdentity,
    BetfairQuoteSide,
    BetfairQuoteState,
    BetfairStreamApplyResult,
    decode_market_change_message,
)

BETFAIR_PROVIDER_ID = "betfair"
SCHEMA_VERSION = "betfair-stream-publish-freshness-v1"


class BetfairStreamFreshnessVerdict(str, Enum):
    FRESH_PROVIDER_PUBLISH = "fresh_provider_publish"
    AUTH_CONTEXT_UNPROVEN = "auth_context_unproven"
    STALE = "stale"
    FUTURE = "future"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BetfairStreamSubscriptionContext:
    """Subscription correlation only; not authenticated acquisition authority.

    upstream_context_sha256 is a secret-free correlation digest supplied by the
    upstream composition layer. This projection cannot prove that digest originated
    from an authenticated Betfair transport, so it never makes freshness
    decision-eligible by itself.
    """

    upstream_context_sha256: str
    subscription_id: str
    provider_request_id: int
    subscription_generation: int
    criteria_sha256: str
    market_filter_sha256: str
    market_data_fields: tuple[str, ...]
    ladder_levels: int | None
    requested_heartbeat_ms: int
    requested_conflate_ms: int
    provider_id: str = BETFAIR_PROVIDER_ID
    source_id: str = BETFAIR_STREAM_SOURCE_ID

    def __post_init__(self) -> None:
        for name in ("subscription_id", "provider_id", "source_id"):
            _text(getattr(self, name), name)
        if not _is_sha256(self.upstream_context_sha256):
            raise ValueError("upstream_context_sha256 must be a lowercase SHA-256 hex digest")
        if self.provider_id != BETFAIR_PROVIDER_ID:
            raise ValueError("provider_id must be canonical Betfair provider")
        if self.source_id != BETFAIR_STREAM_SOURCE_ID:
            raise ValueError("source_id must be canonical Betfair stream source")
        if (
            type(self.provider_request_id) is not int
            or self.provider_request_id < 0
            or self.provider_request_id > 2_147_483_647
        ):
            raise ValueError("provider_request_id must be a non-negative signed 32-bit integer")
        if type(self.subscription_generation) is not int or self.subscription_generation < 0:
            raise ValueError("subscription_generation must be a non-negative integer")
        if not _is_sha256(self.criteria_sha256):
            raise ValueError("criteria_sha256 must be a lowercase SHA-256 hex digest")
        _validate_projection(
            self.market_filter_sha256,
            self.market_data_fields,
            self.ladder_levels,
        )
        if type(self.requested_heartbeat_ms) is not int or self.requested_heartbeat_ms <= 0:
            raise ValueError("requested_heartbeat_ms must be a positive integer")
        if type(self.requested_conflate_ms) is not int or self.requested_conflate_ms < 0:
            raise ValueError("requested_conflate_ms must be a non-negative integer")

    @property
    def context_id(self) -> str:
        return _digest(_context_payload(self, include_id=False))


@dataclass(frozen=True, slots=True)
class BetfairStreamFreshnessPolicy:
    max_age_ms: int
    max_future_skew_ms: int = 0

    def __post_init__(self) -> None:
        for name in ("max_age_ms", "max_future_skew_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def policy_id(self) -> str:
        return _digest(
            {
                "version": SCHEMA_VERSION,
                "max_age_ms": self.max_age_ms,
                "max_future_skew_ms": self.max_future_skew_ms,
            }
        )


@dataclass(frozen=True, slots=True)
class BetfairStreamPublicationEvidence:
    quote: BetfairQuoteState
    context_id: str
    subscription_id: str
    provider_request_id: int
    subscription_generation: int
    criteria_sha256: str
    market_filter_sha256: str
    market_data_fields: tuple[str, ...]
    ladder_levels: int | None
    upstream_context_sha256: str
    requested_heartbeat_ms: int
    requested_conflate_ms: int
    provider_heartbeat_ms: int | None
    provider_conflate_ms: int | None
    frame_sha256: str
    frame_kind: BetfairFrameKind
    provider_health: BetfairProviderStreamHealth
    conflated: bool
    publish_time_ms: int
    received_time_ms: int
    ingested_time_ms: int
    cursor_initial_clk: str | None
    cursor_clk: str | None
    evidence_id: str

    def __post_init__(self) -> None:
        if type(self.quote) is not BetfairQuoteState:
            raise TypeError("quote must be canonical BetfairQuoteState")
        if type(self.frame_kind) is not BetfairFrameKind:
            raise TypeError("frame_kind must be canonical BetfairFrameKind")
        if type(self.provider_health) is not BetfairProviderStreamHealth:
            raise TypeError("provider_health must be canonical BetfairProviderStreamHealth")
        if type(self.conflated) is not bool:
            raise TypeError("conflated must be bool")
        for name in ("context_id", "subscription_id", "evidence_id"):
            _text(getattr(self, name), name)
        if not _is_sha256(self.criteria_sha256):
            raise ValueError("criteria_sha256 must be a lowercase SHA-256 hex digest")
        _validate_projection(
            self.market_filter_sha256,
            self.market_data_fields,
            self.ladder_levels,
        )
        if not _is_sha256(self.upstream_context_sha256):
            raise ValueError("upstream_context_sha256 must be a lowercase SHA-256 hex digest")
        if not _is_sha256(self.frame_sha256):
            raise ValueError("frame_sha256 must be a lowercase SHA-256 hex digest")
        if (
            type(self.provider_request_id) is not int
            or self.provider_request_id < 0
            or self.provider_request_id > 2_147_483_647
        ):
            raise ValueError("provider_request_id must be a non-negative signed 32-bit integer")
        if type(self.subscription_generation) is not int or self.subscription_generation < 0:
            raise ValueError("subscription_generation must be non-negative")
        if type(self.requested_heartbeat_ms) is not int or self.requested_heartbeat_ms <= 0:
            raise ValueError("requested_heartbeat_ms must be positive")
        if type(self.requested_conflate_ms) is not int or self.requested_conflate_ms < 0:
            raise ValueError("requested_conflate_ms must be non-negative")
        if self.provider_heartbeat_ms is not None and (
            type(self.provider_heartbeat_ms) is not int or self.provider_heartbeat_ms <= 0
        ):
            raise ValueError("provider_heartbeat_ms must be null or positive")
        if self.provider_conflate_ms is not None and (
            type(self.provider_conflate_ms) is not int or self.provider_conflate_ms < 0
        ):
            raise ValueError("provider_conflate_ms must be null or non-negative")
        _validate_local_times(self.publish_time_ms, self.received_time_ms, self.ingested_time_ms)
        for name in ("cursor_initial_clk", "cursor_clk"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if self.evidence_id != _digest(_evidence_payload(self, include_id=False)):
            raise ValueError("evidence_id does not match canonical publication payload")


@dataclass(frozen=True, slots=True)
class BetfairStreamFreshnessDecision:
    verdict: BetfairStreamFreshnessVerdict
    reason: str
    evidence_id: str | None
    age_ms: int | None
    policy_id: str
    context_id: str | None

    @property
    def decision_eligible(self) -> bool:
        # This module proves provider-publish structure/timing only. Until a separate
        # product-owned authenticated stream-acquisition witness is composed and
        # re-resolved, no public DTO produced or constructed here can authorize a
        # decision merely by carrying a fresh-looking verdict.
        return False


class _BetfairStreamPublishFreshnessTracker:
    def __init__(self, context: BetfairStreamSubscriptionContext) -> None:
        if type(context) is not BetfairStreamSubscriptionContext:
            raise TypeError("context must be BetfairStreamSubscriptionContext")
        self.context = context
        self.records: dict[BetfairQuoteIdentity, BetfairStreamPublicationEvidence] = {}
        self.restart_quarantine: set[BetfairQuoteIdentity] = set()
        self.last_publish_time_ms: int | None = None
        self.provider_heartbeat_ms: int | None = None
        self.provider_conflate_ms: int | None = None

    def replace_subscription(self, context: BetfairStreamSubscriptionContext) -> None:
        if type(context) is not BetfairStreamSubscriptionContext:
            raise TypeError("context must be BetfairStreamSubscriptionContext")
        if context.upstream_context_sha256 != self.context.upstream_context_sha256:
            raise ValueError("upstream correlation context replacement requires a new runtime")
        if context.subscription_generation <= self.context.subscription_generation:
            raise ValueError("replacement subscription generation must strictly advance")
        if context.provider_request_id == self.context.provider_request_id:
            raise ValueError("replacement subscription must use a new provider request id")
        self.context = context
        self.records.clear()
        self.restart_quarantine.clear()
        self.last_publish_time_ms = None
        self.provider_heartbeat_ms = None
        self.provider_conflate_ms = None

    def observe(
        self,
        result: BetfairStreamApplyResult,
        *,
        frame_sha256: str,
        received_time_ms: int,
        ingested_time_ms: int,
    ) -> tuple[BetfairStreamPublicationEvidence, ...]:
        if type(result) is not BetfairStreamApplyResult:
            raise TypeError("result must be canonical BetfairStreamApplyResult")
        if type(result.status) is not BetfairApplyStatus:
            raise TypeError("result.status must be canonical BetfairApplyStatus")
        if type(result.frame_kind) is not BetfairFrameKind:
            raise TypeError("result.frame_kind must be canonical BetfairFrameKind")
        if type(result.provider_health) is not BetfairProviderStreamHealth:
            raise TypeError("result.provider_health must be canonical BetfairProviderStreamHealth")
        if type(result.conflated) is not bool:
            raise TypeError("result.conflated must be bool")
        if result.request_id != self.context.provider_request_id:
            raise ValueError("provider request id does not match active subscription")
        if not _is_sha256(frame_sha256):
            raise ValueError("frame_sha256 must be the canonical frame digest")
        _validate_local_times(result.publish_time_ms, received_time_ms, ingested_time_ms)
        if self.last_publish_time_ms is not None and result.publish_time_ms < self.last_publish_time_ms:
            raise ValueError("provider publish time regressed")
        self.last_publish_time_ms = result.publish_time_ms

        prior_timing = (self.provider_heartbeat_ms, self.provider_conflate_ms)
        if result.heartbeat_ms is not None:
            self.provider_heartbeat_ms = result.heartbeat_ms
        if result.conflate_ms is not None:
            self.provider_conflate_ms = result.conflate_ms
        current_timing = (self.provider_heartbeat_ms, self.provider_conflate_ms)
        if self.records and current_timing != prior_timing:
            # A changed/first-observed server timing contract invalidates prior
            # per-datum timing authority. Require a fresh quote under the new
            # provider-reported configuration instead of laundering old age.
            self.records.clear()
            self.restart_quarantine.clear()

        changed = _changed(result.changed)
        removed = _removed(result.removed)
        replaced = _markets(result.image_replaced_markets)

        if result.provider_health is BetfairProviderStreamHealth.UNRELIABLE:
            self.records.clear()
            self.restart_quarantine.clear()
            return ()

        if result.status in {BetfairApplyStatus.HEARTBEAT, BetfairApplyStatus.DUPLICATE}:
            if changed or removed or replaced:
                raise ValueError("heartbeat/duplicate apply result cannot contain quote mutations")
            return ()
        if result.status is not BetfairApplyStatus.APPLIED:
            raise ValueError("unsupported apply status")

        for identity in tuple(self.records):
            if identity.market_id in replaced:
                self.records.pop(identity, None)
                self.restart_quarantine.discard(identity)
        for identity in removed:
            self.records.pop(identity, None)
            self.restart_quarantine.discard(identity)

        if result.conflated:
            for quote in changed:
                self.records.pop(quote.identity, None)
                self.restart_quarantine.discard(quote.identity)
            return ()

        issued = []
        for quote in changed:
            record = _record(
                self.context,
                result,
                quote,
                frame_sha256,
                received_time_ms,
                ingested_time_ms,
                provider_heartbeat_ms=self.provider_heartbeat_ms,
                provider_conflate_ms=self.provider_conflate_ms,
            )
            self.records[quote.identity] = record
            self.restart_quarantine.discard(quote.identity)
            issued.append(record)
        return tuple(issued)

    def evaluate(
        self,
        identity: BetfairQuoteIdentity,
        *,
        as_of_ms: int,
        policy: BetfairStreamFreshnessPolicy,
    ) -> BetfairStreamFreshnessDecision:
        if type(policy) is not BetfairStreamFreshnessPolicy:
            raise TypeError("policy must be BetfairStreamFreshnessPolicy")
        if type(identity) is not BetfairQuoteIdentity:
            return _unknown("identity is not canonical BetfairQuoteIdentity", policy, None)
        if type(as_of_ms) is not int or as_of_ms < 0:
            return _unknown("as_of_ms must be a non-negative integer", policy, None)
        record = self.records.get(identity)
        if record is None:
            return _unknown("no current product-issued provider publication evidence", policy, None)
        if identity in self.restart_quarantine:
            return _unknown("persisted publication requires provider resynchronization after restart", policy, record)
        if record.context_id != self.context.context_id:
            return _unknown("publication belongs to a replaced subscription", policy, record)
        if record.provider_health is not BetfairProviderStreamHealth.UP_TO_DATE or record.conflated:
            return _unknown("publication is not exact healthy provider-publish evidence", policy, record)
        if self.provider_heartbeat_ms is None or self.provider_conflate_ms is None:
            return _unknown("provider-reported stream timing is unavailable", policy, record)
        if (
            record.provider_heartbeat_ms != self.provider_heartbeat_ms
            or record.provider_conflate_ms != self.provider_conflate_ms
        ):
            return _unknown("provider stream timing changed after this publication", policy, record)
        if self.provider_conflate_ms > policy.max_age_ms:
            return _unknown("provider-reported conflation exceeds the allowed freshness window", policy, record)
        if record.received_time_ms > record.ingested_time_ms or record.ingested_time_ms > as_of_ms:
            return _unknown("publication was not causally available by decision time", policy, record)
        if record.publish_time_ms > record.received_time_ms + policy.max_future_skew_ms:
            return _decision(BetfairStreamFreshnessVerdict.FUTURE, "provider publish time exceeds receive-time clock skew", policy, record)
        if record.publish_time_ms > as_of_ms + policy.max_future_skew_ms:
            return _decision(BetfairStreamFreshnessVerdict.FUTURE, "provider publish time exceeds decision-time clock skew", policy, record)
        age = max(0, as_of_ms - record.publish_time_ms)
        if age > policy.max_age_ms:
            return _decision(BetfairStreamFreshnessVerdict.STALE, "provider publication exceeds max_age_ms", policy, record, age)
        return _decision(
            BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN,
            "provider publication is structurally fresh but authenticated stream acquisition is unproven",
            policy,
            record,
            age,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "context": _context_payload(self.context, include_id=True),
            "last_publish_time_ms": self.last_publish_time_ms,
            "records": [
                _evidence_payload(record, include_id=True)
                for _, record in sorted(self.records.items(), key=lambda row: _identity_key(row[0]))
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> _BetfairStreamPublishFreshnessTracker:
        if type(payload) is not dict or payload.get("version") != SCHEMA_VERSION:
            raise ValueError("unsupported tracker payload/version")
        context = _context_from_payload(payload.get("context"))
        tracker = cls(context)
        last_pt = payload.get("last_publish_time_ms")
        if last_pt is not None and (type(last_pt) is not int or last_pt < 0):
            raise ValueError("last_publish_time_ms must be null or non-negative integer")
        rows = payload.get("records")
        if type(rows) is not list:
            raise ValueError("records must be a list")
        for raw in rows:
            record = _evidence_from_payload(raw)
            if (
                record.context_id != context.context_id
                or record.subscription_generation != context.subscription_generation
                or record.criteria_sha256 != context.criteria_sha256
                or record.market_filter_sha256 != context.market_filter_sha256
                or record.market_data_fields != context.market_data_fields
                or record.ladder_levels != context.ladder_levels
                or record.upstream_context_sha256 != context.upstream_context_sha256
                or record.provider_request_id != context.provider_request_id
                or record.requested_heartbeat_ms != context.requested_heartbeat_ms
                or record.requested_conflate_ms != context.requested_conflate_ms
            ):
                raise ValueError("publication record does not belong to active context")
            if record.quote.identity in tracker.records:
                raise ValueError("duplicate quote identity in tracker payload")
            tracker.records[record.quote.identity] = record
            tracker.restart_quarantine.add(record.quote.identity)
        max_pt = max((row.publish_time_ms for row in tracker.records.values()), default=None)
        if max_pt is not None and last_pt is None:
            raise ValueError("stored publications require last_publish_time_ms")
        if last_pt is not None and max_pt is not None and last_pt < max_pt:
            raise ValueError("last_publish_time_ms precedes stored publication")
        tracker.last_publish_time_ms = last_pt
        return tracker


class BetfairStreamPublishFreshnessRuntime:
    """Raw stream-shaped message -> canonical #858 codec/state -> freshness projection.

    The raw public seam is intentionally assertion-only for authentication: it can
    prove canonical message semantics, provider request correlation and timing, but
    not that bytes/timestamps came from the authenticated network transport. Hence
    this layer remains non-decision-eligible until upstream acquisition authority is
    composed elsewhere.
    """

    def __init__(self, context: BetfairStreamSubscriptionContext) -> None:
        if type(context) is not BetfairStreamSubscriptionContext:
            raise TypeError("context must be BetfairStreamSubscriptionContext")
        self._state = BetfairMarketStreamState(source_id=BETFAIR_STREAM_SOURCE_ID)
        self._tracker = _BetfairStreamPublishFreshnessTracker(context)

    @property
    def context(self) -> BetfairStreamSubscriptionContext:
        return self._tracker.context

    def replace_subscription(self, context: BetfairStreamSubscriptionContext) -> None:
        self._tracker.replace_subscription(context)
        self._state = BetfairMarketStreamState(source_id=BETFAIR_STREAM_SOURCE_ID)

    def ingest_raw(
        self,
        raw_message: dict[str, Any],
        *,
        received_time_ms: int,
        ingested_time_ms: int,
    ) -> tuple[BetfairStreamPublicationEvidence, ...]:
        if type(raw_message) is not dict:
            raise TypeError("raw_message must be a dict from the authenticated stream transport")
        frame = decode_market_change_message(raw_message)
        # Correlation must be checked before mutating canonical market state. A late
        # message from a retired subscription must have zero state/evidence effect.
        if frame.request_id is None:
            raise ValueError("Betfair market-change message lacks provider request id")
        if frame.request_id != self.context.provider_request_id:
            raise ValueError("Betfair market-change message belongs to another subscription")
        result = self._state.apply(frame)
        return self._tracker.observe(
            result,
            frame_sha256=frame.frame_sha256,
            received_time_ms=received_time_ms,
            ingested_time_ms=ingested_time_ms,
        )

    def resolve(self, identity: BetfairQuoteIdentity) -> BetfairStreamPublicationEvidence | None:
        if type(identity) is not BetfairQuoteIdentity:
            raise TypeError("identity must be canonical BetfairQuoteIdentity")
        return self._tracker.records.get(identity)

    def evaluate(
        self,
        identity: BetfairQuoteIdentity,
        *,
        as_of_ms: int,
        policy: BetfairStreamFreshnessPolicy,
    ) -> BetfairStreamFreshnessDecision:
        return self._tracker.evaluate(identity, as_of_ms=as_of_ms, policy=policy)

    def to_dict(self) -> dict[str, Any]:
        return self._tracker.to_dict()

    @classmethod
    def from_dict(cls, payload: object) -> BetfairStreamPublishFreshnessRuntime:
        tracker = _BetfairStreamPublishFreshnessTracker.from_dict(payload)
        runtime = cls(tracker.context)
        runtime._tracker = tracker
        # #858 currently has no durable cache-state restore contract. Persisted rows
        # therefore remain audit evidence but are quarantined from positive freshness
        # until a new canonical image/update re-establishes each datum.
        return runtime


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty trimmed string")
    value.encode("utf-8", errors="strict")
    return value


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_projection(
    market_filter_sha256: object,
    market_data_fields: object,
    ladder_levels: object,
) -> None:
    if not _is_sha256(market_filter_sha256):
        raise ValueError("market_filter_sha256 must be a lowercase SHA-256 hex digest")
    if type(market_data_fields) is not tuple or not market_data_fields:
        raise ValueError("market_data_fields must be a non-empty canonical tuple")
    fields = tuple(_text(field, "market_data_field") for field in market_data_fields)
    if fields != tuple(sorted(set(fields))):
        raise ValueError("market_data_fields must be sorted and contain no duplicates")
    best_offer_projection = any(
        field in {"EX_BEST_OFFERS", "EX_BEST_OFFERS_DISP"}
        for field in fields
    )
    if ladder_levels is None:
        if best_offer_projection:
            raise ValueError(
                "ladder_levels is required for EX_BEST_OFFERS/EX_BEST_OFFERS_DISP"
            )
        return
    if (
        type(ladder_levels) is not int
        or ladder_levels < 1
        or ladder_levels > 10
    ):
        raise ValueError("ladder_levels must be null or an integer in 1..10")
    if not best_offer_projection:
        raise ValueError(
            "ladder_levels is only authoritative for best-offer stream projections"
        )


def _digest(payload: object) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _validate_local_times(publish: object, received: object, ingested: object) -> None:
    for name, value in (("publish_time_ms", publish), ("received_time_ms", received), ("ingested_time_ms", ingested)):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if received > ingested:
        raise ValueError("local receive time cannot be after ingest time")


def _context_payload(context: BetfairStreamSubscriptionContext, *, include_id: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "upstream_context_sha256": context.upstream_context_sha256,
        "subscription_id": context.subscription_id,
        "provider_request_id": context.provider_request_id,
        "subscription_generation": context.subscription_generation,
        "criteria_sha256": context.criteria_sha256,
        "market_filter_sha256": context.market_filter_sha256,
        "market_data_fields": list(context.market_data_fields),
        "ladder_levels": context.ladder_levels,
        "requested_heartbeat_ms": context.requested_heartbeat_ms,
        "requested_conflate_ms": context.requested_conflate_ms,
        "provider_id": context.provider_id,
        "source_id": context.source_id,
    }
    if include_id:
        payload["context_id"] = context.context_id
    return payload


def _context_from_payload(raw: object) -> BetfairStreamSubscriptionContext:
    if type(raw) is not dict or raw.get("version") != SCHEMA_VERSION:
        raise ValueError("invalid subscription context payload")
    raw_fields = raw.get("market_data_fields")
    if type(raw_fields) is not list:
        raise ValueError("market_data_fields must be a JSON array")
    context = BetfairStreamSubscriptionContext(
        upstream_context_sha256=raw.get("upstream_context_sha256"),
        subscription_id=raw.get("subscription_id"),
        provider_request_id=raw.get("provider_request_id"),
        subscription_generation=raw.get("subscription_generation"),
        criteria_sha256=raw.get("criteria_sha256"),
        market_filter_sha256=raw.get("market_filter_sha256"),
        market_data_fields=tuple(raw_fields),
        ladder_levels=raw.get("ladder_levels"),
        requested_heartbeat_ms=raw.get("requested_heartbeat_ms"),
        requested_conflate_ms=raw.get("requested_conflate_ms"),
        provider_id=raw.get("provider_id"),
        source_id=raw.get("source_id"),
    )
    if raw.get("context_id") != context.context_id:
        raise ValueError("context_id does not match canonical context payload")
    return context


def _identity_payload(identity: BetfairQuoteIdentity) -> dict[str, object]:
    return {
        "source_id": identity.source_id,
        "market_id": identity.market_id,
        "selection_id": identity.selection_id,
        "handicap": str(identity.handicap),
        "side": identity.side.value,
        "price": None if identity.price is None else str(identity.price),
    }


def _quote_payload(quote: BetfairQuoteState) -> dict[str, object]:
    return {
        "identity": _identity_payload(quote.identity),
        "price": str(quote.price),
        "size": None if quote.size is None else str(quote.size),
    }


def _evidence_payload(record: BetfairStreamPublicationEvidence, *, include_id: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "quote": _quote_payload(record.quote),
        "context_id": record.context_id,
        "subscription_id": record.subscription_id,
        "provider_request_id": record.provider_request_id,
        "subscription_generation": record.subscription_generation,
        "criteria_sha256": record.criteria_sha256,
        "market_filter_sha256": record.market_filter_sha256,
        "market_data_fields": list(record.market_data_fields),
        "ladder_levels": record.ladder_levels,
        "upstream_context_sha256": record.upstream_context_sha256,
        "requested_heartbeat_ms": record.requested_heartbeat_ms,
        "requested_conflate_ms": record.requested_conflate_ms,
        "provider_heartbeat_ms": record.provider_heartbeat_ms,
        "provider_conflate_ms": record.provider_conflate_ms,
        "frame_sha256": record.frame_sha256,
        "frame_kind": record.frame_kind.value,
        "provider_health": record.provider_health.value,
        "conflated": record.conflated,
        "publish_time_ms": record.publish_time_ms,
        "received_time_ms": record.received_time_ms,
        "ingested_time_ms": record.ingested_time_ms,
        "cursor_initial_clk": record.cursor_initial_clk,
        "cursor_clk": record.cursor_clk,
    }
    if include_id:
        payload["evidence_id"] = record.evidence_id
    return payload


def _record(
    context: BetfairStreamSubscriptionContext,
    result: BetfairStreamApplyResult,
    quote: BetfairQuoteState,
    frame_sha256: str,
    received_time_ms: int,
    ingested_time_ms: int,
    *,
    provider_heartbeat_ms: int | None,
    provider_conflate_ms: int | None,
) -> BetfairStreamPublicationEvidence:
    cursor = result.cursor
    kwargs = dict(
        quote=quote,
        context_id=context.context_id,
        subscription_id=context.subscription_id,
        provider_request_id=context.provider_request_id,
        subscription_generation=context.subscription_generation,
        criteria_sha256=context.criteria_sha256,
        market_filter_sha256=context.market_filter_sha256,
        market_data_fields=context.market_data_fields,
        ladder_levels=context.ladder_levels,
        upstream_context_sha256=context.upstream_context_sha256,
        requested_heartbeat_ms=context.requested_heartbeat_ms,
        requested_conflate_ms=context.requested_conflate_ms,
        provider_heartbeat_ms=provider_heartbeat_ms,
        provider_conflate_ms=provider_conflate_ms,
        frame_sha256=frame_sha256,
        frame_kind=result.frame_kind,
        provider_health=result.provider_health,
        conflated=result.conflated,
        publish_time_ms=result.publish_time_ms,
        received_time_ms=received_time_ms,
        ingested_time_ms=ingested_time_ms,
        cursor_initial_clk=None if cursor is None else cursor.initial_clk,
        cursor_clk=None if cursor is None else cursor.clk,
    )
    payload = {
        "version": SCHEMA_VERSION,
        "quote": _quote_payload(quote),
        "context_id": context.context_id,
        "subscription_id": context.subscription_id,
        "provider_request_id": context.provider_request_id,
        "subscription_generation": context.subscription_generation,
        "criteria_sha256": context.criteria_sha256,
        "market_filter_sha256": context.market_filter_sha256,
        "market_data_fields": list(context.market_data_fields),
        "ladder_levels": context.ladder_levels,
        "upstream_context_sha256": context.upstream_context_sha256,
        "requested_heartbeat_ms": context.requested_heartbeat_ms,
        "requested_conflate_ms": context.requested_conflate_ms,
        "provider_heartbeat_ms": provider_heartbeat_ms,
        "provider_conflate_ms": provider_conflate_ms,
        "frame_sha256": frame_sha256,
        "frame_kind": result.frame_kind.value,
        "provider_health": result.provider_health.value,
        "conflated": result.conflated,
        "publish_time_ms": result.publish_time_ms,
        "received_time_ms": received_time_ms,
        "ingested_time_ms": ingested_time_ms,
        "cursor_initial_clk": kwargs["cursor_initial_clk"],
        "cursor_clk": kwargs["cursor_clk"],
    }
    return BetfairStreamPublicationEvidence(**kwargs, evidence_id=_digest(payload))


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid {field}")
    return parsed


def _identity_from_payload(raw: object) -> BetfairQuoteIdentity:
    if type(raw) is not dict:
        raise ValueError("identity must be a JSON object")
    try:
        identity = BetfairQuoteIdentity(
            source_id=raw.get("source_id"),
            market_id=raw.get("market_id"),
            selection_id=raw.get("selection_id"),
            handicap=_decimal(raw.get("handicap"), "handicap"),
            side=BetfairQuoteSide(raw.get("side")),
            price=None if raw.get("price") is None else _decimal(raw.get("price"), "identity price"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid quote identity") from exc
    if _identity_payload(identity) != raw:
        raise ValueError("quote identity is not canonical")
    return identity


def _quote_from_payload(raw: object) -> BetfairQuoteState:
    if type(raw) is not dict:
        raise ValueError("quote must be a JSON object")
    identity = _identity_from_payload(raw.get("identity"))
    price = _decimal(raw.get("price"), "quote price")
    size = None if raw.get("size") is None else _decimal(raw.get("size"), "quote size")
    if price <= 1 or (size is not None and size < 0):
        raise ValueError("invalid quote values")
    quote = BetfairQuoteState(identity, price, size)
    if _quote_payload(quote) != raw:
        raise ValueError("quote payload is not canonical")
    return quote


def _evidence_from_payload(raw: object) -> BetfairStreamPublicationEvidence:
    if type(raw) is not dict or raw.get("version") != SCHEMA_VERSION:
        raise ValueError("invalid publication record")
    raw_fields = raw.get("market_data_fields")
    if type(raw_fields) is not list:
        raise ValueError("invalid publication record")
    try:
        record = BetfairStreamPublicationEvidence(
            quote=_quote_from_payload(raw.get("quote")),
            context_id=raw.get("context_id"),
            subscription_id=raw.get("subscription_id"),
            provider_request_id=raw.get("provider_request_id"),
            subscription_generation=raw.get("subscription_generation"),
            criteria_sha256=raw.get("criteria_sha256"),
            market_filter_sha256=raw.get("market_filter_sha256"),
            market_data_fields=tuple(raw_fields),
            ladder_levels=raw.get("ladder_levels"),
            upstream_context_sha256=raw.get("upstream_context_sha256"),
            requested_heartbeat_ms=raw.get("requested_heartbeat_ms"),
            requested_conflate_ms=raw.get("requested_conflate_ms"),
            provider_heartbeat_ms=raw.get("provider_heartbeat_ms"),
            provider_conflate_ms=raw.get("provider_conflate_ms"),
            frame_sha256=raw.get("frame_sha256"),
            frame_kind=BetfairFrameKind(raw.get("frame_kind")),
            provider_health=BetfairProviderStreamHealth(raw.get("provider_health")),
            conflated=raw.get("conflated"),
            publish_time_ms=raw.get("publish_time_ms"),
            received_time_ms=raw.get("received_time_ms"),
            ingested_time_ms=raw.get("ingested_time_ms"),
            cursor_initial_clk=raw.get("cursor_initial_clk"),
            cursor_clk=raw.get("cursor_clk"),
            evidence_id=raw.get("evidence_id"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid publication record") from exc
    if _evidence_payload(record, include_id=True) != raw:
        raise ValueError("publication record is not canonical")
    return record


def _changed(value: object) -> tuple[BetfairQuoteState, ...]:
    if type(value) is not tuple:
        raise ValueError("changed must be a tuple")
    result: list[BetfairQuoteState] = []
    seen: set[BetfairQuoteIdentity] = set()
    for quote in value:
        if type(quote) is not BetfairQuoteState or quote.identity in seen:
            raise ValueError("changed contains non-canonical or duplicate quote")
        seen.add(quote.identity)
        result.append(quote)
    return tuple(result)


def _removed(value: object) -> tuple[BetfairQuoteIdentity, ...]:
    if type(value) is not tuple:
        raise ValueError("removed must be a tuple")
    result: list[BetfairQuoteIdentity] = []
    seen: set[BetfairQuoteIdentity] = set()
    for identity in value:
        if type(identity) is not BetfairQuoteIdentity or identity in seen:
            raise ValueError("removed contains non-canonical or duplicate identity")
        seen.add(identity)
        result.append(identity)
    return tuple(result)


def _markets(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError("image_replaced_markets must be a tuple")
    result = tuple(_text(market, "image_replaced_market") for market in value)
    if len(set(result)) != len(result):
        raise ValueError("image_replaced_markets contains duplicates")
    return result


def _identity_key(identity: BetfairQuoteIdentity) -> tuple[str, int, str, str, str]:
    return (
        identity.market_id,
        identity.selection_id,
        str(identity.handicap),
        identity.side.value,
        "" if identity.price is None else str(identity.price),
    )


def _decision(
    verdict: BetfairStreamFreshnessVerdict,
    reason: str,
    policy: BetfairStreamFreshnessPolicy,
    record: BetfairStreamPublicationEvidence,
    age: int | None = None,
) -> BetfairStreamFreshnessDecision:
    return BetfairStreamFreshnessDecision(verdict, reason, record.evidence_id, age, policy.policy_id, record.context_id)


def _unknown(
    reason: str,
    policy: BetfairStreamFreshnessPolicy,
    record: BetfairStreamPublicationEvidence | None,
) -> BetfairStreamFreshnessDecision:
    return BetfairStreamFreshnessDecision(
        BetfairStreamFreshnessVerdict.UNKNOWN,
        reason,
        None if record is None else record.evidence_id,
        None,
        policy.policy_id,
        None if record is None else record.context_id,
    )
