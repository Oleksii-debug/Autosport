"""Extend the PaperBook persistence witness to frozen module-surface executables.

The module-member freeze replaces mutable stdlib modules with read-only facade objects.
The facade backing mapping is immutable, but attribute lookup still dispatches through
its Python class. Reuse the already-installed value-type executable witness so later
class/code substitution cannot retarget ``json.loads``, ``hashlib.sha256`` or the
other frozen persistence members before positive path load/save.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


def _install() -> None:
    paper_book = _paper.PaperBook
    namespace = vars(paper_book)
    load_descriptor = namespace.get("load")
    save_descriptor = namespace.get("save")
    if type(load_descriptor) is not classmethod:
        raise RuntimeError("canonical PaperBook positive path load must remain a classmethod")
    if type(save_descriptor) is not FunctionType:
        raise RuntimeError("canonical PaperBook durable save must remain a Python function")

    load = load_descriptor.__func__
    save = save_descriptor
    if type(load) is not FunctionType:
        raise RuntimeError("canonical PaperBook positive path load must remain a Python function")
    if load.__globals__ is not save.__globals__:
        raise RuntimeError("PaperBook persistence wrappers must share one witness graph")

    wrapper_globals = load.__globals__
    capture = wrapper_globals.get("_capture_value_type_callable_witnesses")
    existing = wrapper_globals.get("_VALUE_TYPE_CALLABLE_WITNESSES")
    if type(capture) is not FunctionType or type(existing) is not tuple:
        raise RuntimeError("PaperBook persistence value-type witness graph is unavailable")

    surfaces = (_guard.json, _guard.hashlib, _guard.os, _guard.tempfile)
    surface_type = type(surfaces[0])
    if any(type(surface) is not surface_type for surface in surfaces[1:]):
        raise RuntimeError("PaperBook frozen persistence surfaces do not share one canonical type")
    if any(owner is surface_type for owner, _witnesses in existing):
        raise RuntimeError("PaperBook frozen persistence surface type is already witnessed")

    surface_witnesses = capture(surface_type)
    wrapper_globals["_VALUE_TYPE_CALLABLE_WITNESSES"] = (
        *existing,
        (surface_type, surface_witnesses),
    )


_install()
del _install
