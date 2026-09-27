from __future__ import annotations

import inspect

from autosport.risk_of_ruin_authority import verify_risk_of_ruin_authority


def test_public_risk_authority_does_not_expose_product_resolver_injection() -> None:
    """A caller must not be able to replace the product-owned positive issuer seam."""

    signature = inspect.signature(verify_risk_of_ruin_authority)

    assert "_product_evaluator_resolver" not in signature.parameters
    assert "product_evaluator_resolver" not in signature.parameters


def test_public_risk_authority_has_no_writable_resolver_closure() -> None:
    """Positive authority must not depend on caller-writable closure state."""

    assert verify_risk_of_ruin_authority.__closure__ is None
    assert verify_risk_of_ruin_authority.__code__.co_freevars == ()
    assert "_resolve_product_evaluator_result" not in verify_risk_of_ruin_authority.__code__.co_names
