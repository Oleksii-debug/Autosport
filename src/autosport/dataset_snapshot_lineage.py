from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicWorkspaceAuthority,
)
from .scientific_registry import RegistryEntry, ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


_HEX = frozenset("0123456789abcdef")
_MANIFEST_KIND = "autosport-dataset-membership-manifest-v1"
_PROOF_KIND = "autosport-dataset-snapshot-lineage-proof-v1"
_MONOTONIC_DOMAIN = "dataset-snapshot-lineage"
_MONOTONIC_KEY = "append-only-ancestry-v1"
_MONOTONIC_BINDING_KIND = "autosport-dataset-snapshot-lineage-state-v1"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _authority_now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _members(value: object, name: str = "member_sha256") -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    result = tuple(_sha256(item, f"{name} item") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicate members")
    return result


def membership_manifest_sha256(member_sha256: tuple[str, ...]) -> str:
    """Hash one versioned ordered dataset membership manifest.

    Existing/legacy DatasetSnapshot manifests are deliberately not guessed. A snapshot
    is ancestry-provable only when its immutable ``manifest_sha256`` equals this typed
    membership commitment.
    """

    members = _members(member_sha256)
    return _digest(
        {
            "kind": _MANIFEST_KIND,
            "schema_version": 1,
            "member_sha256": list(members),
        }
    )


@dataclass(frozen=True, slots=True)
class DatasetSnapshotLineageRecord:
    snapshot_id: str
    dataset_record_sha256: str
    manifest_sha256: str
    source_identity: str
    license_identity: str
    causal_cutoff: str
    available_at: str
    proof_registered_at: str | None
    member_sha256: tuple[str, ...]
    parent_snapshot_id: str | None
    parent_dataset_record_sha256: str | None
    parent_proof_sha256: str | None
    proof_sha256: str

    def __post_init__(self) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _sha256(self.dataset_record_sha256, "dataset_record_sha256")
        _sha256(self.manifest_sha256, "manifest_sha256")
        _text(self.source_identity, "source_identity")
        _text(self.license_identity, "license_identity")
        _instant(self.causal_cutoff, "causal_cutoff")
        available = _instant(self.available_at, "available_at")
        if self.proof_registered_at is not None:
            registered = _instant(self.proof_registered_at, "proof_registered_at")
            if registered < available:
                raise ValueError(
                    "dataset lineage proof publication predates DatasetSnapshot availability"
                )
        _members(self.member_sha256)
        parent_values = (
            self.parent_snapshot_id,
            self.parent_dataset_record_sha256,
            self.parent_proof_sha256,
        )
        if any(value is None for value in parent_values) and any(
            value is not None for value in parent_values
        ):
            raise ValueError("parent lineage identity must be entirely present or absent")
        if self.parent_snapshot_id is not None:
            _text(self.parent_snapshot_id, "parent_snapshot_id")
            _sha256(self.parent_dataset_record_sha256, "parent_dataset_record_sha256")
            _sha256(self.parent_proof_sha256, "parent_proof_sha256")
            if self.parent_snapshot_id == self.snapshot_id:
                raise ValueError("dataset snapshot cannot parent itself")
        _sha256(self.proof_sha256, "proof_sha256")

    def proof_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": _PROOF_KIND,
            "schema_version": 2 if self.proof_registered_at is not None else 1,
            "snapshot_id": self.snapshot_id,
            "dataset_record_sha256": self.dataset_record_sha256,
            "manifest_sha256": self.manifest_sha256,
            "source_identity": self.source_identity,
            "license_identity": self.license_identity,
            "causal_cutoff": self.causal_cutoff,
            "available_at": self.available_at,
            "member_sha256": list(self.member_sha256),
            "parent_snapshot_id": self.parent_snapshot_id,
            "parent_dataset_record_sha256": self.parent_dataset_record_sha256,
            "parent_proof_sha256": self.parent_proof_sha256,
        }
        if self.proof_registered_at is not None:
            payload["proof_registered_at"] = self.proof_registered_at
        return payload

    def to_payload(self) -> dict[str, Any]:
        payload = self.proof_payload()
        payload["proof_sha256"] = self.proof_sha256
        return payload


class DatasetSnapshotLineageError(RuntimeError):
    pass


class DatasetSnapshotUnprovenError(DatasetSnapshotLineageError):
    pass


class ConflictingDatasetSnapshotLineageError(DatasetSnapshotLineageError):
    pass


class DatasetSnapshotLineageAuthority:
    """Durable proof that DatasetSnapshot membership advances append-only.

    ScientificRegistry remains the sole owner of DatasetSnapshot identity. This
    authority binds a typed membership manifest to the exact immutable registry
    record and records one non-branching append-only ancestry chain per exact
    source/license identity. Its current state digest is independently fenced by the
    shared MonotonicWorkspaceAuthority, so restoring/deleting only the workspace-local
    lineage file cannot silently erase a later committed tip.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | Path,
        registry: ScientificRegistry,
        *,
        authority_root: str | Path | None = None,
        workspace_instance_id: str | None = None,
    ) -> None:
        if not isinstance(registry, ScientificRegistry):
            raise ValueError("registry must be a ScientificRegistry")
        self.path = Path(path).expanduser().resolve(strict=False)
        self.registry = registry
        self.monotonic_authority = self._make_monotonic_authority(
            self.path,
            authority_root=authority_root,
            workspace_instance_id=workspace_instance_id,
        )
        try:
            self._read_and_verify()
        except FileNotFoundError as exc:
            self._recover_monotonic(None)
            raise ValueError("dataset snapshot lineage authority is missing") from exc

    @staticmethod
    def _make_monotonic_authority(
        path: Path,
        *,
        authority_root: str | Path | None,
        workspace_instance_id: str | None,
    ) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=path.parent.resolve(strict=False),
            workspace_instance_id=workspace_instance_id,
            domain=_MONOTONIC_DOMAIN,
            key=_MONOTONIC_KEY,
            authority_root=authority_root,
        )

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        registry: ScientificRegistry,
        *,
        authority_root: str | Path | None = None,
        workspace_instance_id: str | None = None,
    ) -> "DatasetSnapshotLineageAuthority":
        if not isinstance(registry, ScientificRegistry):
            raise ValueError("registry must be a ScientificRegistry")
        target = Path(path).expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        machine = cls._make_monotonic_authority(
            target,
            authority_root=authority_root,
            workspace_instance_id=workspace_instance_id,
        )
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                machine.recover(observed_state_sha256=None)
                records: tuple[DatasetSnapshotLineageRecord, ...] = ()
                intended = cls._state_sha256(records)
                binding = cls._semantic_binding_sha256(intended)
                machine.prepare(
                    tx_id=cls._new_tx_id("bootstrap"),
                    observed_state_sha256=None,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(target, cls._state_payload(records))
        return cls(
            target,
            registry,
            authority_root=machine.authority_root,
            workspace_instance_id=machine.workspace_instance_id,
        )

    @staticmethod
    def _new_tx_id(label: str) -> str:
        clean = _text(label, "transaction label")
        return f"dataset-lineage-{clean}-{uuid.uuid4().hex}"

    @staticmethod
    def _state_payload(
        records: tuple[DatasetSnapshotLineageRecord, ...],
    ) -> dict[str, Any]:
        return {
            "schema_version": DatasetSnapshotLineageAuthority.SCHEMA_VERSION,
            "records": [record.to_payload() for record in records],
        }

    @staticmethod
    def _state_sha256(
        records: tuple[DatasetSnapshotLineageRecord, ...],
    ) -> str:
        return _digest(DatasetSnapshotLineageAuthority._state_payload(records))

    @staticmethod
    def _semantic_binding_sha256(state_sha256: str) -> str:
        return _digest(
            {
                "kind": _MONOTONIC_BINDING_KIND,
                "schema_version": 1,
                "state_sha256": _sha256(state_sha256, "state_sha256"),
            }
        )

    def _recover_monotonic(
        self,
        records: tuple[DatasetSnapshotLineageRecord, ...] | None,
    ) -> None:
        observed = None if records is None else self._state_sha256(records)
        try:
            self.monotonic_authority.recover(observed_state_sha256=observed)
            return
        except MonotonicAuthorityRecoveryRequiredError:
            if observed is None:
                raise
            history = self.monotonic_authority.read_history()
            if not history:
                raise
            pending = history[-1]
            expected_binding = self._semantic_binding_sha256(observed)
            if (
                pending.intended_state_sha256 != observed
                or pending.semantic_binding_sha256 != expected_binding
            ):
                raise
            self.monotonic_authority.recover(
                observed_state_sha256=observed,
                tx_id=pending.tx_id,
                semantic_binding_sha256=expected_binding,
            )

    @staticmethod
    def _record_from_raw(raw: object) -> DatasetSnapshotLineageRecord:
        if type(raw) is not dict:
            raise ValueError("dataset snapshot lineage record must be an object")
        required = {
            "kind",
            "schema_version",
            "snapshot_id",
            "dataset_record_sha256",
            "manifest_sha256",
            "source_identity",
            "license_identity",
            "causal_cutoff",
            "available_at",
            "member_sha256",
            "parent_snapshot_id",
            "parent_dataset_record_sha256",
            "parent_proof_sha256",
            "proof_sha256",
        }
        schema_version = raw.get("schema_version")
        if raw.get("kind") != _PROOF_KIND:
            raise ValueError("dataset snapshot lineage proof schema mismatch")
        if schema_version == 1:
            if set(raw) != required:
                raise ValueError("dataset snapshot lineage record fields mismatch")
            proof_registered_at = None
        elif schema_version == 2:
            if set(raw) != required | {"proof_registered_at"}:
                raise ValueError("dataset snapshot lineage record fields mismatch")
            proof_registered_at = raw["proof_registered_at"]
        else:
            raise ValueError("dataset snapshot lineage proof schema mismatch")
        raw_members = raw.get("member_sha256")
        if type(raw_members) is not list:
            raise ValueError("member_sha256 must be a list in durable JSON")
        record = DatasetSnapshotLineageRecord(
            snapshot_id=raw["snapshot_id"],
            dataset_record_sha256=raw["dataset_record_sha256"],
            manifest_sha256=raw["manifest_sha256"],
            source_identity=raw["source_identity"],
            license_identity=raw["license_identity"],
            causal_cutoff=raw["causal_cutoff"],
            available_at=raw["available_at"],
            proof_registered_at=proof_registered_at,
            member_sha256=tuple(raw_members),
            parent_snapshot_id=raw["parent_snapshot_id"],
            parent_dataset_record_sha256=raw["parent_dataset_record_sha256"],
            parent_proof_sha256=raw["parent_proof_sha256"],
            proof_sha256=raw["proof_sha256"],
        )
        if _digest(record.proof_payload()) != record.proof_sha256:
            raise ValueError("dataset snapshot lineage proof digest mismatch")
        if membership_manifest_sha256(record.member_sha256) != record.manifest_sha256:
            raise ValueError("dataset snapshot lineage membership manifest mismatch")
        return record

    def _read(self) -> tuple[DatasetSnapshotLineageRecord, ...]:
        raw_text = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw_text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("dataset snapshot lineage authority must be valid UTF-8 JSON") from exc
        if type(state) is not dict or set(state) != {"schema_version", "records"}:
            raise ValueError("dataset snapshot lineage authority fields mismatch")
        if state.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("dataset snapshot lineage authority schema_version mismatch")
        raw_records = state.get("records")
        if type(raw_records) is not list:
            raise ValueError("dataset snapshot lineage records must be a list")
        records = tuple(self._record_from_raw(raw) for raw in raw_records)
        self._validate_graph(records)
        return records

    @staticmethod
    def _validate_graph(
        records: tuple[DatasetSnapshotLineageRecord, ...],
    ) -> None:
        by_id: dict[str, DatasetSnapshotLineageRecord] = {}
        roots: dict[tuple[str, str], str] = {}
        child_by_parent: dict[str, str] = {}
        for record in records:
            if record.snapshot_id in by_id:
                raise ValueError("dataset snapshot lineage contains duplicate snapshot identity")
            by_id[record.snapshot_id] = record
            key = (record.source_identity, record.license_identity)
            if record.parent_snapshot_id is None:
                if key in roots:
                    raise ValueError("dataset snapshot lineage contains competing roots")
                roots[key] = record.snapshot_id
            else:
                if record.parent_snapshot_id in child_by_parent:
                    raise ValueError("dataset snapshot lineage contains an ancestry branch")
                child_by_parent[record.parent_snapshot_id] = record.snapshot_id

        for record in records:
            if record.parent_snapshot_id is None:
                continue
            parent = by_id.get(record.parent_snapshot_id)
            if parent is None:
                raise ValueError("dataset snapshot lineage parent is missing")
            if (
                parent.source_identity != record.source_identity
                or parent.license_identity != record.license_identity
            ):
                raise ValueError("dataset snapshot lineage crosses source/license identity")
            if record.parent_dataset_record_sha256 != parent.dataset_record_sha256:
                raise ValueError("dataset snapshot lineage parent registry digest mismatch")
            if record.parent_proof_sha256 != parent.proof_sha256:
                raise ValueError("dataset snapshot lineage parent proof digest mismatch")
            if record.member_sha256[: len(parent.member_sha256)] != parent.member_sha256:
                raise ValueError("dataset snapshot lineage is not append-only")
            if _instant(record.causal_cutoff, "causal_cutoff") < _instant(
                parent.causal_cutoff, "parent.causal_cutoff"
            ):
                raise ValueError("dataset snapshot causal cutoff moved backwards")
            if _instant(record.available_at, "available_at") < _instant(
                parent.available_at, "parent.available_at"
            ):
                raise ValueError("dataset snapshot availability moved backwards")

        for start in by_id:
            seen: set[str] = set()
            current: str | None = start
            while current is not None:
                if current in seen:
                    raise ValueError("dataset snapshot lineage contains a cycle")
                seen.add(current)
                node = by_id[current]
                current = node.parent_snapshot_id

    def _verify_registry_record(self, record: DatasetSnapshotLineageRecord) -> None:
        entry = self.registry.get("DatasetSnapshot", record.snapshot_id)
        if entry is None:
            raise DatasetSnapshotUnprovenError(
                f"DatasetSnapshot:{record.snapshot_id} is missing from ScientificRegistry"
            )
        self._verify_entry_binding(entry, record)

    @staticmethod
    def _verify_entry_binding(
        entry: RegistryEntry,
        record: DatasetSnapshotLineageRecord,
    ) -> None:
        payload = entry.payload
        expected = {
            "record_sha256": record.dataset_record_sha256,
            "manifest_sha256": record.manifest_sha256,
            "source_identity": record.source_identity,
            "license_identity": record.license_identity,
            "causal_cutoff": record.causal_cutoff,
            "available_at": record.available_at,
        }
        actual = {
            "record_sha256": entry.record_sha256,
            "manifest_sha256": payload.get("manifest_sha256"),
            "source_identity": payload.get("source_identity"),
            "license_identity": payload.get("license_identity"),
            "causal_cutoff": payload.get("causal_cutoff"),
            "available_at": entry.available_at,
        }
        if actual != expected:
            raise DatasetSnapshotUnprovenError(
                f"DatasetSnapshot:{record.snapshot_id} no longer matches lineage proof"
            )

    def _verify_registry_records(
        self,
        records: tuple[DatasetSnapshotLineageRecord, ...],
    ) -> None:
        for record in records:
            self._verify_registry_record(record)

    def _read_and_verify(self) -> tuple[DatasetSnapshotLineageRecord, ...]:
        records = self._read()
        self._recover_monotonic(records)
        self._verify_registry_records(records)
        return records

    @staticmethod
    def _new_record(
        entry: RegistryEntry,
        members: tuple[str, ...],
        parent: DatasetSnapshotLineageRecord | None,
        *,
        proof_registered_at: str | None,
    ) -> DatasetSnapshotLineageRecord:
        payload = entry.payload
        if proof_registered_at is not None:
            registered = _instant(proof_registered_at, "proof_registered_at")
            if registered < _instant(entry.available_at, "DatasetSnapshot available_at"):
                raise DatasetSnapshotUnprovenError(
                    "dataset lineage proof publication predates DatasetSnapshot availability"
                )
            if parent is not None and parent.proof_registered_at is not None:
                if registered < _instant(
                    parent.proof_registered_at, "parent proof_registered_at"
                ):
                    raise DatasetSnapshotUnprovenError(
                        "dataset lineage proof publication moved backwards"
                    )
        base: dict[str, Any] = {
            "kind": _PROOF_KIND,
            "schema_version": 2 if proof_registered_at is not None else 1,
            "snapshot_id": entry.record_id,
            "dataset_record_sha256": entry.record_sha256,
            "manifest_sha256": _sha256(payload.get("manifest_sha256"), "manifest_sha256"),
            "source_identity": _text(payload.get("source_identity"), "source_identity"),
            "license_identity": _text(payload.get("license_identity"), "license_identity"),
            "causal_cutoff": _text(payload.get("causal_cutoff"), "causal_cutoff"),
            "available_at": _text(entry.available_at, "available_at"),
            "member_sha256": list(members),
            "parent_snapshot_id": parent.snapshot_id if parent is not None else None,
            "parent_dataset_record_sha256": (
                parent.dataset_record_sha256 if parent is not None else None
            ),
            "parent_proof_sha256": parent.proof_sha256 if parent is not None else None,
        }
        if proof_registered_at is not None:
            base["proof_registered_at"] = proof_registered_at
        return DatasetSnapshotLineageRecord(
            snapshot_id=base["snapshot_id"],
            dataset_record_sha256=base["dataset_record_sha256"],
            manifest_sha256=base["manifest_sha256"],
            source_identity=base["source_identity"],
            license_identity=base["license_identity"],
            causal_cutoff=base["causal_cutoff"],
            available_at=base["available_at"],
            proof_registered_at=proof_registered_at,
            member_sha256=tuple(base["member_sha256"]),
            parent_snapshot_id=base["parent_snapshot_id"],
            parent_dataset_record_sha256=base["parent_dataset_record_sha256"],
            parent_proof_sha256=base["parent_proof_sha256"],
            proof_sha256=_digest(base),
        )

    def register(
        self,
        *,
        snapshot_id: str,
        member_sha256: tuple[str, ...],
        parent_snapshot_id: str | None = None,
    ) -> DatasetSnapshotLineageRecord:
        wanted_id = _text(snapshot_id, "snapshot_id")
        members = _members(member_sha256)
        if parent_snapshot_id is not None:
            parent_snapshot_id = _text(parent_snapshot_id, "parent_snapshot_id")
            if parent_snapshot_id == wanted_id:
                raise ValueError("dataset snapshot cannot parent itself")

        entry = self.registry.get("DatasetSnapshot", wanted_id)
        if entry is None:
            raise DatasetSnapshotUnprovenError(
                f"DatasetSnapshot:{wanted_id} is missing from ScientificRegistry"
            )
        manifest = membership_manifest_sha256(members)
        if entry.payload.get("manifest_sha256") != manifest:
            raise DatasetSnapshotUnprovenError(
                "DatasetSnapshot manifest is not the canonical typed membership commitment"
            )

        with WorkspaceEconomicLock(self.path.parent):
            records = self._read_and_verify()
            by_id = {record.snapshot_id: record for record in records}
            existing = by_id.get(wanted_id)
            parent = by_id.get(parent_snapshot_id) if parent_snapshot_id is not None else None
            if parent_snapshot_id is not None and parent is None:
                raise DatasetSnapshotUnprovenError("parent DatasetSnapshot is not ancestry-proven")

            if existing is not None:
                candidate = self._new_record(
                    entry,
                    members,
                    parent,
                    proof_registered_at=existing.proof_registered_at,
                )
                if existing == candidate:
                    return existing
                raise ConflictingDatasetSnapshotLineageError(
                    f"conflicting immutable ancestry proof for DatasetSnapshot:{wanted_id}"
                )

            candidate = self._new_record(
                entry,
                members,
                parent,
                proof_registered_at=_authority_now_utc(),
            )
            same_lineage = [
                record
                for record in records
                if record.source_identity == candidate.source_identity
                and record.license_identity == candidate.license_identity
            ]
            if parent is None:
                if same_lineage:
                    raise DatasetSnapshotUnprovenError(
                        "existing source/license lineage requires an exact current parent"
                    )
            else:
                if (
                    parent.source_identity != candidate.source_identity
                    or parent.license_identity != candidate.license_identity
                ):
                    raise DatasetSnapshotUnprovenError(
                        "dataset snapshot parent crosses source/license identity"
                    )
                child_parent_ids = {
                    record.parent_snapshot_id
                    for record in same_lineage
                    if record.parent_snapshot_id is not None
                }
                tips = [
                    record
                    for record in same_lineage
                    if record.snapshot_id not in child_parent_ids
                ]
                if len(tips) != 1 or tips[0].snapshot_id != parent.snapshot_id:
                    raise DatasetSnapshotUnprovenError(
                        "parent must be the exact current tip of the append-only lineage"
                    )
                if candidate.member_sha256[: len(parent.member_sha256)] != parent.member_sha256:
                    raise DatasetSnapshotUnprovenError(
                        "candidate membership does not preserve the parent prefix"
                    )
                if _instant(candidate.causal_cutoff, "causal_cutoff") < _instant(
                    parent.causal_cutoff, "parent.causal_cutoff"
                ):
                    raise DatasetSnapshotUnprovenError("candidate causal cutoff moved backwards")
                if _instant(candidate.available_at, "available_at") < _instant(
                    parent.available_at, "parent.available_at"
                ):
                    raise DatasetSnapshotUnprovenError("candidate availability moved backwards")

            updated = (*records, candidate)
            observed = self._state_sha256(records)
            intended = self._state_sha256(updated)
            binding = self._semantic_binding_sha256(intended)
            tx_id = self._new_tx_id(candidate.snapshot_id)
            self.monotonic_authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            atomic_write_json(self.path, self._state_payload(updated))

            verified = self._read()
            self._verify_registry_records(verified)
            if self._state_sha256(verified) != intended:
                raise DatasetSnapshotLineageError(
                    "published dataset lineage state digest mismatch"
                )
            self.monotonic_authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            final = self._read_and_verify()
            return next(record for record in final if record.snapshot_id == wanted_id)

    def record(self, snapshot_id: str) -> DatasetSnapshotLineageRecord | None:
        wanted = _text(snapshot_id, "snapshot_id")
        for record in self._read_and_verify():
            if record.snapshot_id == wanted:
                return record
        return None

    def proves_descendant(
        self,
        *,
        descendant_snapshot_id: str,
        ancestor_snapshot_id: str,
    ) -> bool:
        descendant = _text(descendant_snapshot_id, "descendant_snapshot_id")
        ancestor = _text(ancestor_snapshot_id, "ancestor_snapshot_id")
        records = self._read_and_verify()
        by_id = {record.snapshot_id: record for record in records}
        current = by_id.get(descendant)
        if current is None or ancestor not in by_id:
            return False
        while True:
            if current.snapshot_id == ancestor:
                return True
            if current.parent_snapshot_id is None:
                return False
            current = by_id[current.parent_snapshot_id]

    def require_descendant(
        self,
        *,
        descendant_snapshot_id: str,
        ancestor_snapshot_id: str,
    ) -> DatasetSnapshotLineageRecord:
        if not self.proves_descendant(
            descendant_snapshot_id=descendant_snapshot_id,
            ancestor_snapshot_id=ancestor_snapshot_id,
        ):
            raise DatasetSnapshotUnprovenError(
                f"DatasetSnapshot:{descendant_snapshot_id} is not a proven append-only "
                f"descendant of DatasetSnapshot:{ancestor_snapshot_id}"
            )
        record = self.record(descendant_snapshot_id)
        assert record is not None
        return record


    def require_descendant_as_of(
        self,
        *,
        descendant_snapshot_id: str,
        ancestor_snapshot_id: str,
        as_of: str,
    ) -> DatasetSnapshotLineageRecord:
        """Require exact ancestry whose full proof chain existed by one causal instant."""

        descendant = _text(descendant_snapshot_id, "descendant_snapshot_id")
        ancestor = _text(ancestor_snapshot_id, "ancestor_snapshot_id")
        boundary = _instant(as_of, "as_of")
        records = self._read_and_verify()
        by_id = {record.snapshot_id: record for record in records}
        current = by_id.get(descendant)
        if current is None or ancestor not in by_id:
            raise DatasetSnapshotUnprovenError(
                f"DatasetSnapshot:{descendant} is not a proven append-only "
                f"descendant of DatasetSnapshot:{ancestor}"
            )
        resolved = current
        while True:
            if current.proof_registered_at is None:
                raise DatasetSnapshotUnprovenError(
                    "dataset lineage proof lacks authority-owned publication evidence"
                )
            if _instant(current.proof_registered_at, "proof_registered_at") > boundary:
                raise DatasetSnapshotUnprovenError(
                    "dataset lineage proof was not causally available at the requested instant"
                )
            if current.snapshot_id == ancestor:
                return resolved
            if current.parent_snapshot_id is None:
                raise DatasetSnapshotUnprovenError(
                    f"DatasetSnapshot:{descendant} is not a proven append-only "
                    f"descendant of DatasetSnapshot:{ancestor}"
                )
            current = by_id[current.parent_snapshot_id]
