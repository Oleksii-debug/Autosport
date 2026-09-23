from __future__ import annotations

import hashlib
import json
import weakref
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


_RESULT_ISSUER = object()
_ISSUED_RESULTS: dict[int, tuple[weakref.ReferenceType[object], str]] = {}


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class RegisteredLiveInputCurrentView:
    """Product-issued current-view gate for one already-registered live input.

    A positive result proves only that the exact currently matching canonical
    MarketMirror components were OPEN and inside the configured causal freshness
    window at as_of. It deliberately does not prove stream continuity,
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

    def __init__(
        self,
        *,
        _issuer: object,
        input_id: str,
        as_of: str,
        max_age_microseconds: int,
        mirror_revision: int,
        outcome: LiveInputCurrentViewOutcome,
        wait_reasons: tuple[LiveInputWaitReason, ...],
        components: tuple[LiveMarketComponentEvidence, ...],
        evidence_sha256: str,
    ) -> None:
        if _issuer is not _RESULT_ISSUER:
            raise LiveMarketActionabilityError(
                "RegisteredLiveInputCurrentView must be product-issued"
            )
        object.__setattr__(self, "input_id", input_id)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "max_age_microseconds", max_age_microseconds)
        object.__setattr__(self, "mirror_revision", mirror_revision)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "wait_reasons", wait_reasons)
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "evidence_sha256", evidence_sha256)

    @property
    def is_product_issued(self) -> bool:
        return _is_product_issued(self)

    @property
    def current_view_eligible(self) -> bool:
        _require_product_issued(self)
        return self.outcome is LiveInputCurrentViewOutcome.CURRENT_VIEW_ELIGIBLE

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


def _result_fingerprint(result: RegisteredLiveInputCurrentView) -> str:
    return _canonical_json_sha256(
        {
            "input_id": result.input_id,
            "as_of": result.as_of,
            "max_age_microseconds": result.max_age_microseconds,
            "mirror_revision": result.mirror_revision,
            "outcome": result.outcome.value,
            "wait_reasons": [reason.value for reason in result.wait_reasons],
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
                for item in result.components
            ],
            "evidence_sha256": result.evidence_sha256,
        }
    )


def _is_product_issued(result: RegisteredLiveInputCurrentView) -> bool:
    entry = _ISSUED_RESULTS.get(id(result))
    if entry is None:
        return False
    reference, fingerprint = entry
    return reference() is result and fingerprint == _result_fingerprint(result)


def _require_product_issued(result: RegisteredLiveInputCurrentView) -> None:
    if not _is_product_issued(result):
        raise LiveMarketActionabilityError(
            "RegisteredLiveInputCurrentView is not current process-issued evidence"
        )


def _register_product_issued(
    result: RegisteredLiveInputCurrentView,
) -> RegisteredLiveInputCurrentView:
    key = id(result)

    def cleanup(
        dead_reference: weakref.ReferenceType[object],
        *,
        issued_key: int = key,
    ) -> None:
        current = _ISSUED_RESULTS.get(issued_key)
        if current is not None and current[0] is dead_reference:
            _ISSUED_RESULTS.pop(issued_key, None)

    reference = weakref.ref(result, cleanup)
    _ISSUED_RESULTS[key] = (reference, _result_fingerprint(result))
    return result


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
    wait_reasons = tuple(sorted(aggregate_reasons, key=lambda item: item.value))
    outcome = (
        LiveInputCurrentViewOutcome.CURRENT_VIEW_ELIGIBLE
        if components and not wait_reasons
        else LiveInputCurrentViewOutcome.WAIT
    )
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
        _issuer=_RESULT_ISSUER,
        input_id=normalized_input_id,
        as_of=as_of_text,
        max_age_microseconds=max_age_microseconds,
        mirror_revision=raw_snapshot.revision,
        outcome=outcome,
        wait_reasons=wait_reasons,
        components=components,
        evidence_sha256=evidence_sha256,
    )
    return _register_product_issued(result)