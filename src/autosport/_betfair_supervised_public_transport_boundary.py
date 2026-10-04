"""Seal the public Betfair placeOrders surface to product-owned execution truth.

The irreversible provider primitive is intentionally non-public.  The canonical
high-level executor is the only product entrypoint allowed to obtain it.  The
existing trusted-runtime and STOP admission authorities remain the serialization
roots.  Final send additionally requires one durable supervised-confirmation
receipt bound to the exact plan/action/attempt/intent and exact request bytes.

Ordering at the irreversible boundary is deliberate:
1. the existing STOP callback durably writes SUBMITTED + exact request SHA;
2. this boundary re-reads that exact fact from the verified execution ledger;
3. the exact confirmation receipt is consumed at the durable submitted_at instant;
4. only then may the canonical provider POST execute.

Therefore every crash after step 1 is already on the no-blind-retry side of the
execution ledger, including a crash after receipt consumption but before network I/O.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import sys
from pathlib import Path

from . import betfair_execution_confirmation as _confirmation
from . import betfair_supervised_execution as _impl
from . import real_execution_ledger as _ledger_runtime
from . import trusted_runtime_code_profile as _runtime_profile
from . import workspace_lock as _workspace_lock_runtime


_CLIENT_TYPE = _impl.BetfairSupervisedPlaceOrdersClient
_CANONICAL_TRANSPORT_TYPE = _impl.UrllibBetfairHttpTransport
_RAW_PLACE_ACTION = _CLIENT_TYPE.__dict__.get("place_action")
_RAW_PLACE_ACTION_CODE = getattr(_RAW_PLACE_ACTION, "__code__", None)
_CANONICAL_EXECUTE = _impl.execute_betfair_supervised_action
_CANONICAL_EXECUTE_CODE = getattr(_CANONICAL_EXECUTE, "__code__", None)
_PROVIDER_HTTP_POST = _impl._CANONICAL_PROVIDER_HTTP_POST
_PROVIDER_HTTP_POST_CODE = _impl._CANONICAL_PROVIDER_HTTP_POST_CODE
_RESPONSE_PARSER = _impl._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
_RESPONSE_PARSER_CODE = _impl._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE_CODE
_OBSERVATION_CLOCK = _impl._CANONICAL_PROVIDER_OBSERVATION_CLOCK
_OBSERVATION_CLOCK_CODE = _impl._CANONICAL_PROVIDER_OBSERVATION_CLOCK_CODE
_CONFIRMATION_ERROR = _confirmation.BetfairExecutionConfirmationError
_CONSUME_CONFIRMATION = _confirmation.consume_betfair_execution_confirmation
_CONSUME_CONFIRMATION_CODE = getattr(_CONSUME_CONFIRMATION, "__code__", None)
_VERIFIED_EXECUTION_VIEW = _impl.RealExecutionLedger.verified_execution_view
_VERIFIED_EXECUTION_VIEW_CODE = getattr(_VERIFIED_EXECUTION_VIEW, "__code__", None)
_LEDGER_MUTATE = _impl.RealExecutionLedger._mutate
_LEDGER_MUTATE_CODE = getattr(_LEDGER_MUTATE, "__code__", None)
_FINAL_REQUIRE_APPROVAL = _impl._CANONICAL_REQUIRE_SUPERVISED_APPROVAL
_FINAL_REQUIRE_APPROVAL_CODE = getattr(_FINAL_REQUIRE_APPROVAL, "__code__", None)
_FINAL_REQUIRE_DURABLE_APPROVAL = (
    _impl._CANONICAL_REQUIRE_DURABLE_SUPERVISED_APPROVAL
)
_FINAL_REQUIRE_DURABLE_APPROVAL_CODE = getattr(
    _FINAL_REQUIRE_DURABLE_APPROVAL,
    "__code__",
    None,
)
_LEDGER_MARK_SUBMITTED = _impl._CANONICAL_LEDGER_MARK_SUBMITTED
_LEDGER_MARK_SUBMITTED_CODE = getattr(_LEDGER_MARK_SUBMITTED, "__code__", None)
_LEDGER_APPROVAL_ACTIVE = _impl.RealExecutionLedger.supervised_approval_is_active
_LEDGER_APPROVAL_ACTIVE_CODE = getattr(_LEDGER_APPROVAL_ACTIVE, "__code__", None)
_WORKSPACE_LOCK_TYPE = _workspace_lock_runtime.WorkspaceEconomicLock
_WORKSPACE_LOCK_ERROR = _workspace_lock_runtime.WorkspaceEconomicLockError
_WORKSPACE_LOCK_BUSY_ERROR = _workspace_lock_runtime.WorkspaceEconomicLockBusyError
_WORKSPACE_LOCK_ADD_NOTE = _workspace_lock_runtime._add_secondary_failure_note
_WORKSPACE_LOCK_STABLE_STAT = _workspace_lock_runtime._stable_stat_metadata
_WORKSPACE_LOCK_OPEN_READ_ONLY = _workspace_lock_runtime._open_read_only_descriptor
_WORKSPACE_LOCK_TRANSITIVE_FUNCTIONS = (
    (_WORKSPACE_LOCK_ADD_NOTE, getattr(_WORKSPACE_LOCK_ADD_NOTE, "__code__", None)),
    (_WORKSPACE_LOCK_STABLE_STAT, getattr(_WORKSPACE_LOCK_STABLE_STAT, "__code__", None)),
    (_WORKSPACE_LOCK_OPEN_READ_ONLY, getattr(_WORKSPACE_LOCK_OPEN_READ_ONLY, "__code__", None)),
)
_SUBMITTED_STATE = _impl.AttemptState.SUBMITTED

# The outer transport boundary seals not only the public consume function but every
# transitive authority-bearing helper it calls.  Otherwise a post-import replacement
# of (for example) the exact binding validator could leave the outer function/code
# object unchanged while weakening final-send admission.
_CONFIRMATION_AUTHORITY_GRAPH = _confirmation._authority_graph_unchanged
_CONFIRMATION_REQUIRE_BOUND_ACTION = _confirmation._require_bound_action
_CONFIRMATION_REQUIRE_BINDING = _confirmation._require_confirmation_binding
_CONFIRMATION_SPEC = _confirmation.betfair_execution_confirmation_spec
_CONFIRMATION_DECISION_MATERIAL = _confirmation._decision_material
_CONFIRMATION_CONSUMER_KEY = _confirmation._consumer_key
_CONFIRMATION_DOMAIN_DIGEST = _confirmation._domain_digest
_CONFIRMATION_CANONICAL_BYTES = _confirmation._canonical_bytes
_CONFIRMATION_INSTANT = _confirmation._instant
_CONFIRMATION_SHA = _confirmation._sha
_CONFIRMATION_TEXT = _confirmation._text
_CONFIRMATION_WITNESS_TYPE = _confirmation.BetfairExecutionConfirmationWitness
_CONFIRMATION_WITNESS_INIT = _CONFIRMATION_WITNESS_TYPE.__init__
_CONFIRMATION_WITNESS_POST_INIT = _CONFIRMATION_WITNESS_TYPE.__post_init__
_CONFIRMATION_WITNESS_MATERIAL = _CONFIRMATION_WITNESS_TYPE._material
_CONFIRMATION_SPEC_TYPE = _confirmation.BetfairExecutionConfirmationSpec
_CONFIRMATION_SPEC_INIT = _CONFIRMATION_SPEC_TYPE.__init__
_CONFIRMATION_GENERIC_MODULE = _confirmation._confirmation

# Independently pin the final-confirmation module's own cached generic-authority roots.
# The inner module compares live authority against these aliases and then calls the
# aliases directly, so they must not be allowed to move together and become a new
# self-consistent trust root.
_CONFIRMATION_INNER_AUTHORITY_TYPE = _confirmation._AUTHORITY_TYPE
_CONFIRMATION_INNER_AUTHORITY_INIT = _confirmation._AUTHORITY_INIT
_CONFIRMATION_INNER_AUTHORITY_INIT_CODE = _confirmation._AUTHORITY_INIT_CODE
_CONFIRMATION_INNER_RESOLVE_BINDING = _confirmation._RESOLVE_BINDING
_CONFIRMATION_INNER_RESOLVE_BINDING_CODE = _confirmation._RESOLVE_BINDING_CODE
_CONFIRMATION_INNER_CONSUME_RECEIPT = _confirmation._CONSUME_RECEIPT
_CONFIRMATION_INNER_CONSUME_RECEIPT_CODE = _confirmation._CONSUME_RECEIPT_CODE
_CONFIRMATION_INNER_BINDING_TYPE = _confirmation._BINDING_TYPE
_CONFIRMATION_INNER_ERROR = _confirmation._CONFIRMATION_ERROR

# The generic durable authority methods dynamically resolve module helpers and class
# helpers.  Pin the complete callable surfaces once at composition so changing a
# transitive helper cannot preserve the public method code while changing authority.
def _snapshot_callable_graph(namespace: dict[str, object]):
    return tuple(
        (name, value, getattr(value, "__code__", None))
        for name, value in sorted(namespace.items())
        if callable(value)
    )


def _snapshot_class_callable_graph(authority_type: type):
    return tuple(
        (
            name,
            getattr(authority_type, name),
            getattr(getattr(authority_type, name), "__code__", None),
        )
        for name in sorted(vars(authority_type))
        if callable(getattr(authority_type, name, None))
    )


_CONFIRMATION_GENERIC_AUTHORITY_TYPE = (
    _CONFIRMATION_GENERIC_MODULE.SupervisedConfirmationAuthority
)
_CONFIRMATION_GENERIC_CALLABLE_GRAPH = _snapshot_callable_graph(
    vars(_CONFIRMATION_GENERIC_MODULE)
)
_CONFIRMATION_GENERIC_AUTHORITY_METHOD_GRAPH = _snapshot_class_callable_graph(
    _CONFIRMATION_GENERIC_AUTHORITY_TYPE
)
_CONFIRMATION_GENERIC_MONOTONIC_AUTHORITY_TYPE = (
    _CONFIRMATION_GENERIC_MODULE.MonotonicWorkspaceAuthority
)
_CONFIRMATION_GENERIC_MONOTONIC_AUTHORITY_METHOD_GRAPH = (
    _snapshot_class_callable_graph(
        _CONFIRMATION_GENERIC_MONOTONIC_AUTHORITY_TYPE
    )
)
_WORKSPACE_LOCK_METHOD_GRAPH = _snapshot_class_callable_graph(
    _WORKSPACE_LOCK_TYPE
)
del _snapshot_callable_graph, _snapshot_class_callable_graph

_CONFIRMATION_HASHLIB = _confirmation.hashlib
_CONFIRMATION_HASHLIB_SHA256 = _CONFIRMATION_HASHLIB.sha256
_CONFIRMATION_JSON = _confirmation.json
_CONFIRMATION_JSON_DUMPS = _CONFIRMATION_JSON.dumps
_CONFIRMATION_DATETIME = _confirmation.datetime
_CONFIRMATION_PATH = _confirmation.Path
_CONFIRMATION_PATH_RESOLVE = _CONFIRMATION_PATH.resolve
_CONFIRMATION_EXECUTION_ACTION = _confirmation.ExecutionAction
_CONFIRMATION_ACTION_TO_DICT = _CONFIRMATION_EXECUTION_ACTION.to_dict
_CONFIRMATION_BOUND_PLAN = _confirmation.BoundSupervisedExecutionPlan
_CONFIRMATION_BOUND_VERIFY = _CONFIRMATION_BOUND_PLAN.verify_binding
_CONFIRMATION_BOUND_ACTION_FOR = _CONFIRMATION_BOUND_PLAN.action_for
_CONFIRMATION_APPROVAL = _confirmation.SupervisedApproval
_CONFIRMATION_APPROVAL_REQUIRE_ACTIVE = _CONFIRMATION_APPROVAL.require_active
_CONFIRMATION_EXECUTION_ERROR = _confirmation.SupervisedExecutionError
_CONFIRMATION_FILENAME = _confirmation.CONFIRMATION_FILENAME
_CONFIRMATION_REVIEW_PAYLOAD_DOMAIN = _confirmation._REVIEW_PAYLOAD_DOMAIN
_CONFIRMATION_DECISION_DOMAIN = _confirmation._DECISION_DOMAIN
_CONFIRMATION_DECISION_ID_DOMAIN = _confirmation._DECISION_ID_DOMAIN
_CONFIRMATION_CONSUMER_DOMAIN = _confirmation._CONSUMER_DOMAIN
_CONFIRMATION_WITNESS_DOMAIN = _confirmation._WITNESS_DOMAIN
_CONFIRMATION_TRANSITIVE_FUNCTIONS = (
    (_CONFIRMATION_AUTHORITY_GRAPH, _CONFIRMATION_AUTHORITY_GRAPH.__code__),
    (_CONFIRMATION_REQUIRE_BOUND_ACTION, _CONFIRMATION_REQUIRE_BOUND_ACTION.__code__),
    (_CONFIRMATION_REQUIRE_BINDING, _CONFIRMATION_REQUIRE_BINDING.__code__),
    (_CONFIRMATION_SPEC, _CONFIRMATION_SPEC.__code__),
    (_CONFIRMATION_DECISION_MATERIAL, _CONFIRMATION_DECISION_MATERIAL.__code__),
    (_CONFIRMATION_CONSUMER_KEY, _CONFIRMATION_CONSUMER_KEY.__code__),
    (_CONFIRMATION_DOMAIN_DIGEST, _CONFIRMATION_DOMAIN_DIGEST.__code__),
    (_CONFIRMATION_CANONICAL_BYTES, _CONFIRMATION_CANONICAL_BYTES.__code__),
    (_CONFIRMATION_INSTANT, _CONFIRMATION_INSTANT.__code__),
    (_CONFIRMATION_SHA, _CONFIRMATION_SHA.__code__),
    (_CONFIRMATION_TEXT, _CONFIRMATION_TEXT.__code__),
    (_CONFIRMATION_WITNESS_INIT, _CONFIRMATION_WITNESS_INIT.__code__),
    (_CONFIRMATION_WITNESS_POST_INIT, _CONFIRMATION_WITNESS_POST_INIT.__code__),
    (_CONFIRMATION_WITNESS_MATERIAL, _CONFIRMATION_WITNESS_MATERIAL.__code__),
    (_CONFIRMATION_SPEC_INIT, _CONFIRMATION_SPEC_INIT.__code__),
    (_CONFIRMATION_ACTION_TO_DICT, _CONFIRMATION_ACTION_TO_DICT.__code__),
    (_CONFIRMATION_BOUND_VERIFY, _CONFIRMATION_BOUND_VERIFY.__code__),
    (_CONFIRMATION_BOUND_ACTION_FOR, _CONFIRMATION_BOUND_ACTION_FOR.__code__),
    (_CONFIRMATION_APPROVAL_REQUIRE_ACTIVE, _CONFIRMATION_APPROVAL_REQUIRE_ACTIVE.__code__),
)

# Reuse the exact canonical #1891 process-local authority graph. These are not new
# mirrors; identity checks fail closed if the owning module replaces live authority.
_TRUSTED_PROFILE_TYPE = _runtime_profile.TrustedRuntimeCodeProfile
_TRUSTED_PROFILE_ERROR = _runtime_profile.TrustedRuntimeCodeProfileError
_TRUSTED_PROFILE_LOCK = _runtime_profile._LOCK
_TRUSTED_ACTIVE_BY_WORKSPACE = _runtime_profile._ACTIVE_BY_WORKSPACE
_TRUSTED_ISSUED = _runtime_profile._ISSUED
_REQUIRE_TRUSTED_PROFILE = (
    _runtime_profile.require_authoritative_trusted_runtime_code_profile
)
_REQUIRE_TRUSTED_PROFILE_CODE = getattr(_REQUIRE_TRUSTED_PROFILE, "__code__", None)

if (
    not callable(_RAW_PLACE_ACTION)
    or _RAW_PLACE_ACTION_CODE is None
    or _impl._CANONICAL_BETFAIR_PLACE_ACTION is not _RAW_PLACE_ACTION
    or _impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE is not _RAW_PLACE_ACTION_CODE
    or not callable(_CANONICAL_EXECUTE)
    or _CANONICAL_EXECUTE_CODE is None
    or not callable(_PROVIDER_HTTP_POST)
    or _PROVIDER_HTTP_POST_CODE is None
    or not callable(_RESPONSE_PARSER)
    or _RESPONSE_PARSER_CODE is None
    or not callable(_OBSERVATION_CLOCK)
    or _OBSERVATION_CLOCK_CODE is None
    or not callable(_CONSUME_CONFIRMATION)
    or _CONSUME_CONFIRMATION_CODE is None
    or not callable(_VERIFIED_EXECUTION_VIEW)
    or _VERIFIED_EXECUTION_VIEW_CODE is None
    or not callable(_LEDGER_MUTATE)
    or _LEDGER_MUTATE_CODE is None
    or not callable(_FINAL_REQUIRE_APPROVAL)
    or _FINAL_REQUIRE_APPROVAL_CODE is None
    or not callable(_FINAL_REQUIRE_DURABLE_APPROVAL)
    or _FINAL_REQUIRE_DURABLE_APPROVAL_CODE is None
    or not callable(_LEDGER_MARK_SUBMITTED)
    or _LEDGER_MARK_SUBMITTED_CODE is None
    or not callable(_LEDGER_APPROVAL_ACTIVE)
    or _LEDGER_APPROVAL_ACTIVE_CODE is None
    or not all(
        callable(function) and code is not None
        for function, code in _WORKSPACE_LOCK_TRANSITIVE_FUNCTIONS
    )
    or type(_TRUSTED_ACTIVE_BY_WORKSPACE) is not dict
    or type(_TRUSTED_ISSUED) is not dict
    or not callable(_REQUIRE_TRUSTED_PROFILE)
    or _REQUIRE_TRUSTED_PROFILE_CODE is None
):
    raise RuntimeError("canonical Betfair provider-write composition is unavailable")


@dataclass(frozen=True, slots=True)
class _ExecutionConfirmationContext:
    ledger: _impl.RealExecutionLedger
    bound: _impl.BoundSupervisedExecutionPlan
    approval: _impl.SupervisedApproval
    action_id: str
    attempt_id: str
    client: _impl.BetfairSupervisedPlaceOrdersClient
    workspace: str
    receipt_id: str | None
    review_sha256: str | None


_CONFIRMATION_CONTEXT: ContextVar[_ExecutionConfirmationContext | None] = ContextVar(
    "autosport_betfair_execution_confirmation_context",
    default=None,
)


def _trusted_profile_graph_unchanged() -> bool:
    return (
        _runtime_profile.TrustedRuntimeCodeProfile is _TRUSTED_PROFILE_TYPE
        and _runtime_profile.TrustedRuntimeCodeProfileError is _TRUSTED_PROFILE_ERROR
        and _runtime_profile._LOCK is _TRUSTED_PROFILE_LOCK
        and _runtime_profile._ACTIVE_BY_WORKSPACE is _TRUSTED_ACTIVE_BY_WORKSPACE
        and _runtime_profile._ISSUED is _TRUSTED_ISSUED
        and _runtime_profile.require_authoritative_trusted_runtime_code_profile
        is _REQUIRE_TRUSTED_PROFILE
        and getattr(_REQUIRE_TRUSTED_PROFILE, "__code__", None)
        is _REQUIRE_TRUSTED_PROFILE_CODE
    )


def _confirmation_graph_unchanged() -> bool:
    return (
        _confirmation.BetfairExecutionConfirmationError is _CONFIRMATION_ERROR
        and _confirmation.consume_betfair_execution_confirmation is _CONSUME_CONFIRMATION
        and getattr(_CONSUME_CONFIRMATION, "__code__", None)
        is _CONSUME_CONFIRMATION_CODE
        and _confirmation._authority_graph_unchanged is _CONFIRMATION_AUTHORITY_GRAPH
        and _confirmation._require_bound_action is _CONFIRMATION_REQUIRE_BOUND_ACTION
        and _confirmation._require_confirmation_binding is _CONFIRMATION_REQUIRE_BINDING
        and _confirmation.betfair_execution_confirmation_spec is _CONFIRMATION_SPEC
        and _confirmation._decision_material is _CONFIRMATION_DECISION_MATERIAL
        and _confirmation._consumer_key is _CONFIRMATION_CONSUMER_KEY
        and _confirmation._domain_digest is _CONFIRMATION_DOMAIN_DIGEST
        and _confirmation._canonical_bytes is _CONFIRMATION_CANONICAL_BYTES
        and _confirmation._instant is _CONFIRMATION_INSTANT
        and _confirmation._sha is _CONFIRMATION_SHA
        and _confirmation._text is _CONFIRMATION_TEXT
        and _confirmation.BetfairExecutionConfirmationWitness is _CONFIRMATION_WITNESS_TYPE
        and _CONFIRMATION_WITNESS_TYPE.__init__ is _CONFIRMATION_WITNESS_INIT
        and _CONFIRMATION_WITNESS_TYPE.__post_init__ is _CONFIRMATION_WITNESS_POST_INIT
        and _CONFIRMATION_WITNESS_TYPE._material is _CONFIRMATION_WITNESS_MATERIAL
        and _confirmation.BetfairExecutionConfirmationSpec is _CONFIRMATION_SPEC_TYPE
        and _CONFIRMATION_SPEC_TYPE.__init__ is _CONFIRMATION_SPEC_INIT
        and _confirmation._confirmation is _CONFIRMATION_GENERIC_MODULE
        and _confirmation._AUTHORITY_TYPE is _CONFIRMATION_INNER_AUTHORITY_TYPE
        and _confirmation._AUTHORITY_INIT is _CONFIRMATION_INNER_AUTHORITY_INIT
        and _confirmation._AUTHORITY_INIT_CODE is _CONFIRMATION_INNER_AUTHORITY_INIT_CODE
        and _confirmation._RESOLVE_BINDING is _CONFIRMATION_INNER_RESOLVE_BINDING
        and _confirmation._RESOLVE_BINDING_CODE is _CONFIRMATION_INNER_RESOLVE_BINDING_CODE
        and _confirmation._CONSUME_RECEIPT is _CONFIRMATION_INNER_CONSUME_RECEIPT
        and _confirmation._CONSUME_RECEIPT_CODE is _CONFIRMATION_INNER_CONSUME_RECEIPT_CODE
        and _confirmation._BINDING_TYPE is _CONFIRMATION_INNER_BINDING_TYPE
        and _confirmation._CONFIRMATION_ERROR is _CONFIRMATION_INNER_ERROR
        and _CONFIRMATION_GENERIC_MODULE.SupervisedConfirmationAuthority
        is _CONFIRMATION_GENERIC_AUTHORITY_TYPE
        and all(
            getattr(_CONFIRMATION_GENERIC_MODULE, name, None) is value
            and getattr(value, "__code__", None) is code
            for name, value, code in _CONFIRMATION_GENERIC_CALLABLE_GRAPH
        )
        and all(
            getattr(_CONFIRMATION_GENERIC_AUTHORITY_TYPE, name, None) is value
            and getattr(value, "__code__", None) is code
            for name, value, code in _CONFIRMATION_GENERIC_AUTHORITY_METHOD_GRAPH
        )
        and _CONFIRMATION_GENERIC_MODULE.MonotonicWorkspaceAuthority
        is _CONFIRMATION_GENERIC_MONOTONIC_AUTHORITY_TYPE
        and all(
            getattr(
                _CONFIRMATION_GENERIC_MONOTONIC_AUTHORITY_TYPE,
                name,
                None,
            )
            is value
            and getattr(value, "__code__", None) is code
            for name, value, code in (
                _CONFIRMATION_GENERIC_MONOTONIC_AUTHORITY_METHOD_GRAPH
            )
        )
        and _confirmation.hashlib is _CONFIRMATION_HASHLIB
        and _CONFIRMATION_HASHLIB.sha256 is _CONFIRMATION_HASHLIB_SHA256
        and _confirmation.json is _CONFIRMATION_JSON
        and _CONFIRMATION_JSON.dumps is _CONFIRMATION_JSON_DUMPS
        and _confirmation.datetime is _CONFIRMATION_DATETIME
        and _confirmation.Path is _CONFIRMATION_PATH
        and _CONFIRMATION_PATH.resolve is _CONFIRMATION_PATH_RESOLVE
        and _confirmation.ExecutionAction is _CONFIRMATION_EXECUTION_ACTION
        and _CONFIRMATION_EXECUTION_ACTION.to_dict is _CONFIRMATION_ACTION_TO_DICT
        and _confirmation.BoundSupervisedExecutionPlan is _CONFIRMATION_BOUND_PLAN
        and _CONFIRMATION_BOUND_PLAN.verify_binding is _CONFIRMATION_BOUND_VERIFY
        and _CONFIRMATION_BOUND_PLAN.action_for is _CONFIRMATION_BOUND_ACTION_FOR
        and _confirmation.SupervisedApproval is _CONFIRMATION_APPROVAL
        and _CONFIRMATION_APPROVAL.require_active is _CONFIRMATION_APPROVAL_REQUIRE_ACTIVE
        and _confirmation.SupervisedExecutionError is _CONFIRMATION_EXECUTION_ERROR
        and _confirmation.CONFIRMATION_FILENAME == _CONFIRMATION_FILENAME
        and _confirmation._REVIEW_PAYLOAD_DOMAIN == _CONFIRMATION_REVIEW_PAYLOAD_DOMAIN
        and _confirmation._DECISION_DOMAIN == _CONFIRMATION_DECISION_DOMAIN
        and _confirmation._DECISION_ID_DOMAIN == _CONFIRMATION_DECISION_ID_DOMAIN
        and _confirmation._CONSUMER_DOMAIN == _CONFIRMATION_CONSUMER_DOMAIN
        and _confirmation._WITNESS_DOMAIN == _CONFIRMATION_WITNESS_DOMAIN
        and all(
            getattr(function, "__code__", None) is code
            for function, code in _CONFIRMATION_TRANSITIVE_FUNCTIONS
        )
        and _CONFIRMATION_AUTHORITY_GRAPH()
        and _impl.RealExecutionLedger.verified_execution_view is _VERIFIED_EXECUTION_VIEW
        and getattr(_VERIFIED_EXECUTION_VIEW, "__code__", None)
        is _VERIFIED_EXECUTION_VIEW_CODE
        and _impl.RealExecutionLedger._mutate is _LEDGER_MUTATE
        and getattr(_LEDGER_MUTATE, "__code__", None) is _LEDGER_MUTATE_CODE
        and _impl._CANONICAL_REQUIRE_SUPERVISED_APPROVAL
        is _FINAL_REQUIRE_APPROVAL
        and getattr(_FINAL_REQUIRE_APPROVAL, "__code__", None)
        is _FINAL_REQUIRE_APPROVAL_CODE
        and _impl._CANONICAL_REQUIRE_DURABLE_SUPERVISED_APPROVAL
        is _FINAL_REQUIRE_DURABLE_APPROVAL
        and getattr(_FINAL_REQUIRE_DURABLE_APPROVAL, "__code__", None)
        is _FINAL_REQUIRE_DURABLE_APPROVAL_CODE
        and _impl._CANONICAL_LEDGER_MARK_SUBMITTED
        is _LEDGER_MARK_SUBMITTED
        and getattr(_LEDGER_MARK_SUBMITTED, "__code__", None)
        is _LEDGER_MARK_SUBMITTED_CODE
        and _impl.RealExecutionLedger.mark_submitted is _LEDGER_MARK_SUBMITTED
        and _impl.RealExecutionLedger.supervised_approval_is_active
        is _LEDGER_APPROVAL_ACTIVE
        and getattr(_LEDGER_APPROVAL_ACTIVE, "__code__", None)
        is _LEDGER_APPROVAL_ACTIVE_CODE
        and _ledger_runtime.RealExecutionLedger is _impl.RealExecutionLedger
        and _ledger_runtime.WorkspaceEconomicLock is _WORKSPACE_LOCK_TYPE
        and _ledger_runtime.WorkspaceEconomicLockError is _WORKSPACE_LOCK_ERROR
        and _ledger_runtime.WorkspaceEconomicLockBusyError
        is _WORKSPACE_LOCK_BUSY_ERROR
        and _workspace_lock_runtime.WorkspaceEconomicLock is _WORKSPACE_LOCK_TYPE
        and _workspace_lock_runtime.WorkspaceEconomicLockError
        is _WORKSPACE_LOCK_ERROR
        and _workspace_lock_runtime.WorkspaceEconomicLockBusyError
        is _WORKSPACE_LOCK_BUSY_ERROR
        and _workspace_lock_runtime._add_secondary_failure_note
        is _WORKSPACE_LOCK_ADD_NOTE
        and _workspace_lock_runtime._stable_stat_metadata
        is _WORKSPACE_LOCK_STABLE_STAT
        and _workspace_lock_runtime._open_read_only_descriptor
        is _WORKSPACE_LOCK_OPEN_READ_ONLY
        and all(
            getattr(function, "__code__", None) is code
            for function, code in _WORKSPACE_LOCK_TRANSITIVE_FUNCTIONS
        )
        and all(
            getattr(_WORKSPACE_LOCK_TYPE, name, None) is value
            and getattr(value, "__code__", None) is code
            for name, value, code in _WORKSPACE_LOCK_METHOD_GRAPH
        )
        and _impl.AttemptState.SUBMITTED is _SUBMITTED_STATE
    )


def _canonical_workspace_text(value: object) -> str:
    try:
        resolved = Path(value).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile requires canonical execution workspace"
        ) from exc
    text = str(resolved)
    if not text or text.strip() != text:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile workspace is not canonical"
        )
    return text


def _require_current_workspace_profile_locked(workspace: str):
    if not _trusted_profile_graph_unchanged():
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile authority changed"
        )
    profile_identity = _TRUSTED_ACTIVE_BY_WORKSPACE.get(workspace)
    if type(profile_identity) is not int:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted RUNNING product runtime profile is required for provider write"
        )
    record = _TRUSTED_ISSUED.get(profile_identity)
    profile = getattr(record, "profile", None)
    if type(profile) is not _TRUSTED_PROFILE_TYPE or id(profile) != profile_identity:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile issuance is inconsistent"
        )
    try:
        resolved = _REQUIRE_TRUSTED_PROFILE(profile, workspace=workspace)
    except _TRUSTED_PROFILE_ERROR as exc:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted RUNNING product runtime profile is not authoritative"
        ) from exc
    if resolved is not profile:
        raise _impl.BetfairSupervisedExecutionError(
            "trusted runtime profile resolver changed exact issuance"
        )
    return profile


def _require_confirmation_context(
    *,
    client: _impl.BetfairSupervisedPlaceOrdersClient,
    action: _impl.ExecutionAction,
    bound: _impl.BoundSupervisedExecutionPlan,
    workspace: str,
) -> _ExecutionConfirmationContext:
    context = _CONFIRMATION_CONTEXT.get()
    if type(context) is not _ExecutionConfirmationContext:
        raise _impl.BetfairSupervisedExecutionError(
            "Betfair provider write requires durable operator confirmation"
        )
    if (
        context.client is not client
        or context.bound is not bound
        or context.action_id != action.action_id
        or context.workspace != workspace
        or context.receipt_id is None
        or context.review_sha256 is None
    ):
        raise _impl.BetfairSupervisedExecutionError(
            "Betfair operator confirmation context does not bind this exact execution"
        )
    return context


def _durable_submitted_at(
    context: _ExecutionConfirmationContext,
    *,
    action: _impl.ExecutionAction,
    request_sha256: str,
) -> str:
    """Re-resolve exact SUBMITTED/request identity from one verified ledger view."""

    if not _confirmation_graph_unchanged():
        raise _impl.BetfairSupervisedExecutionError(
            "Betfair confirmation/ledger authority changed before final send"
        )
    view = _VERIFIED_EXECUTION_VIEW(
        context.ledger,
        context.bound.execution_plan.plan_id,
    )
    if (
        view.plan_fingerprint != context.bound.execution_plan.fingerprint
        or view.plan is None
        or view.plan.plan_id != context.bound.execution_plan.plan_id
    ):
        raise _impl.BetfairSupervisedExecutionError(
            "durable execution plan changed before Betfair final send"
        )
    matches = tuple(
        attempt
        for attempt in view.attempts
        if attempt.attempt.attempt_id == context.attempt_id
    )
    if len(matches) != 1:
        raise _impl.BetfairSupervisedExecutionError(
            "durable Betfair attempt is not uniquely submitted"
        )
    attempt = matches[0]
    if (
        attempt.action.action_id != action.action_id
        or attempt.state is not _SUBMITTED_STATE
        or attempt.submitted_at is None
        or attempt.submitted_request_sha256 != request_sha256
    ):
        raise _impl.BetfairSupervisedExecutionError(
            "durable Betfair SUBMITTED/request identity mismatches final-send bytes"
        )
    return attempt.submitted_at


def _build_trusted_private_place_action(private_place_action, private_place_action_code):
    def _trusted_private_place_action(
        self: _impl.BetfairSupervisedPlaceOrdersClient,
        action: _impl.ExecutionAction,
        *,
        profile: _impl.BookmakerCapabilityProfile,
        bound: _impl.BoundSupervisedExecutionPlan,
        provider_order_ref: str,
        execution_workspace: Path,
        _before_transport=None,
        _transport_post=None,
        _response_parser=None,
        _observation_clock=None,
    ):
        try:
            caller_code = sys._getframe(1).f_code
        except (AttributeError, ValueError):
            caller_code = None
        if caller_code is not _CANONICAL_EXECUTE_CODE:
            raise _impl.BetfairSupervisedExecutionError(
                "private Betfair provider write requires canonical execution caller"
            )
        if (
            getattr(private_place_action, "__code__", None) is not private_place_action_code
            or _transport_post is not _PROVIDER_HTTP_POST
            or _response_parser is not _RESPONSE_PARSER
            or _observation_clock is not _OBSERVATION_CLOCK
            or not callable(_before_transport)
        ):
            raise _impl.BetfairSupervisedExecutionError(
                "canonical Betfair internal provider-write dependencies changed"
            )
        workspace = _canonical_workspace_text(execution_workspace)
        confirmation_context = _require_confirmation_context(
            client=self,
            action=action,
            bound=bound,
            workspace=workspace,
        )

        def _confirmed_before_transport(request_sha256: str) -> None:
            # Existing callback first establishes the canonical no-blind-retry
            # boundary and durably binds these exact provider request bytes.
            _before_transport(request_sha256)
            submitted_at = _durable_submitted_at(
                confirmation_context,
                action=action,
                request_sha256=request_sha256,
            )
            if not _confirmation_graph_unchanged():
                raise _impl.BetfairSupervisedExecutionError(
                    "Betfair confirmation authority changed before provider send"
                )
            try:
                _CONSUME_CONFIRMATION(
                    Path(workspace),
                    bound,
                    confirmation_context.approval,
                    action_id=action.action_id,
                    attempt_id=confirmation_context.attempt_id,
                    receipt_id=confirmation_context.receipt_id,
                    expected_review_sha256=confirmation_context.review_sha256,
                    request_sha256=request_sha256,
                    submitted_at=submitted_at,
                )
            except _CONFIRMATION_ERROR as exc:
                raise _impl.BetfairSupervisedExecutionError(
                    "durable Betfair operator confirmation denied final provider send"
                ) from exc

            # Durable confirmation I/O can consume wall time after the pre-submit
            # approval/quote check. Sample the product-owned trusted clock again
            # after receipt consumption and fail closed before POST if authority
            # expired in that interval. The attempt is already SUBMITTED and the
            # receipt already consumed, so every such denial remains on the
            # canonical no-blind-retry side.
            final_send_at = _impl._supervised_execution_runtime._trusted_now()
            if not _confirmation_graph_unchanged():
                raise _impl.BetfairSupervisedExecutionError(
                    "Betfair confirmation authority changed after durable confirmation"
                )
            final_send_instant = _CONFIRMATION_INSTANT(
                final_send_at,
                "final_send_at",
            )
            submitted_instant = _CONFIRMATION_INSTANT(
                submitted_at,
                "submitted_at",
            )
            if final_send_instant < submitted_instant:
                raise _impl.BetfairSupervisedExecutionError(
                    "trusted Betfair final-send clock moved backwards after confirmation"
                )

            # Confirmation lifetime is an independent final-send authority. The
            # receipt was intentionally consumed at the durable SUBMITTED instant,
            # so re-read its exact durable binding using the fresh post-I/O clock
            # and reject if the review TTL elapsed before the actual POST seam.
            try:
                final_confirmation_authority = _CONFIRMATION_INNER_AUTHORITY_TYPE(
                    Path(workspace) / _CONFIRMATION_FILENAME,
                    clock=lambda: final_send_instant,
                )
                final_confirmation = _CONFIRMATION_INNER_RESOLVE_BINDING(
                    final_confirmation_authority,
                    receipt_id=confirmation_context.receipt_id,
                    expected_review_sha256=confirmation_context.review_sha256,
                    require_unconsumed=False,
                )
                _CONFIRMATION_REQUIRE_BINDING(
                    final_confirmation,
                    bound=bound,
                    approval=confirmation_context.approval,
                    action=action,
                    attempt_id=confirmation_context.attempt_id,
                    submitted_at=submitted_at,
                )
            except (_CONFIRMATION_INNER_ERROR, _CONFIRMATION_ERROR) as exc:
                raise _impl.BetfairSupervisedExecutionError(
                    "durable Betfair operator confirmation changed after consumption"
                ) from exc
            if type(final_confirmation) is not _CONFIRMATION_INNER_BINDING_TYPE:
                raise _impl.BetfairSupervisedExecutionError(
                    "durable Betfair operator confirmation binding changed"
                )
            if (
                final_confirmation.receipt.consumed_at is None
                or _CONFIRMATION_INSTANT(
                    final_confirmation.receipt.consumed_at,
                    "receipt.consumed_at",
                )
                != submitted_instant
                or final_confirmation.receipt.consumed_by is None
            ):
                raise _impl.BetfairSupervisedExecutionError(
                    "durable Betfair operator confirmation consumption changed"
                )
            if final_send_instant >= _CONFIRMATION_INSTANT(
                final_confirmation.review.expires_at,
                "review.expires_at",
            ):
                raise _impl.BetfairSupervisedExecutionError(
                    "durable Betfair operator confirmation expired before provider send"
                )

            _FINAL_REQUIRE_APPROVAL(
                bound,
                confirmation_context.approval,
                final_send_at,
            )
            _FINAL_REQUIRE_DURABLE_APPROVAL(
                confirmation_context.ledger,
                bound,
                confirmation_context.approval,
            )
            if final_send_instant >= _CONFIRMATION_INSTANT(
                action.expires_at,
                "action.expires_at",
            ):
                raise _impl.BetfairSupervisedExecutionError(
                    "Betfair action quote expired after durable confirmation"
                )
            if _durable_submitted_at(
                confirmation_context,
                action=action,
                request_sha256=request_sha256,
            ) != submitted_at:
                raise _impl.BetfairSupervisedExecutionError(
                    "durable Betfair submission time changed after confirmation"
                )

        with _TRUSTED_PROFILE_LOCK:
            _require_current_workspace_profile_locked(workspace)
            if (
                not _canonical_internal_dispatch_unchanged()
                or not _confirmation_graph_unchanged()
            ):
                raise _impl.BetfairSupervisedExecutionError(
                    "canonical Betfair internal provider-write authority changed"
                )

            def dispatch_under_durable_approval_fence():
                return private_place_action(
                    self,
                    action,
                    profile=profile,
                    bound=bound,
                    provider_order_ref=provider_order_ref,
                    execution_workspace=execution_workspace,
                    _before_transport=_confirmed_before_transport,
                    _transport_post=_PROVIDER_HTTP_POST,
                    _response_parser=_RESPONSE_PARSER,
                    _observation_clock=_OBSERVATION_CLOCK,
                )

            return _LEDGER_MUTATE(
                confirmation_context.ledger,
                dispatch_under_durable_approval_fence,
            )
    return _trusted_private_place_action


_TRUSTED_PRIVATE_PLACE_ACTION = _build_trusted_private_place_action(
    _RAW_PLACE_ACTION,
    _RAW_PLACE_ACTION_CODE,
)
del _RAW_PLACE_ACTION, _RAW_PLACE_ACTION_CODE
_TRUSTED_PRIVATE_PLACE_ACTION_CODE = _TRUSTED_PRIVATE_PLACE_ACTION.__code__
_impl._CANONICAL_BETFAIR_PLACE_ACTION = _TRUSTED_PRIVATE_PLACE_ACTION
_impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE = _TRUSTED_PRIVATE_PLACE_ACTION_CODE


def _confirmed_execute_betfair_supervised_action(
    ledger: _impl.RealExecutionLedger,
    bound: _impl.BoundSupervisedExecutionPlan,
    approval: _impl.SupervisedApproval,
    *,
    action_id: str,
    attempt_id: str,
    profile: _impl.BookmakerCapabilityProfile,
    client: _impl.BetfairSupervisedPlaceOrdersClient,
    clock=None,
    confirmation_receipt_id: str | None = None,
    confirmation_review_sha256: str | None = None,
):
    """Carry one exact durable final-send receipt through canonical execution."""

    try:
        workspace = str(Path(ledger.path).parent.resolve())
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _impl.BetfairSupervisedExecutionError(
            "Betfair execution ledger workspace is not canonical"
        ) from exc
    context = _ExecutionConfirmationContext(
        ledger=ledger,
        bound=bound,
        approval=approval,
        action_id=action_id,
        attempt_id=attempt_id,
        client=client,
        workspace=workspace,
        receipt_id=confirmation_receipt_id,
        review_sha256=confirmation_review_sha256,
    )
    token = _CONFIRMATION_CONTEXT.set(context)
    try:
        return _CANONICAL_EXECUTE(
            ledger,
            bound,
            approval,
            action_id=action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            clock=clock,
        )
    finally:
        _CONFIRMATION_CONTEXT.reset(token)


_PUBLIC_EXECUTE = _confirmed_execute_betfair_supervised_action
_PUBLIC_EXECUTE_CODE = _PUBLIC_EXECUTE.__code__
_PUBLIC_EXECUTE.__name__ = _CANONICAL_EXECUTE.__name__
_PUBLIC_EXECUTE.__qualname__ = _CANONICAL_EXECUTE.__qualname__
_PUBLIC_EXECUTE.__module__ = _CANONICAL_EXECUTE.__module__
_PUBLIC_EXECUTE.__doc__ = _CANONICAL_EXECUTE.__doc__


def _canonical_internal_dispatch_unchanged() -> bool:
    return (
        _CLIENT_TYPE is _impl.BetfairSupervisedPlaceOrdersClient
        and _CANONICAL_TRANSPORT_TYPE is _impl.UrllibBetfairHttpTransport
        and _CLIENT_TYPE.__dict__.get("place_action") is _BOUNDARY
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION is _TRUSTED_PRIVATE_PLACE_ACTION
        and _impl._CANONICAL_BETFAIR_PLACE_ACTION_CODE is _TRUSTED_PRIVATE_PLACE_ACTION_CODE
        and getattr(_TRUSTED_PRIVATE_PLACE_ACTION, "__code__", None)
        is _TRUSTED_PRIVATE_PLACE_ACTION_CODE
        and "_RAW_PLACE_ACTION" not in globals()
        and "_RAW_PLACE_ACTION_CODE" not in globals()
        and "_PRIVATE_PLACE_ACTION" not in globals()
        and _impl.execute_betfair_supervised_action is _PUBLIC_EXECUTE
        and getattr(_PUBLIC_EXECUTE, "__code__", None) is _PUBLIC_EXECUTE_CODE
        and getattr(_CANONICAL_EXECUTE, "__code__", None) is _CANONICAL_EXECUTE_CODE
        and _impl._PROVIDER_HTTP_POST is _PROVIDER_HTTP_POST
        and _impl._CANONICAL_PROVIDER_HTTP_POST is _PROVIDER_HTTP_POST
        and getattr(_PROVIDER_HTTP_POST, "__code__", None) is _PROVIDER_HTTP_POST_CODE
        and _impl._parse_place_orders_response is _RESPONSE_PARSER
        and _impl._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE is _RESPONSE_PARSER
        and getattr(_RESPONSE_PARSER, "__code__", None) is _RESPONSE_PARSER_CODE
        and _impl._provider_observation_now is _OBSERVATION_CLOCK
        and _impl._CANONICAL_PROVIDER_OBSERVATION_CLOCK is _OBSERVATION_CLOCK
        and getattr(_OBSERVATION_CLOCK, "__code__", None) is _OBSERVATION_CLOCK_CODE
        and _trusted_profile_graph_unchanged()
        and _confirmation_graph_unchanged()
    )


def _public_place_action(
    self: _impl.BetfairSupervisedPlaceOrdersClient,
    action: _impl.ExecutionAction,
    *,
    profile: _impl.BookmakerCapabilityProfile,
    bound: _impl.BoundSupervisedExecutionPlan,
    provider_order_ref: str,
    execution_workspace: Path,
) -> _impl.BetfairPlaceExecutionReport:
    raise _impl.BetfairSupervisedExecutionError(
        "direct public Betfair provider write is disabled; "
        "use execute_betfair_supervised_action"
    )


_public_place_action.__name__ = "place_action"
_public_place_action.__qualname__ = f"{_CLIENT_TYPE.__name__}.place_action"
_public_place_action.__module__ = _impl.__name__


class _PlaceActionBoundary:
    __slots__ = ()

    def __get__(self, instance: object, owner: type | None = None):
        try:
            caller_code = sys._getframe(1).f_code
        except (AttributeError, ValueError):
            caller_code = None
        if caller_code is _CANONICAL_EXECUTE_CODE:
            if not _trusted_profile_graph_unchanged():
                raise _impl.BetfairSupervisedExecutionError(
                    "trusted runtime profile authority changed"
                )
            if not _canonical_internal_dispatch_unchanged():
                raise _impl.BetfairSupervisedExecutionError(
                    "terminal Betfair execution requires canonical client, transport, "
                    "parser, ledger and confirmation authority"
                )
            dispatch = _TRUSTED_PRIVATE_PLACE_ACTION
        else:
            dispatch = _public_place_action
        if instance is None:
            return dispatch
        return dispatch.__get__(instance, owner or _CLIENT_TYPE)


_BOUNDARY = _PlaceActionBoundary()
_CLIENT_TYPE.place_action = _BOUNDARY
_impl.execute_betfair_supervised_action = _PUBLIC_EXECUTE
