from __future__ import annotations

import inspect

import autosport.risk_randomization_precommit as precommit


def test_randomization_entropy_injector_is_not_a_module_capability() -> None:
    """Ordinary callers must have no callable surface for caller-selected entropy."""

    assert not hasattr(precommit, "_issue_risk_randomization_precommit")
    assert "_product_token_bytes" not in inspect.signature(
        precommit.issue_risk_randomization_precommit
    ).parameters
