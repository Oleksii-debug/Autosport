"""Remove pre-generation persistence callables after the guarded graph is sealed.

Generation CAS composes the canonical PaperBook path loader/saver before the later
persistence graph freeze.  The freeze keeps the composed public path self-contained,
so the older pre-generation trusted load/save delegates no longer need to remain
reachable from the owning guard module.  Leaving them there would expose a callable
route around the cross-process publication lock while retaining all other trusted
persistence helpers.

This module owns no persistence, lock, witness, parser, serializer, or economic
authority.  It only removes obsolete module-level capability handles after the
canonical wrappers have captured the composed graph.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper
from . import risk as _risk


_OBSOLETE_HANDLES = (
    "_GENERATION_ORIGINAL_TRUSTED_LOAD",
    "_GENERATION_ORIGINAL_TRUSTED_SAVE",
)


def _install() -> None:
    namespace = _guard.__dict__
    paper_book_namespace = vars(_paper.PaperBook)
    load_descriptor = paper_book_namespace.get("load")
    save_descriptor = paper_book_namespace.get("save")
    if type(load_descriptor) is not classmethod or type(load_descriptor.__func__) is not FunctionType:
        raise RuntimeError("canonical PaperBook guarded load is unavailable")
    if type(save_descriptor) is not FunctionType:
        raise RuntimeError("canonical PaperBook durable save is unavailable")

    # At this point the load-dispatch and wrapper-helper guards must already have
    # detached the public path from the mutable owning module namespace. Refuse to
    # delete anything if composition order drifted.
    if load_descriptor.__func__.__globals__ is namespace or save_descriptor.__globals__ is namespace:
        raise RuntimeError("PaperBook persistence graph is not sealed before handle cleanup")

    trusted_load = namespace.get("_trusted_path_load")
    trusted_save = namespace.get("_trusted_save")
    if type(trusted_load) is not FunctionType or type(trusted_save) is not FunctionType:
        raise RuntimeError("canonical generation-guarded persistence authority is unavailable")

    obsolete_values: list[FunctionType] = []
    for name in _OBSOLETE_HANDLES:
        value = namespace.get(name)
        if type(value) is not FunctionType:
            raise RuntimeError(f"obsolete PaperBook persistence handle is unavailable: {name}")
        if value is trusted_load or value is trusted_save:
            raise RuntimeError("refusing to remove canonical generation-guarded persistence authority")
        obsolete_values.append(value)

    # Delete only the obsolete pre-generation load/save callables. The generation
    # wrappers for committed_stake/open_ticket/settle still require their original
    # non-persistence delegates and are intentionally untouched.
    for name in _OBSOLETE_HANDLES:
        del namespace[name]


_install()
del _install

# Market semantics became part of durable PAPER identity in schema 8. Compose that
# identity into the already-source-defined risk roots before this module captures
# candidate/evaluate witnesses and before generation/private/root sealing runs.
from . import _risk_market_semantics_identity as _risk_market_semantics_identity  # noqa: E402,F401

# Raw risk executable specs remain reconstructible compatibility evidence. Complete
# the existing evaluate witness before any generation/private wrapper snapshots that
# raw function. Only helpers whose descriptors remain stable across later wrapper
# composition belong here. Generation/private-replaced helper roots are sealed after
# their final wrappers are installed by _paperbook_risk_root_surface_seal.
_policy_namespace = vars(_risk.PaperRiskPolicy)
_fraction_limits = _policy_namespace.get("_effective_fraction_limits")
_decimal_context = _policy_namespace.get("_decimal_context")
_exact_positive_sum = _policy_namespace.get("_exact_positive_sum")
_fraction_exceeds = _policy_namespace.get("_fraction_exceeds")
_ruin_candidate = _policy_namespace.get("risk_of_ruin_candidate_sha256")
_ruin_candidate_vector = _policy_namespace.get("risk_of_ruin_candidate_vector_sha256")
if type(_fraction_limits) is not FunctionType:
    raise RuntimeError("canonical PaperRiskPolicy fraction-limit helper is unavailable")
for _helper_name, _helper_descriptor in (
    ("_decimal_context", _decimal_context),
    ("_exact_positive_sum", _exact_positive_sum),
    ("_fraction_exceeds", _fraction_exceeds),
    ("risk_of_ruin_candidate_sha256", _ruin_candidate),
):
    if (
        type(_helper_descriptor) is not staticmethod
        or type(_helper_descriptor.__func__) is not FunctionType
    ):
        raise RuntimeError(
            f"canonical PaperRiskPolicy static helper is unavailable: {_helper_name}"
        )
if (
    type(_ruin_candidate_vector) is not classmethod
    or type(_ruin_candidate_vector.__func__) is not FunctionType
):
    raise RuntimeError(
        "canonical PaperRiskPolicy candidate-vector digest helper is unavailable"
    )
_evaluate_witnesses = _risk._PAPER_RISK_EVALUATE_HELPER_WITNESSES
if type(_evaluate_witnesses) is not tuple:
    raise RuntimeError("canonical PaperRiskPolicy evaluate witness tuple is unavailable")
_witness_names = tuple(item[0] for item in _evaluate_witnesses)
_transitive_witnesses = (
    (
        "_effective_fraction_limits",
        _fraction_limits,
        _fraction_limits,
        _fraction_limits.__code__,
        False,
    ),
    (
        "_decimal_context",
        _decimal_context,
        _decimal_context.__func__,
        _decimal_context.__func__.__code__,
        True,
    ),
    (
        "_exact_positive_sum",
        _exact_positive_sum,
        _exact_positive_sum.__func__,
        _exact_positive_sum.__func__.__code__,
        True,
    ),
    (
        "_fraction_exceeds",
        _fraction_exceeds,
        _fraction_exceeds.__func__,
        _fraction_exceeds.__func__.__code__,
        True,
    ),
    (
        "risk_of_ruin_candidate_sha256",
        _ruin_candidate,
        _ruin_candidate.__func__,
        _ruin_candidate.__func__.__code__,
        True,
    ),
)
_transitive_names = tuple(item[0] for item in _transitive_witnesses)
if any(name in _witness_names for name in _transitive_names):
    if not all(name in _witness_names for name in _transitive_names):
        raise RuntimeError("canonical PaperRiskPolicy transitive helper witness is partial")
else:
    _risk._PAPER_RISK_EVALUATE_HELPER_WITNESSES = (
        _evaluate_witnesses + _transitive_witnesses
    )

_vector_witnesses = _risk._PAPER_RISK_DERIVE_GOAL_STAKE_VECTOR_HELPER_WITNESSES
if type(_vector_witnesses) is not tuple:
    raise RuntimeError(
        "canonical PaperRiskPolicy stake-vector helper witness tuple is unavailable"
    )
_vector_witness_names = tuple(item[0] for item in _vector_witnesses)
_vector_digest_witness = (
    "risk_of_ruin_candidate_vector_sha256",
    _ruin_candidate_vector,
    _ruin_candidate_vector.__func__,
    _ruin_candidate_vector.__func__.__code__,
    True,
)
if "risk_of_ruin_candidate_vector_sha256" not in _vector_witness_names:
    _risk._PAPER_RISK_DERIVE_GOAL_STAKE_VECTOR_HELPER_WITNESSES = (
        _vector_witnesses + (_vector_digest_witness,)
    )

del _policy_namespace
del _fraction_limits
del _decimal_context
del _exact_positive_sum
del _fraction_exceeds
del _ruin_candidate
del _ruin_candidate_vector
del _evaluate_witnesses
del _witness_names
del _transitive_witnesses
del _transitive_names
del _vector_witnesses
del _vector_witness_names
del _vector_digest_witness
del _helper_name
del _helper_descriptor
del _risk

# Risk policy derives portfolio/equity evidence from direct PaperBook fields. Install
# its generation-stable read wrapper only after the canonical persistence graph has
# been sealed, so the wrapper can reuse that detached authority rather than mutable
# guard-module helpers. The raw evaluator independently witnesses stable transitive
# helpers; wrapper-replaced helper roots are finalized by the later root-surface seal.
from . import _paperbook_risk_generation_guard as _paperbook_risk_generation_guard  # noqa: E402,F401

# Structural replay alone cannot prove that caller-visible ticket economics still
# match the product-issued private opening/causal registries. Compose that existing
# authority around the finalized generation-stable risk surface.
from . import _paperbook_risk_private_authority_guard as _paperbook_risk_private_authority_guard  # noqa: E402,F401

# Final owner-facing decision/sizing roots and wrapper-replaced transitive class roots
# are sealed only after generation/private-authority wrapper composition is complete.
from . import _paperbook_risk_root_surface_seal as _paperbook_risk_root_surface_seal  # noqa: E402,F401

# RunTransaction current-binding wrappers exist before direct-dispatch detachment.
# Seal their exact resolver FunctionTypes and transitive FunctionType closure graphs
# now, so direct-dispatch cannot freeze a resolver whose identity/code stays stable
# while captured binding/persistence authority cells have been retargeted.
from . import _run_transaction_current_binding_resolver_closure_guard as _run_transaction_current_binding_resolver_closure_guard  # noqa: E402,F401

# RunTransaction is first imported only after the canonical PaperBook persistence
# graph above is fully composed. Detach its already-installed stage/promotion wrappers
# from live module-global stdlib/PaperBook dispatch before later product surfaces can
# import and capture the transaction class.
from . import _run_transaction_paperbook_direct_dispatch_guard as _run_transaction_paperbook_direct_dispatch_guard  # noqa: E402,F401

# Direct-dispatch composition is also one-shot. Its factory/rebuild helpers can create
# authority-bearing detached FunctionTypes and are not runtime product API. Remove
# those handles only after the canonical stage/promotion methods have captured their
# exact snapshots; downstream verifier sealing consumes the installed methods, not
# these composition utilities.
_direct_dispatch_namespace = _run_transaction_paperbook_direct_dispatch_guard.__dict__
for _direct_dispatch_name in (
    "_detach_consumer",
    "_guard_detached_consumer",
    "_make_surface_authority_checker",
    "_fresh_cell",
):
    _direct_dispatch_value = _direct_dispatch_namespace.get(_direct_dispatch_name)
    if type(_direct_dispatch_value) is not FunctionType:
        raise RuntimeError(
            f"RunTransaction direct-dispatch setup handle is unavailable: {_direct_dispatch_name}"
        )
    del _direct_dispatch_namespace[_direct_dispatch_name]
del _direct_dispatch_namespace
del _direct_dispatch_name
del _direct_dispatch_value

# The detached binding verifier itself still exposes closure cells for exact lookup
# primitives as tamper evidence. Seal those helpers immediately after direct-dispatch
# composition so a later retarget is rejected before the verifier can execute it.
from . import _run_transaction_detached_verifier_helper_guard as _run_transaction_detached_verifier_helper_guard  # noqa: E402,F401
