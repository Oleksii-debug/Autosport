from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


def test_owner_facing_risk_denies_nested_policy_helper_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = PaperBook("100")
    policy = PaperRiskPolicy()

    baseline = policy.evaluate(book, Decimal("1"))
    assert baseline.allowed is True

    def bypass_derived_risk_values(*args, **kwargs):
        raise AssertionError("rebound risk helper must never execute")

    monkeypatch.setattr(
        PaperRiskPolicy,
        "_derived_risk_values",
        bypass_derived_risk_values,
    )

    blocked = policy.evaluate(book, Decimal("1"))
    assert blocked.allowed is False
    assert blocked.reason == "virtual bankroll generation authority is invalid"
