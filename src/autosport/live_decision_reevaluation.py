"""Product-owned persistence and causal verification for disposition reevaluations.

This module extends the existing live-decision-disposition authority without adding a
second registry.  Canonical disposition bytes are persisted as GENERAL records in the
existing append-only :class:`JsonlDecisionLedger`.  Reevaluation verification always
re-resolves the predecessor from that ledger and, for positive policy states, reuses
``verify_product_policy_authority`` against the same durable economic authority.

The verifier grants no provider-write, execution, settlement, learning-outcome, or
real-money authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Final

from .decision_ledger import (
    GENERAL_DECISION_KIND,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from .live_decision_disposition import (
    Disposition,
    LiveDecisionDisposition,
    LiveDecisionDispositionError,
    ProductPolicyAuthorityBinding,
    ReevaluationTrigger,
    verify_product_policy_authority,
)


RECORD_SCHEMA: Final = "autosport.live_decision_disposition_record"
RECORD_SCHEMA_VERSION: Final = 1
RECORD_ACTION: Final = "LIVE_DECISION_DISPOSITION"
RECORD_AGENT: Final = "autosport-live-decision-disposition"
_RECORD_ID_PREFIX: Final = "live-disposition:"
_RECORD_FIELDS: Final = frozenset(
    {"schema", "schema_version", "disposition_id", "canonical_json"}
)
_HEX: Final = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LiveDecisionDispositionError("reevaluation value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise LiveDecisionDispositionError(f"{field} must be lowercase SHA-256 hex")
    return value


def _record_id(disposition_id: str) -> str:
    return f"{_RECORD_ID_PREFIX}{_sha256(disposition_id, 'disposition_id')}"


def _lineage_payload(disposition: LiveDecisionDisposition) -> dict[str, object]:
    return {
        "strategy_id": disposition.strategy_id,
        "strategy_version": disposition.strategy_version,
        "market": disposition.market.to_dict(),
        "required_evidence_policy_sha256": disposition.required_evidence_policy_sha256,
    }


def _lineage_sha256(disposition: LiveDecisionDisposition) -> str:
    return _digest(_lineage_payload(disposition))


def _evidence_identity_sha256(disposition: LiveDecisionDisposition) -> str:
    """Hash bound evidence identities, excluding reasons, truth labels, and clocks."""

    return _digest(
        [
            {
                "predicate_id": predicate.predicate_id,
                "evidence": [reference.to_dict() for reference in predicate.evidence],
            }
            for predicate in disposition.predicates
        ]
    )


def _record_payload(disposition: LiveDecisionDisposition) -> dict[str, object]:
    return {
        "schema": RECORD_SCHEMA,
        "schema_version": RECORD_SCHEMA_VERSION,
        "disposition_id": disposition.disposition_id,
        "canonical_json": disposition.to_json(),
    }


def _decode_record(record: DecisionRecord) -> LiveDecisionDisposition:
    detached = record.to_dict()
    payload = detached.get("payload")
    if type(payload) is not dict or set(payload) != _RECORD_FIELDS:
        raise LiveDecisionDispositionError("durable disposition record fields mismatch")
    if (
        payload.get("schema") != RECORD_SCHEMA
        or payload.get("schema_version") != RECORD_SCHEMA_VERSION
    ):
        raise LiveDecisionDispositionError("durable disposition record schema mismatch")

    disposition_id = _sha256(payload.get("disposition_id"), "durable disposition_id")
    canonical_json = payload.get("canonical_json")
    if type(canonical_json) is not str:
        raise LiveDecisionDispositionError("durable disposition canonical_json must be text")
    try:
        disposition = LiveDecisionDisposition.from_json(canonical_json)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, LiveDecisionDispositionError):
            raise
        raise LiveDecisionDispositionError("durable disposition cannot be decoded") from exc

    if canonical_json != disposition.to_json():
        raise LiveDecisionDispositionError("durable disposition JSON is not canonical")
    if disposition.disposition_id != disposition_id:
        raise LiveDecisionDispositionError("durable disposition identity mismatch")
    if record.decision_kind != GENERAL_DECISION_KIND:
        raise LiveDecisionDispositionError("durable disposition record must be GENERAL")
    if record.decision_id != _record_id(disposition_id):
        raise LiveDecisionDispositionError("durable disposition ledger identity mismatch")
    if record.agent != RECORD_AGENT or record.action != RECORD_ACTION:
        raise LiveDecisionDispositionError("durable disposition record authority mismatch")
    if record.replay_run_id != f"live-disposition:{disposition.decision_id}":
        raise LiveDecisionDispositionError("durable disposition decision lineage mismatch")
    if record.observed_ts != disposition.evaluated_at:
        raise LiveDecisionDispositionError("durable disposition evaluation time mismatch")
    if record.context_hash != _lineage_sha256(disposition):
        raise LiveDecisionDispositionError("durable disposition context lineage mismatch")
    return disposition


def _verified_records_or_pristine(ledger: JsonlDecisionLedger) -> tuple[DecisionRecord, ...]:
    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")
    if not ledger.path.exists():
        return ()
    try:
        return ledger.verified_records()
    except DecisionLedgerIntegrityError as exc:
        raise LiveDecisionDispositionError(
            "durable disposition ledger cannot be verified"
        ) from exc


def persist_live_decision_disposition(
    disposition: LiveDecisionDisposition,
    *,
    ledger: JsonlDecisionLedger,
) -> DecisionRecord:
    """Idempotently persist one canonical disposition in the existing Decision Ledger."""

    if not isinstance(disposition, LiveDecisionDisposition):
        raise TypeError("disposition must be LiveDecisionDisposition")
    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")

    target_id = _record_id(disposition.disposition_id)
    for record in _verified_records_or_pristine(ledger):
        if record.decision_id != target_id:
            continue
        resolved = _decode_record(record)
        if resolved != disposition:
            raise LiveDecisionDispositionError(
                "durable disposition id already resolves to different canonical bytes"
            )
        return record

    record = DecisionRecord(
        replay_run_id=f"live-disposition:{disposition.decision_id}",
        agent=RECORD_AGENT,
        observed_ts=disposition.evaluated_at,
        action=RECORD_ACTION,
        payload=_record_payload(disposition),
        context_hash=_lineage_sha256(disposition),
        decision_id=target_id,
    )
    try:
        ledger.append(record)
    except DecisionLedgerIntegrityError as exc:
        raise LiveDecisionDispositionError(
            "durable disposition could not be persisted"
        ) from exc
    return record


def resolve_live_decision_disposition(
    disposition_id: str,
    *,
    ledger: JsonlDecisionLedger,
) -> LiveDecisionDisposition:
    """Re-resolve exact canonical predecessor bytes from the product Decision Ledger."""

    target_id = _record_id(disposition_id)
    records = _verified_records_or_pristine(ledger)
    for record in records:
        if record.decision_id == target_id:
            return _decode_record(record)
    raise LiveDecisionDispositionError(
        "predecessor disposition is missing from durable product authority"
    )


def _verify_positive_policy_if_present(
    disposition: LiveDecisionDisposition,
    *,
    ledger: JsonlDecisionLedger,
    authority: EconomicDecisionAuthority | None,
) -> None:
    if disposition.product_policy_authority is None:
        return
    if not isinstance(authority, EconomicDecisionAuthority):
        raise LiveDecisionDispositionError(
            "positive reevaluation requires EconomicDecisionAuthority re-resolution"
        )
    verify_product_policy_authority(
        disposition,
        ledger=ledger,
        authority=authority,
    )


def _require_same_lineage(
    predecessor: LiveDecisionDisposition,
    successor: LiveDecisionDisposition,
) -> None:
    if _lineage_payload(predecessor) != _lineage_payload(successor):
        raise LiveDecisionDispositionError(
            "reevaluation predecessor belongs to a different decision lineage"
        )


def _require_monotonic_evaluation(
    predecessor: LiveDecisionDisposition,
    successor: LiveDecisionDisposition,
) -> None:
    predecessor_decision_at = datetime.fromisoformat(
        predecessor.decision_at.replace("Z", "+00:00")
    )
    successor_decision_at = datetime.fromisoformat(
        successor.decision_at.replace("Z", "+00:00")
    )
    predecessor_evaluated_at = datetime.fromisoformat(
        predecessor.evaluated_at.replace("Z", "+00:00")
    )
    successor_evaluated_at = datetime.fromisoformat(
        successor.evaluated_at.replace("Z", "+00:00")
    )
    if successor_decision_at < predecessor_decision_at:
        raise LiveDecisionDispositionError("reevaluation decision time cannot move backwards")
    if successor_evaluated_at < predecessor_evaluated_at:
        raise LiveDecisionDispositionError("reevaluation evaluation time cannot move backwards")
    if (
        successor.product_policy_authority is not None
        and successor_decision_at <= predecessor_evaluated_at
    ):
        raise LiveDecisionDispositionError(
            "positive reevaluation policy decision must follow predecessor evaluation"
        )


def verify_reevaluation_transition(
    successor: LiveDecisionDisposition,
    *,
    ledger: JsonlDecisionLedger,
    authority: EconomicDecisionAuthority | None = None,
) -> LiveDecisionDisposition:
    """Fail closed unless one reevaluation is causally grounded in durable truth.

    The predecessor is always re-read from ``ledger``.  Trigger-specific materiality
    is then enforced:

    * ``NEW_EVIDENCE`` requires a changed bound evidence identity.
    * ``POLICY_REEVALUATION`` requires both policy states to re-resolve and their
      product-owned policy bindings to differ.
    * ``EXPIRY_RECOMPUTE`` requires an EXPIRED predecessor and changed evidence.
    * ``SAFETY_REVALIDATION`` requires a HALT_SAFETY predecessor plus changed
      evidence or a newly/differently re-resolved product policy binding.
    * Any positive policy successor must bind a re-resolved economic decision strictly
      later than the predecessor evaluation, so later evidence/state cannot relabel an
      older policy result as a causal reevaluation.

    Text, timestamps, TTL changes, or a caller-supplied predecessor digest alone are
    never sufficient evidence of a causal reevaluation.
    """

    if not isinstance(successor, LiveDecisionDisposition):
        raise TypeError("successor must be LiveDecisionDisposition")
    if successor.predecessor_disposition_id is None or successor.reevaluation_trigger is None:
        raise LiveDecisionDispositionError(
            "reevaluation verification requires predecessor and trigger"
        )

    predecessor = resolve_live_decision_disposition(
        successor.predecessor_disposition_id,
        ledger=ledger,
    )
    _require_same_lineage(predecessor, successor)
    _require_monotonic_evaluation(predecessor, successor)

    _verify_positive_policy_if_present(predecessor, ledger=ledger, authority=authority)
    _verify_positive_policy_if_present(successor, ledger=ledger, authority=authority)

    evidence_changed = (
        _evidence_identity_sha256(predecessor)
        != _evidence_identity_sha256(successor)
    )
    predecessor_policy = predecessor.product_policy_authority
    successor_policy = successor.product_policy_authority
    policy_changed = predecessor_policy != successor_policy
    trigger = successor.reevaluation_trigger

    if trigger is ReevaluationTrigger.NEW_EVIDENCE:
        if not evidence_changed:
            raise LiveDecisionDispositionError(
                "NEW_EVIDENCE requires materially changed bound evidence"
            )
    elif trigger is ReevaluationTrigger.POLICY_REEVALUATION:
        if not isinstance(predecessor_policy, ProductPolicyAuthorityBinding) or not isinstance(
            successor_policy, ProductPolicyAuthorityBinding
        ):
            raise LiveDecisionDispositionError(
                "POLICY_REEVALUATION requires two product-owned policy bindings"
            )
        if not policy_changed:
            raise LiveDecisionDispositionError(
                "POLICY_REEVALUATION requires materially changed product policy authority"
            )
    elif trigger is ReevaluationTrigger.EXPIRY_RECOMPUTE:
        if predecessor.disposition is not Disposition.EXPIRED:
            raise LiveDecisionDispositionError(
                "EXPIRY_RECOMPUTE requires an EXPIRED predecessor"
            )
        if not evidence_changed:
            raise LiveDecisionDispositionError(
                "EXPIRY_RECOMPUTE requires materially changed bound evidence"
            )
    elif trigger is ReevaluationTrigger.SAFETY_REVALIDATION:
        if predecessor.disposition is not Disposition.HALT_SAFETY:
            raise LiveDecisionDispositionError(
                "SAFETY_REVALIDATION requires a HALT_SAFETY predecessor"
            )
        if not evidence_changed and not policy_changed:
            raise LiveDecisionDispositionError(
                "SAFETY_REVALIDATION requires changed evidence or product policy authority"
            )
    else:  # pragma: no cover - enum exhaustiveness guard
        raise LiveDecisionDispositionError("unsupported reevaluation trigger")

    return predecessor
