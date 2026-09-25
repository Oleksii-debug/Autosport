from __future__ import annotations

from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

from autosport.continuous_session import SessionState
from autosport.product_paper_decision_cycle import (
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)


def _bare_cycle(workspace: Path) -> ProductPaperDecisionCycle:
    cycle = object.__new__(ProductPaperDecisionCycle)
    cycle._cycle_lock = Lock()
    cycle.runtime = SimpleNamespace(
        workspace=workspace,
        tick=lambda: SimpleNamespace(),
        status=lambda: SimpleNamespace(state=SessionState.RUNNING),
    )
    return cycle


def test_supported_tick_rejects_class_level_authority_resolver_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    cycle = _bare_cycle(tmp_path)
    monkeypatch.setattr(
        ProductPaperDecisionCycle,
        "_resolve_product_authority",
        lambda _self: object(),
    )

    with pytest.raises(
        ProductPaperDecisionCycleError,
        match="supported PAPER decision tick dispatch changed",
    ):
        cycle.tick()


def test_supported_tick_ignores_instance_shadowed_authority_resolver(
    tmp_path,
) -> None:
    cycle = _bare_cycle(tmp_path)
    forged_calls: list[bool] = []

    def _forged_resolver():
        forged_calls.append(True)
        return object()

    cycle._resolve_product_authority = _forged_resolver

    with pytest.raises(
        ProductPaperDecisionCycleError,
        match="supported PAPER decision authority cannot be reconstructed from durable START authority",
    ):
        cycle.tick()

    assert forged_calls == []
