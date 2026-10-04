from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .dataset import ReplayDataset


class OutcomeLineageTrustError(ValueError):
    """Raised when authoritative outcome lineage trust is malformed or conflicts."""


@dataclass(frozen=True, slots=True)
class TrustedOutcomeRevision:
    revision: int
    revision_id: str
    record_sha256: str
    first_available_at: str | None = None


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


def outcome_lineage_binding_from_dataset(
    dataset: ReplayDataset,
) -> OutcomeLineageBinding | None:
    """Recover verified lineage identity from checksum-bound released results.

    Schema-v1 provenance deliberately carries no cross-import correction claim.
    Schema-v2 provenance must contain the complete verifier-derived root-to-head
    descriptor emitted by the canonical corpus assembler.  The results bytes are
    re-hashed here so trust cannot be bound from a post-load file swap.
    """

    if dataset.schema_version != 2:
        return None

    try:
        payload = dataset.results_path.read_bytes()
    except OSError as exc:
        raise OutcomeLineageTrustError(
            "sealed results are not readable while recovering outcome lineage trust"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != dataset.results_sha256:
        raise OutcomeLineageTrustError(
            "sealed results hash changed before outcome lineage trust recovery"
        )

    raw = _strict_json_object(payload, context="sealed results trust payload")
    provenance = raw.get("outcome_provenance")
    if provenance is None:
        return None
    if not isinstance(provenance, dict):
        raise OutcomeLineageTrustError(
            "sealed results outcome_provenance must be an object for trust recovery"
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


def outcome_lineage_payload(binding: OutcomeLineageBinding) -> dict[str, Any]:
    return {
        "source_identity": binding.source_identity,
        "record_id": binding.record_id,
        "root_revision_id": binding.root_revision_id,
        "root_record_sha256": binding.root_record_sha256,
        "revisions": [_revision_payload(revision) for revision in binding.revisions],
    }


def _revision_payload(revision: TrustedOutcomeRevision) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "revision": revision.revision,
        "revision_id": revision.revision_id,
        "record_sha256": revision.record_sha256,
    }
    if revision.first_available_at is not None:
        payload["first_available_at"] = _canonical_timestamp(
            revision.first_available_at,
            field=f"outcome revision {revision.revision} first_available_at",
        )
    return payload


def outcome_lineage_binding_from_payload(
    value: object,
    *,
    context: str,
) -> OutcomeLineageBinding:
    if not isinstance(value, dict):
        raise OutcomeLineageTrustError(f"{context} must be an object")
    source_identity = _canonical_text(
        value.get("source_identity"), field=f"{context} source_identity"
    )
    record_id = _canonical_text(value.get("record_id"), field=f"{context} record_id")
    root_revision_id = _canonical_text(
        value.get("root_revision_id"), field=f"{context} root_revision_id"
    )
    root_record_sha256 = _digest(
        value.get("root_record_sha256"), field=f"{context} root record SHA-256"
    )
    raw_revisions = value.get("revisions")
    if not isinstance(raw_revisions, list) or not raw_revisions:
        raise OutcomeLineageTrustError(f"{context} revisions must be a non-empty list")

    revisions: list[TrustedOutcomeRevision] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_revisions, start=1):
        allowed_fields = {
            "revision",
            "revision_id",
            "record_sha256",
        }
        if (
            not isinstance(raw, dict)
            or not allowed_fields.issubset(raw)
            or not set(raw).issubset(allowed_fields | {"first_available_at"})
        ):
            raise OutcomeLineageTrustError(
                f"{context} revision {index} must contain only revision identity/availability fields"
            )
        revision = _positive_int(
            raw.get("revision"), field=f"{context} revision {index} number"
        )
        if revision != index:
            raise OutcomeLineageTrustError(
                f"{context} revisions must be contiguous from revision 1"
            )
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
                first_available_at=(
                    _canonical_timestamp(
                        raw.get("first_available_at"),
                        field=f"{context} revision {index} first_available_at",
                    )
                    if "first_available_at" in raw
                    else None
                ),
            )
        )

    availability_flags = [
        revision.first_available_at is not None for revision in revisions
    ]
    if any(availability_flags) and not all(availability_flags):
        raise OutcomeLineageTrustError(
            f"{context} must bind first availability for either every revision or none"
        )
    if (
        revisions[0].revision_id != root_revision_id
        or revisions[0].record_sha256 != root_record_sha256
    ):
        raise OutcomeLineageTrustError(
            f"{context} root summary does not match revision history"
        )
    return OutcomeLineageBinding(
        source_identity=source_identity,
        record_id=record_id,
        root_revision_id=root_revision_id,
        root_record_sha256=root_record_sha256,
        revisions=tuple(revisions),
    )


def assert_compatible_outcome_lineages(
    trusted: OutcomeLineageBinding,
    incoming: OutcomeLineageBinding,
) -> None:
    if trusted.source_identity != incoming.source_identity or trusted.record_id != incoming.record_id:
        return
    if (
        trusted.root_revision_id != incoming.root_revision_id
        or trusted.root_record_sha256 != incoming.root_record_sha256
    ):
        raise OutcomeLineageTrustError(
            "outcome lineage trust conflict: accepted source/record identity restarted from a different root"
        )
    overlap = min(len(trusted.revisions), len(incoming.revisions))
    for index in range(overlap):
        accepted = trusted.revisions[index]
        candidate = incoming.revisions[index]
        if (
            accepted.revision != candidate.revision
            or accepted.revision_id != candidate.revision_id
            or accepted.record_sha256 != candidate.record_sha256
        ):
            raise OutcomeLineageTrustError(
                "outcome lineage trust conflict: accepted source/record identity diverged at "
                f"revision {index + 1}"
            )
        if (
            accepted.first_available_at is not None
            and candidate.first_available_at is not None
            and _canonical_timestamp(
                accepted.first_available_at,
                field="trusted outcome first_available_at",
            )
            != _canonical_timestamp(
                candidate.first_available_at,
                field="incoming outcome first_available_at",
            )
        ):
            raise OutcomeLineageTrustError(
                "outcome lineage trust conflict: accepted product first availability was rewritten at "
                f"revision {index + 1}"
            )


def assert_outcome_availability_not_downgraded(
    authority: OutcomeLineageBinding,
    evidence: OutcomeLineageBinding,
) -> None:
    """Reject deletion of product availability still preserved by durable evidence."""

    if (
        authority.source_identity != evidence.source_identity
        or authority.record_id != evidence.record_id
    ):
        return
    assert_compatible_outcome_lineages(authority, evidence)
    overlap = min(len(authority.revisions), len(evidence.revisions))
    for index in range(overlap):
        authoritative = authority.revisions[index]
        preserved = evidence.revisions[index]
        if (
            preserved.first_available_at is not None
            and authoritative.first_available_at is None
        ):
            raise OutcomeLineageTrustError(
                "outcome lineage trust lost product first availability preserved by "
                f"durable evidence at revision {index + 1}"
            )


def bind_outcome_lineage_availability(
    incoming: OutcomeLineageBinding,
    *,
    accepted_at: str,
    trusted: OutcomeLineageBinding | None = None,
) -> OutcomeLineageBinding:
    """Bind source lineage to the product time at which it actually became trusted."""

    product_available_at = _canonical_timestamp(
        accepted_at,
        field="outcome lineage product acceptance time",
    )
    accepted_dt = _parse_timestamp(product_available_at)
    if trusted is not None:
        if (
            trusted.source_identity != incoming.source_identity
            or trusted.record_id != incoming.record_id
        ):
            raise OutcomeLineageTrustError(
                "cannot extend outcome availability from another source/record identity"
            )
        assert_compatible_outcome_lineages(trusted, incoming)
        if len(incoming.revisions) < len(trusted.revisions):
            raise OutcomeLineageTrustError(
                "cannot bind product availability from a stale outcome lineage prefix"
            )
        trusted_times = [
            revision.first_available_at for revision in trusted.revisions
        ]
        if any(value is not None for value in trusted_times) and not all(
            value is not None for value in trusted_times
        ):
            raise OutcomeLineageTrustError(
                "trusted outcome lineage has incomplete product availability evidence"
            )
        if (
            len(incoming.revisions) > len(trusted.revisions)
            and trusted_times
            and trusted_times[-1] is not None
        ):
            assert trusted_times[-1] is not None
            if accepted_dt < _parse_timestamp(trusted_times[-1]):
                raise OutcomeLineageTrustError(
                    "product acceptance time predates already trusted outcome availability"
                )

    revisions: list[TrustedOutcomeRevision] = []
    for index, revision in enumerate(incoming.revisions):
        established = (
            trusted.revisions[index].first_available_at
            if trusted is not None
            and index < len(trusted.revisions)
            and trusted.revisions[index].first_available_at is not None
            else product_available_at
        )
        revisions.append(
            TrustedOutcomeRevision(
                revision=revision.revision,
                revision_id=revision.revision_id,
                record_sha256=revision.record_sha256,
                first_available_at=established,
            )
        )
    return OutcomeLineageBinding(
        source_identity=incoming.source_identity,
        record_id=incoming.record_id,
        root_revision_id=incoming.root_revision_id,
        root_record_sha256=incoming.root_record_sha256,
        revisions=tuple(revisions),
    )


def resolve_outcome_revision_as_of(
    binding: OutcomeLineageBinding,
    cutoff: str,
) -> TrustedOutcomeRevision | None:
    """Return the latest product-trusted revision genuinely available by cutoff."""

    cutoff_dt = _parse_timestamp(
        _canonical_timestamp(cutoff, field="outcome as-of cutoff")
    )
    resolved: TrustedOutcomeRevision | None = None
    previous_available: datetime | None = None
    for revision in binding.revisions:
        if revision.first_available_at is None:
            if any(item.first_available_at is not None for item in binding.revisions):
                raise OutcomeLineageTrustError(
                    "outcome lineage has incomplete product availability evidence"
                )
            return None
        available_dt = _parse_timestamp(revision.first_available_at)
        if previous_available is not None and available_dt < previous_available:
            raise OutcomeLineageTrustError(
                "outcome lineage product availability moves backwards"
            )
        previous_available = available_dt
        if available_dt <= cutoff_dt:
            resolved = revision
    return resolved


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeLineageTrustError("outcome timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeLineageTrustError("outcome timestamp must include an explicit timezone")
    return parsed


def _canonical_timestamp(value: object, *, field: str) -> str:
    text = _canonical_text(value, field=field)
    parsed = _parse_timestamp(text)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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
    except RecursionError as exc:
        raise OutcomeLineageTrustError(f"{context} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise OutcomeLineageTrustError(f"{context} must be a JSON object")
    return raw


def _canonical_text(value: object, *, field: str) -> str:
    # Authority-bearing text must not retain caller-defined str dispatch.
    # Exact JSON/product strings are ordinary built-ins; accepting a subclass
    # lets strip/replace/hash/equality change meaning after validation.
    if type(value) is not str or not value or value != value.strip():
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
