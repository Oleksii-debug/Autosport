"""Bind causal reference-price evidence into the durable Decision Ledger.

The reference-price engine (#1245) already produces an exact structural
ReferencePriceEvidence object. This module gives that evidence one narrow
durability property without creating a second journal: a GENERAL decision embeds
the complete canonical evidence payload plus a digest binding before
JsonlDecisionLedger.append. The ledger's existing fsync + record digest then
makes the decision and its as-known reference one durable record.

On restart, the resolver reconstructs ReferencePriceEvidence from the ledger
bytes and re-runs its canonical validation. A later corrected quote/reference
therefore requires a later decision record; it cannot rewrite the evidence that
an earlier decision actually carried.

This module does not prove provider publish/last-change freshness, gap recovery,
execution feasibility, fair value, profitability, or real-money authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from .decision_ledger import (
    GENERAL_DECISION_KIND,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .reference_price_evidence import (
    ReferenceObservation,
    ReferencePriceEvidence,
    ReferencePriceEvidenceError,
    ReferencePriceProtocol,
    ReferenceTargetInclusionPolicy,
)


_BINDING_KEY: Final = "reference_price_decision_evidence"
_BINDING_SCHEMA: Final = "autosport.reference-price-decision-evidence"
_BINDING_SCHEMA_VERSION: Final = 1
_BINDING_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "decision_shell_sha256",
        "evidence_id",
        "protocol_id",
        "decision_ts",
        "evidence_sha256",
        "binding_sha256",
        "evidence",
    }
)
_HEX: Final = frozenset("0123456789abcdef")
_MAX_EVIDENCE_BYTES: Final = 2 * 1024 * 1024


class ReferencePriceDecisionBindingError(RuntimeError):
    """Reference-price evidence is not a truthful durable decision binding."""


@dataclass(frozen=True, slots=True)
class ResolvedReferencePriceDecisionEvidence:
    """One restart-resolved reference evidence binding from the Decision Ledger."""

    decision_id: str
    binding_sha256: str
    decision_shell_sha256: str
    evidence_sha256: str
    evidence: ReferencePriceEvidence

    def __post_init__(self) -> None:
        _text(self.decision_id, "decision_id")
        for name in (
            "binding_sha256",
            "decision_shell_sha256",
            "evidence_sha256",
        ):
            _sha256(getattr(self, name), name)
        if type(self.evidence) is not ReferencePriceEvidence:
            raise ReferencePriceDecisionBindingError(
                "resolved evidence must be exact ReferencePriceEvidence"
            )

    @property
    def executable_reference_verified(self) -> bool:
        return False

    @property
    def fair_probability_verified(self) -> bool:
        return False

    @property
    def real_money_execution_authorized(self) -> bool:
        return False


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ReferencePriceDecisionBindingError(
            f"{name} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReferencePriceDecisionBindingError(
            f"{name} must be valid UTF-8 text"
        ) from exc
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ReferencePriceDecisionBindingError(
            f"{name} must be lowercase SHA-256"
        )
    return value


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReferencePriceDecisionBindingError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReferencePriceDecisionBindingError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReferencePriceDecisionBindingError(
            "reference decision payload is outside canonical JSON domain"
        ) from exc


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _evidence_payload(evidence: ReferencePriceEvidence) -> dict[str, object]:
    if type(evidence) is not ReferencePriceEvidence:
        raise ReferencePriceDecisionBindingError(
            "evidence must be exact ReferencePriceEvidence"
        )
    payload = evidence.to_dict()
    if type(payload) is not dict:
        raise ReferencePriceDecisionBindingError(
            "reference evidence must serialize to one JSON object"
        )
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > _MAX_EVIDENCE_BYTES:
        raise ReferencePriceDecisionBindingError(
            "reference evidence is too large for one durable decision record"
        )
    return payload


def _evidence_from_payload(payload: object) -> ReferencePriceEvidence:
    if type(payload) is not dict:
        raise ReferencePriceDecisionBindingError(
            "durable reference evidence must be an exact JSON object"
        )
    try:
        protocol_payload = payload["protocol"]
        observations_payload = payload["observations"]
        if type(protocol_payload) is not dict or type(observations_payload) is not list:
            raise TypeError
        protocol = ReferencePriceProtocol(
            eligible_source_ids=tuple(protocol_payload["eligible_source_ids"]),
            eligible_price_source_ids=tuple(
                protocol_payload["eligible_price_source_ids"]
            ),
            target_price_source_id=protocol_payload["target_price_source_id"],
            target_inclusion_policy=ReferenceTargetInclusionPolicy(
                protocol_payload["target_inclusion_policy"]
            ),
            price_semantics=protocol_payload["price_semantics"],
            max_age_seconds=protocol_payload["max_age_seconds"],
            max_skew_seconds=protocol_payload["max_skew_seconds"],
            minimum_sources=protocol_payload["minimum_sources"],
            aggregation_method=protocol_payload["aggregation_method"],
        )
        observations = tuple(
            ReferenceObservation(
                event_canonical_json=observation["event_canonical_json"]
            )
            for observation in observations_payload
        )
        evidence = ReferencePriceEvidence(
            sport=payload["sport"],
            event_id=payload["event_id"],
            market_id=payload["market_id"],
            selection_id=payload["selection_id"],
            market_type=payload["market_type"],
            market_semantics_id=payload["market_semantics_id"],
            protocol=protocol,
            decision_ts=payload["decision_ts"],
            observations=observations,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        ReferencePriceEvidenceError,
    ) as exc:
        raise ReferencePriceDecisionBindingError(
            "durable reference evidence cannot be reconstructed canonically"
        ) from exc
    if evidence.to_dict() != payload:
        raise ReferencePriceDecisionBindingError(
            "durable reference evidence is not the exact canonical payload"
        )
    _evidence_payload(evidence)
    return evidence


def _decision_shell_payload(
    decision: DecisionRecord,
    *,
    allow_bound: bool,
) -> dict[str, object]:
    if type(decision) is not DecisionRecord:
        raise ReferencePriceDecisionBindingError(
            "decision must be exact DecisionRecord"
        )
    if decision.decision_kind != GENERAL_DECISION_KIND:
        raise ReferencePriceDecisionBindingError(
            "reference-price decision binding currently supports GENERAL decisions only"
        )
    detached = decision.to_dict()
    payload = detached.get("payload")
    if type(payload) is not dict:
        raise ReferencePriceDecisionBindingError(
            "decision payload must be an exact JSON object"
        )
    if _BINDING_KEY in payload:
        if not allow_bound:
            raise ReferencePriceDecisionBindingError(
                "decision already contains reference-price evidence"
            )
        payload = dict(payload)
        del payload[_BINDING_KEY]
        detached = dict(detached)
        detached["payload"] = payload
    return detached


def _decision_shell_sha256(
    decision: DecisionRecord,
    *,
    allow_bound: bool,
) -> str:
    return _canonical_sha256(
        _decision_shell_payload(decision, allow_bound=allow_bound)
    )


def _same_decision_instant(
    decision: DecisionRecord,
    evidence: ReferencePriceEvidence,
) -> bool:
    return _instant(
        decision.observed_ts,
        "decision observed_ts",
    ) == _instant(evidence.decision_ts, "reference decision_ts")


def _binding_identity_payload(
    *,
    decision_id: str,
    decision_shell_sha256: str,
    evidence_id: str,
    protocol_id: str,
    decision_ts: str,
    evidence_sha256: str,
) -> dict[str, object]:
    return {
        "schema": _BINDING_SCHEMA,
        "schema_version": _BINDING_SCHEMA_VERSION,
        "decision_id": decision_id,
        "decision_shell_sha256": decision_shell_sha256,
        "evidence_id": evidence_id,
        "protocol_id": protocol_id,
        "decision_ts": decision_ts,
        "evidence_sha256": evidence_sha256,
    }


def bind_reference_price_evidence(
    decision: DecisionRecord,
    evidence: ReferencePriceEvidence,
) -> DecisionRecord:
    """Return decision with exact reference evidence atomically in its payload.

    The returned record is not yet durable. Call the existing
    JsonlDecisionLedger.append exactly as for any other GENERAL decision.
    Once appended, evidence and decision share one fsynced record digest.
    """

    shell = _decision_shell_payload(decision, allow_bound=False)
    evidence_payload = _evidence_payload(evidence)
    if not _same_decision_instant(decision, evidence):
        raise ReferencePriceDecisionBindingError(
            "DecisionRecord observed_ts must equal reference evidence decision_ts"
        )

    shell_sha = _canonical_sha256(shell)
    evidence_sha = _canonical_sha256(evidence_payload)
    identity = _binding_identity_payload(
        decision_id=decision.decision_id,
        decision_shell_sha256=shell_sha,
        evidence_id=evidence.evidence_id,
        protocol_id=evidence.protocol_id,
        decision_ts=evidence.decision_ts,
        evidence_sha256=evidence_sha,
    )
    binding_sha = _canonical_sha256(identity)
    binding = {
        "schema": _BINDING_SCHEMA,
        "schema_version": _BINDING_SCHEMA_VERSION,
        "decision_shell_sha256": shell_sha,
        "evidence_id": evidence.evidence_id,
        "protocol_id": evidence.protocol_id,
        "decision_ts": evidence.decision_ts,
        "evidence_sha256": evidence_sha,
        "binding_sha256": binding_sha,
        "evidence": evidence_payload,
    }

    detached = decision.to_dict()
    payload = detached["payload"]
    assert type(payload) is dict
    payload[_BINDING_KEY] = binding
    return DecisionRecord(
        replay_run_id=decision.replay_run_id,
        agent=decision.agent,
        observed_ts=decision.observed_ts,
        action=decision.action,
        payload=payload,
        context_hash=decision.context_hash,
        decision_id=decision.decision_id,
        recorded_at=decision.recorded_at,
        decision_kind=decision.decision_kind,
    )


def resolve_reference_price_evidence(
    decision: DecisionRecord,
) -> ResolvedReferencePriceDecisionEvidence:
    """Verify and reconstruct reference evidence from one durable-style record."""

    _decision_shell_payload(decision, allow_bound=True)
    detached = decision.to_dict()
    payload = detached["payload"]
    assert type(payload) is dict
    binding = payload.get(_BINDING_KEY)
    if type(binding) is not dict or frozenset(binding) != _BINDING_KEYS:
        raise ReferencePriceDecisionBindingError(
            "decision reference-price binding schema is invalid"
        )
    if (
        binding["schema"] != _BINDING_SCHEMA
        or binding["schema_version"] != _BINDING_SCHEMA_VERSION
        or type(binding["schema_version"]) is not int
    ):
        raise ReferencePriceDecisionBindingError(
            "decision reference-price binding version is invalid"
        )

    for name in (
        "decision_shell_sha256",
        "evidence_id",
        "protocol_id",
        "evidence_sha256",
        "binding_sha256",
    ):
        _sha256(binding[name], name)
    _text(binding["decision_ts"], "decision_ts")

    evidence = _evidence_from_payload(binding["evidence"])
    evidence_payload = _evidence_payload(evidence)
    evidence_sha = _canonical_sha256(evidence_payload)
    shell_sha = _decision_shell_sha256(decision, allow_bound=True)

    if (
        binding["decision_shell_sha256"] != shell_sha
        or binding["evidence_id"] != evidence.evidence_id
        or binding["protocol_id"] != evidence.protocol_id
        or binding["decision_ts"] != evidence.decision_ts
        or binding["evidence_sha256"] != evidence_sha
    ):
        raise ReferencePriceDecisionBindingError(
            "decision reference-price binding does not match canonical evidence"
        )
    if not _same_decision_instant(decision, evidence):
        raise ReferencePriceDecisionBindingError(
            "durable decision timestamp does not match reference evidence"
        )

    identity = _binding_identity_payload(
        decision_id=decision.decision_id,
        decision_shell_sha256=shell_sha,
        evidence_id=evidence.evidence_id,
        protocol_id=evidence.protocol_id,
        decision_ts=evidence.decision_ts,
        evidence_sha256=evidence_sha,
    )
    binding_sha = _canonical_sha256(identity)
    if binding["binding_sha256"] != binding_sha:
        raise ReferencePriceDecisionBindingError(
            "decision reference-price binding digest mismatch"
        )

    return ResolvedReferencePriceDecisionEvidence(
        decision_id=decision.decision_id,
        binding_sha256=binding_sha,
        decision_shell_sha256=shell_sha,
        evidence_sha256=evidence_sha,
        evidence=evidence,
    )


def resolve_ledger_reference_price_evidence(
    ledger: JsonlDecisionLedger,
    decision_id: str,
) -> ResolvedReferencePriceDecisionEvidence:
    """Restart-safe readback from the canonical verified Decision Ledger."""

    if type(ledger) is not JsonlDecisionLedger:
        raise ReferencePriceDecisionBindingError(
            "ledger must be exact JsonlDecisionLedger"
        )
    _text(decision_id, "decision_id")
    matches = tuple(
        record
        for record in ledger.verified_records()
        if record.decision_id == decision_id
    )
    if len(matches) != 1:
        raise ReferencePriceDecisionBindingError(
            "Decision Ledger must contain exactly one requested decision"
        )
    return resolve_reference_price_evidence(matches[0])
