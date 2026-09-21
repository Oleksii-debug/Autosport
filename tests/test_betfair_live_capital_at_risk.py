from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_live_capital_at_risk import (
    BetfairLiveCapitalAtRiskError,
    BetfairLiveCapitalAtRiskStatus,
    resolve_betfair_live_capital_at_risk,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    _bound_binding_sha256,
)


NOW = datetime(2026, 9, 21, 22, 30, tzinfo=timezone.utc)
CREATED_AT = "2026-09-21T22:20:00+00:00"
RESERVED_AT = "2026-09-21T22:20:01+00:00"
SUBMITTED_AT = "2026-09-21T22:20:02+00:00"
ACK_AT = "2026-09-21T22:20:03+00:00"
EXPIRES_AT = "2026-09-21T22:40:00+00:00"
ACTION_ID = "1" * 64


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _bound(
    *,
    requested_stake: Decimal = Decimal("10"),
    requested_odds: Decimal = Decimal("2"),
) -> BoundSupervisedExecutionPlan:
    action = ExecutionAction(
        action_id=ACTION_ID,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        quote_id="quote-1",
        quote_observed_at=CREATED_AT,
        expires_at=EXPIRES_AT,
    )
    provisional = ExecutionPlan(
        plan_id="provisional",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=CREATED_AT,
        actions=(action,),
    )
    profile_bindings = (
        ProfileBinding(
            venue_id="betfair",
            account_id="acct-1",
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            profile_sha256="a" * 64,
        ),
    )
    constraints = (
        ExecutionLegConstraint(
            leg_id=ACTION_ID,
            side="BACK",
            quote_expires_at=EXPIRES_AT,
            max_slippage_fraction=Decimal("0.05"),
        ),
    )
    kwargs = {
        "portfolio_plan_sha256": "b" * 64,
        "economic_goal_contract_sha256": "c" * 64,
        "intent_id": "intent-1",
        "intent_sha256": "d" * 64,
        "approval_fingerprint": "e" * 64,
        "profile_bindings": profile_bindings,
        "constraints": constraints,
    }
    binding = _bound_binding_sha256(provisional, **kwargs)
    plan = replace(provisional, plan_id=f"supervised-v2-{binding}")
    return BoundSupervisedExecutionPlan(execution_plan=plan, **kwargs)


def _ledger(
    tmp_path: Path,
    bound: BoundSupervisedExecutionPlan,
    *,
    partial_matched: Decimal | None = Decimal("4"),
) -> tuple[RealExecutionLedger, str]:
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.begin_attempt(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
        attempt_id="attempt-1",
        reserved_at=RESERVED_AT,
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted("attempt-1", submitted_at=SUBMITTED_AT)
    if partial_matched is not None:
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id="attempt-1",
                external_receipt_id="bet-1",
                status=AcknowledgementStatus.PARTIAL,
                acknowledged_at=ACK_AT,
                accepted_odds=Decimal("2"),
                accepted_stake=partial_matched,
            )
        )
    return ledger, provider_ref


def _current_row(
    provider_ref: str,
    *,
    bet_id: str = "bet-1",
    price: float = 2.0,
    requested_size: float = 10.0,
    matched: float = 4.0,
    remaining: float = 6.0,
    status: str = "EXECUTABLE",
) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.234",
        "selectionId": 42,
        "side": "BACK",
        "status": status,
        "placedDate": "2026-09-21T22:20:02+00:00",
        "priceSize": {"price": price, "size": requested_size},
        "averagePriceMatched": 2.0 if matched else 0,
        "sizeMatched": matched,
        "sizeRemaining": remaining,
        "customerOrderRef": provider_ref,
    }


def _cleared_row(provider_ref: str) -> dict[str, object]:
    return {
        "betId": "bet-1",
        "marketId": "1.234",
        "selectionId": 42,
        "side": "BACK",
        "placedDate": "2026-09-21T22:20:02+00:00",
        "settledDate": "2026-09-21T22:29:00+00:00",
        "priceRequested": 2.0,
        "priceMatched": 2.0,
        "sizeSettled": 4.0,
        "profit": 4.0,
        "customerOrderRef": provider_ref,
        "eventId": "event-1",
    }


def _readback(
    provider_ref: str,
    *,
    current_rows: list[dict[str, object]],
    settled_rows: list[dict[str, object]] | None = None,
):
    transport = FakeTransport(
        [
            response([{"marketId": "1.234", "event": {"id": "event-1"}}], 1),
            response({"currentOrders": current_rows, "moreAvailable": False}, 2),
            response(
                {
                    "clearedOrders": settled_rows or [],
                    "moreAvailable": False,
                },
                3,
            ),
            response({"clearedOrders": [], "moreAvailable": False}, 4),
            response({"clearedOrders": [], "moreAvailable": False}, 5),
            response({"clearedOrders": [], "moreAvailable": False}, 6),
        ]
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: NOW,
        account_id="acct-1",
    )
    capture = client.read_execution_readback(
        action_id=ACTION_ID,
        market_id="1.234",
        provider_order_ref=provider_ref,
    )
    assert not transport.responses
    return capture


def test_partial_ack_matched_stake_does_not_understate_live_capital_at_risk(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound, partial_matched=Decimal("4"))
    readback = _readback(
        provider_ref,
        current_rows=[_current_row(provider_ref, matched=4.0, remaining=6.0)],
    )

    truth = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=readback,
    )

    truth.assert_authoritative()
    assert truth.status is BetfairLiveCapitalAtRiskStatus.EXACT_CURRENT_ORDER
    assert truth.matched_stake == Decimal("4")
    assert truth.unmatched_stake == Decimal("6")
    assert truth.exact_capital_at_risk == Decimal("10")
    assert truth.exact_capital_at_risk > Decimal("4")
    assert truth.execution_authority is False
    assert truth.settlement_authority is False
    assert truth.risk_release_authority is False


def test_caller_copied_readback_cannot_mint_live_risk_authority(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound)
    canonical = _readback(
        provider_ref,
        current_rows=[_current_row(provider_ref)],
    )
    copied = replace(canonical)

    truth = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=copied,
    )

    truth.assert_authoritative()
    assert truth.status is BetfairLiveCapitalAtRiskStatus.UNKNOWN
    assert truth.exact_capital_at_risk is None
    assert "not canonical authoritative" in truth.reason


def test_missing_current_order_never_releases_risk(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound)
    readback = _readback(provider_ref, current_rows=[])

    truth = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=readback,
    )

    assert truth.status is BetfairLiveCapitalAtRiskStatus.UNKNOWN
    assert truth.exact_capital_at_risk is None
    assert truth.risk_release_authority is False


def test_multiple_current_orders_for_one_durable_reference_fail_closed(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound)
    readback = _readback(
        provider_ref,
        current_rows=[
            _current_row(provider_ref, bet_id="bet-1"),
            _current_row(provider_ref, bet_id="bet-2"),
        ],
    )

    truth = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=readback,
    )

    assert truth.status is BetfairLiveCapitalAtRiskStatus.UNKNOWN
    assert truth.exact_capital_at_risk is None
    assert "exactly one" in truth.reason


def test_current_and_cleared_transition_is_not_claimed_as_exact_live_risk(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound)
    readback = _readback(
        provider_ref,
        current_rows=[_current_row(provider_ref)],
        settled_rows=[_cleared_row(provider_ref)],
    )

    truth = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=readback,
    )

    assert truth.status is BetfairLiveCapitalAtRiskStatus.UNKNOWN
    assert truth.exact_capital_at_risk is None
    assert "cleared evidence" in truth.reason


def test_provider_price_or_size_drift_fails_closed(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound)
    readback = _readback(
        provider_ref,
        current_rows=[
            _current_row(
                provider_ref,
                price=2.02,
                requested_size=10.0,
                matched=4.0,
                remaining=6.0,
            )
        ],
    )

    truth = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=readback,
    )

    assert truth.status is BetfairLiveCapitalAtRiskStatus.UNKNOWN
    assert truth.exact_capital_at_risk is None
    assert "economics mismatch" in truth.reason


def test_exact_sum_ignores_hostile_ambient_decimal_precision(tmp_path):
    old_precision = getcontext().prec
    try:
        getcontext().prec = 2
        bound = _bound(requested_stake=Decimal("10"))
        ledger, provider_ref = _ledger(tmp_path, bound, partial_matched=None)
        readback = _readback(
            provider_ref,
            current_rows=[
                _current_row(
                    provider_ref,
                    matched=4.125,
                    remaining=5.875,
                )
            ],
        )

        truth = resolve_betfair_live_capital_at_risk(
            bound,
            ledger,
            attempt_id="attempt-1",
            readback=readback,
        )
    finally:
        getcontext().prec = old_precision

    assert truth.status is BetfairLiveCapitalAtRiskStatus.EXACT_CURRENT_ORDER
    assert truth.exact_capital_at_risk == Decimal("10.000")


def test_resolver_output_itself_cannot_be_caller_minted(tmp_path):
    bound = _bound()
    ledger, provider_ref = _ledger(tmp_path, bound)
    readback = _readback(
        provider_ref,
        current_rows=[_current_row(provider_ref)],
    )
    canonical = resolve_betfair_live_capital_at_risk(
        bound,
        ledger,
        attempt_id="attempt-1",
        readback=readback,
    )

    with pytest.raises(BetfairLiveCapitalAtRiskError, match="issued only"):
        type(canonical)()

    forged = object.__new__(type(canonical))
    for name in canonical.__dataclass_fields__:
        object.__setattr__(forged, name, getattr(canonical, name))
    with pytest.raises(
        BetfairLiveCapitalAtRiskError,
        match="not issued by the canonical resolver",
    ):
        forged.assert_authoritative()
