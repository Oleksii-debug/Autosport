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

    Public wrapper closure cells intentionally capture only this state object, never
    an unguarded predecessor FunctionType. Extracting the state through ordinary
    closure reflection therefore still leaves the caller on guard-enforcing methods
    rather than handing out a directly callable bypass.
    """

    __slots__ = (
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
    )

    def __init__(self) -> None:
        self._error_type = _issuance.ProductPolicyEvaluationIssuanceError
        self.__original_open = _issuance._open_canonical_authorities
        self.__original_issue = _issuance.issue_product_policy_evaluation
        self.__original_resolve = _issuance.resolve_product_policy_evaluation
        self.__original_verify = _issuance.verify_product_policy_evaluation

        self._store_type = _issuance._STORE_TYPE
        self._store_init = self._store_type.__init__
        self._store_init_globals = self._store_init.__globals__
        self._store_init_code = marshal.dumps(self._store_init.__code__)
        self._factory_datetime = _factory_module.datetime
        self._factory_timezone = _factory_module.timezone

        self._target = _issuance._target
        self._issuance_id = _issuance._issuance_id
        self._require_source = _issuance._require_source_factory_evaluation
        self._derive = _issuance._derive_policy_evaluation
        self._registry_get = _issuance._registry_get
        self._store_read = _issuance._store_read
        self._store_materialize = _issuance._STORE_MATERIALIZE
        self._store_receipt = _issuance._STORE_RECEIPT
        self._registry_append = _issuance._REGISTRY_APPEND
        self._canonical_bundle_sha256 = (
            _issuance.canonical_product_policy_evaluation_bundle_sha256
        )

        self._workspace_type = _issuance.ProductPolicyEvaluationWorkspace
        self._ref_type = _issuance.IssuedPolicyEvaluationRef
        self._policy_evaluation_type = _issuance.PolicyEvaluation
        self._protocol_type = _issuance.FrozenBaselineProtocol
        self._baseline_kind_type = _issuance.BaselineKind
        self._bundle_type = _issuance.EvaluationBundleRef

        self._result_artifact_kind = _issuance._RESULT_ARTIFACT_KIND
        self._bundle_id_prefix = _issuance._BUNDLE_ID_PREFIX
        self._issuer_source_sha256 = _issuance._ISSUER_SOURCE_SHA256

        self._trusted_datetime = _datetime_type
        self._trusted_utc = _timezone_type.utc

        self._public_open = None
        self._public_issue = None
        self._public_resolve = None
        self._public_verify = None

    def __getattribute__(self, name: str):
        if name.startswith("_DispatchState__original_"):
            raise AttributeError("unguarded predecessor entrypoints are not exposed")
        return object.__getattribute__(self, name)

    def _predecessor(self, name: str):
        return object.__getattribute__(self, f"_DispatchState__original_{name}")

    def bind_public(self, open_fn, issue_fn, resolve_fn, verify_fn) -> None:
        self._public_open = open_fn
        self._public_issue = issue_fn
        self._public_resolve = resolve_fn
        self._public_verify = verify_fn

    def _raise_rebound(self, detail: str) -> None:
        raise self._error_type(
            f"product PolicyEvaluation issuance authority was rebound: {detail}"
        )

    def _require_store_constructor_authority(self) -> None:
        candidate_init = self._store_type.__init__
        candidate_code = getattr(candidate_init, "__code__", None)
        if (
            _factory_module.FactoryArtifactStore is not self._store_type
            or _issuance.FactoryArtifactStore is not self._store_type
            or candidate_init is not self._store_init
            or getattr(candidate_init, "__globals__", None)
            is not self._store_init_globals
            or candidate_code is None
            or marshal.dumps(candidate_code) != self._store_init_code
            or _factory_module.datetime is not self._factory_datetime
            or _factory_module.timezone is not self._factory_timezone
            or self._store_init_globals.get("datetime") is not self._factory_datetime
            or self._store_init_globals.get("timezone") is not self._factory_timezone
        ):
            self._raise_rebound("FactoryArtifactStore constructor/clock dispatch")

    def trusted_clock(self):
        return self._trusted_datetime.now(self._trusted_utc)

    def open(self, authority):
        self._require_store_constructor_authority()
        registry, store = self._predecessor("open")(authority)
        self._require_store_constructor_authority()
        if type(store) is not self._store_type:
            self._raise_rebound("canonical artifact store type")
        # The owning constructor publicly supports an injected test clock. The
        # product issuer never accepts one: replace its default source before any
        # result materialization can occur.
        store._clock = self.trusted_clock
        return registry, store

    def _require_common_dispatch(self) -> None:
        if (
            _issuance._open_canonical_authorities is not self._public_open
            or _issuance.issue_product_policy_evaluation is not self._public_issue
            or _issuance.resolve_product_policy_evaluation is not self._public_resolve
            or _issuance.verify_product_policy_evaluation is not self._public_verify
            or _issuance._target is not self._target
            or _issuance._issuance_id is not self._issuance_id
            or _issuance._require_source_factory_evaluation is not self._require_source
            or _issuance._derive_policy_evaluation is not self._derive
            or _issuance._registry_get is not self._registry_get
            or _issuance._store_read is not self._store_read
            or _issuance._STORE_MATERIALIZE is not self._store_materialize
            or _issuance._STORE_RECEIPT is not self._store_receipt
            or _issuance._REGISTRY_APPEND is not self._registry_append
            or _issuance.canonical_product_policy_evaluation_bundle_sha256
            is not self._canonical_bundle_sha256
            or _issuance.ProductPolicyEvaluationWorkspace is not self._workspace_type
            or _issuance.IssuedPolicyEvaluationRef is not self._ref_type
            or _issuance.PolicyEvaluation is not self._policy_evaluation_type
            or _issuance.FrozenBaselineProtocol is not self._protocol_type
            or _issuance.BaselineKind is not self._baseline_kind_type
            or _issuance.EvaluationBundleRef is not self._bundle_type
            or _issuance._RESULT_ARTIFACT_KIND != self._result_artifact_kind
            or _issuance._BUNDLE_ID_PREFIX != self._bundle_id_prefix
            or _issuance._ISSUER_SOURCE_SHA256 != self._issuer_source_sha256
        ):
            self._raise_rebound("direct helper graph")
        self._require_store_constructor_authority()

    def issue(
        self,
        authority,
        protocol,
        *,
        source_evaluation_bundle_id: str,
        baseline_kind=None,
    ):
        self._require_common_dispatch()
        result = self._predecessor("issue")(
            authority,
            protocol,
            source_evaluation_bundle_id=source_evaluation_bundle_id,
            baseline_kind=baseline_kind,
        )
        self._require_common_dispatch()
        return result

    def resolve(self, authority, protocol, reference):
        self._require_common_dispatch()
        result = self._predecessor("resolve")(authority, protocol, reference)
        self._require_common_dispatch()
        return result

    def verify(self, authority, protocol, reference, claimed):
        self._require_common_dispatch()
        result = self._predecessor("verify")(
            authority,
            protocol,
            reference,
            claimed,
        )
        self._require_common_dispatch()
        return result


def _build_dispatch_guards():
    state = _DispatchState()

    def guarded_open(authority: ProductPolicyEvaluationWorkspace):
        return state.open(authority)

    def guarded_issue(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        *,
        source_evaluation_bundle_id: str,
        baseline_kind: BaselineKind | None = None,
    ) -> IssuedPolicyEvaluationRef:
        return state.issue(
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
        return state.resolve(authority, protocol, reference)

    def guarded_verify(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        reference: IssuedPolicyEvaluationRef,
        claimed: PolicyEvaluation,
    ) -> PolicyEvaluation:
        return state.verify(authority, protocol, reference, claimed)

    state.bind_public(guarded_open, guarded_issue, guarded_resolve, guarded_verify)
    return guarded_open, guarded_issue, guarded_resolve, guarded_verify


(
    _issuance._open_canonical_authorities,
    _issuance.issue_product_policy_evaluation,
    _issuance.resolve_product_policy_evaluation,
    _issuance.verify_product_policy_evaluation,
) = _build_dispatch_guards()

del _build_dispatch_guards
