from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence


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
    authority_record_file: str
    governance_proof_bytes: bytes = field(repr=False)
    authority_record_bytes: bytes = field(repr=False)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path, *, context: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{context} is not readable: {path}") from exc


def _object_bytes(payload: bytes, *, context: str, path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    """Verify that rights/retention claims are bound to the exact evidence bytes consumed.

    This is a provenance/integrity gate, not a legal opinion. The proof and authority
    record are each read exactly once; parsing, claim checks and digests all derive from
    those same byte snapshots so a path replacement cannot make hashing and parsing
    observe different content.
    """

    proof_path = Path(governance_proof_path)
    proof_bytes = _read_bytes(proof_path, context="governance proof")
    proof = _object_bytes(proof_bytes, context="governance proof", path=proof_path)
    proof_sha256 = _sha256_bytes(proof_bytes)
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
    authority_bytes = _read_bytes(authority_path, context="governance authority record")
    actual_authority_sha256 = _sha256_bytes(authority_bytes)
    if actual_authority_sha256 != authority_record_sha256:
        raise ValueError(
            "governance proof.authority_record_sha256 does not match authority evidence artifact"
        )

    authority = _object_bytes(
        authority_bytes,
        context="governance authority record",
        path=authority_path,
    )
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

    for bound_field in _BOUND_FIELDS:
        if bound_field == "source_ids":
            continue
        if (bound_field in proof) != (bound_field in authority):
            raise ValueError(
                f"governance proof.{bound_field} presence does not match authority evidence artifact"
            )
        if bound_field in proof and proof[bound_field] != authority[bound_field]:
            raise ValueError(
                f"governance proof.{bound_field} does not match authority evidence artifact"
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
        governance_proof_sha256=proof_sha256,
        authority_record=str(authority_path),
        authority_record_sha256=authority_record_sha256,
        evidence_reference=evidence_reference,
        verification_method=verification_method,
        recorded_by=recorded_by,
        authority_record_file=authority_record_file,
        governance_proof_bytes=proof_bytes,
        authority_record_bytes=authority_bytes,
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


def _replace_argument_value(argv: Sequence[str], flag: str, replacement: str) -> list[str]:
    prefix = f"{flag}="
    forwarded: list[str] = []
    index = 0
    replaced = False
    while index < len(argv):
        value = argv[index]
        if value == flag:
            if index + 1 >= len(argv):
                raise ValueError(f"{flag} requires a value")
            if replaced:
                raise ValueError(f"{flag} must be provided exactly once")
            forwarded.extend((flag, replacement))
            replaced = True
            index += 2
            continue
        if value.startswith(prefix):
            if replaced:
                raise ValueError(f"{flag} must be provided exactly once")
            forwarded.append(f"{prefix}{replacement}")
            replaced = True
            index += 1
            continue
        forwarded.append(value)
        index += 1
    if not replaced:
        raise ValueError(f"{flag} is required before rights provenance can be verified")
    return forwarded


def _help_requested(argv: Sequence[str]) -> bool:
    return any(value in {"-h", "--help"} for value in argv)


def _require_bound_governance(argv: Sequence[str]) -> GovernanceAuthorityBinding:
    proof = _argument_value(argv, "--governance-proof")
    if proof is None:
        raise ValueError("--governance-proof is required before rights provenance can be verified")
    return verify_governance_authority_binding(proof)


@contextmanager
def _frozen_governance_args(
    argv: Sequence[str],
    binding: GovernanceAuthorityBinding,
) -> Iterator[list[str]]:
    """Delegate only a private snapshot of the exact bytes that passed verification.

    The canonical assemblers may reopen their input path, but that path is no longer the
    caller-controlled source path. It is a private temporary copy written from the exact
    proof/authority byte snapshots used for digest and claim verification above. Replacing
    the original files after verification therefore cannot change what the assembler parses
    or hashes. The snapshot digests are checked before and after canonical assembly.
    """

    with tempfile.TemporaryDirectory(prefix="autosport-governance-") as tmp:
        root = Path(tmp)
        proof_path = root / "governance-proof.json"
        authority_path = root / binding.authority_record_file
        if authority_path == proof_path:
            raise ValueError("authority record file must differ from governance proof file")
        proof_path.write_bytes(binding.governance_proof_bytes)
        authority_path.write_bytes(binding.authority_record_bytes)

        if _sha256_bytes(proof_path.read_bytes()) != binding.governance_proof_sha256:
            raise ValueError("frozen governance proof bytes do not match verified digest")
        if _sha256_bytes(authority_path.read_bytes()) != binding.authority_record_sha256:
            raise ValueError("frozen governance authority bytes do not match verified digest")

        frozen = _replace_argument_value(
            argv,
            "--governance-proof",
            str(proof_path),
        )
        yield frozen

        if _sha256_bytes(proof_path.read_bytes()) != binding.governance_proof_sha256:
            raise ValueError("frozen governance proof changed during canonical assembly")
        if _sha256_bytes(authority_path.read_bytes()) != binding.authority_record_sha256:
            raise ValueError("frozen governance authority changed during canonical assembly")


def corpus_main(argv: list[str] | None = None) -> int:
    from .historical_corpus import main as canonical_main

    forwarded = list(sys.argv[1:] if argv is None else argv)
    if _help_requested(forwarded):
        return canonical_main(forwarded)
    try:
        binding = _require_bound_governance(forwarded)
        with _frozen_governance_args(forwarded, binding) as frozen:
            return canonical_main(frozen)
    except (ValueError, OSError) as exc:
        print(f"historical_corpus=FAIL_CLOSED error={exc}")
        return 3


def bundle_corpus_main(argv: list[str] | None = None) -> int:
    from .historical_bundle_corpus import main as canonical_main

    forwarded = list(sys.argv[1:] if argv is None else argv)
    if _help_requested(forwarded):
        return canonical_main(forwarded)
    try:
        binding = _require_bound_governance(forwarded)
        with _frozen_governance_args(forwarded, binding) as frozen:
            return canonical_main(frozen)
    except (ValueError, OSError) as exc:
        print(f"historical_bundle_corpus=FAIL_CLOSED error={exc}")
        return 3
