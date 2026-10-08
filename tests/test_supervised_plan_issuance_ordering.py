from __future__ import annotations

from types import SimpleNamespace

from autosport.real_execution_ledger import RealExecutionLedger
from autosport.supervised_plan_issuance import (
    SupervisedPlanIssuanceStore,
    issue_supervised_execution,
)

from test_betfair_supervised_execution import _bound, _profile


def test_product_approval_producer_persists_before_execution_ledger_reservation(
    monkeypatch, tmp_path
) -> None:
    import autosport.supervised_plan_issuance as module

    bound, approval, _goal = _bound(_profile())
    store = SupervisedPlanIssuanceStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "machine-authority",
    )
    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")
    order: list[str] = []
    issued = SimpleNamespace(bound=bound, approval=approval)

    monkeypatch.setattr(
        module,
        "build_supervised_execution_plan",
        lambda *args, **kwargs: bound,
    )
    monkeypatch.setattr(
        store,
        "issue",
        lambda *, bound, approval: order.append("product-issuance") or issued,
    )
    monkeypatch.setattr(
        module,
        "reserve_supervised_plan",
        lambda ledger, bound, approval: order.append("execution-reservation"),
    )

    result = issue_supervised_execution(
        store=store,
        ledger=ledger,
        portfolio_plan=object(),
        intents=(),
        routing_proposal=object(),
        profiles=(),
        approval=approval,
        constraints=(),
        created_at=bound.execution_plan.created_at,
    )

    assert result is issued
    assert order == ["product-issuance", "execution-reservation"]
