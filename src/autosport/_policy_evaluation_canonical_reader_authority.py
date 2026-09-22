"""Seal the canonical artifact reader used by product PolicyEvaluation issuance.

The ProductPolicyEvaluation issuer already captures the concrete ScientificRegistry and
FactoryArtifactStore surfaces.  FactoryArtifactStore._stable_snapshot delegates one
level lower to RunTransaction._read_canonical_file_snapshot, so that transitive
executable must be frozen as part of the same product-owned read authority.
"""

from __future__ import annotations

from . import external_validity_policy_issuance as _issuance
from . import run_transaction as _run_transaction_module
from . import strategy_model_factory as _strategy_model_factory_module


_ORIGINAL_REQUIRE_STORE = _issuance._require_store
_RUN_TRANSACTION_TYPE = _run_transaction_module.RunTransaction
_CANONICAL_FILE_READER = _RUN_TRANSACTION_TYPE._read_canonical_file_snapshot


def _require_store_with_canonical_reader_authority(store: object):
    """Reject mutable dispatch below the issuer's captured store surface."""

    if (
        _run_transaction_module.RunTransaction is not _RUN_TRANSACTION_TYPE
        or _strategy_model_factory_module.RunTransaction is not _RUN_TRANSACTION_TYPE
        or _RUN_TRANSACTION_TYPE._read_canonical_file_snapshot
        is not _CANONICAL_FILE_READER
    ):
        raise _issuance.ProductPolicyEvaluationIssuanceError(
            "FactoryArtifactStore executable authority was rebound"
        )
    return _ORIGINAL_REQUIRE_STORE(store)


_issuance._require_store = _require_store_with_canonical_reader_authority
