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

# Raw risk executable specs remain reconstructible compatibility evidence. Complete
# the existing evaluate witness before any generation/private wrapper snapshots that
# raw function: _derived_risk_values dispatches through these three exact class
# helpers, so first-level function identity alone is not sufficient authority.
_policy_namespace = vars(_risk.PaperRiskPolicy)
_fraction_limits = _policy_namespace.get("_effective_fraction_limits")
_decimal_context = _policy_namespace.get("_decimal_context")
_exact_positive_sum = _policy_namespace.get("_exact_positive_sum")
if type(_fraction_limits) is not FunctionType:
    raise RuntimeError("canonical PaperRiskPolicy fraction-limit helper is unavailable")
if (
    type(_decimal_context) is not staticmethod
    or type(_decimal_context.__func__) is not FunctionType
):
    raise RuntimeError("canonical PaperRiskPolicy decimal-context helper is unavailable")
if (
    type(_exact_positive_sum) is not staticmethod
    or type(_exact_positive_sum.__func__) is not FunctionType
):
    raise RuntimeError("canonical PaperRiskPolicy exact-sum helper is unavailable")
_evaluate_witnesses = _risk._PAPER_RISK_EVALUATE_HELPER_WITNESSES
if type(_evaluate_witnesses) is not tuple:
    raise RuntimeError("canonical PaperRiskPolicy evaluate witness tuple is unavailable")
_witness_names = tuple(item[0] for item in _evaluate_witnesses)
_transitive_names = (
    "_effective_fraction_limits",
    "_decimal_context",
    "_exact_positive_sum",
)
if any(name in _witness_names for name in _transitive_names):
    if not all(name in _witness_names for name in _transitive_names):
        raise RuntimeError("canonical PaperRiskPolicy transitive helper witness is partial")
else:
    _risk._PAPER_RISK_EVALUATE_HELPER_WITNESSES = _evaluate_witnesses + (
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
    )

del _policy_namespace
del _fraction_limits
del _decimal_context
del _exact_positive_sum
del _evaluate_witnesses
del _witness_names
del _transitive_names
del _risk

# Risk policy derives portfolio/equity evidence from direct PaperBook fields. Install
# its generation-stable read wrapper only after the canonical persistence graph has
# been sealed, so the wrapper can reuse that detached authority rather than mutable
# guard-module helpers. The raw evaluator now independently witnesses the transitive
# class helpers it dispatches through before this wrapper captures its executable spec.
from . import _paperbook_risk_generation_guard as _paperbook_risk_generation_guard  # noqa: E402,F401

# Structural replay alone cannot prove that caller-visible ticket economics still
# match the product-issued private opening/causal registries. Compose that existing
# authority around the finalized generation-stable risk surface.
from . import _paperbook_risk_private_authority_guard as _paperbook_risk_private_authority_guard  # noqa: E402,F401

# Final owner-facing decision/sizing roots are sealed only after all generation/private
# wrappers are installed. The source-defined custom metaclass lets data descriptors
# reject normal and base-type class mutation without replacing PaperRiskPolicy identity.
from . import _paperbook_risk_root_surface_seal as _paperbook_risk_root_surface_seal  # noqa: E402,F401

# RunTransaction is first imported only after the canonical PaperBook persistence
# graph above is fully composed. Detach its already-installed stage/promotion wrappers
# from live module-global stdlib/PaperBook dispatch before later product surfaces can
# import and capture the transaction class.
from . import _run_transaction_paperbook_direct_dispatch_guard as _run_transaction_paperbook_direct_dispatch_guard  # noqa: E402,F401

# The detached binding verifier itself still exposes closure cells for exact lookup
# primitives as tamper evidence. Seal those helpers immediately after direct-dispatch
# composition so a later retarget is rejected before the verifier can execute it.
from . import _run_transaction_detached_verifier_helper_guard as _run_transaction_detached_verifier_helper_guard  # noqa: E402,F401
