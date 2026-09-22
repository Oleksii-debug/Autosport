"""Seal the canonical artifact reader used by product PolicyEvaluation issuance.

The ProductPolicyEvaluation issuer already captures the concrete ScientificRegistry and
FactoryArtifactStore surfaces. FactoryArtifactStore._stable_snapshot delegates one
level lower to RunTransaction._read_canonical_file_snapshot, so that transitive
executable must be frozen as part of the same product-owned read authority.
"""

from __future__ import annotations

import marshal
from types import FunctionType

from . import external_validity_policy_issuance as _issuance
from . import run_transaction as _run_transaction_module
from . import strategy_model_factory as _strategy_model_factory_module


def _build_require_store_guard():
    """Build one closure-local executable authority for canonical artifact reads."""

    original_require_store = _issuance._require_store
    issuance_error_type = _issuance.ProductPolicyEvaluationIssuanceError
    run_transaction_module = _run_transaction_module
    strategy_model_factory_module = _strategy_model_factory_module
    run_transaction_type = run_transaction_module.RunTransaction
    canonical_reader = run_transaction_type._read_canonical_file_snapshot

    type_builtin = type
    vars_builtin = vars
    staticmethod_type = staticmethod
    function_type = FunctionType
    marshal_dumps = marshal.dumps

    reader_module_name = run_transaction_module.__name__
    reader_qualname = "RunTransaction._read_canonical_file_snapshot"
    reader_globals = vars_builtin(run_transaction_module)
    canonical_reader_code = marshal_dumps(canonical_reader.__code__)

    def raise_rebound() -> None:
        raise issuance_error_type(
            "FactoryArtifactStore executable authority was rebound"
        )

    def require_canonical_file_reader() -> FunctionType:
        if (
            run_transaction_module.RunTransaction is not run_transaction_type
            or strategy_model_factory_module.RunTransaction is not run_transaction_type
        ):
            raise_rebound()

        raw = vars_builtin(run_transaction_type).get(
            "_read_canonical_file_snapshot"
        )
        if type_builtin(raw) is not staticmethod_type:
            raise_rebound()
        candidate = raw.__func__
        if (
            type_builtin(candidate) is not function_type
            or candidate is not canonical_reader
            or candidate.__module__ != reader_module_name
            or candidate.__qualname__ != reader_qualname
            or candidate.__globals__ is not reader_globals
            or marshal_dumps(candidate.__code__) != canonical_reader_code
        ):
            raise_rebound()
        return candidate

    def require_store_with_canonical_reader_authority(store: object):
        require_canonical_file_reader()
        return original_require_store(store)

    return require_store_with_canonical_reader_authority


_issuance._require_store = _build_require_store_guard()
del _build_require_store_guard
