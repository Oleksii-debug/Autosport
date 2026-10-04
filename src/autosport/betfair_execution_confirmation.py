"""Durable operator confirmation for one exact Betfair final-send decision.

This module composes the generic supervised confirmation journal with the exact
Betfair execution plan/action/attempt that may reach ``placeOrders``.  It does
not perform provider I/O and possession of any returned dataclass is not an
execution capability.  Positive authority is re-resolved from the durable
confirmation journal immediately before the provider transport is invoked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping

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


CONFIRMATION_FILENAME = "supervised-confirmation.jsonl"
_REVIEW_PAYLOAD_DOMAIN = "autosport.supervised-review-payload.v1"
_DECISION_DOMAIN = "autosport.betfair-final-send-decision.v1"
_CONSUMER_DOMAIN = "autosport.betfair-final-send-consumer.v1"
_WITNESS_DOMAIN = "autosport.betfair-final-send-confirmation-witness.v1"


class BetfairExecutionConfirmationError(RuntimeError):
    """Durable operator confirmation does not authorize this exact Betfair send."""


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
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + _canonical_bytes(value)).hexdigest()


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
    """Exact values an operator-review surface must submit to the generic journal."""

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
    """Build the exact operator-visible final-send decision for one attempt.

    Merely building this spec is side-effect free: it does not prepare, confirm,
    or consume a receipt.  The caller may pass these fields to
    ``SupervisedConfirmationAuthority.prepare_review`` and then explicitly
    confirm that durable review.
    """

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
    decision_id = f"betfair-final-send-v1-{decision_sha256}"
    review_payload = {
        "schema": "autosport.betfair_final_send_review",
        "schema_version": 1,
        **material,
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
    binding: SupervisedConfirmationBinding,
    *,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    action: ExecutionAction,
    attempt_id: str,
) -> None:
    if type(binding) is not SupervisedConfirmationBinding:
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
    if review.decision_id != spec.decision_id:
        raise BetfairExecutionConfirmationError(
            "operator confirmation does not bind this exact Betfair attempt decision"
        )
    if review.decision_sha256 != spec.decision_sha256:
        raise BetfairExecutionConfirmationError(
            "operator confirmation decision digest does not match exact Betfair send"
        )
    if (
        review.bookmaker_id != action.bookmaker_id
        or review.account_id != action.account_id
    ):
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
            "operator confirmation review payload does not match canonical Betfair final-send review"
        )
    try:
        approval.require_active(review.reviewed_at)
    except SupervisedExecutionError as exc:
        raise BetfairExecutionConfirmationError(
            "SupervisedApproval was not active when operator review was created"
        ) from exc
    if _instant(review.expires_at, "review.expires_at") > _instant(
        approval.expires_at,
        "approval.expires_at",
    ):
        raise BetfairExecutionConfirmationError(
            "operator confirmation lifetime exceeds underlying SupervisedApproval"
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
    """Audit projection of a consumed receipt; not a transferable capability."""

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
        expected = _domain_digest(_WITNESS_DOMAIN, self._material())
        if self.evidence_id != expected:
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
) -> BetfairExecutionConfirmationWitness:
    """Consume one exact durable confirmation immediately before provider send.

    ``request_sha256`` is the exact request digest already persisted in the
    execution ledger's SUBMITTED transition.  The durable consumption key commits
    that request together with the exact attempt and upstream intent hash.
    """

    if type(execution_workspace) is not Path:
        raise BetfairExecutionConfirmationError(
            "execution_workspace must be exact Path"
        )
    workspace = execution_workspace.resolve()
    action = _require_bound_action(bound, approval, action_id=action_id)
    attempt_id = _text(attempt_id, "attempt_id", max_length=256)
    receipt_id = _sha(receipt_id, "receipt_id")
    expected_review_sha256 = _sha(
        expected_review_sha256,
        "expected_review_sha256",
    )
    request_sha256 = _sha(request_sha256, "request_sha256")
    authority_path = workspace / CONFIRMATION_FILENAME
    try:
        authority = SupervisedConfirmationAuthority(authority_path)
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
            attempt_id=attempt_id,
        )
        consumer_key = _consumer_key(
            bound=bound,
            action=action,
            attempt_id=attempt_id,
            request_sha256=request_sha256,
            review_sha256=before.review.review_sha256,
        )
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
        raise BetfairExecutionConfirmationError(
            "durable Betfair operator confirmation could not be consumed"
        ) from exc
    _require_confirmation_binding(
        after,
        bound=bound,
        approval=approval,
        action=action,
        attempt_id=attempt_id,
    )
    if after.receipt.consumed_by != consumer_key or after.receipt.consumed_at is None:
        raise BetfairExecutionConfirmationError(
            "durable Betfair confirmation was not consumed by this exact send identity"
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
