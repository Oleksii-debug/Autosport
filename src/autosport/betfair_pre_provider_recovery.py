from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .betfair_supervised_execution import WRITE_ADAPTER_ID, WRITE_ADAPTER_VERSION
from .real_execution_ledger import AttemptState, ExecutionAttempt, RealExecutionLedger
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedApproval,
    SupervisedExecutionError,
    _canonical_supervised_ledger_dispatch,
    _require_durable_approval,
    _require_reserved,
    begin_supervised_attempt,
)


_PROOF_SCHEMA = "autosport.betfair_pre_provider_no_external_effect"
_PROOF_VERSION = 2
_SOURCE_PREFIX = (
    f"{_PROOF_SCHEMA}:v{_PROOF_VERSION}:"
    f"{WRITE_ADAPTER_ID}:v{WRITE_ADAPTER_VERSION}:"
)

_LEDGER_TYPE = RealExecutionLedger
_DISPATCH = _canonical_supervised_ledger_dispatch
_DISPATCH_CODE = getattr(_DISPATCH, "__code__", None)
_REQUIRE_RESERVED = _require_reserved
_REQUIRE_RESERVED_CODE = getattr(_REQUIRE_RESERVED, "__code__", None)
_REQUIRE_DURABLE_APPROVAL = _require_durable_approval
_REQUIRE_DURABLE_APPROVAL_CODE = getattr(
    _REQUIRE_DURABLE_APPROVAL,
    "__code__",
    None,
)
_BEGIN_SUPERVISED_ATTEMPT = begin_supervised_attempt
_BEGIN_SUPERVISED_ATTEMPT_CODE = getattr(
    _BEGIN_SUPERVISED_ATTEMPT,
    "__code__",
    None,
)


class BetfairPreProviderRecoveryError(RuntimeError):
    """The durable history cannot prove the canonical pre-provider safe boundary."""


@dataclass(frozen=True, slots=True)
class BetfairPreProviderRecoveryResult:
    attempt_id: str
    action_id: str
    evidence_id: str
    observed_at: str
    pre_authority_snapshot_sha256: str
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


def _require_product_graph() -> None:
    checks = (
        (
            globals().get("_canonical_supervised_ledger_dispatch"),
            _DISPATCH,
            _DISPATCH_CODE,
        ),
        (
            globals().get("_require_reserved"),
            _REQUIRE_RESERVED,
            _REQUIRE_RESERVED_CODE,
        ),
        (
            globals().get("_require_durable_approval"),
            _REQUIRE_DURABLE_APPROVAL,
            _REQUIRE_DURABLE_APPROVAL_CODE,
        ),
        (
            globals().get("begin_supervised_attempt"),
            _BEGIN_SUPERVISED_ATTEMPT,
            _BEGIN_SUPERVISED_ATTEMPT_CODE,
        ),
    )
    if globals().get("RealExecutionLedger") is not _LEDGER_TYPE:
        raise BetfairPreProviderRecoveryError(
            "canonical execution-ledger type changed"
        )
    for current, expected, expected_code in checks:
        if (
            current is not expected
            or (
                expected_code is not None
                and getattr(current, "__code__", None) is not expected_code
            )
        ):
            raise BetfairPreProviderRecoveryError(
                "canonical supervised recovery authority graph changed"
            )


def _proof_document(
    *,
    snapshot_sha256: str,
    plan_id: str,
    plan_fingerprint: str,
    action_id: str,
    attempt_id: str,
    approval_id: str,
    approval_fingerprint: str,
    restart_unknown_observed_at: str,
) -> dict[str, object]:
    return {
        "schema": _PROOF_SCHEMA,
        "schema_version": _PROOF_VERSION,
        "write_adapter_id": WRITE_ADAPTER_ID,
        "write_adapter_version": WRITE_ADAPTER_VERSION,
        "pre_authority_snapshot_sha256": snapshot_sha256,
        "plan_id": plan_id,
        "plan_fingerprint": plan_fingerprint,
        "action_id": action_id,
        "attempt_id": attempt_id,
        "approval_id": approval_id,
        "approval_fingerprint": approval_fingerprint,
        "restart_unknown_observed_at": restart_unknown_observed_at,
        "no_provider_order_reference": True,
        "no_submission": True,
        "no_provider_evidence": True,
        "no_external_acknowledgement": True,
        "no_reconciliation": True,
    }


def _require_bound_identity(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
) -> None:
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairPreProviderRecoveryError(
            "recovery requires exact BoundSupervisedExecutionPlan"
        )
    if type(approval) is not SupervisedApproval:
        raise BetfairPreProviderRecoveryError(
            "recovery requires exact SupervisedApproval"
        )
    try:
        bound.verify_binding()
    except (AttributeError, TypeError, ValueError, SupervisedExecutionError) as exc:
        raise BetfairPreProviderRecoveryError(
            "bound execution plan is not structurally valid"
        ) from exc
    if (
        approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != bound.execution_plan.approval_id
    ):
        raise BetfairPreProviderRecoveryError(
            "approval identity does not match the bound execution plan"
        )


def _attempt_view(verified_view, attempt_id: str):
    matches = [
        attempt
        for attempt in verified_view.attempts
        if attempt.attempt.attempt_id == attempt_id
    ]
    if len(matches) != 1:
        raise BetfairPreProviderRecoveryError(
            "attempt does not belong uniquely to the bound execution plan"
        )
    return matches[0]


def _stored_result(
    *,
    attempt_id: str,
    action_id: str,
    authority: dict[str, str],
    state: AttemptState,
) -> BetfairPreProviderRecoveryResult:
    return BetfairPreProviderRecoveryResult(
        attempt_id=attempt_id,
        action_id=action_id,
        evidence_id=authority["evidence_id"],
        observed_at=authority["authorized_at"],
        pre_authority_snapshot_sha256=authority[
            "pre_authority_snapshot_sha256"
        ],
        state=state,
    )


def recover_betfair_pre_provider_attempt(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    attempt_id: str,
    observed_at: str | None = None,
) -> BetfairPreProviderRecoveryResult:
    """Issue durable product no-effect authority for one true pre-provider orphan.

    The ordinary restart path first promotes unresolved RESERVED/SUBMITTED attempts
    to UNKNOWN. This product authority is deliberately narrower: it is issuable only
    when the exact Betfair attempt never crossed provider-reference, submission,
    provider-evidence, acknowledgement or reconciliation boundaries.

    A generic RECONCILED_NOT_FOUND record is never enough. The canonical execution
    ledger writes a separate product-issued authority event first, then terminalizes
    the attempt as RECONCILED_NOT_FOUND. If the process dies between those fsyncs,
    retry remains blocked and exact replay may only finish the matching terminal
    diagnostic.
    """

    _require_product_graph()
    if type(ledger) is not _LEDGER_TYPE:
        raise BetfairPreProviderRecoveryError(
            "recovery requires exact canonical RealExecutionLedger"
        )
    if type(attempt_id) is not str or not attempt_id or attempt_id != attempt_id.strip():
        raise BetfairPreProviderRecoveryError("attempt_id must be canonical text")
    _require_bound_identity(bound, approval)

    try:
        _REQUIRE_RESERVED(ledger, bound)
        _REQUIRE_DURABLE_APPROVAL(ledger, bound, approval)
        methods = _DISPATCH(ledger)
    except SupervisedExecutionError as exc:
        raise BetfairPreProviderRecoveryError(
            "bound plan/approval is not current durable product authority"
        ) from exc

    plan = bound.execution_plan
    view = methods["verified_execution_view"](plan.plan_id)
    if view.plan_fingerprint != plan.fingerprint:
        raise BetfairPreProviderRecoveryError(
            "durable execution-plan fingerprint mismatch"
        )
    attempt = _attempt_view(view, attempt_id)
    action = attempt.action
    if action.bookmaker_id != "betfair":
        raise BetfairPreProviderRecoveryError(
            "pre-provider recovery is restricted to canonical Betfair execution"
        )

    existing = methods["betfair_pre_provider_no_effect_authority"](attempt_id)
    if existing is not None:
        required = {
            "evidence_id",
            "authorized_at",
            "approval_id",
            "approval_fingerprint",
            "write_adapter_id",
            "write_adapter_version",
            "pre_authority_snapshot_sha256",
            "source",
        }
        if set(existing) != required:
            raise BetfairPreProviderRecoveryError(
                "stored pre-provider no-effect authority is malformed"
            )
        if (
            existing["approval_id"] != approval.ledger_identity
            or existing["approval_fingerprint"] != approval.fingerprint
            or existing["write_adapter_id"] != WRITE_ADAPTER_ID
            or existing["write_adapter_version"] != WRITE_ADAPTER_VERSION
            or existing["source"]
            != f"{_SOURCE_PREFIX}{existing['pre_authority_snapshot_sha256']}"
        ):
            raise BetfairPreProviderRecoveryError(
                "stored pre-provider no-effect authority identity mismatch"
            )
        try:
            methods["_bind_betfair_pre_provider_no_effect"](
                attempt_id=attempt_id,
                evidence_id=existing["evidence_id"],
                observed_at=existing["authorized_at"],
                source=existing["source"],
                approval_id=existing["approval_id"],
                approval_fingerprint=existing["approval_fingerprint"],
                write_adapter_id=existing["write_adapter_id"],
                write_adapter_version=existing["write_adapter_version"],
                expected_snapshot_sha256=existing[
                    "pre_authority_snapshot_sha256"
                ],
            )
        except (ValueError, RuntimeError) as exc:
            raise BetfairPreProviderRecoveryError(
                "stored pre-provider no-effect authority cannot be completed"
            ) from exc
        final = methods["verified_execution_view"](plan.plan_id)
        final_attempt = _attempt_view(final, attempt_id)
        if final_attempt.state is not AttemptState.RECONCILED_NOT_FOUND:
            raise BetfairPreProviderRecoveryError(
                "pre-provider no-effect authority did not terminalize the attempt"
            )
        return _stored_result(
            attempt_id=attempt_id,
            action_id=action.action_id,
            authority=existing,
            state=final_attempt.state,
        )

    if attempt.state is not AttemptState.UNKNOWN:
        raise BetfairPreProviderRecoveryError(
            "new pre-provider no-effect authority requires UNKNOWN attempt"
        )
    if attempt.unknown_reason != "process_restart":
        raise BetfairPreProviderRecoveryError(
            "UNKNOWN attempt was not produced by canonical process-restart recovery"
        )
    if (
        attempt.submitted_at is not None
        or attempt.provider_order_ref is not None
        or attempt.provider_evidence is not None
        or attempt.acknowledgement is not None
        or attempt.found_reconciliations
        or attempt.not_found_reconciliation is not None
    ):
        raise BetfairPreProviderRecoveryError(
            "attempt crossed a provider/submission/evidence boundary; verified readback is required"
        )
    if attempt.unknown_observed_at is None:
        raise BetfairPreProviderRecoveryError(
            "restart UNKNOWN attempt lacks observed_at"
        )

    actual_observed_at = observed_at or _now()
    recovery_time = _parse_time(actual_observed_at, "recovery observed_at")
    unknown_time = _parse_time(
        attempt.unknown_observed_at,
        "restart observed_at",
    )
    if recovery_time <= unknown_time:
        raise BetfairPreProviderRecoveryError(
            "recovery authority must be newer than restart uncertainty boundary"
        )
    if recovery_time >= _parse_time(approval.expires_at, "approval expires_at"):
        raise BetfairPreProviderRecoveryError(
            "recovery authority cannot be issued after approval expiry"
        )

    proof = _proof_document(
        snapshot_sha256=view.snapshot_sha256,
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        action_id=action.action_id,
        attempt_id=attempt_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        restart_unknown_observed_at=attempt.unknown_observed_at,
    )
    evidence_id = _canonical_digest(proof)
    source = f"{_SOURCE_PREFIX}{view.snapshot_sha256}"
    try:
        methods["_bind_betfair_pre_provider_no_effect"](
            attempt_id=attempt_id,
            evidence_id=evidence_id,
            observed_at=actual_observed_at,
            source=source,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
            write_adapter_id=WRITE_ADAPTER_ID,
            write_adapter_version=WRITE_ADAPTER_VERSION,
            expected_snapshot_sha256=view.snapshot_sha256,
        )
    except (ValueError, RuntimeError) as exc:
        raise BetfairPreProviderRecoveryError(
            "durable pre-provider no-effect authority issuance failed"
        ) from exc

    final = methods["verified_execution_view"](plan.plan_id)
    final_attempt = _attempt_view(final, attempt_id)
    authority = methods["betfair_pre_provider_no_effect_authority"](attempt_id)
    if authority is None or final_attempt.state is not AttemptState.RECONCILED_NOT_FOUND:
        raise BetfairPreProviderRecoveryError(
            "durable pre-provider no-effect authority is incomplete"
        )
    if authority["evidence_id"] != evidence_id:
        raise BetfairPreProviderRecoveryError(
            "durable pre-provider no-effect authority identity changed"
        )
    return _stored_result(
        attempt_id=attempt_id,
        action_id=action.action_id,
        authority=authority,
        state=final_attempt.state,
    )


def begin_betfair_pre_provider_retry_attempt(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    previous_attempt_id: str,
    retry_attempt_id: str,
) -> ExecutionAttempt:
    """Consume one durable pre-provider no-effect authority for one new attempt."""

    _require_product_graph()
    if type(ledger) is not _LEDGER_TYPE:
        raise BetfairPreProviderRecoveryError(
            "retry requires exact canonical RealExecutionLedger"
        )
    _require_bound_identity(bound, approval)
    if (
        type(previous_attempt_id) is not str
        or not previous_attempt_id
        or previous_attempt_id != previous_attempt_id.strip()
        or type(retry_attempt_id) is not str
        or not retry_attempt_id
        or retry_attempt_id != retry_attempt_id.strip()
    ):
        raise BetfairPreProviderRecoveryError(
            "attempt identities must be canonical text"
        )

    try:
        _REQUIRE_RESERVED(ledger, bound)
        _REQUIRE_DURABLE_APPROVAL(ledger, bound, approval)
        methods = _DISPATCH(ledger)
    except SupervisedExecutionError as exc:
        raise BetfairPreProviderRecoveryError(
            "bound plan/approval is not current durable product authority"
        ) from exc

    authority = methods["betfair_pre_provider_no_effect_authority"](
        previous_attempt_id
    )
    if authority is None:
        raise BetfairPreProviderRecoveryError(
            "prior attempt lacks product-issued pre-provider no-effect authority"
        )
    saga = methods["saga"](bound.execution_plan.plan_id)
    action_id = saga.attempt_action_ids.get(previous_attempt_id)
    if action_id is None:
        raise BetfairPreProviderRecoveryError(
            "prior attempt does not belong to bound execution plan"
        )
    action = bound.action_for(action_id)
    if action.bookmaker_id != "betfair":
        raise BetfairPreProviderRecoveryError(
            "pre-provider retry authority is restricted to Betfair"
        )
    try:
        return _BEGIN_SUPERVISED_ATTEMPT(
            ledger,
            bound,
            approval,
            action_id=action_id,
            attempt_id=retry_attempt_id,
            product_no_effect_authority_id=authority["evidence_id"],
        )
    except (SupervisedExecutionError, RuntimeError, ValueError) as exc:
        raise BetfairPreProviderRecoveryError(
            "product-issued pre-provider no-effect authority cannot admit retry"
        ) from exc
