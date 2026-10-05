from __future__ import annotations

"""Fresh product-owned re-resolution verifier for Betfair standard-LIMIT bounds.

``BetfairStandardLimitPriceBoundEvidence`` and ``BoundSupervisedExecutionPlan``
are ordinary Python values, not unforgeable capabilities. Positive product
authority therefore starts from the independent durable supervised-plan issuance
store. The execution ledger is consulted only after that provenance is restored,
for exact reservation/approval execution-state continuity. A caller cannot supply
a bound DTO to this public verifier.

The verifier deliberately does not late-dispatch through its public resolver
module global.  One exact resolver/writer graph is captured when this module is
loaded and witnessed immediately before and after fresh re-resolution.  This
prevents a caller from installing the same hostile resolver in issuance and in
verification and thereby turning common-mode agreement into provider authority.
"""

from decimal import Decimal

from . import betfair_standard_limit_price_bound as _price_bound_module
from . import real_execution_ledger as _ledger_module
from . import supervised_plan_issuance as _issuance_module
from . import monotonic_workspace_authority as _monotonic_module
from . import json_integrity as _json_integrity_module
from . import integrity as _integrity_module
from . import workspace_lock as _workspace_lock_module
from . import supervised_execution as _supervised_module
from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
)
from .real_execution_ledger import ExecutionAction, RealExecutionLedger
from .supervised_execution import BoundSupervisedExecutionPlan
from .supervised_plan_issuance import (
    IssuedSupervisedPlan,
    SupervisedPlanIssuanceError,
    SupervisedPlanIssuanceStore,
)


# Compatibility/debug alias only.  The authoritative verifier entrypoint below
# never late-dispatches through this mutable module global.
resolve_betfair_standard_limit_price_bound = (
    _price_bound_module.resolve_betfair_standard_limit_price_bound
)


_EVIDENCE_FIELDS = (
    "execution_plan_id",
    "execution_plan_sha256",
    "portfolio_plan_sha256",
    "intent_id",
    "intent_sha256",
    "action_id",
    "bookmaker_id",
    "account_id",
    "event_id",
    "market_id",
    "selection_id",
    "side",
    "requested_stake",
    "price_floor_odds",
    "quote_id",
    "quote_observed_at",
    "quote_expires_at",
    "decision_at",
    "instruction_sha256",
    "provider_contract_id",
    "provider_contract_ref",
    "write_adapter_id",
    "write_adapter_version",
    "status",
    "matchme_applicability_proven",
    "zero_adverse_price_deterioration",
    "execution_feasibility_proven",
    "realized_price_exact",
)


def _exact_snapshot(
    evidence: BetfairStandardLimitPriceBoundEvidence,
) -> tuple[tuple[str, type[object], object], ...]:
    if type(evidence) is not BetfairStandardLimitPriceBoundEvidence:
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence must be the exact canonical evidence type"
        )

    snapshot: list[tuple[str, type[object], object]] = []
    for field in _EVIDENCE_FIELDS:
        try:
            value = object.__getattribute__(evidence, field)
        except (AttributeError, TypeError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "price-bound evidence is incomplete"
            ) from exc
        comparable: object = str(value) if type(value) is Decimal else value
        snapshot.append((field, type(value), comparable))
    return tuple(snapshot)


def _require_execution_state_continuity(
    *,
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
) -> None:
    """Require ledger continuity without allowing ledger rows to mint provenance."""

    if type(ledger) is not RealExecutionLedger:
        raise BetfairStandardLimitPriceBoundError(
            "ledger must be the exact canonical RealExecutionLedger type"
        )
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairStandardLimitPriceBoundError(
            "issued bound must be the exact canonical BoundSupervisedExecutionPlan type"
        )

    BoundSupervisedExecutionPlan.verify_binding(bound)
    try:
        saga = RealExecutionLedger.saga(ledger, bound.execution_plan.plan_id)
    except KeyError as exc:
        raise BetfairStandardLimitPriceBoundError(
            "product-issued execution plan is not durably reserved"
        ) from exc
    if saga.plan_fingerprint != bound.execution_plan.fingerprint:
        raise BetfairStandardLimitPriceBoundError(
            "durable execution-plan fingerprint mismatches product issuance"
        )
    if not RealExecutionLedger.supervised_approval_is_active(
        ledger,
        plan_id=bound.execution_plan.plan_id,
        approval_id=bound.execution_plan.approval_id,
        approval_fingerprint=bound.approval_fingerprint,
    ):
        raise BetfairStandardLimitPriceBoundError(
            "durable supervised approval is missing or revoked"
        )


def _require_issuance_time_provider_request(
    *,
    issued_requests: tuple[dict[str, object], ...],
    expected: BetfairStandardLimitPriceBoundEvidence,
) -> None:
    """Require that this action had positive canonical request proof at issuance."""

    matches = [
        item
        for item in issued_requests
        if item.get("action_id") == expected.action_id
    ]
    if len(matches) != 1:
        raise BetfairStandardLimitPriceBoundError(
            "Betfair request identity was not durably proven at plan issuance"
        )
    durable = matches[0]
    expected_request = {
        "action_id": expected.action_id,
        "bookmaker_id": expected.bookmaker_id,
        "account_id": expected.account_id,
        "instruction_sha256": expected.instruction_sha256,
        "write_adapter_id": expected.write_adapter_id,
        "write_adapter_version": expected.write_adapter_version,
    }
    if durable != expected_request:
        raise BetfairStandardLimitPriceBoundError(
            "durable issuance-time Betfair request identity changed"
        )


def _build_product_verifier():
    """Capture the exact resolver graph before any caller can use the verifier."""

    resolver = _price_bound_module.resolve_betfair_standard_limit_price_bound
    resolver_code = resolver.__code__
    error_cls = BetfairStandardLimitPriceBoundError
    evidence_cls = BetfairStandardLimitPriceBoundEvidence
    issuance_store_cls = SupervisedPlanIssuanceStore
    issued_plan_cls = IssuedSupervisedPlan
    issued_plan_init = issued_plan_cls.__init__
    issued_plan_init_code = issued_plan_init.__code__
    issuance_error_cls = SupervisedPlanIssuanceError
    ledger_cls = RealExecutionLedger
    bound_cls = BoundSupervisedExecutionPlan
    decimal_cls = Decimal
    evidence_fields = tuple(_EVIDENCE_FIELDS)

    direct_helpers = (
        ("resolver text", "_text", _price_bound_module._text),
        (
            "resolver positive decimal",
            "_positive_decimal",
            _price_bound_module._positive_decimal,
        ),
        ("resolver digest", "_digest", _price_bound_module._digest),
        (
            "canonical instruction projection",
            "_canonical_instruction_projection",
            _price_bound_module._canonical_instruction_projection,
        ),
        (
            "canonical request capture",
            "_capture_canonical_instruction",
            _price_bound_module._capture_canonical_instruction,
        ),
        ("evidence issuer", "_issue_evidence", _price_bound_module._issue_evidence),
    )
    helper_witnesses = tuple(
        (label, name, function, function.__code__)
        for label, name, function in direct_helpers
    )

    captured_place_action = _price_bound_module._CANONICAL_PLACE_ACTION
    captured_place_action_code = captured_place_action.__code__
    execution_to_dict = ExecutionAction.to_dict
    execution_to_dict_code = execution_to_dict.__code__
    bound_verify = BoundSupervisedExecutionPlan.verify_binding
    bound_verify_code = bound_verify.__code__
    bound_action_for = BoundSupervisedExecutionPlan.action_for
    bound_action_for_code = bound_action_for.__code__
    bound_binding_helper = _supervised_module._bound_binding_sha256
    bound_binding_helper_code = bound_binding_helper.__code__
    supervised_digest = _supervised_module._digest
    supervised_digest_code = supervised_digest.__code__
    supervised_json_dumps = _supervised_module.json.dumps
    supervised_json_dumps_code = supervised_json_dumps.__code__
    supervised_hash_constructor = _supervised_module.hashlib.sha256
    execution_plan_to_dict = _supervised_module.ExecutionPlan.to_dict
    execution_plan_to_dict_code = execution_plan_to_dict.__code__
    execution_plan_fingerprint_getter = _supervised_module.ExecutionPlan.fingerprint.fget
    execution_plan_fingerprint_getter_code = execution_plan_fingerprint_getter.__code__
    constraint_to_dict = _supervised_module.ExecutionLegConstraint.to_dict
    constraint_to_dict_code = constraint_to_dict.__code__
    approval_fingerprint_getter = _supervised_module.SupervisedApproval.fingerprint.fget
    approval_fingerprint_getter_code = approval_fingerprint_getter.__code__
    approval_ledger_identity_getter = _supervised_module.SupervisedApproval.ledger_identity.fget
    approval_ledger_identity_getter_code = approval_ledger_identity_getter.__code__
    issuance_load = issuance_store_cls.load
    issuance_load_code = issuance_load.__code__
    issuance_load_locked = issuance_store_cls._load_locked
    issuance_load_locked_code = issuance_load_locked.__code__
    issuance_authority = issuance_store_cls._authority
    issuance_authority_code = issuance_authority.__code__
    issuance_path = issuance_store_cls._path
    issuance_path_code = issuance_path.__code__
    issuance_plan_file_name = _issuance_module._plan_file_name
    issuance_plan_file_name_code = issuance_plan_file_name.__code__
    issuance_exact_keys = _issuance_module._exact_keys
    issuance_exact_keys_code = issuance_exact_keys.__code__
    issuance_text = _issuance_module._text
    issuance_text_code = issuance_text.__code__
    issuance_sha = _issuance_module._sha
    issuance_sha_code = issuance_sha.__code__
    issuance_digest = _issuance_module._digest
    issuance_digest_code = issuance_digest.__code__
    issuance_decode_approval = _issuance_module._decode_approval
    issuance_decode_approval_code = issuance_decode_approval.__code__
    issuance_decode_bound = _issuance_module._decode_bound
    issuance_decode_bound_code = issuance_decode_bound.__code__
    issuance_provider_request = _issuance_module._provider_request_payload
    issuance_provider_request_code = issuance_provider_request.__code__
    issuance_provider_resolver = _issuance_module.resolve_betfair_standard_limit_price_bound
    issuance_provider_resolver_code = issuance_provider_resolver.__code__
    issuance_decode_execution_plan = _issuance_module._decode_execution_plan
    issuance_decode_execution_plan_code = issuance_decode_execution_plan.__code__
    issuance_execution_action_cls = _issuance_module.ExecutionAction
    issuance_execution_action_init = issuance_execution_action_cls.__init__
    issuance_execution_action_init_code = issuance_execution_action_init.__code__
    issuance_execution_plan_cls = _issuance_module.ExecutionPlan
    issuance_execution_plan_init = issuance_execution_plan_cls.__init__
    issuance_execution_plan_init_code = issuance_execution_plan_init.__code__
    issuance_supervised_approval_cls = _issuance_module.SupervisedApproval
    issuance_supervised_approval_init = issuance_supervised_approval_cls.__init__
    issuance_supervised_approval_init_code = issuance_supervised_approval_init.__code__
    issuance_approval_state_cls = _issuance_module.ApprovalState
    issuance_profile_binding_cls = _issuance_module.ProfileBinding
    issuance_profile_binding_init = issuance_profile_binding_cls.__init__
    issuance_profile_binding_init_code = issuance_profile_binding_init.__code__
    issuance_constraint_cls = _issuance_module.ExecutionLegConstraint
    issuance_constraint_init = issuance_constraint_cls.__init__
    issuance_constraint_init_code = issuance_constraint_init.__code__
    issuance_bound_cls = _issuance_module.BoundSupervisedExecutionPlan
    issuance_bound_init = issuance_bound_cls.__init__
    issuance_bound_init_code = issuance_bound_init.__code__
    issuance_decimal_cls = _issuance_module.Decimal
    issuance_schema = _issuance_module._SCHEMA
    issuance_schema_version = _issuance_module._SCHEMA_VERSION
    issuance_authority_domain = _issuance_module._AUTHORITY_DOMAIN
    issuance_directory_name = _issuance_module._DIRECTORY
    issuance_root_keys = _issuance_module._ROOT_KEYS
    issuance_body_keys = _issuance_module._ISSUANCE_KEYS
    issuance_bound_keys = _issuance_module._BOUND_KEYS
    issuance_approval_keys = _issuance_module._APPROVAL_KEYS
    issuance_provider_request_keys = _issuance_module._PROVIDER_REQUEST_KEYS
    issuance_strict_json_loads = _issuance_module.strict_json_loads
    issuance_strict_json_loads_code = issuance_strict_json_loads.__code__
    issuance_durable_path_lock = _issuance_module.durable_path_lock
    issuance_durable_path_lock_code = issuance_durable_path_lock.__code__
    issuance_durable_path_lock_body = issuance_durable_path_lock.__wrapped__
    issuance_durable_path_lock_body_code = issuance_durable_path_lock_body.__code__
    issuance_hash_constructor = _issuance_module.hashlib.sha256
    issuance_workspace_lock_cls = _issuance_module.WorkspaceEconomicLock
    issuance_workspace_lock_init = issuance_workspace_lock_cls.__init__
    issuance_workspace_lock_init_code = issuance_workspace_lock_init.__code__
    issuance_workspace_lock_enter = issuance_workspace_lock_cls.__enter__
    issuance_workspace_lock_enter_code = issuance_workspace_lock_enter.__code__
    issuance_workspace_lock_exit = issuance_workspace_lock_cls.__exit__
    issuance_workspace_lock_exit_code = issuance_workspace_lock_exit.__code__
    issuance_workspace_lock_acquire = issuance_workspace_lock_cls.acquire
    issuance_workspace_lock_acquire_code = issuance_workspace_lock_acquire.__code__
    issuance_workspace_lock_release = issuance_workspace_lock_cls.release
    issuance_workspace_lock_release_code = issuance_workspace_lock_release.__code__
    integrity_durable_lock = _integrity_module.durable_path_lock
    integrity_durable_lock_body = integrity_durable_lock.__wrapped__
    integrity_resolved_key = _integrity_module._resolved_key
    integrity_resolved_key_code = integrity_resolved_key.__code__
    integrity_thread_lock_for = _integrity_module._thread_lock_for
    integrity_thread_lock_for_code = integrity_thread_lock_for.__code__
    integrity_lock_handle = _integrity_module._lock_handle
    integrity_lock_handle_code = integrity_lock_handle.__code__
    integrity_unlock_handle = _integrity_module._unlock_handle
    integrity_unlock_handle_code = integrity_unlock_handle.__code__
    workspace_lock_cls = _workspace_lock_module.WorkspaceEconomicLock
    workspace_lock_open = workspace_lock_cls._open_lock_handle
    workspace_lock_open_code = workspace_lock_open.__code__
    workspace_lock_validate_existing = workspace_lock_cls._validate_existing_lock_path
    workspace_lock_validate_existing_code = workspace_lock_validate_existing.__code__
    workspace_lock_validate_handle = workspace_lock_cls._validate_open_handle_identity
    workspace_lock_validate_handle_code = workspace_lock_validate_handle.__code__
    workspace_lock_lock_handle = workspace_lock_cls._lock_handle
    workspace_lock_lock_handle_code = workspace_lock_lock_handle.__code__
    workspace_lock_unlock_handle = workspace_lock_cls._unlock_handle
    workspace_lock_unlock_handle_code = workspace_lock_unlock_handle.__code__
    monotonic_cls = _issuance_module.MonotonicWorkspaceAuthority
    monotonic_init = monotonic_cls.__init__
    monotonic_init_code = monotonic_init.__code__
    monotonic_recover = monotonic_cls.recover
    monotonic_recover_code = monotonic_recover.__code__
    monotonic_load_bound_history = monotonic_cls._load_bound_history
    monotonic_load_bound_history_code = monotonic_load_bound_history.__code__
    monotonic_load_history = monotonic_cls._load_history
    monotonic_load_history_code = monotonic_load_history.__code__
    monotonic_decode_record = monotonic_cls._decode_record
    monotonic_decode_record_code = monotonic_decode_record.__code__
    monotonic_validate_namespace = monotonic_cls._validate_namespace_marker
    monotonic_validate_namespace_code = monotonic_validate_namespace.__code__
    monotonic_strict_json_loads = _monotonic_module.strict_json_loads
    monotonic_strict_json_loads_code = monotonic_strict_json_loads.__code__
    monotonic_record_hash = _monotonic_module._record_hash
    monotonic_record_hash_code = monotonic_record_hash.__code__
    monotonic_digest = _monotonic_module._digest
    monotonic_digest_code = monotonic_digest.__code__
    monotonic_text = _monotonic_module._text
    monotonic_text_code = monotonic_text.__code__
    monotonic_absolute_path = _monotonic_module._absolute_path
    monotonic_absolute_path_code = monotonic_absolute_path.__code__
    monotonic_resolve_root = _monotonic_module.resolve_monotonic_authority_root
    monotonic_resolve_root_code = monotonic_resolve_root.__code__
    monotonic_preflight_root = _monotonic_module.preflight_authority_root_selection
    monotonic_preflight_root_code = monotonic_preflight_root.__code__
    monotonic_workspace_binding_cls = _monotonic_module.WorkspaceIdentityBinding
    monotonic_workspace_resolve = monotonic_workspace_binding_cls.__dict__["resolve"].__func__
    monotonic_workspace_resolve_code = monotonic_workspace_resolve.__code__
    monotonic_root_binding_cls = _monotonic_module.AuthorityRootSelectionBinding
    monotonic_root_resolve = monotonic_root_binding_cls.__dict__["resolve"].__func__
    monotonic_root_resolve_code = monotonic_root_resolve.__code__
    monotonic_validate_root_selection = monotonic_cls._validate_authority_root_selection
    monotonic_validate_root_selection_code = monotonic_validate_root_selection.__code__
    monotonic_validate_root_activation = monotonic_cls._validate_authority_root_activation
    monotonic_validate_root_activation_code = monotonic_validate_root_activation.__code__
    monotonic_validate_workspace = monotonic_cls._validate_workspace_binding
    monotonic_validate_workspace_code = monotonic_validate_workspace.__code__
    monotonic_ensure_root_bound = monotonic_cls._ensure_authority_root_bound
    monotonic_ensure_root_bound_code = monotonic_ensure_root_bound.__code__
    monotonic_ensure_root_activated = monotonic_cls._ensure_authority_root_activated
    monotonic_ensure_root_activated_code = monotonic_ensure_root_activated.__code__
    monotonic_new_terminal = monotonic_cls._new_terminal_record
    monotonic_new_terminal_code = monotonic_new_terminal.__code__
    monotonic_append_record = monotonic_cls._append_record
    monotonic_append_record_code = monotonic_append_record.__code__
    monotonic_workspace_lock_cls = _monotonic_module.WorkspaceEconomicLock
    monotonic_hash_constructor = _monotonic_module.hashlib.sha256
    monotonic_authority_id = _monotonic_module.AUTHORITY_ID
    monotonic_authority_schema = _monotonic_module.AUTHORITY_SCHEMA
    monotonic_authority_schema_version = _monotonic_module.AUTHORITY_SCHEMA_VERSION
    monotonic_namespace_schema = _monotonic_module._NAMESPACE_SCHEMA
    monotonic_namespace_keys = _monotonic_module._NAMESPACE_MARKER_KEYS
    monotonic_record_keys = _monotonic_module._RECORD_KEYS
    monotonic_record_file_re = _monotonic_module._RECORD_FILE_RE
    strict_json_function = _json_integrity_module.strict_json_loads
    strict_json_function_code = strict_json_function.__code__
    strict_json_loader = _json_integrity_module.json.loads
    strict_json_loader_code = strict_json_loader.__code__
    strict_json_unique_object = _json_integrity_module._unique_json_object
    strict_json_unique_object_code = strict_json_unique_object.__code__
    strict_json_reject_constant = _json_integrity_module._reject_nonstandard_json_constant
    strict_json_reject_constant_code = strict_json_reject_constant.__code__
    strict_json_parse_integer = _json_integrity_module._parse_bounded_json_integer
    strict_json_parse_integer_code = strict_json_parse_integer.__code__
    strict_json_validate_value = _json_integrity_module._validate_strict_json_value
    strict_json_validate_value_code = strict_json_validate_value.__code__
    strict_json_integer_limit = _json_integrity_module._JSON_INTEGER_MAX_DIGITS
    ledger_saga = ledger_cls.saga
    ledger_saga_code = ledger_saga.__code__
    ledger_approval_active = ledger_cls.supervised_approval_is_active
    ledger_approval_active_code = ledger_approval_active.__code__
    ledger_events = ledger_cls._events
    ledger_events_code = ledger_events.__code__
    ledger_ensure_durable = ledger_cls._ensure_existing_path_durable
    ledger_ensure_durable_code = ledger_ensure_durable.__code__
    ledger_parse = ledger_cls._parse.__func__
    ledger_parse_code = ledger_parse.__code__
    ledger_validate_event = ledger_cls._validate_event.__func__
    ledger_validate_event_code = ledger_validate_event.__code__
    ledger_validate_semantics = ledger_cls._validate_semantics.__func__
    ledger_validate_semantics_code = ledger_validate_semantics.__code__
    ledger_json_loads = _ledger_module.json.loads
    ledger_json_loads_code = ledger_json_loads.__code__
    ledger_pairs = _ledger_module._pairs
    ledger_pairs_code = ledger_pairs.__code__
    ledger_nonfinite = _ledger_module._nonfinite
    ledger_nonfinite_code = ledger_nonfinite.__code__
    ledger_digest = _ledger_module._digest
    ledger_digest_code = ledger_digest.__code__
    ledger_timestamp = _ledger_module._timestamp
    ledger_timestamp_code = ledger_timestamp.__code__
    ledger_validate_json = _ledger_module._validate_json
    ledger_validate_json_code = ledger_validate_json.__code__

    def require_canonical_resolver_authority() -> None:
        if (
            _price_bound_module.resolve_betfair_standard_limit_price_bound is not resolver
            or resolver.__code__ is not resolver_code
        ):
            raise error_cls(
                "canonical Betfair price-bound resolver authority changed"
            )
        for label, name, function, code in helper_witnesses:
            if (
                getattr(_price_bound_module, name, None) is not function
                or function.__code__ is not code
            ):
                raise error_cls(
                    f"canonical Betfair price-bound {label} authority changed"
                )
        if (
            _price_bound_module._CANONICAL_PLACE_ACTION is not captured_place_action
            or captured_place_action.__code__ is not captured_place_action_code
            or _price_bound_module.BetfairSupervisedPlaceOrdersClient.place_action
            is not captured_place_action
        ):
            raise error_cls(
                "canonical Betfair placeOrders writer authority changed"
            )
        if (
            ExecutionAction.to_dict is not execution_to_dict
            or execution_to_dict.__code__ is not execution_to_dict_code
            or BoundSupervisedExecutionPlan.verify_binding is not bound_verify
            or bound_verify.__code__ is not bound_verify_code
            or BoundSupervisedExecutionPlan.action_for is not bound_action_for
            or bound_action_for.__code__ is not bound_action_for_code
            or _supervised_module._bound_binding_sha256 is not bound_binding_helper
            or bound_binding_helper.__code__ is not bound_binding_helper_code
            or _supervised_module._digest is not supervised_digest
            or supervised_digest.__code__ is not supervised_digest_code
            or _supervised_module.json.dumps is not supervised_json_dumps
            or supervised_json_dumps.__code__ is not supervised_json_dumps_code
            or _supervised_module.hashlib.sha256 is not supervised_hash_constructor
            or _supervised_module.ExecutionPlan.to_dict is not execution_plan_to_dict
            or execution_plan_to_dict.__code__ is not execution_plan_to_dict_code
            or _supervised_module.ExecutionPlan.fingerprint.fget is not execution_plan_fingerprint_getter
            or execution_plan_fingerprint_getter.__code__ is not execution_plan_fingerprint_getter_code
            or _supervised_module.ExecutionLegConstraint.to_dict is not constraint_to_dict
            or constraint_to_dict.__code__ is not constraint_to_dict_code
            or _supervised_module.SupervisedApproval.fingerprint.fget is not approval_fingerprint_getter
            or approval_fingerprint_getter.__code__ is not approval_fingerprint_getter_code
            or _supervised_module.SupervisedApproval.ledger_identity.fget is not approval_ledger_identity_getter
            or approval_ledger_identity_getter.__code__ is not approval_ledger_identity_getter_code
        ):
            raise error_cls(
                "canonical Betfair bound-plan dependency authority changed"
            )

    def require_verifier_dependency_authority() -> None:
        if (
            issuance_store_cls.load is not issuance_load
            or issuance_load.__code__ is not issuance_load_code
            or issuance_store_cls._load_locked is not issuance_load_locked
            or issuance_load_locked.__code__ is not issuance_load_locked_code
            or issuance_store_cls._authority is not issuance_authority
            or issuance_authority.__code__ is not issuance_authority_code
            or issuance_store_cls._path is not issuance_path
            or issuance_path.__code__ is not issuance_path_code
            or _issuance_module._plan_file_name is not issuance_plan_file_name
            or issuance_plan_file_name.__code__ is not issuance_plan_file_name_code
            or _issuance_module._exact_keys is not issuance_exact_keys
            or issuance_exact_keys.__code__ is not issuance_exact_keys_code
            or _issuance_module._text is not issuance_text
            or issuance_text.__code__ is not issuance_text_code
            or _issuance_module._sha is not issuance_sha
            or issuance_sha.__code__ is not issuance_sha_code
            or _issuance_module._digest is not issuance_digest
            or issuance_digest.__code__ is not issuance_digest_code
            or _issuance_module._decode_approval is not issuance_decode_approval
            or issuance_decode_approval.__code__ is not issuance_decode_approval_code
            or _issuance_module._decode_bound is not issuance_decode_bound
            or issuance_decode_bound.__code__ is not issuance_decode_bound_code
            or _issuance_module._provider_request_payload is not issuance_provider_request
            or issuance_provider_request.__code__ is not issuance_provider_request_code
            or _issuance_module.resolve_betfair_standard_limit_price_bound is not issuance_provider_resolver
            or issuance_provider_resolver.__code__ is not issuance_provider_resolver_code
            or _issuance_module._decode_execution_plan is not issuance_decode_execution_plan
            or issuance_decode_execution_plan.__code__ is not issuance_decode_execution_plan_code
            or _issuance_module.IssuedSupervisedPlan is not issued_plan_cls
            or issued_plan_cls.__init__ is not issued_plan_init
            or issued_plan_init.__code__ is not issued_plan_init_code
            or _issuance_module.ExecutionAction is not issuance_execution_action_cls
            or issuance_execution_action_cls.__init__ is not issuance_execution_action_init
            or issuance_execution_action_init.__code__ is not issuance_execution_action_init_code
            or _issuance_module.ExecutionPlan is not issuance_execution_plan_cls
            or issuance_execution_plan_cls.__init__ is not issuance_execution_plan_init
            or issuance_execution_plan_init.__code__ is not issuance_execution_plan_init_code
            or _issuance_module.SupervisedApproval is not issuance_supervised_approval_cls
            or issuance_supervised_approval_cls.__init__ is not issuance_supervised_approval_init
            or issuance_supervised_approval_init.__code__ is not issuance_supervised_approval_init_code
            or _issuance_module.ApprovalState is not issuance_approval_state_cls
            or _issuance_module.ProfileBinding is not issuance_profile_binding_cls
            or issuance_profile_binding_cls.__init__ is not issuance_profile_binding_init
            or issuance_profile_binding_init.__code__ is not issuance_profile_binding_init_code
            or _issuance_module.ExecutionLegConstraint is not issuance_constraint_cls
            or issuance_constraint_cls.__init__ is not issuance_constraint_init
            or issuance_constraint_init.__code__ is not issuance_constraint_init_code
            or _issuance_module.BoundSupervisedExecutionPlan is not issuance_bound_cls
            or issuance_bound_cls.__init__ is not issuance_bound_init
            or issuance_bound_init.__code__ is not issuance_bound_init_code
            or _issuance_module.Decimal is not issuance_decimal_cls
            or _issuance_module._SCHEMA != issuance_schema
            or _issuance_module._SCHEMA_VERSION != issuance_schema_version
            or _issuance_module._AUTHORITY_DOMAIN != issuance_authority_domain
            or _issuance_module._DIRECTORY != issuance_directory_name
            or _issuance_module._ROOT_KEYS is not issuance_root_keys
            or _issuance_module._ISSUANCE_KEYS is not issuance_body_keys
            or _issuance_module._BOUND_KEYS is not issuance_bound_keys
            or _issuance_module._APPROVAL_KEYS is not issuance_approval_keys
            or _issuance_module._PROVIDER_REQUEST_KEYS is not issuance_provider_request_keys
            or _issuance_module.strict_json_loads is not issuance_strict_json_loads
            or issuance_strict_json_loads.__code__ is not issuance_strict_json_loads_code
            or _issuance_module.durable_path_lock is not issuance_durable_path_lock
            or issuance_durable_path_lock.__code__ is not issuance_durable_path_lock_code
            or issuance_durable_path_lock.__wrapped__ is not issuance_durable_path_lock_body
            or issuance_durable_path_lock_body.__code__ is not issuance_durable_path_lock_body_code
            or _issuance_module.hashlib.sha256 is not issuance_hash_constructor
            or _issuance_module.WorkspaceEconomicLock is not issuance_workspace_lock_cls
            or issuance_workspace_lock_cls.__init__ is not issuance_workspace_lock_init
            or issuance_workspace_lock_init.__code__ is not issuance_workspace_lock_init_code
            or issuance_workspace_lock_cls.__enter__ is not issuance_workspace_lock_enter
            or issuance_workspace_lock_enter.__code__ is not issuance_workspace_lock_enter_code
            or issuance_workspace_lock_cls.__exit__ is not issuance_workspace_lock_exit
            or issuance_workspace_lock_exit.__code__ is not issuance_workspace_lock_exit_code
            or issuance_workspace_lock_cls.acquire is not issuance_workspace_lock_acquire
            or issuance_workspace_lock_acquire.__code__ is not issuance_workspace_lock_acquire_code
            or issuance_workspace_lock_cls.release is not issuance_workspace_lock_release
            or issuance_workspace_lock_release.__code__ is not issuance_workspace_lock_release_code
            or _integrity_module.durable_path_lock is not integrity_durable_lock
            or integrity_durable_lock.__wrapped__ is not integrity_durable_lock_body
            or _integrity_module._resolved_key is not integrity_resolved_key
            or integrity_resolved_key.__code__ is not integrity_resolved_key_code
            or _integrity_module._thread_lock_for is not integrity_thread_lock_for
            or integrity_thread_lock_for.__code__ is not integrity_thread_lock_for_code
            or _integrity_module._lock_handle is not integrity_lock_handle
            or integrity_lock_handle.__code__ is not integrity_lock_handle_code
            or _integrity_module._unlock_handle is not integrity_unlock_handle
            or integrity_unlock_handle.__code__ is not integrity_unlock_handle_code
            or _workspace_lock_module.WorkspaceEconomicLock is not workspace_lock_cls
            or workspace_lock_cls is not issuance_workspace_lock_cls
            or workspace_lock_cls._open_lock_handle is not workspace_lock_open
            or workspace_lock_open.__code__ is not workspace_lock_open_code
            or workspace_lock_cls._validate_existing_lock_path is not workspace_lock_validate_existing
            or workspace_lock_validate_existing.__code__ is not workspace_lock_validate_existing_code
            or workspace_lock_cls._validate_open_handle_identity is not workspace_lock_validate_handle
            or workspace_lock_validate_handle.__code__ is not workspace_lock_validate_handle_code
            or workspace_lock_cls._lock_handle is not workspace_lock_lock_handle
            or workspace_lock_lock_handle.__code__ is not workspace_lock_lock_handle_code
            or workspace_lock_cls._unlock_handle is not workspace_lock_unlock_handle
            or workspace_lock_unlock_handle.__code__ is not workspace_lock_unlock_handle_code
            or _issuance_module.MonotonicWorkspaceAuthority is not monotonic_cls
            or monotonic_cls.__init__ is not monotonic_init
            or monotonic_init.__code__ is not monotonic_init_code
            or monotonic_cls.recover is not monotonic_recover
            or monotonic_recover.__code__ is not monotonic_recover_code
            or monotonic_cls._load_bound_history is not monotonic_load_bound_history
            or monotonic_load_bound_history.__code__ is not monotonic_load_bound_history_code
            or monotonic_cls._load_history is not monotonic_load_history
            or monotonic_load_history.__code__ is not monotonic_load_history_code
            or monotonic_cls._decode_record is not monotonic_decode_record
            or monotonic_decode_record.__code__ is not monotonic_decode_record_code
            or monotonic_cls._validate_namespace_marker is not monotonic_validate_namespace
            or monotonic_validate_namespace.__code__ is not monotonic_validate_namespace_code
            or _monotonic_module.strict_json_loads is not monotonic_strict_json_loads
            or monotonic_strict_json_loads.__code__ is not monotonic_strict_json_loads_code
            or _monotonic_module._record_hash is not monotonic_record_hash
            or monotonic_record_hash.__code__ is not monotonic_record_hash_code
            or _monotonic_module._digest is not monotonic_digest
            or monotonic_digest.__code__ is not monotonic_digest_code
            or _monotonic_module._text is not monotonic_text
            or monotonic_text.__code__ is not monotonic_text_code
            or _monotonic_module._absolute_path is not monotonic_absolute_path
            or monotonic_absolute_path.__code__ is not monotonic_absolute_path_code
            or _monotonic_module.resolve_monotonic_authority_root is not monotonic_resolve_root
            or monotonic_resolve_root.__code__ is not monotonic_resolve_root_code
            or _monotonic_module.preflight_authority_root_selection is not monotonic_preflight_root
            or monotonic_preflight_root.__code__ is not monotonic_preflight_root_code
            or _monotonic_module.WorkspaceIdentityBinding is not monotonic_workspace_binding_cls
            or monotonic_workspace_binding_cls.__dict__["resolve"].__func__ is not monotonic_workspace_resolve
            or monotonic_workspace_resolve.__code__ is not monotonic_workspace_resolve_code
            or _monotonic_module.AuthorityRootSelectionBinding is not monotonic_root_binding_cls
            or monotonic_root_binding_cls.__dict__["resolve"].__func__ is not monotonic_root_resolve
            or monotonic_root_resolve.__code__ is not monotonic_root_resolve_code
            or monotonic_cls._validate_authority_root_selection is not monotonic_validate_root_selection
            or monotonic_validate_root_selection.__code__ is not monotonic_validate_root_selection_code
            or monotonic_cls._validate_authority_root_activation is not monotonic_validate_root_activation
            or monotonic_validate_root_activation.__code__ is not monotonic_validate_root_activation_code
            or monotonic_cls._validate_workspace_binding is not monotonic_validate_workspace
            or monotonic_validate_workspace.__code__ is not monotonic_validate_workspace_code
            or monotonic_cls._ensure_authority_root_bound is not monotonic_ensure_root_bound
            or monotonic_ensure_root_bound.__code__ is not monotonic_ensure_root_bound_code
            or monotonic_cls._ensure_authority_root_activated is not monotonic_ensure_root_activated
            or monotonic_ensure_root_activated.__code__ is not monotonic_ensure_root_activated_code
            or monotonic_cls._new_terminal_record is not monotonic_new_terminal
            or monotonic_new_terminal.__code__ is not monotonic_new_terminal_code
            or monotonic_cls._append_record is not monotonic_append_record
            or monotonic_append_record.__code__ is not monotonic_append_record_code
            or _monotonic_module.WorkspaceEconomicLock is not monotonic_workspace_lock_cls
            or _monotonic_module.hashlib.sha256 is not monotonic_hash_constructor
            or _monotonic_module.AUTHORITY_ID != monotonic_authority_id
            or _monotonic_module.AUTHORITY_SCHEMA != monotonic_authority_schema
            or _monotonic_module.AUTHORITY_SCHEMA_VERSION != monotonic_authority_schema_version
            or _monotonic_module._NAMESPACE_SCHEMA != monotonic_namespace_schema
            or _monotonic_module._NAMESPACE_MARKER_KEYS is not monotonic_namespace_keys
            or _monotonic_module._RECORD_KEYS is not monotonic_record_keys
            or _monotonic_module._RECORD_FILE_RE is not monotonic_record_file_re
            or _json_integrity_module.strict_json_loads is not strict_json_function
            or strict_json_function.__code__ is not strict_json_function_code
            or _json_integrity_module.json.loads is not strict_json_loader
            or strict_json_loader.__code__ is not strict_json_loader_code
            or _json_integrity_module._unique_json_object is not strict_json_unique_object
            or strict_json_unique_object.__code__ is not strict_json_unique_object_code
            or _json_integrity_module._reject_nonstandard_json_constant is not strict_json_reject_constant
            or strict_json_reject_constant.__code__ is not strict_json_reject_constant_code
            or _json_integrity_module._parse_bounded_json_integer is not strict_json_parse_integer
            or strict_json_parse_integer.__code__ is not strict_json_parse_integer_code
            or _json_integrity_module._validate_strict_json_value is not strict_json_validate_value
            or strict_json_validate_value.__code__ is not strict_json_validate_value_code
            or _json_integrity_module._JSON_INTEGER_MAX_DIGITS != strict_json_integer_limit
            or ledger_cls.saga is not ledger_saga
            or ledger_saga.__code__ is not ledger_saga_code
            or ledger_cls.supervised_approval_is_active is not ledger_approval_active
            or ledger_approval_active.__code__ is not ledger_approval_active_code
            or ledger_cls._events is not ledger_events
            or ledger_events.__code__ is not ledger_events_code
            or ledger_cls._ensure_existing_path_durable is not ledger_ensure_durable
            or ledger_ensure_durable.__code__ is not ledger_ensure_durable_code
            or ledger_cls._parse.__func__ is not ledger_parse
            or ledger_parse.__code__ is not ledger_parse_code
            or ledger_cls._validate_event.__func__ is not ledger_validate_event
            or ledger_validate_event.__code__ is not ledger_validate_event_code
            or ledger_cls._validate_semantics.__func__ is not ledger_validate_semantics
            or ledger_validate_semantics.__code__ is not ledger_validate_semantics_code
            or _ledger_module.json.loads is not ledger_json_loads
            or ledger_json_loads.__code__ is not ledger_json_loads_code
            or _ledger_module._pairs is not ledger_pairs
            or ledger_pairs.__code__ is not ledger_pairs_code
            or _ledger_module._nonfinite is not ledger_nonfinite
            or ledger_nonfinite.__code__ is not ledger_nonfinite_code
            or _ledger_module._digest is not ledger_digest
            or ledger_digest.__code__ is not ledger_digest_code
            or _ledger_module._timestamp is not ledger_timestamp
            or ledger_timestamp.__code__ is not ledger_timestamp_code
            or _ledger_module._validate_json is not ledger_validate_json
            or ledger_validate_json.__code__ is not ledger_validate_json_code
        ):
            raise error_cls(
                "canonical Betfair verifier dependency authority changed"
            )

    def exact_snapshot(
        evidence: BetfairStandardLimitPriceBoundEvidence,
    ) -> tuple[tuple[str, type[object], object], ...]:
        if type(evidence) is not evidence_cls:
            raise error_cls(
                "price-bound evidence must be the exact canonical evidence type"
            )
        snapshot: list[tuple[str, type[object], object]] = []
        for field in evidence_fields:
            try:
                value = object.__getattribute__(evidence, field)
            except (AttributeError, TypeError) as exc:
                raise error_cls("price-bound evidence is incomplete") from exc
            comparable: object = str(value) if type(value) is decimal_cls else value
            snapshot.append((field, type(value), comparable))
        return tuple(snapshot)

    def require_execution_state_continuity(
        *,
        ledger: RealExecutionLedger,
        bound: BoundSupervisedExecutionPlan,
    ) -> None:
        if type(ledger) is not ledger_cls:
            raise error_cls(
                "ledger must be the exact canonical RealExecutionLedger type"
            )
        if hasattr(ledger, "__dict__") and any(
            name in vars(ledger)
            for name in (
                "saga",
                "supervised_approval_is_active",
                "_events",
                "_ensure_existing_path_durable",
                "_parse",
            )
        ):
            raise error_cls("ledger authority method shadow is not allowed")
        if type(bound) is not bound_cls:
            raise error_cls(
                "issued bound must be the exact canonical BoundSupervisedExecutionPlan type"
            )
        require_verifier_dependency_authority()
        bound_verify(bound)
        try:
            saga = ledger_saga(ledger, bound.execution_plan.plan_id)
        except KeyError as exc:
            raise error_cls(
                "product-issued execution plan is not durably reserved"
            ) from exc
        if saga.plan_fingerprint != bound.execution_plan.fingerprint:
            raise error_cls(
                "durable execution-plan fingerprint mismatches product issuance"
            )
        if not ledger_approval_active(
            ledger,
            plan_id=bound.execution_plan.plan_id,
            approval_id=bound.execution_plan.approval_id,
            approval_fingerprint=bound.approval_fingerprint,
        ):
            raise error_cls(
                "durable supervised approval is missing or revoked"
            )
        require_verifier_dependency_authority()

    def require_issuance_time_provider_request(
        *,
        issued_requests: tuple[dict[str, object], ...],
        expected: BetfairStandardLimitPriceBoundEvidence,
    ) -> None:
        matches = [
            item
            for item in issued_requests
            if item.get("action_id") == object.__getattribute__(expected, "action_id")
        ]
        if len(matches) != 1:
            raise error_cls(
                "Betfair request identity was not durably proven at plan issuance"
            )
        durable = matches[0]
        expected_request = {
            "action_id": object.__getattribute__(expected, "action_id"),
            "bookmaker_id": object.__getattribute__(expected, "bookmaker_id"),
            "account_id": object.__getattribute__(expected, "account_id"),
            "instruction_sha256": object.__getattribute__(expected, "instruction_sha256"),
            "write_adapter_id": object.__getattribute__(expected, "write_adapter_id"),
            "write_adapter_version": object.__getattribute__(expected, "write_adapter_version"),
        }
        if durable != expected_request:
            raise error_cls(
                "durable issuance-time Betfair request identity changed"
            )

    def verify_betfair_standard_limit_price_bound(
        *,
        evidence: BetfairStandardLimitPriceBoundEvidence,
        ledger: RealExecutionLedger,
        issuance_store: SupervisedPlanIssuanceStore,
        execution_plan_id: str,
        action_id: str,
    ) -> BetfairStandardLimitPriceBoundEvidence:
        """Accept candidate evidence only by durable product issuance re-resolution.

        The caller supplies only identities, never a bound execution DTO. The exact
        bound+approval unit is reloaded from the rollback-resistant product issuance
        authority.  Provider-request evidence is then re-resolved through the exact
        resolver/writer graph captured by this verifier, not through a mutable public
        resolver binding.  This makes coordinated pre-issuance/verifier rebinding
        fail closed instead of turning common-mode agreement into provider authority.
        """

        if type(issuance_store) is not issuance_store_cls:
            raise error_cls(
                "issuance_store must be the exact canonical SupervisedPlanIssuanceStore type"
            )
        if hasattr(issuance_store, "__dict__") and any(
            name in vars(issuance_store)
            for name in ("load", "_load_locked", "_authority", "_path")
        ):
            raise error_cls(
                "issuance_store method shadow is not allowed"
            )

        # Fail before durable issuance reload can re-resolve provider-request
        # identity through the shared canonical resolver object. The verifier's
        # own continuity/snapshot helpers are closure-local so later module-global
        # rebinding cannot remove provenance or approval gates.
        require_canonical_resolver_authority()
        require_verifier_dependency_authority()
        try:
            issued = issuance_load(
                issuance_store,
                execution_plan_id,
            )
        except issuance_error_cls as exc:
            raise error_cls(
                "durable product supervised-plan issuance is missing or invalid"
            ) from exc
        require_verifier_dependency_authority()
        if type(issued) is not issued_plan_cls:
            raise error_cls(
                "durable product supervised-plan issuance returned a non-canonical record"
            )
        bound = object.__getattribute__(issued, "bound")
        require_execution_state_continuity(ledger=ledger, bound=bound)

        require_canonical_resolver_authority()
        require_verifier_dependency_authority()
        expected = resolver(
            bound=bound,
            action_id=action_id,
        )
        require_canonical_resolver_authority()
        require_verifier_dependency_authority()

        require_issuance_time_provider_request(
            issued_requests=object.__getattribute__(issued, "provider_requests"),
            expected=expected,
        )
        if exact_snapshot(evidence) != exact_snapshot(expected):
            raise error_cls(
                "price-bound evidence does not match fresh canonical re-resolution"
            )
        require_verifier_dependency_authority()
        return expected

    verify_betfair_standard_limit_price_bound.__name__ = (
        "verify_betfair_standard_limit_price_bound"
    )
    verify_betfair_standard_limit_price_bound.__qualname__ = (
        "verify_betfair_standard_limit_price_bound"
    )
    return verify_betfair_standard_limit_price_bound


verify_betfair_standard_limit_price_bound = _build_product_verifier()
del _build_product_verifier
