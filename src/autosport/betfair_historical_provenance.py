from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from .historical_data_fidelity import HistoricalDataFidelity


_PROVIDER = "BETFAIR"
_SOURCE_KIND = "HISTORICAL_MARKET_STREAM"


class HistoricalFileLayout(str, Enum):
    MARKET_FILE = "M"
    EVENT_FILE = "E"


class EvaluationStratum(str, Enum):
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    DELAYED_FORWARD = "DELAYED_FORWARD"
    LIVE_READ_FORWARD = "LIVE_READ_FORWARD"
    PAPER_EXECUTION = "PAPER_EXECUTION"
    SUPERVISED_EXECUTION = "SUPERVISED_EXECUTION"


class StreamKeyClass(str, Enum):
    DELAYED = "DELAYED"
    LIVE = "LIVE"


class FileRedownloadDisposition(str, Enum):
    DUPLICATE_BYTES = "DUPLICATE_BYTES"
    NEW_IMMUTABLE_REVISION = "NEW_IMMUTABLE_REVISION"


def _text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical string")
    return value


def _sha256(value: str, *, field: str) -> str:
    digest = _text(value, field=field)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError(f"{field} must be a canonical lowercase SHA-256 digest")
    return digest


def _timestamp(value: str, *, field: str) -> datetime:
    canonical = _text(value, field=field)
    try:
        parsed = datetime.fromisoformat(canonical.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def _utc_iso(value: str, *, field: str) -> str:
    return (
        _timestamp(value, field=field)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _date(value: str, *, field: str) -> date:
    canonical = _text(value, field=field)
    try:
        return date.fromisoformat(canonical)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 calendar date") from exc


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalFileEvidence:
    purchase_item_id: str
    provider_file_path: str
    file_layout: HistoricalFileLayout
    file_sha256: str
    file_size: int
    provider_publish_start_ts: str
    provider_publish_end_ts: str
    settlement_available_ts: str
    acquisition_ts: str
    raw_retention_reference: str
    market_id: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.purchase_item_id, field="purchase_item_id")
        _text(self.provider_file_path, field="provider_file_path")
        if type(self.file_layout) is not HistoricalFileLayout:
            raise TypeError("file_layout must be HistoricalFileLayout")
        _sha256(self.file_sha256, field="file_sha256")
        if type(self.file_size) is not int or self.file_size <= 0:
            raise ValueError("file_size must be a positive integer")
        start = _timestamp(self.provider_publish_start_ts, field="provider_publish_start_ts")
        end = _timestamp(self.provider_publish_end_ts, field="provider_publish_end_ts")
        available = _timestamp(self.settlement_available_ts, field="settlement_available_ts")
        acquired = _timestamp(self.acquisition_ts, field="acquisition_ts")
        _text(self.raw_retention_reference, field="raw_retention_reference")
        if start > end:
            raise ValueError("provider publish range must be monotonic")
        if end > available:
            raise ValueError("historical settlement availability must not precede provider publish end")
        if available > acquired:
            raise ValueError("historical acquisition must not precede settlement availability")
        if self.market_id is not None:
            _text(self.market_id, field="market_id")
        if self.event_id is not None:
            _text(self.event_id, field="event_id")
        if self.file_layout is HistoricalFileLayout.MARKET_FILE and self.market_id is None:
            raise ValueError("MARKET_FILE requires market_id")
        if self.file_layout is HistoricalFileLayout.EVENT_FILE and self.event_id is None:
            raise ValueError("EVENT_FILE requires event_id")

    def authority_payload(self) -> dict[str, Any]:
        return {
            "purchase_item_id": self.purchase_item_id,
            "provider_file_path": self.provider_file_path,
            "file_layout": self.file_layout.value,
            "file_sha256": self.file_sha256,
            "file_size": self.file_size,
            "provider_publish_start_ts": self.provider_publish_start_ts,
            "provider_publish_end_ts": self.provider_publish_end_ts,
            "settlement_available_ts": self.settlement_available_ts,
            "acquisition_ts": self.acquisition_ts,
            "raw_retention_reference": self.raw_retention_reference,
            "market_id": self.market_id,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentProvenance:
    dataset_id: str
    dataset_sha256: str
    source_identity: str
    observed_at: str
    license_or_terms_reference: str
    join_policy_id: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.dataset_id, field="enrichment.dataset_id")
        _sha256(self.dataset_sha256, field="enrichment.dataset_sha256")
        _text(self.source_identity, field="enrichment.source_identity")
        _timestamp(self.observed_at, field="enrichment.observed_at")
        _text(self.license_or_terms_reference, field="enrichment.license_or_terms_reference")
        _text(self.join_policy_id, field="enrichment.join_policy_id")
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ValueError("enrichment.capabilities must be a non-empty tuple")
        normalized = tuple(sorted(self.capabilities))
        if normalized != self.capabilities:
            raise ValueError("enrichment.capabilities must be sorted canonically")
        if len(set(normalized)) != len(normalized):
            raise ValueError("enrichment.capabilities must be unique")
        for capability in normalized:
            _text(capability, field="enrichment.capabilities")

    def authority_payload(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_sha256": self.dataset_sha256,
            "source_identity": self.source_identity,
            "observed_at": self.observed_at,
            "license_or_terms_reference": self.license_or_terms_reference,
            "join_policy_id": self.join_policy_id,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class BetfairHistoricalSnapshot:
    dataset_id: str
    sport: str
    request_start_date: str
    request_end_date: str
    package_fidelity: HistoricalDataFidelity
    parser_version: str
    jurisdiction_class: str
    files: tuple[HistoricalFileEvidence, ...]
    enrichments: tuple[EnrichmentProvenance, ...] = ()

    def __post_init__(self) -> None:
        _text(self.dataset_id, field="dataset_id")
        _text(self.sport, field="sport")
        start = _date(self.request_start_date, field="request_start_date")
        end = _date(self.request_end_date, field="request_end_date")
        if start > end:
            raise ValueError("requested date interval must be monotonic")
        if type(self.package_fidelity) is not HistoricalDataFidelity:
            raise TypeError("package_fidelity must be HistoricalDataFidelity")
        _text(self.parser_version, field="parser_version")
        _text(self.jurisdiction_class, field="jurisdiction_class")
        if not isinstance(self.files, tuple) or not self.files:
            raise ValueError("files must be a non-empty tuple")
        if any(not isinstance(item, HistoricalFileEvidence) for item in self.files):
            raise TypeError("files must contain HistoricalFileEvidence values")
        layouts = {item.file_layout for item in self.files}
        if len(layouts) != 1:
            raise ValueError(
                "one snapshot must use one canonical historical file layout; M/E copies are not independent observations"
            )
        file_paths = [item.provider_file_path for item in self.files]
        if len(set(file_paths)) != len(file_paths):
            raise ValueError("provider_file_path must be unique within one immutable snapshot")
        if self.canonical_layout is HistoricalFileLayout.MARKET_FILE:
            market_ids = [item.market_id for item in self.files]
            if len(set(market_ids)) != len(market_ids):
                raise ValueError("one immutable MARKET_FILE snapshot may contain each market_id only once")
        else:
            event_ids = [item.event_id for item in self.files]
            if len(set(event_ids)) != len(event_ids):
                raise ValueError("one immutable EVENT_FILE snapshot may contain each event_id only once")
        if not isinstance(self.enrichments, tuple):
            raise TypeError("enrichments must be a tuple")
        if any(not isinstance(item, EnrichmentProvenance) for item in self.enrichments):
            raise TypeError("enrichments must contain EnrichmentProvenance values")
        enrichment_ids = [item.dataset_id for item in self.enrichments]
        if len(set(enrichment_ids)) != len(enrichment_ids):
            raise ValueError("enrichment dataset identities must be unique")

    @property
    def provider(self) -> str:
        return _PROVIDER

    @property
    def source_kind(self) -> str:
        return _SOURCE_KIND

    @property
    def canonical_layout(self) -> HistoricalFileLayout:
        return self.files[0].file_layout

    @property
    def historical_market_data_proves_execution(self) -> bool:
        return False

    @property
    def external_licensing_authority_verified(self) -> bool:
        """This structural record carries references; it does not verify legal authority."""
        return False

    @property
    def provider_account_capability_verified(self) -> bool:
        """Jurisdiction/account class is provenance metadata, not provider-issued authority."""
        return False

    @property
    def snapshot_identity_sha256(self) -> str:
        file_payloads = sorted(
            (item.authority_payload() for item in self.files),
            key=lambda item: (
                item["provider_file_path"],
                item["file_sha256"],
                item["purchase_item_id"],
            ),
        )
        enrichment_payloads = sorted(
            (item.authority_payload() for item in self.enrichments),
            key=lambda item: item["dataset_id"],
        )
        return _canonical_digest(
            {
                "schema_version": 1,
                "provider": self.provider,
                "source_kind": self.source_kind,
                "dataset_id": self.dataset_id,
                "sport": self.sport,
                "request_start_date": self.request_start_date,
                "request_end_date": self.request_end_date,
                "package_fidelity": self.package_fidelity.name,
                "parser_version": self.parser_version,
                "jurisdiction_class": self.jurisdiction_class,
                "canonical_layout": self.canonical_layout.value,
                "files": file_payloads,
                "enrichments": enrichment_payloads,
                "historical_market_data_proves_execution": False,
                "external_licensing_authority_verified": False,
                "provider_account_capability_verified": False,
            }
        )


def canonical_provider_change_identity(
    *,
    market_id: str,
    provider_publish_ts: str,
    change_payload_sha256: str,
) -> str:
    """Identity for one provider market change independent of M/E archive layout."""

    return _canonical_digest(
        {
            "provider": _PROVIDER,
            "market_id": _text(market_id, field="market_id"),
            "provider_publish_ts": _utc_iso(
                provider_publish_ts,
                field="provider_publish_ts",
            ),
            "change_payload_sha256": _sha256(
                change_payload_sha256,
                field="change_payload_sha256",
            ),
        }
    )


def classify_redownload(
    previous: HistoricalFileEvidence,
    current: HistoricalFileEvidence,
) -> FileRedownloadDisposition:
    if not isinstance(previous, HistoricalFileEvidence) or not isinstance(current, HistoricalFileEvidence):
        raise TypeError("previous and current must be HistoricalFileEvidence")
    if previous.purchase_item_id != current.purchase_item_id:
        raise ValueError("redownload comparison requires the same purchase_item_id")
    if previous.provider_file_path != current.provider_file_path:
        raise ValueError("redownload comparison requires the same provider_file_path")
    if previous.file_sha256 == current.file_sha256 and previous.file_size == current.file_size:
        return FileRedownloadDisposition.DUPLICATE_BYTES
    return FileRedownloadDisposition.NEW_IMMUTABLE_REVISION


@dataclass(frozen=True, slots=True)
class HistoricalFeatureFact:
    provider_publish_ts: str
    contains_settlement_or_outcome: bool = False
    settlement_available_ts: str | None = None

    def __post_init__(self) -> None:
        _timestamp(self.provider_publish_ts, field="provider_publish_ts")
        if type(self.contains_settlement_or_outcome) is not bool:
            raise TypeError("contains_settlement_or_outcome must be bool")
        if self.contains_settlement_or_outcome:
            if self.settlement_available_ts is None:
                raise ValueError(
                    "settlement/outcome fact requires settlement_available_ts"
                )
            available = _timestamp(
                self.settlement_available_ts,
                field="settlement_available_ts",
            )
            published = _timestamp(
                self.provider_publish_ts,
                field="provider_publish_ts",
            )
            if available < published:
                raise ValueError(
                    "settlement availability must not precede provider publish time"
                )
        elif self.settlement_available_ts is not None:
            _timestamp(self.settlement_available_ts, field="settlement_available_ts")


def historical_fact_visible_at(
    fact: HistoricalFeatureFact,
    *,
    decision_cutoff_ts: str,
) -> bool:
    if not isinstance(fact, HistoricalFeatureFact):
        raise TypeError("fact must be HistoricalFeatureFact")
    cutoff = _timestamp(decision_cutoff_ts, field="decision_cutoff_ts")
    published = _timestamp(fact.provider_publish_ts, field="provider_publish_ts")
    if published > cutoff:
        return False
    if fact.contains_settlement_or_outcome:
        assert fact.settlement_available_ts is not None
        return _timestamp(
            fact.settlement_available_ts,
            field="settlement_available_ts",
        ) <= cutoff
    return True


@dataclass(frozen=True, slots=True)
class StreamCaptureEvidence:
    capture_id: str
    key_class: StreamKeyClass
    configured_delay_seconds: int
    observed_at: str

    def __post_init__(self) -> None:
        _text(self.capture_id, field="capture_id")
        if type(self.key_class) is not StreamKeyClass:
            raise TypeError("key_class must be StreamKeyClass")
        if type(self.configured_delay_seconds) is not int:
            raise TypeError("configured_delay_seconds must be int")
        _timestamp(self.observed_at, field="observed_at")
        if self.key_class is StreamKeyClass.DELAYED:
            if not 1 <= self.configured_delay_seconds <= 180:
                raise ValueError("DELAYED stream delay must be within 1..180 seconds")
        elif self.configured_delay_seconds != 0:
            raise ValueError("LIVE stream must declare zero configured delay")


@dataclass(frozen=True, slots=True)
class StratumQualification:
    compatible: bool
    requested: EvaluationStratum
    promotion_authorized: bool
    reason: str

    def __post_init__(self) -> None:
        if self.promotion_authorized:
            raise ValueError(
                "provenance compatibility alone must never authorize model/policy promotion"
            )


def qualify_historical_stratum(
    snapshot: BetfairHistoricalSnapshot,
    requested: EvaluationStratum,
) -> StratumQualification:
    if not isinstance(snapshot, BetfairHistoricalSnapshot):
        raise TypeError("snapshot must be BetfairHistoricalSnapshot")
    if type(requested) is not EvaluationStratum:
        raise TypeError("requested must be EvaluationStratum")
    if requested is EvaluationStratum.HISTORICAL_REPLAY:
        return StratumQualification(True, requested, False, "historical archive is offline replay evidence")
    return StratumQualification(
        False,
        requested,
        False,
        "settled historical archive cannot be promoted to forward/live/execution evidence",
    )


def qualify_stream_stratum(
    capture: StreamCaptureEvidence,
    requested: EvaluationStratum,
) -> StratumQualification:
    if not isinstance(capture, StreamCaptureEvidence):
        raise TypeError("capture must be StreamCaptureEvidence")
    if type(requested) is not EvaluationStratum:
        raise TypeError("requested must be EvaluationStratum")
    if capture.key_class is StreamKeyClass.DELAYED:
        qualified = requested is EvaluationStratum.DELAYED_FORWARD
        return StratumQualification(
            qualified,
            requested,
            False,
            "delayed stream supports DELAYED_FORWARD only; provenance alone is not promotion authority",
        )
    qualified = requested is EvaluationStratum.LIVE_READ_FORWARD
    return StratumQualification(
        qualified,
        requested,
        False,
        "live read stream supports LIVE_READ_FORWARD only; provider authority and execution evidence remain separate",
    )


def enrichment_declares_feature_at(
    snapshot: BetfairHistoricalSnapshot,
    *,
    capability: str,
    decision_cutoff_ts: str,
) -> bool:
    """Return causal declaration availability, never licensing/source authority.

    A True result means only that an explicitly bound enrichment artifact declares the
    capability and was observed by the cutoff. A separate authority verifier must decide
    whether that source is lawful/authorized for the intended use.
    """
    if not isinstance(snapshot, BetfairHistoricalSnapshot):
        raise TypeError("snapshot must be BetfairHistoricalSnapshot")
    wanted = _text(capability, field="capability")
    cutoff = _timestamp(decision_cutoff_ts, field="decision_cutoff_ts")
    for enrichment in snapshot.enrichments:
        if wanted not in enrichment.capabilities:
            continue
        observed = _timestamp(enrichment.observed_at, field="enrichment.observed_at")
        if observed <= cutoff:
            return True
    return False
