from __future__ import annotations

"""Product-owned durable issuance authority for supervised execution plans.

The execution ledger is an execution-state journal, not an issuance trust root.
This module gives the product/operator approval boundary a separate durable
authority: an exact BoundSupervisedExecutionPlan plus SupervisedApproval is
persisted before reservation, bound to the immutable workspace identity and to
the provider request projection that the production Betfair write adapter would
emit. The projection is captured through the existing fail-before-I/O resolver;
no provider write is reachable from this module.

Each plan has an independent monotonic machine-state authority. A workspace-file
rollback/deletion is therefore rejected while that independent authority root
survives. Exact replay is idempotent; a same-plan semantic conflict fails closed.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Final
import uuid

from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    resolve_betfair_standard_limit_price_bound,
)
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .integrity import atomic_write_json, durable_path_lock, sha256_file
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .real_execution_ledger import ExecutionAction, ExecutionPlan, RealExecutionLedger
from .supervised_execution import (
    ApprovalState,
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    SupervisedApproval,
    SupervisedExecutionError,
    build_supervised_execution_plan,
    reserve_supervised_plan,
)
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA: Final = "autosport.supervised_plan_issuance"
_SCHEMA_VERSION: Final = 1
_AUTHORITY_DOMAIN: Final = "autosport.supervised-plan-issuance.v1"
_DIRECTORY: Final = "supervised-plan-issuance"
_ROOT_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "transaction_id",
        "semantic_binding_sha256",
        "issuance",
    }
)
_ISSUANCE_KEYS: Final = frozenset(
    {
        "bound",
        "approval",
        "economic_goal_contract_sha256",
        "provider_requests",
    }
)
_BOUND_KEYS: Final = frozenset(
    {
        "execution_plan",
        "portfolio_plan_sha256",
        "economic_goal_contract_sha256",
        "intent_id",
        "intent_sha256",
        "approval_fingerprint",
        "profile_bindings",
        "constraints",
    }
)
_APPROVAL_KEYS: Final = frozenset(
    {
        "approval_id",
        "portfolio_plan_sha256",
        "intent_id",
        "routing_request_id",
        "execution_terms_sha256",
        "approved_at",
        "expires_at",
        "evidence_sha256",
        "state",
    }
)
_PROVIDER_REQUEST_KEYS: Final = frozenset(
    {
        "action_id",
        "bookmaker_id",
        "account_id",
        "instruction_sha256",
        "write_adapter_id",
        "write_adapter_version",
    }
)


class SupervisedPlanIssuanceError(RuntimeError):
    """Durable supervised-plan issuance authority rejected an operation."""


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
        raise SupervisedPlanIssuanceError(
            "supervised plan issuance is outside canonical JSON domain"
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
        raise SupervisedPlanIssuanceError(
            "supervised plan issuance binding is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _exact_keys(name: str, value: object, expected: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or not all(type(key) is str for key in value):
        raise SupervisedPlanIssuanceError(f"{name} must be an exact JSON object")
    if frozenset(value) != expected:
        raise SupervisedPlanIssuanceError(f"{name} schema is not exact")
    return value


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise SupervisedPlanIssuanceError(f"{name} must be lowercase SHA-256")
    return value


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SupervisedPlanIssuanceError(f"{name} must be non-empty canonical text")
    return value


def _plan_file_name(plan_id: str) -> str:
    raw = _text(plan_id, "plan_id")
    prefix = "supervised-v2-"
    if not raw.startswith(prefix):
        raise SupervisedPlanIssuanceError("plan_id is not a supervised-v2 identity")
    _sha(raw[len(prefix) :], "plan_id binding")
    return f"{raw}.json"


def _approval_payload(approval: SupervisedApproval) -> dict[str, object]:
    if type(approval) is not SupervisedApproval:
        raise SupervisedPlanIssuanceError("approval must be exact SupervisedApproval")
    return {
        "approval_id": approval.approval_id,
        "portfolio_plan_sha256": approval.portfolio_plan_sha256,
        "intent_id": approval.intent_id,
        "routing_request_id": approval.routing_request_id,
        "execution_terms_sha256": approval.execution_terms_sha256,
        "approved_at": approval.approved_at,
        "expires_at": approval.expires_at,
        "evidence_sha256": approval.evidence_sha256,
        "state": approval.state.value,
    }


def _bound_payload(bound: BoundSupervisedExecutionPlan) -> dict[str, object]:
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise SupervisedPlanIssuanceError(
            "bound must be exact BoundSupervisedExecutionPlan"
        )
    BoundSupervisedExecutionPlan.verify_binding(bound)
    return {
        "execution_plan": bound.execution_plan.to_dict(),
        "portfolio_plan_sha256": bound.portfolio_plan_sha256,
        "economic_goal_contract_sha256": bound.economic_goal_contract_sha256,
        "intent_id": bound.intent_id,
        "intent_sha256": bound.intent_sha256,
        "approval_fingerprint": bound.approval_fingerprint,
        "profile_bindings": [
            {
                "venue_id": item.venue_id,
                "account_id": item.account_id,
                "adapter_id": item.adapter_id,
                "adapter_version": item.adapter_version,
                "profile_version": item.profile_version,
                "profile_sha256": item.profile_sha256,
            }
            for item in bound.profile_bindings
        ],
        "constraints": [item.to_dict() for item in bound.constraints],
    }


def _provider_request_payload(
    bound: BoundSupervisedExecutionPlan,
    action: ExecutionAction,
) -> dict[str, object] | None:
    """Return issuance-time positive request identity when canonically provable.

    Generic supervised-plan issuance is provider-neutral.  A Betfair-specific
    prospective price-bound proof is optional evidence attached to an action,
    not a prerequisite for issuing unrelated/unsupported provider actions.
    """

    if action.bookmaker_id != "betfair":
        return None
    try:
        evidence = resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )
    except (BetfairStandardLimitPriceBoundError, SupervisedExecutionError):
        return None
    return {
        "action_id": action.action_id,
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "instruction_sha256": evidence.instruction_sha256,
        "write_adapter_id": evidence.write_adapter_id,
        "write_adapter_version": evidence.write_adapter_version,
    }


def _provider_request_payloads(
    bound: BoundSupervisedExecutionPlan,
) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []
    for action in bound.execution_plan.actions:
        request = _provider_request_payload(bound, action)
        if request is not None:
            requests.append(request)
    return requests


def _decode_execution_plan(raw: object) -> ExecutionPlan:
    body = _exact_keys(
        "execution_plan",
        raw,
        frozenset(
            {
                "schema_version",
                "plan_id",
                "bookmaker_profile_version",
                "decision_id",
                "approval_id",
                "created_at",
                "actions",
            }
        ),
    )
    actions_raw = body["actions"]
    if type(actions_raw) is not list or not actions_raw:
        raise SupervisedPlanIssuanceError("execution_plan actions are invalid")
    try:
        actions = tuple(ExecutionAction(**item) for item in actions_raw)
        return ExecutionPlan(
            plan_id=body["plan_id"],
            bookmaker_profile_version=body["bookmaker_profile_version"],
            decision_id=body["decision_id"],
            approval_id=body["approval_id"],
            created_at=body["created_at"],
            actions=actions,
            schema_version=body["schema_version"],
        )
    except (TypeError, ValueError) as exc:
        raise SupervisedPlanIssuanceError("execution_plan values are invalid") from exc


def _decode_approval(raw: object) -> SupervisedApproval:
    body = _exact_keys("approval", raw, _APPROVAL_KEYS)
    try:
        return SupervisedApproval(
            approval_id=body["approval_id"],
            portfolio_plan_sha256=body["portfolio_plan_sha256"],
            intent_id=body["intent_id"],
            routing_request_id=body["routing_request_id"],
            execution_terms_sha256=body["execution_terms_sha256"],
            approved_at=body["approved_at"],
            expires_at=body["expires_at"],
            evidence_sha256=body["evidence_sha256"],
            state=ApprovalState(body["state"]),
        )
    except (TypeError, ValueError, SupervisedExecutionError) as exc:
        raise SupervisedPlanIssuanceError("approval values are invalid") from exc


def _decode_bound(raw: object) -> BoundSupervisedExecutionPlan:
    body = _exact_keys("bound", raw, _BOUND_KEYS)
    profiles_raw = body["profile_bindings"]
    constraints_raw = body["constraints"]
    if type(profiles_raw) is not list or type(constraints_raw) is not list:
        raise SupervisedPlanIssuanceError("bound profile/constraint vectors are invalid")
    try:
        profiles = tuple(ProfileBinding(**item) for item in profiles_raw)
        constraints = tuple(
            ExecutionLegConstraint(
                leg_id=item["leg_id"],
                side=item["side"],
                quote_expires_at=item["quote_expires_at"],
                max_slippage_fraction=Decimal(item["max_slippage_fraction"]),
            )
            for item in constraints_raw
        )
        return BoundSupervisedExecutionPlan(
            execution_plan=_decode_execution_plan(body["execution_plan"]),
            portfolio_plan_sha256=body["portfolio_plan_sha256"],
            economic_goal_contract_sha256=body["economic_goal_contract_sha256"],
            intent_id=body["intent_id"],
            intent_sha256=body["intent_sha256"],
            approval_fingerprint=body["approval_fingerprint"],
            profile_bindings=profiles,
            constraints=constraints,
        )
    except (KeyError, TypeError, ValueError, SupervisedExecutionError) as exc:
        raise SupervisedPlanIssuanceError("bound values are invalid") from exc


@dataclass(frozen=True, slots=True)
class IssuedSupervisedPlan:
    bound: BoundSupervisedExecutionPlan
    approval: SupervisedApproval
    workspace_instance_id: str
    transaction_id: str
    semantic_binding_sha256: str
    provider_requests: tuple[dict[str, object], ...]


class SupervisedPlanIssuanceStore:
    """Workspace-local creation-only issuance store with independent rollback proof."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=False)
        self.directory = self.workspace / _DIRECTORY
        self.authority_root = authority_root

    def _path(self, plan_id: str) -> Path:
        return self.directory / _plan_file_name(plan_id)

    def _authority(self, plan_id: str) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=plan_id,
            authority_root=self.authority_root,
        )

    def _issuance_core(
        self,
        *,
        bound: BoundSupervisedExecutionPlan,
        approval: SupervisedApproval,
        workspace_instance_id: str,
    ) -> dict[str, object]:
        BoundSupervisedExecutionPlan.verify_binding(bound)
        if (
            approval.fingerprint != bound.approval_fingerprint
            or approval.ledger_identity != bound.execution_plan.approval_id
            or approval.portfolio_plan_sha256 != bound.portfolio_plan_sha256
            or approval.intent_id != bound.intent_id
        ):
            raise SupervisedPlanIssuanceError(
                "approval does not exactly own the bound supervised plan"
            )
        try:
            goal = EconomicGoalStore(self.workspace).load()
            goal_sha256 = provenance_for(goal).contract_sha256
        except Exception as exc:
            raise SupervisedPlanIssuanceError(
                "cannot resolve current durable owner authority for issuance"
            ) from exc
        if goal_sha256 != bound.economic_goal_contract_sha256:
            raise SupervisedPlanIssuanceError(
                "bound plan does not match current durable owner authority"
            )
        return {
            "bound": _bound_payload(bound),
            "approval": _approval_payload(approval),
            "economic_goal_contract_sha256": goal_sha256,
            "provider_requests": _provider_request_payloads(bound),
        }

    def issue(
        self,
        *,
        bound: BoundSupervisedExecutionPlan,
        approval: SupervisedApproval,
    ) -> IssuedSupervisedPlan:
        plan_id = bound.execution_plan.plan_id
        path = self._path(plan_id)
        with WorkspaceEconomicLock(self.workspace):
            authority = self._authority(plan_id)
            core = self._issuance_core(
                bound=bound,
                approval=approval,
                workspace_instance_id=authority.workspace_instance_id,
            )
            binding = _digest(
                {
                    "schema": _SCHEMA,
                    "schema_version": _SCHEMA_VERSION,
                    "workspace_instance_id": authority.workspace_instance_id,
                    "issuance": core,
                }
            )
            if path.exists():
                existing = self._load_locked(plan_id, authority=authority)
                if existing.semantic_binding_sha256 != binding:
                    raise SupervisedPlanIssuanceError(
                        "same plan_id already has different product issuance authority"
                    )
                return existing

            try:
                authority.recover(observed_state_sha256=None)
            except MonotonicWorkspaceAuthorityError as exc:
                raise SupervisedPlanIssuanceError(
                    "missing issuance bytes conflict with independent monotonic authority"
                ) from exc

            transaction_id = f"supervised-plan-issuance-{uuid.uuid4().hex}"
            payload: dict[str, object] = {
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "workspace_instance_id": authority.workspace_instance_id,
                "transaction_id": transaction_id,
                "semantic_binding_sha256": binding,
                "issuance": core,
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
                    raise SupervisedPlanIssuanceError(
                        "published issuance bytes differ from prepared authority state"
                    )
                authority.commit(
                    tx_id=transaction_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
            except (OSError, MonotonicWorkspaceAuthorityError) as exc:
                raise SupervisedPlanIssuanceError(
                    "cannot durably publish supervised plan issuance"
                ) from exc
            return self._load_locked(plan_id, authority=authority)

    def _load_locked(
        self,
        plan_id: str,
        *,
        authority: MonotonicWorkspaceAuthority,
    ) -> IssuedSupervisedPlan:
        path = self._path(plan_id)
        if not path.exists():
            try:
                authority.recover(observed_state_sha256=None)
            except MonotonicWorkspaceAuthorityError as exc:
                raise SupervisedPlanIssuanceError(
                    "issuance was deleted or rolled back"
                ) from exc
            raise SupervisedPlanIssuanceError("supervised plan was never product-issued")
        with durable_path_lock(path):
            try:
                raw_bytes = path.read_bytes()
                payload = strict_json_loads(raw_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
                raise SupervisedPlanIssuanceError(
                    "cannot decode durable supervised plan issuance"
                ) from exc
            observed = hashlib.sha256(raw_bytes).hexdigest()
            root = _exact_keys("issuance root", payload, _ROOT_KEYS)
            if root["schema"] != _SCHEMA or root["schema_version"] != _SCHEMA_VERSION:
                raise SupervisedPlanIssuanceError("unsupported issuance schema")
            if root["workspace_instance_id"] != authority.workspace_instance_id:
                raise SupervisedPlanIssuanceError(
                    "issuance workspace identity does not match current workspace"
                )
            transaction_id = _text(root["transaction_id"], "transaction_id")
            binding = _sha(root["semantic_binding_sha256"], "semantic_binding_sha256")
            try:
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=transaction_id,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise SupervisedPlanIssuanceError(
                    "issuance is stale, tampered, rolled back, or unproven"
                ) from exc

            issuance = _exact_keys("issuance", root["issuance"], _ISSUANCE_KEYS)
            expected_binding = _digest(
                {
                    "schema": _SCHEMA,
                    "schema_version": _SCHEMA_VERSION,
                    "workspace_instance_id": root["workspace_instance_id"],
                    "issuance": issuance,
                }
            )
            if binding != expected_binding:
                raise SupervisedPlanIssuanceError(
                    "issuance semantic binding does not match durable payload"
                )
            approval = _decode_approval(issuance["approval"])
            bound = _decode_bound(issuance["bound"])
            if bound.execution_plan.plan_id != plan_id:
                raise SupervisedPlanIssuanceError("issuance plan identity changed")
            if (
                approval.fingerprint != bound.approval_fingerprint
                or approval.ledger_identity != bound.execution_plan.approval_id
            ):
                raise SupervisedPlanIssuanceError(
                    "durable approval does not own durable bound plan"
                )
            goal_sha = _sha(
                issuance["economic_goal_contract_sha256"],
                "economic_goal_contract_sha256",
            )
            if goal_sha != bound.economic_goal_contract_sha256:
                raise SupervisedPlanIssuanceError(
                    "durable owner authority identity changed inside issuance"
                )
            requests_raw = issuance["provider_requests"]
            if (
                type(requests_raw) is not list
                or len(requests_raw) > len(bound.execution_plan.actions)
            ):
                raise SupervisedPlanIssuanceError(
                    "provider request identity vector is invalid"
                )
            actions_by_id = {
                action.action_id: action
                for action in bound.execution_plan.actions
            }
            requests: list[dict[str, object]] = []
            seen_request_actions: set[str] = set()
            for item in requests_raw:
                request = _exact_keys(
                    "provider request identity", item, _PROVIDER_REQUEST_KEYS
                )
                action_id = _text(request["action_id"], "provider request action_id")
                bookmaker_id = _text(
                    request["bookmaker_id"],
                    "provider request bookmaker_id",
                )
                account_id = _text(
                    request["account_id"],
                    "provider request account_id",
                )
                _sha(request["instruction_sha256"], "instruction_sha256")
                _text(request["write_adapter_id"], "write_adapter_id")
                _text(request["write_adapter_version"], "write_adapter_version")
                if action_id in seen_request_actions:
                    raise SupervisedPlanIssuanceError(
                        "provider request identity action is duplicated"
                    )
                seen_request_actions.add(action_id)
                action = actions_by_id.get(action_id)
                if (
                    action is None
                    or action.bookmaker_id != bookmaker_id
                    or action.account_id != account_id
                ):
                    raise SupervisedPlanIssuanceError(
                        "provider request identity does not own a durable action"
                    )
                fresh_request = _provider_request_payload(bound, action)
                if fresh_request is None or dict(request) != fresh_request:
                    raise SupervisedPlanIssuanceError(
                        "durable provider request identity no longer matches canonical adapter"
                    )
                requests.append(dict(request))
            return IssuedSupervisedPlan(
                bound=bound,
                approval=approval,
                workspace_instance_id=root["workspace_instance_id"],
                transaction_id=transaction_id,
                semantic_binding_sha256=binding,
                provider_requests=tuple(requests),
            )

    def load(self, plan_id: str) -> IssuedSupervisedPlan:
        _plan_file_name(plan_id)
        with WorkspaceEconomicLock(self.workspace):
            return self._load_locked(plan_id, authority=self._authority(plan_id))


def issue_supervised_execution(
    *,
    store: SupervisedPlanIssuanceStore,
    ledger: RealExecutionLedger,
    portfolio_plan,
    intents,
    routing_proposal,
    profiles,
    approval: SupervisedApproval,
    constraints,
    created_at: str,
) -> IssuedSupervisedPlan:
    """Canonical product approval producer: persist issuance before reservation."""

    if type(store) is not SupervisedPlanIssuanceStore:
        raise SupervisedPlanIssuanceError(
            "store must be exact SupervisedPlanIssuanceStore"
        )
    if type(ledger) is not RealExecutionLedger:
        raise SupervisedPlanIssuanceError("ledger must be exact RealExecutionLedger")
    bound = build_supervised_execution_plan(
        portfolio_plan,
        intents,
        routing_proposal,
        profiles,
        approval,
        constraints,
        created_at=created_at,
    )
    issued = store.issue(bound=bound, approval=approval)
    reserve_supervised_plan(ledger, issued.bound, issued.approval)
    return issued
