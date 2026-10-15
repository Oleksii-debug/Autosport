from __future__ import annotations

from types import FunctionType

from autosport.risk import PaperRiskPolicy


def _function_for_descriptor(name: str) -> FunctionType:
    descriptor = vars(PaperRiskPolicy)[name]
    if type(descriptor) in {classmethod, staticmethod}:
        function = descriptor.__func__
    else:
        function = descriptor
    assert type(function) is FunctionType
    return function


def test_public_risk_wrappers_expose_no_callable_pre_guard_delegate() -> None:
    """Generation authority must not leave a directly callable original-method bypass."""

    for name in (
        "_book_state",
        "risk_of_ruin_portfolio_sha256",
        "_historical_risk_metrics",
        "_shadow_book_for_allocation",
        "_identity_concentration_decision",
        "derive_goal_stake",
        "derive_goal_stake_vector",
        "evaluate",
    ):
        function = _function_for_descriptor(name)
        leaked = {
            global_name: value
            for global_name, value in function.__globals__.items()
            if global_name.startswith("_ORIGINAL_") and type(value) is FunctionType
        }
        assert leaked == {}
