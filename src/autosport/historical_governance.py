from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


_AUTHORITY_RECORD_KIND = "historical_corpus_governance_authority_record"
_GOVERNANCE_PROOF_KIND = "historical_corpus_governance_proof"
_BOUND_FIELDS = (
    "source_identity",
    "source_ids",
    "terms_reference",
    "retention_basis",
    "retention_expires_at",
    "retention_extension_authority_reference",
    "authority_reference",
    "verified_at",
    "redistribution_policy",
    "redistribution_verified",
    "licensing_or_retention_verified",
)


@dataclass(frozen=True, slots=True)
class GovernanceAuthorityBinding:
    governance_proof: str
    governance_proof_sha256: str
    authority_record: str
    authority_record_sha256: str
    evidence_reference: str
    verification_method: str
    recorded_by: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(path: Path, *, context: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} is not readable valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a JSON object")
    return raw


def _text(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _digest(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = _text(raw, key, context=context)
    if (
        len(value) != 64
        or value != value.lower()
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{context}.{key} must be a canonical lowercase SHA-256 hex digest")
    return value


def _direct_sibling(root: Path, value: str, *, field: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or len(relative.parts) != 1 or value in {".", ".."}:
        raise ValueError(f"{field} must name one direct sibling artifact")
    candidate = root / relative
    if not candidate.is_file():
        raise ValueError(f"{field} does not resolve to a regular sibling artifact")
    return candidate


def _normalized_source_ids(raw: Any, *, context: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{context}.source_ids must be a non-empty list")
    values = tuple(sorted(str(value).strip() for value in raw))
    if any(not value for value in values) or len(set(values)) != len(values):
        raise ValueError(f"{context}.source_ids must contain unique non-empty strings")
    return values


def verify_governance_authority_binding(
    governance_proof_path: str | Path,
) -> GovernanceAuthorityBinding:
    """Verify that a rights/retention claim is content-bound to a sibling evidence record.

    This is a provenance/integrity gate, not a legal opinion. It prevents the canonical
    product entrypoints from accepting a bare governance JSON that simply flips
    ``licensing_or_retention_verified`` to true with unbound free-text references.
    The authority record remains external input and must represent real, lawful evidence.
    """

    proof_path = Path(governance_proof_path)
    proof = _object(proof_path, context="governance proof")
    if int(proof.get("schema_version", 0)) != 1:
        raise ValueError("governance proof schema_version must be 1")
    if proof.get("kind") != _GOVERNANCE_PROOF_KIND:
        raise ValueError(f"governance proof kind must be {_GOVERNANCE_PROOF_KIND}")
    if proof.get("licensing_or_retention_verified") is not True:
        raise ValueError("governance proof must explicitly set licensing_or_retention_verified=true")

    authority_record_file = _text(
        proof,
        "authority_record_file",
        context="governance proof",
    )
    authority_record_sha256 = _digest(
        proof,
        "authority_record_sha256",
        context="governance proof",
    )
    authority_path = _direct_sibling(
        proof_path.parent,
        authority_record_file,
        field="governance proof.authority_record_file",
    )
    actual_authority_sha256 = _sha256(authority_path)
    if actual_authority_sha256 != authority_record_sha256:
        raise ValueError(
            "governance proof.authority_record_sha256 does not match authority evidence artifact"
        )

    authority = _object(authority_path, context="governance authority record")
    if int(authority.get("schema_version", 0)) != 1:
        raise ValueError("governance authority record schema_version must be 1")
    if authority.get("kind") != _AUTHORITY_RECORD_KIND:
        raise ValueError(f"governance authority record kind must be {_AUTHORITY_RECORD_KIND}")
    if authority.get("licensing_or_retention_verified") is not True:
        raise ValueError(
            "governance authority record must explicitly set licensing_or_retention_verified=true"
        )

    proof_source_ids = _normalized_source_ids(proof.get("source_ids"), context="governance proof")
    authority_source_ids = _normalized_source_ids(
        authority.get("source_ids"),
        context="governance authority record",
    )
    if proof_source_ids != authority_source_ids:
        raise ValueError("governance proof source_ids do not match authority evidence artifact")

    for field in _BOUND_FIELDS:
        if field == "source_ids":
            continue
        if proof.get(field) != authority.get(field):
            raise ValueError(
                f"governance proof.{field} does not match authority evidence artifact"
            )

    evidence_reference = _text(
        authority,
        "evidence_reference",
        context="governance authority record",
    )
    verification_method = _text(
        authority,
        "verification_method",
        context="governance authority record",
    )
    recorded_by = _text(
        authority,
        "recorded_by",
        context="governance authority record",
    )

    return GovernanceAuthorityBinding(
        governance_proof=str(proof_path),
        governance_proof_sha256=_sha256(proof_path),
        authority_record=str(authority_path),
        authority_record_sha256=authority_record_sha256,
        evidence_reference=evidence_reference,
        verification_method=verification_method,
        recorded_by=recorded_by,
    )


def _argument_value(argv: Sequence[str], flag: str) -> str | None:
    values: list[str] = []
    prefix = f"{flag}="
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == flag:
            if index + 1 >= len(argv):
                raise ValueError(f"{flag} requires a value")
            values.append(argv[index + 1])
            index += 2
            continue
        if value.startswith(prefix):
            values.append(value[len(prefix) :])
        index += 1

    if len(values) > 1:
        raise ValueError(f"{flag} must be provided exactly once")
    if not values:
        return None
    if not values[0]:
        raise ValueError(f"{flag} requires a non-empty value")
    return values[0]


def _help_requested(argv: Sequence[str]) -> bool:
    return any(value in {"-h", "--help"} for value in argv)


def _require_bound_governance(argv: Sequence[str]) -> GovernanceAuthorityBinding:
    proof = _argument_value(argv, "--governance-proof")
    if proof is None:
        raise ValueError("--governance-proof is required before rights provenance can be verified")
    return verify_governance_authority_binding(proof)


def corpus_main(argv: list[str] | None = None) -> int:
    from .historical_corpus import main as canonical_main

    forwarded = list(sys.argv[1:] if argv is None else argv)
    if _help_requested(forwarded):
        return canonical_main(forwarded)
    try:
        _require_bound_governance(forwarded)
    except (ValueError, OSError) as exc:
        print(f"historical_corpus=FAIL_CLOSED error={exc}")
        return 3
    return canonical_main(forwarded)


def bundle_corpus_main(argv: list[str] | None = None) -> int:
    from .historical_bundle_corpus import main as canonical_main

    forwarded = list(sys.argv[1:] if argv is None else argv)
    if _help_requested(forwarded):
        return canonical_main(forwarded)
    try:
        _require_bound_governance(forwarded)
    except (ValueError, OSError) as exc:
        print(f"historical_bundle_corpus=FAIL_CLOSED error={exc}")
        return 3
    return canonical_main(forwarded)
