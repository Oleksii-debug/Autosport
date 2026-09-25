"""Seal product PolicyEvaluation issuance dispatch and materialization time.

The #762 issuer is intentionally a composition layer over the canonical scientific
registry and factory artifact store. Positive issuance must therefore depend on
exact product-owned helper dispatch and on a product-owned UTC materialization
clock, not on caller-rebound module globals or a same-class constructor mutation.

This module adds no registry, artifact store, evaluator, or promotion authority.
It only fail-closes the existing issuer's direct dispatch graph and replaces the
newly-opened canonical store's default clock with a state-owned trusted UTC clock.
"""
from __future__ import annotations

import marshal
from datetime import datetime as _datetime_type
from datetime import timezone as _timezone_type

from . import external_validity_policy_issuance as _issuance
from . import strategy_model_factory as _factory_module


class _DispatchState:
    """Keep predecessor entrypoints behind guard-enforcing methods.

    Public wrapper closure cells intentionally capture only this sealed state
    object and guard-enforcing bound methods, never an unguarded predecessor
    FunctionType. The bound methods and their internal guard callables are pinned
    before publication so later class-method rebinding cannot redirect dispatch.
    """

    __slots__ = (
        "_issuance_module",
        "_factory_module",
        "_marshal_dumps",
        "_error_type",
        "__original_open",
        "__original_issue",
        "__original_resolve",
        "__original_verify",
        "_store_type",
        "_store_init",
        "_store_init_globals",
        "_store_init_code",
        "_factory_datetime",
        "_factory_timezone",
        "_target",
        "_issuance_id",
        "_require_source",
        "_derive",
        "_registry_get",
        "_store_read",
        "_store_materialize",
        "_store_receipt",
        "_registry_append",
        "_canonical_bundle_sha256",
        "_workspace_type",
        "_ref_type",
        "_policy_evaluation_type",
        "_protocol_type",
        "_baseline_kind_type",
        "_bundle_type",
        "_result_artifact_kind",
        "_bundle_id_prefix",
        "_issuer_source_sha256",
        "_trusted_datetime",
        "_trusted_utc",
        "_public_open",
        "_public_issue",
        "_public_resolve",
        "_public_verify",
        "_constructor_guard",
        "_common_guard",
        "_trusted_clock_callable",
        "_sealed",
    )

    def __init__(self) -> None:
        object.__setattr__(self, "_sealed", False)
        issuance_module = _issuance
        factory_module = _factory_module
        marshal_dumps = marshal.dumps
        object.__setattr__(self, "_issuance_module", issuance_module)
        object.__setattr__(self, "_factory_module", factory_module)
        object.__setattr__(self, "_marshal_dumps", marshal_dumps)
        object.__setattr__(
            self,
            "_error_type",
            issuance_module.ProductPolicyEvaluationIssuanceError,
        )
        object.__setattr__(
            self,
            "_DispatchState__original_open",
            issuance_module._open_canonical_authorities,
        )
        object.__setattr__(
            self,
            "_DispatchState__original_issue",
            issuance_module.issue_product_policy_evaluation,
        )
        object.__setattr__(
            self,
            "_DispatchState__original_resolve",
            issuance_module.resolve_product_policy_evaluation,
        )
        object.__setattr__(
            self,
            "_DispatchState__original_verify",
            issuance_module.verify_product_policy_evaluation,
        )

        store_type = issuance_module._STORE_TYPE
        store_init = store_type.__init__
        object.__setattr__(self, "_store_type", store_type)
        object.__setattr__(self, "_store_init", store_init)
        object.__setattr__(self, "_store_init_globals", store_init.__globals__)
        object.__setattr__(self, "_store_init_code", marshal_dumps(store_init.__code__))
        object.__setattr__(self, "_factory_datetime", factory_module.datetime)
        object.__setattr__(self, "_factory_timezone", factory_module.timezone)

        object.__setattr__(self, "_target", issuance_module._target)
        object.__setattr__(self, "_issuance_id", issuance_module._issuance_id)
        object.__setattr__(
            self,
            "_require_source",
            issuance_module._require_source_factory_evaluation,
        )
        object.__setattr__(self, "_derive", issuance_module._derive_policy_evaluation)
        object.__setattr__(self, "_registry_get", issuance_module._registry_get)
        object.__setattr__(self, "_store_read", issuance_module._store_read)
        object.__setattr__(self, "_store_materialize", issuance_module._STORE_MATERIALIZE)
        object.__setattr__(self, "_store_receipt", issuance_module._STORE_RECEIPT)
        object.__setattr__(self, "_registry_append", issuance_module._REGISTRY_APPEND)
        object.__setattr__(
            self,
            "_canonical_bundle_sha256",
            issuance_module.canonical_product_policy_evaluation_bundle_sha256,
        )

        object.__setattr__(
            self,
            "_workspace_type",
            issuance_module.ProductPolicyEvaluationWorkspace,
        )
        object.__setattr__(self, "_ref_type", issuance_module.IssuedPolicyEvaluationRef)
        object.__setattr__(self, "_policy_evaluation_type", issuance_module.PolicyEvaluation)
        object.__setattr__(self, "_protocol_type", issuance_module.FrozenBaselineProtocol)
        object.__setattr__(self, "_baseline_kind_type", issuance_module.BaselineKind)
        object.__setattr__(self, "_bundle_type", issuance_module.EvaluationBundleRef)

        object.__setattr__(self, "_result_artifact_kind", issuance_module._RESULT_ARTIFACT_KIND)
        object.__setattr__(self, "_bundle_id_prefix", issuance_module._BUNDLE_ID_PREFIX)
        object.__setattr__(self, "_issuer_source_sha256", issuance_module._ISSUER_SOURCE_SHA256)

        object.__setattr__(self, "_trusted_datetime", _datetime_type)
        object.__setattr__(self, "_trusted_utc", _timezone_type.utc)

        object.__setattr__(self, "_public_open", None)
        object.__setattr__(self, "_public_issue", None)
        object.__setattr__(self, "_public_resolve", None)
        object.__setattr__(self, "_public_verify", None)

        # Pin bound guard methods before the state becomes reachable through any
        # published wrapper. Later monkeypatching of _DispatchState methods must
        # not redirect a positive authority entrypoint or its internal checks.
        object.__setattr__(
            self,
            "_constructor_guard",
            object.__getattribute__(self, "_require_store_constructor_authority"),
        )
        object.__setattr__(
            self,
            "_common_guard",
            object.__getattribute__(self, "_require_common_dispatch"),
        )
        object.__setattr__(
            self,
            "_trusted_clock_callable",
            object.__getattribute__(self, "trusted_clock"),
        )

    def __getattribute__(self, name: str):
        if name.startswith("_DispatchState__original_"):
            raise AttributeError("unguarded predecessor entrypoints are not exposed")
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value) -> None:
        if object.__getattribute__(self, "_sealed"):
            raise AttributeError("policy issuance dispatch state is sealed")
        object.__setattr__(self, name, value)

    def bind_public(self, open_fn, issue_fn, resolve_fn, verify_fn) -> None:
        if object.__getattribute__(self, "_sealed"):
            raise RuntimeError("policy issuance dispatch state is already sealed")
        object.__setattr__(self, "_public_open", open_fn)
        object.__setattr__(self, "_public_issue", issue_fn)
        object.__setattr__(self, "_public_resolve", resolve_fn)
        object.__setattr__(self, "_public_verify", verify_fn)
        object.__setattr__(self, "_sealed", True)

    def _require_store_constructor_authority(self) -> None:
        issuance_module = self._issuance_module
        factory_module = self._factory_module
        candidate_init = self._store_type.__init__
        candidate_code = getattr(candidate_init, "__code__", None)
        if (
            factory_module.FactoryArtifactStore is not self._store_type
            or issuance_module.FactoryArtifactStore is not self._store_type
            or candidate_init is not self._store_init
            or getattr(candidate_init, "__globals__", None)
            is not self._store_init_globals
            or candidate_code is None
            or self._marshal_dumps(candidate_code) != self._store_init_code
            or factory_module.datetime is not self._factory_datetime
            or factory_module.timezone is not self._factory_timezone
            or self._store_init_globals.get("datetime") is not self._factory_datetime
            or self._store_init_globals.get("timezone") is not self._factory_timezone
        ):
            raise self._error_type(
                "product PolicyEvaluation issuance authority was rebound: "
                "FactoryArtifactStore constructor/clock dispatch"
            )

    def trusted_clock(self):
        return self._trusted_datetime.now(self._trusted_utc)

    def open(self, authority):
        constructor_guard = object.__getattribute__(self, "_constructor_guard")
        constructor_guard()
        original_open = object.__getattribute__(self, "_DispatchState__original_open")
        registry, store = original_open(authority)
        constructor_guard()
        if type(store) is not self._store_type:
            raise self._error_type(
                "product PolicyEvaluation issuance authority was rebound: "
                "canonical artifact store type"
            )
        # The owning constructor publicly supports an injected test clock. The
        # product issuer never accepts one: replace its default source before any
        # result materialization can occur.
        store._clock = object.__getattribute__(self, "_trusted_clock_callable")
        return registry, store

    def _require_common_dispatch(self) -> None:
        issuance_module = self._issuance_module
        if (
            issuance_module._open_canonical_authorities is not self._public_open
            or issuance_module.issue_product_policy_evaluation is not self._public_issue
            or issuance_module.resolve_product_policy_evaluation is not self._public_resolve
            or issuance_module.verify_product_policy_evaluation is not self._public_verify
            or issuance_module._target is not self._target
            or issuance_module._issuance_id is not self._issuance_id
            or issuance_module._require_source_factory_evaluation is not self._require_source
            or issuance_module._derive_policy_evaluation is not self._derive
            or issuance_module._registry_get is not self._registry_get
            or issuance_module._store_read is not self._store_read
            or issuance_module._STORE_MATERIALIZE is not self._store_materialize
            or issuance_module._STORE_RECEIPT is not self._store_receipt
            or issuance_module._REGISTRY_APPEND is not self._registry_append
            or issuance_module.canonical_product_policy_evaluation_bundle_sha256
            is not self._canonical_bundle_sha256
            or issuance_module.ProductPolicyEvaluationWorkspace is not self._workspace_type
            or issuance_module.IssuedPolicyEvaluationRef is not self._ref_type
            or issuance_module.PolicyEvaluation is not self._policy_evaluation_type
            or issuance_module.FrozenBaselineProtocol is not self._protocol_type
            or issuance_module.BaselineKind is not self._baseline_kind_type
            or issuance_module.EvaluationBundleRef is not self._bundle_type
            or issuance_module._RESULT_ARTIFACT_KIND != self._result_artifact_kind
            or issuance_module._BUNDLE_ID_PREFIX != self._bundle_id_prefix
            or issuance_module._ISSUER_SOURCE_SHA256 != self._issuer_source_sha256
        ):
            raise self._error_type(
                "product PolicyEvaluation issuance authority was rebound: "
                "direct helper graph"
            )
        object.__getattribute__(self, "_constructor_guard")()

    def issue(
        self,
        authority,
        protocol,
        *,
        source_evaluation_bundle_id: str,
        baseline_kind=None,
    ):
        common_guard = object.__getattribute__(self, "_common_guard")
        common_guard()
        original_issue = object.__getattribute__(self, "_DispatchState__original_issue")
        result = original_issue(
            authority,
            protocol,
            source_evaluation_bundle_id=source_evaluation_bundle_id,
            baseline_kind=baseline_kind,
        )
        common_guard()
        return result

    def resolve(self, authority, protocol, reference):
        common_guard = object.__getattribute__(self, "_common_guard")
        common_guard()
        original_resolve = object.__getattribute__(self, "_DispatchState__original_resolve")
        result = original_resolve(authority, protocol, reference)
        common_guard()
        return result

    def verify(self, authority, protocol, reference, claimed):
        common_guard = object.__getattribute__(self, "_common_guard")
        common_guard()
        original_verify = object.__getattribute__(self, "_DispatchState__original_verify")
        result = original_verify(authority, protocol, reference, claimed)
        common_guard()
        return result


def _build_dispatch_guards():
    state = _DispatchState()
    state_type = type(state)
    error_type = object.__getattribute__(state, "_error_type")
    state_getattribute = state_type.__getattribute__
    state_setattr = state_type.__setattr__
    state_bind_public = state_type.bind_public
    state_constructor_guard = state_type._require_store_constructor_authority
    state_common_guard = state_type._require_common_dispatch
    state_trusted_clock = state_type.trusted_clock
    state_open = state_type.open
    state_issue = state_type.issue
    state_resolve = state_type.resolve
    state_verify = state_type.verify

    open_dispatch = state.open
    issue_dispatch = state.issue
    resolve_dispatch = state.resolve
    verify_dispatch = state.verify

    def require_state_class_authority() -> None:
        if (
            type(state) is not state_type
            or state_type.__getattribute__ is not state_getattribute
            or state_type.__setattr__ is not state_setattr
            or state_type.bind_public is not state_bind_public
            or state_type._require_store_constructor_authority
            is not state_constructor_guard
            or state_type._require_common_dispatch is not state_common_guard
            or state_type.trusted_clock is not state_trusted_clock
            or state_type.open is not state_open
            or state_type.issue is not state_issue
            or state_type.resolve is not state_resolve
            or state_type.verify is not state_verify
        ):
            raise error_type(
                "product PolicyEvaluation issuance authority was rebound: "
                "dispatch state class"
            )

    def guarded_open(authority: ProductPolicyEvaluationWorkspace):
        require_state_class_authority()
        if object.__getattribute__(state, "_sealed") is not True:
            raise error_type("policy issuance dispatch state is not sealed")
        return open_dispatch(authority)

    def guarded_issue(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        *,
        source_evaluation_bundle_id: str,
        baseline_kind: BaselineKind | None = None,
    ) -> IssuedPolicyEvaluationRef:
        require_state_class_authority()
        if object.__getattribute__(state, "_sealed") is not True:
            raise error_type("policy issuance dispatch state is not sealed")
        return issue_dispatch(
            authority,
            protocol,
            source_evaluation_bundle_id=source_evaluation_bundle_id,
            baseline_kind=baseline_kind,
        )

    def guarded_resolve(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        reference: IssuedPolicyEvaluationRef,
    ) -> PolicyEvaluation:
        require_state_class_authority()
        if object.__getattribute__(state, "_sealed") is not True:
            raise error_type("policy issuance dispatch state is not sealed")
        return resolve_dispatch(authority, protocol, reference)

    def guarded_verify(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        reference: IssuedPolicyEvaluationRef,
        claimed: PolicyEvaluation,
    ) -> PolicyEvaluation:
        require_state_class_authority()
        if object.__getattribute__(state, "_sealed") is not True:
            raise error_type("policy issuance dispatch state is not sealed")
        return verify_dispatch(authority, protocol, reference, claimed)

    state.bind_public(guarded_open, guarded_issue, guarded_resolve, guarded_verify)
    return guarded_open, guarded_issue, guarded_resolve, guarded_verify


(
    _issuance._open_canonical_authorities,
    _issuance.issue_product_policy_evaluation,
    _issuance.resolve_product_policy_evaluation,
    _issuance.verify_product_policy_evaluation,
) = _build_dispatch_guards()

del _build_dispatch_guards
