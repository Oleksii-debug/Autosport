from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .scientific_registry import DatasetSnapshot, FeatureSet, promotion_holdout_access_id
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA_VERSION = 1
_FEATURE_REVISION_POLICY_ID = "scientific-registry-feature-set-v1"
_HEX = frozenset("0123456789abcdef")


class PointInTimeEvidenceError(ValueError):
    """Base class for point-in-time evidence failures."""


class FutureEvidenceError(PointInTimeEvidenceError):
    """Evidence was not causally available at the decision cutoff."""


class HoldoutAlreadyConsumedError(PointInTimeEvidenceError):
    """A canonical holdout identity was already consumed."""


class EvidenceLedgerCorruptError(PointInTimeEvidenceError):
    """Durable holdout evidence failed structural or digest validation."""


class _HoldoutLedgerLock(WorkspaceEconomicLock):
    """Dedicated crash-releasing lock; do not serialize unrelated economic writers."""

    FILE_NAME = ".holdout-consumption.lock"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PointInTimeEvidenceError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise PointInTimeEvidenceError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PointInTimeEvidenceError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PointInTimeEvidenceError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _holdout_freshness_id(
    *,
    dataset_manifest_sha256: str,
    source_identity: str,
    license_identity: str,
    confirmation_trial_family_id: str,
) -> str:
    """Identity for the physical confirmation evidence, independent of protocol aliases."""
    return _digest(
        {
            "schema_version": _SCHEMA_VERSION,
            "dataset_manifest_sha256": _sha256(dataset_manifest_sha256, "dataset_manifest_sha256"),
            "source_identity": _text(source_identity, "source_identity"),
            "license_identity": _text(license_identity, "license_identity"),
            "confirmation_trial_family_id": _text(
                confirmation_trial_family_id, "confirmation_trial_family_id"
            ),
        }
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityEvidence:
    """Immutable point-in-time proof derived from canonical registry records.

    It stores identities and hashes only. ``DatasetSnapshot`` and ``FeatureSet`` stay
    the canonical data/feature truth; this record only proves what could have been
    known at one decision cutoff.
    """

    feature_identity: str
    feature_version: str
    feature_definition_sha256: str
    source_identity: str
    source_revision: str
    revision_policy_id: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    feature_payload_sha256: str
    as_of_utc: str
    available_at_utc: str
    decision_cutoff_utc: str

    def __post_init__(self) -> None:
        for name in (
            "feature_identity",
            "feature_version",
            "source_identity",
            "revision_policy_id",
            "dataset_snapshot_id",
        ):
            _text(getattr(self, name), name)
        _sha256(self.feature_definition_sha256, "feature_definition_sha256")
        _sha256(self.source_revision, "source_revision")
        _sha256(self.dataset_manifest_sha256, "dataset_manifest_sha256")
        _sha256(self.feature_payload_sha256, "feature_payload_sha256")
        as_of = _instant(self.as_of_utc, "as_of_utc")
        available_at = _instant(self.available_at_utc, "available_at_utc")
        decision_cutoff = _instant(self.decision_cutoff_utc, "decision_cutoff_utc")
        if as_of > decision_cutoff:
            raise FutureEvidenceError("feature as_of is after decision cutoff")
        if available_at > decision_cutoff:
            raise FutureEvidenceError("feature was not available by decision cutoff")

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "feature_identity": self.feature_identity,
            "feature_version": self.feature_version,
            "feature_definition_sha256": self.feature_definition_sha256.lower(),
            "source_identity": self.source_identity,
            "source_revision": self.source_revision.lower(),
            "revision_policy_id": self.revision_policy_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
            "feature_payload_sha256": self.feature_payload_sha256.lower(),
            "as_of_utc": self.as_of_utc,
            "available_at_utc": self.available_at_utc,
            "decision_cutoff_utc": self.decision_cutoff_utc,
        }


class PointInTimeFeatureAuthority:
    """Derive leakage-resistant availability from canonical registry records only."""

    @staticmethod
    def bind(
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        feature_payload_sha256: str,
        decision_cutoff_utc: str,
    ) -> FeatureAvailabilityEvidence:
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise PointInTimeEvidenceError("dataset_snapshot must be an exact DatasetSnapshot")
        if type(feature_set) is not FeatureSet:
            raise PointInTimeEvidenceError("feature_set must be an exact FeatureSet")

        decision_cutoff = _instant(decision_cutoff_utc, "decision_cutoff_utc")
        dataset_cutoff = _instant(dataset_snapshot.causal_cutoff, "dataset_snapshot.causal_cutoff")
        dataset_available = _instant(
            dataset_snapshot.available_at_utc, "dataset_snapshot.available_at"
        )
        feature_available = _instant(feature_set.available_at_utc, "feature_set.available_at")
        if dataset_cutoff > decision_cutoff:
            raise FutureEvidenceError("dataset causal cutoff is after decision cutoff")
        if dataset_available > decision_cutoff:
            raise FutureEvidenceError("dataset snapshot was not available by decision cutoff")
        if feature_available > decision_cutoff:
            raise FutureEvidenceError("feature set was not available by decision cutoff")

        authoritative_available_at = max(dataset_available, feature_available)
        return FeatureAvailabilityEvidence(
            feature_identity=_text(feature_set.feature_set_id, "feature_set.feature_set_id"),
            feature_version=_text(feature_set.version, "feature_set.version"),
            feature_definition_sha256=_sha256(
                feature_set.definition_sha256, "feature_set.definition_sha256"
            ),
            source_identity=_text(dataset_snapshot.source_identity, "dataset_snapshot.source_identity"),
            source_revision=_sha256(feature_set.source_sha256, "feature_set.source_sha256"),
            revision_policy_id=_FEATURE_REVISION_POLICY_ID,
            dataset_snapshot_id=_text(dataset_snapshot.dataset_snapshot_id, "dataset_snapshot_id"),
            dataset_manifest_sha256=_sha256(
                dataset_snapshot.manifest_sha256, "dataset_snapshot.manifest_sha256"
            ),
            feature_payload_sha256=_sha256(feature_payload_sha256, "feature_payload_sha256"),
            as_of_utc=_utc_text(dataset_snapshot.causal_cutoff, "dataset_snapshot.causal_cutoff"),
            available_at_utc=authoritative_available_at.isoformat().replace("+00:00", "Z"),
            decision_cutoff_utc=_utc_text(decision_cutoff_utc, "decision_cutoff_utc"),
        )


@dataclass(frozen=True, slots=True)
class HoldoutConsumption:
    holdout_access_id: str
    holdout_freshness_id: str
    research_protocol_id: str
    confirmation_trial_family_id: str
    dataset_manifest_sha256: str
    source_identity: str
    license_identity: str
    consumer_identity: str
    purpose: str
    consumed_at_utc: str

    def __post_init__(self) -> None:
        _sha256(self.holdout_access_id, "holdout_access_id")
        _sha256(self.holdout_freshness_id, "holdout_freshness_id")
        for name in (
            "research_protocol_id",
            "confirmation_trial_family_id",
            "source_identity",
            "license_identity",
            "consumer_identity",
            "purpose",
        ):
            _text(getattr(self, name), name)
        _sha256(self.dataset_manifest_sha256, "dataset_manifest_sha256")
        _instant(self.consumed_at_utc, "consumed_at_utc")
        expected_access_id = promotion_holdout_access_id(
            research_protocol_id=self.research_protocol_id,
            dataset_manifest_sha256=self.dataset_manifest_sha256,
            source_identity=self.source_identity,
            license_identity=self.license_identity,
            confirmation_trial_family_id=self.confirmation_trial_family_id,
        )
        if self.holdout_access_id != expected_access_id:
            raise PointInTimeEvidenceError("holdout_access_id does not match canonical source identity")
        expected_freshness_id = _holdout_freshness_id(
            dataset_manifest_sha256=self.dataset_manifest_sha256,
            source_identity=self.source_identity,
            license_identity=self.license_identity,
            confirmation_trial_family_id=self.confirmation_trial_family_id,
        )
        if self.holdout_freshness_id != expected_freshness_id:
            raise PointInTimeEvidenceError("holdout_freshness_id does not match canonical source identity")

    @property
    def consumption_id(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "holdout_access_id": self.holdout_access_id,
            "holdout_freshness_id": self.holdout_freshness_id,
            "research_protocol_id": self.research_protocol_id,
            "confirmation_trial_family_id": self.confirmation_trial_family_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "consumer_identity": self.consumer_identity,
            "purpose": self.purpose,
            "consumed_at_utc": self.consumed_at_utc,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HoldoutConsumption":
        if type(payload) is not dict:
            raise EvidenceLedgerCorruptError("holdout record must be an object")
        try:
            return cls(
                holdout_access_id=payload["holdout_access_id"],
                holdout_freshness_id=payload["holdout_freshness_id"],
                research_protocol_id=payload["research_protocol_id"],
                confirmation_trial_family_id=payload["confirmation_trial_family_id"],
                dataset_manifest_sha256=payload["dataset_manifest_sha256"],
                source_identity=payload["source_identity"],
                license_identity=payload["license_identity"],
                consumer_identity=payload["consumer_identity"],
                purpose=payload["purpose"],
                consumed_at_utc=payload["consumed_at_utc"],
            )
        except (KeyError, PointInTimeEvidenceError) as exc:
            raise EvidenceLedgerCorruptError("invalid holdout record") from exc


class HoldoutConsumptionLedger:
    """Append-only durable proof that a canonical confirmation holdout was consumed.

    Freshness deliberately excludes both ``dataset_snapshot_id`` and
    ``research_protocol_id``. Renaming/reloading the same immutable bytes or wrapping
    them in a new protocol therefore cannot manufacture a fresh confirmation set.
    Mutations reload under a dedicated cross-process lock before deciding whether the
    holdout is unused, preventing stale ledger instances from losing a concurrent use.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._records: dict[str, HoldoutConsumption] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def records(self) -> tuple[HoldoutConsumption, ...]:
        with self._lock:
            with _HoldoutLedgerLock(self._path.parent):
                self._load()
                return tuple(self._records[key] for key in sorted(self._records))

    def _load(self) -> None:
        if not self._path.exists():
            self._records = {}
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceLedgerCorruptError("holdout ledger is unreadable") from exc
        if type(payload) is not dict or payload.get("schema_version") != _SCHEMA_VERSION:
            raise EvidenceLedgerCorruptError("holdout ledger schema is unsupported")
        records_payload = payload.get("records")
        if type(records_payload) is not list:
            raise EvidenceLedgerCorruptError("holdout ledger records must be a list")
        declared_digest = payload.get("records_sha256")
        if type(declared_digest) is not str or declared_digest != _digest({"records": records_payload}):
            raise EvidenceLedgerCorruptError("holdout ledger digest mismatch")

        restored: dict[str, HoldoutConsumption] = {}
        for item in records_payload:
            record = HoldoutConsumption.from_payload(item)
            if record.holdout_freshness_id in restored:
                raise EvidenceLedgerCorruptError("duplicate canonical holdout freshness identity")
            restored[record.holdout_freshness_id] = record
        self._records = restored

    def _persist(self) -> None:
        records_payload = [self._records[key].to_payload() for key in sorted(self._records)]
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "records": records_payload,
            "records_sha256": _digest({"records": records_payload}),
        }
        _atomic_write_json(self._path, payload)

    @staticmethod
    def access_id(
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
    ) -> str:
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise PointInTimeEvidenceError("dataset_snapshot must be an exact DatasetSnapshot")
        return promotion_holdout_access_id(
            research_protocol_id=_text(research_protocol_id, "research_protocol_id"),
            dataset_manifest_sha256=_sha256(
                dataset_snapshot.manifest_sha256, "dataset_snapshot.manifest_sha256"
            ),
            source_identity=_text(dataset_snapshot.source_identity, "dataset_snapshot.source_identity"),
            license_identity=_text(dataset_snapshot.license_identity, "dataset_snapshot.license_identity"),
            confirmation_trial_family_id=_text(
                confirmation_trial_family_id, "confirmation_trial_family_id"
            ),
        )

    @staticmethod
    def freshness_id(
        *,
        dataset_snapshot: DatasetSnapshot,
        confirmation_trial_family_id: str,
    ) -> str:
        if type(dataset_snapshot) is not DatasetSnapshot:
            raise PointInTimeEvidenceError("dataset_snapshot must be an exact DatasetSnapshot")
        return _holdout_freshness_id(
            dataset_manifest_sha256=_sha256(
                dataset_snapshot.manifest_sha256, "dataset_snapshot.manifest_sha256"
            ),
            source_identity=_text(dataset_snapshot.source_identity, "dataset_snapshot.source_identity"),
            license_identity=_text(dataset_snapshot.license_identity, "dataset_snapshot.license_identity"),
            confirmation_trial_family_id=_text(
                confirmation_trial_family_id, "confirmation_trial_family_id"
            ),
        )

    def assert_unused(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
    ) -> None:
        _text(research_protocol_id, "research_protocol_id")
        freshness_id = self.freshness_id(
            dataset_snapshot=dataset_snapshot,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        with self._lock:
            with _HoldoutLedgerLock(self._path.parent):
                self._load()
                existing = self._records.get(freshness_id)
        if existing is not None:
            raise HoldoutAlreadyConsumedError(
                f"holdout {existing.holdout_access_id} was already consumed by "
                f"{existing.consumer_identity}"
            )

    def consume(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        consumer_identity: str,
        purpose: str,
        consumed_at_utc: str,
    ) -> HoldoutConsumption:
        access_id = self.access_id(
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        freshness_id = self.freshness_id(
            dataset_snapshot=dataset_snapshot,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        consumed_at = _instant(consumed_at_utc, "consumed_at_utc")
        if consumed_at < _instant(dataset_snapshot.available_at_utc, "dataset_snapshot.available_at"):
            raise FutureEvidenceError("holdout cannot be consumed before the dataset is available")

        record = HoldoutConsumption(
            holdout_access_id=access_id,
            holdout_freshness_id=freshness_id,
            research_protocol_id=_text(research_protocol_id, "research_protocol_id"),
            confirmation_trial_family_id=_text(
                confirmation_trial_family_id, "confirmation_trial_family_id"
            ),
            dataset_manifest_sha256=_sha256(
                dataset_snapshot.manifest_sha256, "dataset_snapshot.manifest_sha256"
            ),
            source_identity=_text(dataset_snapshot.source_identity, "dataset_snapshot.source_identity"),
            license_identity=_text(dataset_snapshot.license_identity, "dataset_snapshot.license_identity"),
            consumer_identity=_text(consumer_identity, "consumer_identity"),
            purpose=_text(purpose, "purpose"),
            consumed_at_utc=_utc_text(consumed_at_utc, "consumed_at_utc"),
        )

        with self._lock:
            with _HoldoutLedgerLock(self._path.parent):
                self._load()
                existing = self._records.get(freshness_id)
                if existing is not None:
                    if (
                        existing.holdout_access_id == record.holdout_access_id
                        and existing.consumer_identity == record.consumer_identity
                        and existing.purpose == record.purpose
                    ):
                        return existing
                    raise HoldoutAlreadyConsumedError(
                        f"holdout {existing.holdout_access_id} was already consumed by "
                        f"{existing.consumer_identity}"
                    )
                self._records[freshness_id] = record
                try:
                    self._persist()
                except BaseException:
                    del self._records[freshness_id]
                    raise
                return record
