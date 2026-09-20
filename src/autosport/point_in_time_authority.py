from __future__ import annotations

"""Point-in-time research-data authority for Autosport.

This module does not create a second dataset or scientific registry. It binds
research/evaluation callers to the canonical DatasetSnapshot and FeatureSet
identities, then adds two missing fail-closed guarantees:

* feature/source revisions must have been causally available at the evaluated
  decision cutoff; and
* a confirmation/holdout window has one durable alias-resistant consumption
  identity and cannot silently become "fresh" again after restart.

The durable ledger reuses Autosport's canonical workspace writer lock, strict
JSON decoder and atomic JSON publication.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Mapping

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .scientific_registry import (
    DatasetSnapshot,
    FeatureSet,
    promotion_holdout_access_id,
)
from .workspace_lock import WorkspaceEconomicLock


_HEX: Final = frozenset("0123456789abcdef")
_LEDGER_SCHEMA: Final = "autosport.holdout_consumption_ledger"
_LEDGER_SCHEMA_VERSION: Final = 1
_LEDGER_ROOT_KEYS: Final = frozenset({"schema", "schema_version", "receipts"})
_RECEIPT_KEYS: Final = frozenset(
    {
        "holdout_access_id",
        "dataset_snapshot_id",
        "dataset_manifest_sha256",
        "source_identity",
        "license_identity",
        "research_protocol_id",
        "confirmation_trial_family_id",
        "consumer_identity",
        "purpose",
        "consumed_at",
        "receipt_sha256",
    }
)


class PointInTimeAuthorityError(ValueError):
    """Base error for point-in-time evidence and holdout authority."""


class FeatureAvailabilityError(PointInTimeAuthorityError):
    """Feature/source evidence is not causally available at decision time."""


class HoldoutConsumptionError(PointInTimeAuthorityError):
    """Holdout consumption evidence is malformed, unavailable or conflicting."""


class HoldoutAlreadyConsumedError(HoldoutConsumptionError):
    """The same semantic confirmation/holdout evidence was already consumed."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PointInTimeAuthorityError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PointInTimeAuthorityError(f"{name} must contain valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise PointInTimeAuthorityError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return text


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PointInTimeAuthorityError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PointInTimeAuthorityError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PointInTimeAuthorityError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PointInTimeAuthorityError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    normalized = _aware_utc(value, "timestamp")
    return normalized.isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _require_exact_keys(
    name: str,
    payload: dict[str, object],
    expected: frozenset[str],
) -> None:
    keys = frozenset(payload)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise HoldoutConsumptionError(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityEvidence:
    """Self-contained proof that one feature revision was knowable in time."""

    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    source_identity: str
    license_identity: str
    dataset_causal_cutoff: datetime
    dataset_available_at: datetime
    feature_set_id: str
    feature_definition_sha256: str
    feature_source_sha256: str
    feature_set_available_at: datetime
    feature_name: str
    source_revision: str
    source_revision_sha256: str
    revision_policy_id: str
    revision_policy_sha256: str
    source_as_of: datetime
    available_at: datetime
    decision_cutoff: datetime

    def __post_init__(self) -> None:
        for name in (
            "dataset_snapshot_id",
            "source_identity",
            "license_identity",
            "feature_set_id",
            "feature_name",
            "source_revision",
            "revision_policy_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "dataset_manifest_sha256",
            "feature_definition_sha256",
            "feature_source_sha256",
            "source_revision_sha256",
            "revision_policy_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))

        for name in (
            "dataset_causal_cutoff",
            "dataset_available_at",
            "feature_set_available_at",
            "source_as_of",
            "available_at",
            "decision_cutoff",
        ):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))

        if self.dataset_causal_cutoff > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "dataset causal cutoff is after the evaluated decision cutoff"
            )
        if self.dataset_available_at > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "dataset snapshot was not available at the evaluated decision cutoff"
            )
        if self.feature_set_available_at > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "feature-set definition was not available at the evaluated decision cutoff"
            )
        if self.source_as_of > self.available_at:
            raise FeatureAvailabilityError(
                "source revision cannot become available before its as-of instant"
            )
        if self.available_at > self.decision_cutoff:
            raise FeatureAvailabilityError(
                "feature revision became available after the evaluated decision cutoff"
            )

    @classmethod
    def from_canonical(
        cls,
        *,
        dataset_snapshot: DatasetSnapshot,
        feature_set: FeatureSet,
        feature_name: str,
        source_revision: str,
        source_revision_sha256: str,
        revision_policy_id: str,
        revision_policy_sha256: str,
        source_as_of: datetime,
        available_at: datetime,
        decision_cutoff: datetime,
    ) -> "FeatureAvailabilityEvidence":
        """Bind evidence to existing canonical dataset and feature records."""

        if not isinstance(dataset_snapshot, DatasetSnapshot):
            raise PointInTimeAuthorityError(
                "dataset_snapshot must be a canonical DatasetSnapshot"
            )
        if not isinstance(feature_set, FeatureSet):
            raise PointInTimeAuthorityError(
                "feature_set must be a canonical FeatureSet"
            )
        return cls(
            dataset_snapshot_id=dataset_snapshot.dataset_snapshot_id,
            dataset_manifest_sha256=dataset_snapshot.manifest_sha256,
            source_identity=dataset_snapshot.source_identity,
            license_identity=dataset_snapshot.license_identity,
            dataset_causal_cutoff=_instant(
                dataset_snapshot.causal_cutoff,
                "dataset_snapshot.causal_cutoff",
            ),
            dataset_available_at=_instant(
                dataset_snapshot.available_at_utc,
                "dataset_snapshot.available_at_utc",
            ),
            feature_set_id=feature_set.feature_set_id,
            feature_definition_sha256=feature_set.definition_sha256,
            feature_source_sha256=feature_set.source_sha256,
            feature_set_available_at=_instant(
                feature_set.available_at_utc,
                "feature_set.available_at_utc",
            ),
            feature_name=feature_name,
            source_revision=source_revision,
            source_revision_sha256=source_revision_sha256,
            revision_policy_id=revision_policy_id,
            revision_policy_sha256=revision_policy_sha256,
            source_as_of=source_as_of,
            available_at=available_at,
            decision_cutoff=decision_cutoff,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.feature_availability_evidence",
            "schema_version": 1,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "dataset_causal_cutoff": _iso(self.dataset_causal_cutoff),
            "dataset_available_at": _iso(self.dataset_available_at),
            "feature_set_id": self.feature_set_id,
            "feature_definition_sha256": self.feature_definition_sha256,
            "feature_source_sha256": self.feature_source_sha256,
            "feature_set_available_at": _iso(self.feature_set_available_at),
            "feature_name": self.feature_name,
            "source_revision": self.source_revision,
            "source_revision_sha256": self.source_revision_sha256,
            "revision_policy_id": self.revision_policy_id,
            "revision_policy_sha256": self.revision_policy_sha256,
            "source_as_of": _iso(self.source_as_of),
            "available_at": _iso(self.available_at),
            "decision_cutoff": _iso(self.decision_cutoff),
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class HoldoutConsumptionReceipt:
    """Immutable receipt for one semantic confirmation/holdout consumption."""

    holdout_access_id: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    source_identity: str
    license_identity: str
    research_protocol_id: str
    confirmation_trial_family_id: str
    consumer_identity: str
    purpose: str
    consumed_at: datetime
    receipt_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "dataset_snapshot_id",
            "source_identity",
            "license_identity",
            "research_protocol_id",
            "confirmation_trial_family_id",
            "consumer_identity",
            "purpose",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(
            self,
            "holdout_access_id",
            _sha256(self.holdout_access_id, "holdout_access_id"),
        )
        object.__setattr__(
            self,
            "dataset_manifest_sha256",
            _sha256(self.dataset_manifest_sha256, "dataset_manifest_sha256"),
        )
        object.__setattr__(
            self,
            "consumed_at",
            _aware_utc(self.consumed_at, "consumed_at"),
        )
        expected = _digest(self.to_payload(include_digest=False))
        supplied = _sha256(self.receipt_sha256, "receipt_sha256")
        if supplied != expected:
            raise HoldoutConsumptionError("holdout receipt digest mismatch")
        object.__setattr__(self, "receipt_sha256", supplied)

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "holdout_access_id": self.holdout_access_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "research_protocol_id": self.research_protocol_id,
            "confirmation_trial_family_id": self.confirmation_trial_family_id,
            "consumer_identity": self.consumer_identity,
            "purpose": self.purpose,
            "consumed_at": _iso(self.consumed_at),
        }
        if include_digest:
            payload["receipt_sha256"] = self.receipt_sha256
        return payload

    @classmethod
    def create(
        cls,
        *,
        holdout_access_id: str,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        consumer_identity: str,
        purpose: str,
        consumed_at: datetime,
    ) -> "HoldoutConsumptionReceipt":
        consumed = _aware_utc(consumed_at, "consumed_at")
        base: dict[str, object] = {
            "holdout_access_id": _sha256(holdout_access_id, "holdout_access_id"),
            "dataset_snapshot_id": _text(
                dataset_snapshot.dataset_snapshot_id,
                "dataset_snapshot_id",
            ),
            "dataset_manifest_sha256": _sha256(
                dataset_snapshot.manifest_sha256,
                "dataset_manifest_sha256",
            ),
            "source_identity": _text(
                dataset_snapshot.source_identity,
                "source_identity",
            ),
            "license_identity": _text(
                dataset_snapshot.license_identity,
                "license_identity",
            ),
            "research_protocol_id": _text(
                research_protocol_id,
                "research_protocol_id",
            ),
            "confirmation_trial_family_id": _text(
                confirmation_trial_family_id,
                "confirmation_trial_family_id",
            ),
            "consumer_identity": _text(consumer_identity, "consumer_identity"),
            "purpose": _text(purpose, "purpose"),
            "consumed_at": _iso(consumed),
        }
        return cls(
            holdout_access_id=base["holdout_access_id"],
            dataset_snapshot_id=base["dataset_snapshot_id"],
            dataset_manifest_sha256=base["dataset_manifest_sha256"],
            source_identity=base["source_identity"],
            license_identity=base["license_identity"],
            research_protocol_id=base["research_protocol_id"],
            confirmation_trial_family_id=base["confirmation_trial_family_id"],
            consumer_identity=base["consumer_identity"],
            purpose=base["purpose"],
            consumed_at=consumed,
            receipt_sha256=_digest(base),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "HoldoutConsumptionReceipt":
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise HoldoutConsumptionError("holdout receipt must be a JSON object")
        body: dict[str, object] = payload
        _require_exact_keys("holdout receipt", body, _RECEIPT_KEYS)
        return cls(
            holdout_access_id=body["holdout_access_id"],
            dataset_snapshot_id=body["dataset_snapshot_id"],
            dataset_manifest_sha256=body["dataset_manifest_sha256"],
            source_identity=body["source_identity"],
            license_identity=body["license_identity"],
            research_protocol_id=body["research_protocol_id"],
            confirmation_trial_family_id=body["confirmation_trial_family_id"],
            consumer_identity=body["consumer_identity"],
            purpose=body["purpose"],
            consumed_at=_instant(body["consumed_at"], "consumed_at"),
            receipt_sha256=body["receipt_sha256"],
        )


def holdout_identity(
    *,
    dataset_snapshot: DatasetSnapshot,
    research_protocol_id: str,
    confirmation_trial_family_id: str,
) -> str:
    """Return the canonical alias-resistant holdout access identity."""

    if not isinstance(dataset_snapshot, DatasetSnapshot):
        raise HoldoutConsumptionError(
            "dataset_snapshot must be a canonical DatasetSnapshot"
        )
    return promotion_holdout_access_id(
        research_protocol_id=research_protocol_id,
        dataset_manifest_sha256=dataset_snapshot.manifest_sha256,
        source_identity=dataset_snapshot.source_identity,
        license_identity=dataset_snapshot.license_identity,
        confirmation_trial_family_id=confirmation_trial_family_id,
    )


def _validate_holdout_available(
    dataset_snapshot: DatasetSnapshot,
    *,
    consumed_at: datetime,
) -> None:
    consumed = _aware_utc(consumed_at, "consumed_at")
    snapshot_available = _instant(
        dataset_snapshot.available_at_utc,
        "dataset_snapshot.available_at_utc",
    )
    causal_cutoff = _instant(
        dataset_snapshot.causal_cutoff,
        "dataset_snapshot.causal_cutoff",
    )
    if snapshot_available > consumed:
        raise HoldoutConsumptionError(
            "dataset snapshot was not available when holdout consumption was recorded"
        )
    if causal_cutoff > consumed:
        raise HoldoutConsumptionError(
            "dataset causal cutoff is after holdout consumption time"
        )
    if dataset_snapshot.outcome_reveal_after is not None:
        reveal = _instant(
            dataset_snapshot.outcome_reveal_after,
            "dataset_snapshot.outcome_reveal_after",
        )
        if reveal > consumed:
            raise HoldoutConsumptionError(
                "confirmation outcome was not revealed at holdout consumption time"
            )


class HoldoutConsumptionLedger:
    """Restart-safe immutable ledger for confirmation/holdout consumption."""

    FILE_NAME: Final = "holdout_consumption_ledger.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def _decode(self, text: str) -> tuple[HoldoutConsumptionReceipt, ...]:
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise HoldoutConsumptionError("invalid holdout ledger JSON") from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise HoldoutConsumptionError("holdout ledger must be a JSON object")
        root: dict[str, object] = raw
        _require_exact_keys("holdout ledger", root, _LEDGER_ROOT_KEYS)
        if root["schema"] != _LEDGER_SCHEMA:
            raise HoldoutConsumptionError("unsupported holdout ledger schema")
        version = root["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise HoldoutConsumptionError("holdout ledger schema_version must be an integer")
        if version != _LEDGER_SCHEMA_VERSION:
            raise HoldoutConsumptionError("unsupported holdout ledger schema_version")
        raw_receipts = root["receipts"]
        if not isinstance(raw_receipts, list):
            raise HoldoutConsumptionError("holdout ledger receipts must be a JSON array")

        receipts = tuple(
            HoldoutConsumptionReceipt.from_payload(payload)
            for payload in raw_receipts
        )
        seen: set[str] = set()
        for receipt in receipts:
            if receipt.holdout_access_id in seen:
                raise HoldoutConsumptionError(
                    "holdout ledger contains duplicate semantic consumption identity"
                )
            seen.add(receipt.holdout_access_id)
        return receipts

    def _load_unlocked(self) -> tuple[HoldoutConsumptionReceipt, ...]:
        if not self.path.exists():
            return ()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HoldoutConsumptionError(f"cannot read holdout ledger: {exc}") from exc
        return self._decode(text)

    def receipts(self) -> tuple[HoldoutConsumptionReceipt, ...]:
        return self._load_unlocked()

    @staticmethod
    def _payload(
        receipts: tuple[HoldoutConsumptionReceipt, ...],
    ) -> dict[str, object]:
        return {
            "schema": _LEDGER_SCHEMA,
            "schema_version": _LEDGER_SCHEMA_VERSION,
            "receipts": [receipt.to_payload() for receipt in receipts],
        }

    def receipt_for(self, holdout_access_id: str) -> HoldoutConsumptionReceipt | None:
        target = _sha256(holdout_access_id, "holdout_access_id")
        for receipt in self._load_unlocked():
            if receipt.holdout_access_id == target:
                return receipt
        return None

    def is_consumed(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
    ) -> bool:
        access_id = holdout_identity(
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )
        return self.receipt_for(access_id) is not None

    def consume(
        self,
        *,
        dataset_snapshot: DatasetSnapshot,
        research_protocol_id: str,
        confirmation_trial_family_id: str,
        consumer_identity: str,
        purpose: str,
        consumed_at: datetime,
    ) -> HoldoutConsumptionReceipt:
        """Atomically consume one semantic holdout identity exactly once."""

        if not isinstance(dataset_snapshot, DatasetSnapshot):
            raise HoldoutConsumptionError(
                "dataset_snapshot must be a canonical DatasetSnapshot"
            )
        _validate_holdout_available(dataset_snapshot, consumed_at=consumed_at)
        access_id = holdout_identity(
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=research_protocol_id,
            confirmation_trial_family_id=confirmation_trial_family_id,
        )

        with WorkspaceEconomicLock(self.workspace):
            current = self._load_unlocked()
            for existing in current:
                if existing.holdout_access_id == access_id:
                    raise HoldoutAlreadyConsumedError(
                        "confirmation/holdout evidence was already consumed"
                    )
            receipt = HoldoutConsumptionReceipt.create(
                holdout_access_id=access_id,
                dataset_snapshot=dataset_snapshot,
                research_protocol_id=research_protocol_id,
                confirmation_trial_family_id=confirmation_trial_family_id,
                consumer_identity=consumer_identity,
                purpose=purpose,
                consumed_at=consumed_at,
            )
            atomic_write_json(self.path, self._payload(current + (receipt,)))
            return receipt


__all__ = [
    "FeatureAvailabilityError",
    "FeatureAvailabilityEvidence",
    "HoldoutAlreadyConsumedError",
    "HoldoutConsumptionError",
    "HoldoutConsumptionLedger",
    "HoldoutConsumptionReceipt",
    "PointInTimeAuthorityError",
    "holdout_identity",
]
