"""Exact denominator-context binding for pre-evaluation evidence.

This companion layer keeps the existing pre-evaluation decision derivation intact while
binding each slot to the authoritative denominator context and provider member identity
that downstream evaluation-universe construction must consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Iterable, Mapping

from .decision_ledger import JsonlDecisionLedger
from .pre_evaluation_evidence import (
    PreEvaluationEvidenceStore,
    PreEvaluationSessionEvidence,
)


SCHEMA_VERSION = 1
AUTHORITY_FAMILY = "research.pre-evaluation-denominator-binding-v1"
_HEX = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _sha(name: str, value: object) -> str:
    raw = _text(name, value).lower()
    if len(raw) != 64 or any(ch not in _HEX for ch in raw):
        raise ValueError(f"{name} must be canonical SHA-256 hex")
    return raw


@dataclass(frozen=True, slots=True)
class PreEvaluationDenominatorContext:
    session_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    provider_evidence_sha256: str

    def __post_init__(self) -> None:
        _text("session_id", self.session_id)
        _text("campaign_id", self.campaign_id)
        _text("research_protocol_id", self.research_protocol_id)
        object.__setattr__(
            self, "protocol_sha256", _sha("protocol_sha256", self.protocol_sha256)
        )
        object.__setattr__(
            self,
            "provider_evidence_sha256",
            _sha("provider_evidence_sha256", self.provider_evidence_sha256),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "provider_evidence_sha256": self.provider_evidence_sha256,
        }

    @property
    def digest(self) -> str:
        return _digest(
            {
                "authority_family": AUTHORITY_FAMILY,
                "schema_version": SCHEMA_VERSION,
                **self.to_payload(),
            }
        )

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "PreEvaluationDenominatorContext":
        expected = {
            "session_id",
            "campaign_id",
            "research_protocol_id",
            "protocol_sha256",
            "provider_evidence_sha256",
        }
        if set(payload) != expected:
            raise ValueError("denominator context payload has unexpected fields")
        return cls(
            session_id=_text("session_id", payload["session_id"]),
            campaign_id=_text("campaign_id", payload["campaign_id"]),
            research_protocol_id=_text(
                "research_protocol_id", payload["research_protocol_id"]
            ),
            protocol_sha256=_sha("protocol_sha256", payload["protocol_sha256"]),
            provider_evidence_sha256=_sha(
                "provider_evidence_sha256", payload["provider_evidence_sha256"]
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderMemberIdentity:
    row_key: str
    member_sha256: str

    def __post_init__(self) -> None:
        _text("row_key", self.row_key)
        object.__setattr__(
            self, "member_sha256", _sha("member_sha256", self.member_sha256)
        )

    def to_payload(self) -> dict[str, str]:
        return {"row_key": self.row_key, "member_sha256": self.member_sha256}


@dataclass(frozen=True, slots=True)
class BoundPreEvaluationMember:
    row_key: str
    member_sha256: str
    slot_evidence_digest: str

    def __post_init__(self) -> None:
        _text("row_key", self.row_key)
        object.__setattr__(
            self, "member_sha256", _sha("member_sha256", self.member_sha256)
        )
        object.__setattr__(
            self,
            "slot_evidence_digest",
            _sha("slot_evidence_digest", self.slot_evidence_digest),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "row_key": self.row_key,
            "member_sha256": self.member_sha256,
            "slot_evidence_digest": self.slot_evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class BoundPreEvaluationSession:
    context: PreEvaluationDenominatorContext
    evidence: PreEvaluationSessionEvidence
    members: tuple[BoundPreEvaluationMember, ...]
    decision_ledger_prefix_sha256: str | None = None
    decision_ledger_prefix_record_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, PreEvaluationDenominatorContext):
            raise TypeError("context must be PreEvaluationDenominatorContext")
        if not isinstance(self.evidence, PreEvaluationSessionEvidence):
            raise TypeError("evidence must be PreEvaluationSessionEvidence")
        if self.evidence.session_id != self.context.session_id:
            raise ValueError("session evidence is bound to a different session")

        prefix_sha = self.decision_ledger_prefix_sha256
        prefix_count = self.decision_ledger_prefix_record_count
        if (prefix_sha is None) != (prefix_count is None):
            raise ValueError(
                "decision-ledger prefix sha256 and record_count must be supplied together"
            )
        if prefix_sha is not None:
            object.__setattr__(
                self,
                "decision_ledger_prefix_sha256",
                _sha("decision_ledger_prefix_sha256", prefix_sha),
            )
            if type(prefix_count) is not int or prefix_count < 0:
                raise ValueError(
                    "decision_ledger_prefix_record_count must be a non-negative integer"
                )

        row_keys = tuple(member.row_key for member in self.members)
        if row_keys != tuple(sorted(row_keys)):
            raise ValueError("bound members must be sorted by row_key")
        if len(row_keys) != len(set(row_keys)):
            raise ValueError("duplicate row_key in bound pre-evaluation session")

        slot_by_key = {slot.candidate_id: slot for slot in self.evidence.slots}
        if len(slot_by_key) != len(self.evidence.slots):
            raise ValueError("duplicate candidate_id in pre-evaluation session")
        if set(row_keys) != set(slot_by_key):
            raise ValueError(
                "provider member set must exactly equal pre-evaluation slot membership"
            )
        for member in self.members:
            if member.slot_evidence_digest != slot_by_key[member.row_key].evidence_digest:
                raise ValueError(
                    f"slot evidence digest mismatch for row_key {member.row_key}"
                )

    def _ledger_prefix_payload(self) -> dict[str, object] | None:
        if self.decision_ledger_prefix_sha256 is None:
            return None
        return {
            "sha256": self.decision_ledger_prefix_sha256,
            "record_count": self.decision_ledger_prefix_record_count,
        }

    @property
    def authority_digest(self) -> str:
        return _digest(self.to_payload())

    @property
    def authority_id(self) -> str:
        return (
            f"pre-evaluation-binding:{self.context.session_id}:"
            f"{self.authority_digest[:24]}"
        )

    def member_authority_digest(self, row_key: str) -> str:
        _text("row_key", row_key)
        member = next((item for item in self.members if item.row_key == row_key), None)
        if member is None:
            raise KeyError(row_key)
        payload: dict[str, object] = {
            "authority_family": AUTHORITY_FAMILY,
            "schema_version": SCHEMA_VERSION,
            "context_digest": self.context.digest,
            "session_authority_digest": self.evidence.authority_digest,
            **member.to_payload(),
        }
        ledger_prefix = self._ledger_prefix_payload()
        if ledger_prefix is not None:
            payload["decision_ledger_prefix"] = ledger_prefix
        return _digest(payload)

    @property
    def member_authority_sha256(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (member.row_key, self.member_authority_digest(member.row_key))
            for member in self.members
        )

    def resolve_slot(self, row_key: str):
        _text("row_key", row_key)
        for slot in self.evidence.slots:
            if slot.candidate_id == row_key:
                return slot
        raise KeyError(row_key)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "context": self.context.to_payload(),
            "context_digest": self.context.digest,
            "session_authority_id": self.evidence.authority_id,
            "session_authority_digest": self.evidence.authority_digest,
            "session_evidence": self.evidence.to_payload(),
            "members": [member.to_payload() for member in self.members],
            "member_authority_sha256": [
                [row_key, digest] for row_key, digest in self.member_authority_sha256
            ],
        }
        ledger_prefix = self._ledger_prefix_payload()
        if ledger_prefix is not None:
            payload["decision_ledger_prefix"] = ledger_prefix
        return payload


def bind_pre_evaluation_session(
    evidence: PreEvaluationSessionEvidence,
    *,
    context: PreEvaluationDenominatorContext,
    provider_members: Iterable[ProviderMemberIdentity],
    ledger: JsonlDecisionLedger | None = None,
) -> BoundPreEvaluationSession:
    if not isinstance(evidence, PreEvaluationSessionEvidence):
        raise TypeError("evidence must be PreEvaluationSessionEvidence")
    if not isinstance(context, PreEvaluationDenominatorContext):
        raise TypeError("context must be PreEvaluationDenominatorContext")
    if ledger is not None and not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger or None")

    member_by_key: dict[str, ProviderMemberIdentity] = {}
    for member in provider_members:
        if not isinstance(member, ProviderMemberIdentity):
            raise TypeError("provider_members must contain ProviderMemberIdentity values")
        if member.row_key in member_by_key:
            raise ValueError(f"duplicate provider row_key: {member.row_key}")
        member_by_key[member.row_key] = member

    slot_by_key = {slot.candidate_id: slot for slot in evidence.slots}
    if set(member_by_key) != set(slot_by_key):
        raise ValueError(
            "provider member set must exactly equal pre-evaluation slot membership"
        )

    members = tuple(
        BoundPreEvaluationMember(
            row_key=row_key,
            member_sha256=member_by_key[row_key].member_sha256,
            slot_evidence_digest=slot_by_key[row_key].evidence_digest,
        )
        for row_key in sorted(member_by_key)
    )
    prefix_sha256 = None
    prefix_record_count = None
    if ledger is not None:
        prefix = ledger.verified_snapshot()
        prefix_sha256 = prefix.sha256
        prefix_record_count = prefix.record_count
    return BoundPreEvaluationSession(
        context=context,
        evidence=evidence,
        members=members,
        decision_ledger_prefix_sha256=prefix_sha256,
        decision_ledger_prefix_record_count=prefix_record_count,
    )


class BoundPreEvaluationEvidenceStore:
    """Immutable durable store for exact denominator-bound pre-evaluation evidence."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, bound: BoundPreEvaluationSession) -> None:
        if not isinstance(bound, BoundPreEvaluationSession):
            raise TypeError("bound must be BoundPreEvaluationSession")
        if self._path.exists():
            current = self.load()
            if current.authority_digest == bound.authority_digest:
                return
            raise ValueError(
                "pre-evaluation binding already exists with conflicting authority"
            )

        envelope = {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "authority_id": bound.authority_id,
            "authority_digest": bound.authority_digest,
            "payload": bound.to_payload(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(_canonical_json(envelope) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)

    def load(self) -> BoundPreEvaluationSession:
        try:
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid pre-evaluation binding file") from exc
        if type(envelope) is not dict:
            raise ValueError("pre-evaluation binding envelope must be an object")
        expected = {
            "schema_version",
            "authority_family",
            "authority_id",
            "authority_digest",
            "payload",
        }
        if set(envelope) != expected:
            raise ValueError("pre-evaluation binding envelope has unexpected fields")
        if envelope["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported pre-evaluation binding schema")
        if envelope["authority_family"] != AUTHORITY_FAMILY:
            raise ValueError("unexpected pre-evaluation binding authority family")
        payload = envelope["payload"]
        if type(payload) is not dict:
            raise ValueError("pre-evaluation binding payload must be an object")
        bound = self._from_payload(payload)
        if bound.to_payload() != payload:
            raise ValueError("pre-evaluation binding semantic replay mismatch")
        if envelope["authority_digest"] != bound.authority_digest:
            raise ValueError("pre-evaluation binding digest mismatch")
        if envelope["authority_id"] != bound.authority_id:
            raise ValueError("pre-evaluation binding authority id mismatch")
        return bound

    def load_expected(
        self,
        *,
        context: PreEvaluationDenominatorContext,
        provider_members: Iterable[ProviderMemberIdentity],
    ) -> BoundPreEvaluationSession:
        bound = self.load()
        supplied = tuple(provider_members)
        if any(not isinstance(member, ProviderMemberIdentity) for member in supplied):
            raise TypeError("provider_members must contain ProviderMemberIdentity values")
        if len({member.row_key for member in supplied}) != len(supplied):
            raise ValueError("duplicate provider row_key")
        expected_members = tuple(
            sorted(
                ((member.row_key, member.member_sha256) for member in supplied),
                key=lambda item: item[0],
            )
        )
        if bound.context != context:
            raise ValueError("pre-evaluation binding context mismatch")
        actual_members = tuple((m.row_key, m.member_sha256) for m in bound.members)
        if actual_members != expected_members:
            raise ValueError("pre-evaluation provider member identity mismatch")
        return bound

    @staticmethod
    def _from_payload(payload: Mapping[str, object]) -> BoundPreEvaluationSession:
        base_expected = {
            "schema_version",
            "authority_family",
            "context",
            "context_digest",
            "session_authority_id",
            "session_authority_digest",
            "session_evidence",
            "members",
            "member_authority_sha256",
        }
        allowed = (
            frozenset(base_expected),
            frozenset(base_expected | {"decision_ledger_prefix"}),
        )
        if frozenset(payload) not in allowed:
            raise ValueError("pre-evaluation binding payload has unexpected fields")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported pre-evaluation binding payload schema")
        if payload["authority_family"] != AUTHORITY_FAMILY:
            raise ValueError("unexpected pre-evaluation binding payload family")

        raw_context = payload["context"]
        if type(raw_context) is not dict:
            raise ValueError("context must be an object")
        context = PreEvaluationDenominatorContext.from_payload(raw_context)
        if payload["context_digest"] != context.digest:
            raise ValueError("denominator context digest mismatch")

        raw_evidence = payload["session_evidence"]
        if type(raw_evidence) is not dict:
            raise ValueError("session_evidence must be an object")
        evidence = PreEvaluationEvidenceStore._from_payload(raw_evidence)
        if payload["session_authority_id"] != evidence.authority_id:
            raise ValueError("session authority id mismatch")
        if payload["session_authority_digest"] != evidence.authority_digest:
            raise ValueError("session authority digest mismatch")

        raw_members = payload["members"]
        if type(raw_members) is not list:
            raise ValueError("members must be a list")
        members: list[BoundPreEvaluationMember] = []
        for raw in raw_members:
            if type(raw) is not dict or set(raw) != {
                "row_key",
                "member_sha256",
                "slot_evidence_digest",
            }:
                raise ValueError("bound member payload has unexpected fields")
            members.append(
                BoundPreEvaluationMember(
                    row_key=_text("row_key", raw["row_key"]),
                    member_sha256=_sha("member_sha256", raw["member_sha256"]),
                    slot_evidence_digest=_sha(
                        "slot_evidence_digest", raw["slot_evidence_digest"]
                    ),
                )
            )

        prefix_sha256 = None
        prefix_record_count = None
        raw_prefix = payload.get("decision_ledger_prefix")
        if raw_prefix is not None:
            if type(raw_prefix) is not dict or set(raw_prefix) != {"sha256", "record_count"}:
                raise ValueError("decision_ledger_prefix has unexpected fields")
            prefix_sha256 = _sha("decision_ledger_prefix.sha256", raw_prefix["sha256"])
            raw_count = raw_prefix["record_count"]
            if type(raw_count) is not int or raw_count < 0:
                raise ValueError(
                    "decision_ledger_prefix.record_count must be a non-negative integer"
                )
            prefix_record_count = raw_count

        bound = BoundPreEvaluationSession(
            context=context,
            evidence=evidence,
            members=tuple(members),
            decision_ledger_prefix_sha256=prefix_sha256,
            decision_ledger_prefix_record_count=prefix_record_count,
        )
        expected_member_digests = [
            [row_key, digest] for row_key, digest in bound.member_authority_sha256
        ]
        if payload["member_authority_sha256"] != expected_member_digests:
            raise ValueError("member authority digest mismatch")
        return bound
