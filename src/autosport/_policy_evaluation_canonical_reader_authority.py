"""Seal the canonical artifact reader used by product PolicyEvaluation issuance.

The ProductPolicyEvaluation issuer already captures the concrete ScientificRegistry and
FactoryArtifactStore surfaces. FactoryArtifactStore._stable_snapshot delegates one
level lower to RunTransaction._read_canonical_file_snapshot, so that transitive
executable must be frozen as part of the same product-owned read authority.
"""

from __future__ import annotations

from types import FunctionType

from . import external_validity_policy_issuance as _issuance
from . import resolver_semantics as _resolver_semantics
from . import run_transaction as _run_transaction_module
from . import strategy_model_factory as _strategy_model_factory_module


_ORIGINAL_REQUIRE_STORE = _issuance._require_store
_RUN_TRANSACTION_TYPE = _run_transaction_module.RunTransaction
_CANONICAL_FILE_READER = _RUN_TRANSACTION_TYPE._read_canonical_file_snapshot
_READER_QUALNAME = "RunTransaction._read_canonical_file_snapshot"


def _raise_rebound() -> None:
    raise _issuance.ProductPolicyEvaluationIssuanceError(
        "FactoryArtifactStore executable authority was rebound"
    )


def _require_canonical_file_reader() -> FunctionType:
    """Re-derive the exact lower reader semantics before positive store use."""

    if (
        _run_transaction_module.RunTransaction is not _RUN_TRANSACTION_TYPE
        or _strategy_model_factory_module.RunTransaction is not _RUN_TRANSACTION_TYPE
    ):
        _raise_rebound()

    raw = vars(_RUN_TRANSACTION_TYPE).get("_read_canonical_file_snapshot")
    if type(raw) is not staticmethod:
        _raise_rebound()
    candidate = raw.__func__
    if (
        type(candidate) is not FunctionType
        or candidate is not _CANONICAL_FILE_READER
        or candidate.__module__ != _run_transaction_module.__name__
        or candidate.__qualname__ != _READER_QUALNAME
        or candidate.__globals__ is not vars(_run_transaction_module)
    ):
        _raise_rebound()

    try:
        source = _resolver_semantics._module_source(candidate)
        parts = _resolver_semantics._qualname_parts(candidate)
        expected = _resolver_semantics._compiled_resolver_code(source, parts)
        if _resolver_semantics._code_payload(
            candidate.__code__
        ) != _resolver_semantics._code_payload(expected):
            _raise_rebound()
    except _resolver_semantics.ResolverSemanticIdentityError as exc:
        raise _issuance.ProductPolicyEvaluationIssuanceError(
            "FactoryArtifactStore executable authority was rebound"
        ) from exc
    return candidate


def _require_store_with_canonical_reader_authority(store: object):
    """Reject mutable dispatch below the issuer's captured store surface."""

    _require_canonical_file_reader()
    return _ORIGINAL_REQUIRE_STORE(store)


_issuance._require_store = _require_store_with_canonical_reader_authority
