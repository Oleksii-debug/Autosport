from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .integrity import atomic_write_json
from .scientific_registry import RegistryEntry, ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
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


def _members(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("member_sha256s must be an iterable of digests")
    members = tuple(_sha256(value, "member_sha256") for value in values)
    if not members:
        raise ValueError("member_sha256s must not be empty")
    if len(members) != len(set(members)):
        raise ValueError("member_sha256s must contain unique immutable members")
    return members


def membership_sha256(member_sha256s: Iterable[str]) -> str:
    """Return the versioned commitment for one exact ordered dataset membership."""

    members = _members(member_sha256s)
    return _digest({"schema_version": 1, "member_sha256s": list(members)})


class DatasetSnapshotLineageError(RuntimeError):
    """Raised when immutable snapshot ancestry cannot be proven safely."""


class ConflictingDatasetSnapshotLineageError(DatasetSnapshotLineageError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetSnapshotAncestryProof:
    ancestor_snapshot_id: str
    descendant_snapshot_id: str
    chain: tuple[str, ...]
    ancestor_snapshot_record_sha256: str
    descendant_snapshot_record_sha256: str
    ancestor_membership_sha256: str
    descendant_membership_sha256: str

    def __post_init__(self) -> None:
        _text(self.ancestor_snapshot_id, "ancestor_snapshot_id")
        _text(self.descendant_snapshot_id, "descendant_snapshot_id")
        if len(self.chain) < 2:
            raise ValueError("ancestry proof chain must contain distinct ancestor and descendant")
        if self.chain[0] != self.ancestor_snapshot_id:
            raise ValueError("ancestry proof chain does not start at ancestor")
        if self.chain[-1] != self.descendant_snapshot_id:
            raise ValueError("ancestry proof chain does not end at descendant")
        for value in self.chain:
            _text(value, "chain snapshot id")
        _sha256(self.ancestor_snapshot_record_sha256, "ancestor_snapshot_record_sha256")
        _sha256(self.descendant_snapshot_record_sha256, "descendant_snapshot_record_sha256")
        _sha256(self.ancestor_membership_sha256, "ancestor_membership_sha256")
        _sha256(self.descendant_membership_sha256, "descendant_membership_sha256")


@dataclass(frozen=True, slots=True)
class DatasetSnapshotLineageRecord:
    snapshot_id: str
    snapshot_record_sha256: str
    manifest_sha256: str
    source_identity: str
    license_identity: str
    causal_cutoff: str
    available_at: str
    member_sha256s: tuple[str, ...]
    membership_sha256: str
    parent_snapshot_id: str | None
    parent_snapshot_record_sha256: str | None
    record_sha256: str


class DatasetSnapshotLineageAuthority:
    """Durable proof that registered DatasetSnapshots form append-only chains.

    ScientificRegistry remains the canonical owner of DatasetSnapshot identity. This
    authority adds only the evidence missing from that record: exact immutable member
    commitments and parent links. A child is valid only when the complete parent member
    sequence is an exact prefix of the child sequence. Legacy DatasetSnapshots without a
    record here remain deliberately unproven.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, registry: ScientificRegistry) -> None:
        if not isinstance(registry, ScientificRegistry):
            raise ValueError("registry must be a ScientificRegistry")
        self.path = Path(path)
        self.registry = registry
        try:
            self._read()
        except FileNotFoundError as exc:
            raise ValueError("dataset snapshot lineage authority is missing") from exc

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        *,
        registry: ScientificRegistry,
    ) -> "DatasetSnapshotLineageAuthority":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                records: list[dict[str, Any]] = []
                atomic_write_json(
                    target,
                    {
                        "schema_version": cls.SCHEMA_VERSION,
                        "records": records,
                        "state_sha256": cls._state_digest(records),
                    },
                )
        return cls(target, registry=registry)

    @staticmethod
    def _state_digest(records: list[dict[str, Any]]) -> str:
        return _digest({"schema_version": 1, "records": records})

    @staticmethod
    def _record_digest(record: Mapping[str, Any]) -> str:
        payload = dict(record)
        payload.pop("record_sha256", None)
        return _digest(payload)

    def _snapshot_entry(self, snapshot_id: str) -> RegistryEntry:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        entry = self.registry.get("DatasetSnapshot", snapshot_id)
        if entry is None:
            raise DatasetSnapshotLineageError(
                f"DatasetSnapshot is missing from ScientificRegistry: {snapshot_id}"
            )
        payload = entry.payload
        if not isinstance(payload, Mapping):
            raise DatasetSnapshotLineageError("DatasetSnapshot payload is invalid")
        for field in ("manifest_sha256", "source_identity", "license_identity", "causal_cutoff"):
            if field not in payload:
                raise DatasetSnapshotLineageError(
                    f"DatasetSnapshot payload lacks required lineage field: {field}"
                )
        _sha256(entry.record_sha256, "DatasetSnapshot.record_sha256")
        _sha256(payload["manifest_sha256"], "DatasetSnapshot.manifest_sha256")
        _text(payload["source_identity"], "DatasetSnapshot.source_identity")
        _text(payload["license_identity"], "DatasetSnapshot.license_identity")
        _instant(payload["causal_cutoff"], "DatasetSnapshot.causal_cutoff")
        _instant(entry.available_at, "DatasetSnapshot.available_at")
        return entry

    def _read(self) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("dataset snapshot lineage authority must be valid UTF-8 JSON") from exc
        if type(state) is not dict:
            raise ValueError("dataset snapshot lineage authority must be a JSON object")
        if set(state) != {"schema_version", "records", "state_sha256"}:
            raise ValueError("dataset snapshot lineage authority fields mismatch")
        if state["schema_version"] != self.SCHEMA_VERSION:
            raise ValueError("dataset snapshot lineage authority schema_version mismatch")
        records = state["records"]
        if type(records) is not list:
            raise ValueError("dataset snapshot lineage records must be a list")
        if _sha256(state["state_sha256"], "state_sha256") != self._state_digest(records):
            raise ValueError("dataset snapshot lineage state digest mismatch")
        self._validate_records(records)
        return state

    def _validate_records(self, records: list[dict[str, Any]]) -> None:
        by_id: dict[str, dict[str, Any]] = {}
        required = {
            "snapshot_id",
            "snapshot_record_sha256",
            "manifest_sha256",
            "source_identity",
            "license_identity",
            "causal_cutoff",
            "available_at",
            "member_sha256s",
            "membership_sha256",
            "parent_snapshot_id",
            "parent_snapshot_record_sha256",
            "record_sha256",
        }
        for raw_record in records:
            if type(raw_record) is not dict or set(raw_record) != required:
                raise ValueError("dataset snapshot lineage record fields mismatch")
            snapshot_id = _text(raw_record["snapshot_id"], "snapshot_id")
            if snapshot_id in by_id:
                raise ValueError("dataset snapshot lineage contains duplicate snapshot identity")
            snapshot_record_sha256 = _sha256(
                raw_record["snapshot_record_sha256"], "snapshot_record_sha256"
            )
            manifest_sha256 = _sha256(raw_record["manifest_sha256"], "manifest_sha256")
            source_identity = _text(raw_record["source_identity"], "source_identity")
            license_identity = _text(raw_record["license_identity"], "license_identity")
            causal_cutoff = _instant(raw_record["causal_cutoff"], "causal_cutoff")
            available_at = _instant(raw_record["available_at"], "available_at")
            raw_members = raw_record["member_sha256s"]
            if type(raw_members) is not list:
                raise ValueError("member_sha256s must be a list")
            members = _members(raw_members)
            if _sha256(raw_record["membership_sha256"], "membership_sha256") != membership_sha256(members):
                raise ValueError("dataset snapshot membership digest mismatch")
            if _sha256(raw_record["record_sha256"], "record_sha256") != self._record_digest(raw_record):
                raise ValueError("dataset snapshot lineage record digest mismatch")

            entry = self._snapshot_entry(snapshot_id)
            payload = entry.payload
            if entry.record_sha256 != snapshot_record_sha256:
                raise DatasetSnapshotLineageError(
                    "dataset snapshot lineage record no longer binds the exact registry record"
                )
            if payload["manifest_sha256"].lower() != manifest_sha256:
                raise DatasetSnapshotLineageError("dataset snapshot manifest binding mismatch")
            if payload["source_identity"] != source_identity:
                raise DatasetSnapshotLineageError("dataset snapshot source identity binding mismatch")
            if payload["license_identity"] != license_identity:
                raise DatasetSnapshotLineageError("dataset snapshot license identity binding mismatch")
            if _instant(payload["causal_cutoff"], "registry causal_cutoff") != causal_cutoff:
                raise DatasetSnapshotLineageError("dataset snapshot causal cutoff binding mismatch")
            if _instant(entry.available_at, "registry available_at") != available_at:
                raise DatasetSnapshotLineageError("dataset snapshot availability binding mismatch")

            parent_id = raw_record["parent_snapshot_id"]
            parent_record_sha = raw_record["parent_snapshot_record_sha256"]
            if parent_id is None:
                if parent_record_sha is not None:
                    raise ValueError("root lineage record cannot carry parent record digest")
            else:
                parent_id = _text(parent_id, "parent_snapshot_id")
                if parent_id == snapshot_id:
                    raise ValueError("dataset snapshot cannot parent itself")
                if parent_record_sha is None:
                    raise ValueError("child lineage record requires parent record digest")
                parent_record_sha = _sha256(
                    parent_record_sha, "parent_snapshot_record_sha256"
                )
                parent = by_id.get(parent_id)
                if parent is None:
                    raise ValueError("dataset snapshot parent must precede child in authority")
                if parent["snapshot_record_sha256"] != parent_record_sha:
                    raise DatasetSnapshotLineageError("dataset snapshot parent record digest mismatch")
                if parent["source_identity"] != source_identity:
                    raise DatasetSnapshotLineageError("dataset snapshot source identity changed across lineage")
                if parent["license_identity"] != license_identity:
                    raise DatasetSnapshotLineageError("dataset snapshot license identity changed across lineage")
                if _instant(parent["causal_cutoff"], "parent causal_cutoff") > causal_cutoff:
                    raise DatasetSnapshotLineageError("dataset snapshot causal cutoff moved backwards")
                if _instant(parent["available_at"], "parent available_at") > available_at:
                    raise DatasetSnapshotLineageError("dataset snapshot availability moved backwards")
                parent_members = tuple(parent["member_sha256s"])
                if members[: len(parent_members)] != parent_members:
                    raise DatasetSnapshotLineageError(
                        "dataset snapshot does not preserve complete parent membership prefix"
                    )
                if len(members) == len(parent_members) and manifest_sha256 != parent["manifest_sha256"]:
                    raise DatasetSnapshotLineageError(
                        "dataset snapshot changed manifest without appending immutable members"
                    )
            by_id[snapshot_id] = raw_record

    @staticmethod
    def _to_record(raw: Mapping[str, Any]) -> DatasetSnapshotLineageRecord:
        return DatasetSnapshotLineageRecord(
            snapshot_id=raw["snapshot_id"],
            snapshot_record_sha256=raw["snapshot_record_sha256"],
            manifest_sha256=raw["manifest_sha256"],
            source_identity=raw["source_identity"],
            license_identity=raw["license_identity"],
            causal_cutoff=raw["causal_cutoff"],
            available_at=raw["available_at"],
            member_sha256s=tuple(raw["member_sha256s"]),
            membership_sha256=raw["membership_sha256"],
            parent_snapshot_id=raw["parent_snapshot_id"],
            parent_snapshot_record_sha256=raw["parent_snapshot_record_sha256"],
            record_sha256=raw["record_sha256"],
        )

    def get(self, snapshot_id: str) -> DatasetSnapshotLineageRecord | None:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        state = self._read()
        for raw_record in state["records"]:
            if raw_record["snapshot_id"] == snapshot_id:
                return self._to_record(raw_record)
        return None

    def register(
        self,
        *,
        snapshot_id: str,
        member_sha256s: Iterable[str],
        parent_snapshot_id: str | None = None,
    ) -> str:
        snapshot_id = _text(snapshot_id, "snapshot_id")
        members = _members(member_sha256s)
        if parent_snapshot_id is not None:
            parent_snapshot_id = _text(parent_snapshot_id, "parent_snapshot_id")
            if parent_snapshot_id == snapshot_id:
                raise DatasetSnapshotLineageError("dataset snapshot cannot parent itself")

        entry = self._snapshot_entry(snapshot_id)
        payload = entry.payload
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            by_id = {record["snapshot_id"]: record for record in state["records"]}
            parent: dict[str, Any] | None = None
            if parent_snapshot_id is not None:
                parent = by_id.get(parent_snapshot_id)
                if parent is None:
                    raise DatasetSnapshotLineageError(
                        "parent snapshot lacks prior durable lineage authority"
                    )

            record: dict[str, Any] = {
                "snapshot_id": snapshot_id,
                "snapshot_record_sha256": entry.record_sha256,
                "manifest_sha256": payload["manifest_sha256"].lower(),
                "source_identity": payload["source_identity"],
                "license_identity": payload["license_identity"],
                "causal_cutoff": payload["causal_cutoff"],
                "available_at": entry.available_at,
                "member_sha256s": list(members),
                "membership_sha256": membership_sha256(members),
                "parent_snapshot_id": parent_snapshot_id,
                "parent_snapshot_record_sha256": (
                    parent["snapshot_record_sha256"] if parent is not None else None
                ),
            }
            record["record_sha256"] = self._record_digest(record)

            existing = by_id.get(snapshot_id)
            if existing is not None:
                if existing["record_sha256"] == record["record_sha256"]:
                    return record["record_sha256"]
                raise ConflictingDatasetSnapshotLineageError(
                    f"conflicting immutable dataset snapshot lineage: {snapshot_id}"
                )

            candidate_records = [*state["records"], record]
            self._validate_records(candidate_records)
            atomic_write_json(
                self.path,
                {
                    "schema_version": self.SCHEMA_VERSION,
                    "records": candidate_records,
                    "state_sha256": self._state_digest(candidate_records),
                },
            )
            self._read()
            return record["record_sha256"]

    def prove_descendant(
        self,
        *,
        ancestor_snapshot_id: str,
        descendant_snapshot_id: str,
    ) -> DatasetSnapshotAncestryProof:
        ancestor_snapshot_id = _text(ancestor_snapshot_id, "ancestor_snapshot_id")
        descendant_snapshot_id = _text(descendant_snapshot_id, "descendant_snapshot_id")
        if ancestor_snapshot_id == descendant_snapshot_id:
            raise DatasetSnapshotLineageError("ancestry proof requires distinct snapshots")
        state = self._read()
        by_id = {record["snapshot_id"]: record for record in state["records"]}
        ancestor = by_id.get(ancestor_snapshot_id)
        descendant = by_id.get(descendant_snapshot_id)
        if ancestor is None or descendant is None:
            raise DatasetSnapshotLineageError("both snapshots require durable lineage authority")

        reverse_chain = [descendant_snapshot_id]
        current = descendant
        while current["snapshot_id"] != ancestor_snapshot_id:
            parent_id = current["parent_snapshot_id"]
            if parent_id is None:
                raise DatasetSnapshotLineageError(
                    f"{descendant_snapshot_id} is not a descendant of {ancestor_snapshot_id}"
                )
            parent = by_id.get(parent_id)
            if parent is None:
                raise DatasetSnapshotLineageError("dataset snapshot lineage chain is incomplete")
            reverse_chain.append(parent_id)
            current = parent
        chain = tuple(reversed(reverse_chain))
        return DatasetSnapshotAncestryProof(
            ancestor_snapshot_id=ancestor_snapshot_id,
            descendant_snapshot_id=descendant_snapshot_id,
            chain=chain,
            ancestor_snapshot_record_sha256=ancestor["snapshot_record_sha256"],
            descendant_snapshot_record_sha256=descendant["snapshot_record_sha256"],
            ancestor_membership_sha256=ancestor["membership_sha256"],
            descendant_membership_sha256=descendant["membership_sha256"],
        )
