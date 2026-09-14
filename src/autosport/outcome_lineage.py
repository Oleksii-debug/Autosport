from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


_ALLOWED_OUTCOMES = frozenset({"win", "loss", "void"})
_MAX_LINEAGE_DEPTH = 1024


@dataclass(frozen=True, slots=True)
class OutcomeSourceLineage:
    source_identity: str
    record_id: str
    revision_id: str
    revision: int
    revision_kind: str
    recorded_at: str
    quote_outcomes: dict[str, str]
    predecessor_record_sha256: str | None
    supersedes_revision_id: str | None
    lineage_root_sha256: str
    lineage_root_revision_id: str
    lineage_depth: int


def validate_outcome_source_lineage(
    *,
    source_root: Path,
    source_record_file: str,
    source_record_sha256: str,
    source_record: dict[str, Any],
    expected_source_identity: str,
) -> OutcomeSourceLineage:
    """Validate a schema-v2 append-only correction chain.

    ``source_record`` is the already-frozen, already-hash-verified current head.
    This function deliberately never re-opens ``source_record_file``. Each
    predecessor is read exactly once, hashed, and parsed from those same bytes.
    """

    current_file = _sibling_name(
        source_record_file,
        field="sealed results outcome_provenance.source_record_file",
    )
    current_sha = _sha256_hex(
        source_record_sha256,
        field="sealed results outcome_provenance.source_record_sha256",
    )
    expected_source = _canonical_text(
        expected_source_identity,
        field="sealed results outcome_provenance.source_identity",
    )
    if not isinstance(source_record, dict):
        raise ValueError("sealed outcome source record must be a JSON object")

    source_root_resolved = source_root.resolve()
    current_record = source_record
    visited_files: set[str] = set()
    visited_paths: set[Path] = {(source_root / current_file).resolve()}
    visited_revision_ids: set[str] = set()
    head: dict[str, Any] | None = None
    head_predecessor_sha: str | None = None
    head_supersedes_revision_id: str | None = None
    expected_record_id: str | None = None
    expected_revision: int | None = None
    successor_recorded_at: datetime | None = None
    successor_supersedes_revision_id: str | None = None
    lineage_depth = 0
    lineage_root_sha256: str | None = None
    lineage_root_revision_id: str | None = None

    while True:
        if lineage_depth >= _MAX_LINEAGE_DEPTH:
            raise ValueError(
                f"sealed outcome source record lineage exceeds {_MAX_LINEAGE_DEPTH} revisions"
            )
        if current_file in visited_files:
            raise ValueError("sealed outcome source record lineage contains a cycle")
        visited_files.add(current_file)

        fields = _record_fields(current_record)
        source_identity = fields["source_identity"]
        record_id = fields["record_id"]
        revision_id = fields["revision_id"]
        revision = fields["revision"]
        revision_kind = fields["revision_kind"]
        recorded_at = fields["recorded_at"]
        recorded_dt = fields["recorded_dt"]
        quote_outcomes = fields["quote_outcomes"]

        if revision_id in visited_revision_ids:
            raise ValueError("sealed outcome source record lineage reuses a revision_id")
        visited_revision_ids.add(revision_id)

        if lineage_depth == 0:
            if source_identity != expected_source:
                raise ValueError(
                    "sealed outcome source record.source must match sealed results outcome_provenance.source_identity"
                )
            expected_record_id = record_id
            expected_revision = revision
            head = {
                "source_identity": source_identity,
                "record_id": record_id,
                "revision_id": revision_id,
                "revision": revision,
                "revision_kind": revision_kind,
                "recorded_at": recorded_at,
                "quote_outcomes": quote_outcomes,
            }
        else:
            if source_identity != expected_source:
                raise ValueError(
                    "sealed outcome predecessor source must match correction source identity"
                )
            if record_id != expected_record_id:
                raise ValueError(
                    "sealed outcome predecessor record_id must match correction record_id"
                )
            if revision != expected_revision:
                raise ValueError(
                    "sealed outcome predecessor revision must be exactly one less than its successor"
                )
            if successor_recorded_at is None or recorded_dt >= successor_recorded_at:
                raise ValueError(
                    "sealed outcome correction recorded_at must strictly follow its predecessor"
                )
            if successor_supersedes_revision_id != revision_id:
                raise ValueError(
                    "sealed outcome correction supersedes_revision_id must equal its immediate predecessor revision_id"
                )

        lineage_depth += 1
        predecessor_file_raw = current_record.get("predecessor_record_file")
        predecessor_sha_raw = current_record.get("predecessor_record_sha256")
        supersedes_raw = current_record.get("supersedes_revision_id")
        correction_reason_raw = current_record.get("correction_reason")

        if revision == 1:
            if revision_kind != "initial":
                raise ValueError(
                    "sealed outcome source record revision 1 must set revision_kind=initial"
                )
            if any(
                value is not None
                for value in (
                    predecessor_file_raw,
                    predecessor_sha_raw,
                    supersedes_raw,
                    correction_reason_raw,
                )
            ):
                raise ValueError(
                    "sealed outcome source record revision 1 must not declare correction lineage fields"
                )
            lineage_root_sha256 = current_sha
            lineage_root_revision_id = revision_id
            break

        if revision_kind != "correction":
            raise ValueError(
                "sealed outcome source record revision above 1 must set revision_kind=correction"
            )
        predecessor_file = _sibling_name(
            predecessor_file_raw,
            field="sealed outcome source record.predecessor_record_file",
        )
        predecessor_sha = _sha256_hex(
            predecessor_sha_raw,
            field="sealed outcome source record.predecessor_record_sha256",
        )
        supersedes_revision_id = _canonical_text(
            supersedes_raw,
            field="sealed outcome source record.supersedes_revision_id",
        )
        _canonical_text(
            correction_reason_raw,
            field="sealed outcome source record.correction_reason",
        )
        if lineage_depth == 1:
            head_predecessor_sha = predecessor_sha
            head_supersedes_revision_id = supersedes_revision_id

        predecessor_path = source_root / predecessor_file
        predecessor_resolved = predecessor_path.resolve()
        if predecessor_resolved.parent != source_root_resolved:
            raise ValueError(
                "sealed outcome source record predecessor must resolve to a direct sibling artifact"
            )
        if predecessor_resolved in visited_paths:
            raise ValueError("sealed outcome source record lineage contains a path cycle")
        visited_paths.add(predecessor_resolved)

        predecessor_bytes = _read_bytes(predecessor_path)
        actual_predecessor_sha = hashlib.sha256(predecessor_bytes).hexdigest()
        if actual_predecessor_sha != predecessor_sha:
            raise ValueError(
                "sealed outcome source record predecessor SHA-256 does not match referenced artifact"
            )
        predecessor_record = _json_object_bytes(
            predecessor_bytes,
            path=predecessor_path,
            context="sealed outcome predecessor record",
        )

        expected_revision = revision - 1
        successor_recorded_at = recorded_dt
        successor_supersedes_revision_id = supersedes_revision_id
        current_file = predecessor_file
        current_sha = predecessor_sha
        current_record = predecessor_record

    assert head is not None
    assert lineage_root_sha256 is not None
    assert lineage_root_revision_id is not None
    return OutcomeSourceLineage(
        source_identity=str(head["source_identity"]),
        record_id=str(head["record_id"]),
        revision_id=str(head["revision_id"]),
        revision=int(head["revision"]),
        revision_kind=str(head["revision_kind"]),
        recorded_at=str(head["recorded_at"]),
        quote_outcomes=dict(head["quote_outcomes"]),
        predecessor_record_sha256=head_predecessor_sha,
        supersedes_revision_id=head_supersedes_revision_id,
        lineage_root_sha256=lineage_root_sha256,
        lineage_root_revision_id=lineage_root_revision_id,
        lineage_depth=lineage_depth,
    )


def _record_fields(raw: dict[str, Any]) -> dict[str, Any]:
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        raise ValueError("sealed outcome source record.schema_version must be exact integer 2")
    source_identity = _canonical_text(
        raw.get("source"), field="sealed outcome source record.source"
    )
    record_id = _canonical_text(
        raw.get("record_id"), field="sealed outcome source record.record_id"
    )
    revision_id = _canonical_text(
        raw.get("revision_id"), field="sealed outcome source record.revision_id"
    )
    revision = _positive_int(
        raw.get("revision"), field="sealed outcome source record.revision"
    )
    revision_kind = _canonical_text(
        raw.get("revision_kind"), field="sealed outcome source record.revision_kind"
    )
    recorded_at = _canonical_text(
        raw.get("recorded_at"), field="sealed outcome source record.recorded_at"
    )
    recorded_dt = _timestamp(
        recorded_at, field="sealed outcome source record.recorded_at"
    )
    quote_outcomes = _quote_outcomes(raw)
    return {
        "source_identity": source_identity,
        "record_id": record_id,
        "revision_id": revision_id,
        "revision": revision,
        "revision_kind": revision_kind,
        "recorded_at": recorded_at,
        "recorded_dt": recorded_dt,
        "quote_outcomes": quote_outcomes,
    }


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"sealed outcome predecessor record is not readable: {path}") from exc


def _json_object_bytes(
    payload: bytes,
    *,
    path: Path,
    context: str,
) -> dict[str, Any]:
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{context} contains duplicate JSON object key: {key}")
            value[key] = item
        return value

    def _reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"{context} contains non-standard JSON constant: {value}")

    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} is not readable valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a JSON object")
    return raw


def _canonical_text(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(
            f"{field} must be a non-empty canonical string without surrounding whitespace"
        )
    return value


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive exact integer")
    return value


def _sha256_hex(value: object, *, field: str) -> str:
    text = _canonical_text(value, field=field)
    if (
        len(text) != 64
        or text != text.lower()
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise ValueError(f"{field} must be a canonical lowercase SHA-256 hex digest")
    return text


def _sibling_name(value: object, *, field: str) -> str:
    text = _canonical_text(value, field=field)
    candidate = Path(text)
    if candidate.is_absolute() or len(candidate.parts) != 1 or text in {".", ".."}:
        raise ValueError(f"{field} must name one direct sibling artifact")
    return text


def _timestamp(value: object, *, field: str) -> datetime:
    text = _canonical_text(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def _quote_outcomes(raw: dict[str, Any]) -> dict[str, str]:
    value = raw.get("quote_outcomes")
    if not isinstance(value, dict):
        raise ValueError("sealed outcome source record quote_outcomes must be an object")
    normalized: dict[str, str] = {}
    for quote_key, outcome in value.items():
        if not isinstance(quote_key, str) or not quote_key:
            raise ValueError("sealed outcome source record quote keys must be non-empty strings")
        if not isinstance(outcome, str) or outcome not in _ALLOWED_OUTCOMES:
            raise ValueError(
                "sealed outcome source record contains unsupported outcome; allowed values are win, loss, void"
            )
        normalized[quote_key] = outcome
    return normalized
