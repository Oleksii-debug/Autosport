from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

import autosport.betfair_live_capital_at_risk as live_risk
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_live_capital_at_risk import (
    BetfairLiveCapitalAtRiskError,
    BetfairLiveCapitalAtRiskTruth,
    resolve_betfair_live_capital_at_risk,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    _bound_binding_sha256,
)


NOW = datetime(2026, 9, 22, 8, 40, tzinfo=timezone.utc)


class _CallerTransport:
    """Caller-controlled bytes; deliberately not the product HTTPS transport."""

    def __init__(self, provider_order_ref: str) -> None:
        self.provider_order_ref = provider_order_ref

    def post(self, url, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body)
        method = request["method"]
        if method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {
                "currentOrders": [
                    {
                        "betId": "caller-bet-1",
                        "marketId": "1.234",
                        "selectionId": 10,
                        "side": "BACK",
                        "status": "EXECUTABLE",
                        "placedDate": "2026-09-22T08:39:55+00:00",
                        "priceSize": {"price": 2, "size": 5},
                        "averagePriceMatched": 0,
                        "sizeMatched": 0,
                        "sizeRemaining": 5,
                        "customerOrderRef": self.provider_order_ref,
                    }
                ],
                "moreAvailable": False,
            }
        elif method.endswith("listClearedOrders"):
            result = {"clearedOrders": [], "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode()


def _bound_plan() -> BoundSupervisedExecutionPlan:
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side="BACK",
        requested_odds=Decimal("2"),
        requested_stake=Decimal("5"),
        quote_id="q" * 64,
        quote_observed_at="2026-09-22T08:35:00+00:00",
        expires_at="2026-09-22T09:30:00+00:00",
    )
    profile = ProfileBinding(
        "betfair",
        "acct-1",
        "betfair-supervised",
        "1",
        1,
        "a" * 64,
    )
    constraint = ExecutionLegConstraint(
        "action-1",
        "BACK",
        "2026-09-22T09:30:00+00:00",
        Decimal("0.05"),
    )
    common = dict(
        bookmaker_profile_version="profile-set-test",
        decision_id="decision-test",
        approval_id="approval-test",
        created_at="2026-09-22T08:36:00+00:00",
        actions=(action,),
    )
    provisional = ExecutionPlan(
        plan_id="pending-supervised-v2-binding",
        **common,
    )
    binding_args = (
        "b" * 64,
        "c" * 64,
        "intent-test",
        "d" * 64,
        "e" * 64,
        (profile,),
        (constraint,),
    )
    bridge = _bound_binding_sha256(provisional, *binding_args)
    execution = ExecutionPlan(plan_id=f"supervised-v2-{bridge}", **common)
    return BoundSupervisedExecutionPlan(execution, *binding_args)


def test_caller_transport_cannot_mint_exact_live_capital(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(live_risk, "_utc_now", lambda: NOW)
    bound = _bound_plan()
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.begin_attempt(
        plan_id=bound.execution_plan.plan_id,
        action_id="action-1",
        attempt_id="attempt-1",
        reserved_at="2026-09-22T08:37:00+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-22T08:38:00+00:00",
    )

    direct_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("caller-app-key", "caller-session-token"),
        transport=_CallerTransport(provider_ref),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    )

    # Either the acquisition boundary or the downstream risk resolver may
    # reject this direct/custom origin. What must never happen is positive
    # EXACT money-risk truth from caller-controlled transport bytes.
    try:
        readback = direct_client.read_execution_readback(
            action_id="action-1",
            market_id="1.234",
            provider_order_ref=provider_ref,
        )
    except BetfairReadOnlyError:
        return

    try:
        evidence = resolve_betfair_live_capital_at_risk(
            ledger,
            bound,
            readback,
            attempt_id="attempt-1",
        )
    except BetfairLiveCapitalAtRiskError:
        return

    assert evidence.truth is not BetfairLiveCapitalAtRiskTruth.EXACT
