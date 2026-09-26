from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
import json
from pathlib import Path

import pytest

import autosport.betfair_realized_match as realized_match
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_realized_match import (
    RealizedMatchEvidenceError,
    RealizedMatchSource,
    resolve_betfair_realized_match,
    validate_betfair_realized_match_revision,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


FIXED_NOW = datetime(2026, 9, 21, 9, 55, tzinfo=timezone.utc)
MARKET_ID = "1.234"
EVENT_ID = "event-1"
SELECTION_ID = 10
ATTEMPT_ID = "attempt-1"


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


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": result,
            "id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _action(
    *,
    requested_odds: str = "3.0",
    requested_stake: str = "10",
) -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id=EVENT_ID,
        market_id=MARKET_ID,
        selection_id=str(SELECTION_ID),
        side="BACK",
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        quote_id="quote-1",
        quote_observed_at="2026-09-21T09:54:50+00:00",
        expires_at="2026-09-21T09:56:00+00:00",
    )


def _prepared(
    root: Path,
    *,
    submitted: bool = True,
    requested_odds: str = "3.0",
    requested_stake: str = "10",
) -> tuple[ExecutionPlan, RealExecutionLedger, str]:
    action = _action(
        requested_odds=requested_odds,
        requested_stake=requested_stake,
    )
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T09:54:45+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(root / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id=ATTEMPT_ID,
        reserved_at="2026-09-21T09:54:52+00:00",
    )
    provider_order_ref = ledger.bind_provider_order_reference(
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    if submitted:
        ledger.mark_submitted(
            ATTEMPT_ID,
            submitted_at="2026-09-21T09:54:55+00:00",
        )
    return plan, ledger, provider_order_ref


def _current_order(
    provider_order_ref: str,
    *,
    bet_id: str = "bet-1",
    price: float = 3.0,
    requested_size: float = 10.0,
    average_price_matched: float = 3.2,
    size_matched: float = 4.0,
    size_remaining: float = 6.0,
) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "priceSize": {
            "price": price,
            "size": requested_size,
        },
        "averagePriceMatched": average_price_matched,
        "sizeMatched": size_matched,
        "sizeRemaining": size_remaining,
        "customerOrderRef": provider_order_ref,
    }


def _cleared_order(
    provider_order_ref: str,
    *,
    bet_id: str = "bet-1",
    price_requested: float = 3.0,
    price_matched: float = 3.2,
    size_settled: float = 10.0,
    profit: float = 22.0,
) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "settledDate": "2026-09-21T10:30:00+00:00",
        "priceRequested": price_requested,
        "priceMatched": price_matched,
        "sizeSettled": size_settled,
        "profit": profit,
        "customerOrderRef": provider_order_ref,
        "eventId": EVENT_ID,
    }


def _capture(
    provider_order_ref: str,
    *,
    current_orders: list[dict[str, object]] | None = None,
    cleared_by_status: dict[str, list[dict[str, object]]] | None = None,
    observed_at: datetime = FIXED_NOW,
):
    current_orders = current_orders or []
    cleared_by_status = cleared_by_status or {}
    statuses = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
    responses = [
        _response(
            [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
            1,
        ),
        _response(
            {
                "currentOrders": current_orders,
                "moreAvailable": False,
            },
            2,
        ),
    ]
    for request_id, status in enumerate(statuses, 3):
        responses.append(
            _response(
                {
                    "clearedOrders": cleared_by_status.get(status, []),
                    "moreAvailable": False,
                },
                request_id,
            )
        )

    transport = FakeTransport(responses)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: observed_at,
        venue_id="betfair",
        account_id="acct-1",
    )
    capture = client.read_execution_readback(
        action_id="action-1",
        provider_order_ref=provider_order_ref,
        market_id=MARKET_ID,
    )
    assert len(transport.calls) == 6
    return capture


def _resolve(
    plan: ExecutionPlan,
    ledger: RealExecutionLedger,
    capture,
):
    return resolve_betfair_realized_match(
        plan,
        ledger,
        capture,
        attempt_id=ATTEMPT_ID,
    )


def test_cleared_bet_truth_overrides_transient_current_match(tmp_path: Path) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    capture = _capture(
        provider_ref,
        current_orders=[
            _current_order(
                provider_ref,
                average_price_matched=3.1,
                size_matched=4.0,
                size_remaining=6.0,
            )
        ],
        cleared_by_status={
            "SETTLED": [
                _cleared_order(
                    provider_ref,
                    price_matched=3.2,
                    size_settled=10.0,
                )
            ]
        },
    )

    evidence = _resolve(plan, ledger, capture)

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CLEARED_BET
    assert evidence.finalized is True
    assert evidence.requested_odds == Decimal("3.0")
    assert evidence.provider_matched_odds == Decimal("3.2")
    assert evidence.provider_matched_stake == Decimal("10.0")
    assert evidence.unrealized_requested_stake == Decimal("0")
    assert evidence.provider_order_ref == provider_ref
    assert evidence.plan_fingerprint == plan.fingerprint
    assert evidence.bet_id == "bet-1"
    assert evidence.provider_status == "SETTLED"
    assert evidence.has_matched_economics is True


def test_partial_current_order_is_transient_realized_match_evidence(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    evidence = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            current_orders=[
                _current_order(
                    provider_ref,
                    average_price_matched=3.2,
                    size_matched=4.0,
                    size_remaining=6.0,
                )
            ],
        ),
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CURRENT_ORDER
    assert evidence.finalized is False
    assert evidence.provider_matched_odds == Decimal("3.2")
    assert evidence.provider_matched_stake == Decimal("4.0")
    assert evidence.unrealized_requested_stake == Decimal("6.0")
    assert evidence.has_matched_economics is True


def test_no_provider_order_row_is_incomplete_not_synthetic_zero(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    evidence = _resolve(
        plan,
        ledger,
        _capture(provider_ref),
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.INCOMPLETE_EVIDENCE
    assert evidence.finalized is False
    assert evidence.bet_id is None
    assert evidence.provider_matched_odds is None
    assert evidence.provider_matched_stake is None
    assert evidence.unrealized_requested_stake is None
    assert evidence.has_matched_economics is False


def test_cleared_zero_match_is_authoritative_zero_realized_stake(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    evidence = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            cleared_by_status={
                "LAPSED": [
                    _cleared_order(
                        provider_ref,
                        price_matched=0.0,
                        size_settled=0.0,
                        profit=0.0,
                    )
                ]
            },
        ),
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CLEARED_BET
    assert evidence.finalized is True
    assert evidence.provider_status == "LAPSED"
    assert evidence.provider_matched_odds is None
    assert evidence.provider_matched_stake == Decimal("0")
    assert evidence.unrealized_requested_stake == Decimal("10")
    assert evidence.has_matched_economics is False


def test_caller_plan_must_match_durable_plan_fingerprint(tmp_path: Path) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    forged_action = replace(
        plan.actions[0],
        requested_odds=Decimal("2.8"),
    )
    forged_plan = replace(
        plan,
        actions=(forged_action,),
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="durable execution plan fingerprint",
    ):
        _resolve(
            forged_plan,
            ledger,
            _capture(provider_ref),
        )


def test_foreign_canonical_readback_cannot_rebind_durable_attempt(
    tmp_path: Path,
) -> None:
    plan, ledger, _provider_ref = _prepared(tmp_path)
    foreign_ref = "b" * 32
    capture = _capture(
        foreign_ref,
        current_orders=[
            _current_order(
                foreign_ref,
            )
        ],
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="provider order reference mismatch",
    ):
        _resolve(plan, ledger, capture)


def test_reserved_attempt_cannot_claim_provider_effect(tmp_path: Path) -> None:
    plan, ledger, provider_ref = _prepared(
        tmp_path,
        submitted=False,
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="conflicts with durable attempt state",
    ):
        _resolve(
            plan,
            ledger,
            _capture(
                provider_ref,
                current_orders=[_current_order(provider_ref)],
            ),
        )


def test_cleared_price_requested_must_match_execution_action(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    capture = _capture(
        provider_ref,
        cleared_by_status={
            "SETTLED": [
                _cleared_order(
                    provider_ref,
                    price_requested=2.8,
                    price_matched=3.2,
                )
            ]
        },
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="requested price differs",
    ):
        _resolve(plan, ledger, capture)


def test_returned_row_must_keep_exact_customer_order_identity(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    capture = _capture(
        provider_ref,
        current_orders=[
            _current_order(
                "c" * 32,
            )
        ],
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="exact durable customer order reference",
    ):
        _resolve(plan, ledger, capture)


def test_multiple_provider_bet_ids_for_one_order_reference_fail_closed(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    capture = _capture(
        provider_ref,
        current_orders=[
            _current_order(
                provider_ref,
                bet_id="bet-current",
            )
        ],
        cleared_by_status={
            "SETTLED": [
                _cleared_order(
                    provider_ref,
                    bet_id="bet-cleared",
                    price_matched=3.2,
                    size_settled=10.0,
                )
            ]
        },
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="multiple bet ids",
    ):
        _resolve(plan, ledger, capture)


def test_forged_readback_copy_cannot_mint_realized_match_evidence(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    capture = _capture(
        provider_ref,
        current_orders=[_current_order(provider_ref)],
    )
    forged = replace(capture)

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="not canonical provider evidence",
    ):
        _resolve(plan, ledger, forged)


def test_copied_realized_match_result_is_not_authoritative(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    evidence = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            current_orders=[_current_order(provider_ref)],
        ),
    )
    copied = replace(evidence)

    evidence.assert_authoritative()
    with pytest.raises(
        RealizedMatchEvidenceError,
        match="not issued by canonical resolver",
    ):
        copied.assert_authoritative()


def test_provider_revision_advances_without_rewriting_decision_quote(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    transient = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            current_orders=[
                _current_order(
                    provider_ref,
                    average_price_matched=3.1,
                    size_matched=4.0,
                    size_remaining=6.0,
                )
            ],
            observed_at=datetime(
                2026,
                9,
                21,
                9,
                55,
                tzinfo=timezone.utc,
            ),
        ),
    )
    final = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            cleared_by_status={
                "SETTLED": [
                    _cleared_order(
                        provider_ref,
                        price_matched=3.2,
                        size_settled=10.0,
                    )
                ]
            },
            observed_at=datetime(
                2026,
                9,
                21,
                10,
                31,
                tzinfo=timezone.utc,
            ),
        ),
    )

    selected = validate_betfair_realized_match_revision(
        transient,
        final,
    )

    assert selected is final
    assert transient.requested_odds == final.requested_odds == Decimal("3.0")
    assert transient.provider_matched_odds == Decimal("3.1")
    assert final.provider_matched_odds == Decimal("3.2")
    assert transient.evidence_id != final.evidence_id
    assert transient.finalized is False
    assert final.finalized is True


def test_revision_rejects_decreasing_matched_stake(tmp_path: Path) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    previous = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            current_orders=[
                _current_order(
                    provider_ref,
                    average_price_matched=3.1,
                    size_matched=5.0,
                    size_remaining=5.0,
                )
            ],
            observed_at=datetime(
                2026,
                9,
                21,
                9,
                55,
                tzinfo=timezone.utc,
            ),
        ),
    )
    current = _resolve(
        plan,
        ledger,
        _capture(
            provider_ref,
            current_orders=[
                _current_order(
                    provider_ref,
                    average_price_matched=3.1,
                    size_matched=4.0,
                    size_remaining=6.0,
                )
            ],
            observed_at=datetime(
                2026,
                9,
                21,
                9,
                56,
                tzinfo=timezone.utc,
            ),
        ),
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="decreases matched stake",
    ):
        validate_betfair_realized_match_revision(
            previous,
            current,
        )


def test_current_order_matched_plus_remaining_cannot_exceed_request(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(tmp_path)
    capture = _capture(
        provider_ref,
        current_orders=[
            _current_order(
                provider_ref,
                size_matched=7.0,
                size_remaining=4.0,
            )
        ],
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="matched plus remaining",
    ):
        _resolve(plan, ledger, capture)


def test_decimal_identity_ignores_ambient_context_for_all_economic_fields(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref = _prepared(
        tmp_path,
        requested_odds="3.12345678",
        requested_stake="10.12345678",
    )
    current = [
        _current_order(
            provider_ref,
            price=3.12345678,
            requested_size=10.12345678,
            average_price_matched=3.23456789,
            size_matched=4.12345678,
            size_remaining=6.0,
        )
    ]

    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_DOWN
        low_precision = _resolve(
            plan,
            ledger,
            _capture(
                provider_ref,
                current_orders=current,
            ),
        )
        low_precision.assert_authoritative()

    with localcontext() as context:
        context.prec = 40
        context.rounding = ROUND_UP
        high_precision = _resolve(
            plan,
            ledger,
            _capture(
                provider_ref,
                current_orders=current,
            ),
        )
        high_precision.assert_authoritative()

    assert low_precision.evidence_id == high_precision.evidence_id
    assert low_precision.requested_odds == Decimal("3.12345678")
    assert low_precision.requested_stake == Decimal("10.12345678")
    assert low_precision.provider_matched_odds == Decimal("3.23456789")
    assert low_precision.provider_matched_stake == Decimal("4.12345678")

    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        low_precision.assert_authoritative()
        high_precision.assert_authoritative()


def test_exact_decimal_serializer_does_not_collapse_distinct_values() -> None:
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        first = realized_match._decimal_text(Decimal("1.23456789"))
        second = realized_match._decimal_text(Decimal("1.23556789"))

    assert first == "1.23456789"
    assert second == "1.23556789"
    assert first != second
