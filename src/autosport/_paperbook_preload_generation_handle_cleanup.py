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
        raise RuntimeError("canonical PaperBook guarded save is unavailable")

    # At this point the load-dispatch and wrapper-helper guards must already have
    # detached the public path from the mutable owning module namespace.  Refuse to
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

    # Delete only the obsolete pre-generation load/save callables.  The generation
    # wrappers for committed_stake/open_ticket/settle still require their original
    # non-persistence delegates and are intentionally untouched.
    for name in _OBSOLETE_HANDLES:
        del namespace[name]


_install()
del _install
