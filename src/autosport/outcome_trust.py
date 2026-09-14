from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .integrity import atomic_write_json

if TYPE_CHECKING:
    from .dataset import ReplayDataset


_TRUST_FILE = "outcome_lineage_trust.json"


class OutcomeLineageTrustError(ValueError):
    """Raised when a governed workspace observes a conflicting outcome history."""


@dataclass(frozen=True, slots=True)
class TrustedOutcomeRevision:
    revision: int
    revision_id: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class OutcomeLineageBinding:
    source_identity: str
    record_id: str
    root_revision_id: str
    root_record_sha256: str
    revisions: tuple[TrustedOutcomeRevision, ...]

    @property
    def head(self) -> TrustedOutcomeRevision:
        return self.revisions[-1]


def bind_dataset_outcome_lineage(
    workspace: str | Path,
    dataset: ReplayDataset,
) -> OutcomeLineageBinding | None:
    """Bind one verified schema-v2 outcome chain to a durable workspace.

    The caller must own the workspace's cross-process writer lock.  This routine
    deliberately performs trust-on-first-use only for the already checksum-bound
    sealed ``results.json`` bytes.  Once a ``(source_identity, record_id)`` has
    been accepted, a later import may only repeat a verified prefix or extend the
    exact same root-to-head chain.  A restarted revision-1 root or any divergent
    revision therefore fails before paper/economic mutation.
    """

    binding = _binding_from_dataset(dataset)
    if binding is None:
        return None

    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    trust_path = workspace_path / _TRUST_FILE
    records = _load_registry(trust_path)

    matches = [
        record
        for record in records
        if record.source_identity == binding.source_identity
        and record.record_id == binding.record_id
    ]
    if len(matches) > 1:
        raise OutcomeLineageTrustError(
            "outcome lineage trust registry contains duplicate source/record identities"
        )

    changed = False
    if not matches:
        records.append(binding)
        changed = True
    else:
        trusted = matches[0]
        if (
            trusted.root_revision_id != binding.root_revision_id
            or trusted.root_record_sha256 != binding.root_record_sha256
        ):
            raise OutcomeLineageTrustError(
                "outcome lineage trust conflict: accepted source/record identity restarted from a different root"
            )

        overlap = min(len(trusted.revisions), len(binding.revisions))
        for index in range(overlap):
            accepted = trusted.revisions[index]
            incoming = binding.revisions[index]
            if accepted != incoming:
                raise OutcomeLineageTrustError(
                    "outcome lineage trust conflict: accepted source/record identity diverged at "
                    f"revision {index + 1}"
                )

        if len(binding.revisions) > len(trusted.revisions):
            records[records.index(trusted)] = binding
            changed = True

    if changed:
        records.sort(key=lambda item: (item.source_identity, item.record_id))
        atomic_write_json(
            trust_path,
            {
                "schema_version": 1,
                "records": [_record_payload(record) for record in records],
            },
        )
    return binding


def _binding_from_dataset(dataset: ReplayDataset) -> OutcomeLineageBinding | None:
    if dataset.schema_version != 2:
        return None

    try:
        payload = dataset.results_path.read_bytes()
    except OSError as exc:
        raise OutcomeLineageTrustError(
            "sealed results are not readable while binding outcome lineage trust"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != dataset.results_sha256:
        raise OutcomeLineageTrustError(
            "sealed results hash changed before outcome lineage trust binding"
        )

    raw = _strict_json_object(payload, context="sealed results trust payload")
    provenance = raw.get("outcome_provenance")
    if provenance is None:
        return None
    if not isinstance(provenance, dict):
        raise OutcomeLineageTrustError(
            "sealed results outcome_provenance must be an object for trust binding"
        )

    schema_version = provenance.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise OutcomeLineageTrustError(
            "sealed results outcome_provenance.schema_version must be exact integer 1 or 2"
        )
    if schema_version == 1:
        return None

    if provenance.get("source_record_lineage_verified") is not True:
        raise OutcomeLineageTrustError(
            "schema-v2 outcome provenance must carry verified complete lineage evidence"
        )

    source_identity = _canonical_text(
        provenance.get("source_identity"),
        field="outcome provenance source_identity",
    )
    record_id = _canonical_text(
        provenance.get("source_record_id"),
        field="outcome provenance source_record_id",
    )
    root_revision_id = _canonical_text(
        provenance.get("source_record_lineage_root_revision_id"),
        field="outcome provenance lineage root revision_id",
    )
    root_record_sha256 = _digest(
        provenance.get("source_record_lineage_root_sha256"),
        field="outcome provenance lineage root SHA-256",
    )
    head_revision_id = _canonical_text(
        provenance.get("source_record_revision_id"),
        field="outcome provenance head revision_id",
    )
    head_revision = _positive_int(
        provenance.get("source_record_revision"),
        field="outcome provenance head revision",
    )
    head_record_sha256 = _digest(
        provenance.get("source_record_sha256"),
        field="outcome provenance head record SHA-256",
    )
    lineage_depth = _positive_int(
        provenance.get("source_record_lineage_depth"),
        field="outcome provenance lineage depth",
    )

    raw_revisions = provenance.get("source_record_lineage")
    if not isinstance(raw_revisions, list) or not raw_revisions:
        raise OutcomeLineageTrustError(
            "schema-v2 outcome provenance source_record_lineage must be a non-empty list"
        )
    if len(raw_revisions) != lineage_depth:
        raise OutcomeLineageTrustError(
            "schema-v2 outcome provenance lineage depth does not match complete lineage evidence"
        )

    revisions: list[TrustedOutcomeRevision] = []
    previous: TrustedOutcomeRevision | None = None
    seen_revision_ids: set[str] = set()
    for index, raw_revision in enumerate(raw_revisions, start=1):
        if not isinstance(raw_revision, dict):
            raise OutcomeLineageTrustError(
                f"outcome provenance lineage revision {index} must be an object"
            )
        revision = _positive_int(
            raw_revision.get("revision"),
            field=f"outcome provenance lineage revision {index} number",
        )
        if revision != index:
            raise OutcomeLineageTrustError(
                "outcome provenance complete lineage must be contiguous from revision 1"
            )
        revision_id = _canonical_text(
            raw_revision.get("revision_id"),
            field=f"outcome provenance lineage revision {index} revision_id",
        )
        if revision_id in seen_revision_ids:
            raise OutcomeLineageTrustError(
                "outcome provenance complete lineage reuses a revision_id"
            )
        seen_revision_ids.add(revision_id)
        record_sha256 = _digest(
            raw_revision.get("record_sha256"),
            field=f"outcome provenance lineage revision {index} record SHA-256",
        )
        revision_kind = _canonical_text(
            raw_revision.get("revision_kind"),
            field=f"outcome provenance lineage revision {index} kind",
        )

        if index == 1:
            if revision_kind != "initial":
                raise OutcomeLineageTrustError(
                    "outcome provenance lineage revision 1 must be initial"
                )
            if raw_revision.get("predecessor_record_sha256") is not None:
                raise OutcomeLineageTrustError(
                    "outcome provenance lineage revision 1 must not name a predecessor SHA-256"
                )
            if raw_revision.get("supersedes_revision_id") is not None:
                raise OutcomeLineageTrustError(
                    "outcome provenance lineage revision 1 must not supersede another revision"
                )
        else:
            if revision_kind != "correction":
                raise OutcomeLineageTrustError(
                    "outcome provenance lineage revisions above 1 must be corrections"
                )
            assert previous is not None
            predecessor_sha = _digest(
                raw_revision.get("predecessor_record_sha256"),
                field=f"outcome provenance lineage revision {index} predecessor SHA-256",
            )
            if predecessor_sha != previous.record_sha256:
                raise OutcomeLineageTrustError(
                    "outcome provenance lineage predecessor SHA-256 does not match prior revision"
                )
            supersedes = _canonical_text(
                raw_revision.get("supersedes_revision_id"),
                field=f"outcome provenance lineage revision {index} supersedes_revision_id",
            )
            if supersedes != previous.revision_id:
                raise OutcomeLineageTrustError(
                    "outcome provenance lineage supersedes_revision_id does not match prior revision"
                )

        current = TrustedOutcomeRevision(revision, revision_id, record_sha256)
        revisions.append(current)
        previous = current

    first = revisions[0]
    last = revisions[-1]
    if first.revision_id != root_revision_id or first.record_sha256 != root_record_sha256:
        raise OutcomeLineageTrustError(
            "outcome provenance lineage root summary does not match complete lineage evidence"
        )
    if (
        last.revision_id != head_revision_id
        or last.revision != head_revision
        or last.record_sha256 != head_record_sha256
    ):
        raise OutcomeLineageTrustError(
            "outcome provenance lineage head summary does not match complete lineage evidence"
        )

    return OutcomeLineageBinding(
        source_identity=source_identity,
        record_id=record_id,
        root_revision_id=root_revision_id,
        root_record_sha256=root_record_sha256,
        revisions=tuple(revisions),
    )


def _load_registry(path: Path) -> list[OutcomeLineageBinding]:
    if not path.exists():
        return []
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise OutcomeLineageTrustError(
            "outcome lineage trust registry is not readable"
        ) from exc
    raw = _strict_json_object(payload, context="outcome lineage trust registry")
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise OutcomeLineageTrustError(
            "outcome lineage trust registry schema_version must be exact integer 1"
        )
    raw_records = raw.get("records")
    if not isinstance(raw_records, list):
        raise OutcomeLineageTrustError(
            "outcome lineage trust registry records must be a list"
        )

    records: list[OutcomeLineageBinding] = []
    identities: set[tuple[str, str]] = set()
    for record_index, raw_record in enumerate(raw_records, start=1):
        if not isinstance(raw_record, dict):
            raise OutcomeLineageTrustError(
                f"outcome lineage trust registry record {record_index} must be an object"
            )
        source_identity = _canonical_text(
            raw_record.get("source_identity"),
            field=f"trust registry record {record_index} source_identity",
        )
        record_id = _canonical_text(
            raw_record.get("record_id"),
            field=f"trust registry record {record_index} record_id",
        )
        identity = (source_identity, record_id)
        if identity in identities:
            raise OutcomeLineageTrustError(
                "outcome lineage trust registry contains duplicate source/record identities"
            )
        identities.add(identity)
        root_revision_id = _canonical_text(
            raw_record.get("root_revision_id"),
            field=f"trust registry record {record_index} root_revision_id",
        )
        root_record_sha256 = _digest(
            raw_record.get("root_record_sha256"),
            field=f"trust registry record {record_index} root record SHA-256",
        )
        revisions = _registry_revisions(
            raw_record.get("revisions"),
            context=f"trust registry record {record_index}",
        )
        if (
            revisions[0].revision_id != root_revision_id
            or revisions[0].record_sha256 != root_record_sha256
        ):
            raise OutcomeLineageTrustError(
                "outcome lineage trust registry root summary does not match revision history"
            )
        records.append(
            OutcomeLineageBinding(
                source_identity=source_identity,
                record_id=record_id,
                root_revision_id=root_revision_id,
                root_record_sha256=root_record_sha256,
                revisions=revisions,
            )
        )
    return records


def _registry_revisions(value: object, *, context: str) -> tuple[TrustedOutcomeRevision, ...]:
    if not isinstance(value, list) or not value:
        raise OutcomeLineageTrustError(f"{context} revisions must be a non-empty list")
    revisions: list[TrustedOutcomeRevision] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise OutcomeLineageTrustError(f"{context} revision {index} must be an object")
        revision = _positive_int(raw.get("revision"), field=f"{context} revision {index} number")
        if revision != index:
            raise OutcomeLineageTrustError(f"{context} revisions must be contiguous from revision 1")
        revision_id = _canonical_text(
            raw.get("revision_id"), field=f"{context} revision {index} revision_id"
        )
        if revision_id in seen_ids:
            raise OutcomeLineageTrustError(f"{context} revisions reuse a revision_id")
        seen_ids.add(revision_id)
        revisions.append(
            TrustedOutcomeRevision(
                revision=revision,
                revision_id=revision_id,
                record_sha256=_digest(
                    raw.get("record_sha256"),
                    field=f"{context} revision {index} record SHA-256",
                ),
            )
        )
    return tuple(revisions)


def _record_payload(binding: OutcomeLineageBinding) -> dict[str, Any]:
    return {
        "source_identity": binding.source_identity,
        "record_id": binding.record_id,
        "root_revision_id": binding.root_revision_id,
        "root_record_sha256": binding.root_record_sha256,
        "revisions": [
            {
                "revision": revision.revision,
                "revision_id": revision.revision_id,
                "record_sha256": revision.record_sha256,
            }
            for revision in binding.revisions
        ],
    }


def _strict_json_object(payload: bytes, *, context: str) -> dict[str, Any]:
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise OutcomeLineageTrustError(
                    f"{context} contains duplicate JSON object key: {key}"
                )
            value[key] = item
        return value

    def _reject_nonstandard_constant(value: str) -> None:
        raise OutcomeLineageTrustError(
            f"{context} contains non-standard JSON constant: {value}"
        )

    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except UnicodeDecodeError as exc:
        raise OutcomeLineageTrustError(f"{context} is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise OutcomeLineageTrustError(f"{context} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise OutcomeLineageTrustError(f"{context} must be a JSON object")
    return raw


def _canonical_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OutcomeLineageTrustError(
            f"{field} must be a non-empty canonical string without surrounding whitespace"
        )
    return value


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise OutcomeLineageTrustError(f"{field} must be a positive exact integer")
    return value


def _digest(value: object, *, field: str) -> str:
    text = _canonical_text(value, field=field)
    if (
        len(text) != 64
        or text != text.lower()
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise OutcomeLineageTrustError(
            f"{field} must be a canonical lowercase SHA-256 hex digest"
        )
    return text
