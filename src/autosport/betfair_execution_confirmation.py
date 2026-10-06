"""Durable operator confirmation for one exact Betfair final send.

This composes the generic supervised-confirmation journal with the exact
plan/action/attempt that may reach Betfair ``placeOrders``.  It performs no
provider I/O.  Returned values are audit evidence, never transferable execution
capabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .real_execution_ledger import ExecutionAction
from . import supervised_confirmation as _confirmation
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedApproval,
    SupervisedExecutionError,
)


CONFIRMATION_FILENAME = "supervised-confirmation.jsonl"
_REVIEW_PAYLOAD_DOMAIN = "autosport.supervised-review-payload.v1"
_DECISION_DOMAIN = "autosport.betfair-final-send-decision.v1"
_DECISION_ID_DOMAIN = "autosport.betfair-final-send-decision-id.v1"
_CONSUMER_DOMAIN = "autosport.betfair-final-send-consumer.v1"
_WITNESS_DOMAIN = "autosport.betfair-final-send-confirmation-witness.v1"

_AUTHORITY_TYPE = _confirmation.SupervisedConfirmationAuthority
_AUTHORITY_INIT = _AUTHORITY_TYPE.__init__
_AUTHORITY_INIT_CODE = _AUTHORITY_INIT.__code__
_RESOLVE_BINDING = _AUTHORITY_TYPE.resolve_receipt_binding
_RESOLVE_BINDING_CODE = _RESOLVE_BINDING.__code__
_CONSUME_RECEIPT = _AUTHORITY_TYPE.consume_receipt
_CONSUME_RECEIPT_CODE = _CONSUME_RECEIPT.__code__
_BINDING_TYPE = _confirmation.SupervisedConfirmationBinding
_CONFIRMATION_ERROR = _confirmation.SupervisedConfirmationError


class BetfairExecutionConfirmationError(RuntimeError):
    """Durable operator confirmation does not authorize this exact send."""


def _authority_graph_unchanged() -> bool:
    return (
        _confirmation.SupervisedConfirmationAuthority is _AUTHORITY_TYPE
        and _confirmation.SupervisedConfirmationBinding is _BINDING_TYPE
        and _confirmation.SupervisedConfirmationError is _CONFIRMATION_ERROR
        and _AUTHORITY_TYPE.__init__ is _AUTHORITY_INIT
        and getattr(_AUTHORITY_INIT, "__code__", None) is _AUTHORITY_INIT_CODE
        and _AUTHORITY_TYPE.resolve_receipt_binding is _RESOLVE_BINDING
        and getattr(_RESOLVE_BINDING, "__code__", None) is _RESOLVE_BINDING_CODE
        and _AUTHORITY_TYPE.consume_receipt is _CONSUME_RECEIPT
        and getattr(_CONSUME_RECEIPT, "__code__", None) is _CONSUME_RECEIPT_CODE
    )


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise BetfairExecutionConfirmationError(
            f"{name} must be non-empty canonical text"
        )
    return value


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(character not in "0123456789abcdef" for character in raw):
        raise BetfairExecutionConfirmationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return raw


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairExecutionConfirmationError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairExecutionConfirmationError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairExecutionConfirmationError(
            "Betfair confirmation evidence is not canonical JSON"
        ) from exc


def _domain_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("utf-8") + b"\0" + _canonical_bytes(value)
    ).hexdigest()


def _require_bound_action(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
) -> ExecutionAction:
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairExecutionConfirmationError(
            "bound must be exact BoundSupervisedExecutionPlan"
        )
    if type(approval) is not SupervisedApproval:
        raise BetfairExecutionConfirmationError(
            "approval must be exact SupervisedApproval"
        )
    action_id = _text(action_id, "action_id", max_length=256)
    try:
        bound.verify_binding()
        action = bound.action_for(action_id)
    except (SupervisedExecutionError, ValueError) as exc:
        raise BetfairExecutionConfirmationError(
            "bound supervised execution plan is not authoritative"
        ) from exc
    if action.bookmaker_id != "betfair":
        raise BetfairExecutionConfirmationError(
            "execution action bookmaker is not exact Betfair authority"
        )
    if (
        approval.portfolio_plan_sha256 != bound.portfolio_plan_sha256
        or approval.intent_id != bound.intent_id
        or approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != bound.execution_plan.approval_id
    ):
        raise BetfairExecutionConfirmationError(
            "SupervisedApproval does not bind the exact supervised execution plan"
        )
    return action


def _decision_material(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action: ExecutionAction,
    *,
    attempt_id: str,
    risk_evidence_sha256: str,
) -> dict[str, object]:
    return {
        "schema": "autosport.betfair_final_send_decision",
        "schema_version": 1,
        "execution_plan_id": bound.execution_plan.plan_id,
        "execution_plan_sha256": bound.execution_plan.fingerprint,
        "execution_decision_id": bound.execution_plan.decision_id,
        "portfolio_plan_sha256": bound.portfolio_plan_sha256,
        "economic_goal_contract_sha256": bound.economic_goal_contract_sha256,
        "intent_id": bound.intent_id,
        "intent_sha256": bound.intent_sha256,
        "approval_fingerprint": approval.fingerprint,
        "approval_evidence_sha256": approval.evidence_sha256,
        "risk_evidence_sha256": risk_evidence_sha256,
        "action_id": action.action_id,
        "attempt_id": attempt_id,
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "action": action.to_dict(),
    }


@dataclass(frozen=True, slots=True)
class BetfairExecutionConfirmationSpec:
    review_id: str
    decision_id: str
    decision_sha256: str
    bookmaker_id: str
    account_id: str
    approval_evidence_sha256: str
    risk_evidence_sha256: str
    review_payload: Mapping[str, object]


def betfair_execution_confirmation_spec(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    attempt_id: str,
    review_id: str,
    risk_evidence_sha256: str,
) -> BetfairExecutionConfirmationSpec:
    """Return the exact review material; this has no durable side effect."""

    action = _require_bound_action(bound, approval, action_id=action_id)
    attempt_id = _text(attempt_id, "attempt_id", max_length=256)
    review_id = _text(review_id, "review_id", max_length=256)
    risk_evidence_sha256 = _sha(risk_evidence_sha256, "risk_evidence_sha256")
    material = _decision_material(
        bound,
        approval,
        action,
        attempt_id=attempt_id,
        risk_evidence_sha256=risk_evidence_sha256,
    )
    decision_sha256 = _domain_digest(_DECISION_DOMAIN, material)
    decision_identity = {
        "schema": "autosport.betfair_final_send_identity",
        "schema_version": 1,
        **{
            key: value
            for key, value in material.items()
            if key not in {"schema", "schema_version", "risk_evidence_sha256"}
        },
    }
    decision_id = (
        "betfair-final-send-v1-"
        + _domain_digest(_DECISION_ID_DOMAIN, decision_identity)
    )
    review_payload = {
        "schema": "autosport.betfair_final_send_review",
        "schema_version": 1,
        **{
            key: value
            for key, value in material.items()
            if key not in {"schema", "schema_version"}
        },
        "decision_id": decision_id,
        "decision_sha256": decision_sha256,
    }
    return BetfairExecutionConfirmationSpec(
        review_id=review_id,
        decision_id=decision_id,
        decision_sha256=decision_sha256,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        approval_evidence_sha256=approval.evidence_sha256,
        risk_evidence_sha256=risk_evidence_sha256,
        review_payload=review_payload,
    )


def _require_confirmation_binding(
    binding: _confirmation.SupervisedConfirmationBinding,
    *,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action: ExecutionAction,
    attempt_id: str,
    submitted_at: str,
) -> None:
    if type(binding) is not _BINDING_TYPE:
        raise BetfairExecutionConfirmationError(
            "confirmation binding must come from canonical durable authority"
        )
    review = binding.review
    receipt = binding.receipt
    spec = betfair_execution_confirmation_spec(
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        review_id=review.review_id,
        risk_evidence_sha256=review.risk_evidence_sha256,
    )
    if review.decision_id != spec.decision_id or review.decision_sha256 != spec.decision_sha256:
        raise BetfairExecutionConfirmationError(
            "operator confirmation does not bind this exact Betfair attempt decision"
        )
    if review.bookmaker_id != action.bookmaker_id or review.account_id != action.account_id:
        raise BetfairExecutionConfirmationError(
            "operator confirmation bookmaker/account does not match execution action"
        )
    if review.approval_evidence_sha256 != approval.evidence_sha256:
        raise BetfairExecutionConfirmationError(
            "operator confirmation approval evidence does not match SupervisedApproval"
        )
    expected_payload_sha256 = _domain_digest(
        _REVIEW_PAYLOAD_DOMAIN,
        spec.review_payload,
    )
    if review.review_payload_sha256 != expected_payload_sha256:
        raise BetfairExecutionConfirmationError(
            "operator confirmation review payload does not match canonical final-send review"
        )
    try:
        approval.require_active(review.reviewed_at)
        approval.require_active(submitted_at)
    except SupervisedExecutionError as exc:
        raise BetfairExecutionConfirmationError(
            "SupervisedApproval is not active across review and final send"
        ) from exc
    submitted = _instant(submitted_at, "submitted_at")
    if _instant(review.expires_at, "review.expires_at") > _instant(
        approval.expires_at,
        "approval.expires_at",
    ):
        raise BetfairExecutionConfirmationError(
            "operator confirmation lifetime exceeds underlying SupervisedApproval"
        )
    if _instant(receipt.confirmed_at, "receipt.confirmed_at") > submitted:
        raise BetfairExecutionConfirmationError(
            "operator confirmation was recorded after final-send admission"
        )
    if (
        receipt.review_id != review.review_id
        or receipt.review_sha256 != review.review_sha256
        or receipt.decision_id != review.decision_id
        or receipt.bookmaker_id != review.bookmaker_id
        or receipt.account_id != review.account_id
    ):
        raise BetfairExecutionConfirmationError(
            "operator confirmation receipt/review identity is inconsistent"
        )


def _consumer_key(
    *,
    bound: BoundSupervisedExecutionPlan,
    action: ExecutionAction,
    attempt_id: str,
    request_sha256: str,
    review_sha256: str,
) -> str:
    digest = _domain_digest(
        _CONSUMER_DOMAIN,
        {
            "execution_plan_id": bound.execution_plan.plan_id,
            "action_id": action.action_id,
            "attempt_id": attempt_id,
            "intent_id": bound.intent_id,
            "intent_sha256": bound.intent_sha256,
            "request_sha256": request_sha256,
            "review_sha256": review_sha256,
        },
    )
    return f"betfair-final-send:v1:{digest}"


@dataclass(frozen=True, slots=True)
class BetfairExecutionConfirmationWitness:
    execution_plan_id: str
    action_id: str
    attempt_id: str
    intent_sha256: str
    request_sha256: str
    review_sha256: str
    receipt_id: str
    receipt_sha256: str
    confirmed_at: str
    consumed_at: str
    consumer_key: str
    evidence_id: str

    def __post_init__(self) -> None:
        for name in ("execution_plan_id", "action_id", "attempt_id", "consumer_key"):
            _text(getattr(self, name), name)
        for name in (
            "intent_sha256",
            "request_sha256",
            "review_sha256",
            "receipt_id",
            "receipt_sha256",
            "evidence_id",
        ):
            _sha(getattr(self, name), name)
        _instant(self.confirmed_at, "confirmed_at")
        _instant(self.consumed_at, "consumed_at")
        if self.evidence_id != _domain_digest(_WITNESS_DOMAIN, self._material()):
            raise BetfairExecutionConfirmationError(
                "Betfair confirmation witness evidence_id mismatch"
            )

    def _material(self) -> dict[str, str]:
        return {
            "execution_plan_id": self.execution_plan_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "intent_sha256": self.intent_sha256,
            "request_sha256": self.request_sha256,
            "review_sha256": self.review_sha256,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "confirmed_at": self.confirmed_at,
            "consumed_at": self.consumed_at,
            "consumer_key": self.consumer_key,
        }




def require_consumed_betfair_execution_confirmation_current(
    execution_workspace: Path,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    attempt_id: str,
    receipt_id: str,
    expected_review_sha256: str,
    request_sha256: str,
    submitted_at: str,
    current_at: str,
) -> str:
    """Re-read consumed confirmation and prove it is still current before POST."""

    if type(execution_workspace) is not Path:
        raise BetfairExecutionConfirmationError(
            "execution_workspace must be exact Path"
        )
    workspace = execution_workspace.resolve()
    action = _require_bound_action(bound, approval, action_id=action_id)
    attempt_id = _text(attempt_id, "attempt_id", max_length=256)
    receipt_id = _sha(receipt_id, "receipt_id")
    expected_review_sha256 = _sha(expected_review_sha256, "expected_review_sha256")
    request_sha256 = _sha(request_sha256, "request_sha256")
    submitted_instant = _instant(submitted_at, "submitted_at")
    current_instant = _instant(current_at, "current_at")
    if current_instant < submitted_instant:
        raise BetfairExecutionConfirmationError(
            "trusted final-send clock moved backwards after confirmation consumption"
        )
    if not _authority_graph_unchanged():
        raise BetfairExecutionConfirmationError(
            "supervised confirmation authority executable graph changed"
        )
    try:
        authority = _AUTHORITY_TYPE(
            workspace / CONFIRMATION_FILENAME,
            clock=lambda: current_instant,
        )
        binding = _RESOLVE_BINDING(
            authority,
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=False,
        )
    except _CONFIRMATION_ERROR as exc:
        raise BetfairExecutionConfirmationError(
            "durable Betfair operator confirmation cannot be revalidated"
        ) from exc
    if not _authority_graph_unchanged():
        raise BetfairExecutionConfirmationError(
            "supervised confirmation authority changed during final-send revalidation"
        )
    _require_confirmation_binding(
        binding,
        bound=bound,
        approval=approval,
        action=action,
        attempt_id=attempt_id,
        submitted_at=submitted_at,
    )
    consumer_key = _consumer_key(
        bound=bound,
        action=action,
        attempt_id=attempt_id,
        request_sha256=request_sha256,
        review_sha256=binding.review.review_sha256,
    )
    if (
        binding.receipt.consumed_by != consumer_key
        or binding.receipt.consumed_at is None
        or _instant(binding.receipt.consumed_at, "consumed_at") != submitted_instant
    ):
        raise BetfairExecutionConfirmationError(
            "durable Betfair operator confirmation consumption changed"
        )
    expires_at = _instant(binding.review.expires_at, "review.expires_at")
    if current_instant >= expires_at:
        raise BetfairExecutionConfirmationError(
            "durable Betfair operator confirmation expired before provider send"
        )
    return binding.review.expires_at

def consume_betfair_execution_confirmation(
    execution_workspace: Path,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    attempt_id: str,
    receipt_id: str,
    expected_review_sha256: str,
    request_sha256: str,
    submitted_at: str,
) -> BetfairExecutionConfirmationWitness:
    """Consume one exact receipt at the already-durable SUBMITTED instant.

    The caller of this authority must derive ``submitted_at`` and
    ``request_sha256`` from the verified execution ledger after the SUBMITTED
    transition.  This keeps confirmation expiry in the same causal clock domain
    as the irreversible provider-send boundary and avoids a second wall-clock
    trust root.
    """

    if type(execution_workspace) is not Path:
        raise BetfairExecutionConfirmationError(
            "execution_workspace must be exact Path"
        )
    workspace = execution_workspace.resolve()
    action = _require_bound_action(bound, approval, action_id=action_id)
    attempt_id = _text(attempt_id, "attempt_id", max_length=256)
    receipt_id = _sha(receipt_id, "receipt_id")
    expected_review_sha256 = _sha(expected_review_sha256, "expected_review_sha256")
    request_sha256 = _sha(request_sha256, "request_sha256")
    submitted_instant = _instant(submitted_at, "submitted_at")
    if not _authority_graph_unchanged():
        raise BetfairExecutionConfirmationError(
            "supervised confirmation authority executable graph changed"
        )
    authority_path = workspace / CONFIRMATION_FILENAME
    try:
        authority = _AUTHORITY_TYPE(authority_path, clock=lambda: submitted_instant)
        before = _RESOLVE_BINDING(
            authority,
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=True,
        )
        _require_confirmation_binding(
            before,
            bound=bound,
            approval=approval,
            action=action,
            attempt_id=attempt_id,
            submitted_at=submitted_at,
        )
        consumer_key = _consumer_key(
            bound=bound,
            action=action,
            attempt_id=attempt_id,
            request_sha256=request_sha256,
            review_sha256=before.review.review_sha256,
        )
        _CONSUME_RECEIPT(
            authority,
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            consumer_key=consumer_key,
        )
        after = _RESOLVE_BINDING(
            authority,
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=False,
        )
    except _CONFIRMATION_ERROR as exc:
        raise BetfairExecutionConfirmationError(
            "durable Betfair operator confirmation could not be consumed"
        ) from exc
    if not _authority_graph_unchanged():
        raise BetfairExecutionConfirmationError(
            "supervised confirmation authority changed during final-send admission"
        )
    _require_confirmation_binding(
        after,
        bound=bound,
        approval=approval,
        action=action,
        attempt_id=attempt_id,
        submitted_at=submitted_at,
    )
    if after.receipt.consumed_by != consumer_key or after.receipt.consumed_at is None:
        raise BetfairExecutionConfirmationError(
            "durable Betfair confirmation was not consumed by this exact send identity"
        )
    if _instant(after.receipt.consumed_at, "consumed_at") != submitted_instant:
        raise BetfairExecutionConfirmationError(
            "Betfair confirmation consumption time does not match durable send admission"
        )
    material = {
        "execution_plan_id": bound.execution_plan.plan_id,
        "action_id": action.action_id,
        "attempt_id": attempt_id,
        "intent_sha256": bound.intent_sha256,
        "request_sha256": request_sha256,
        "review_sha256": after.review.review_sha256,
        "receipt_id": after.receipt.receipt_id,
        "receipt_sha256": after.receipt.receipt_sha256,
        "confirmed_at": after.receipt.confirmed_at,
        "consumed_at": after.receipt.consumed_at,
        "consumer_key": consumer_key,
    }
    return BetfairExecutionConfirmationWitness(
        **material,
        evidence_id=_domain_digest(_WITNESS_DOMAIN, material),
    )
