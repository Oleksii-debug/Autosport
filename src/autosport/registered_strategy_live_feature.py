"""Registered-strategy live feature observation for the supported PAPER product.

This module closes only the point-in-time feature boundary.  It deliberately does
not turn a model output into a probability, create a ForecastRecord, construct an
OpportunityIntent, or grant execution authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .domain import MarketEvent
from .market_mirror import MirrorSnapshot
from .scientific_registry import FeatureSet, ModelVersion, ScientificRegistry


LIVE_FEATURE_SET_ID = "autosport-live-implied-probability-v1"
LIVE_FEATURE_SET_VERSION = "1"
LIVE_FEATURE_FRESHNESS_POLICY_ID = "market-mirror-source-ts-preferred-v1"
LIVE_FEATURE_DEFINITION_JSON = (
    '{"availability":"max(observed_ts,ingest_ts)<=decision_at; freshness_timestamp=source_ts when present else observed_ts; 0<=decision_at-freshness_timestamp<=freshness_max_age_seconds",'
    '"feature":"implied_probability_binary64_v1",'
    '"feature_set_id":"autosport-live-implied-probability-v1",'
    '"formula":"1.0 / float(decimal_odds)",'
    f'"freshness_policy":"{LIVE_FEATURE_FRESHNESS_POLICY_ID}",'
    '"input":"canonical MarketEvent.decimal_odds",'
    '"schema":"autosport.live_feature_definition","schema_version":1,'
    '"semantics":"market-price feature only; not a calibrated outcome probability",'
    '"snapshot_identity":"sha256(canonical JSON of sorted canonical MarketEvent payloads)",'
    '"version":"1"}'
)
LIVE_FEATURE_DEFINITION_SHA256 = hashlib.sha256(
    LIVE_FEATURE_DEFINITION_JSON.encode("utf-8")
).hexdigest()
LIVE_FEATURE_SOURCE_CONTRACT_JSON = (
    '{"callable":"observe_registered_strategy_live_features",'
    '"decision_boundary":"normalized decision_at + exact non-negative freshness_max_age_seconds are required and evidence-bound",'
    '"event_authority":"autosport.domain.MarketEvent.from_dict/to_dict",'
    f'"feature_definition_sha256":"{LIVE_FEATURE_DEFINITION_SHA256}",'
    f'"freshness_policy":"{LIVE_FEATURE_FRESHNESS_POLICY_ID}",'
    '"module":"autosport.registered_strategy_live_feature",'
    '"numeric_contract":"Python binary64 reciprocal; evidence binds float.hex",'
    '"schema":"autosport.live_feature_source_contract","schema_version":1}'
)
LIVE_FEATURE_SOURCE_SHA256 = hashlib.sha256(
    LIVE_FEATURE_SOURCE_CONTRACT_JSON.encode("utf-8")
).hexdigest()

_SHA256_HEX = frozenset("0123456789abcdef")


class RegisteredStrategyLiveFeatureError(ValueError):
    """Raised when live feature provenance cannot be proven exactly."""


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise RegisteredStrategyLiveFeatureError(
            f"{name} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RegisteredStrategyLiveFeatureError(
            f"{name} must be valid UTF-8 text"
        ) from exc
    return value


def _sha256(name: str, value: object) -> str:
    text = _canonical_text(name, value)
    if (
        len(text) != 64
        or text != text.lower()
        or any(character not in _SHA256_HEX for character in text)
    ):
        raise RegisteredStrategyLiveFeatureError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(name: str, value: object) -> datetime:
    text = _canonical_text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegisteredStrategyLiveFeatureError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegisteredStrategyLiveFeatureError(
            f"{name} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _nonnegative_seconds(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise RegisteredStrategyLiveFeatureError(
            f"{name} must be a non-negative integer number of seconds"
        )
    return value


def _canonical_json_sha256(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RegisteredStrategyLiveFeatureError(
            "live feature evidence is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RegisteredLiveFeatureAuthority:
    """Exact registry lineage authorizing one frozen live feature definition."""

    model_version_id: str
    model_record_sha256: str
    model_available_at: str
    feature_set_id: str
    feature_version: str
    feature_definition_sha256: str
    feature_source_sha256: str
    feature_record_sha256: str
    feature_available_at: str

    def __post_init__(self) -> None:
        _canonical_text("model_version_id", self.model_version_id)
        _sha256("model_record_sha256", self.model_record_sha256)
        model_at = _instant("model_available_at", self.model_available_at)
        _canonical_text("feature_set_id", self.feature_set_id)
        _canonical_text("feature_version", self.feature_version)
        _sha256("feature_definition_sha256", self.feature_definition_sha256)
        _sha256("feature_source_sha256", self.feature_source_sha256)
        _sha256("feature_record_sha256", self.feature_record_sha256)
        feature_at = _instant("feature_available_at", self.feature_available_at)
        if feature_at > model_at:
            raise RegisteredStrategyLiveFeatureError(
                "FeatureSet was not available before ModelVersion"
            )

    @property
    def authority_sha256(self) -> str:
        return _canonical_json_sha256(
            {
                "schema": "autosport.registered_live_feature_authority",
                "schema_version": 1,
                "model_version_id": self.model_version_id,
                "model_record_sha256": self.model_record_sha256,
                "model_available_at": self.model_available_at,
                "feature_set_id": self.feature_set_id,
                "feature_version": self.feature_version,
                "feature_definition_sha256": self.feature_definition_sha256,
                "feature_source_sha256": self.feature_source_sha256,
                "feature_record_sha256": self.feature_record_sha256,
                "feature_available_at": self.feature_available_at,
            }
        )


@dataclass(frozen=True, slots=True)
class LiveFeatureObservation:
    """One immutable quote feature bound to the exact decision-visible snapshot."""

    input_id: str
    quote_key: str
    feature: float
    available_at: str
    decision_at: str
    freshness_policy_id: str
    freshness_max_age_seconds: int
    freshness_timestamp: str
    market_event_sha256: str
    market_snapshot_sha256: str
    authority_sha256: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _canonical_text("input_id", self.input_id)
        _canonical_text("quote_key", self.quote_key)
        if (
            type(self.feature) is not float
            or not math.isfinite(self.feature)
            or not 0.0 < self.feature < 1.0
        ):
            raise RegisteredStrategyLiveFeatureError(
                "feature must be a finite binary64 value strictly between 0 and 1"
            )
        available = _instant("available_at", self.available_at)
        decision = _instant("decision_at", self.decision_at)
        if self.freshness_policy_id != LIVE_FEATURE_FRESHNESS_POLICY_ID:
            raise RegisteredStrategyLiveFeatureError(
                "freshness_policy_id does not match the supported live feature contract"
            )
        max_age_seconds = _nonnegative_seconds(
            "freshness_max_age_seconds",
            self.freshness_max_age_seconds,
        )
        freshness = _instant("freshness_timestamp", self.freshness_timestamp)
        if available > decision:
            raise RegisteredStrategyLiveFeatureError(
                "feature availability is after decision time"
            )
        freshness_age_seconds = (decision - freshness).total_seconds()
        if freshness_age_seconds < 0 or freshness_age_seconds > max_age_seconds:
            raise RegisteredStrategyLiveFeatureError(
                "feature freshness timestamp is outside the bound decision window"
            )
        _sha256("market_event_sha256", self.market_event_sha256)
        _sha256("market_snapshot_sha256", self.market_snapshot_sha256)
        _sha256("authority_sha256", self.authority_sha256)
        _sha256("evidence_sha256", self.evidence_sha256)

    @property
    def feature_hex(self) -> str:
        return self.feature.hex()


def _make_registered_live_feature_authority_resolver():
    registry_type = ScientificRegistry
    registry_schema_version = registry_type.SCHEMA_VERSION
    registry_init = registry_type.__init__
    registry_init_code = getattr(registry_init, "__code__", None)
    registry_read = registry_type._read
    registry_read_code = getattr(registry_read, "__code__", None)
    registry_validate_entry = registry_type._validate_entry
    registry_validate_entry_code = getattr(registry_validate_entry, "__code__", None)
    registry_get = registry_type.get
    registry_get_code = getattr(registry_get, "__code__", None)
    registry_causal_precedes = registry_type.causal_precedes
    registry_causal_precedes_code = getattr(
        registry_causal_precedes,
        "__code__",
        None,
    )

    model_version_type = ModelVersion
    model_version_init = model_version_type.__init__
    model_version_init_code = getattr(model_version_init, "__code__", None)
    model_version_post_init = model_version_type.__post_init__
    model_version_post_init_code = getattr(model_version_post_init, "__code__", None)
    model_version_to_payload = model_version_type.to_payload
    model_version_to_payload_code = getattr(
        model_version_to_payload,
        "__code__",
        None,
    )

    feature_set_type = FeatureSet
    feature_set_init = feature_set_type.__init__
    feature_set_init_code = getattr(feature_set_init, "__code__", None)
    feature_set_post_init = feature_set_type.__post_init__
    feature_set_post_init_code = getattr(feature_set_post_init, "__code__", None)
    feature_set_to_payload = feature_set_type.to_payload
    feature_set_to_payload_code = getattr(
        feature_set_to_payload,
        "__code__",
        None,
    )

    def same_canonical_callable(
        current: object,
        expected: object,
        expected_code: object,
    ) -> bool:
        return (
            current is expected
            and getattr(current, "__code__", None) is expected_code
        )

    def require_canonical_scientific_dispatch() -> None:
        if ScientificRegistry is not registry_type:
            raise RegisteredStrategyLiveFeatureError(
                "canonical ScientificRegistry type changed"
            )
        if (
            ScientificRegistry.SCHEMA_VERSION != registry_schema_version
            or not same_canonical_callable(
                ScientificRegistry.__init__,
                registry_init,
                registry_init_code,
            )
            or not same_canonical_callable(
                ScientificRegistry._read,
                registry_read,
                registry_read_code,
            )
            or not same_canonical_callable(
                ScientificRegistry._validate_entry,
                registry_validate_entry,
                registry_validate_entry_code,
            )
            or not same_canonical_callable(
                ScientificRegistry.get,
                registry_get,
                registry_get_code,
            )
            or not same_canonical_callable(
                ScientificRegistry.causal_precedes,
                registry_causal_precedes,
                registry_causal_precedes_code,
            )
        ):
            raise RegisteredStrategyLiveFeatureError(
                "canonical ScientificRegistry read/causal authority changed"
            )
        if (
            ModelVersion is not model_version_type
            or FeatureSet is not feature_set_type
        ):
            raise RegisteredStrategyLiveFeatureError(
                "canonical scientific record type changed"
            )
        if (
            not same_canonical_callable(
                ModelVersion.__init__,
                model_version_init,
                model_version_init_code,
            )
            or not same_canonical_callable(
                ModelVersion.__post_init__,
                model_version_post_init,
                model_version_post_init_code,
            )
            or not same_canonical_callable(
                ModelVersion.to_payload,
                model_version_to_payload,
                model_version_to_payload_code,
            )
            or not same_canonical_callable(
                FeatureSet.__init__,
                feature_set_init,
                feature_set_init_code,
            )
            or not same_canonical_callable(
                FeatureSet.__post_init__,
                feature_set_post_init,
                feature_set_post_init_code,
            )
            or not same_canonical_callable(
                FeatureSet.to_payload,
                feature_set_to_payload,
                feature_set_to_payload_code,
            )
        ):
            raise RegisteredStrategyLiveFeatureError(
                "canonical scientific record validation changed"
            )

    def resolve_registered_live_feature_authority(
        registry: ScientificRegistry,
        model_version_id: str,
        *,
        decision_at: str,
    ) -> RegisteredLiveFeatureAuthority:
        """Resolve exact ModelVersion -> FeatureSet lineage from durable registry truth."""

        require_canonical_scientific_dispatch()
        if type(registry) is not registry_type:
            raise RegisteredStrategyLiveFeatureError(
                "registry must be the canonical ScientificRegistry"
            )
        wanted_model = _canonical_text("model_version_id", model_version_id)
        decision = _instant("decision_at", decision_at)

        try:
            durable_registry = registry_type(registry.path)
        except (OSError, ValueError) as exc:
            raise RegisteredStrategyLiveFeatureError(
                "durable ScientificRegistry cannot be reopened canonically"
            ) from exc

        model_entry = registry_get(
            durable_registry,
            "ModelVersion",
            wanted_model,
        )
        if model_entry is None:
            raise RegisteredStrategyLiveFeatureError(
                "registered ModelVersion is missing"
            )
        try:
            model = model_version_type(**model_entry.payload)
        except (TypeError, ValueError) as exc:
            raise RegisteredStrategyLiveFeatureError(
                "registered ModelVersion is invalid"
            ) from exc
        if model_version_to_payload(model) != model_entry.payload:
            raise RegisteredStrategyLiveFeatureError(
                "registered ModelVersion payload is not canonical"
            )
        if (
            model_entry.record_id != model.model_version_id
            or model.model_version_id != wanted_model
            or model_entry.available_at != model.created_at
        ):
            raise RegisteredStrategyLiveFeatureError(
                "registered ModelVersion identity is inconsistent"
            )
        model_available = _instant("ModelVersion.available_at", model_entry.available_at)
        if model_available > decision:
            raise RegisteredStrategyLiveFeatureError(
                "ModelVersion was not available at decision time"
            )

        if model.feature_set_id != LIVE_FEATURE_SET_ID:
            raise RegisteredStrategyLiveFeatureError(
                "ModelVersion is not bound to the supported live feature set"
            )
        feature_entry = registry_get(
            durable_registry,
            "FeatureSet",
            model.feature_set_id,
        )
        if feature_entry is None:
            raise RegisteredStrategyLiveFeatureError(
                "registered FeatureSet is missing"
            )
        feature_payload = feature_entry.payload
        if type(feature_payload) is not dict or set(feature_payload) != {
            "feature_set_id",
            "version",
            "definition_sha256",
            "source_sha256",
            "available_at",
        }:
            raise RegisteredStrategyLiveFeatureError(
                "registered FeatureSet payload schema is invalid"
            )
        try:
            feature = feature_set_type(
                feature_set_id=feature_payload["feature_set_id"],
                version=feature_payload["version"],
                definition_sha256=feature_payload["definition_sha256"],
                source_sha256=feature_payload["source_sha256"],
                available_at_utc=feature_payload["available_at"],
            )
        except (TypeError, ValueError) as exc:
            raise RegisteredStrategyLiveFeatureError(
                "registered FeatureSet is invalid"
            ) from exc
        if feature_set_to_payload(feature) != feature_payload:
            raise RegisteredStrategyLiveFeatureError(
                "registered FeatureSet payload is not canonical"
            )
        if (
            feature_entry.record_id != feature.feature_set_id
            or feature.feature_set_id != model.feature_set_id
            or feature_entry.available_at != feature.available_at_utc
        ):
            raise RegisteredStrategyLiveFeatureError(
                "registered FeatureSet identity is inconsistent"
            )
        feature_available = _instant(
            "FeatureSet.available_at", feature_entry.available_at
        )
        if feature_available > model_available:
            raise RegisteredStrategyLiveFeatureError(
                "FeatureSet was not available before ModelVersion"
            )
        if feature_available > decision:
            raise RegisteredStrategyLiveFeatureError(
                "FeatureSet was not available at decision time"
            )
        if (
            feature.version != LIVE_FEATURE_SET_VERSION
            or feature.definition_sha256 != LIVE_FEATURE_DEFINITION_SHA256
            or feature.source_sha256 != LIVE_FEATURE_SOURCE_SHA256
        ):
            raise RegisteredStrategyLiveFeatureError(
                "registered FeatureSet does not match the supported live feature contract"
            )
        if not registry_causal_precedes(
            durable_registry,
            "FeatureSet",
            feature.feature_set_id,
            "ModelVersion",
            model.model_version_id,
        ):
            raise RegisteredStrategyLiveFeatureError(
                "FeatureSet does not durably precede ModelVersion in ScientificRegistry"
            )

        return RegisteredLiveFeatureAuthority(
            model_version_id=model.model_version_id,
            model_record_sha256=model_entry.record_sha256,
            model_available_at=model_entry.available_at,
            feature_set_id=feature.feature_set_id,
            feature_version=feature.version,
            feature_definition_sha256=feature.definition_sha256,
            feature_source_sha256=feature.source_sha256,
            feature_record_sha256=feature_entry.record_sha256,
            feature_available_at=feature_entry.available_at,
        )

    return resolve_registered_live_feature_authority


resolve_registered_live_feature_authority = (
    _make_registered_live_feature_authority_resolver()
)


def _canonical_snapshot_events(snapshot: MirrorSnapshot) -> tuple[MarketEvent, ...]:
    if type(snapshot) is not MirrorSnapshot:
        raise RegisteredStrategyLiveFeatureError(
            "snapshot must be the canonical MirrorSnapshot"
        )
    if type(snapshot.revision) is not int or snapshot.revision < 0:
        raise RegisteredStrategyLiveFeatureError(
            "snapshot revision must be a non-negative integer"
        )
    if type(snapshot.events) is not tuple:
        raise RegisteredStrategyLiveFeatureError(
            "snapshot events must be a canonical tuple"
        )

    canonical: list[MarketEvent] = []
    identities: set[tuple[str, str]] = set()
    for event in snapshot.events:
        if type(event) is not MarketEvent:
            raise RegisteredStrategyLiveFeatureError(
                "snapshot contains a non-canonical MarketEvent"
            )
        try:
            rebound = MarketEvent.from_dict(event.to_dict())
        except (TypeError, ValueError) as exc:
            raise RegisteredStrategyLiveFeatureError(
                "snapshot contains an invalid MarketEvent"
            ) from exc
        if rebound != event:
            raise RegisteredStrategyLiveFeatureError(
                "snapshot MarketEvent does not round-trip canonically"
            )
        if event.status != "open":
            raise RegisteredStrategyLiveFeatureError(
                "live feature input must be decision-eligible open market state"
            )
        identity = (event.source_id, event.quote_key)
        if identity in identities:
            raise RegisteredStrategyLiveFeatureError(
                "snapshot contains duplicate source/quote identity"
            )
        identities.add(identity)
        canonical.append(event)

    return tuple(
        sorted(
            canonical,
            key=lambda event: (
                event.source_id,
                event.sport or "",
                event.event_id,
                event.market_id,
                event.selection_id,
                event.exchange_side or "",
                event.sequence,
            ),
        )
    )


def market_snapshot_sha256(snapshot: MirrorSnapshot) -> str:
    """Hash exact market evidence, excluding the process-local mirror revision."""

    events = _canonical_snapshot_events(snapshot)
    return _canonical_json_sha256([event.to_dict() for event in events])


def observe_registered_strategy_live_features(
    input_id: str,
    snapshot: MirrorSnapshot,
    *,
    decision_at: str,
    freshness_max_age_seconds: int,
    registry: ScientificRegistry,
    model_version_id: str,
) -> tuple[LiveFeatureObservation, ...]:
    """Resolve registry authority and derive deterministic point-in-time quote features."""

    wanted_input = _canonical_text("input_id", input_id)
    decision = _instant("decision_at", decision_at)
    decision_text = decision.isoformat().replace("+00:00", "Z")
    max_age_seconds = _nonnegative_seconds(
        "freshness_max_age_seconds",
        freshness_max_age_seconds,
    )
    authority = resolve_registered_live_feature_authority(
        registry,
        model_version_id,
        decision_at=decision_at,
    )
    events = _canonical_snapshot_events(snapshot)
    snapshot_sha256 = _canonical_json_sha256(
        [event.to_dict() for event in events]
    )

    result: list[LiveFeatureObservation] = []
    for event in events:
        observed = _instant("MarketEvent.observed_ts", event.observed_ts)
        ingested = _instant("MarketEvent.ingest_ts", event.ingest_ts)
        available = max(observed, ingested)
        if available > decision:
            raise RegisteredStrategyLiveFeatureError(
                "market evidence was not causally available at decision time"
            )
        if event.source_ts is not None:
            source_timestamp = _instant("MarketEvent.source_ts", event.source_ts)
            if source_timestamp > decision:
                raise RegisteredStrategyLiveFeatureError(
                    "provider source timestamp is after decision time"
                )
            if source_timestamp > observed:
                raise RegisteredStrategyLiveFeatureError(
                    "provider source timestamp is after local observation"
                )
            freshness_timestamp = source_timestamp
        else:
            freshness_timestamp = observed
        freshness_age_seconds = (decision - freshness_timestamp).total_seconds()
        if (
            freshness_age_seconds < 0
            or freshness_age_seconds > max_age_seconds
        ):
            raise RegisteredStrategyLiveFeatureError(
                "market evidence is outside the live freshness boundary"
            )

        try:
            decimal_odds = float(event.decimal_odds)
            feature = 1.0 / decimal_odds
        except (OverflowError, ValueError, ZeroDivisionError) as exc:
            raise RegisteredStrategyLiveFeatureError(
                "decimal odds cannot produce supported binary64 feature"
            ) from exc
        if not math.isfinite(feature) or not 0.0 < feature < 1.0:
            raise RegisteredStrategyLiveFeatureError(
                "decimal odds produced an invalid live feature"
            )

        event_sha256 = _canonical_json_sha256(event.to_dict())
        available_text = available.isoformat().replace("+00:00", "Z")
        freshness_text = freshness_timestamp.isoformat().replace("+00:00", "Z")
        evidence_payload = {
            "schema": "autosport.registered_strategy_live_feature_observation",
            "schema_version": 1,
            "input_id": wanted_input,
            "quote_key": event.quote_key,
            "feature_set_id": authority.feature_set_id,
            "feature_version": authority.feature_version,
            "feature_definition_sha256": authority.feature_definition_sha256,
            "feature_source_sha256": authority.feature_source_sha256,
            "model_version_id": authority.model_version_id,
            "model_record_sha256": authority.model_record_sha256,
            "feature_record_sha256": authority.feature_record_sha256,
            "authority_sha256": authority.authority_sha256,
            "feature_hex": feature.hex(),
            "available_at": available_text,
            "decision_at": decision_text,
            "freshness_policy_id": LIVE_FEATURE_FRESHNESS_POLICY_ID,
            "freshness_max_age_seconds": max_age_seconds,
            "freshness_timestamp": freshness_text,
            "market_event_sha256": event_sha256,
            "market_snapshot_sha256": snapshot_sha256,
        }
        result.append(
            LiveFeatureObservation(
                input_id=wanted_input,
                quote_key=event.quote_key,
                feature=feature,
                available_at=available_text,
                decision_at=decision_text,
                freshness_policy_id=LIVE_FEATURE_FRESHNESS_POLICY_ID,
                freshness_max_age_seconds=max_age_seconds,
                freshness_timestamp=freshness_text,
                market_event_sha256=event_sha256,
                market_snapshot_sha256=snapshot_sha256,
                authority_sha256=authority.authority_sha256,
                evidence_sha256=_canonical_json_sha256(evidence_payload),
            )
        )

    return tuple(result)


__all__ = [
    "LIVE_FEATURE_DEFINITION_JSON",
    "LIVE_FEATURE_DEFINITION_SHA256",
    "LIVE_FEATURE_FRESHNESS_POLICY_ID",
    "LIVE_FEATURE_SET_ID",
    "LIVE_FEATURE_SET_VERSION",
    "LIVE_FEATURE_SOURCE_CONTRACT_JSON",
    "LIVE_FEATURE_SOURCE_SHA256",
    "LiveFeatureObservation",
    "RegisteredLiveFeatureAuthority",
    "RegisteredStrategyLiveFeatureError",
    "market_snapshot_sha256",
    "observe_registered_strategy_live_features",
    "resolve_registered_live_feature_authority",
]
