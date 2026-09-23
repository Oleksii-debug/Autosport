"""Falsify caller-reset of Matchbook's persisted per-minute poll budget."""

from __future__ import annotations

import pytest

from autosport.matchbook_market_reader import (
    MatchbookEnvironment,
    MatchbookExchangeType,
    MatchbookMarketReadError,
    MatchbookMarketReadRequest,
    MatchbookPollCursor,
    MatchbookPriceMode,
    MatchbookSide,
    plan_matchbook_poll,
)


def _request(runner_id: int) -> MatchbookMarketReadRequest:
    return MatchbookMarketReadRequest(
        account_identity="acct-01",
        environment=MatchbookEnvironment.PRODUCTION,
        sport_id=15,
        event_id=101,
        market_id=202,
        runner_id=runner_id,
        side=MatchbookSide.BACK,
        exchange_type=MatchbookExchangeType.BACK_LAY,
        odds_type="DECIMAL",
        currency="GBP",
        price_mode=MatchbookPriceMode.EXPANDED,
        depth=1,
        minimum_liquidity="1",
        exclude_mirrored_prices=True,
    )


def test_caller_cannot_reset_current_window_budget_with_forged_cursor() -> None:
    requests = tuple(_request(index) for index in range(1, 701))
    now = "2026-09-23T00:00:00Z"

    exhausted = plan_matchbook_poll(
        requests,
        now=now,
        cursor=None,
        max_requests_per_minute=700,
        batch_limit=700,
    )
    assert len(exhausted.request_sha256s) == 700
    assert exhausted.next_cursor.used_in_window == 700

    forged_reset = MatchbookPollCursor(
        universe_sha256=exhausted.next_cursor.universe_sha256,
        next_index=exhausted.next_cursor.next_index,
        window_started_at=exhausted.next_cursor.window_started_at,
        used_in_window=0,
    )

    with pytest.raises(MatchbookMarketReadError):
        plan_matchbook_poll(
            requests,
            now=now,
            cursor=forged_reset,
            max_requests_per_minute=700,
            batch_limit=1,
        )
