"""Non-caller-reproducible issuance proof for legacy lineage publication witnesses.

``_dataset_snapshot_lineage_publication`` keeps immutable schema-v1 lineage proof
identities and uses the shared monotonic workspace authority to fence rollback.  The
monotonic primitive intentionally accepts opaque caller-provided state digests, so it
cannot by itself prove *who* issued a legacy re-observation timestamp.

This guard keeps that existing authority family and adds one machine-local issuance
credential.  Every causal legacy publication must have an HMAC-authenticated issuance
record bound to the exact workspace instance, monotonic namespace, proof and witness.
Raw witness bytes plus a caller-created matching generic monotonic journal therefore
cannot mint causal publication time.  Missing/deleted/truncated issuance evidence only
fails closed; it can never move publication time earlier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from . import _dataset_snapshot_lineage_publication as publication
from .dataset_snapshot_lineage import DatasetSnapshotLineageRecord
from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


_ISSUANCE_SCHEMA: Final = "autosport.dataset-lineage-publication-issuance"
_ISSUANCE_SCHEMA_VERSION: Final = 1
_ISSUANCE_RECORD_KIND: Final = "autosport-dataset-lineage-publication-issuance-v1"
_KEY_BYTES: Final = 32
_HEX: Final = frozenset("0123456789abcdef")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(ch not in _HEX for ch in value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _workspace_key(authority: publication.LegacyLineagePublicationAuthority) -> str:
    machine = authority.monotonic_authority
    material = "\0".join(
        (
            machine.workspace_instance_id,
            machine.namespace_sha256,
            _ISSUANCE_RECORD_KIND,
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _key_path(authority: publication.LegacyLineagePublicationAuthority) -> Path:
    key = _workspace_key(authority)
    return (
        authority.monotonic_authority.authority_root
        / "issuer-credentials"
        / "dataset-lineage-publication-v1"
        / key[:2]
        / f"{key}.key"
    )


def _issuance_path(authority: publication.LegacyLineagePublicationAuthority) -> Path:
    key = _workspace_key(authority)
    return (
        authority.monotonic_authority.authority_root
        / "issuer-records"
        / "dataset-lineage-publication-v1"
        / key[:2]
        / f"{key}.json"
    )


def _secure_existing_key(path: Path) -> bytes:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("legacy publication issuer credential is unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("legacy publication issuer credential must be a regular file")
    if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("legacy publication issuer credential permissions are too broad")
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise ValueError("legacy publication issuer credential is unreadable") from exc
    if len(value) != _KEY_BYTES:
        raise ValueError("legacy publication issuer credential has invalid length")
    return value


def _load_or_create_key(authority: publication.LegacyLineagePublicationAuthority) -> bytes:
    path = _key_path(authority)
    if path.exists():
        return _secure_existing_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(_KEY_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if descriptor is not None:
            os.close(descriptor)
        return _secure_existing_key(path)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return _secure_existing_key(path)


def _load_key(authority: publication.LegacyLineagePublicationAuthority) -> bytes | None:
    path = _key_path(authority)
    if not path.exists():
        return None
    return _secure_existing_key(path)


@dataclass(frozen=True, slots=True)
class _IssuanceRecord:
    workspace_instance_id: str
    authority_namespace_sha256: str
    snapshot_id: str
    proof_sha256: str
    dataset_record_sha256: str
    witness_sha256: str
    published_at: str
    issuer_mac_sha256: str

    def core_payload(self) -> dict[str, object]:
        return {
            "kind": _ISSUANCE_RECORD_KIND,
            "schema_version": _ISSUANCE_SCHEMA_VERSION,
            "workspace_instance_id": self.workspace_instance_id,
            "authority_namespace_sha256": self.authority_namespace_sha256,
            "snapshot_id": self.snapshot_id,
            "proof_sha256": self.proof_sha256,
            "dataset_record_sha256": self.dataset_record_sha256,
            "witness_sha256": self.witness_sha256,
            "published_at": self.published_at,
        }

    @classmethod
    def issue(
        cls,
        authority: publication.LegacyLineagePublicationAuthority,
        witness: publication.LegacyLineagePublicationWitness,
        *,
        published_at: str,
        key: bytes,
    ) -> "_IssuanceRecord":
        publication._instant(published_at, "published_at")
        core = {
            "kind": _ISSUANCE_RECORD_KIND,
            "schema_version": _ISSUANCE_SCHEMA_VERSION,
            "workspace_instance_id": authority.monotonic_authority.workspace_instance_id,
            "authority_namespace_sha256": authority.monotonic_authority.namespace_sha256,
            "snapshot_id": witness.snapshot_id,
            "proof_sha256": witness.proof_sha256,
            "dataset_record_sha256": witness.dataset_record_sha256,
            "witness_sha256": witness.witness_sha256,
            "published_at": published_at,
        }
        mac = hmac.new(key, _canonical_json(core), hashlib.sha256).hexdigest()
        return cls(
            workspace_instance_id=core["workspace_instance_id"],  # type: ignore[arg-type]
            authority_namespace_sha256=core["authority_namespace_sha256"],  # type: ignore[arg-type]
            snapshot_id=witness.snapshot_id,
            proof_sha256=witness.proof_sha256,
            dataset_record_sha256=witness.dataset_record_sha256,
            witness_sha256=witness.witness_sha256,
            published_at=published_at,
            issuer_mac_sha256=mac,
        )

    def verify(
        self,
        authority: publication.LegacyLineagePublicationAuthority,
        key: bytes,
    ) -> None:
        if self.workspace_instance_id != authority.monotonic_authority.workspace_instance_id:
            raise ValueError("legacy publication issuance workspace identity mismatch")
        if self.authority_namespace_sha256 != authority.monotonic_authority.namespace_sha256:
            raise ValueError("legacy publication issuance authority namespace mismatch")
        for value, name in (
            (self.authority_namespace_sha256, "authority_namespace_sha256"),
            (self.proof_sha256, "proof_sha256"),
            (self.dataset_record_sha256, "dataset_record_sha256"),
            (self.witness_sha256, "witness_sha256"),
            (self.issuer_mac_sha256, "issuer_mac_sha256"),
        ):
            _sha(value, name)
        publication._text(self.workspace_instance_id, "workspace_instance_id")
        publication._text(self.snapshot_id, "snapshot_id")
        publication._instant(self.published_at, "published_at")
        expected = hmac.new(key, _canonical_json(self.core_payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.issuer_mac_sha256):
            raise ValueError("legacy publication issuance authentication failed")

    def to_payload(self) -> dict[str, object]:
        return {**self.core_payload(), "issuer_mac_sha256": self.issuer_mac_sha256}

    @classmethod
    def from_payload(
        cls,
        raw: object,
        authority: publication.LegacyLineagePublicationAuthority,
        key: bytes,
    ) -> "_IssuanceRecord":
        expected = {
            "kind",
            "schema_version",
            "workspace_instance_id",
            "authority_namespace_sha256",
            "snapshot_id",
            "proof_sha256",
            "dataset_record_sha256",
            "witness_sha256",
            "published_at",
            "issuer_mac_sha256",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("legacy publication issuance record fields mismatch")
        if raw["kind"] != _ISSUANCE_RECORD_KIND or raw["schema_version"] != _ISSUANCE_SCHEMA_VERSION:
            raise ValueError("legacy publication issuance record schema mismatch")
        value = cls(
            workspace_instance_id=raw["workspace_instance_id"],
            authority_namespace_sha256=raw["authority_namespace_sha256"],
            snapshot_id=raw["snapshot_id"],
            proof_sha256=raw["proof_sha256"],
            dataset_record_sha256=raw["dataset_record_sha256"],
            witness_sha256=raw["witness_sha256"],
            published_at=raw["published_at"],
            issuer_mac_sha256=raw["issuer_mac_sha256"],
        )
        value.verify(authority, key)
        return value


def _read_issuances(
    authority: publication.LegacyLineagePublicationAuthority,
) -> tuple[_IssuanceRecord, ...]:
    key = _load_key(authority)
    path = _issuance_path(authority)
    if key is None or not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=publication._reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("legacy publication issuance state is unreadable") from exc
    if type(raw) is not dict or set(raw) != {"schema", "schema_version", "records"}:
        raise ValueError("legacy publication issuance state fields mismatch")
    if raw["schema"] != _ISSUANCE_SCHEMA or raw["schema_version"] != _ISSUANCE_SCHEMA_VERSION:
        raise ValueError("legacy publication issuance state schema mismatch")
    if type(raw["records"]) is not list:
        raise ValueError("legacy publication issuance records must be a list")
    records = tuple(_IssuanceRecord.from_payload(item, authority, key) for item in raw["records"])
    identities: set[tuple[str, str]] = set()
    for record in records:
        identity = (record.snapshot_id, record.proof_sha256)
        if identity in identities:
            raise ValueError("duplicate legacy publication issuance identity")
        identities.add(identity)
    return records


def _write_issuances(
    authority: publication.LegacyLineagePublicationAuthority,
    records: tuple[_IssuanceRecord, ...],
) -> None:
    path = _issuance_path(authority)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "schema": _ISSUANCE_SCHEMA,
            "schema_version": _ISSUANCE_SCHEMA_VERSION,
            "records": [record.to_payload() for record in records],
        },
    )


_ORIGINAL_PUBLISH_EXACT_CHAIN = publication.LegacyLineagePublicationAuthority.publish_exact_chain
_ORIGINAL_WITNESS_FOR = publication.LegacyLineagePublicationAuthority.witness_for


def _publish_exact_chain_with_issuance(
    self: publication.LegacyLineagePublicationAuthority,
    records: tuple[DatasetSnapshotLineageRecord, ...],
) -> tuple[publication.LegacyLineagePublicationWitness, ...]:
    key = _load_or_create_key(self)
    resolved = _ORIGINAL_PUBLISH_EXACT_CHAIN(self, records)
    if not resolved:
        return resolved
    with WorkspaceEconomicLock(_issuance_path(self).parent):
        existing = _read_issuances(self)
        by_identity = {(item.snapshot_id, item.proof_sha256): item for item in existing}
        additions: list[_IssuanceRecord] = []
        for witness in resolved:
            identity = (witness.snapshot_id, witness.proof_sha256)
            prior = by_identity.get(identity)
            if prior is not None:
                if (
                    prior.dataset_record_sha256 != witness.dataset_record_sha256
                    or prior.witness_sha256 != witness.witness_sha256
                ):
                    raise ValueError("legacy publication issuance binding mismatch")
                continue
            # The authenticated issuance time is always generated NOW.  In particular,
            # a pre-existing caller-forged/backdated generic witness is never inherited
            # as causal publication time when it is first legitimately re-observed.
            published_at = publication._authority_now_utc()
            additions.append(
                _IssuanceRecord.issue(
                    self,
                    witness,
                    published_at=published_at,
                    key=key,
                )
            )
        if additions:
            _write_issuances(self, (*existing, *additions))
    return resolved


def _witness_for_with_authenticated_issuance(
    self: publication.LegacyLineagePublicationAuthority,
    record: DatasetSnapshotLineageRecord,
) -> publication.LegacyLineagePublicationWitness | None:
    witness = _ORIGINAL_WITNESS_FOR(self, record)
    if witness is None:
        return None
    issuances = _read_issuances(self)
    for issued in issuances:
        if issued.snapshot_id == witness.snapshot_id and issued.proof_sha256 == witness.proof_sha256:
            if (
                issued.dataset_record_sha256 != witness.dataset_record_sha256
                or issued.witness_sha256 != witness.witness_sha256
            ):
                raise ValueError("legacy publication authenticated issuance binding mismatch")
            return publication.LegacyLineagePublicationWitness(
                snapshot_id=witness.snapshot_id,
                proof_sha256=witness.proof_sha256,
                dataset_record_sha256=witness.dataset_record_sha256,
                registered_at=issued.published_at,
            )
    return None


if not getattr(
    publication.LegacyLineagePublicationAuthority.publish_exact_chain,
    "_authenticated_issuance_guard",
    False,
):
    setattr(_publish_exact_chain_with_issuance, "_authenticated_issuance_guard", True)
    publication.LegacyLineagePublicationAuthority.publish_exact_chain = (
        _publish_exact_chain_with_issuance
    )

if not getattr(
    publication.LegacyLineagePublicationAuthority.witness_for,
    "_authenticated_issuance_guard",
    False,
):
    setattr(_witness_for_with_authenticated_issuance, "_authenticated_issuance_guard", True)
    publication.LegacyLineagePublicationAuthority.witness_for = (
        _witness_for_with_authenticated_issuance
    )


__all__: list[str] = []
