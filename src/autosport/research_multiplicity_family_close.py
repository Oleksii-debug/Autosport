from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .research_multiplicity import (
    SequentialDecision,
    SequentialMultiplicityEvidenceStore,
)
from .workspace_lock import WorkspaceEconomicLock


_HEX = frozenset("0123456789abcdef")


class MultiplicityFamilyCloseError(ValueError):
    """The frozen multiplicity family cannot produce verified close evidence."""


_CANONICAL_STORE_TYPE = SequentialMultiplicityEvidenceStore
_CANONICAL_STORE_READ_STATE_DESCRIPTOR = vars(_CANONICAL_STORE_TYPE).get("_read_state")
if not callable(_CANONICAL_STORE_READ_STATE_DESCRIPTOR):
    raise RuntimeError("canonical multiplicity store _read_state is unavailable")
_CANONICAL_STORE_READ_STATE = _CANONICAL_STORE_READ_STATE_DESCRIPTOR
_CANONICAL_STORE_READ_STATE_CODE = getattr(
    _CANONICAL_STORE_READ_STATE,
    "__code__",
    None,
)
if _CANONICAL_STORE_READ_STATE_CODE is None:
    raise RuntimeError("canonical multiplicity store _read_state code is unavailable")

_CANONICAL_STORE_VALIDATE_ENROLLMENT_DESCRIPTOR = vars(_CANONICAL_STORE_TYPE).get(
    "_validate_workspace_enrollment"
)
if not callable(_CANONICAL_STORE_VALIDATE_ENROLLMENT_DESCRIPTOR):
    raise RuntimeError(
        "canonical multiplicity store _validate_workspace_enrollment is unavailable"
    )
_CANONICAL_STORE_VALIDATE_ENROLLMENT = (
    _CANONICAL_STORE_VALIDATE_ENROLLMENT_DESCRIPTOR
)
_CANONICAL_STORE_VALIDATE_ENROLLMENT_CODE = getattr(
    _CANONICAL_STORE_VALIDATE_ENROLLMENT,
    "__code__",
    None,
)
if _CANONICAL_STORE_VALIDATE_ENROLLMENT_CODE is None:
    raise RuntimeError(
        "canonical multiplicity store _validate_workspace_enrollment code is unavailable"
    )


def _require_canonical_store_read_dispatch() -> None:
    if SequentialMultiplicityEvidenceStore is not _CANONICAL_STORE_TYPE:
        raise MultiplicityFamilyCloseError(
            "canonical multiplicity store type binding changed"
        )
    current = vars(_CANONICAL_STORE_TYPE).get("_read_state")
    if (
        current is not _CANONICAL_STORE_READ_STATE_DESCRIPTOR
        or getattr(current, "__code__", None) is not _CANONICAL_STORE_READ_STATE_CODE
    ):
        raise MultiplicityFamilyCloseError(
            "canonical multiplicity store read dispatch changed"
        )
    current_validate = vars(_CANONICAL_STORE_TYPE).get(
        "_validate_workspace_enrollment"
    )
    if (
        current_validate is not _CANONICAL_STORE_VALIDATE_ENROLLMENT_DESCRIPTOR
        or getattr(current_validate, "__code__", None)
        is not _CANONICAL_STORE_VALIDATE_ENROLLMENT_CODE
    ):
        raise MultiplicityFamilyCloseError(
            "canonical multiplicity store enrollment dispatch changed"
        )


def _read_canonical_store_state(
    store: SequentialMultiplicityEvidenceStore,
) -> dict[str, Any]:
    _require_canonical_store_read_dispatch()
    loaded = _CANONICAL_STORE_READ_STATE(store)
    _require_canonical_store_read_dispatch()
    _CANONICAL_STORE_VALIDATE_ENROLLMENT(store, loaded["plan"])
    _require_canonical_store_read_dispatch()
    return loaded


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


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")
    return value


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


@dataclass(frozen=True, slots=True)
class TerminalMultiplicityMemberEvidence:
    member_authority_id: str
    hypothesis_id: str
    evidence_sha256: str
    decision: SequentialDecision
    look_index: int
    observed_at: str

    def __post_init__(self) -> None:
        _sha256(self.member_authority_id, "member_authority_id")
        _text(self.hypothesis_id, "hypothesis_id")
        _sha256(self.evidence_sha256, "evidence_sha256")
        if not isinstance(self.decision, SequentialDecision):
            raise ValueError("decision must be a SequentialDecision")
        if self.decision is SequentialDecision.CONTINUE:
            raise ValueError("terminal member evidence cannot carry CONTINUE")
        _positive_int(self.look_index, "look_index")
        _text(self.observed_at, "observed_at")

    def to_payload(self) -> dict[str, Any]:
        return {
            "member_authority_id": self.member_authority_id.lower(),
            "hypothesis_id": self.hypothesis_id,
            "evidence_sha256": self.evidence_sha256.lower(),
            "decision": self.decision.value,
            "look_index": self.look_index,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TerminalMultiplicityMemberEvidence":
        if type(payload) is not dict or set(payload) != {
            "member_authority_id",
            "hypothesis_id",
            "evidence_sha256",
            "decision",
            "look_index",
            "observed_at",
        }:
            raise ValueError("terminal multiplicity member payload fields mismatch")
        try:
            decision = SequentialDecision(payload["decision"])
        except (TypeError, ValueError) as exc:
            raise ValueError("terminal multiplicity member decision is invalid") from exc
        return cls(
            member_authority_id=payload["member_authority_id"],
            hypothesis_id=payload["hypothesis_id"],
            evidence_sha256=payload["evidence_sha256"],
            decision=decision,
            look_index=payload["look_index"],
            observed_at=payload["observed_at"],
        )


@dataclass(frozen=True, slots=True)
class MultiplicityFamilyCloseEvidence:
    family_plan_sha256: str
    family_id: str
    research_protocol_id: str
    protocol_sha256: str
    research_question_id: str
    method_id: str
    journal_state_sha256: str
    record_count: int
    terminal_members: tuple[TerminalMultiplicityMemberEvidence, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        _sha256(self.family_plan_sha256, "family_plan_sha256")
        for name in (
            "family_id",
            "research_protocol_id",
            "research_question_id",
            "method_id",
        ):
            _text(getattr(self, name), name)
        _sha256(self.protocol_sha256, "protocol_sha256")
        _sha256(self.journal_state_sha256, "journal_state_sha256")
        _positive_int(self.record_count, "record_count")
        if type(self.terminal_members) is not tuple or not self.terminal_members:
            raise ValueError("terminal_members must be a non-empty tuple")
        if any(
            not isinstance(member, TerminalMultiplicityMemberEvidence)
            for member in self.terminal_members
        ):
            raise ValueError(
                "terminal_members must contain TerminalMultiplicityMemberEvidence values"
            )
        member_ids = tuple(member.member_authority_id for member in self.terminal_members)
        if member_ids != tuple(sorted(member_ids)):
            raise ValueError("terminal_members must use canonical member-authority order")
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("terminal_members cannot contain duplicates")

    def authority_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "family_plan_sha256": self.family_plan_sha256.lower(),
            "family_id": self.family_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "research_question_id": self.research_question_id,
            "method_id": self.method_id,
            "journal_state_sha256": self.journal_state_sha256.lower(),
            "record_count": self.record_count,
            "terminal_members": [
                member.to_payload() for member in self.terminal_members
            ],
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.authority_payload())

    @property
    def promotion_authorized(self) -> bool:
        """Family closure is evidence completeness, never promotion authority."""
        return False

    def to_payload(self) -> dict[str, Any]:
        return {**self.authority_payload(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def from_payload(cls, payload: object) -> "MultiplicityFamilyCloseEvidence":
        required = {
            "schema_version",
            "family_plan_sha256",
            "family_id",
            "research_protocol_id",
            "protocol_sha256",
            "research_question_id",
            "method_id",
            "journal_state_sha256",
            "record_count",
            "terminal_members",
            "evidence_sha256",
        }
        if type(payload) is not dict or set(payload) != required:
            raise ValueError("multiplicity family-close payload fields mismatch")
        if type(payload["terminal_members"]) is not list:
            raise ValueError("terminal_members payload must be a list")
        evidence = cls(
            schema_version=payload["schema_version"],
            family_plan_sha256=payload["family_plan_sha256"],
            family_id=payload["family_id"],
            research_protocol_id=payload["research_protocol_id"],
            protocol_sha256=payload["protocol_sha256"],
            research_question_id=payload["research_question_id"],
            method_id=payload["method_id"],
            journal_state_sha256=payload["journal_state_sha256"],
            record_count=payload["record_count"],
            terminal_members=tuple(
                TerminalMultiplicityMemberEvidence.from_payload(member)
                for member in payload["terminal_members"]
            ),
        )
        if payload["evidence_sha256"] != evidence.evidence_sha256:
            raise ValueError("multiplicity family-close evidence identity mismatch")
        return evidence


def _derive_locked(
    store: SequentialMultiplicityEvidenceStore,
) -> MultiplicityFamilyCloseEvidence:
    loaded = _read_canonical_store_state(store)
    plan = loaded["plan"]
    by_member = loaded["by_member"]
    declared_members = tuple(
        sorted(plan.members, key=lambda member: member.member_authority_id)
    )
    declared_ids = {member.member_authority_id for member in declared_members}
    if set(by_member) - declared_ids:
        raise MultiplicityFamilyCloseError(
            "multiplicity store contains evidence for an undeclared family member"
        )

    terminal_members: list[TerminalMultiplicityMemberEvidence] = []
    for member in declared_members:
        history = by_member.get(member.member_authority_id, [])
        if not history:
            raise MultiplicityFamilyCloseError(
                f"family member {member.member_authority_id} has no durable evidence"
            )
        final = history[-1]
        if not final.terminal:
            raise MultiplicityFamilyCloseError(
                f"family member {member.member_authority_id} is not terminal"
            )
        terminal_members.append(
            TerminalMultiplicityMemberEvidence(
                member_authority_id=member.member_authority_id,
                hypothesis_id=member.hypothesis_id,
                evidence_sha256=final.evidence.evidence_sha256,
                decision=final.decision,
                look_index=final.evidence.look_index,
                observed_at=final.evidence.observed_at,
            )
        )

    records = loaded["state"]["records"]
    record_count = len(records)
    if record_count < len(declared_members):
        raise MultiplicityFamilyCloseError(
            "multiplicity family record count cannot cover every declared member"
        )
    journal_state_sha256 = _digest(
        {
            "family_plan_sha256": plan.plan_sha256,
            "record_evidence_sha256": [
                _sha256(record["evidence_sha256"], "record evidence_sha256")
                for record in records
            ],
        }
    )
    return MultiplicityFamilyCloseEvidence(
        family_plan_sha256=plan.plan_sha256,
        family_id=plan.family_id,
        research_protocol_id=plan.research_protocol_id,
        protocol_sha256=plan.protocol_sha256,
        research_question_id=plan.research_question_id,
        method_id=plan.method_id,
        journal_state_sha256=journal_state_sha256,
        record_count=record_count,
        terminal_members=tuple(terminal_members),
    )


def _canonical_location(
    store: SequentialMultiplicityEvidenceStore,
) -> tuple[Path, Path]:
    if type(store) is not _CANONICAL_STORE_TYPE:
        raise TypeError("store must be the canonical SequentialMultiplicityEvidenceStore")
    path = Path(store.path).resolve(strict=False)
    workspace_root = Path(store.workspace_root).resolve(strict=False)
    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise MultiplicityFamilyCloseError(
            "multiplicity store path is outside its canonical workspace"
        ) from exc
    return path, workspace_root


def derive_multiplicity_family_close(
    store: SequentialMultiplicityEvidenceStore,
) -> MultiplicityFamilyCloseEvidence:
    """Derive close evidence only from a complete canonical multiplicity store.

    The returned bundle is deliberately non-authorizing: consumers that rely on it
    must call 'require_current_multiplicity_family_close' against the canonical
    store. A caller-created or stale bundle never substitutes for store re-resolution.
    """

    path, workspace_root = _canonical_location(store)
    with WorkspaceEconomicLock(workspace_root):
        _require_canonical_store_read_dispatch()
        canonical = _CANONICAL_STORE_TYPE(
            path,
            workspace_root=workspace_root,
        )
        _require_canonical_store_read_dispatch()
        return _derive_locked(canonical)


def require_current_multiplicity_family_close(
    store: SequentialMultiplicityEvidenceStore,
    evidence: MultiplicityFamilyCloseEvidence,
) -> MultiplicityFamilyCloseEvidence:
    """Re-resolve the canonical store and reject stale/caller-forged close evidence."""

    if type(evidence) is not MultiplicityFamilyCloseEvidence:
        raise TypeError("evidence must be MultiplicityFamilyCloseEvidence")
    path, workspace_root = _canonical_location(store)
    with WorkspaceEconomicLock(workspace_root):
        _require_canonical_store_read_dispatch()
        canonical = _CANONICAL_STORE_TYPE(
            path,
            workspace_root=workspace_root,
        )
        _require_canonical_store_read_dispatch()
        current = _derive_locked(canonical)
    if current != evidence or current.evidence_sha256 != evidence.evidence_sha256:
        raise MultiplicityFamilyCloseError(
            "family-close evidence does not match the current canonical multiplicity store"
        )
    return current
