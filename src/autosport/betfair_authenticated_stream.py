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
import math
from threading import RLock
import time
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, ref

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
_MAX_FILTER_DEPTH = 8
_MAX_FILTER_NODES = 2_048
_MAX_FILTER_COLLECTION_ITEMS = 1_024
_MAX_FILTER_STRING_BYTES = 4_096
_MAX_TRACKED_AUTHORITATIVE_QUOTES = 100_000
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
        with _AUTHORITY_LOCK:
            authority = _ISSUED_SUBSCRIPTIONS.get(self)
            if (
                authority is None
                or authority.fingerprint != _subscription_fingerprint(self)
                or authority.transport_ref() is None
            ):
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


@dataclass(frozen=True, slots=True)
class _SubscriptionAuthority:
    fingerprint: str
    transport_ref: ReferenceType[BetfairStreamTlsTransport]


_AUTHORITY_LOCK = RLock()
_ISSUED_SUBSCRIPTIONS: WeakKeyDictionary[
    BetfairAuthenticatedMarketSubscription, _SubscriptionAuthority
] = WeakKeyDictionary()
# None means an issuance transaction owns this transport but has not yet received
# the provider acknowledgement. A strong subscription value keeps the one active
# process-local capability alive for the current connection generation.
_ACTIVE_SUBSCRIPTION_BY_TRANSPORT: WeakKeyDictionary[
    BetfairStreamTlsTransport, BetfairAuthenticatedMarketSubscription | None
] = WeakKeyDictionary()


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
        with _AUTHORITY_LOCK:
            authority = _ISSUED_DECISIONS.get(self)
        if authority is None or authority.fingerprint != _decision_fingerprint(self):
            return False
        runtime = authority.runtime_ref()
        if runtime is None:
            return False
        try:
            now_ms = _wall_time_ms()
        except BetfairAuthenticatedStreamError:
            return False
        return runtime._decision_is_current(  # noqa: SLF001 - process-local authority check
            self,
            authority.identity,
            authority.policy,
            now_ms,
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


@dataclass(frozen=True, slots=True)
class _DecisionAuthority:
    fingerprint: str
    runtime_ref: ReferenceType[Any]
    identity: BetfairQuoteIdentity
    policy: BetfairStreamFreshnessPolicy


_ISSUED_DECISIONS: WeakKeyDictionary[
    BetfairAuthenticatedFreshnessDecision, _DecisionAuthority
] = WeakKeyDictionary()


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
    """Send and positively acknowledge one read-only Market Stream subscription.

    This intentionally supports one active product-owned subscription per authenticated
    connection generation. Replacement/resubscribe is a separate recovery authority
    because Betfair clock continuation has materially different truth semantics.
    """

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
    _validate_json_bounds(market_filter)
    _reject_secret_keys(market_filter)

    # The caller owns ``market_filter`` and can retain/mutate aliases.  Freeze one
    # canonical byte snapshot inside the authority-bearing issuer itself, detach a
    # product-owned JSON value from those exact bytes, and use that same detached
    # value for both the capability hash and the provider request.  This makes every
    # callable path to this issuer safe, including aliases/wrappers that bypass any
    # package-level guard.
    market_filter_bytes = _canonical_json_bytes(market_filter)
    try:
        sealed_market_filter = json.loads(market_filter_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise ValueError("market_filter canonical snapshot is not valid JSON") from exc
    if type(sealed_market_filter) is not dict or not sealed_market_filter:
        raise ValueError("market_filter canonical snapshot must be a non-empty object")
    _validate_json_bounds(sealed_market_filter)
    _reject_secret_keys(sealed_market_filter)
    if _canonical_json_bytes(sealed_market_filter) != market_filter_bytes:
        raise ValueError("market_filter canonical snapshot is not stable")

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
        "marketFilter": sealed_market_filter,
        "marketDataFilter": market_data_filter,
    }
    request_payload = _canonical_json_bytes(request) + b"\r\n"
    if len(request_payload) > _MAX_SUBSCRIPTION_BYTES:
        raise ValueError("market subscription request exceeds bounded size")
    request_sha256 = sha256(request_payload).hexdigest()

    connection_id = transport.connection_id
    connection_generation = _transport_generation(transport)
    with _AUTHORITY_LOCK:
        if transport in _ACTIVE_SUBSCRIPTION_BY_TRANSPORT:
            active = _ACTIVE_SUBSCRIPTION_BY_TRANSPORT[transport]
            if active is None:
                raise BetfairAuthenticatedStreamError(
                    "Betfair market subscription issuance is already in progress"
                )
            if (
                active.connection_id == connection_id
                and active.connection_generation == connection_generation
            ):
                raise BetfairAuthenticatedStreamError(
                    "Betfair Stream connection already has an active product subscription"
                )
            # A reconnect on the same transport object invalidates the prior process-local
            # capability and may begin a fresh first-subscription transaction.
            _ACTIVE_SUBSCRIPTION_BY_TRANSPORT.pop(transport, None)
        _ACTIVE_SUBSCRIPTION_BY_TRANSPORT[transport] = None

    try:
        lock = getattr(transport, "_lifecycle_lock", None)
        stream = getattr(transport, "_socket", None)
        if lock is None or stream is None:
            raise BetfairAuthenticatedStreamError(
                "authenticated transport socket is unavailable"
            )
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
        if acknowledgement.frame_sequence != 1:
            raise BetfairAuthenticatedStreamError(
                "marketSubscription acknowledgement was not the first post-auth frame"
            )
        raw_status = _decode_exact_transport_frame(acknowledgement)
        if raw_status.get("op") != "status":
            raise BetfairAuthenticatedStreamError(
                "first frame after marketSubscription must be provider status acknowledgement"
            )
        if raw_status.get("id") != provider_request_id:
            raise BetfairAuthenticatedStreamError("provider status acknowledgement id mismatch")
        provider_error_present = "error" in raw_status
        provider_error = raw_status.get("error")
        if (
            raw_status.get("statusCode") != "SUCCESS"
            or raw_status.get("connectionClosed") is True
            or "errorCode" in raw_status
            or "errorMessage" in raw_status
            or (provider_error_present and provider_error is not False)
        ):
            raise BetfairAuthenticatedStreamError(
                "Betfair market subscription was not acknowledged SUCCESS"
            )
    except Exception:
        with _AUTHORITY_LOCK:
            if (
                transport in _ACTIVE_SUBSCRIPTION_BY_TRANSPORT
                and _ACTIVE_SUBSCRIPTION_BY_TRANSPORT[transport] is None
            ):
                _ACTIVE_SUBSCRIPTION_BY_TRANSPORT.pop(transport, None)
        transport.close()
        raise

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
    with _AUTHORITY_LOCK:
        if (
            transport not in _ACTIVE_SUBSCRIPTION_BY_TRANSPORT
            or _ACTIVE_SUBSCRIPTION_BY_TRANSPORT[transport] is not None
        ):
            transport.close()
            raise BetfairAuthenticatedStreamError(
                "Betfair subscription issuance ownership changed before acknowledgement commit"
            )
        _ISSUED_SUBSCRIPTIONS[issued] = _SubscriptionAuthority(
            _subscription_fingerprint(issued),
            ref(transport),
        )
        _ACTIVE_SUBSCRIPTION_BY_TRANSPORT[transport] = issued
    return issued


class BetfairAuthenticatedStreamFreshnessRuntime:
    """Authenticated-frame -> canonical codec/freshness composition.

    Public callers never supply receive, ingest, or decision timestamps. The local wall
    time used here is captured only after the canonical transport has returned an issued
    authenticated frame, making it a conservative product availability boundary rather
    than a claim about the exact kernel socket-receive instant.
    """

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
        _require_subscription_for_transport(subscription, transport)
        self._transport = transport
        self._subscription = subscription
        self._read_lock = RLock()
        self._state_lock = RLock()
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
        self._transport_by_identity: dict[BetfairQuoteIdentity, tuple[str, str]] = {}

    @property
    def subscription(self) -> BetfairAuthenticatedMarketSubscription:
        return self._subscription

    def read_and_ingest(self) -> tuple[BetfairStreamPublicationEvidence, ...]:
        # Serialize the canonical transport reader so the shared receive buffer and
        # provider frame order cannot be raced by two composition callers. Decision
        # evaluation uses a separate short state lock and does not wait behind recv().
        with self._read_lock:
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
            with self._state_lock:
                self._require_current_connection()
                issued = self._freshness.ingest_raw(
                    raw,
                    received_time_ms=accepted_ms,
                    ingested_time_ms=accepted_ms,
                )
                for evidence in issued:
                    identity = evidence.quote.identity
                    if (
                        identity not in self._transport_by_identity
                        and len(self._transport_by_identity)
                        >= _MAX_TRACKED_AUTHORITATIVE_QUOTES
                    ):
                        self._transport_by_identity.clear()
                        raise BetfairAuthenticatedStreamError(
                            "authenticated freshness transport-origin map exceeded its bound"
                        )
                    self._transport_by_identity[identity] = (
                        evidence.evidence_id,
                        frame.payload_sha256,
                    )
                return issued

    def evaluate(
        self,
        identity: BetfairQuoteIdentity,
        *,
        policy: BetfairStreamFreshnessPolicy,
    ) -> BetfairAuthenticatedFreshnessDecision:
        self._require_current_connection()
        evaluated_at_ms = _wall_time_ms()
        with self._state_lock:
            self._require_current_connection()
            structural = self._freshness.evaluate(
                identity,
                as_of_ms=evaluated_at_ms,
                policy=policy,
            )
            frame_sha = self._bound_frame_sha(identity, structural.evidence_id)
            if (
                structural.verdict
                is not BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
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
            with _AUTHORITY_LOCK:
                _ISSUED_DECISIONS[decision] = _DecisionAuthority(
                    _decision_fingerprint(decision),
                    ref(self),
                    identity,
                    policy,
                )
            return decision

    def _bound_frame_sha(
        self,
        identity: BetfairQuoteIdentity,
        evidence_id: str | None,
    ) -> str | None:
        binding = self._transport_by_identity.get(identity)
        if evidence_id is not None and binding is not None:
            bound_evidence_id, bound_frame_sha = binding
            if bound_evidence_id == evidence_id:
                return bound_frame_sha
            self._transport_by_identity.pop(identity, None)
        elif evidence_id is None:
            self._transport_by_identity.pop(identity, None)
        return None

    def _decision_is_current(
        self,
        decision: BetfairAuthenticatedFreshnessDecision,
        identity: BetfairQuoteIdentity,
        policy: BetfairStreamFreshnessPolicy,
        now_ms: int,
    ) -> bool:
        if now_ms < decision.evaluated_at_ms:
            return False
        try:
            self._require_current_connection()
        except BetfairAuthenticatedStreamError:
            return False
        with self._state_lock:
            try:
                self._require_current_connection()
            except BetfairAuthenticatedStreamError:
                return False
            if decision.subscription_id != self._subscription.subscription_id:
                return False
            structural = self._freshness.evaluate(
                identity,
                as_of_ms=now_ms,
                policy=policy,
            )
            if (
                structural.verdict
                is not BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
                or structural.evidence_id != decision.evidence_id
            ):
                return False
            frame_sha = self._bound_frame_sha(identity, structural.evidence_id)
            return (
                frame_sha is not None
                and frame_sha == decision.transport_frame_sha256
            )

    def _require_current_connection(self) -> None:
        _require_subscription_for_transport(self._subscription, self._transport)
        if (
            not self._transport.is_authenticated
            or self._transport.connection_id != self._subscription.connection_id
            or _transport_generation(self._transport)
            != self._subscription.connection_generation
        ):
            raise BetfairAuthenticatedStreamError(
                "authenticated subscription is no longer bound to the live transport connection"
            )


def _require_subscription_for_transport(
    subscription: BetfairAuthenticatedMarketSubscription,
    transport: BetfairStreamTlsTransport,
) -> None:
    subscription.assert_issued()
    with _AUTHORITY_LOCK:
        authority = _ISSUED_SUBSCRIPTIONS.get(subscription)
        active = _ACTIVE_SUBSCRIPTION_BY_TRANSPORT.get(transport)
        if (
            authority is None
            or authority.transport_ref() is not transport
            or active is not subscription
        ):
            raise BetfairAuthenticatedStreamError(
                "Betfair market subscription was not issued for this exact active transport object"
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
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError("value is not bounded canonical JSON") from exc


def _validate_json_bounds(value: Any) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_FILTER_NODES:
            raise ValueError("market_filter exceeds bounded JSON node count")
        if depth > _MAX_FILTER_DEPTH:
            raise ValueError("market_filter exceeds bounded JSON nesting depth")
        if type(item) is dict:
            if len(item) > _MAX_FILTER_COLLECTION_ITEMS:
                raise ValueError("market_filter object has too many members")
            for key, child in item.items():
                if type(key) is not str or not key or key.strip() != key:
                    raise ValueError(
                        "market_filter keys must be non-empty trimmed strings"
                    )
                if len(key.encode("utf-8")) > _MAX_FILTER_STRING_BYTES:
                    raise ValueError("market_filter key exceeds bounded UTF-8 size")
                visit(child, depth + 1)
            return
        if type(item) is list:
            if len(item) > _MAX_FILTER_COLLECTION_ITEMS:
                raise ValueError("market_filter list has too many items")
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is str:
            if len(item.encode("utf-8")) > _MAX_FILTER_STRING_BYTES:
                raise ValueError("market_filter string exceeds bounded UTF-8 size")
            return
        if type(item) is bool or item is None:
            return
        if type(item) is int:
            if item < -(2**63) or item > 2**63 - 1:
                raise ValueError("market_filter integer exceeds bounded 64-bit range")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("market_filter float must be finite")
            return
        raise ValueError("market_filter contains unsupported JSON value type")

    visit(value, 0)


def _reject_secret_keys(value: Any) -> None:
    if type(value) is dict:
        for key, item in value.items():
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
