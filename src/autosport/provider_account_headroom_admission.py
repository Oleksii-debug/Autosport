"""Fail-closed provider-account headroom composition for supervised real execution.

This module composes canonical provider account acquisition, conservative durable
execution liability, and RealExecutionLedger snapshot-CAS. It intentionally
does not claim provider-side atomic funds reservation, provider write authority,
or whole-product real-money readiness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from enum import Enum
import hashlib
import json
import os
import threading
import weakref

from . import account_snapshot_acquisition as _account_acquisition
from .account_snapshot_acquisition import (
    AccountSnapshotAcquisitionError,
    AuthoritativeAccountSnapshot,
    assert_account_snapshot_acquisition_authoritative,
)
from .bookmaker_capability import BookmakerCapability
from .economic_goal import EconomicGoalContractError
from . import economic_goal_provenance as _economic_goal_provenance
from .economic_goal_provenance import provenance_for
from . import economic_goal_store as _economic_goal_store
from .economic_goal_store import EconomicGoalStore
from . import execution_capital_at_risk as _capital_risk
from .execution_capital_at_risk import (
    ExecutionCapitalAtRiskError,
    ExecutionCapitalAtRiskEvidence,
    resolve_execution_capital_at_risk,
)
from . import portfolio_plan as _portfolio_plan
from .portfolio_plan import OpportunityIntent
from . import risk as _risk
from .risk import ProposedTicketRiskContext
from .real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionAttempt,
    ExecutionLedgerIntegrityError,
    ExecutionStateError,
    RealExecutionLedger,
    VerifiedExecutionPlanView,
)
from . import supervised_execution as _supervised_execution
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedExecutionError,
    assert_bound_supervised_execution_plan_authoritative,
)
from . import workspace_lock as _workspace_lock
from .workspace_lock import WorkspaceEconomicLock


_VERIFIED_SNAPSHOT = RealExecutionLedger.verified_snapshot
_VERIFIED_EXECUTION_VIEW = RealExecutionLedger.verified_execution_view
_BEGIN_ATTEMPT = RealExecutionLedger.begin_attempt
_ATTEMPT_STATE = RealExecutionLedger.attempt_state
_PARSE_VERIFIED_LEDGER = RealExecutionLedger._parse
_PARSE_VERIFIED_LEDGER_FUNC = getattr(_PARSE_VERIFIED_LEDGER, "__func__", None)
_PARSE_VERIFIED_LEDGER_CODE = getattr(_PARSE_VERIFIED_LEDGER_FUNC, "__code__", None)
_VERIFIED_SNAPSHOT_CODE = getattr(_VERIFIED_SNAPSHOT, "__code__", None)
_VERIFIED_EXECUTION_VIEW_CODE = getattr(_VERIFIED_EXECUTION_VIEW, "__code__", None)
_BEGIN_ATTEMPT_CODE = getattr(_BEGIN_ATTEMPT, "__code__", None)
_ATTEMPT_STATE_CODE = getattr(_ATTEMPT_STATE, "__code__", None)
_LEDGER_CANONICAL_MONOTONIC_AUTHORITY = RealExecutionLedger._canonical_monotonic_authority
_LEDGER_CANONICAL_MONOTONIC_AUTHORITY_CODE = getattr(
    _LEDGER_CANONICAL_MONOTONIC_AUTHORITY,
    "__code__",
    None,
)
_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY = assert_account_snapshot_acquisition_authoritative
_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY_CODE = getattr(
    _ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY,
    "__code__",
    None,
)
_RESOLVE_CAPITAL_AT_RISK = resolve_execution_capital_at_risk
_RESOLVE_CAPITAL_AT_RISK_CODE = getattr(_RESOLVE_CAPITAL_AT_RISK, "__code__", None)
_ASSERT_CAPITAL_ISSUED_CURRENT = ExecutionCapitalAtRiskEvidence.assert_issued_current
_ASSERT_CAPITAL_ISSUED_CURRENT_CODE = getattr(
    _ASSERT_CAPITAL_ISSUED_CURRENT,
    "__code__",
    None,
)
_ECONOMIC_GOAL_STORE_TYPE = EconomicGoalStore
_ECONOMIC_GOAL_STORE_FILE_NAME = EconomicGoalStore.FILE_NAME
_ECONOMIC_GOAL_STORE_INIT = EconomicGoalStore.__init__
_ECONOMIC_GOAL_STORE_INIT_CODE = getattr(_ECONOMIC_GOAL_STORE_INIT, "__code__", None)
_ECONOMIC_GOAL_STORE_LOAD = EconomicGoalStore.load
_ECONOMIC_GOAL_STORE_LOAD_CODE = getattr(_ECONOMIC_GOAL_STORE_LOAD, "__code__", None)
_ECONOMIC_GOAL_FROM_JSON = getattr(_economic_goal_store, "economic_goal_from_json", None)
_ECONOMIC_GOAL_FROM_JSON_CODE = getattr(_ECONOMIC_GOAL_FROM_JSON, "__code__", None)
_ECONOMIC_GOAL_FROM_PAYLOAD = getattr(_economic_goal_store, "economic_goal_from_payload", None)
_ECONOMIC_GOAL_FROM_PAYLOAD_CODE = getattr(
    _ECONOMIC_GOAL_FROM_PAYLOAD,
    "__code__",
    None,
)
_ECONOMIC_GOAL_STRICT_JSON_LOADS = getattr(_economic_goal_store, "strict_json_loads", None)
_ECONOMIC_GOAL_STRICT_JSON_LOADS_CODE = getattr(
    _ECONOMIC_GOAL_STRICT_JSON_LOADS,
    "__code__",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_FOR = provenance_for
_ECONOMIC_GOAL_PROVENANCE_FOR_CODE = getattr(
    _ECONOMIC_GOAL_PROVENANCE_FOR,
    "__code__",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_CONTRACT_SHA256 = getattr(
    _economic_goal_provenance,
    "contract_sha256",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_CONTRACT_SHA256_CODE = getattr(
    _ECONOMIC_GOAL_PROVENANCE_CONTRACT_SHA256,
    "__code__",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_CANONICAL_JSON = getattr(
    _economic_goal_provenance,
    "_canonical_json",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_CANONICAL_JSON_CODE = getattr(
    _ECONOMIC_GOAL_PROVENANCE_CANONICAL_JSON,
    "__code__",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_TO_PAYLOAD = getattr(
    _economic_goal_provenance,
    "economic_goal_to_payload",
    None,
)
_ECONOMIC_GOAL_PROVENANCE_TO_PAYLOAD_CODE = getattr(
    _ECONOMIC_GOAL_PROVENANCE_TO_PAYLOAD,
    "__code__",
    None,
)
_BOUND_SUPERVISED_PLAN_TYPE = BoundSupervisedExecutionPlan
_BOUND_SUPERVISED_BINDING_SHA256 = getattr(
    _supervised_execution,
    "_bound_binding_sha256",
    None,
)
_BOUND_SUPERVISED_BINDING_SHA256_CODE = getattr(
    _BOUND_SUPERVISED_BINDING_SHA256,
    "__code__",
    None,
)
_BOUND_SUPERVISED_PLAN_WITNESS = getattr(
    _supervised_execution,
    "_bound_plan_witness",
    None,
)
_BOUND_SUPERVISED_PLAN_WITNESS_CODE = getattr(
    _BOUND_SUPERVISED_PLAN_WITNESS,
    "__code__",
    None,
)
_BOUND_SUPERVISED_DIGEST = getattr(_supervised_execution, "_digest", None)
_BOUND_SUPERVISED_DIGEST_CODE = getattr(
    _BOUND_SUPERVISED_DIGEST,
    "__code__",
    None,
)
_BOUND_SUPERVISED_PLAN_VERIFY = BoundSupervisedExecutionPlan.verify_binding
_BOUND_SUPERVISED_PLAN_VERIFY_CODE = getattr(
    _BOUND_SUPERVISED_PLAN_VERIFY,
    "__code__",
    None,
)
_ASSERT_BOUND_SUPERVISED_PLAN_AUTHORITY = (
    assert_bound_supervised_execution_plan_authoritative
)
_ASSERT_BOUND_SUPERVISED_PLAN_AUTHORITY_CODE = getattr(
    _ASSERT_BOUND_SUPERVISED_PLAN_AUTHORITY,
    "__code__",
    None,
)
_OPPORTUNITY_INTENT_TYPE = OpportunityIntent
_OPPORTUNITY_INTENT_ID_DESCRIPTOR = vars(OpportunityIntent).get("intent_id")
_OPPORTUNITY_INTENT_CONTEXT_DESCRIPTOR = vars(OpportunityIntent).get("risk_context")
_OPPORTUNITY_INTENT_SHA_DESCRIPTOR = vars(OpportunityIntent).get("intent_sha256")
_OPPORTUNITY_INTENT_SHA_GETTER = getattr(
    _OPPORTUNITY_INTENT_SHA_DESCRIPTOR,
    "fget",
    None,
)
_OPPORTUNITY_INTENT_SHA_GETTER_CODE = getattr(
    _OPPORTUNITY_INTENT_SHA_GETTER,
    "__code__",
    None,
)
_OPPORTUNITY_INTENT_CANDIDATE_DESCRIPTOR = vars(OpportunityIntent).get(
    "candidate_sha256"
)
_OPPORTUNITY_INTENT_CANDIDATE_GETTER = getattr(
    _OPPORTUNITY_INTENT_CANDIDATE_DESCRIPTOR,
    "fget",
    None,
)
_OPPORTUNITY_INTENT_CANDIDATE_GETTER_CODE = getattr(
    _OPPORTUNITY_INTENT_CANDIDATE_GETTER,
    "__code__",
    None,
)
_PORTFOLIO_INTENT_SHA256_PAYLOAD = getattr(_portfolio_plan, "_sha256_payload", None)
_PORTFOLIO_INTENT_SHA256_PAYLOAD_CODE = getattr(
    _PORTFOLIO_INTENT_SHA256_PAYLOAD,
    "__code__",
    None,
)
_PAPER_RISK_POLICY_TYPE = getattr(_risk, "PaperRiskPolicy", None)
_PAPER_RISK_CANDIDATE_DESCRIPTOR = (
    vars(_PAPER_RISK_POLICY_TYPE).get("risk_of_ruin_candidate_sha256")
    if _PAPER_RISK_POLICY_TYPE is not None
    else None
)
_PAPER_RISK_CANDIDATE_SHA256 = (
    getattr(_PAPER_RISK_POLICY_TYPE, "risk_of_ruin_candidate_sha256", None)
    if _PAPER_RISK_POLICY_TYPE is not None
    else None
)
_PAPER_RISK_CANDIDATE_SHA256_CODE = getattr(
    _PAPER_RISK_CANDIDATE_SHA256,
    "__code__",
    None,
)
_RISK_SHA256_PAYLOAD = getattr(_risk, "_sha256_payload", None)
_RISK_SHA256_PAYLOAD_CODE = getattr(_RISK_SHA256_PAYLOAD, "__code__", None)
_PROPOSED_RISK_CONTEXT_TYPE = ProposedTicketRiskContext
_RISK_CONTEXT_PROVIDER_ACCOUNTS_DESCRIPTOR = vars(ProposedTicketRiskContext).get(
    "provider_accounts"
)
_RISK_CONTEXT_BANKROLL_DESCRIPTOR = vars(ProposedTicketRiskContext).get("bankroll_id")
_RISK_CONTEXT_CURRENCY_DESCRIPTOR = vars(ProposedTicketRiskContext).get("currency")
_WORKSPACE_ECONOMIC_LOCK_TYPE = WorkspaceEconomicLock
_WORKSPACE_ECONOMIC_LOCK_FILE_NAME = WorkspaceEconomicLock.FILE_NAME
_WORKSPACE_ECONOMIC_LOCK_INIT = WorkspaceEconomicLock.__init__
_WORKSPACE_ECONOMIC_LOCK_INIT_CODE = getattr(
    _WORKSPACE_ECONOMIC_LOCK_INIT,
    "__code__",
    None,
)
_WORKSPACE_ECONOMIC_LOCK_ACQUIRE = WorkspaceEconomicLock.acquire
_WORKSPACE_ECONOMIC_LOCK_ACQUIRE_CODE = getattr(
    _WORKSPACE_ECONOMIC_LOCK_ACQUIRE,
    "__code__",
    None,
)
_WORKSPACE_ECONOMIC_LOCK_RELEASE = WorkspaceEconomicLock.release
_WORKSPACE_ECONOMIC_LOCK_RELEASE_CODE = getattr(
    _WORKSPACE_ECONOMIC_LOCK_RELEASE,
    "__code__",
    None,
)
_WORKSPACE_ECONOMIC_LOCK_PATH_FACTORY = getattr(_workspace_lock, "Path", None)
_WORKSPACE_ECONOMIC_LOCK_ENTER = WorkspaceEconomicLock.__enter__
_WORKSPACE_ECONOMIC_LOCK_ENTER_CODE = getattr(
    _WORKSPACE_ECONOMIC_LOCK_ENTER,
    "__code__",
    None,
)
_WORKSPACE_ECONOMIC_LOCK_EXIT = WorkspaceEconomicLock.__exit__
_WORKSPACE_ECONOMIC_LOCK_EXIT_CODE = getattr(
    _WORKSPACE_ECONOMIC_LOCK_EXIT,
    "__code__",
    None,
)


class ProviderAccountHeadroomError(RuntimeError):
    """Base error for provider-account capital-axis admission evidence."""


class ProviderAccountHeadroomStale(ProviderAccountHeadroomError):
    """An input generation moved before a new internal reservation committed."""


class ProviderAccountHeadroomUnsupported(ProviderAccountHeadroomError):
    """Current canonical evidence cannot support this account/action shape."""


class HeadroomDecision(str, Enum):
    SUFFICIENT_LOWER_BOUND = "SUFFICIENT_LOWER_BOUND"
    INSUFFICIENT_UPPER_BOUND = "INSUFFICIENT_UPPER_BOUND"
    WAIT_COVERAGE = "WAIT_COVERAGE"


def _canonical_capital_risk_dispatch(
    *,
    _resolve=_RESOLVE_CAPITAL_AT_RISK,
    _resolve_code=_RESOLVE_CAPITAL_AT_RISK_CODE,
    _assert_current=_ASSERT_CAPITAL_ISSUED_CURRENT,
    _assert_current_code=_ASSERT_CAPITAL_ISSUED_CURRENT_CODE,
):
    live_resolve = getattr(_capital_risk, "resolve_execution_capital_at_risk", None)
    live_assert = vars(ExecutionCapitalAtRiskEvidence).get("assert_issued_current")
    if (
        live_resolve is not _resolve
        or globals().get("resolve_execution_capital_at_risk") is not _resolve
        or getattr(_resolve, "__code__", None) is not _resolve_code
        or live_assert is not _assert_current
        or getattr(_assert_current, "__code__", None) is not _assert_current_code
    ):
        raise ProviderAccountHeadroomError(
            "canonical capital-at-risk headroom authority changed"
        )
    return _resolve, _assert_current


def _canonical_ledger_workspace(
    ledger: RealExecutionLedger,
    *,
    _authority=_LEDGER_CANONICAL_MONOTONIC_AUTHORITY,
    _authority_code=_LEDGER_CANONICAL_MONOTONIC_AUTHORITY_CODE,
):
    live = vars(RealExecutionLedger).get("_canonical_monotonic_authority")
    if (
        live is not _authority
        or getattr(_authority, "__code__", None) is not _authority_code
    ):
        raise ProviderAccountHeadroomError(
            "canonical execution-ledger workspace authority changed"
        )
    try:
        authority = _authority(ledger)
        path = ledger.path.resolve(strict=False)
        authority_workspace = authority.workspace.resolve(strict=False)
        authority_key = authority.key
    except (AttributeError, OSError, TypeError, ExecutionLedgerIntegrityError) as exc:
        raise ProviderAccountHeadroomError(
            "canonical execution-ledger workspace identity is unavailable"
        ) from exc
    if (
        os.path.normcase(str(path.parent))
        != os.path.normcase(str(authority_workspace))
        or os.path.normcase(path.name) != os.path.normcase(authority_key)
    ):
        raise ProviderAccountHeadroomError(
            "execution-ledger path no longer matches canonical workspace authority"
        )
    return authority_workspace


def _canonical_denomination_dispatch(
    *,
    _store_type=_ECONOMIC_GOAL_STORE_TYPE,
    _store_file_name=_ECONOMIC_GOAL_STORE_FILE_NAME,
    _store_init=_ECONOMIC_GOAL_STORE_INIT,
    _store_init_code=_ECONOMIC_GOAL_STORE_INIT_CODE,
    _store_load=_ECONOMIC_GOAL_STORE_LOAD,
    _store_load_code=_ECONOMIC_GOAL_STORE_LOAD_CODE,
    _store_from_json=_ECONOMIC_GOAL_FROM_JSON,
    _store_from_json_code=_ECONOMIC_GOAL_FROM_JSON_CODE,
    _store_from_payload=_ECONOMIC_GOAL_FROM_PAYLOAD,
    _store_from_payload_code=_ECONOMIC_GOAL_FROM_PAYLOAD_CODE,
    _store_strict_json=_ECONOMIC_GOAL_STRICT_JSON_LOADS,
    _store_strict_json_code=_ECONOMIC_GOAL_STRICT_JSON_LOADS_CODE,
    _provenance=_ECONOMIC_GOAL_PROVENANCE_FOR,
    _provenance_code=_ECONOMIC_GOAL_PROVENANCE_FOR_CODE,
    _provenance_contract_sha=_ECONOMIC_GOAL_PROVENANCE_CONTRACT_SHA256,
    _provenance_contract_sha_code=_ECONOMIC_GOAL_PROVENANCE_CONTRACT_SHA256_CODE,
    _provenance_canonical_json=_ECONOMIC_GOAL_PROVENANCE_CANONICAL_JSON,
    _provenance_canonical_json_code=_ECONOMIC_GOAL_PROVENANCE_CANONICAL_JSON_CODE,
    _provenance_to_payload=_ECONOMIC_GOAL_PROVENANCE_TO_PAYLOAD,
    _provenance_to_payload_code=_ECONOMIC_GOAL_PROVENANCE_TO_PAYLOAD_CODE,
    _bound_type=_BOUND_SUPERVISED_PLAN_TYPE,
    _bound_binding_sha=_BOUND_SUPERVISED_BINDING_SHA256,
    _bound_binding_sha_code=_BOUND_SUPERVISED_BINDING_SHA256_CODE,
    _bound_witness=_BOUND_SUPERVISED_PLAN_WITNESS,
    _bound_witness_code=_BOUND_SUPERVISED_PLAN_WITNESS_CODE,
    _bound_digest=_BOUND_SUPERVISED_DIGEST,
    _bound_digest_code=_BOUND_SUPERVISED_DIGEST_CODE,
    _bound_verify=_BOUND_SUPERVISED_PLAN_VERIFY,
    _bound_verify_code=_BOUND_SUPERVISED_PLAN_VERIFY_CODE,
    _bound_assert=_ASSERT_BOUND_SUPERVISED_PLAN_AUTHORITY,
    _bound_assert_code=_ASSERT_BOUND_SUPERVISED_PLAN_AUTHORITY_CODE,
    _lock_type=_WORKSPACE_ECONOMIC_LOCK_TYPE,
    _lock_file_name=_WORKSPACE_ECONOMIC_LOCK_FILE_NAME,
    _lock_init=_WORKSPACE_ECONOMIC_LOCK_INIT,
    _lock_init_code=_WORKSPACE_ECONOMIC_LOCK_INIT_CODE,
    _lock_acquire=_WORKSPACE_ECONOMIC_LOCK_ACQUIRE,
    _lock_acquire_code=_WORKSPACE_ECONOMIC_LOCK_ACQUIRE_CODE,
    _lock_release=_WORKSPACE_ECONOMIC_LOCK_RELEASE,
    _lock_release_code=_WORKSPACE_ECONOMIC_LOCK_RELEASE_CODE,
    _lock_path_factory=_WORKSPACE_ECONOMIC_LOCK_PATH_FACTORY,
    _lock_enter=_WORKSPACE_ECONOMIC_LOCK_ENTER,
    _lock_enter_code=_WORKSPACE_ECONOMIC_LOCK_ENTER_CODE,
    _lock_exit=_WORKSPACE_ECONOMIC_LOCK_EXIT,
    _lock_exit_code=_WORKSPACE_ECONOMIC_LOCK_EXIT_CODE,
):
    live_store_type = getattr(_economic_goal_store, "EconomicGoalStore", None)
    live_store_init = (
        vars(live_store_type).get("__init__") if live_store_type is _store_type else None
    )
    live_store_load = (
        vars(live_store_type).get("load") if live_store_type is _store_type else None
    )
    live_bound_type = getattr(
        _supervised_execution,
        "BoundSupervisedExecutionPlan",
        None,
    )
    live_bound_verify = (
        vars(live_bound_type).get("verify_binding")
        if live_bound_type is _bound_type
        else None
    )
    live_bound_assert = getattr(
        _supervised_execution,
        "assert_bound_supervised_execution_plan_authoritative",
        None,
    )
    live_lock_type = getattr(_workspace_lock, "WorkspaceEconomicLock", None)
    live_lock_init = (
        vars(live_lock_type).get("__init__") if live_lock_type is _lock_type else None
    )
    live_lock_acquire = (
        vars(live_lock_type).get("acquire") if live_lock_type is _lock_type else None
    )
    live_lock_release = (
        vars(live_lock_type).get("release") if live_lock_type is _lock_type else None
    )
    live_lock_enter = (
        vars(live_lock_type).get("__enter__") if live_lock_type is _lock_type else None
    )
    live_lock_exit = (
        vars(live_lock_type).get("__exit__") if live_lock_type is _lock_type else None
    )
    if (
        live_store_type is not _store_type
        or globals().get("EconomicGoalStore") is not _store_type
        or vars(live_store_type).get("FILE_NAME") != _store_file_name
        or live_store_init is not _store_init
        or getattr(_store_init, "__code__", None) is not _store_init_code
        or live_store_load is not _store_load
        or getattr(_store_load, "__code__", None) is not _store_load_code
        or getattr(_economic_goal_provenance, "provenance_for", None) is not _provenance
        or globals().get("provenance_for") is not _provenance
        or getattr(_provenance, "__code__", None) is not _provenance_code
        or getattr(_economic_goal_provenance, "contract_sha256", None)
        is not _provenance_contract_sha
        or getattr(_provenance_contract_sha, "__code__", None)
        is not _provenance_contract_sha_code
        or getattr(_economic_goal_provenance, "_canonical_json", None)
        is not _provenance_canonical_json
        or getattr(_provenance_canonical_json, "__code__", None)
        is not _provenance_canonical_json_code
        or getattr(_economic_goal_provenance, "economic_goal_to_payload", None)
        is not _provenance_to_payload
        or getattr(_provenance_to_payload, "__code__", None)
        is not _provenance_to_payload_code
        or live_bound_type is not _bound_type
        or globals().get("BoundSupervisedExecutionPlan") is not _bound_type
        or getattr(_supervised_execution, "_bound_binding_sha256", None)
        is not _bound_binding_sha
        or getattr(_bound_binding_sha, "__code__", None) is not _bound_binding_sha_code
        or getattr(_supervised_execution, "_bound_plan_witness", None)
        is not _bound_witness
        or getattr(_bound_witness, "__code__", None) is not _bound_witness_code
        or getattr(_supervised_execution, "_digest", None) is not _bound_digest
        or getattr(_bound_digest, "__code__", None) is not _bound_digest_code
        or live_bound_verify is not _bound_verify
        or getattr(_bound_verify, "__code__", None) is not _bound_verify_code
        or live_bound_assert is not _bound_assert
        or globals().get("assert_bound_supervised_execution_plan_authoritative")
        is not _bound_assert
        or getattr(_bound_assert, "__code__", None) is not _bound_assert_code
        or live_lock_type is not _lock_type
        or globals().get("WorkspaceEconomicLock") is not _lock_type
        or vars(live_lock_type).get("FILE_NAME") != _lock_file_name
        or live_lock_init is not _lock_init
        or getattr(_lock_init, "__code__", None) is not _lock_init_code
        or live_lock_acquire is not _lock_acquire
        or getattr(_lock_acquire, "__code__", None) is not _lock_acquire_code
        or live_lock_release is not _lock_release
        or getattr(_lock_release, "__code__", None) is not _lock_release_code
        or getattr(_workspace_lock, "Path", None) is not _lock_path_factory
        or live_lock_enter is not _lock_enter
        or getattr(_lock_enter, "__code__", None) is not _lock_enter_code
        or live_lock_exit is not _lock_exit
        or getattr(_lock_exit, "__code__", None) is not _lock_exit_code
    ):
        raise ProviderAccountHeadroomError(
            "canonical monetary denomination authority changed"
        )
    return (
        _store_type,
        _store_load,
        _provenance,
        _bound_type,
        _bound_verify,
        _bound_assert,
        _lock_type,
    )


def _canonical_economic_lock(workspace):
    lock_type = _canonical_denomination_dispatch()[-1]
    lock = lock_type(workspace)
    expected_path = workspace / _WORKSPACE_ECONOMIC_LOCK_FILE_NAME
    lock_workspace = getattr(lock, "workspace", None)
    lock_path = getattr(lock, "path", None)
    if (
        type(lock) is not lock_type
        or type(lock_workspace) is not type(workspace)
        or lock_workspace != workspace
        or type(lock_path) is not type(expected_path)
        or lock_path != expected_path
    ):
        raise ProviderAccountHeadroomError(
            "canonical economic lock construction authority changed"
        )
    return lock


def _canonical_intent_denomination_dispatch(
    *,
    _intent_type=_OPPORTUNITY_INTENT_TYPE,
    _intent_id_descriptor=_OPPORTUNITY_INTENT_ID_DESCRIPTOR,
    _intent_context_descriptor=_OPPORTUNITY_INTENT_CONTEXT_DESCRIPTOR,
    _intent_sha_descriptor=_OPPORTUNITY_INTENT_SHA_DESCRIPTOR,
    _intent_sha_getter=_OPPORTUNITY_INTENT_SHA_GETTER,
    _intent_sha_getter_code=_OPPORTUNITY_INTENT_SHA_GETTER_CODE,
    _intent_candidate_descriptor=_OPPORTUNITY_INTENT_CANDIDATE_DESCRIPTOR,
    _intent_candidate_getter=_OPPORTUNITY_INTENT_CANDIDATE_GETTER,
    _intent_candidate_getter_code=_OPPORTUNITY_INTENT_CANDIDATE_GETTER_CODE,
    _intent_hash_payload=_PORTFOLIO_INTENT_SHA256_PAYLOAD,
    _intent_hash_payload_code=_PORTFOLIO_INTENT_SHA256_PAYLOAD_CODE,
    _risk_policy_type=_PAPER_RISK_POLICY_TYPE,
    _risk_candidate_descriptor=_PAPER_RISK_CANDIDATE_DESCRIPTOR,
    _risk_candidate_sha=_PAPER_RISK_CANDIDATE_SHA256,
    _risk_candidate_sha_code=_PAPER_RISK_CANDIDATE_SHA256_CODE,
    _risk_hash_payload=_RISK_SHA256_PAYLOAD,
    _risk_hash_payload_code=_RISK_SHA256_PAYLOAD_CODE,
    _context_type=_PROPOSED_RISK_CONTEXT_TYPE,
    _provider_accounts_descriptor=_RISK_CONTEXT_PROVIDER_ACCOUNTS_DESCRIPTOR,
    _bankroll_descriptor=_RISK_CONTEXT_BANKROLL_DESCRIPTOR,
    _currency_descriptor=_RISK_CONTEXT_CURRENCY_DESCRIPTOR,
):
    live_intent_type = getattr(_portfolio_plan, "OpportunityIntent", None)
    live_context_type = getattr(_risk, "ProposedTicketRiskContext", None)
    live_risk_policy_type = getattr(_risk, "PaperRiskPolicy", None)
    portfolio_risk_policy_type = getattr(_portfolio_plan, "PaperRiskPolicy", None)
    if (
        live_intent_type is not _intent_type
        or globals().get("OpportunityIntent") is not _intent_type
        or vars(_intent_type).get("intent_id") is not _intent_id_descriptor
        or vars(_intent_type).get("risk_context") is not _intent_context_descriptor
        or vars(_intent_type).get("intent_sha256") is not _intent_sha_descriptor
        or getattr(_intent_sha_descriptor, "fget", None) is not _intent_sha_getter
        or getattr(_intent_sha_getter, "__code__", None) is not _intent_sha_getter_code
        or vars(_intent_type).get("candidate_sha256") is not _intent_candidate_descriptor
        or getattr(_intent_candidate_descriptor, "fget", None)
        is not _intent_candidate_getter
        or getattr(_intent_candidate_getter, "__code__", None)
        is not _intent_candidate_getter_code
        or getattr(_portfolio_plan, "_sha256_payload", None) is not _intent_hash_payload
        or getattr(_intent_hash_payload, "__code__", None)
        is not _intent_hash_payload_code
        or live_risk_policy_type is not _risk_policy_type
        or portfolio_risk_policy_type is not _risk_policy_type
        or vars(_risk_policy_type).get("risk_of_ruin_candidate_sha256")
        is not _risk_candidate_descriptor
        or getattr(_risk_policy_type, "risk_of_ruin_candidate_sha256", None)
        is not _risk_candidate_sha
        or getattr(_risk_candidate_sha, "__code__", None)
        is not _risk_candidate_sha_code
        or getattr(_risk, "_sha256_payload", None) is not _risk_hash_payload
        or getattr(_risk_hash_payload, "__code__", None) is not _risk_hash_payload_code
        or live_context_type is not _context_type
        or globals().get("ProposedTicketRiskContext") is not _context_type
        or vars(_context_type).get("provider_accounts")
        is not _provider_accounts_descriptor
        or vars(_context_type).get("bankroll_id") is not _bankroll_descriptor
        or vars(_context_type).get("currency") is not _currency_descriptor
    ):
        raise ProviderAccountHeadroomError(
            "canonical opportunity-intent denomination authority changed"
        )
    return (
        _intent_type,
        _intent_id_descriptor,
        _intent_context_descriptor,
        _intent_sha_descriptor,
        _context_type,
        _provider_accounts_descriptor,
        _bankroll_descriptor,
        _currency_descriptor,
    )


def _canonical_account_snapshot_authority(
    *,
    _assert=_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY,
    _assert_code=_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY_CODE,
):
    live_module = getattr(
        _account_acquisition,
        "assert_account_snapshot_acquisition_authoritative",
        None,
    )
    live_alias = globals().get("assert_account_snapshot_acquisition_authoritative")
    if (
        live_module is not _assert
        or live_alias is not _assert
        or getattr(_assert, "__code__", None) is not _assert_code
    ):
        raise ProviderAccountHeadroomError(
            "canonical account snapshot headroom authority changed"
        )
    return _assert


def _canonical_ledger_dispatch(
    *,
    _snapshot=_VERIFIED_SNAPSHOT,
    _snapshot_code=_VERIFIED_SNAPSHOT_CODE,
    _view=_VERIFIED_EXECUTION_VIEW,
    _view_code=_VERIFIED_EXECUTION_VIEW_CODE,
    _begin=_BEGIN_ATTEMPT,
    _begin_code=_BEGIN_ATTEMPT_CODE,
    _state=_ATTEMPT_STATE,
    _state_code=_ATTEMPT_STATE_CODE,
):
    expected = (_snapshot, _view, _begin, _state)
    live_class = (
        vars(RealExecutionLedger).get("verified_snapshot"),
        vars(RealExecutionLedger).get("verified_execution_view"),
        vars(RealExecutionLedger).get("begin_attempt"),
        vars(RealExecutionLedger).get("attempt_state"),
    )
    live_aliases = (
        globals().get("_VERIFIED_SNAPSHOT"),
        globals().get("_VERIFIED_EXECUTION_VIEW"),
        globals().get("_BEGIN_ATTEMPT"),
        globals().get("_ATTEMPT_STATE"),
    )
    expected_codes = (
        _snapshot_code,
        _view_code,
        _begin_code,
        _state_code,
    )
    if (
        live_class != expected
        or live_aliases != expected
        or tuple(getattr(item, "__code__", None) for item in expected)
        != expected_codes
    ):
        raise ProviderAccountHeadroomError(
            "canonical execution ledger headroom authority changed"
        )
    return expected


_PRODUCT_MAX_ACCOUNT_SNAPSHOT_AGE = timedelta(seconds=30)
# Version 2 binds the exact durable economic-goal denomination and the set of
# liability-bearing supervised execution plans into every assessment identity.
_SCHEMA_VERSION = 2
_ZERO = Decimal("0")


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAccountHeadroomError(f"{field} must be non-empty trimmed text")
    return value


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ProviderAccountHeadroomError(
            f"{field} must be lowercase 64-character SHA-256 text"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderAccountHeadroomError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderAccountHeadroomError(f"{field} must include timezone offset")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ProviderAccountHeadroomError(f"{field} must be exact finite Decimal")
    if value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ProviderAccountHeadroomError(f"{field} must be {qualifier}")
    return value


def _add(left: Decimal, right: Decimal) -> Decimal:
    _decimal(left, "left")
    _decimal(right, "right")
    required = max(
        len(left.as_tuple().digits) + abs(int(left.as_tuple().exponent)),
        len(right.as_tuple().digits) + abs(int(right.as_tuple().exponent)),
        64,
    ) + 4
    if required > 4096:
        raise ProviderAccountHeadroomUnsupported(
            "capital scale exceeds bounded exact arithmetic"
        )
    with localcontext() as context:
        context.prec = required
        result = left + right
    if not result.is_finite():
        raise ProviderAccountHeadroomError("capital addition became non-finite")
    return result


def _subtract_floor_zero(left: Decimal, right: Decimal) -> Decimal:
    _decimal(left, "left")
    _decimal(right, "right")
    required = max(
        len(left.as_tuple().digits) + abs(int(left.as_tuple().exponent)),
        len(right.as_tuple().digits) + abs(int(right.as_tuple().exponent)),
        64,
    ) + 4
    if required > 4096:
        raise ProviderAccountHeadroomUnsupported(
            "capital scale exceeds bounded exact arithmetic"
        )
    with localcontext() as context:
        context.prec = required
        result = left - right
    if not result.is_finite():
        raise ProviderAccountHeadroomError("capital subtraction became non-finite")
    return max(_ZERO, result)


def _decimal_text(value: Decimal) -> str:
    _decimal(value, "decimal")
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    raw = list(digits)
    exp = int(exponent)
    while raw and raw[-1] == 0:
        raw.pop()
        exp += 1
    coefficient = "".join(str(digit) for digit in raw) or "0"
    return ("-" if sign else "") + coefficient + f"e{exp}"


def _canonical_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_HEADROOM_CANONICAL_DIGEST = _canonical_digest
_HEADROOM_CANONICAL_DIGEST_CODE = getattr(_HEADROOM_CANONICAL_DIGEST, "__code__", None)
_HEADROOM_JSON_DUMPS = json.dumps
_HEADROOM_HASHLIB_SHA256 = hashlib.sha256


def _assert_headroom_digest_authority(
    *,
    _digest=_HEADROOM_CANONICAL_DIGEST,
    _digest_code=_HEADROOM_CANONICAL_DIGEST_CODE,
    _json_dumps=_HEADROOM_JSON_DUMPS,
    _sha256=_HEADROOM_HASHLIB_SHA256,
) -> None:
    if (
        globals().get("_canonical_digest") is not _digest
        or getattr(_digest, "__code__", None) is not _digest_code
        or getattr(json, "dumps", None) is not _json_dumps
        or getattr(hashlib, "sha256", None) is not _sha256
    ):
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom digest authority changed"
        )


_HEADROOM_DATETIME_NOW = datetime.now
_HEADROOM_UTC = timezone.utc


def _utc_now(
    *,
    _datetime_now=_HEADROOM_DATETIME_NOW,
    _utc=_HEADROOM_UTC,
) -> datetime:
    return _datetime_now(_utc)


def _read_headroom_utc_now(
    *,
    _clock=_utc_now,
    _clock_code=getattr(_utc_now, "__code__", None),
    _datetime_now=_HEADROOM_DATETIME_NOW,
    _utc=_HEADROOM_UTC,
) -> datetime:
    live = globals().get("_utc_now")
    kwdefaults = getattr(_clock, "__kwdefaults__", None)
    if (
        live is not _clock
        or getattr(live, "__code__", None) is not _clock_code
        or type(kwdefaults) is not dict
        or kwdefaults.get("_datetime_now") is not _datetime_now
        or kwdefaults.get("_utc") is not _utc
    ):
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom clock authority changed"
        )
    value = _clock()
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom clock returned invalid time"
        )
    return value.astimezone(timezone.utc)


def _ledger_plan_ids(snapshot_payload: bytes) -> tuple[str, ...]:
    """Enumerate plans through the canonical RealExecutionLedger envelope parser."""
    if type(snapshot_payload) is not bytes:
        raise ProviderAccountHeadroomError("verified ledger payload must be exact bytes")
    parse_func = getattr(_PARSE_VERIFIED_LEDGER, "__func__", None)
    if (
        parse_func is not _PARSE_VERIFIED_LEDGER_FUNC
        or getattr(parse_func, "__code__", None) is not _PARSE_VERIFIED_LEDGER_CODE
    ):
        raise ProviderAccountHeadroomError(
            "canonical execution ledger parser authority changed"
        )
    try:
        events = _PARSE_VERIFIED_LEDGER(snapshot_payload)
    except ExecutionLedgerIntegrityError as exc:
        raise ProviderAccountHeadroomError(
            "verified ledger payload could not be enumerated canonically"
        ) from exc
    plan_ids: set[str] = set()
    for event in events:
        if type(event) is not dict:
            raise ProviderAccountHeadroomError("verified ledger event must be an object")
        plan_id = event.get("plan_id")
        if type(plan_id) is not str or not plan_id:
            raise ProviderAccountHeadroomError(
                "verified ledger event lacks canonical plan identity"
            )
        plan_ids.add(plan_id)
    return tuple(sorted(plan_ids))


def _find_action(
    view: VerifiedExecutionPlanView,
    action_id: str,
) -> ExecutionAction:
    matches = tuple(action for action in view.plan.actions if action.action_id == action_id)
    if len(matches) != 1:
        raise ProviderAccountHeadroomUnsupported(
            "target action is not uniquely present in the canonical execution plan"
        )
    return matches[0]


def _classify(
    lower_headroom: Decimal,
    upper_headroom: Decimal,
    proposed_liability: Decimal,
) -> HeadroomDecision:
    _decimal(lower_headroom, "lower_headroom")
    _decimal(upper_headroom, "upper_headroom")
    _decimal(proposed_liability, "proposed_liability", positive=True)
    if lower_headroom >= proposed_liability:
        return HeadroomDecision.SUFFICIENT_LOWER_BOUND
    if upper_headroom < proposed_liability:
        return HeadroomDecision.INSUFFICIENT_UPPER_BOUND
    return HeadroomDecision.WAIT_COVERAGE


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderAccountHeadroomAssessment:
    provider_id: str
    account_id: str
    currency: str
    economic_goal_contract_sha256: str
    denomination_authority_sha256: str
    acquisition_id: str
    acquisition_snapshot_sha256: str
    acquired_at: str
    balance_observed_at: str
    expires_at: str
    ledger_snapshot_sha256: str
    ledger_event_count: int
    plan_id: str
    action_id: str
    action_fingerprint: str
    proposed_liability: Decimal
    provider_available_to_bet: Decimal
    definitely_unreflected_product_liability: Decimal
    unknown_reflection_product_liability: Decimal
    lower_headroom: Decimal
    upper_headroom: Decimal
    decision: HeadroomDecision
    evidence_sha256: str
    provider_atomicity_proven: bool = False
    provider_balance_generation_cas_proven: bool = False
    execution_authority: bool = False
    real_money_readiness: bool = False
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field in (
            "provider_id",
            "account_id",
            "currency",
            "acquisition_id",
            "acquired_at",
            "balance_observed_at",
            "expires_at",
            "plan_id",
            "action_id",
        ):
            _text(getattr(self, field), field)
        for field in (
            "economic_goal_contract_sha256",
            "denomination_authority_sha256",
            "acquisition_snapshot_sha256",
            "ledger_snapshot_sha256",
            "action_fingerprint",
            "evidence_sha256",
        ):
            _sha(getattr(self, field), field)
        if type(self.ledger_event_count) is not int or self.ledger_event_count < 0:
            raise ProviderAccountHeadroomError(
                "ledger_event_count must be non-negative exact int"
            )
        if type(self.decision) is not HeadroomDecision:
            raise ProviderAccountHeadroomError("decision must be exact HeadroomDecision")
        for field in (
            "proposed_liability",
            "provider_available_to_bet",
            "definitely_unreflected_product_liability",
            "unknown_reflection_product_liability",
            "lower_headroom",
            "upper_headroom",
        ):
            _decimal(
                getattr(self, field),
                field,
                positive=(field == "proposed_liability"),
            )
        if self.lower_headroom > self.upper_headroom:
            raise ProviderAccountHeadroomError(
                "lower_headroom cannot exceed upper_headroom"
            )
        expected_upper = _subtract_floor_zero(
            self.provider_available_to_bet,
            self.definitely_unreflected_product_liability,
        )
        expected_lower = _subtract_floor_zero(
            expected_upper,
            self.unknown_reflection_product_liability,
        )
        if self.upper_headroom != expected_upper or self.lower_headroom != expected_lower:
            raise ProviderAccountHeadroomError(
                "headroom bounds do not match conservative liability lattice"
            )
        expected_decision = _classify(
            self.lower_headroom,
            self.upper_headroom,
            self.proposed_liability,
        )
        if self.decision is not expected_decision:
            raise ProviderAccountHeadroomError(
                "headroom decision does not match conservative bounds"
            )
        if (
            self.provider_atomicity_proven is not False
            or self.provider_balance_generation_cas_proven is not False
            or self.execution_authority is not False
            or self.real_money_readiness is not False
        ):
            raise ProviderAccountHeadroomError(
                "headroom assessment cannot claim provider atomicity, balance-head CAS, "
                "execution authority, or real-money readiness"
            )
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ProviderAccountHeadroomError("unsupported headroom assessment schema")
        acquired_time = _timestamp(self.acquired_at, "acquired_at")
        balance_time = _timestamp(self.balance_observed_at, "balance_observed_at")
        if balance_time > acquired_time:
            raise ProviderAccountHeadroomError(
                "provider balance observation cannot postdate product acquisition"
            )
        if _timestamp(self.expires_at, "expires_at") <= balance_time:
            raise ProviderAccountHeadroomError(
                "assessment expiry must follow provider balance observation"
            )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProductInternalHeadroomReservation:
    assessment_sha256: str
    attempt_id: str
    attempt_fingerprint: str
    reserved_at: str
    post_reservation_ledger_sha256: str
    post_reservation_event_count: int
    provider_atomicity_proven: bool = False
    execution_authority: bool = False
    real_money_readiness: bool = False

    def __post_init__(self) -> None:
        for field in (
            "assessment_sha256",
            "attempt_fingerprint",
            "post_reservation_ledger_sha256",
        ):
            _sha(getattr(self, field), field)
        _text(self.attempt_id, "attempt_id")
        _timestamp(self.reserved_at, "reserved_at")
        if (
            type(self.post_reservation_event_count) is not int
            or self.post_reservation_event_count < 0
        ):
            raise ProviderAccountHeadroomError(
                "post_reservation_event_count must be non-negative exact int"
            )
        if (
            self.provider_atomicity_proven is not False
            or self.execution_authority is not False
            or self.real_money_readiness is not False
        ):
            raise ProviderAccountHeadroomError(
                "internal reservation cannot claim provider atomicity, execution authority, "
                "or real-money readiness"
            )

    @property
    def product_internal_reservation_proven(self) -> bool:
        return _reservation_is_issued(self)


def _reservation_digest(value: ProductInternalHeadroomReservation) -> str:
    return _canonical_digest(
        {
            "schema": "autosport.product_internal_headroom_reservation",
            "assessment_sha256": value.assessment_sha256,
            "attempt_id": value.attempt_id,
            "attempt_fingerprint": value.attempt_fingerprint,
            "reserved_at": value.reserved_at,
            "post_reservation_ledger_sha256": value.post_reservation_ledger_sha256,
            "post_reservation_event_count": value.post_reservation_event_count,
            "provider_atomicity_proven": value.provider_atomicity_proven,
            "execution_authority": value.execution_authority,
            "real_money_readiness": value.real_money_readiness,
        }
    )


def _assessment_payload(value: ProviderAccountHeadroomAssessment) -> dict[str, object]:
    return {
        "schema": "autosport.provider_account_headroom_assessment",
        "schema_version": value.schema_version,
        "provider_id": value.provider_id,
        "account_id": value.account_id,
        "currency": value.currency,
        "economic_goal_contract_sha256": value.economic_goal_contract_sha256,
        "denomination_authority_sha256": value.denomination_authority_sha256,
        "acquisition_id": value.acquisition_id,
        "acquisition_snapshot_sha256": value.acquisition_snapshot_sha256,
        "acquired_at": value.acquired_at,
        "balance_observed_at": value.balance_observed_at,
        "expires_at": value.expires_at,
        "ledger_snapshot_sha256": value.ledger_snapshot_sha256,
        "ledger_event_count": value.ledger_event_count,
        "plan_id": value.plan_id,
        "action_id": value.action_id,
        "action_fingerprint": value.action_fingerprint,
        "proposed_liability": _decimal_text(value.proposed_liability),
        "provider_available_to_bet": _decimal_text(value.provider_available_to_bet),
        "definitely_unreflected_product_liability": _decimal_text(
            value.definitely_unreflected_product_liability
        ),
        "unknown_reflection_product_liability": _decimal_text(
            value.unknown_reflection_product_liability
        ),
        "lower_headroom": _decimal_text(value.lower_headroom),
        "upper_headroom": _decimal_text(value.upper_headroom),
        "decision": value.decision.value,
        "provider_atomicity_proven": value.provider_atomicity_proven,
        "provider_balance_generation_cas_proven": (
            value.provider_balance_generation_cas_proven
        ),
        "execution_authority": value.execution_authority,
        "real_money_readiness": value.real_money_readiness,
    }


def _assessment_digest(value: ProviderAccountHeadroomAssessment) -> str:
    return _canonical_digest(_assessment_payload(value))


def _require_live_balance(
    acquired: AuthoritativeAccountSnapshot,
    *,
    now: datetime,
) -> tuple[Decimal, str, datetime, datetime]:
    if type(acquired) is not AuthoritativeAccountSnapshot:
        raise ProviderAccountHeadroomError(
            "account evidence must be exact AuthoritativeAccountSnapshot"
        )
    try:
        assert_live = _canonical_account_snapshot_authority()
        assert_live(acquired)
    except AccountSnapshotAcquisitionError as exc:
        raise ProviderAccountHeadroomError(
            "account snapshot lacks live canonical provider-origin authority"
        ) from exc
    receipt = acquired.receipt
    snapshot = acquired.snapshot
    if BookmakerCapability.BALANCE_READ.value not in receipt.requested_capabilities:
        raise ProviderAccountHeadroomUnsupported(
            "account acquisition does not bind BALANCE_READ"
        )
    balance = snapshot.balance
    if balance is None:
        raise ProviderAccountHeadroomUnsupported(
            "live account acquisition lacks provider available-balance evidence"
        )
    if (
        receipt.venue_id != snapshot.profile.venue_id
        or receipt.account_id != snapshot.profile.account_id
        or balance.venue_id != receipt.venue_id
        or balance.account_id != receipt.account_id
    ):
        raise ProviderAccountHeadroomError(
            "account acquisition provider/account scope is inconsistent"
        )
    if balance.currency != balance.currency.upper() or len(balance.currency) != 3:
        raise ProviderAccountHeadroomUnsupported(
            "provider account currency is not canonical three-letter code"
        )
    acquired_at = _timestamp(receipt.acquired_at, "acquired_at")
    balance_observed_at = _timestamp(balance.observed_at, "balance observed_at")
    if acquired_at > now:
        raise ProviderAccountHeadroomStale("account acquisition is from the future")
    if balance_observed_at > acquired_at:
        raise ProviderAccountHeadroomError(
            "provider balance observation postdates product acquisition receipt"
        )
    if balance_observed_at > now:
        raise ProviderAccountHeadroomStale(
            "provider balance observation is from the future"
        )
    if now - balance_observed_at > _PRODUCT_MAX_ACCOUNT_SNAPSHOT_AGE:
        raise ProviderAccountHeadroomStale(
            "provider balance observation exceeds product headroom freshness ceiling"
        )
    return (
        _decimal(balance.available_balance, "available_to_bet"),
        balance.currency,
        acquired_at,
        balance_observed_at,
    )





def _validated_bound_plan_map(
    bound_plans: tuple[BoundSupervisedExecutionPlan, ...],
) -> dict[str, BoundSupervisedExecutionPlan]:
    _, _, _, bound_type, verify_binding, assert_bound_authoritative, _ = (
        _canonical_denomination_dispatch()
    )
    if type(bound_plans) is not tuple or not bound_plans:
        raise ProviderAccountHeadroomUnsupported(
            "exact bound supervised execution plans are required for monetary denomination"
        )
    result: dict[str, BoundSupervisedExecutionPlan] = {}
    for bound in bound_plans:
        if type(bound) is not bound_type:
            raise ProviderAccountHeadroomUnsupported(
                "denomination coverage must contain exact BoundSupervisedExecutionPlan values"
            )
        try:
            verify_binding(bound)
            assert_bound_authoritative(bound)
        except SupervisedExecutionError as exc:
            raise ProviderAccountHeadroomUnsupported(
                "bound supervised execution plan lacks canonical product issuance authority"
            ) from exc
        plan_id = bound.execution_plan.plan_id
        if plan_id in result:
            raise ProviderAccountHeadroomError(
                "denomination coverage duplicated execution plan identity"
            )
        result[plan_id] = bound
    return result



def _validated_intent_denomination_map(
    intents: tuple[OpportunityIntent, ...],
) -> dict[
    tuple[str, str],
    tuple[str, str, tuple[tuple[str, str], ...]],
]:
    (
        intent_type,
        intent_id_descriptor,
        intent_context_descriptor,
        intent_sha_descriptor,
        context_type,
        provider_accounts_descriptor,
        bankroll_descriptor,
        currency_descriptor,
    ) = _canonical_intent_denomination_dispatch()
    if type(intents) is not tuple or not intents:
        raise ProviderAccountHeadroomUnsupported(
            "exact OpportunityIntent denomination evidence is required"
        )
    result: dict[
        tuple[str, str],
        tuple[str, str, tuple[tuple[str, str], ...]],
    ] = {}
    for intent in intents:
        if type(intent) is not intent_type:
            raise ProviderAccountHeadroomUnsupported(
                "denomination evidence must contain exact OpportunityIntent values"
            )
        try:
            intent_id = _text(
                intent_id_descriptor.__get__(intent, intent_type),
                "opportunity intent id",
            )
            intent_sha256 = _sha(
                intent_sha_descriptor.__get__(intent, intent_type),
                "opportunity intent sha256",
            )
            context = intent_context_descriptor.__get__(intent, intent_type)
            if type(context) is not context_type:
                raise TypeError("risk context is not canonical")
            provider_accounts = provider_accounts_descriptor.__get__(
                context,
                context_type,
            )
            if type(provider_accounts) is not tuple:
                raise TypeError("provider_accounts is not canonical tuple")
            canonical_provider_accounts: list[tuple[str, str]] = []
            for binding in provider_accounts:
                if type(binding) is not tuple or len(binding) != 2:
                    raise TypeError("provider account binding is not canonical")
                canonical_provider_accounts.append(
                    (
                        _text(binding[0], "opportunity intent provider source_id"),
                        _text(binding[1], "opportunity intent provider account_id"),
                    )
                )
            if tuple(canonical_provider_accounts) != provider_accounts:
                raise TypeError("provider account bindings are not canonical")
            bankroll_id = _text(
                bankroll_descriptor.__get__(context, context_type),
                "opportunity intent bankroll_id",
            )
            currency = _text(
                currency_descriptor.__get__(context, context_type),
                "opportunity intent currency",
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderAccountHeadroomUnsupported(
                "opportunity intent denomination evidence is invalid"
            ) from exc
        key = (intent_id, intent_sha256)
        if key in result:
            raise ProviderAccountHeadroomError(
                "denomination evidence duplicated opportunity intent identity"
            )
        result[key] = (bankroll_id, currency, provider_accounts)
    return result


def _current_economic_goal_denomination(
    ledger: RealExecutionLedger,
    *,
    provider_currency: str,
) -> tuple[str, str]:
    store_type, store_load, derive_provenance, _, _, _ = (
        _canonical_denomination_dispatch()
    )
    workspace = _canonical_ledger_workspace(ledger)
    store = store_type(workspace)
    expected_store_path = workspace / _ECONOMIC_GOAL_STORE_FILE_NAME
    store_workspace = getattr(store, "workspace", None)
    store_path = getattr(store, "path", None)
    if (
        type(store) is not store_type
        or type(store_workspace) is not type(workspace)
        or store_workspace != workspace
        or type(store_path) is not type(expected_store_path)
        or store_path != expected_store_path
    ):
        raise ProviderAccountHeadroomError(
            "canonical economic-goal store path authority changed"
        )
    try:
        goal = store_load(store)
        goal_sha256 = derive_provenance(goal).contract_sha256
    except EconomicGoalContractError as exc:
        raise ProviderAccountHeadroomUnsupported(
            "durable economic-goal denomination authority is unavailable"
        ) from exc
    if goal.currency != provider_currency:
        raise ProviderAccountHeadroomUnsupported(
            "provider balance currency mismatches durable economic-goal currency"
        )
    return goal_sha256, _text(goal.bankroll_id, "economic goal bankroll_id")


def _require_plan_denomination(
    view: VerifiedExecutionPlanView,
    *,
    bound_by_plan_id: dict[str, BoundSupervisedExecutionPlan],
    intent_denomination_by_identity: dict[
        tuple[str, str],
        tuple[str, str, tuple[tuple[str, str], ...]],
    ],
    economic_goal_contract_sha256: str,
    economic_goal_bankroll_id: str,
    provider_currency: str,
    provider_id: str,
    account_id: str,
) -> tuple[str, str, str, str, str, str, str, str]:
    bound = bound_by_plan_id.get(view.plan.plan_id)
    if bound is None:
        raise ProviderAccountHeadroomUnsupported(
            "relevant execution plan lacks exact supervised denomination binding"
        )
    _, _, _, bound_type, verify_binding, assert_bound_authoritative, _ = (
        _canonical_denomination_dispatch()
    )
    if type(bound) is not bound_type:
        raise ProviderAccountHeadroomUnsupported(
            "relevant denomination binding is not canonical"
        )
    try:
        verify_binding(bound)
        assert_bound_authoritative(bound)
    except SupervisedExecutionError as exc:
        raise ProviderAccountHeadroomUnsupported(
            "relevant supervised plan binding lacks current product issuance authority"
        ) from exc
    if (
        bound.economic_goal_contract_sha256 != economic_goal_contract_sha256
        or bound.execution_plan != view.plan
        or bound.execution_plan.fingerprint != view.plan_fingerprint
    ):
        raise ProviderAccountHeadroomUnsupported(
            "ledger execution plan does not match current durable denomination authority"
        )
    intent_identity = (bound.intent_id, bound.intent_sha256)
    intent_denomination = intent_denomination_by_identity.get(intent_identity)
    if intent_denomination is None:
        raise ProviderAccountHeadroomUnsupported(
            "relevant supervised plan lacks exact OpportunityIntent denomination evidence"
        )
    intent_bankroll_id, intent_currency, intent_provider_accounts = (
        intent_denomination
    )
    if (
        intent_bankroll_id != economic_goal_bankroll_id
        or intent_currency != provider_currency
    ):
        raise ProviderAccountHeadroomUnsupported(
            "intent denomination does not match current durable economic goal"
        )
    if (provider_id, account_id) not in intent_provider_accounts:
        raise ProviderAccountHeadroomUnsupported(
            "intent provider-account scope does not cover execution account"
        )
    return (
        view.plan.plan_id,
        view.plan_fingerprint,
        bound.intent_id,
        bound.intent_sha256,
        intent_bankroll_id,
        intent_currency,
        provider_id,
        account_id,
    )


def _denomination_authority_sha256(
    *,
    currency: str,
    economic_goal_contract_sha256: str,
    economic_goal_bankroll_id: str,
    plan_bindings: tuple[
        tuple[str, str, str, str, str, str, str, str],
        ...,
    ],
) -> str:
    ordered = tuple(sorted(set(plan_bindings)))
    if not ordered:
        raise ProviderAccountHeadroomUnsupported(
            "denomination authority must cover at least the target execution plan"
        )
    return _canonical_digest(
        {
            "schema": "autosport.provider_account_headroom_denomination_authority",
            "schema_version": 3,
            "currency": currency,
            "economic_goal_contract_sha256": economic_goal_contract_sha256,
            "economic_goal_bankroll_id": economic_goal_bankroll_id,
            "plans": [
                {
                    "plan_id": plan_id,
                    "plan_fingerprint": plan_fingerprint,
                    "intent_id": intent_id,
                    "intent_sha256": intent_sha256,
                    "intent_bankroll_id": intent_bankroll_id,
                    "intent_currency": intent_currency,
                    "provider_id": provider_id,
                    "account_id": account_id,
                }
                for (
                    plan_id,
                    plan_fingerprint,
                    intent_id,
                    intent_sha256,
                    intent_bankroll_id,
                    intent_currency,
                    provider_id,
                    account_id,
                ) in ordered
            ],
        }
    )


def _resolve_account_liability_lattice(
    ledger: RealExecutionLedger,
    *,
    provider_id: str,
    account_id: str,
    expected_snapshot_sha256: str,
    expected_event_count: int,
    bound_by_plan_id: dict[str, BoundSupervisedExecutionPlan],
    intent_denomination_by_identity: dict[
        tuple[str, str],
        tuple[str, str, tuple[tuple[str, str], ...]],
    ],
    economic_goal_contract_sha256: str,
    economic_goal_bankroll_id: str,
    provider_currency: str,
) -> tuple[
    Decimal,
    Decimal,
    tuple[tuple[str, str, str, str, str, str, str, str], ...],
]:
    """Return (definitely-unreflected, unknown-reflection) liability.

    RESERVED is definitely product-side only: it has not crossed the provider
    boundary and therefore must be subtracted from available-to-bet. All
    externally plausible non-RESERVED liability remains UNKNOWN with respect to
    inclusion in the exact provider balance observation until a separate
    canonical causal-coverage authority proves otherwise.
    """
    verified_snapshot, verified_execution_view, _, _ = _canonical_ledger_dispatch()
    resolve_capital, assert_capital_current = _canonical_capital_risk_dispatch()
    start = verified_snapshot(ledger)
    if (
        start.sha256 != expected_snapshot_sha256
        or start.event_count != expected_event_count
    ):
        raise ProviderAccountHeadroomStale(
            "execution ledger changed before account-liability resolution"
        )
    definitely_unreflected = _ZERO
    unknown_reflection = _ZERO
    denomination_bindings: list[
        tuple[str, str, str, str, str, str, str, str]
    ] = []

    for plan_id in _ledger_plan_ids(start.payload):
        try:
            view = verified_execution_view(ledger, plan_id)
            relevant_attempts = tuple(
                attempt
                for attempt in view.attempts
                if (attempt.action.bookmaker_id, attempt.action.account_id)
                == (provider_id, account_id)
            )
            if not relevant_attempts:
                continue
            denomination_bindings.append(
                _require_plan_denomination(
                    view,
                    bound_by_plan_id=bound_by_plan_id,
                    intent_denomination_by_identity=intent_denomination_by_identity,
                    economic_goal_contract_sha256=economic_goal_contract_sha256,
                    economic_goal_bankroll_id=economic_goal_bankroll_id,
                    provider_currency=provider_currency,
                    provider_id=provider_id,
                    account_id=account_id,
                )
            )
            capital: ExecutionCapitalAtRiskEvidence = resolve_capital(
                ledger,
                plan_id,
            )
            assert_capital_current(capital, ledger)
        except ExecutionCapitalAtRiskError as exc:
            raise ProviderAccountHeadroomUnsupported(
                "canonical capital-at-risk evidence cannot cover the full execution ledger"
            ) from exc
        if (
            view.snapshot_sha256 != expected_snapshot_sha256
            or view.event_count != expected_event_count
            or capital.snapshot_sha256 != expected_snapshot_sha256
            or capital.event_count != expected_event_count
        ):
            raise ProviderAccountHeadroomStale(
                "execution ledger changed during account-liability resolution"
            )
        risk_by_attempt = {item.attempt_id: item for item in capital.attempts}
        if len(risk_by_attempt) != len(capital.attempts):
            raise ProviderAccountHeadroomError(
                "canonical capital-at-risk evidence duplicated attempt identity"
            )
        for attempt in relevant_attempts:
            action = attempt.action
            risk = risk_by_attempt.get(attempt.attempt.attempt_id)
            if risk is None:
                raise ProviderAccountHeadroomUnsupported(
                    "account attempt is absent from canonical capital-at-risk evidence"
                )
            if attempt.state is AttemptState.RESERVED:
                definitely_unreflected = _add(
                    definitely_unreflected,
                    risk.requested_capital_at_limit,
                )
            else:
                unknown_reflection = _add(
                    unknown_reflection,
                    risk.max_plausible_capital_at_risk,
                )

    finish = verified_snapshot(ledger)
    if (
        finish.sha256 != expected_snapshot_sha256
        or finish.event_count != expected_event_count
    ):
        raise ProviderAccountHeadroomStale(
            "execution ledger changed during complete account-liability scan"
        )
    return (
        definitely_unreflected,
        unknown_reflection,
        tuple(denomination_bindings),
    )


def assess_provider_account_headroom(
    ledger: RealExecutionLedger,
    acquired: AuthoritativeAccountSnapshot,
    *,
    plan_id: str,
    action_id: str,
    bound_plans: tuple[BoundSupervisedExecutionPlan, ...],
    intents: tuple[OpportunityIntent, ...],
    _digest_authority=_assert_headroom_digest_authority,
    _digest_authority_code=getattr(_assert_headroom_digest_authority, "__code__", None),
) -> ProviderAccountHeadroomAssessment:
    """Issue conservative capital-axis evidence from exact canonical truth."""
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    if getattr(_digest_authority, "__code__", None) is not _digest_authority_code:
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom digest guard changed"
        )
    _digest_authority()
    plan_id = _text(plan_id, "plan_id")
    action_id = _text(action_id, "action_id")
    verified_snapshot, verified_execution_view, _, _ = _canonical_ledger_dispatch()
    now = _read_headroom_utc_now()
    available, currency, acquired_at, balance_observed_at = _require_live_balance(
        acquired,
        now=now,
    )
    bound_by_plan_id = _validated_bound_plan_map(bound_plans)
    intent_denomination_by_identity = _validated_intent_denomination_map(intents)
    workspace = _canonical_ledger_workspace(ledger)

    with _canonical_economic_lock(workspace):
        (
            economic_goal_contract_sha256,
            economic_goal_bankroll_id,
        ) = _current_economic_goal_denomination(
            ledger,
            provider_currency=currency,
        )
        snapshot = verified_snapshot(ledger)
        try:
            target_view = verified_execution_view(ledger, plan_id)
        except KeyError as exc:
            raise ProviderAccountHeadroomUnsupported(
                "target execution plan is not durably reserved"
            ) from exc
        if (
            target_view.snapshot_sha256 != snapshot.sha256
            or target_view.event_count != snapshot.event_count
        ):
            raise ProviderAccountHeadroomStale(
                "execution ledger changed while resolving target action"
            )
        action = _find_action(target_view, action_id)
        target_denomination_binding = _require_plan_denomination(
            target_view,
            bound_by_plan_id=bound_by_plan_id,
            intent_denomination_by_identity=intent_denomination_by_identity,
            economic_goal_contract_sha256=economic_goal_contract_sha256,
            economic_goal_bankroll_id=economic_goal_bankroll_id,
            provider_currency=currency,
            provider_id=action.bookmaker_id,
            account_id=action.account_id,
        )
        if action.bookmaker_id != "betfair" or action.side != "BACK":
            raise ProviderAccountHeadroomUnsupported(
                "current provider-account headroom admission supports Betfair BACK only"
            )
        if (action.bookmaker_id, action.account_id) != (
            acquired.receipt.venue_id,
            acquired.receipt.account_id,
        ):
            raise ProviderAccountHeadroomUnsupported(
                "target action provider/account mismatches live balance acquisition"
            )
        proposed = _decimal(
            action.requested_stake,
            "proposed Betfair BACK liability",
            positive=True,
        )

        (
            definitely_unreflected,
            unknown_reflection,
            liability_denomination_bindings,
        ) = _resolve_account_liability_lattice(
            ledger,
            provider_id=action.bookmaker_id,
            account_id=action.account_id,
            expected_snapshot_sha256=snapshot.sha256,
            expected_event_count=snapshot.event_count,
            bound_by_plan_id=bound_by_plan_id,
            intent_denomination_by_identity=intent_denomination_by_identity,
            economic_goal_contract_sha256=economic_goal_contract_sha256,
            economic_goal_bankroll_id=economic_goal_bankroll_id,
            provider_currency=currency,
        )
        denomination_authority_sha256 = _denomination_authority_sha256(
            currency=currency,
            economic_goal_contract_sha256=economic_goal_contract_sha256,
            economic_goal_bankroll_id=economic_goal_bankroll_id,
            plan_bindings=(
                target_denomination_binding,
                *liability_denomination_bindings,
            ),
        )
        upper = _subtract_floor_zero(available, definitely_unreflected)
        lower = _subtract_floor_zero(upper, unknown_reflection)
        decision = _classify(lower, upper, proposed)

        action_fingerprint = _canonical_digest(
            {
                "plan_fingerprint": target_view.plan_fingerprint,
                "action": action.to_dict(),
                "currency": currency,
                "economic_goal_contract_sha256": economic_goal_contract_sha256,
                "denomination_authority_sha256": denomination_authority_sha256,
            }
        )
        action_expiry = _timestamp(action.expires_at, "action expires_at")
        freshness_expiry = balance_observed_at + _PRODUCT_MAX_ACCOUNT_SNAPSHOT_AGE
        expiry = min(action_expiry, freshness_expiry)
        if now >= expiry:
            raise ProviderAccountHeadroomStale(
                "target quote or provider-account observation already expired"
            )

        provisional = ProviderAccountHeadroomAssessment(
            provider_id=action.bookmaker_id,
            account_id=action.account_id,
            currency=currency,
            economic_goal_contract_sha256=economic_goal_contract_sha256,
            denomination_authority_sha256=denomination_authority_sha256,
            acquisition_id=acquired.receipt.acquisition_id,
            acquisition_snapshot_sha256=acquired.receipt.snapshot_sha256,
            acquired_at=acquired.receipt.acquired_at,
            balance_observed_at=acquired.snapshot.balance.observed_at,
            expires_at=expiry.isoformat(),
            ledger_snapshot_sha256=snapshot.sha256,
            ledger_event_count=snapshot.event_count,
            plan_id=plan_id,
            action_id=action_id,
            action_fingerprint=action_fingerprint,
            proposed_liability=proposed,
            provider_available_to_bet=available,
            definitely_unreflected_product_liability=definitely_unreflected,
            unknown_reflection_product_liability=unknown_reflection,
            lower_headroom=lower,
            upper_headroom=upper,
            decision=decision,
            evidence_sha256="0" * 64,
        )
        assessment = ProviderAccountHeadroomAssessment(
            **{
                field: getattr(provisional, field)
                for field in provisional.__dataclass_fields__
                if field != "evidence_sha256"
            },
            evidence_sha256=_assessment_digest(provisional),
        )
        if (
            _current_economic_goal_denomination(
                ledger,
                provider_currency=currency,
            )
            != (economic_goal_contract_sha256, economic_goal_bankroll_id)
        ):
            raise ProviderAccountHeadroomStale(
                "economic-goal denomination authority changed during assessment"
            )
        final_snapshot = verified_snapshot(ledger)
        if (
            final_snapshot.sha256 != snapshot.sha256
            or final_snapshot.event_count != snapshot.event_count
        ):
            raise ProviderAccountHeadroomStale(
                "execution ledger changed before headroom assessment issuance"
            )
        return assessment


def reserve_observed_provider_headroom(
    ledger: RealExecutionLedger,
    acquired: AuthoritativeAccountSnapshot,
    assessment: ProviderAccountHeadroomAssessment,
    *,
    attempt_id: str,
    bound_plans: tuple[BoundSupervisedExecutionPlan, ...],
    intents: tuple[OpportunityIntent, ...] = (),
    _issued_assertion=None,
    _digest_authority=_assert_headroom_digest_authority,
    _digest_authority_code=getattr(_assert_headroom_digest_authority, "__code__", None),
) -> ProductInternalHeadroomReservation:
    """Atomically consume product-internal headroom against exact ledger bytes.

    This does not place an order. The provider account can still change
    out-of-band after the observation; that uncertainty remains explicit.
    """
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    if getattr(_digest_authority, "__code__", None) is not _digest_authority_code:
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom digest guard changed"
        )
    _digest_authority()
    (
        verified_snapshot,
        verified_execution_view,
        begin_attempt,
        attempt_state,
    ) = _canonical_ledger_dispatch()
    if _issued_assertion is None:
        raise ProviderAccountHeadroomError(
            "canonical headroom assessment issuance assertion is unavailable"
        )
    _issued_assertion(assessment)
    attempt_id = _text(attempt_id, "attempt_id")
    if type(acquired) is not AuthoritativeAccountSnapshot:
        raise ProviderAccountHeadroomError(
            "account evidence must be exact AuthoritativeAccountSnapshot"
        )
    if (
        acquired.receipt.acquisition_id != assessment.acquisition_id
        or acquired.receipt.snapshot_sha256 != assessment.acquisition_snapshot_sha256
    ):
        raise ProviderAccountHeadroomStale(
            "account acquisition changed after headroom assessment"
        )

    try:
        attempt_state(ledger, attempt_id)
    except KeyError:
        prior_exists = False
    else:
        prior_exists = True

    if not prior_exists:
        if assessment.decision is not HeadroomDecision.SUFFICIENT_LOWER_BOUND:
            raise ProviderAccountHeadroomUnsupported(
                "new internal reservation requires proven sufficient lower-bound headroom"
            )
        now = _read_headroom_utc_now()
        _, current_currency, _, _ = _require_live_balance(acquired, now=now)
        if current_currency != assessment.currency:
            raise ProviderAccountHeadroomStale(
                "provider account denomination changed after headroom assessment"
            )
        if now >= _timestamp(assessment.expires_at, "assessment expires_at"):
            raise ProviderAccountHeadroomStale(
                "headroom assessment expired before reservation"
            )
        bound_by_plan_id = _validated_bound_plan_map(bound_plans)
        intent_denomination_by_identity = _validated_intent_denomination_map(intents)
        workspace = _canonical_ledger_workspace(ledger)
        with _canonical_economic_lock(workspace):
            (
                current_goal_sha256,
                current_goal_bankroll_id,
            ) = _current_economic_goal_denomination(
                ledger,
                provider_currency=current_currency,
            )
            if current_goal_sha256 != assessment.economic_goal_contract_sha256:
                raise ProviderAccountHeadroomStale(
                    "economic-goal denomination authority changed after assessment"
                )
            current_snapshot = verified_snapshot(ledger)
            if (
                current_snapshot.sha256 != assessment.ledger_snapshot_sha256
                or current_snapshot.event_count != assessment.ledger_event_count
            ):
                raise ProviderAccountHeadroomStale(
                    "execution ledger changed; recompute provider-account headroom"
                )
            try:
                target_view = verified_execution_view(ledger, assessment.plan_id)
            except KeyError as exc:
                raise ProviderAccountHeadroomStale(
                    "target execution plan disappeared after headroom assessment"
                ) from exc
            target_binding = _require_plan_denomination(
                target_view,
                bound_by_plan_id=bound_by_plan_id,
                intent_denomination_by_identity=intent_denomination_by_identity,
                economic_goal_contract_sha256=current_goal_sha256,
                economic_goal_bankroll_id=current_goal_bankroll_id,
                provider_currency=current_currency,
                provider_id=assessment.provider_id,
                account_id=assessment.account_id,
            )
            (
                definitely_unreflected,
                unknown_reflection,
                liability_bindings,
            ) = _resolve_account_liability_lattice(
                ledger,
                provider_id=assessment.provider_id,
                account_id=assessment.account_id,
                expected_snapshot_sha256=current_snapshot.sha256,
                expected_event_count=current_snapshot.event_count,
                bound_by_plan_id=bound_by_plan_id,
                intent_denomination_by_identity=intent_denomination_by_identity,
                economic_goal_contract_sha256=current_goal_sha256,
                economic_goal_bankroll_id=current_goal_bankroll_id,
                provider_currency=current_currency,
            )
            if (
                definitely_unreflected
                != assessment.definitely_unreflected_product_liability
                or unknown_reflection
                != assessment.unknown_reflection_product_liability
                or _denomination_authority_sha256(
                    currency=current_currency,
                    economic_goal_contract_sha256=current_goal_sha256,
                    economic_goal_bankroll_id=current_goal_bankroll_id,
                    plan_bindings=(target_binding, *liability_bindings),
                )
                != assessment.denomination_authority_sha256
            ):
                raise ProviderAccountHeadroomStale(
                    "denomination or liability authority changed after assessment"
                )
            if (
                _current_economic_goal_denomination(
                    ledger,
                    provider_currency=current_currency,
                )
                != (current_goal_sha256, current_goal_bankroll_id)
            ):
                raise ProviderAccountHeadroomStale(
                    "economic-goal denomination authority changed before reservation commit"
                )
            try:
                # The ledger CAS proves the exact liability snapshot is still current.
                # WorkspaceEconomicLock keeps the durable economic-goal revision stable
                # across the final denomination reread and internal reservation commit.
                attempt: ExecutionAttempt = begin_attempt(
                    ledger,
                    plan_id=assessment.plan_id,
                    action_id=assessment.action_id,
                    attempt_id=attempt_id,
                    expected_snapshot_sha256=assessment.ledger_snapshot_sha256,
                )
            except ExecutionStateError as exc:
                raise ProviderAccountHeadroomStale(
                    "execution ledger changed; recompute provider-account headroom"
                ) from exc
    else:
        try:
            # Exact replay resolves an existing attempt before the stale-snapshot fence;
            # it consumes no new provider headroom and therefore does not require current
            # owner-denomination authority to recreate an already-durable reservation.
            attempt = begin_attempt(
                ledger,
                plan_id=assessment.plan_id,
                action_id=assessment.action_id,
                attempt_id=attempt_id,
                expected_snapshot_sha256=assessment.ledger_snapshot_sha256,
            )
        except ExecutionStateError as exc:
            raise ProviderAccountHeadroomStale(
                "execution ledger changed; recompute provider-account headroom"
            ) from exc

    post = verified_snapshot(ledger)
    reservation = ProductInternalHeadroomReservation(
        assessment_sha256=assessment.evidence_sha256,
        attempt_id=attempt.attempt_id,
        attempt_fingerprint=attempt.effect_fingerprint,
        reserved_at=attempt.reserved_at,
        post_reservation_ledger_sha256=post.sha256,
        post_reservation_event_count=post.event_count,
    )
    return reservation

def _install_headroom_issuance_authority() -> None:
    assessment_lock = threading.RLock()
    assessment_fields = tuple(ProviderAccountHeadroomAssessment.__dataclass_fields__)
    reservation_fields = tuple(ProductInternalHeadroomReservation.__dataclass_fields__)
    assessment_issued: dict[
        int,
        tuple[weakref.ReferenceType[ProviderAccountHeadroomAssessment], tuple[object, ...]],
    ] = {}
    reservation_lock = threading.RLock()
    reservation_issued: dict[
        int,
        tuple[
            weakref.ReferenceType[ProductInternalHeadroomReservation],
            tuple[object, ...],
        ],
    ] = {}

    raw_assess = assess_provider_account_headroom
    raw_reserve = reserve_observed_provider_headroom
    digest_authority = _assert_headroom_digest_authority
    digest_authority_code = getattr(digest_authority, "__code__", None)

    def require_digest_authority() -> None:
        if getattr(digest_authority, "__code__", None) is not digest_authority_code:
            raise ProviderAccountHeadroomError(
                "canonical provider-account headroom digest guard changed"
            )
        digest_authority()

    def assessment_state(
        value: ProviderAccountHeadroomAssessment,
    ) -> tuple[object, ...]:
        return tuple(getattr(value, field) for field in assessment_fields)

    def reservation_state(
        value: ProductInternalHeadroomReservation,
    ) -> tuple[object, ...]:
        return tuple(getattr(value, field) for field in reservation_fields)

    def issue_assessment(value: ProviderAccountHeadroomAssessment) -> None:
        require_digest_authority()
        digest = _assessment_digest(value)
        if digest != value.evidence_sha256:
            raise ProviderAccountHeadroomError(
                "headroom assessment identity changed before canonical issuance"
            )
        identity = id(value)

        def clear(
            reference: weakref.ReferenceType[ProviderAccountHeadroomAssessment],
            *,
            _identity: int = identity,
        ) -> None:
            with assessment_lock:
                current = assessment_issued.get(_identity)
                if current is not None and current[0] is reference:
                    assessment_issued.pop(_identity, None)

        reference = weakref.ref(value, clear)
        with assessment_lock:
            assessment_issued[identity] = (reference, assessment_state(value))

    def assert_issued(value: ProviderAccountHeadroomAssessment) -> None:
        if type(value) is not ProviderAccountHeadroomAssessment:
            raise ProviderAccountHeadroomError(
                "assessment must be exact ProviderAccountHeadroomAssessment"
            )
        require_digest_authority()
        try:
            digest = _assessment_digest(value)
        except (ProviderAccountHeadroomError, TypeError, ValueError) as exc:
            raise ProviderAccountHeadroomError(
                "headroom assessment identity is invalid"
            ) from exc
        with assessment_lock:
            current = assessment_issued.get(id(value))
        if (
            current is None
            or current[0]() is not value
            or current[1] != assessment_state(value)
            or value.evidence_sha256 != digest
        ):
            raise ProviderAccountHeadroomError(
                "headroom assessment was not canonically issued"
            )

    def issue_reservation(value: ProductInternalHeadroomReservation) -> None:
        require_digest_authority()
        _reservation_digest(value)
        identity = id(value)

        def clear(
            reference: weakref.ReferenceType[ProductInternalHeadroomReservation],
            *,
            _identity: int = identity,
        ) -> None:
            with reservation_lock:
                current = reservation_issued.get(_identity)
                if current is not None and current[0] is reference:
                    reservation_issued.pop(_identity, None)

        reference = weakref.ref(value, clear)
        with reservation_lock:
            reservation_issued[identity] = (reference, reservation_state(value))

    def reservation_is_issued(value: ProductInternalHeadroomReservation) -> bool:
        if type(value) is not ProductInternalHeadroomReservation:
            return False
        try:
            require_digest_authority()
            _reservation_digest(value)
        except (ProviderAccountHeadroomError, TypeError, ValueError):
            return False
        with reservation_lock:
            current = reservation_issued.get(id(value))
        return (
            current is not None
            and current[0]() is value
            and current[1] == reservation_state(value)
        )

    def authoritative_assess(
        ledger: RealExecutionLedger,
        acquired: AuthoritativeAccountSnapshot,
        *,
        plan_id: str,
        action_id: str,
        bound_plans: tuple[BoundSupervisedExecutionPlan, ...],
        intents: tuple[OpportunityIntent, ...],
    ) -> ProviderAccountHeadroomAssessment:
        value = raw_assess(
            ledger,
            acquired,
            plan_id=plan_id,
            action_id=action_id,
            bound_plans=bound_plans,
            intents=intents,
        )
        issue_assessment(value)
        return value

    def authoritative_reserve(
        ledger: RealExecutionLedger,
        acquired: AuthoritativeAccountSnapshot,
        assessment: ProviderAccountHeadroomAssessment,
        *,
        attempt_id: str,
        bound_plans: tuple[BoundSupervisedExecutionPlan, ...],
        intents: tuple[OpportunityIntent, ...] = (),
    ) -> ProductInternalHeadroomReservation:
        value = raw_reserve(
            ledger,
            acquired,
            assessment,
            attempt_id=attempt_id,
            bound_plans=bound_plans,
            intents=intents,
            _issued_assertion=assert_issued,
        )
        issue_reservation(value)
        return value

    setattr(
        ProductInternalHeadroomReservation,
        "product_internal_reservation_proven",
        property(reservation_is_issued),
    )
    globals()["assess_provider_account_headroom"] = authoritative_assess
    globals()["reserve_observed_provider_headroom"] = authoritative_reserve


_install_headroom_issuance_authority()
del _install_headroom_issuance_authority

