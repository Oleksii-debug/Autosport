from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_ALLOWED_OUTCOMES = frozenset({"win", "loss", "void"})
_ALLOWED_REVISION_KINDS = frozenset({"initial", "correction"})
_MAX_REVISION_CHAIN_LENGTH = 128


class _DuplicateJsonKeyError(ValueError):
    pass


class _NonStandardJsonConstantError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OutcomeRevisionChain:
    revision_id: str
    revision_kind: str
    revision_effective_at: str
    supersedes_revision_id: str | None
    predecessor_record_file: str | None
    predecessor_record_sha256: str | None
    root_revision_id: str
    chain_length: int
    quote_outcomes_sha256: str


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise _NonStandardJsonConstantError(value)


def _strict_json_object(payload: bytes, *, context: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{context} is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except _DuplicateJsonKeyError as exc:
        raise ValueError(
            f"{context} contains duplicate JSON object key: {exc.args[0]}"
        ) from exc
    except _NonStandardJsonConstantError as exc:
        raise ValueError(
            f"{context} contains non-standard JSON constant: {exc.args[0]}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a JSON object")
    return raw


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{field} must be a non-empty canonical ISO-8601 timestamp without surrounding whitespace"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return canonical, parsed


def _canonical_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"{field} must be a non-empty canonical string without surrounding whitespace"
        )
    return value


def _digest(value: Any, *, field: str) -> str:
    digest = _canonical_string(value, field=field)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{field} must be a canonical lowercase SHA-256 hex digest")
    return digest


def _direct_sibling(value: Any, *, field: str) -> str:
    text = _canonical_string(value, field=field)
    relative = Path(text)
    if relative.is_absolute() or len(relative.parts) != 1 or text in {".", ".."}:
        raise ValueError(f"{field} must name one direct sibling artifact")
    return text


def _quote_outcomes(record: dict[str, Any], *, context: str) -> dict[str, str]:
    raw = record.get("quote_outcomes")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{context}.quote_outcomes must be a non-empty object")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or key != key.strip():
            raise ValueError(
                f"{context}.quote_outcomes keys must be non-empty canonical strings"
            )
        if not isinstance(value, str) or value not in _ALLOWED_OUTCOMES:
            raise ValueError(
                f"{context}.quote_outcomes values must be win, loss, or void"
            )
        normalized[key] = value
    return normalized


def _read_predecessor(path: Path, *, context: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{context} is not readable: {path}") from exc


def _verify_record(
    *,
    source_root: Path,
    record_file: str,
    record: dict[str, Any],
    expected_source_identity: str,
    seen_files: frozenset[str],
    depth: int,
) -> tuple[OutcomeRevisionChain, dict[str, str], datetime]:
    if depth > _MAX_REVISION_CHAIN_LENGTH:
        raise ValueError(
            f"outcome revision chain exceeds maximum length {_MAX_REVISION_CHAIN_LENGTH}"
        )
    if record_file in seen_files:
        raise ValueError("outcome revision predecessor chain contains a cycle")
    seen_files = seen_files | {record_file}

    source = _canonical_string(
        record.get("source"),
        field="sealed outcome source record.source",
    )
    if source != expected_source_identity:
        raise ValueError(
            "sealed outcome revision predecessor source must match outcome_provenance.source_identity"
        )
    outcomes = _quote_outcomes(record, context="sealed outcome source record")
    outcomes_sha256 = _canonical_json_sha256(outcomes)

    revision = record.get("revision")
    if not isinstance(revision, dict):
        raise ValueError("sealed outcome source record.revision must be an object")
    schema_version = revision.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError("sealed outcome source record.revision.schema_version must be exact integer 1")
    kind = _canonical_string(
        revision.get("kind"),
        field="sealed outcome source record.revision.kind",
    )
    if kind not in _ALLOWED_REVISION_KINDS:
        raise ValueError(
            "sealed outcome source record.revision.kind must be initial or correction"
        )
    effective_at, effective_dt = _canonical_timestamp(
        revision.get("effective_at"),
        field="sealed outcome source record.revision.effective_at",
    )

    supersedes_raw = revision.get("supersedes_revision_id")
    predecessor_file_raw = revision.get("predecessor_record_file")
    predecessor_sha_raw = revision.get("predecessor_record_sha256")

    predecessor_chain: OutcomeRevisionChain | None = None
    predecessor_outcomes: dict[str, str] | None = None
    predecessor_effective_dt: datetime | None = None
    supersedes_revision_id: str | None
    predecessor_record_file: str | None
    predecessor_record_sha256: str | None

    if kind == "initial":
        if supersedes_raw is not None:
            raise ValueError(
                "initial outcome revision must set supersedes_revision_id=null"
            )
        if predecessor_file_raw is not None:
            raise ValueError(
                "initial outcome revision must set predecessor_record_file=null"
            )
        if predecessor_sha_raw is not None:
            raise ValueError(
                "initial outcome revision must set predecessor_record_sha256=null"
            )
        supersedes_revision_id = None
        predecessor_record_file = None
        predecessor_record_sha256 = None
    else:
        supersedes_revision_id = _digest(
            supersedes_raw,
            field="sealed outcome source record.revision.supersedes_revision_id",
        )
        predecessor_record_file = _direct_sibling(
            predecessor_file_raw,
            field="sealed outcome source record.revision.predecessor_record_file",
        )
        predecessor_record_sha256 = _digest(
            predecessor_sha_raw,
            field="sealed outcome source record.revision.predecessor_record_sha256",
        )
        predecessor_path = source_root / predecessor_record_file
        predecessor_bytes = _read_predecessor(
            predecessor_path,
            context="sealed outcome predecessor source record",
        )
        actual_predecessor_sha = hashlib.sha256(predecessor_bytes).hexdigest()
        if actual_predecessor_sha != predecessor_record_sha256:
            raise ValueError(
                "sealed outcome predecessor_record_sha256 does not match predecessor source record artifact"
            )
        predecessor_record = _strict_json_object(
            predecessor_bytes,
            context="sealed outcome predecessor source record",
        )
        predecessor_chain, predecessor_outcomes, predecessor_effective_dt = _verify_record(
            source_root=source_root,
            record_file=predecessor_record_file,
            record=predecessor_record,
            expected_source_identity=expected_source_identity,
            seen_files=seen_files,
            depth=depth + 1,
        )
        if supersedes_revision_id != predecessor_chain.revision_id:
            raise ValueError(
                "correction supersedes_revision_id must match verified predecessor revision_id"
            )
        if effective_dt <= predecessor_effective_dt:
            raise ValueError(
                "correction revision effective_at must be strictly after predecessor effective_at"
            )
        if set(outcomes) != set(predecessor_outcomes):
            raise ValueError(
                "correction revision must preserve predecessor quote_outcomes key set"
            )
        if outcomes == predecessor_outcomes:
            raise ValueError("correction revision must change at least one outcome")

    identity_payload = {
        "schema_version": 1,
        "source_identity": expected_source_identity,
        "kind": kind,
        "effective_at": effective_at,
        "supersedes_revision_id": supersedes_revision_id,
        "predecessor_record_sha256": predecessor_record_sha256,
        "quote_outcomes_sha256": outcomes_sha256,
    }
    expected_revision_id = _canonical_json_sha256(identity_payload)
    revision_id = _digest(
        revision.get("revision_id"),
        field="sealed outcome source record.revision.revision_id",
    )
    if revision_id != expected_revision_id:
        raise ValueError(
            "sealed outcome source record.revision.revision_id does not match canonical revision identity"
        )

    if predecessor_chain is None:
        root_revision_id = revision_id
        chain_length = 1
    else:
        root_revision_id = predecessor_chain.root_revision_id
        chain_length = predecessor_chain.chain_length + 1

    return (
        OutcomeRevisionChain(
            revision_id=revision_id,
            revision_kind=kind,
            revision_effective_at=effective_at,
            supersedes_revision_id=supersedes_revision_id,
            predecessor_record_file=predecessor_record_file,
            predecessor_record_sha256=predecessor_record_sha256,
            root_revision_id=root_revision_id,
            chain_length=chain_length,
            quote_outcomes_sha256=outcomes_sha256,
        ),
        outcomes,
        effective_dt,
    )


def canonical_outcome_revision_id(
    *,
    source_identity: str,
    kind: str,
    effective_at: str,
    quote_outcomes: dict[str, str],
    supersedes_revision_id: str | None = None,
    predecessor_record_sha256: str | None = None,
) -> str:
    """Return the canonical revision identity used by authoritative outcome records."""

    source_identity = _canonical_string(source_identity, field="source_identity")
    if kind not in _ALLOWED_REVISION_KINDS:
        raise ValueError("kind must be initial or correction")
    canonical_effective_at, _ = _canonical_timestamp(effective_at, field="effective_at")
    outcomes = _quote_outcomes(
        {"quote_outcomes": quote_outcomes},
        context="outcome revision",
    )
    if kind == "initial":
        if supersedes_revision_id is not None or predecessor_record_sha256 is not None:
            raise ValueError("initial revision cannot name a predecessor")
    else:
        supersedes_revision_id = _digest(
            supersedes_revision_id,
            field="supersedes_revision_id",
        )
        predecessor_record_sha256 = _digest(
            predecessor_record_sha256,
            field="predecessor_record_sha256",
        )
    return _canonical_json_sha256(
        {
            "schema_version": 1,
            "source_identity": source_identity,
            "kind": kind,
            "effective_at": canonical_effective_at,
            "supersedes_revision_id": supersedes_revision_id,
            "predecessor_record_sha256": predecessor_record_sha256,
            "quote_outcomes_sha256": _canonical_json_sha256(outcomes),
        }
    )


def verify_outcome_revision_chain(
    *,
    source_root: Path,
    source_record_file: str,
    source_record: dict[str, Any],
    expected_source_identity: str,
    available_at: datetime,
) -> OutcomeRevisionChain:
    """Verify one authoritative outcome revision and its recursively hash-bound predecessors.

    The current source record is supplied by the caller so its already-hashed bytes do not
    need to be read a second time. Correction predecessors are read exactly once each,
    hash-checked, strictly parsed, and recursively bound to the same source identity.
    """

    source_record_file = _direct_sibling(
        source_record_file,
        field="sealed results outcome_provenance.source_record_file",
    )
    expected_source_identity = _canonical_string(
        expected_source_identity,
        field="sealed results outcome_provenance.source_identity",
    )
    chain, _, effective_dt = _verify_record(
        source_root=Path(source_root),
        record_file=source_record_file,
        record=source_record,
        expected_source_identity=expected_source_identity,
        seen_files=frozenset(),
        depth=1,
    )
    if effective_dt > available_at:
        raise ValueError(
            "outcome revision effective_at must not be after outcome source available_at"
        )
    return chain
