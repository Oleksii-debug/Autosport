from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .market_mirror import MarketMirror
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)


class LiveMarketActionabilityError(ValueError):
    """Raised when registered live-input actionability evidence is malformed."""


class LiveInputCurrentViewOutcome(str, Enum):
    CURRENT_VIEW_ELIGIBLE = "current_view_eligible"
    WAIT = "wait"


class LiveInputWaitReason(str, Enum):
    NO_COMPONENTS = "no_components"
    NON_OPEN_STATUS = "non_open_status"
    INVALID_CAUSAL_TIMESTAMP = "invalid_causal_timestamp"
    FUTURE_CAUSALITY = "future_causality"
    STALE = "stale"
    PRODUCT_ORIGIN_UNPROVEN = "product_origin_unproven"


def _canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        (value.days * 86_400 + value.seconds) * 1_000_000
        + value.microseconds
    )


def _canonical_input_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise LiveMarketActionabilityError(
            "input_id must be a non-empty trimmed string"
        )
    if "\x00" in value:
        raise LiveMarketActionabilityError("input_id must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LiveMarketActionabilityError(
            "input_id must be valid UTF-8 text"
        ) from exc
    return value


def _canonical_as_of(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("as_of must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveMarketActionabilityError("as_of must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_max_age(value: object) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError("max_age must be a timedelta")
    if value < timedelta(0):
        raise LiveMarketActionabilityError("max_age must be non-negative")
    return value


@dataclass(frozen=True, slots=True)
class LiveMarketComponentEvidence:
    source_id: str
    quote_key: str
    event_sha256: str
    sequence: int
    status: str
    freshness_ts: str | None
    age_microseconds: int | None
    wait_reasons: tuple[LiveInputWaitReason, ...]


@dataclass(frozen=True, slots=True)
class RegisteredLiveInputCurrentView:
    """Diagnostic current-view evidence for one already-registered live input.

    Standalone evaluation never proves product/live acquisition origin and therefore
    never issues positive actionability. It deliberately does not prove stream continuity,
    provider gap recovery, depth/liquidity, account capability, portfolio/risk,
    execution, settlement, profitability, readiness, or real-money permission.
    """

    input_id: str
    as_of: str
    max_age_microseconds: int
    mirror_revision: int
    outcome: LiveInputCurrentViewOutcome
    wait_reasons: tuple[LiveInputWaitReason, ...]
    components: tuple[LiveMarketComponentEvidence, ...]
    evidence_sha256: str

    @property
    def is_product_issued(self) -> bool:
        # This standalone diagnostic evaluator has no product-owned mirror-origin
        # composition. Positive issuance remains intentionally unavailable.
        return False

    @property
    def current_view_eligible(self) -> bool:
        # Diagnostic structural freshness is not actionability authority. A future
        # product composition may consume the same evidence only after binding the
        # exact builder-owned live mirror provenance.
        return False

    @property
    def continuity_proven(self) -> bool:
        return False

    @property
    def depth_liquidity_proven(self) -> bool:
        return False

    @property
    def provider_capability_proven(self) -> bool:
        return False

    @property
    def risk_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def settlement_proven(self) -> bool:
        return False

    @property
    def real_money_authorized(self) -> bool:
        return False


def evaluate_registered_input_current_view(
    *,
    updates: BoundedMirrorInvalidationBuffer,
    dependencies: FocusedMirrorDependencyIndex,
    input_id: str,
    as_of: datetime,
    max_age: timedelta,
) -> RegisteredLiveInputCurrentView:
    """Derive one fail-closed current-view gate from canonical live mirror state.

    The function intentionally consumes the existing MarketMirror and registered
    dependency index. It creates no provider state, no second market store, and no
    financial authority.
    """

    if not isinstance(updates, BoundedMirrorInvalidationBuffer):
        raise TypeError("updates must be BoundedMirrorInvalidationBuffer")
    if not isinstance(dependencies, FocusedMirrorDependencyIndex):
        raise TypeError("dependencies must be FocusedMirrorDependencyIndex")
    if dependencies._mirror is not updates.mirror:
        raise LiveMarketActionabilityError(
            "dependencies and updates must share the same canonical MarketMirror"
        )

    normalized_input_id = _canonical_input_id(input_id)
    boundary = _canonical_as_of(as_of)
    age_limit = _canonical_max_age(max_age)

    try:
        dependency = dependencies._dependency(normalized_input_id)
    except KeyError as exc:
        raise LiveMarketActionabilityError(
            f"unknown registered live input {normalized_input_id!r}"
        ) from exc

    raw_snapshot = updates.mirror.view(
        **dependencies._selectors(dependency)
    )

    component_evidence: list[LiveMarketComponentEvidence] = []
    aggregate_reasons: set[LiveInputWaitReason] = set()

    if not raw_snapshot.events:
        aggregate_reasons.add(LiveInputWaitReason.NO_COMPONENTS)

    for event in raw_snapshot.events:
        reasons: set[LiveInputWaitReason] = set()
        if event.status not in MarketMirror._DECISION_ELIGIBLE_STATUSES:
            reasons.add(LiveInputWaitReason.NON_OPEN_STATUS)

        observed = MarketMirror._utc_timestamp(event.observed_ts)
        ingested = MarketMirror._utc_timestamp(event.ingest_ts)
        freshness = MarketMirror._utc_timestamp(
            event.source_ts or event.observed_ts
        )

        if observed is None or ingested is None or freshness is None:
            reasons.add(LiveInputWaitReason.INVALID_CAUSAL_TIMESTAMP)
            freshness_text = None
            age_microseconds = None
        else:
            freshness_text = freshness.isoformat()
            if observed > boundary or ingested > boundary or freshness > boundary:
                reasons.add(LiveInputWaitReason.FUTURE_CAUSALITY)
                age_microseconds = None
            else:
                age = boundary - freshness
                age_microseconds = _timedelta_microseconds(age)
                if age > age_limit:
                    reasons.add(LiveInputWaitReason.STALE)

        event_sha256 = _canonical_json_sha256(event.to_dict())
        ordered_reasons = tuple(sorted(reasons, key=lambda item: item.value))
        aggregate_reasons.update(ordered_reasons)
        component_evidence.append(
            LiveMarketComponentEvidence(
                source_id=event.source_id,
                quote_key=event.quote_key,
                event_sha256=event_sha256,
                sequence=event.sequence,
                status=event.status,
                freshness_ts=freshness_text,
                age_microseconds=age_microseconds,
                wait_reasons=ordered_reasons,
            )
        )

    components = tuple(
        sorted(
            component_evidence,
            key=lambda item: (item.source_id, item.quote_key),
        )
    )
    # Exact type/coherence only proves a structurally readable mirror, not that
    # its bytes came from the builder-owned live acquisition path. Until that
    # composition exists, otherwise-eligible caller mirrors remain explicit WAIT.
    if components and not aggregate_reasons:
        aggregate_reasons.add(LiveInputWaitReason.PRODUCT_ORIGIN_UNPROVEN)

    wait_reasons = tuple(sorted(aggregate_reasons, key=lambda item: item.value))
    outcome = LiveInputCurrentViewOutcome.WAIT
    as_of_text = boundary.isoformat()
    max_age_microseconds = _timedelta_microseconds(age_limit)

    payload = {
        "schema": "autosport.registered_live_input_current_view",
        "schema_version": 1,
        "input_id": normalized_input_id,
        "as_of": as_of_text,
        "max_age_microseconds": max_age_microseconds,
        "mirror_revision": raw_snapshot.revision,
        "outcome": outcome.value,
        "wait_reasons": [reason.value for reason in wait_reasons],
        "components": [
            {
                "source_id": item.source_id,
                "quote_key": item.quote_key,
                "event_sha256": item.event_sha256,
                "sequence": item.sequence,
                "status": item.status,
                "freshness_ts": item.freshness_ts,
                "age_microseconds": item.age_microseconds,
                "wait_reasons": [
                    reason.value for reason in item.wait_reasons
                ],
            }
            for item in components
        ],
        "stronger_authority": {
            "continuity_proven": False,
            "depth_liquidity_proven": False,
            "provider_capability_proven": False,
            "risk_authorized": False,
            "execution_authorized": False,
            "settlement_proven": False,
            "real_money_authorized": False,
        },
    }
    evidence_sha256 = _canonical_json_sha256(payload)
    result = RegisteredLiveInputCurrentView(
        input_id=normalized_input_id,
        as_of=as_of_text,
        max_age_microseconds=max_age_microseconds,
        mirror_revision=raw_snapshot.revision,
        outcome=outcome,
        wait_reasons=wait_reasons,
        components=components,
        evidence_sha256=evidence_sha256,
    )
    return result