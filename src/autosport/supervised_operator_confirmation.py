from __future__ import annotations

"""Durable product/operator confirmation origin for supervised execution.

This module deliberately does not submit provider writes and does not reserve an
execution plan. It supplies the missing REVIEW -> CONFIRM authority boundary:
a review is an in-memory, one-use snapshot of the exact bound plan, approval and
current owner contract; only a deliberate confirm of that exact live snapshot
may publish an immutable receipt. Restart before confirmation invalidates the
review by construction. A confirmed receipt is restart-stable and protected by
the existing independent monotonic workspace authority.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Final
import uuid

from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .integrity import atomic_write_json, durable_path_lock, sha256_file
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .supervised_execution import (
    ApprovalState,
    BoundSupervisedExecutionPlan,
    SupervisedApproval,
    SupervisedExecutionError,
)
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA: Final = "autosport.supervised_operator_confirmation"
_SCHEMA_VERSION: Final = 1
_AUTHORITY_DOMAIN: Final = "autosport.supervised-operator-confirmation.v1"
_DIRECTORY: Final = "supervised-operator-confirmations"
_SHA256 = frozenset("0123456789abcdef")
_ROOT_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "transaction_id",
        "receipt",
    }
)
_RECEIPT_KEYS: Final = frozenset(
    {
        "plan_id",
        "plan_fingerprint",
        "decision_id",
        "action_bindings",
        "owner_contract_sha256",
        "approval_id",
        "approval_fingerprint",
        "approval_evidence_sha256",
        "execution_terms_sha256",
        "reviewed_at",
        "confirmed_at",
        "expires_at",
        "review_nonce",
        "review_sha256",
        "semantic_binding_sha256",
    }
)


class SupervisedOperatorConfirmationError(RuntimeError):
    """The product/operator confirmation authority rejected an operation."""


def _canonical_bytes(payload: object) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SupervisedOperatorConfirmationError(
            "operator confirmation is outside canonical JSON domain"
        ) from exc


def _digest(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SupervisedOperatorConfirmationError(
            "operator confirmation binding is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SupervisedOperatorConfirmationError(
            f"{name} must be non-empty canonical text"
        )
    return value


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(character not in _SHA256 for character in raw):
        raise SupervisedOperatorConfirmationError(
            f"{name} must be lowercase SHA-256"
        )
    return raw


def _timestamp(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisedOperatorConfirmationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SupervisedOperatorConfirmationError(
            f"{name} must be timezone-aware"
        )
    return parsed


def _trusted_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _plan_file_name(plan_id: str) -> str:
    raw = _text(plan_id, "plan_id")
    prefix = "supervised-v2-"
    if not raw.startswith(prefix):
        raise SupervisedOperatorConfirmationError(
            "plan_id is not a supervised-v2 identity"
        )
    _sha(raw[len(prefix) :], "plan_id binding")
    return f"{raw}.json"


def _action_bindings(
    bound: BoundSupervisedExecutionPlan,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (action.action_id, _digest(action.to_dict()))
        for action in bound.execution_plan.actions
    )


def _current_owner_contract_sha256(workspace: Path) -> str:
    try:
        contract = EconomicGoalStore(workspace).load()
        digest = provenance_for(contract).contract_sha256
    except Exception as exc:
        raise SupervisedOperatorConfirmationError(
            "cannot resolve current durable owner authority"
        ) from exc
    if getattr(contract, "emergency_stop", None) is not False:
        raise SupervisedOperatorConfirmationError(
            "owner emergency STOP blocks supervised confirmation"
        )
    return _sha(digest, "owner_contract_sha256")


def _require_inputs(
    workspace: Path,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    at: str,
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise SupervisedOperatorConfirmationError(
            "bound must be exact BoundSupervisedExecutionPlan"
        )
    if type(approval) is not SupervisedApproval:
        raise SupervisedOperatorConfirmationError(
            "approval must be exact SupervisedApproval"
        )
    try:
        bound.verify_binding()
        approval.require_active(at)
    except SupervisedExecutionError as exc:
        raise SupervisedOperatorConfirmationError(
            "supervised plan/approval is not active canonical evidence"
        ) from exc
    if approval.state is not ApprovalState.APPROVED:
        raise SupervisedOperatorConfirmationError(
            "operator confirmation requires APPROVED state"
        )
    if (
        approval.portfolio_plan_sha256 != bound.portfolio_plan_sha256
        or approval.intent_id != bound.intent_id
        or approval.fingerprint != bound.approval_fingerprint
        or approval.ledger_identity != bound.execution_plan.approval_id
    ):
        raise SupervisedOperatorConfirmationError(
            "approval does not exactly bind the supervised plan"
        )
    owner_sha = _current_owner_contract_sha256(workspace)
    if owner_sha != bound.economic_goal_contract_sha256:
        raise SupervisedOperatorConfirmationError(
            "supervised plan does not match current durable owner authority"
        )
    action_bindings = _action_bindings(bound)
    if not action_bindings:
        raise SupervisedOperatorConfirmationError(
            "supervised confirmation requires at least one action"
        )
    expiries = [
        _timestamp(approval.expires_at, "approval expires_at"),
        *(
            _timestamp(action.expires_at, "action expires_at")
            for action in bound.execution_plan.actions
        ),
    ]
    expires = min(expiries)
    current = _timestamp(at, "confirmation time")
    if current >= expires:
        raise SupervisedOperatorConfirmationError(
            "supervised review/confirmation evidence is expired"
        )
    return owner_sha, expires.isoformat(), action_bindings


@dataclass(frozen=True, slots=True)
class SupervisedOperatorReview:
    """One in-memory immutable REVIEW snapshot. It is not durable authority."""

    plan_id: str
    plan_fingerprint: str
    decision_id: str
    action_bindings: tuple[tuple[str, str], ...]
    owner_contract_sha256: str
    approval_id: str
    approval_fingerprint: str
    approval_evidence_sha256: str
    execution_terms_sha256: str
    reviewed_at: str
    expires_at: str
    review_nonce: str
    review_sha256: str

    def semantic_payload(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "decision_id": self.decision_id,
            "action_bindings": [list(item) for item in self.action_bindings],
            "owner_contract_sha256": self.owner_contract_sha256,
            "approval_id": self.approval_id,
            "approval_fingerprint": self.approval_fingerprint,
            "approval_evidence_sha256": self.approval_evidence_sha256,
            "execution_terms_sha256": self.execution_terms_sha256,
            "reviewed_at": self.reviewed_at,
            "expires_at": self.expires_at,
            "review_nonce": self.review_nonce,
        }


@dataclass(frozen=True, slots=True)
class SupervisedOperatorConfirmationReceipt:
    """Immutable durable result of one exact REVIEW -> CONFIRM transition."""

    plan_id: str
    plan_fingerprint: str
    decision_id: str
    action_bindings: tuple[tuple[str, str], ...]
    owner_contract_sha256: str
    approval_id: str
    approval_fingerprint: str
    approval_evidence_sha256: str
    execution_terms_sha256: str
    reviewed_at: str
    confirmed_at: str
    expires_at: str
    review_nonce: str
    review_sha256: str
    semantic_binding_sha256: str
    workspace_instance_id: str
    transaction_id: str

    def semantic_payload(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "decision_id": self.decision_id,
            "action_bindings": [list(item) for item in self.action_bindings],
            "owner_contract_sha256": self.owner_contract_sha256,
            "approval_id": self.approval_id,
            "approval_fingerprint": self.approval_fingerprint,
            "approval_evidence_sha256": self.approval_evidence_sha256,
            "execution_terms_sha256": self.execution_terms_sha256,
            "reviewed_at": self.reviewed_at,
            "confirmed_at": self.confirmed_at,
            "expires_at": self.expires_at,
            "review_nonce": self.review_nonce,
            "review_sha256": self.review_sha256,
        }


@dataclass(slots=True)
class _PendingReview:
    review: SupervisedOperatorReview
    bound: BoundSupervisedExecutionPlan
    approval: SupervisedApproval


class _ConfirmationReceiptStore:
    """Creation-only durable receipt persistence; not a public approval issuer."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.directory = workspace / _DIRECTORY

    def _path(self, plan_id: str) -> Path:
        return self.directory / _plan_file_name(plan_id)

    def _authority(self, plan_id: str) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=plan_id,
        )

    def exists(self, plan_id: str) -> bool:
        return self._path(plan_id).exists()

    def publish(
        self,
        *,
        review: SupervisedOperatorReview,
        confirmed_at: str,
    ) -> SupervisedOperatorConfirmationReceipt:
        path = self._path(review.plan_id)
        authority = self._authority(review.plan_id)
        with WorkspaceEconomicLock(self.workspace):
            if path.exists():
                existing = self._load_locked(review.plan_id, authority=authority)
                if existing.review_sha256 != review.review_sha256:
                    raise SupervisedOperatorConfirmationError(
                        "plan already has a different operator confirmation"
                    )
                return existing

            try:
                authority.recover(observed_state_sha256=None)
            except MonotonicWorkspaceAuthorityError as exc:
                raise SupervisedOperatorConfirmationError(
                    "missing confirmation bytes conflict with monotonic authority"
                ) from exc

            receipt_core = {
                **review.semantic_payload(),
                "confirmed_at": confirmed_at,
                "review_sha256": review.review_sha256,
            }
            binding = _digest(receipt_core)
            transaction_id = f"operator-confirm-{uuid.uuid4().hex}"
            payload = {
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "workspace_instance_id": authority.workspace_instance_id,
                "transaction_id": transaction_id,
                "receipt": {
                    **receipt_core,
                    "semantic_binding_sha256": binding,
                },
            }
            intended = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
            try:
                authority.prepare(
                    tx_id=transaction_id,
                    observed_state_sha256=None,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(path, payload)
                observed = sha256_file(path)
                if observed != intended:
                    raise SupervisedOperatorConfirmationError(
                        "published confirmation bytes differ from prepared state"
                    )
                authority.commit(
                    tx_id=transaction_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
            except (OSError, MonotonicWorkspaceAuthorityError) as exc:
                raise SupervisedOperatorConfirmationError(
                    "cannot durably publish operator confirmation"
                ) from exc
            return self._load_locked(review.plan_id, authority=authority)

    def resolve(self, plan_id: str) -> SupervisedOperatorConfirmationReceipt:
        authority = self._authority(plan_id)
        with WorkspaceEconomicLock(self.workspace):
            return self._load_locked(plan_id, authority=authority)

    def _load_locked(
        self,
        plan_id: str,
        *,
        authority: MonotonicWorkspaceAuthority,
    ) -> SupervisedOperatorConfirmationReceipt:
        path = self._path(plan_id)
        if not path.exists():
            try:
                authority.recover(observed_state_sha256=None)
            except MonotonicWorkspaceAuthorityError as exc:
                raise SupervisedOperatorConfirmationError(
                    "operator confirmation was deleted or rolled back"
                ) from exc
            raise SupervisedOperatorConfirmationError(
                "supervised plan has no operator confirmation"
            )
        with durable_path_lock(path):
            try:
                raw_bytes = path.read_bytes()
                payload = strict_json_loads(raw_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
                raise SupervisedOperatorConfirmationError(
                    "cannot decode durable operator confirmation"
                ) from exc
        observed = hashlib.sha256(raw_bytes).hexdigest()
        if type(payload) is not dict or frozenset(payload) != _ROOT_KEYS:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation root schema is not exact"
            )
        if (
            payload["schema"] != _SCHEMA
            or payload["schema_version"] != _SCHEMA_VERSION
        ):
            raise SupervisedOperatorConfirmationError(
                "operator confirmation schema/version is unsupported"
            )
        if payload["workspace_instance_id"] != authority.workspace_instance_id:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation workspace identity changed"
            )
        receipt = payload["receipt"]
        if type(receipt) is not dict or frozenset(receipt) != _RECEIPT_KEYS:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation receipt schema is not exact"
            )
        binding = _sha(
            receipt["semantic_binding_sha256"],
            "semantic_binding_sha256",
        )
        core = dict(receipt)
        core.pop("semantic_binding_sha256")
        if _digest(core) != binding:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation semantic binding changed"
            )
        review_core = {
            key: core[key]
            for key in (
                "plan_id",
                "plan_fingerprint",
                "decision_id",
                "action_bindings",
                "owner_contract_sha256",
                "approval_id",
                "approval_fingerprint",
                "approval_evidence_sha256",
                "execution_terms_sha256",
                "reviewed_at",
                "expires_at",
                "review_nonce",
            )
        }
        if _digest(review_core) != _sha(core["review_sha256"], "review_sha256"):
            raise SupervisedOperatorConfirmationError(
                "operator REVIEW snapshot binding changed"
            )
        if receipt["plan_id"] != plan_id:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation plan identity changed"
            )
        _timestamp(core["reviewed_at"], "reviewed_at")
        confirmed = _timestamp(core["confirmed_at"], "confirmed_at")
        expires = _timestamp(core["expires_at"], "expires_at")
        if confirmed < _timestamp(core["reviewed_at"], "reviewed_at") or confirmed >= expires:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation chronology is invalid"
            )
        action_bindings_raw = core["action_bindings"]
        if (
            type(action_bindings_raw) is not list
            or not action_bindings_raw
            or any(
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                for item in action_bindings_raw
            )
        ):
            raise SupervisedOperatorConfirmationError(
                "operator confirmation action bindings are invalid"
            )
        action_bindings = tuple(
            (
                _text(item[0], "action_id"),
                _sha(item[1], "action_sha256"),
            )
            for item in action_bindings_raw
        )
        if len({item[0] for item in action_bindings}) != len(action_bindings):
            raise SupervisedOperatorConfirmationError(
                "operator confirmation action bindings are duplicated"
            )
        try:
            authority.recover(
                observed_state_sha256=observed,
                tx_id=_text(payload["transaction_id"], "transaction_id"),
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise SupervisedOperatorConfirmationError(
                "operator confirmation fails monotonic authority verification"
            ) from exc
        return SupervisedOperatorConfirmationReceipt(
            plan_id=_text(core["plan_id"], "plan_id"),
            plan_fingerprint=_sha(core["plan_fingerprint"], "plan_fingerprint"),
            decision_id=_text(core["decision_id"], "decision_id"),
            action_bindings=action_bindings,
            owner_contract_sha256=_sha(
                core["owner_contract_sha256"], "owner_contract_sha256"
            ),
            approval_id=_text(core["approval_id"], "approval_id"),
            approval_fingerprint=_sha(
                core["approval_fingerprint"], "approval_fingerprint"
            ),
            approval_evidence_sha256=_sha(
                core["approval_evidence_sha256"], "approval_evidence_sha256"
            ),
            execution_terms_sha256=_sha(
                core["execution_terms_sha256"], "execution_terms_sha256"
            ),
            reviewed_at=_text(core["reviewed_at"], "reviewed_at"),
            confirmed_at=_text(core["confirmed_at"], "confirmed_at"),
            expires_at=_text(core["expires_at"], "expires_at"),
            review_nonce=_text(core["review_nonce"], "review_nonce"),
            review_sha256=_sha(core["review_sha256"], "review_sha256"),
            semantic_binding_sha256=binding,
            workspace_instance_id=_text(
                payload["workspace_instance_id"], "workspace_instance_id"
            ),
            transaction_id=_text(payload["transaction_id"], "transaction_id"),
        )


class SupervisedOperatorConfirmationService:
    """Two-phase REVIEW -> CONFIRM product authority for one workspace.

    REVIEW is intentionally ephemeral. A process restart before CONFIRM loses the
    pending object and therefore requires a fresh review. CONFIRM consumes the
    same object by identity before durable publication, so reconstructing a
    same-shaped DTO is not positive authority.
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve(strict=False)
        self._store = _ConfirmationReceiptStore(self.workspace)
        self._pending: _PendingReview | None = None

    def review(
        self,
        *,
        bound: BoundSupervisedExecutionPlan,
        approval: SupervisedApproval,
    ) -> SupervisedOperatorReview:
        reviewed_at = _trusted_now()
        owner_sha, expires_at, action_bindings = _require_inputs(
            self.workspace,
            bound,
            approval,
            at=reviewed_at,
        )
        nonce = uuid.uuid4().hex
        semantic = {
            "plan_id": bound.execution_plan.plan_id,
            "plan_fingerprint": bound.execution_plan.fingerprint,
            "decision_id": bound.execution_plan.decision_id,
            "action_bindings": [list(item) for item in action_bindings],
            "owner_contract_sha256": owner_sha,
            "approval_id": approval.ledger_identity,
            "approval_fingerprint": approval.fingerprint,
            "approval_evidence_sha256": approval.evidence_sha256,
            "execution_terms_sha256": approval.execution_terms_sha256,
            "reviewed_at": reviewed_at,
            "expires_at": expires_at,
            "review_nonce": nonce,
        }
        review = SupervisedOperatorReview(
            plan_id=bound.execution_plan.plan_id,
            plan_fingerprint=bound.execution_plan.fingerprint,
            decision_id=bound.execution_plan.decision_id,
            action_bindings=action_bindings,
            owner_contract_sha256=owner_sha,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
            approval_evidence_sha256=approval.evidence_sha256,
            execution_terms_sha256=approval.execution_terms_sha256,
            reviewed_at=reviewed_at,
            expires_at=expires_at,
            review_nonce=nonce,
            review_sha256=_digest(semantic),
        )
        self._pending = _PendingReview(review=review, bound=bound, approval=approval)
        return review

    def invalidate_review(self) -> None:
        """Consume any pending review without creating durable authority."""

        self._pending = None

    def confirm(
        self,
        review: SupervisedOperatorReview,
    ) -> SupervisedOperatorConfirmationReceipt:
        if type(review) is not SupervisedOperatorReview:
            raise SupervisedOperatorConfirmationError(
                "confirmation requires exact SupervisedOperatorReview"
            )

        if self._store.exists(review.plan_id):
            existing = self._store.resolve(review.plan_id)
            if existing.review_sha256 == review.review_sha256:
                return existing
            raise SupervisedOperatorConfirmationError(
                "plan already has a different operator confirmation"
            )

        pending = self._pending
        if pending is None or pending.review is not review:
            raise SupervisedOperatorConfirmationError(
                "confirmation requires the exact live REVIEW object"
            )
        self._pending = None

        confirmed_at = _trusted_now()
        owner_sha, expires_at, action_bindings = _require_inputs(
            self.workspace,
            pending.bound,
            pending.approval,
            at=confirmed_at,
        )
        expected = {
            "plan_id": pending.bound.execution_plan.plan_id,
            "plan_fingerprint": pending.bound.execution_plan.fingerprint,
            "decision_id": pending.bound.execution_plan.decision_id,
            "action_bindings": action_bindings,
            "owner_contract_sha256": owner_sha,
            "approval_id": pending.approval.ledger_identity,
            "approval_fingerprint": pending.approval.fingerprint,
            "approval_evidence_sha256": pending.approval.evidence_sha256,
            "execution_terms_sha256": pending.approval.execution_terms_sha256,
            "expires_at": expires_at,
        }
        observed = {
            "plan_id": review.plan_id,
            "plan_fingerprint": review.plan_fingerprint,
            "decision_id": review.decision_id,
            "action_bindings": review.action_bindings,
            "owner_contract_sha256": review.owner_contract_sha256,
            "approval_id": review.approval_id,
            "approval_fingerprint": review.approval_fingerprint,
            "approval_evidence_sha256": review.approval_evidence_sha256,
            "execution_terms_sha256": review.execution_terms_sha256,
            "expires_at": review.expires_at,
        }
        if observed != expected or _digest(review.semantic_payload()) != review.review_sha256:
            raise SupervisedOperatorConfirmationError(
                "reviewed execution authority changed before CONFIRM"
            )
        if _timestamp(confirmed_at, "confirmed_at") < _timestamp(
            review.reviewed_at, "reviewed_at"
        ):
            raise SupervisedOperatorConfirmationError(
                "confirmation time predates REVIEW"
            )
        return self._store.publish(review=review, confirmed_at=confirmed_at)

    def resolve_for_execution(
        self,
        *,
        bound: BoundSupervisedExecutionPlan,
        approval: SupervisedApproval,
    ) -> SupervisedOperatorConfirmationReceipt:
        """Re-resolve exact durable confirmation for current execution inputs."""

        now = _trusted_now()
        owner_sha, expires_at, action_bindings = _require_inputs(
            self.workspace,
            bound,
            approval,
            at=now,
        )
        receipt = self._store.resolve(bound.execution_plan.plan_id)
        expected = {
            "plan_id": bound.execution_plan.plan_id,
            "plan_fingerprint": bound.execution_plan.fingerprint,
            "decision_id": bound.execution_plan.decision_id,
            "action_bindings": action_bindings,
            "owner_contract_sha256": owner_sha,
            "approval_id": approval.ledger_identity,
            "approval_fingerprint": approval.fingerprint,
            "approval_evidence_sha256": approval.evidence_sha256,
            "execution_terms_sha256": approval.execution_terms_sha256,
            "expires_at": expires_at,
        }
        observed = {
            "plan_id": receipt.plan_id,
            "plan_fingerprint": receipt.plan_fingerprint,
            "decision_id": receipt.decision_id,
            "action_bindings": receipt.action_bindings,
            "owner_contract_sha256": receipt.owner_contract_sha256,
            "approval_id": receipt.approval_id,
            "approval_fingerprint": receipt.approval_fingerprint,
            "approval_evidence_sha256": receipt.approval_evidence_sha256,
            "execution_terms_sha256": receipt.execution_terms_sha256,
            "expires_at": receipt.expires_at,
        }
        if observed != expected:
            raise SupervisedOperatorConfirmationError(
                "durable operator confirmation does not bind current execution authority"
            )
        return receipt


__all__ = [
    "SupervisedOperatorConfirmationError",
    "SupervisedOperatorConfirmationReceipt",
    "SupervisedOperatorConfirmationService",
    "SupervisedOperatorReview",
]
