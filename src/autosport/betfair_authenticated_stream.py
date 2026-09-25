"""Authenticated Betfair Market Stream subscription/freshness composition.

This module reuses the canonical TLS/auth transport, strict Stream codec, and per-datum
freshness projection. It adds only the missing product-owned composition: issue one
bounded read-only marketSubscription on the authenticated socket, require provider
SUCCESS on that same connection, consume only transport-issued frames, and promote
otherwise-fresh structural evidence only while the exact acknowledged subscription and
connection remain authoritative in this process.

It does not add betting writes, order-stream actions, durable-persistence authority,
account attestation, settlement, execution, or real-money authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import time
from typing import Any
from weakref import WeakKeyDictionary

from .betfair_stream_codec import BetfairCrlfJsonDecoder, BetfairQuoteIdentity
from .betfair_stream_publish_freshness import (
    BetfairStreamFreshnessPolicy,
    BetfairStreamFreshnessVerdict,
    BetfairStreamPublicationEvidence,
    BetfairStreamPublishFreshnessRuntime,
    BetfairStreamSubscriptionContext,
)
from .betfair_stream_transport import (
    BetfairStreamAuthenticatedFrame,
    BetfairStreamTlsTransport,
)

_SCHEMA = "autosport.betfair_authenticated_stream_subscription.v1"
_MAX_SUBSCRIPTION_BYTES = 64 * 1024
_SECRET_KEY_FRAGMENTS = (
    "password",
    "secret",
    "sessiontoken",
    "session_token",
    "appkey",
    "app_key",
)


class BetfairAuthenticatedStreamError(RuntimeError):
    """Fail-closed authenticated Stream composition error."""


class BetfairAuthenticatedFreshnessVerdict(str, Enum):
    FRESH_AUTHENTICATED_PROVIDER_PUBLISH = "fresh_authenticated_provider_publish"
    NOT_AUTHORIZED = "not_authorized"


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class BetfairAuthenticatedMarketSubscription:
    """Process-local proof that this transport sent and Betfair acknowledged a subscription."""

    connection_id: str
    connection_generation: int
    provider_request_id: int
    request_sha256: str
    acknowledgement_frame_sha256: str
    market_filter_sha256: str
    market_data_fields: tuple[str, ...]
    ladder_levels: int | None
    requested_heartbeat_ms: int
    requested_conflate_ms: int
    subscription_id: str
    upstream_context_sha256: str

    def assert_issued(self) -> None:
        expected = _ISSUED_SUBSCRIPTIONS.get(self)
        if expected is None or expected != _subscription_fingerprint(self):
            raise BetfairAuthenticatedStreamError(
                "Betfair market subscription was not issued by authenticated product composition"
            )

    @property
    def grants_provider_write_authority(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def real_money_authorized(self) -> bool:
        return False


_ISSUED_SUBSCRIPTIONS: WeakKeyDictionary[BetfairAuthenticatedMarketSubscription, str] = (
    WeakKeyDictionary()
)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class BetfairAuthenticatedFreshnessDecision:
    verdict: BetfairAuthenticatedFreshnessVerdict
    reason: str
    evidence_id: str | None
    subscription_id: str
    transport_frame_sha256: str | None
    evaluated_at_ms: int

    @property
    def decision_eligible(self) -> bool:
        if (
            self.verdict
            is not BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
        ):
            return False
        expected = _ISSUED_DECISIONS.get(self)
        return expected is not None and expected == _decision_fingerprint(self)

    @property
    def grants_provider_write_authority(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def real_money_authorized(self) -> bool:
        return False


_ISSUED_DECISIONS: WeakKeyDictionary[BetfairAuthenticatedFreshnessDecision, str] = (
    WeakKeyDictionary()
)


def open_authenticated_market_subscription(
    transport: BetfairStreamTlsTransport,
    *,
    provider_request_id: int,
    market_filter: dict[str, Any],
    market_data_fields: tuple[str, ...],
    ladder_levels: int | None,
    heartbeat_ms: int,
    conflate_ms: int,
) -> BetfairAuthenticatedMarketSubscription:
    """Send and positively acknowledge one read-only Market Stream subscription."""

    if type(transport) is not BetfairStreamTlsTransport:
        raise TypeError("transport must be canonical BetfairStreamTlsTransport")
    if not transport.is_authenticated or not transport.connection_id:
        raise BetfairAuthenticatedStreamError("Betfair Stream transport is not authenticated")
    if type(provider_request_id) is not int or not (
        2 <= provider_request_id <= 2_147_483_647
    ):
        raise ValueError("provider_request_id must be an integer in 2..2147483647")
    if type(market_filter) is not dict or not market_filter:
        raise ValueError("market_filter must be a non-empty exact dict")
    _reject_secret_keys(market_filter)

    market_filter_bytes = _canonical_json_bytes(market_filter)
    market_filter_sha256 = sha256(market_filter_bytes).hexdigest()
    provisional_context = BetfairStreamSubscriptionContext(
        upstream_context_sha256="0" * 64,
        subscription_id="pending",
        provider_request_id=provider_request_id,
        subscription_generation=1,
        criteria_sha256="0" * 64,
        market_filter_sha256=market_filter_sha256,
        market_data_fields=market_data_fields,
        ladder_levels=ladder_levels,
        requested_heartbeat_ms=heartbeat_ms,
        requested_conflate_ms=conflate_ms,
    )

    market_data_filter: dict[str, Any] = {
        "fields": list(provisional_context.market_data_fields)
    }
    if ladder_levels is not None:
        market_data_filter["ladderLevels"] = ladder_levels
    request = {
        "op": "marketSubscription",
        "id": provider_request_id,
        "heartbeatMs": heartbeat_ms,
        "conflateMs": conflate_ms,
        "marketFilter": market_filter,
        "marketDataFilter": market_data_filter,
    }
    request_payload = _canonical_json_bytes(request) + b"\r\n"
    if len(request_payload) > _MAX_SUBSCRIPTION_BYTES:
        raise ValueError("market subscription request exceeds bounded size")
    request_sha256 = sha256(request_payload).hexdigest()

    connection_id = transport.connection_id
    connection_generation = _transport_generation(transport)
    lock = getattr(transport, "_lifecycle_lock", None)
    stream = getattr(transport, "_socket", None)
    if lock is None or stream is None:
        raise BetfairAuthenticatedStreamError("authenticated transport socket is unavailable")
    with lock:
        if (
            not transport.is_authenticated
            or transport.connection_id != connection_id
            or _transport_generation(transport) != connection_generation
            or getattr(transport, "_socket", None) is not stream
        ):
            raise BetfairAuthenticatedStreamError(
                "Betfair Stream connection changed before subscription send"
            )
        try:
            stream.sendall(request_payload)
        except Exception as exc:
            raise BetfairAuthenticatedStreamError(
                "Betfair market subscription send failed"
            ) from exc

    acknowledgement = transport.read_authenticated_frame()
    acknowledgement.assert_transport_issued()
    _require_same_connection(acknowledgement, connection_id, connection_generation)
    raw_status = _decode_exact_transport_frame(acknowledgement)
    if raw_status.get("op") != "status":
        raise BetfairAuthenticatedStreamError(
            "first frame after marketSubscription must be provider status acknowledgement"
        )
    if raw_status.get("id") != provider_request_id:
        raise BetfairAuthenticatedStreamError("provider status acknowledgement id mismatch")
    if raw_status.get("statusCode") != "SUCCESS" or raw_status.get("error") is not False:
        raise BetfairAuthenticatedStreamError(
            "Betfair market subscription was not acknowledged SUCCESS"
        )

    subscription_id = sha256(
        _canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "connection_id": connection_id,
                "connection_generation": connection_generation,
                "provider_request_id": provider_request_id,
                "request_sha256": request_sha256,
                "acknowledgement_frame_sha256": acknowledgement.payload_sha256,
            }
        )
    ).hexdigest()
    upstream_context_sha256 = sha256(
        _canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "subscription_id": subscription_id,
                "transport_frame_origin": (
                    "verified_tls_same_connection_betfair_auth_success"
                ),
            }
        )
    ).hexdigest()
    issued = BetfairAuthenticatedMarketSubscription(
        connection_id=connection_id,
        connection_generation=connection_generation,
        provider_request_id=provider_request_id,
        request_sha256=request_sha256,
        acknowledgement_frame_sha256=acknowledgement.payload_sha256,
        market_filter_sha256=market_filter_sha256,
        market_data_fields=provisional_context.market_data_fields,
        ladder_levels=ladder_levels,
        requested_heartbeat_ms=heartbeat_ms,
        requested_conflate_ms=conflate_ms,
        subscription_id=subscription_id,
        upstream_context_sha256=upstream_context_sha256,
    )
    _ISSUED_SUBSCRIPTIONS[issued] = _subscription_fingerprint(issued)
    return issued


class BetfairAuthenticatedStreamFreshnessRuntime:
    """Authenticated-frame -> canonical codec/freshness composition."""

    def __init__(
        self,
        transport: BetfairStreamTlsTransport,
        subscription: BetfairAuthenticatedMarketSubscription,
    ) -> None:
        if type(transport) is not BetfairStreamTlsTransport:
            raise TypeError("transport must be canonical BetfairStreamTlsTransport")
        if type(subscription) is not BetfairAuthenticatedMarketSubscription:
            raise TypeError(
                "subscription must be canonical BetfairAuthenticatedMarketSubscription"
            )
        subscription.assert_issued()
        self._transport = transport
        self._subscription = subscription
        self._require_current_connection()
        context = BetfairStreamSubscriptionContext(
            upstream_context_sha256=subscription.upstream_context_sha256,
            subscription_id=subscription.subscription_id,
            provider_request_id=subscription.provider_request_id,
            subscription_generation=subscription.connection_generation,
            criteria_sha256=subscription.request_sha256,
            market_filter_sha256=subscription.market_filter_sha256,
            market_data_fields=subscription.market_data_fields,
            ladder_levels=subscription.ladder_levels,
            requested_heartbeat_ms=subscription.requested_heartbeat_ms,
            requested_conflate_ms=subscription.requested_conflate_ms,
        )
        self._freshness = BetfairStreamPublishFreshnessRuntime(context)
        self._transport_by_evidence: dict[str, str] = {}

    @property
    def subscription(self) -> BetfairAuthenticatedMarketSubscription:
        return self._subscription

    def read_and_ingest(self) -> tuple[BetfairStreamPublicationEvidence, ...]:
        self._require_current_connection()
        frame = self._transport.read_authenticated_frame()
        frame.assert_transport_issued()
        _require_same_connection(
            frame,
            self._subscription.connection_id,
            self._subscription.connection_generation,
        )
        raw = _decode_exact_transport_frame(frame)
        if raw.get("op") != "mcm":
            raise BetfairAuthenticatedStreamError(
                "authenticated market freshness runtime accepts only mcm frames after subscription acknowledgement"
            )
        accepted_ms = _wall_time_ms()
        issued = self._freshness.ingest_raw(
            raw,
            received_time_ms=accepted_ms,
            ingested_time_ms=accepted_ms,
        )
        for evidence in issued:
            self._transport_by_evidence[evidence.evidence_id] = frame.payload_sha256
        return issued

    def evaluate(
        self,
        identity: BetfairQuoteIdentity,
        *,
        policy: BetfairStreamFreshnessPolicy,
    ) -> BetfairAuthenticatedFreshnessDecision:
        self._require_current_connection()
        evaluated_at_ms = _wall_time_ms()
        structural = self._freshness.evaluate(
            identity,
            as_of_ms=evaluated_at_ms,
            policy=policy,
        )
        frame_sha = (
            None
            if structural.evidence_id is None
            else self._transport_by_evidence.get(structural.evidence_id)
        )
        if (
            structural.verdict is not BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
            or structural.evidence_id is None
            or frame_sha is None
        ):
            return BetfairAuthenticatedFreshnessDecision(
                verdict=BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED,
                reason=structural.reason,
                evidence_id=structural.evidence_id,
                subscription_id=self._subscription.subscription_id,
                transport_frame_sha256=frame_sha,
                evaluated_at_ms=evaluated_at_ms,
            )
        self._subscription.assert_issued()
        self._require_current_connection()
        decision = BetfairAuthenticatedFreshnessDecision(
            verdict=(
                BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
            ),
            reason=(
                "canonical provider publication is fresh and bound to this authenticated acknowledged subscription"
            ),
            evidence_id=structural.evidence_id,
            subscription_id=self._subscription.subscription_id,
            transport_frame_sha256=frame_sha,
            evaluated_at_ms=evaluated_at_ms,
        )
        _ISSUED_DECISIONS[decision] = _decision_fingerprint(decision)
        return decision

    def _require_current_connection(self) -> None:
        self._subscription.assert_issued()
        if (
            not self._transport.is_authenticated
            or self._transport.connection_id != self._subscription.connection_id
            or _transport_generation(self._transport)
            != self._subscription.connection_generation
        ):
            raise BetfairAuthenticatedStreamError(
                "authenticated subscription is no longer bound to the live transport connection"
            )


def _decode_exact_transport_frame(
    frame: BetfairStreamAuthenticatedFrame,
) -> dict[str, Any]:
    if type(frame) is not BetfairStreamAuthenticatedFrame:
        raise TypeError("frame must be canonical BetfairStreamAuthenticatedFrame")
    frame.assert_transport_issued()
    decoder = BetfairCrlfJsonDecoder(max_frame_bytes=max(1, len(frame.payload)))
    messages = decoder.feed(frame.payload)
    decoder.finish()
    if len(messages) != 1:
        raise BetfairAuthenticatedStreamError(
            "authenticated transport frame must decode to exactly one JSON object"
        )
    return messages[0]


def _require_same_connection(
    frame: BetfairStreamAuthenticatedFrame,
    connection_id: str,
    connection_generation: int,
) -> None:
    if (
        frame.connection_id != connection_id
        or frame.connection_generation != connection_generation
    ):
        raise BetfairAuthenticatedStreamError(
            "transport frame belongs to another connection generation"
        )


def _transport_generation(transport: BetfairStreamTlsTransport) -> int:
    value = getattr(transport, "_connection_generation", None)
    if type(value) is not int or value <= 0:
        raise BetfairAuthenticatedStreamError(
            "authenticated transport connection generation is unavailable"
        )
    return value


def _wall_time_ms() -> int:
    value = time.time_ns() // 1_000_000
    if type(value) is not int or value <= 0:
        raise BetfairAuthenticatedStreamError("product wall clock is unavailable")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("value is not bounded canonical JSON") from exc


def _reject_secret_keys(value: Any) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key or key.strip() != key:
                raise ValueError(
                    "market_filter keys must be non-empty trimmed strings"
                )
            normalized = key.lower().replace("-", "").replace(" ", "")
            if any(
                fragment.replace("_", "") in normalized
                for fragment in _SECRET_KEY_FRAGMENTS
            ):
                raise ValueError("market_filter must not contain credential-like keys")
            _reject_secret_keys(item)
    elif type(value) is list:
        for item in value:
            _reject_secret_keys(item)
    elif type(value) in {str, int, bool, type(None), float}:
        return
    else:
        raise ValueError("market_filter contains unsupported JSON value type")


def _subscription_fingerprint(
    value: BetfairAuthenticatedMarketSubscription,
) -> str:
    return sha256(
        _canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "connection_id": value.connection_id,
                "connection_generation": value.connection_generation,
                "provider_request_id": value.provider_request_id,
                "request_sha256": value.request_sha256,
                "acknowledgement_frame_sha256": value.acknowledgement_frame_sha256,
                "market_filter_sha256": value.market_filter_sha256,
                "market_data_fields": list(value.market_data_fields),
                "ladder_levels": value.ladder_levels,
                "requested_heartbeat_ms": value.requested_heartbeat_ms,
                "requested_conflate_ms": value.requested_conflate_ms,
                "subscription_id": value.subscription_id,
                "upstream_context_sha256": value.upstream_context_sha256,
            }
        )
    ).hexdigest()


def _decision_fingerprint(value: BetfairAuthenticatedFreshnessDecision) -> str:
    return sha256(
        _canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "verdict": value.verdict.value,
                "reason": value.reason,
                "evidence_id": value.evidence_id,
                "subscription_id": value.subscription_id,
                "transport_frame_sha256": value.transport_frame_sha256,
                "evaluated_at_ms": value.evaluated_at_ms,
            }
        )
    ).hexdigest()
