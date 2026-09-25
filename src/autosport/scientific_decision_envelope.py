"""Fail-closed sealing for scientific pre-decision evidence assertions.

This module records the identity and chronology asserted for evidence presented to a
decision path. Caller-created evidence references are deliberately not product-owned
proof that the evidence was causally available at the asserted time. The envelope is
scientific structure only: it grants no causal-availability, execution, risk,
provider-write, promotion, or real-money authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


_HEX = frozenset("0123456789abcdef")


class DecisionEnvelopeError(ValueError):
    """Raised when pre-decision evidence is non-canonical or causally invalid."""


class EvidenceKind(StrEnum):
    FEATURE = "FEATURE"
    MARKET = "MARKET"
    CONTEXT = "CONTEXT"


class DecisionDisposition(StrEnum):
    ACTION = "ACTION"
    ABSTAIN = "ABSTAIN"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise DecisionEnvelopeError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DecisionEnvelopeError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise DecisionEnvelopeError(f"{name} must be lowercase SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionEnvelopeError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionEnvelopeError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


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
class CausalEvidenceRef:
    """One immutable caller assertion about evidence chronology and identity.

    Structural ordering is validated, but ordinary construction does not prove that
    the product actually observed or durably published the evidence at ``available_at``.
    Consumers requiring scientific causal-availability authority must therefore use a
    separate product-owned availability/trust receipt and must not treat this DTO as
    that receipt.
    """

    evidence_id: str
    kind: EvidenceKind
    evidence_sha256: str
    event_at: str
    ingested_at: str
    available_at: str

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence_id")
        if not isinstance(self.kind, EvidenceKind):
            raise DecisionEnvelopeError("kind must be EvidenceKind")
        _sha256(self.evidence_sha256, "evidence_sha256")
        event = _instant(self.event_at, "event_at")
        ingested = _instant(self.ingested_at, "ingested_at")
        available = _instant(self.available_at, "available_at")
        if event > ingested:
            raise DecisionEnvelopeError("event_at must not be after ingested_at")
        if ingested > available:
            raise DecisionEnvelopeError("ingested_at must not be after available_at")

    @property
    def availability_authority_proven(self) -> bool:
        """Caller-authored chronology is never product-owned availability authority."""

        return False

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "evidence_sha256": self.evidence_sha256,
            "event_at": _time(self.event_at, "event_at"),
            "ingested_at": _time(self.ingested_at, "ingested_at"),
            "available_at": _time(self.available_at, "available_at"),
            "availability_authority_proven": False,
        }


def evidence_snapshot_sha256(
    evidence: tuple[CausalEvidenceRef, ...],
    kind: EvidenceKind,
) -> str:
    """Hash the exact canonical assertion subset for one scientific surface."""

    if not isinstance(evidence, tuple):
        raise DecisionEnvelopeError("evidence must be a tuple")
    if not isinstance(kind, EvidenceKind):
        raise DecisionEnvelopeError("kind must be EvidenceKind")
    if any(type(item) is not CausalEvidenceRef for item in evidence):
        raise DecisionEnvelopeError("evidence must contain exact CausalEvidenceRef values")
    selected = sorted(
        (item.canonical_payload() for item in evidence if item.kind is kind),
        key=lambda item: (item["evidence_id"], item["evidence_sha256"]),
    )
    if not selected:
        raise DecisionEnvelopeError(f"{kind.value} evidence must not be empty")
    return _digest({"kind": kind.value, "evidence": selected, "schema_version": 2})


@dataclass(frozen=True, slots=True)
class SealedDecisionEnvelope:
    """Canonical pre-decision scientific assertion envelope.

    The envelope seals exactly what a caller presented and when the caller asserted it
    was available. It does not prove the product-owned causal availability of those
    inputs. This distinction is machine-readable and non-overridable so downstream
    evaluation/promotion code cannot lawfully interpret the sealed timestamps as a
    positive availability receipt.
    """

    protocol_id: str
    decision_id: str
    decision_at: str
    event_watermark: str
    ingest_watermark: str
    disposition: DecisionDisposition
    evidence: tuple[CausalEvidenceRef, ...]
    feature_snapshot_sha256: str
    market_snapshot_sha256: str
    action_identity: str | None = None
    abstention_reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.protocol_id, "protocol_id")
        _text(self.decision_id, "decision_id")
        if not isinstance(self.disposition, DecisionDisposition):
            raise DecisionEnvelopeError("disposition must be DecisionDisposition")
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise DecisionEnvelopeError("evidence must be a non-empty tuple")
        if any(type(item) is not CausalEvidenceRef for item in self.evidence):
            raise DecisionEnvelopeError("evidence must contain exact CausalEvidenceRef values")

        decision = _instant(self.decision_at, "decision_at")
        event_watermark = _instant(self.event_watermark, "event_watermark")
        ingest_watermark = _instant(self.ingest_watermark, "ingest_watermark")
        if event_watermark > ingest_watermark:
            raise DecisionEnvelopeError("event_watermark must not be after ingest_watermark")
        if ingest_watermark > decision:
            raise DecisionEnvelopeError("ingest_watermark must not be after decision_at")

        canonical = tuple(
            sorted(
                self.evidence,
                key=lambda item: (
                    item.kind.value,
                    item.evidence_id,
                    item.evidence_sha256,
                    _time(item.available_at, "available_at"),
                ),
            )
        )
        identities = [(item.kind.value, item.evidence_id) for item in canonical]
        if len(identities) != len(set(identities)):
            raise DecisionEnvelopeError("evidence identities must not be duplicated")
        object.__setattr__(self, "evidence", canonical)

        for item in canonical:
            event = _instant(item.event_at, "event_at")
            ingested = _instant(item.ingested_at, "ingested_at")
            available = _instant(item.available_at, "available_at")
            if event > event_watermark:
                raise DecisionEnvelopeError("evidence event is after event_watermark")
            if ingested > ingest_watermark:
                raise DecisionEnvelopeError("evidence was ingested after ingest_watermark")
            if available > decision:
                raise DecisionEnvelopeError("future evidence is not available at decision time")

        feature_hash = _sha256(self.feature_snapshot_sha256, "feature_snapshot_sha256")
        market_hash = _sha256(self.market_snapshot_sha256, "market_snapshot_sha256")
        if feature_hash != evidence_snapshot_sha256(canonical, EvidenceKind.FEATURE):
            raise DecisionEnvelopeError("feature snapshot hash does not match evidence")
        if market_hash != evidence_snapshot_sha256(canonical, EvidenceKind.MARKET):
            raise DecisionEnvelopeError("market snapshot hash does not match evidence")

        if self.disposition is DecisionDisposition.ACTION:
            _text(self.action_identity, "action_identity")
            if self.abstention_reason is not None:
                raise DecisionEnvelopeError("ACTION must not carry abstention_reason")
        else:
            _text(self.abstention_reason, "abstention_reason")
            if self.action_identity is not None:
                raise DecisionEnvelopeError("ABSTAIN must not carry action_identity")

    @classmethod
    def seal(
        cls,
        *,
        protocol_id: str,
        decision_id: str,
        decision_at: str,
        event_watermark: str,
        ingest_watermark: str,
        disposition: DecisionDisposition,
        evidence: tuple[CausalEvidenceRef, ...],
        action_identity: str | None = None,
        abstention_reason: str | None = None,
    ) -> "SealedDecisionEnvelope":
        return cls(
            protocol_id=protocol_id,
            decision_id=decision_id,
            decision_at=decision_at,
            event_watermark=event_watermark,
            ingest_watermark=ingest_watermark,
            disposition=disposition,
            evidence=evidence,
            feature_snapshot_sha256=evidence_snapshot_sha256(evidence, EvidenceKind.FEATURE),
            market_snapshot_sha256=evidence_snapshot_sha256(evidence, EvidenceKind.MARKET),
            action_identity=action_identity,
            abstention_reason=abstention_reason,
        )

    @property
    def availability_authority_proven(self) -> bool:
        """This contract never upgrades asserted timestamps into causal authority."""

        return False

    def require_product_proven_causal_availability(self) -> None:
        """Fail closed for consumers that require authoritative causal availability."""

        raise DecisionEnvelopeError(
            "sealed decision envelope contains caller-asserted chronology only; "
            "product-owned causal availability authority is unproven"
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": "autosport.sealed_decision_envelope",
            "schema_version": 2,
            "protocol_id": self.protocol_id,
            "decision_id": self.decision_id,
            "decision_at": _time(self.decision_at, "decision_at"),
            "event_watermark": _time(self.event_watermark, "event_watermark"),
            "ingest_watermark": _time(self.ingest_watermark, "ingest_watermark"),
            "disposition": self.disposition.value,
            "action_identity": self.action_identity,
            "abstention_reason": self.abstention_reason,
            "feature_snapshot_sha256": self.feature_snapshot_sha256,
            "market_snapshot_sha256": self.market_snapshot_sha256,
            "evidence": [item.canonical_payload() for item in self.evidence],
            "availability_authority_proven": False,
            "availability_semantics": "caller_asserted_ordering_only",
            "authority_grant": False,
        }

    @property
    def envelope_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class DecisionOutcomeAppend:
    """Outcome evidence that can only reference, never rewrite, a sealed envelope."""

    decision_id: str
    envelope_sha256: str
    outcome_id: str
    outcome_sha256: str
    revealed_at: str

    def __post_init__(self) -> None:
        _text(self.decision_id, "decision_id")
        _sha256(self.envelope_sha256, "envelope_sha256")
        _text(self.outcome_id, "outcome_id")
        _sha256(self.outcome_sha256, "outcome_sha256")
        _instant(self.revealed_at, "revealed_at")

    @classmethod
    def attach(
        cls,
        envelope: SealedDecisionEnvelope,
        *,
        outcome_id: str,
        outcome_sha256: str,
        revealed_at: str,
    ) -> "DecisionOutcomeAppend":
        if type(envelope) is not SealedDecisionEnvelope:
            raise DecisionEnvelopeError("envelope must be exact SealedDecisionEnvelope")
        if _instant(revealed_at, "revealed_at") <= _instant(
            envelope.decision_at, "decision_at"
        ):
            raise DecisionEnvelopeError("outcome must be revealed after decision_at")
        return cls(
            decision_id=envelope.decision_id,
            envelope_sha256=envelope.envelope_sha256,
            outcome_id=outcome_id,
            outcome_sha256=outcome_sha256,
            revealed_at=revealed_at,
        )

    def verify_envelope(self, envelope: SealedDecisionEnvelope) -> None:
        if type(envelope) is not SealedDecisionEnvelope:
            raise DecisionEnvelopeError("envelope must be exact SealedDecisionEnvelope")
        if self.decision_id != envelope.decision_id:
            raise DecisionEnvelopeError("outcome decision_id does not match envelope")
        if self.envelope_sha256 != envelope.envelope_sha256:
            raise DecisionEnvelopeError("outcome envelope hash does not match envelope")
        if _instant(self.revealed_at, "revealed_at") <= _instant(
            envelope.decision_at, "decision_at"
        ):
            raise DecisionEnvelopeError("outcome must be revealed after decision_at")
