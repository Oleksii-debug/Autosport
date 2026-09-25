"""Seal product PolicyEvaluation issuance dispatch and materialization time.

The #762 issuer is intentionally a composition layer over the canonical scientific
registry and factory artifact store.  Positive issuance must therefore depend on
exact product-owned helper dispatch and on a product-owned UTC materialization
clock, not on caller-rebound module globals or a same-class constructor mutation.

This module adds no registry, artifact store, evaluator, or promotion authority.
It only fail-closes the existing issuer's direct dispatch graph and replaces the
newly-opened canonical store's default clock with a closure-local trusted clock.
"""
from __future__ import annotations

import marshal
from datetime import datetime as _datetime_type
from datetime import timezone as _timezone_type

from . import external_validity_policy_issuance as _issuance
from . import strategy_model_factory as _factory_module


def _build_dispatch_guards():
    error_type = _issuance.ProductPolicyEvaluationIssuanceError

    original_open = _issuance._open_canonical_authorities
    original_issue = _issuance.issue_product_policy_evaluation
    original_resolve = _issuance.resolve_product_policy_evaluation
    original_verify = _issuance.verify_product_policy_evaluation

    store_type = _issuance._STORE_TYPE
    store_init = store_type.__init__
    store_init_globals = store_init.__globals__
    store_init_code = marshal.dumps(store_init.__code__)
    factory_datetime = _factory_module.datetime
    factory_timezone = _factory_module.timezone

    target = _issuance._target
    issuance_id = _issuance._issuance_id
    require_source = _issuance._require_source_factory_evaluation
    derive = _issuance._derive_policy_evaluation
    registry_get = _issuance._registry_get
    store_read = _issuance._store_read
    store_materialize = _issuance._STORE_MATERIALIZE
    store_receipt = _issuance._STORE_RECEIPT
    registry_append = _issuance._REGISTRY_APPEND
    canonical_bundle_sha256 = _issuance.canonical_product_policy_evaluation_bundle_sha256

    workspace_type = _issuance.ProductPolicyEvaluationWorkspace
    ref_type = _issuance.IssuedPolicyEvaluationRef
    policy_evaluation_type = _issuance.PolicyEvaluation
    protocol_type = _issuance.FrozenBaselineProtocol
    baseline_kind_type = _issuance.BaselineKind
    bundle_type = _issuance.EvaluationBundleRef

    result_artifact_kind = _issuance._RESULT_ARTIFACT_KIND
    bundle_id_prefix = _issuance._BUNDLE_ID_PREFIX
    issuer_source_sha256 = _issuance._ISSUER_SOURCE_SHA256

    trusted_datetime = _datetime_type
    trusted_utc = _timezone_type.utc

    def _raise_rebound(detail: str) -> None:
        raise error_type(f"product PolicyEvaluation issuance authority was rebound: {detail}")

    def _require_store_constructor_authority() -> None:
        candidate_init = store_type.__init__
        if (
            _factory_module.FactoryArtifactStore is not store_type
            or _issuance.FactoryArtifactStore is not store_type
            or candidate_init is not store_init
            or getattr(candidate_init, "__globals__", None) is not store_init_globals
            or marshal.dumps(candidate_init.__code__) != store_init_code
            or _factory_module.datetime is not factory_datetime
            or _factory_module.timezone is not factory_timezone
            or store_init_globals.get("datetime") is not factory_datetime
            or store_init_globals.get("timezone") is not factory_timezone
        ):
            _raise_rebound("FactoryArtifactStore constructor/clock dispatch")

    def _trusted_clock():
        return trusted_datetime.now(trusted_utc)

    def guarded_open(authority: ProductPolicyEvaluationWorkspace):
        _require_store_constructor_authority()
        registry, store = original_open(authority)
        _require_store_constructor_authority()
        if type(store) is not store_type:
            _raise_rebound("canonical artifact store type")
        # The owning constructor publicly supports an injected test clock.  The
        # product issuer never accepts one: replace its default lambda with this
        # closure-local UTC source before any result materialization can occur.
        store._clock = _trusted_clock
        return registry, store

    def _require_common_dispatch() -> None:
        if (
            _issuance._open_canonical_authorities is not guarded_open
            or _issuance._target is not target
            or _issuance._issuance_id is not issuance_id
            or _issuance._require_source_factory_evaluation is not require_source
            or _issuance._derive_policy_evaluation is not derive
            or _issuance._registry_get is not registry_get
            or _issuance._store_read is not store_read
            or _issuance._STORE_MATERIALIZE is not store_materialize
            or _issuance._STORE_RECEIPT is not store_receipt
            or _issuance._REGISTRY_APPEND is not registry_append
            or _issuance.canonical_product_policy_evaluation_bundle_sha256
            is not canonical_bundle_sha256
            or _issuance.ProductPolicyEvaluationWorkspace is not workspace_type
            or _issuance.IssuedPolicyEvaluationRef is not ref_type
            or _issuance.PolicyEvaluation is not policy_evaluation_type
            or _issuance.FrozenBaselineProtocol is not protocol_type
            or _issuance.BaselineKind is not baseline_kind_type
            or _issuance.EvaluationBundleRef is not bundle_type
            or _issuance._RESULT_ARTIFACT_KIND != result_artifact_kind
            or _issuance._BUNDLE_ID_PREFIX != bundle_id_prefix
            or _issuance._ISSUER_SOURCE_SHA256 != issuer_source_sha256
        ):
            _raise_rebound("direct helper graph")
        _require_store_constructor_authority()

    def guarded_issue(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        *,
        source_evaluation_bundle_id: str,
        baseline_kind: BaselineKind | None = None,
    ) -> IssuedPolicyEvaluationRef:
        _require_common_dispatch()
        if _issuance.resolve_product_policy_evaluation is not guarded_resolve:
            _raise_rebound("restart resolver")
        result = original_issue(
            authority,
            protocol,
            source_evaluation_bundle_id=source_evaluation_bundle_id,
            baseline_kind=baseline_kind,
        )
        _require_common_dispatch()
        return result

    def guarded_resolve(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        reference: IssuedPolicyEvaluationRef,
    ) -> PolicyEvaluation:
        _require_common_dispatch()
        result = original_resolve(authority, protocol, reference)
        _require_common_dispatch()
        return result

    def guarded_verify(
        authority: ProductPolicyEvaluationWorkspace,
        protocol: FrozenBaselineProtocol,
        reference: IssuedPolicyEvaluationRef,
        claimed: PolicyEvaluation,
    ) -> PolicyEvaluation:
        _require_common_dispatch()
        if _issuance.resolve_product_policy_evaluation is not guarded_resolve:
            _raise_rebound("restart resolver")
        result = original_verify(authority, protocol, reference, claimed)
        _require_common_dispatch()
        return result

    return guarded_open, guarded_issue, guarded_resolve, guarded_verify


(
    _issuance._open_canonical_authorities,
    _issuance.issue_product_policy_evaluation,
    _issuance.resolve_product_policy_evaluation,
    _issuance.verify_product_policy_evaluation,
) = _build_dispatch_guards()

del _build_dispatch_guards
