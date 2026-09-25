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

from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .scientific_registry import DatasetSnapshot, FeatureSet, promotion_holdout_access_id
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA_VERSION = 1
_FEATURE_REVISION_POLICY_ID = "scientific-registry-feature-set-v1"
_HOLDOUT_AUTHORITY_DOMAIN = "data.point-in-time-holdout-consumption-v1"
_HOLDOUT_TRANSITION_SCHEMA = "autosport.holdout-consumption-transition-v1"
_HEX = frozenset("0123456789abcdef")


class PointInTimeEvidenceError(ValueError):
    """Base class for point-in-time evidence failures."""


class FutureEvidenceError(PointInTimeEvidenceError):
    """Evidence was not causally available at the decision cutoff."""


class HoldoutAlreadyConsumedError(PointInTimeEvidenceError):
    """A canonical holdout identity was already consumed."""


class EvidenceLedgerCorruptError(PointInTimeEvidenceError):
    """Durable holdout evidence failed structural, digest, or freshness validation."""


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


def _optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, name)


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


def _transition_tx_prefix(consumption_id: str) -> str:
    return f"holdout-consumption:{_sha256(consumption_id, 'consumption_id')}:"


def _validate_transition_tx_id(tx_id: object, consumption_id: str) -> str:
    text = _text(tx_id, "authority_tx_id")
    prefix = _transition_tx_prefix(consumption_id)
    if not text.startswith(prefix):
        raise PointInTimeEvidenceError("authority_tx_id does not bind consumption_id")
    attempt = text[len(prefix) :]
    if not attempt.isascii() or not attempt.isdigit() or int(attempt) <= 0:
        raise PointInTimeEvidenceError("authority_tx_id attempt must be a positive integer")
    return text


def _transition_binding(
    *,
    previous_state_sha256: str | None,
    consumption_id: str,
    tx_id: str,
) -> str:
    return _digest(
        {
            "schema": _HOLDOUT_TRANSITION_SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "previous_state_sha256": _optional_sha256(
                previous_state_sha256, "previous_state_sha256"
            ),
            "consumption_id": _sha256(consumption_id, "consumption_id"),
            "tx_id": _validate_transition_tx_id(tx_id, consumption_id),
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


def _physical_holdout_key(
    dataset_snapshot: DatasetSnapshot,
) -> str:
    """Canonical immutable content identity of a physical confirmation dataset."""
    if type(dataset_snapshot) is not DatasetSnapshot:
        raise PointInTimeEvidenceError("dataset_snapshot must be an exact DatasetSnapshot")
    return _sha256(dataset_snapshot.manifest_sha256, "dataset_snapshot.manifest_sha256")


def _find_physical_holdout_consumption(
    records: Mapping[str, HoldoutConsumption],
    *,
    dataset_snapshot: DatasetSnapshot,
) -> HoldoutConsumption | None:
    """Find prior physical consumption despite aliases in provenance or family labels."""
    wanted = _physical_holdout_key(dataset_snapshot)
    for record in records.values():
        existing = _sha256(record.dataset_manifest_sha256, "dataset_manifest_sha256")
        if existing == wanted:
            return record
    return None


class HoldoutConsumptionLedger:
    """Append-only durable proof that a canonical confirmation holdout was consumed.

    Persisted freshness identifiers deliberately exclude both
    ``dataset_snapshot_id`` and ``research_protocol_id``. For schema compatibility
    they retain ``confirmation_trial_family_id`` plus provenance labels, but none of
    those caller-visible aliases are physical freshness authority. Eligibility also
    checks the immutable manifest/content identity, so renaming a snapshot, protocol,
    family, source label, or license label cannot manufacture a fresh confirmation set.

    Every workspace-local ledger version is fenced by the shared, workspace-external
    ``MonotonicWorkspaceAuthority``. A valid older JSON file or a deleted ledger is
    therefore rejected while the independent machine-state authority survives. The
    writer order is workspace lock -> PREPARE -> local atomic publish -> re-read ->
    COMMIT, so both supported crash prefixes recover deterministically.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self._path = Path(path)
        self._workspace = self._path.parent.resolve(strict=False)
        self._lock = threading.RLock()
        self._records: dict[str, HoldoutConsumption] = {}
        self._state_sha256: str | None = None
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self._workspace,
            domain=_HOLDOUT_AUTHORITY_DOMAIN,
            key=f"holdout-ledger:{self._path.name}",
            authority_root=authority_root,
        )
        with self._lock:
            with _HoldoutLedgerLock(self._path.parent):
                self._load()

    @property
    def path(self) -> Path:
        return self._path

    def records(self) -> tuple[HoldoutConsumption, ...]:
        with self._lock:
            with _HoldoutLedgerLock(self._path.parent):
                self._load()
                return tuple(self._records[key] for key in sorted(self._records))

    def _recover_authority(
        self,
        *,
        observed_state_sha256: str | None,
        tx_id: str | None = None,
        semantic_binding_sha256: str | None = None,
    ) -> None:
        try:
            self._authority.recover(
                observed_state_sha256=observed_state_sha256,
                tx_id=tx_id,
                semantic_binding_sha256=semantic_binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise EvidenceLedgerCorruptError(
                "holdout ledger failed independent monotonic authority validation"
            ) from exc

    def _next_transition_tx_id(self, consumption_id: str) -> str:
        prefix = _transition_tx_prefix(consumption_id)
        try:
            history = self._authority.read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise EvidenceLedgerCorruptError(
                "holdout ledger monotonic history is unreadable"
            ) from exc
        attempts: list[int] = []
        for authority_record in history:
            if authority_record.tx_id.startswith(prefix):
                suffix = authority_record.tx_id[len(prefix) :]
                if suffix.isascii() and suffix.isdigit() and int(suffix) > 0:
                    attempts.append(int(suffix))
        return f"{prefix}{max(attempts, default=0) + 1}"

    def _load(self) -> None:
        if not self._path.exists():
            self._recover_authority(observed_state_sha256=None)
            self._records = {}
            self._state_sha256 = None
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceLedgerCorruptError("holdout ledger is unreadable") from exc
        if type(payload) is not dict or payload.get("schema_version") != _SCHEMA_VERSION:
            raise EvidenceLedgerCorruptError("holdout ledger schema is unsupported")
        records_payload = payload.get("records")
        if type(records_payload) is not list or not records_payload:
            raise EvidenceLedgerCorruptError("holdout ledger records must be a non-empty list")
        declared_digest = payload.get("records_sha256")
        if type(declared_digest) is not str or declared_digest != _digest({"records": records_payload}):
            raise EvidenceLedgerCorruptError("holdout ledger digest mismatch")

        try:
            previous_state_sha256 = _optional_sha256(
                payload.get("authority_previous_state_sha256"),
                "authority_previous_state_sha256",
            )
            authority_consumption_id = _sha256(
                payload.get("authority_consumption_id"), "authority_consumption_id"
            )
            authority_tx_id = _validate_transition_tx_id(
                payload.get("authority_tx_id"), authority_consumption_id
            )
            authority_binding = _sha256(
                payload.get("authority_semantic_binding_sha256"),
                "authority_semantic_binding_sha256",
            )
        except PointInTimeEvidenceError as exc:
            raise EvidenceLedgerCorruptError("invalid monotonic authority metadata") from exc

        expected_binding = _transition_binding(
            previous_state_sha256=previous_state_sha256,
            consumption_id=authority_consumption_id,
            tx_id=authority_tx_id,
        )
        if authority_binding != expected_binding:
            raise EvidenceLedgerCorruptError("holdout ledger authority semantic binding mismatch")

        restored: dict[str, HoldoutConsumption] = {}
        authority_record_found = False
        for item in records_payload:
            record = HoldoutConsumption.from_payload(item)
            if record.holdout_freshness_id in restored:
                raise EvidenceLedgerCorruptError("duplicate canonical holdout freshness identity")
            restored[record.holdout_freshness_id] = record
            if record.consumption_id == authority_consumption_id:
                authority_record_found = True
        if not authority_record_found:
            raise EvidenceLedgerCorruptError(
                "holdout ledger authority transition does not name a persisted consumption"
            )

        state_sha256 = _digest(payload)
        self._recover_authority(
            observed_state_sha256=state_sha256,
            tx_id=authority_tx_id,
            semantic_binding_sha256=authority_binding,
        )
        self._records = restored
        self._state_sha256 = state_sha256

    def _payload_for_transition(
        self,
        *,
        added_record: HoldoutConsumption,
        previous_state_sha256: str | None,
        tx_id: str,
    ) -> tuple[dict[str, Any], str]:
        consumption_id = added_record.consumption_id
        tx_id = _validate_transition_tx_id(tx_id, consumption_id)
        semantic_binding = _transition_binding(
            previous_state_sha256=previous_state_sha256,
            consumption_id=consumption_id,
            tx_id=tx_id,
        )
        records_payload = [self._records[key].to_payload() for key in sorted(self._records)]
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "records": records_payload,
            "records_sha256": _digest({"records": records_payload}),
            "authority_previous_state_sha256": previous_state_sha256,
            "authority_consumption_id": consumption_id,
            "authority_tx_id": tx_id,
            "authority_semantic_binding_sha256": semantic_binding,
        }
        return payload, semantic_binding

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
        _text(confirmation_trial_family_id, "confirmation_trial_family_id")
        with self._lock:
            with _HoldoutLedgerLock(self._path.parent):
                self._load()
                existing = _find_physical_holdout_consumption(
                    self._records,
                    dataset_snapshot=dataset_snapshot,
                )
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

                physical_existing = _find_physical_holdout_consumption(
                    self._records,
                    dataset_snapshot=dataset_snapshot,
                )
                if physical_existing is not None:
                    raise HoldoutAlreadyConsumedError(
                        f"physical holdout {physical_existing.holdout_access_id} was already "
                        f"consumed under confirmation trial family "
                        f"{physical_existing.confirmation_trial_family_id}"
                    )

                previous_state_sha256 = self._state_sha256
                self._records[freshness_id] = record
                tx_id = self._next_transition_tx_id(record.consumption_id)
                payload, semantic_binding = self._payload_for_transition(
                    added_record=record,
                    previous_state_sha256=previous_state_sha256,
                    tx_id=tx_id,
                )
                intended_state_sha256 = _digest(payload)
                try:
                    self._authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=previous_state_sha256,
                        intended_state_sha256=intended_state_sha256,
                        semantic_binding_sha256=semantic_binding,
                    )
                    _atomic_write_json(self._path, payload)
                    self._load()
                except MonotonicWorkspaceAuthorityError as exc:
                    self._records.pop(freshness_id, None)
                    self._state_sha256 = previous_state_sha256
                    raise EvidenceLedgerCorruptError(
                        "holdout ledger monotonic transition was rejected"
                    ) from exc
                except BaseException:
                    self._records.pop(freshness_id, None)
                    self._state_sha256 = previous_state_sha256
                    raise
                persisted = self._records.get(freshness_id)
                if persisted != record:
                    raise EvidenceLedgerCorruptError(
                        "holdout ledger publish did not re-resolve the intended consumption"
                    )
                return persisted
