import hashlib
import json
from decimal import Decimal

import pytest

from autosport.matchbook_market_reader import (
    MatchbookEnvironment,
    MatchbookExchangeType,
    MatchbookMarketReadError,
    MatchbookMarketReadScope,
    MatchbookMarketState,
    MatchbookOddsType,
    MatchbookPollCheckpoint,
    MatchbookPriceMode,
    MatchbookSide,
    compare_displayed_liquidity,
    parse_matchbook_price_snapshot,
    plan_matchbook_poll_batch,
    require_current_open_snapshot,
)


def scope(**overrides):
    values = dict(
        account_ref="acct-test-1",
        environment=MatchbookEnvironment.PRODUCTION,
        sport_id=15,
        event_id=101,
        market_id=202,
        runner_id=303,
        side=MatchbookSide.BACK,
        exchange_type=MatchbookExchangeType.BACK_LAY,
        odds_type=MatchbookOddsType.DECIMAL,
        currency="EUR",
        price_mode=MatchbookPriceMode.EXPANDED,
        depth=3,
        minimum_liquidity="2",
        exclude_mirrored_prices=True,
    )
    values.update(overrides)
    return MatchbookMarketReadScope(**values)


def raw(rows):
    return json.dumps({"prices": rows}, separators=(",", ":")).encode("utf-8")


def snapshot(s=None, rows=None, *, state=MatchbookMarketState.OPEN, observed="2026-09-23T00:00:00Z"):
    return parse_matchbook_price_snapshot(
        s or scope(),
        raw(rows or [{"side": "back", "odds": "2.5", "available-amount": "10"}]),
        observed_at=observed,
        market_state=state,
    )


def test_request_identity_is_explicit_and_deterministic():
    s = scope()
    assert s.request_target.startswith("/edge/rest/events/101/markets/202/runners/303/prices?")
    assert "exchange-type=back-lay" in s.request_target
    assert "odds-type=DECIMAL" in s.request_target
    assert "depth=3" in s.request_target
    assert "side=back" in s.request_target
    assert "currency=EUR" in s.request_target
    assert "minimum-liquidity=2" in s.request_target
    assert "price-mode=expanded" in s.request_target
    assert "exclude-mirrored-prices=true" in s.request_target
    assert len(s.scope_sha256) == 64
    assert s.scope_sha256 == scope().scope_sha256


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_ref", ""),
        ("sport_id", True),
        ("event_id", "001"),
        ("market_id", 0),
        ("runner_id", -1),
        ("depth", True),
        ("depth", 0),
        ("minimum_liquidity", 0),
        ("minimum_liquidity", 2.0),
        ("exclude_mirrored_prices", 1),
    ],
)
def test_implicit_or_lossy_scope_values_fail_closed(field, value):
    with pytest.raises(MatchbookMarketReadError):
        scope(**{field: value})


def test_exchange_and_side_must_match():
    with pytest.raises(MatchbookMarketReadError):
        scope(exchange_type=MatchbookExchangeType.BINARY, side=MatchbookSide.BACK)
    binary = scope(
        exchange_type=MatchbookExchangeType.BINARY,
        side=MatchbookSide.WIN,
        exclude_mirrored_prices=False,
    )
    assert binary.side is MatchbookSide.WIN


def test_non_decimal_odds_formats_fail_closed_until_exact_parser_exists():
    for odds_type in (
        MatchbookOddsType.US,
        MatchbookOddsType.HK,
        MatchbookOddsType.MALAY,
        MatchbookOddsType.INDO,
        MatchbookOddsType.PERCENT,
    ):
        with pytest.raises(MatchbookMarketReadError, match="DECIMAL odds only"):
            scope(odds_type=odds_type)


def test_query_semantics_change_snapshot_identity():
    base = snapshot()
    variants = [
        scope(depth=4),
        scope(minimum_liquidity="3"),
        scope(price_mode=MatchbookPriceMode.AGGREGATED),
        scope(currency="GBP"),
        scope(exclude_mirrored_prices=False),
    ]
    for variant in variants:
        other = snapshot(variant)
        assert other.scope.scope_sha256 != base.scope.scope_sha256
        assert other.snapshot_sha256 != base.snapshot_sha256


def test_strict_json_duplicate_key_and_nonfinite_fail_closed():
    s = scope()
    with pytest.raises(MatchbookMarketReadError, match="duplicate JSON key"):
        parse_matchbook_price_snapshot(
            s,
            b'{"prices":[],"prices":[]}',
            observed_at="2026-09-23T00:00:00Z",
            market_state=MatchbookMarketState.OPEN,
        )
    with pytest.raises(MatchbookMarketReadError, match="non-standard JSON"):
        parse_matchbook_price_snapshot(
            s,
            b'{"prices":[{"side":"back","odds":NaN,"available-amount":10}]}',
            observed_at="2026-09-23T00:00:00Z",
            market_state=MatchbookMarketState.OPEN,
        )


def test_numeric_json_is_parsed_exactly_not_via_binary_float():
    snap = parse_matchbook_price_snapshot(
        scope(minimum_liquidity="0.1"),
        b'{"prices":[{"side":"back","odds":2.3,"available-amount":0.1}]}',
        observed_at="2026-09-23T00:00:00Z",
        market_state=MatchbookMarketState.OPEN,
    )
    assert snap.prices[0].odds == Decimal("2.3")
    assert snap.prices[0].available_amount == Decimal("0.1")


def test_aggregated_third_price_is_never_raw_depth():
    s = scope(price_mode=MatchbookPriceMode.AGGREGATED, depth=20)
    snap = snapshot(
        s,
        rows=[
            {"side": "back", "odds": "2.0", "available-amount": "2"},
            {"side": "back", "odds": "1.9", "available-amount": "3"},
            {"side": "back", "odds": "1.8", "available-amount": "50"},
        ],
    )
    assert not snap.raw_depth_qualified
    assert not snap.prices[0].aggregated_tail
    assert not snap.prices[1].aggregated_tail
    assert snap.prices[2].aggregated_tail
    with pytest.raises(MatchbookMarketReadError, match="not raw market-depth"):
        snap.require_raw_depth()


def test_aggregated_response_cannot_exceed_three_levels():
    s = scope(price_mode=MatchbookPriceMode.AGGREGATED, depth=20)
    rows = [
        {"side": "back", "odds": str(2 + i / 10), "available-amount": "2"}
        for i in range(4)
    ]
    with pytest.raises(MatchbookMarketReadError, match="more price rows"):
        snapshot(s, rows=rows)


def test_expanded_depth_is_available_as_raw_depth_evidence():
    snap = snapshot(
        rows=[
            {"side": "back", "odds": "2.0", "available-amount": "2"},
            {"side": "back", "odds": "1.9", "available-amount": "3"},
        ]
    )
    assert snap.require_raw_depth() == snap.prices


def test_side_and_minimum_liquidity_are_enforced_from_response():
    with pytest.raises(MatchbookMarketReadError, match="outside explicit request scope"):
        snapshot(rows=[{"side": "lay", "odds": "2.5", "available-amount": "10"}])
    with pytest.raises(MatchbookMarketReadError, match="below explicit minimum"):
        snapshot(rows=[{"side": "back", "odds": "2.5", "available-amount": "1.99"}])


def test_duplicate_price_rows_fail_closed():
    row = {"side": "back", "odds": "2.5", "available-amount": "10"}
    with pytest.raises(MatchbookMarketReadError, match="duplicate exact price row"):
        snapshot(rows=[row, row])


def test_suspended_market_is_observable_but_not_actionable():
    snap = snapshot(state=MatchbookMarketState.SUSPENDED)
    assert not snap.actionable_market_state
    with pytest.raises(MatchbookMarketReadError, match="not actionable"):
        require_current_open_snapshot(
            snap,
            as_of="2026-09-23T00:00:01Z",
            max_age_seconds=5,
        )


def test_unknown_market_state_cannot_be_smuggled_in():
    with pytest.raises(MatchbookMarketReadError, match="market_state"):
        parse_matchbook_price_snapshot(
            scope(),
            raw([]),
            observed_at="2026-09-23T00:00:00Z",
            market_state="mystery",  # type: ignore[arg-type]
        )


def test_future_and_stale_observations_fail_currentness_gate():
    future = snapshot(observed="2026-09-23T00:00:10Z")
    with pytest.raises(MatchbookMarketReadError, match="future"):
        require_current_open_snapshot(
            future,
            as_of="2026-09-23T00:00:09Z",
            max_age_seconds=30,
        )
    stale = snapshot(observed="2026-09-23T00:00:00Z")
    with pytest.raises(MatchbookMarketReadError, match="stale"):
        require_current_open_snapshot(
            stale,
            as_of="2026-09-23T00:01:01Z",
            max_age_seconds=60,
        )


def test_local_receipt_time_is_not_invented_provider_event_time():
    snap = snapshot()
    assert snap.observed_at == "2026-09-23T00:00:00Z"
    assert snap.provider_origin == "matchbook"
    assert not snap.provider_origin_verified
    assert snap.provider_event_time is None
    assert not snap.atomic_market_snapshot
    assert not snap.authoritative_absence


def test_binary_or_mirrored_read_never_mints_write_authority():
    s = scope(
        exchange_type=MatchbookExchangeType.BINARY,
        side=MatchbookSide.WIN,
        exclude_mirrored_prices=False,
    )
    snap = snapshot(s, rows=[{"side": "win", "odds": "0.55", "available-amount": "5"}])
    assert not snap.provider_origin_verified
    assert not snap.provider_write_authority
    assert not snap.execution_authority


def test_cross_currency_liquidity_requires_separate_fx_authority():
    eur = snapshot(scope(currency="EUR"))
    gbp = snapshot(scope(currency="GBP"))
    with pytest.raises(MatchbookMarketReadError, match="FX authority"):
        compare_displayed_liquidity(eur, gbp)


def test_same_currency_displayed_liquidity_delta_is_exact_decimal():
    left = snapshot(rows=[{"side": "back", "odds": "2", "available-amount": "2.25"}])
    right = snapshot(rows=[{"side": "back", "odds": "2", "available-amount": "5.75"}])
    assert compare_displayed_liquidity(left, right) == Decimal("3.50")


def test_poll_plan_is_bounded_deterministic_and_resumable():
    scopes = [hashlib.sha256(f"scope-{i}".encode()).hexdigest() for i in range(10)]
    first = plan_matchbook_poll_batch(scopes, request_budget=3)
    again = plan_matchbook_poll_batch(list(reversed(scopes)), request_budget=3)
    assert first.scope_sha256s == again.scope_sha256s
    assert len(first.scope_sha256s) == 3
    second = plan_matchbook_poll_batch(
        scopes,
        request_budget=3,
        checkpoint=first.checkpoint,
    )
    assert not set(first.scope_sha256s).intersection(second.scope_sha256s)
    assert len(second.scope_sha256s) == 3


def test_poll_plan_never_expands_one_restart_call_beyond_budget():
    scopes = [hashlib.sha256(f"scope-{i}".encode()).hexdigest() for i in range(500)]
    fresh_restart = plan_matchbook_poll_batch(scopes, request_budget=7)
    assert len(fresh_restart.scope_sha256s) == 7
    assert fresh_restart.universe_size == 500
    assert fresh_restart.request_budget == 7


def test_poll_checkpoint_rejects_universe_drift():
    scopes = [hashlib.sha256(f"scope-{i}".encode()).hexdigest() for i in range(4)]
    first = plan_matchbook_poll_batch(scopes, request_budget=2)
    changed = scopes + [hashlib.sha256(b"new").hexdigest()]
    with pytest.raises(MatchbookMarketReadError, match="does not match current universe"):
        plan_matchbook_poll_batch(
            changed,
            request_budget=2,
            checkpoint=first.checkpoint,
        )


def test_invalid_checkpoint_shape_is_fail_closed():
    with pytest.raises(MatchbookMarketReadError):
        MatchbookPollCheckpoint("x" * 64, 0, 0)


def test_raw_response_digest_and_snapshot_identity_are_bound_to_bytes():
    a = snapshot(rows=[{"side": "back", "odds": "2.5", "available-amount": "10"}])
    b = parse_matchbook_price_snapshot(
        scope(),
        b'{"prices": [{"side":"back","odds":"2.5","available-amount":"10"}]}',
        observed_at="2026-09-23T00:00:00Z",
        market_state=MatchbookMarketState.OPEN,
    )
    assert a.prices == b.prices
    assert a.raw_response_sha256 != b.raw_response_sha256
    assert a.snapshot_sha256 != b.snapshot_sha256
