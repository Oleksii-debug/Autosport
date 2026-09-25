from __future__ import annotations

from types import FunctionType

import autosport.external_validity_policy_issuance as issuance


def _closure_functions(function: FunctionType) -> tuple[FunctionType, ...]:
    closure = function.__closure__ or ()
    captured: list[FunctionType] = []
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if isinstance(value, FunctionType):
            captured.append(value)
    return tuple(captured)


def test_product_policy_evaluation_guards_do_not_expose_original_authority_callables() -> None:
    """Public guard reflection must not recover an unguarded issuance entrypoint.

    Python closure cells are caller-inspectable. A guard that retains the original
    issuer/resolver/verifier/open callable in a closure therefore hands callers a
    direct bypass around the dispatch checks it is supposed to enforce.
    """

    guarded_entries = (
        issuance._open_canonical_authorities,
        issuance.issue_product_policy_evaluation,
        issuance.resolve_product_policy_evaluation,
        issuance.verify_product_policy_evaluation,
    )
    authority_names = {
        "_open_canonical_authorities",
        "issue_product_policy_evaluation",
        "resolve_product_policy_evaluation",
        "verify_product_policy_evaluation",
    }

    exposed: list[str] = []
    for guarded in guarded_entries:
        for captured in _closure_functions(guarded):
            if (
                captured.__module__ == "autosport.external_validity_policy_issuance"
                and captured.__name__ in authority_names
            ):
                exposed.append(f"{guarded.__name__}->{captured.__name__}")

    assert exposed == [], (
        "dispatch guard exposes unguarded product issuance authority through "
        f"inspectable closure cells: {sorted(exposed)}"
    )
