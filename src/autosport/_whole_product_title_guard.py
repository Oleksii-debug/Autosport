"""Keep user-facing product chrome aligned with Autosport's one-product scope.

The merged localization stack still delegates ``ui.app.title`` to the immutable
v2 base catalog.  Older v2 bytes describe the application as a ``V1`` laboratory,
which is a presentation regression: versioned strategy/evidence identifiers remain
valid, but the product itself has no separate V1 finish line.

This compatibility guard changes only that presentation value and is deliberately
content-bound to the known legacy title so an unexpected upstream title change
cannot be silently overwritten.
"""

from __future__ import annotations

from types import MappingProxyType

from . import localization_v2 as _v2

_LEGACY_TITLE = "Автоспорт — V1 лабораторія паперового моделювання для Windows"
_WHOLE_PRODUCT_TITLE = "Автоспорт — аналітична програма для Windows"


def _install() -> None:
    messages = dict(_v2.catalog(_v2.DEFAULT_LOCALE))
    current = messages.get("ui.app.title")
    if current == _WHOLE_PRODUCT_TITLE:
        return
    if current != _LEGACY_TITLE:
        raise RuntimeError(
            "ui.app.title changed outside the whole-product title authority"
        )

    messages["ui.app.title"] = _WHOLE_PRODUCT_TITLE
    ukrainian = MappingProxyType(messages)
    _v2._UK_UA = ukrainian
    _v2._CATALOGS = MappingProxyType({_v2.DEFAULT_LOCALE: ukrainian})


_install()
del _install

__all__: list[str] = []
