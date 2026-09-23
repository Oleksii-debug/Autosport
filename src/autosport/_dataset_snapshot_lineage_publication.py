"""Causal publication witness for immutable legacy DatasetSnapshot lineage proofs.

The first DatasetSnapshot lineage schema predates authority-owned publication time.
Those v1 proof bytes are still cryptographically useful ancestry evidence, but they
must never be treated as if they were causally available before Autosport actually
re-observed them.  Rewriting a v1 proof to v2 would change ``proof_sha256`` and every
child's ``parent_proof_sha256`` (and can invalidate historical ActivationBindings).

This compatibility layer therefore keeps the original proof identity byte-for-byte
and appends a separate authority-owned *re-observation* witness.  The witness time is
created only by this module at the instant an exact, registry-verified lineage is
successfully registered/re-registered.  Causal reads accept a v1 proof only at or
after that witness time.  A separate MonotonicWorkspaceAuthority fences witness
rollback/deletion while the machine-state authority survives.

The module patches the existing DatasetSnapshotLineageAuthority methods at package
startup.  This mirrors Autosport's other narrowly-scoped compatibility guards while
preserving DatasetSnapshotLineageAuthority as the sole ancestry truth owner.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from .dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotLineageRecord,
    DatasetSnapshotUnprovenError,
)
from .integrity import atomic_write_json
from .monotonic_workspace_authority import (
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicWorkspaceAuthority,
)
from .workspace_lock import WorkspaceEconomicLock


_WITNESS_KIND: Final = "autosport-dataset-lineage-publication-witness-v1"
_STATE_SCHEMA_VERSION: Final = 1
_MONOTONIC_DOMAIN: Final = "dataset-snapshot-lineage-publication"
_MONOTONIC_KEY: Final = "legacy-proof-reobservation-v1"
_MONOTONIC_BINDING_KIND: Final = "autosport-dataset-lineage-publication-state-v1"
_HEX: Final = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{name} must be canonical SHA-256 hex")
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
            raise ValueError(f"duplicate lineage publication witness key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite lineage publication witness value: {value}")


@dataclass(frozen=True, slots=True)
class LegacyLineagePublicationWitness:
    snapshot_id: str
    proof_sha256: str
    dataset_record_sha256: str
    registered_at: str

    def __post_init__(self) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _sha(self.proof_sha256, "proof_sha256")
        _sha(self.dataset_record_sha256, "dataset_record_sha256")
        _instant(self.registered_at, "registered_at")

    def core_payload(self) -> dict[str, object]:
        return {
            "kind": _WITNESS_KIND,
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "proof_sha256": self.proof_sha256,
            "dataset_record_sha256": self.dataset_record_sha256,
            "registered_at": self.registered_at,
        }

    @property
    def witness_sha256(self) -> str:
        return _digest(self.core_payload())

    def to_payload(self) -> dict[str, object]:
        return {**self.core_payload(), "witness_sha256": self.witness_sha256}


class LegacyLineagePublicationAuthority:
    """Append-only causal re-observation authority for schema-v1 lineage proofs."""

    def __init__(self, lineage: DatasetSnapshotLineageAuthority) -> None:
        if not isinstance(lineage, DatasetSnapshotLineageAuthority):
            raise TypeError("lineage must be DatasetSnapshotLineageAuthority")
        self.lineage = lineage
        self.path = lineage.path.with_name(
            f"{lineage.path.name}.publication-witnesses.json"
        )
        self.monotonic_authority = MonotonicWorkspaceAuthority(
            workspace=self.path.parent.resolve(strict=False),
            workspace_instance_id=lineage.monotonic_authority.workspace_instance_id,
            domain=_MONOTONIC_DOMAIN,
            key=_MONOTONIC_KEY,
            authority_root=lineage.monotonic_authority.authority_root,
        )
        try:
            self._read_and_recover()
        except FileNotFoundError:
            # This call is intentionally made before re-raising.  If a witness file
            # existed and was deleted, machine-state history turns this into a
            # rollback error rather than allowing silent re-bootstrap.
            self.monotonic_authority.recover(observed_state_sha256=None)
            raise

    @classmethod
    def initialize_pristine(
        cls, lineage: DatasetSnapshotLineageAuthority
    ) -> "LegacyLineagePublicationAuthority":
        try:
            return cls(lineage)
        except FileNotFoundError:
            pass

        target = lineage.path.with_name(
            f"{lineage.path.name}.publication-witnesses.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        machine = MonotonicWorkspaceAuthority(
            workspace=target.parent.resolve(strict=False),
            workspace_instance_id=lineage.monotonic_authority.workspace_instance_id,
            domain=_MONOTONIC_DOMAIN,
            key=_MONOTONIC_KEY,
            authority_root=lineage.monotonic_authority.authority_root,
        )
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                machine.recover(observed_state_sha256=None)
                records: tuple[LegacyLineagePublicationWitness, ...] = ()
                intended = cls._state_sha256(records)
                binding = cls._semantic_binding_sha256(intended)
                tx_id = f"dataset-lineage-publication-bootstrap-{uuid.uuid4().hex}"
                machine.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(target, cls._state_payload(records))
                machine.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
        return cls(lineage)

    @staticmethod
    def _state_payload(
        records: tuple[LegacyLineagePublicationWitness, ...],
    ) -> dict[str, object]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "records": [record.to_payload() for record in records],
        }

    @classmethod
    def _state_sha256(
        cls, records: tuple[LegacyLineagePublicationWitness, ...]
    ) -> str:
        return _digest(cls._state_payload(records))

    @staticmethod
    def _semantic_binding_sha256(state_sha256: str) -> str:
        return _digest(
            {
                "kind": _MONOTONIC_BINDING_KIND,
                "schema_version": 1,
                "state_sha256": _sha(state_sha256, "state_sha256"),
            }
        )

    @staticmethod
    def _from_raw(raw: object) -> LegacyLineagePublicationWitness:
        expected = {
            "kind",
            "schema_version",
            "snapshot_id",
            "proof_sha256",
            "dataset_record_sha256",
            "registered_at",
            "witness_sha256",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("lineage publication witness fields mismatch")
        if raw["kind"] != _WITNESS_KIND or raw["schema_version"] != 1:
            raise ValueError("lineage publication witness schema mismatch")
        witness = LegacyLineagePublicationWitness(
            snapshot_id=raw["snapshot_id"],
            proof_sha256=raw["proof_sha256"],
            dataset_record_sha256=raw["dataset_record_sha256"],
            registered_at=raw["registered_at"],
        )
        if _sha(raw["witness_sha256"], "witness_sha256") != witness.witness_sha256:
            raise ValueError("lineage publication witness digest mismatch")
        return witness

    def _read(self) -> tuple[LegacyLineagePublicationWitness, ...]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("lineage publication authority must be valid JSON") from exc
        if type(state) is not dict or set(state) != {"schema_version", "records"}:
            raise ValueError("lineage publication authority fields mismatch")
        if state["schema_version"] != _STATE_SCHEMA_VERSION:
            raise ValueError("lineage publication authority schema mismatch")
        raw_records = state["records"]
        if type(raw_records) is not list:
            raise ValueError("lineage publication authority records must be a list")
        records = tuple(self._from_raw(item) for item in raw_records)
        identities: set[tuple[str, str]] = set()
        for record in records:
            key = (record.snapshot_id, record.proof_sha256)
            if key in identities:
                raise ValueError("duplicate lineage publication witness")
            identities.add(key)
        return records

    def _recover(
        self, records: tuple[LegacyLineagePublicationWitness, ...]
    ) -> None:
        observed = self._state_sha256(records)
        try:
            self.monotonic_authority.recover(observed_state_sha256=observed)
            return
        except MonotonicAuthorityRecoveryRequiredError:
            history = self.monotonic_authority.read_history()
            if not history:
                raise
            pending = history[-1]
            binding = self._semantic_binding_sha256(observed)
            if (
                pending.intended_state_sha256 != observed
                or pending.semantic_binding_sha256 != binding
            ):
                raise
            self.monotonic_authority.recover(
                observed_state_sha256=observed,
                tx_id=pending.tx_id,
                semantic_binding_sha256=binding,
            )

    def _read_and_recover(self) -> tuple[LegacyLineagePublicationWitness, ...]:
        records = self._read()
        self._recover(records)
        return records

    def publish_exact_chain(
        self, records: tuple[DatasetSnapshotLineageRecord, ...]
    ) -> tuple[LegacyLineagePublicationWitness, ...]:
        """Publish one authority-owned NOW witness for missing exact v1 proofs."""

        legacy = tuple(record for record in records if record.proof_registered_at is None)
        if not legacy:
            return ()
        for record in legacy:
            if not isinstance(record, DatasetSnapshotLineageRecord):
                raise TypeError("records must contain DatasetSnapshotLineageRecord")

        with WorkspaceEconomicLock(self.path.parent):
            existing = self._read_and_recover()
            by_identity = {
                (witness.snapshot_id, witness.proof_sha256): witness
                for witness in existing
            }
            resolved: list[LegacyLineagePublicationWitness] = []
            missing: list[DatasetSnapshotLineageRecord] = []
            for record in legacy:
                key = (record.snapshot_id, record.proof_sha256)
                witness = by_identity.get(key)
                if witness is None:
                    missing.append(record)
                    continue
                if witness.dataset_record_sha256 != record.dataset_record_sha256:
                    raise ValueError("legacy lineage publication witness binding mismatch")
                resolved.append(witness)

            if not missing:
                return tuple(resolved)

            registered_at = _authority_now_utc()
            registered = _instant(registered_at, "registered_at")
            additions: list[LegacyLineagePublicationWitness] = []
            for record in missing:
                if registered < _instant(record.available_at, "DatasetSnapshot available_at"):
                    raise DatasetSnapshotUnprovenError(
                        "lineage re-observation predates DatasetSnapshot availability"
                    )
                additions.append(
                    LegacyLineagePublicationWitness(
                        snapshot_id=record.snapshot_id,
                        proof_sha256=record.proof_sha256,
                        dataset_record_sha256=record.dataset_record_sha256,
                        registered_at=registered_at,
                    )
                )
            updated = (*existing, *additions)
            observed = self._state_sha256(existing)
            intended = self._state_sha256(updated)
            binding = self._semantic_binding_sha256(intended)
            tx_id = f"dataset-lineage-publication-{uuid.uuid4().hex}"
            self.monotonic_authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            atomic_write_json(self.path, self._state_payload(updated))
            verified = self._read()
            if self._state_sha256(verified) != intended:
                raise RuntimeError("published lineage witness state digest mismatch")
            self.monotonic_authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._read_and_recover()
            return tuple([*resolved, *additions])

    def witness_for(
        self, record: DatasetSnapshotLineageRecord
    ) -> LegacyLineagePublicationWitness | None:
        if not isinstance(record, DatasetSnapshotLineageRecord):
            raise TypeError("record must be DatasetSnapshotLineageRecord")
        for witness in self._read_and_recover():
            if (
                witness.snapshot_id == record.snapshot_id
                and witness.proof_sha256 == record.proof_sha256
            ):
                if witness.dataset_record_sha256 != record.dataset_record_sha256:
                    raise ValueError("legacy lineage publication witness binding mismatch")
                return witness
        return None


def _legacy_chain_for(
    authority: DatasetSnapshotLineageAuthority,
    record: DatasetSnapshotLineageRecord,
) -> tuple[DatasetSnapshotLineageRecord, ...]:
    records = authority._read_and_verify()
    by_id = {item.snapshot_id: item for item in records}
    current = by_id.get(record.snapshot_id)
    if current is None or current.proof_sha256 != record.proof_sha256:
        raise DatasetSnapshotUnprovenError("registered lineage proof disappeared")
    chain: list[DatasetSnapshotLineageRecord] = []
    while True:
        if current.proof_registered_at is None:
            chain.append(current)
        if current.parent_snapshot_id is None:
            return tuple(chain)
        current = by_id[current.parent_snapshot_id]


_ORIGINAL_REGISTER = DatasetSnapshotLineageAuthority.register


def _register_with_legacy_publication(
    self: DatasetSnapshotLineageAuthority,
    *,
    snapshot_id: str,
    member_sha256: tuple[str, ...],
    parent_snapshot_id: str | None = None,
) -> DatasetSnapshotLineageRecord:
    result = _ORIGINAL_REGISTER(
        self,
        snapshot_id=snapshot_id,
        member_sha256=member_sha256,
        parent_snapshot_id=parent_snapshot_id,
    )
    legacy_chain = _legacy_chain_for(self, result)
    if legacy_chain:
        LegacyLineagePublicationAuthority.initialize_pristine(
            self
        ).publish_exact_chain(legacy_chain)
    return result


def _require_descendant_as_of_with_legacy_publication(
    self: DatasetSnapshotLineageAuthority,
    *,
    descendant_snapshot_id: str,
    ancestor_snapshot_id: str,
    as_of: str,
) -> DatasetSnapshotLineageRecord:
    """Require ancestry that was directly published or re-observed by ``as_of``."""

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
    publication: LegacyLineagePublicationAuthority | None = None
    while True:
        registered_at = current.proof_registered_at
        if registered_at is None:
            if publication is None:
                try:
                    publication = LegacyLineagePublicationAuthority(self)
                except FileNotFoundError as exc:
                    raise DatasetSnapshotUnprovenError(
                        "dataset lineage proof lacks authority-owned publication evidence"
                    ) from exc
            witness = publication.witness_for(current)
            if witness is None:
                raise DatasetSnapshotUnprovenError(
                    "dataset lineage proof lacks authority-owned publication evidence"
                )
            registered_at = witness.registered_at
        if _instant(registered_at, "proof publication time") > boundary:
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


# Idempotent patch installation.  Importing/reloading package guards must not stack
# wrappers around the same method.
if not getattr(DatasetSnapshotLineageAuthority.register, "_legacy_publication_guard", False):
    setattr(_register_with_legacy_publication, "_legacy_publication_guard", True)
    DatasetSnapshotLineageAuthority.register = _register_with_legacy_publication

if not getattr(
    DatasetSnapshotLineageAuthority.require_descendant_as_of,
    "_legacy_publication_guard",
    False,
):
    setattr(
        _require_descendant_as_of_with_legacy_publication,
        "_legacy_publication_guard",
        True,
    )
    DatasetSnapshotLineageAuthority.require_descendant_as_of = (
        _require_descendant_as_of_with_legacy_publication
    )


__all__ = [
    "LegacyLineagePublicationAuthority",
    "LegacyLineagePublicationWitness",
]
