"""Product-owned supervised execution approval for Smarkets.

This module composes the generic durable supervised-confirmation authority with
one exact canonical Smarkets execution action. It does not place an order,
access Smarkets, prove provider readback, or enable real-money execution.

Positive authority is never accepted from a caller-created receipt DTO. The
receipt and review are re-resolved from the canonical Autosport workspace and
the receipt is consumed once under a deterministic action identity. After a
restart the same approval may be reconstructed only from that durable consumed
receipt and only for the exact same bound execution plan/action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Final

from .paths import default_workspace
from .real_execution_ledger import ExecutionAction
from .supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationBinding,
    SupervisedConfirmationError,
)
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedApproval,
    SupervisedExecutionError,
)


_CONFIRMATION_FILENAME: Final = "supervised-confirmation.jsonl"
_SUPERVISED_REVIEW_PAYLOAD_DOMAIN: Final = "autosport.supervised-review-payload.v1"
_CANONICAL_PATH_LOCK: Final = Lock()
_CANONICAL_CONFIRMATION_PATH: Path | None = None


class SmarketsExecutionApprovalError(RuntimeError):
    """Product-owned supervised approval could not be proven for a Smarkets action."""


def canonical_supervised_confirmation_path() -> Path:
    """Resolve and freeze this process' product-configured confirmation path.

    AUTOSPORT_WORKSPACE is a startup/operator configuration input. Once this
    financial authority has observed the resolved workspace, changing that input
    in-process cannot switch the trust root to another confirmation journal.
    """

    global _CANONICAL_CONFIRMATION_PATH
    resolved = default_workspace() / _CONFIRMATION_FILENAME
    with _CANONICAL_PATH_LOCK:
        if _CANONICAL_CONFIRMATION_PATH is None:
            _CANONICAL_CONFIRMATION_PATH = resolved
        elif _CANONICAL_CONFIRMATION_PATH != resolved:
            raise SmarketsExecutionApprovalError(
                "canonical Autosport workspace changed after approval authority initialization"
            )
        return _CANONICAL_CONFIRMATION_PATH


def _open_confirmation_authority() -> SupervisedConfirmationAuthority:
    return SupervisedConfirmationAuthority(canonical_supervised_confirmation_path())


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SmarketsExecutionApprovalError(f"{name} must be non-empty canonical text")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SmarketsExecutionApprovalError(f"{name} must be lowercase SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SmarketsExecutionApprovalError(f"{name} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SmarketsExecutionApprovalError(f"{name} must be timezone-aware ISO-8601")
    return parsed


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SmarketsExecutionApprovalError(
            "Smarkets execution approval evidence is not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _domain_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("utf-8")
        + bytes((0,))
        + json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _consumer_key(
    bound: BoundSupervisedExecutionPlan,
    action: ExecutionAction,
    review_sha256: str,
) -> str:
    return (
        "smarkets-execution-approval:v1:"
        f"{bound.execution_plan.plan_id}:{action.action_id}:{review_sha256}"
    )


def _require_bound_action(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action_id: str,
) -> ExecutionAction:
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise SmarketsExecutionApprovalError(
            "bound must be canonical BoundSupervisedExecutionPlan"
        )
    if type(approval) is not SupervisedApproval:
        raise SmarketsExecutionApprovalError("approval must be canonical SupervisedApproval")
    action_id = _text(action_id, "action_id")
    try:
        bound.verify_binding()
        action = bound.action_for(action_id)
    except (SupervisedExecutionError, ValueError) as exc:
        raise SmarketsExecutionApprovalError(
            "bound supervised execution plan is not authoritative"
        ) from exc

    if action.bookmaker_id.lower() != "smarkets":
        raise SmarketsExecutionApprovalError("execution action bookmaker is not Smarkets")
    if (
        approval.portfolio_plan_sha256 != bound.portfolio_plan_sha256
        or approval.intent_id != bound.intent_id
        or approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != bound.execution_plan.approval_id
    ):
        raise SmarketsExecutionApprovalError(
            "SupervisedApproval does not bind the exact supervised execution plan"
        )
    return action


def _review_payload(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action: ExecutionAction,
    risk_evidence_sha256: str,
) -> dict[str, object]:
    risk_evidence_sha256 = _sha256(
        risk_evidence_sha256, "risk_evidence_sha256"
    )
    return {
        "schema": "autosport.smarkets_execution_review",
        "schema_version": 1,
        "execution_plan_id": bound.execution_plan.plan_id,
        "execution_plan_sha256": bound.execution_plan.fingerprint,
        "execution_decision_id": bound.execution_plan.plan_id,
        "upstream_decision_id": bound.execution_plan.decision_id,
        "portfolio_plan_sha256": bound.portfolio_plan_sha256,
        "economic_goal_contract_sha256": bound.economic_goal_contract_sha256,
        "intent_id": bound.intent_id,
        "intent_sha256": bound.intent_sha256,
        "approval_fingerprint": approval.fingerprint,
        "approval_evidence_sha256": approval.evidence_sha256,
        "risk_evidence_sha256": risk_evidence_sha256,
        "action": action.to_dict(),
    }


def smarkets_execution_review_payload(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    risk_evidence_sha256: str,
) -> dict[str, object]:
    """Build the exact operator-visible payload this approval boundary accepts."""

    action = _require_bound_action(bound, approval, action_id)
    return _review_payload(bound, approval, action, risk_evidence_sha256)


def _require_confirmation_binding(
    binding: SupervisedConfirmationBinding,
    *,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action: ExecutionAction,
) -> None:
    if type(binding) is not SupervisedConfirmationBinding:
        raise SmarketsExecutionApprovalError(
            "confirmation binding must come from canonical durable authority"
        )
    review = binding.review
    receipt = binding.receipt
    plan = bound.execution_plan
    expected_decision_sha256 = plan.fingerprint

    if review.decision_id != plan.plan_id:
        raise SmarketsExecutionApprovalError(
            "operator confirmation decision_id does not match exact execution plan identity"
        )
    if review.decision_sha256 != expected_decision_sha256:
        raise SmarketsExecutionApprovalError(
            "operator confirmation does not bind exact execution plan bytes"
        )
    if (
        review.bookmaker_id != action.bookmaker_id
        or review.account_id != action.account_id
    ):
        raise SmarketsExecutionApprovalError(
            "operator confirmation bookmaker/account does not match execution action"
        )
    matching_actions = tuple(
        item
        for item in plan.actions
        if (item.bookmaker_id, item.account_id)
        == (review.bookmaker_id, review.account_id)
    )
    if matching_actions != (action,):
        raise SmarketsExecutionApprovalError(
            "operator confirmation does not identify one unique action for bookmaker/account"
        )
    if review.approval_evidence_sha256 != approval.evidence_sha256:
        raise SmarketsExecutionApprovalError(
            "operator confirmation approval evidence does not match SupervisedApproval"
        )
    expected_review_payload_sha256 = _domain_digest(
        _SUPERVISED_REVIEW_PAYLOAD_DOMAIN,
        _review_payload(
            bound,
            approval,
            action,
            review.risk_evidence_sha256,
        ),
    )
    if review.review_payload_sha256 != expected_review_payload_sha256:
        raise SmarketsExecutionApprovalError(
            "operator confirmation review payload does not match canonical Smarkets execution review"
        )
    try:
        approval.require_active(review.reviewed_at)
    except SupervisedExecutionError as exc:
        raise SmarketsExecutionApprovalError(
            "SupervisedApproval was not active when operator review was created"
        ) from exc
    if _instant(review.expires_at, "review.expires_at") > _instant(
        approval.expires_at, "approval.expires_at"
    ):
        raise SmarketsExecutionApprovalError(
            "operator confirmation lifetime exceeds underlying SupervisedApproval"
        )
    if (
        receipt.review_id != review.review_id
        or receipt.review_sha256 != review.review_sha256
        or receipt.decision_id != review.decision_id
        or receipt.bookmaker_id != review.bookmaker_id
        or receipt.account_id != review.account_id
    ):
        raise SmarketsExecutionApprovalError(
            "operator confirmation receipt/review identity is inconsistent"
        )


@dataclass(frozen=True, slots=True)
class SmarketsExecutionApprovalRecord:
    """Structural result of durable Smarkets approval resolution.

    The record is intentionally caller-constructible. Its type, immutability and
    evidence_id do not prove product origin. Any authority-bearing consumer must
    call verify_smarkets_execution_approval() or otherwise re-resolve the exact
    consumed receipt through the canonical SupervisedConfirmationAuthority.
    """

    execution_plan_id: str
    action_id: str
    bookmaker_id: str
    account_id: str
    decision_id: str
    decision_sha256: str
    approval_fingerprint: str
    approval_evidence_sha256: str
    risk_evidence_sha256: str
    review_payload_sha256: str
    review_id: str
    review_sha256: str
    receipt_id: str
    receipt_sha256: str
    confirmed_at: str
    consumed_at: str
    consumer_key: str
    evidence_id: str

    def __post_init__(self) -> None:
        for name in (
            "execution_plan_id",
            "action_id",
            "bookmaker_id",
            "account_id",
            "decision_id",
            "review_id",
            "consumer_key",
        ):
            _text(getattr(self, name), name)
        for name in (
            "decision_sha256",
            "approval_fingerprint",
            "approval_evidence_sha256",
            "risk_evidence_sha256",
            "review_payload_sha256",
            "review_sha256",
            "receipt_id",
            "receipt_sha256",
            "evidence_id",
        ):
            _sha256(getattr(self, name), name)
        _instant(self.confirmed_at, "confirmed_at")
        _instant(self.consumed_at, "consumed_at")
        expected = _digest(
            {
                "schema": "autosport.smarkets_execution_approval",
                "schema_version": 1,
                **self._evidence_material(),
            }
        )
        if self.evidence_id != expected:
            raise SmarketsExecutionApprovalError(
                "Smarkets execution approval record evidence_id mismatch"
            )

    def _evidence_material(self) -> dict[str, str]:
        return {
            "execution_plan_id": self.execution_plan_id,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "decision_id": self.decision_id,
            "decision_sha256": self.decision_sha256,
            "approval_fingerprint": self.approval_fingerprint,
            "approval_evidence_sha256": self.approval_evidence_sha256,
            "risk_evidence_sha256": self.risk_evidence_sha256,
            "review_payload_sha256": self.review_payload_sha256,
            "review_id": self.review_id,
            "review_sha256": self.review_sha256,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "confirmed_at": self.confirmed_at,
            "consumed_at": self.consumed_at,
            "consumer_key": self.consumer_key,
        }

def _record_from_binding(
    binding: SupervisedConfirmationBinding,
    *,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action: ExecutionAction,
) -> SmarketsExecutionApprovalRecord:
    receipt = binding.receipt
    review = binding.review
    if receipt.consumed_by is None or receipt.consumed_at is None:
        raise SmarketsExecutionApprovalError(
            "operator confirmation receipt is not durably consumed"
        )
    consumer_key = _consumer_key(bound, action, review.review_sha256)
    if receipt.consumed_by != consumer_key:
        raise SmarketsExecutionApprovalError(
            "operator confirmation receipt was consumed by another execution identity"
        )
    witness_payload = {
        "execution_plan_id": bound.execution_plan.plan_id,
        "action_id": action.action_id,
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "decision_id": review.decision_id,
        "decision_sha256": review.decision_sha256,
        "approval_fingerprint": approval.fingerprint,
        "approval_evidence_sha256": review.approval_evidence_sha256,
        "risk_evidence_sha256": review.risk_evidence_sha256,
        "review_payload_sha256": review.review_payload_sha256,
        "review_id": review.review_id,
        "review_sha256": review.review_sha256,
        "receipt_id": receipt.receipt_id,
        "receipt_sha256": receipt.receipt_sha256,
        "confirmed_at": receipt.confirmed_at,
        "consumed_at": receipt.consumed_at,
        "consumer_key": consumer_key,
    }
    evidence_payload = {
        "schema": "autosport.smarkets_execution_approval",
        "schema_version": 1,
        **witness_payload,
    }
    return SmarketsExecutionApprovalRecord(
        **witness_payload,
        evidence_id=_digest(evidence_payload),
    )


def consume_smarkets_execution_approval(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    receipt_id: str,
    expected_review_sha256: str,
) -> SmarketsExecutionApprovalRecord:
    """Consume one durable operator receipt for one exact Smarkets action.

    This is the one-shot transition intended to precede an irreversible provider
    mutation. It performs no provider I/O itself.
    """

    action = _require_bound_action(bound, approval, action_id)
    receipt_id = _sha256(receipt_id, "receipt_id")
    expected_review_sha256 = _sha256(
        expected_review_sha256, "expected_review_sha256"
    )
    try:
        authority = _open_confirmation_authority()
        before = authority.resolve_receipt_binding(
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=True,
        )
        _require_confirmation_binding(
            before,
            bound=bound,
            approval=approval,
            action=action,
        )
        consumer_key = _consumer_key(bound, action, before.review.review_sha256)
        authority.consume_receipt(
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            consumer_key=consumer_key,
        )
        after = authority.resolve_receipt_binding(
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=False,
        )
    except SupervisedConfirmationError as exc:
        raise SmarketsExecutionApprovalError(
            "durable operator confirmation could not be consumed"
        ) from exc
    _require_confirmation_binding(
        after,
        bound=bound,
        approval=approval,
        action=action,
    )
    return _record_from_binding(after, bound=bound, approval=approval, action=action)


def resolve_consumed_smarkets_execution_approval(
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    receipt_id: str,
    expected_review_sha256: str,
) -> SmarketsExecutionApprovalRecord:
    """Re-resolve a previously consumed exact Smarkets approval after restart."""

    action = _require_bound_action(bound, approval, action_id)
    receipt_id = _sha256(receipt_id, "receipt_id")
    expected_review_sha256 = _sha256(
        expected_review_sha256, "expected_review_sha256"
    )
    try:
        authority = _open_confirmation_authority()
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=False,
        )
    except SupervisedConfirmationError as exc:
        raise SmarketsExecutionApprovalError(
            "durable operator confirmation could not be re-resolved"
        ) from exc
    _require_confirmation_binding(
        binding,
        bound=bound,
        approval=approval,
        action=action,
    )
    return _record_from_binding(binding, bound=bound, approval=approval, action=action)


def verify_smarkets_execution_approval(
    record: SmarketsExecutionApprovalRecord,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
) -> SmarketsExecutionApprovalRecord:
    """Re-resolve durable confirmation and exact-compare a structural record.

    This function is the authority-bearing read boundary for downstream callers.
    Possession of a SmarketsExecutionApprovalRecord alone never grants execution
    authority.
    """

    if type(record) is not SmarketsExecutionApprovalRecord:
        raise SmarketsExecutionApprovalError(
            "record must be exact SmarketsExecutionApprovalRecord"
        )
    fresh = resolve_consumed_smarkets_execution_approval(
        bound,
        approval,
        action_id=record.action_id,
        receipt_id=record.receipt_id,
        expected_review_sha256=record.review_sha256,
    )
    if fresh != record:
        raise SmarketsExecutionApprovalError(
            "Smarkets execution approval record does not match durable product authority"
        )
    return fresh
