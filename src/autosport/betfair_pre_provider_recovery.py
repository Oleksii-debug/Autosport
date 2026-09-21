from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .betfair_supervised_execution import WRITE_ADAPTER_ID, WRITE_ADAPTER_VERSION
from .real_execution_ledger import (
    AttemptState,
    RealExecutionLedger,
    ReconciliationSnapshot,
)
from .supervised_execution import BoundSupervisedExecutionPlan, SupervisedApproval


_PROOF_SCHEMA = "autosport.betfair_pre_provider_no_external_effect"
_PROOF_VERSION = 1
_RESTART_REASON = "process_restart"
_SOURCE_PREFIX = (
    f"{_PROOF_SCHEMA}:v{_PROOF_VERSION}:"
    f"{WRITE_ADAPTER_ID}:v{WRITE_ADAPTER_VERSION}:"
)


class BetfairPreProviderRecoveryError(RuntimeError):
    """The durable history cannot prove the canonical pre-provider safe boundary."""


@dataclass(frozen=True, slots=True)
class BetfairPreProviderRecoveryResult:
    attempt_id: str
    action_id: str
    evidence_id: str
    observed_at: str
    pre_recovery_ledger_sha256: str
    state: AttemptState


def _canonical_digest(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairPreProviderRecoveryError(
            "pre-provider recovery proof is not canonical JSON"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _parse_time(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairPreProviderRecoveryError(f"{name} must be canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairPreProviderRecoveryError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairPreProviderRecoveryError(f"{name} must be timezone-aware")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validated_events(ledger: RealExecutionLedger) -> tuple[object, list[dict[str, object]]]:
    """Read only bytes that the canonical ledger has already fully validated."""

    snapshot = ledger.verified_snapshot()
    events: list[dict[str, object]] = []
    if not snapshot.payload:
        return snapshot, events
    try:
        text = snapshot.payload.decode("utf-8")
        for line in text.splitlines():
            envelope = json.loads(line)
            event = envelope["event"]
            if not isinstance(event, dict):
                raise TypeError("event is not an object")
            events.append(event)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        # verified_snapshot() has already parsed the same bytes. Reaching this branch
        # would mean this projection disagrees with the ledger's canonical decoder.
        raise BetfairPreProviderRecoveryError(
            "validated execution-ledger snapshot cannot be projected"
        ) from exc
    return snapshot, events


def _attempt_events(
    events: list[dict[str, object]], attempt_id: str
) -> list[dict[str, object]]:
    return [event for event in events if event.get("attempt_id") == attempt_id]


def _existing_product_proof(
    *,
    attempt_id: str,
    action_id: str,
    attempt_events: list[dict[str, object]],
) -> BetfairPreProviderRecoveryResult | None:
    matches = [
        event
        for event in attempt_events
        if event.get("event_type") == "RECONCILED_NOT_FOUND"
        and isinstance(event.get("payload"), dict)
        and str(event["payload"].get("source", "")).startswith(_SOURCE_PREFIX)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise BetfairPreProviderRecoveryError(
            "attempt has multiple product-issued pre-provider recovery proofs"
        )
    payload = matches[0]["payload"]
    evidence_id = payload.get("evidence_id")
    observed_at = payload.get("observed_at")
    source = payload.get("source")
    if (
        type(evidence_id) is not str
        or len(evidence_id) != 64
        or any(ch not in "0123456789abcdef" for ch in evidence_id)
        or type(observed_at) is not str
        or type(source) is not str
    ):
        raise BetfairPreProviderRecoveryError(
            "stored pre-provider recovery proof is malformed"
        )
    ledger_sha = source.removeprefix(_SOURCE_PREFIX)
    if len(ledger_sha) != 64 or any(ch not in "0123456789abcdef" for ch in ledger_sha):
        raise BetfairPreProviderRecoveryError(
            "stored pre-provider recovery proof lacks exact ledger identity"
        )
    return BetfairPreProviderRecoveryResult(
        attempt_id=attempt_id,
        action_id=action_id,
        evidence_id=evidence_id,
        observed_at=observed_at,
        pre_recovery_ledger_sha256=ledger_sha,
        state=AttemptState.RECONCILED_NOT_FOUND,
    )


def recover_betfair_pre_provider_attempt(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    attempt_id: str,
    observed_at: str | None = None,
) -> BetfairPreProviderRecoveryResult:
    """Release exactly one canonical Betfair retry after a pre-provider restart crash.

    The normal restart path first calls ``RealExecutionLedger.recover_uncertain()``.
    That intentionally turns unresolved attempts into UNKNOWN. For the canonical
    Betfair writer there is one narrower state in which UNKNOWN can be resolved
    without provider readback: the durable attempt was reserved, but the process
    died before the deterministic provider order reference was bound. The canonical
    Betfair writer binds that reference and marks the attempt submitted *before*
    transport, so an exact history containing only reservation + restart UNKNOWN
    proves that no provider call was reachable.

    This function does not weaken generic UNKNOWN semantics. Any provider reference,
    submission, provider evidence, acknowledgement, or reconciliation event requires
    the existing verified-provider readback route.
    """

    if type(ledger) is not RealExecutionLedger:
        raise BetfairPreProviderRecoveryError(
            "recovery requires exact canonical RealExecutionLedger"
        )
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairPreProviderRecoveryError(
            "recovery requires exact BoundSupervisedExecutionPlan"
        )
    if type(approval) is not SupervisedApproval:
        raise BetfairPreProviderRecoveryError(
            "recovery requires exact SupervisedApproval"
        )
    if type(attempt_id) is not str or not attempt_id or attempt_id != attempt_id.strip():
        raise BetfairPreProviderRecoveryError("attempt_id must be canonical text")

    bound.verify_binding()
    plan = bound.execution_plan
    if (
        approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != plan.approval_id
    ):
        raise BetfairPreProviderRecoveryError(
            "approval identity does not match the bound execution plan"
        )
    if not ledger.supervised_approval_is_active(
        plan_id=plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
    ):
        raise BetfairPreProviderRecoveryError(
            "durable supervised approval is missing or revoked"
        )

    try:
        saga = ledger.saga(plan.plan_id)
    except KeyError as exc:
        raise BetfairPreProviderRecoveryError(
            "bound execution plan is not durably reserved"
        ) from exc
    if saga.plan_fingerprint != plan.fingerprint:
        raise BetfairPreProviderRecoveryError(
            "durable execution-plan fingerprint mismatch"
        )
    action_id = saga.attempt_action_ids.get(attempt_id)
    if action_id is None:
        raise BetfairPreProviderRecoveryError(
            "attempt does not belong to the bound execution plan"
        )
    action = bound.action_for(action_id)
    if action.bookmaker_id != "betfair":
        raise BetfairPreProviderRecoveryError(
            "pre-provider recovery is restricted to canonical Betfair execution"
        )

    snapshot, events = _validated_events(ledger)
    attempt_events = _attempt_events(events, attempt_id)
    prior_product_proof = _existing_product_proof(
        attempt_id=attempt_id,
        action_id=action_id,
        attempt_events=attempt_events,
    )
    state = saga.attempts[attempt_id]
    if prior_product_proof is not None:
        if state is not AttemptState.RECONCILED_NOT_FOUND:
            raise BetfairPreProviderRecoveryError(
                "stored product proof does not match terminal attempt state"
            )
        return prior_product_proof
    if state is not AttemptState.UNKNOWN:
        raise BetfairPreProviderRecoveryError(
            "pre-provider restart recovery requires UNKNOWN attempt"
        )

    # The exact event sequence is the safety proof. In particular, a durable
    # provider-order-reference event is already a provider-effect boundary even
    # while the state machine still labels the attempt RESERVED.
    event_types = [event.get("event_type") for event in attempt_events]
    if event_types != ["ATTEMPT_RESERVED", "ATTEMPT_UNKNOWN"]:
        raise BetfairPreProviderRecoveryError(
            "attempt crossed a provider/submission/evidence boundary; verified readback is required"
        )
    unknown_payload = attempt_events[-1].get("payload")
    if not isinstance(unknown_payload, dict) or set(unknown_payload) != {
        "reason",
        "observed_at",
    }:
        raise BetfairPreProviderRecoveryError(
            "restart UNKNOWN event has noncanonical payload"
        )
    if unknown_payload.get("reason") != _RESTART_REASON:
        raise BetfairPreProviderRecoveryError(
            "UNKNOWN attempt was not produced by canonical process-restart recovery"
        )
    unknown_at = unknown_payload.get("observed_at")
    unknown_time = _parse_time(unknown_at, "restart observed_at")

    if ledger.provider_order_reference(
        attempt_id=attempt_id,
        provider_id=action.bookmaker_id,
    ) is not None:
        raise BetfairPreProviderRecoveryError(
            "provider order reference exists; verified provider readback is required"
        )
    if ledger.provider_evidence_binding(attempt_id) is not None:
        raise BetfairPreProviderRecoveryError(
            "provider evidence exists; verified provider readback is required"
        )

    recovery_at = observed_at or _now()
    recovery_time = _parse_time(recovery_at, "recovery observed_at")
    if recovery_time <= unknown_time:
        raise BetfairPreProviderRecoveryError(
            "recovery proof must be newer than the restart uncertainty boundary"
        )

    proof = {
        "schema": _PROOF_SCHEMA,
        "schema_version": _PROOF_VERSION,
        "write_adapter_id": WRITE_ADAPTER_ID,
        "write_adapter_version": WRITE_ADAPTER_VERSION,
        "ledger_sha256": snapshot.sha256,
        "plan_id": plan.plan_id,
        "plan_fingerprint": plan.fingerprint,
        "action_id": action_id,
        "attempt_id": attempt_id,
        "approval_id": approval.ledger_identity,
        "approval_fingerprint": approval.fingerprint,
        "restart_unknown_event_id": attempt_events[-1].get("event_id"),
        "restart_observed_at": unknown_at,
        "no_provider_order_reference": True,
        "no_submission": True,
        "no_provider_evidence": True,
        "no_external_acknowledgement": True,
        "no_reconciliation": True,
    }
    evidence_id = _canonical_digest(proof)
    source = f"{_SOURCE_PREFIX}{snapshot.sha256}"

    # ReconciliationSnapshot is used here as a durable no-external-effect fact, not
    # as fabricated provider readback. Its source binds the exact validated ledger
    # snapshot and the product-owned Betfair sequencing proof above.
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id=attempt_id,
            evidence_id=evidence_id,
            observed_at=recovery_at,
            external_effect_found=False,
            source=source,
        )
    )
    if ledger.attempt_state(attempt_id) is not AttemptState.RECONCILED_NOT_FOUND:
        raise BetfairPreProviderRecoveryError(
            "pre-provider recovery did not reach durable terminal state"
        )
    if not ledger.can_retry_action(plan_id=plan.plan_id, action_id=action_id):
        raise BetfairPreProviderRecoveryError(
            "pre-provider recovery did not release exactly one retry"
        )
    return BetfairPreProviderRecoveryResult(
        attempt_id=attempt_id,
        action_id=action_id,
        evidence_id=evidence_id,
        observed_at=recovery_at,
        pre_recovery_ledger_sha256=snapshot.sha256,
        state=AttemptState.RECONCILED_NOT_FOUND,
    )
