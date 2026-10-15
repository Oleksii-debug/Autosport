"""Fail-closed regression for caller-minted process-local Betfair admission.

Fixture only: no real accounts, credentials or network operations.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import test_betfair_supervised_execution as fixtures
from autosport.betfair_supervised_execution import (
    _FINAL_SEND_ADMISSION,
    BetfairSupervisedExecutionError,
)


def test_caller_minted_contextvar_cannot_send_without_durable_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: fixtures.RESERVED_AT,
    )
    profile, bound, _approval, ledger, action, store = (
        fixtures._prepared(str(tmp_path))
    )
    transport = fixtures._Transport(
        lambda request: fixtures._response(request)
    )
    client = fixtures._enabled_client(profile, transport, store=store)

    # ContextVar.set is callable from ordinary application code. Historically
    # this matched the low-level callback identity check and would reach POST
    # without any ledger attempt or consumed operator-confirmation receipt.
    observed: list[str] = []

    def forged_admission(request_digest: str) -> None:
        observed.append(request_digest)

    token = _FINAL_SEND_ADMISSION.set(forged_admission)
    try:
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="direct placeOrders dispatch requires canonical confirmed executor",
        ):
            client.place_action(
                action,
                profile=profile,
                bound=bound,
                provider_order_ref="a1b2c3",
                execution_workspace=tmp_path.resolve(),
                _before_transport=forged_admission,
            )
    finally:
        _FINAL_SEND_ADMISSION.reset(token)

    assert transport.calls == []
    assert observed == []
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
