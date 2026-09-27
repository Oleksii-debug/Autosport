from __future__ import annotations

import builtins
from types import SimpleNamespace

import autosport.product_entrypoint as entry


class AUTOSPORT_PROVIDER_API_KEY_leaked(Exception):
    pass


def test_exception_type_label_rejects_mutated_helper_builtin_namespace(monkeypatch) -> None:
    """Canonical function/code identity alone must not authorize hostile globals.

    The canonical secret-redaction helper consults its module-global ``builtins``
    namespace when classifying exception names. Rebinding only that dependency keeps
    the helper function object and code object unchanged, so the product boundary must
    still fail closed instead of publishing a caller-controlled class name.
    """

    canonical = entry._SAFE_EXCEPTION_TYPE_LABEL
    globals_dict = canonical.__globals__
    hostile_builtins = SimpleNamespace(
        **{AUTOSPORT_PROVIDER_API_KEY_leaked.__name__: AUTOSPORT_PROVIDER_API_KEY_leaked}
    )
    monkeypatch.setitem(globals_dict, "builtins", hostile_builtins)

    rendered = entry._canonical_exception_type_label(
        AUTOSPORT_PROVIDER_API_KEY_leaked("ordinary failure")
    )

    assert canonical is entry._SAFE_EXCEPTION_TYPE_LABEL
    assert rendered == "Exception"
    assert "AUTOSPORT_PROVIDER_API_KEY" not in rendered


def test_exception_type_label_rejects_in_place_builtin_exception_authority_mutation(
    monkeypatch,
) -> None:
    """Adding a caller exception to the canonical builtins object must fail closed."""

    canonical = entry._SAFE_EXCEPTION_TYPE_LABEL
    globals_dict = canonical.__globals__
    assert globals_dict["builtins"] is builtins

    monkeypatch.setattr(
        builtins,
        AUTOSPORT_PROVIDER_API_KEY_leaked.__name__,
        AUTOSPORT_PROVIDER_API_KEY_leaked,
        raising=False,
    )

    rendered = entry._canonical_exception_type_label(
        AUTOSPORT_PROVIDER_API_KEY_leaked("ordinary failure")
    )

    assert canonical is entry._SAFE_EXCEPTION_TYPE_LABEL
    assert rendered == "Exception"
    assert "AUTOSPORT_PROVIDER_API_KEY" not in rendered
