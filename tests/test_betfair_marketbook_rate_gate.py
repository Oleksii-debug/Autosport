from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.betfair_marketbook_rate_gate import (
    BETFAIR_MARKETBOOK_RATE_POLICY_VERSION,
    BetfairMarketBookPerMarketRateGate,
    MarketBookRateGateState,
    MarketBookRateWindowState,
)


T0 = datetime(2026, 9, 22, 0, 0, 0, tzinfo=timezone.utc)


def test_five_calls_allowed_and_sixth_is_denied_with_exact_next_time() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        decision = gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=100 * index))
        assert decision.allowed is True

    denied = gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=500))
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("1.234",)
    assert denied.next_eligible_at == T0 + timedelta(seconds=1)


def test_exact_one_second_boundary_expires_oldest_reservation() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=100 * index)).allowed

    assert gate.reserve(("1.234",), scheduled_at=T0 + timedelta(seconds=1)).allowed


def test_batch_consumes_one_call_for_every_market() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.1", "1.2"), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    denied = gate.reserve(("1.1", "1.2"), scheduled_at=T0 + timedelta(milliseconds=10))
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("1.1", "1.2")


def test_mixed_batch_denial_is_atomic() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    before = gate.snapshot()
    denied = gate.reserve(("cold", "hot"), scheduled_at=T0 + timedelta(milliseconds=10))
    after = gate.snapshot()

    assert denied.allowed is False
    assert denied.blocked_market_ids == ("hot",)
    assert all(window.market_id != "cold" for window in after.markets)
    assert tuple((m.market_id, m.accepted_at_utc_us) for m in after.markets) == tuple(
        (m.market_id, m.accepted_at_utc_us) for m in before.markets
    )


def test_hot_market_does_not_block_unrelated_market() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    assert gate.reserve(("cold",), scheduled_at=T0 + timedelta(milliseconds=10)).allowed


def test_denial_reports_latest_next_eligible_for_multiple_blockers() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("a",), scheduled_at=T0 + timedelta(milliseconds=10 * index)).allowed
    for index in range(5):
        assert gate.reserve(("b",), scheduled_at=T0 + timedelta(milliseconds=100 + 10 * index)).allowed

    denied = gate.reserve(("a", "b"), scheduled_at=T0 + timedelta(milliseconds=200))
    assert denied.allowed is False
    assert denied.blocked_market_ids == ("a", "b")
    assert denied.next_eligible_at == T0 + timedelta(milliseconds=1100)


def test_restart_snapshot_preserves_admission_decision() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.234",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed

    restored = BetfairMarketBookPerMarketRateGate(gate.snapshot())
    when = T0 + timedelta(milliseconds=20)
    assert gate.reserve(("1.234",), scheduled_at=when) == restored.reserve(
        ("1.234",), scheduled_at=when
    )
    assert gate.snapshot() == restored.snapshot()


def test_timezone_equivalent_instants_have_same_decision_time() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    plus_two = timezone(timedelta(hours=2))
    first = gate.reserve(("1.1",), scheduled_at=T0)
    second = gate.reserve(("1.2",), scheduled_at=T0.astimezone(plus_two))
    assert first.scheduled_at_utc_us == second.scheduled_at_utc_us


def test_scheduled_time_cannot_move_backwards() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    assert gate.reserve(("1.1",), scheduled_at=T0 + timedelta(seconds=1)).allowed
    with pytest.raises(ValueError, match="must not move backwards"):
        gate.reserve(("1.2",), scheduled_at=T0)


@pytest.mark.parametrize("bad", ["1.1", b"1.1"])
def test_market_id_collection_cannot_be_scalar_text(bad: object) -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises(TypeError):
        gate.reserve(bad, scheduled_at=T0)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [(), ("",), (" 1.1",), ("1.1 ",), (1,)])
def test_invalid_market_ids_fail_closed(bad: object) -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises((TypeError, ValueError)):
        gate.reserve(bad, scheduled_at=T0)  # type: ignore[arg-type]


def test_duplicate_market_in_one_batch_is_rejected_without_state_change() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises(ValueError, match="duplicate"):
        gate.reserve(("1.1", "1.1"), scheduled_at=T0)
    assert gate.snapshot().markets == ()


def test_naive_time_is_rejected() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    with pytest.raises(ValueError, match="timezone-aware"):
        gate.reserve(("1.1",), scheduled_at=datetime(2026, 9, 22))


def test_snapshot_is_canonical_sorted_and_prunes_expired_markets() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    assert gate.reserve(("z", "a"), scheduled_at=T0).allowed
    assert [item.market_id for item in gate.snapshot().markets] == ["a", "z"]

    assert gate.reserve(("m",), scheduled_at=T0 + timedelta(seconds=2)).allowed
    assert [item.market_id for item in gate.snapshot().markets] == ["m"]


def test_snapshot_has_bounded_per_market_history() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("1.1",), scheduled_at=T0 + timedelta(microseconds=index)).allowed
    state = gate.snapshot()
    assert len(state.markets[0].accepted_at_utc_us) == 5


def test_state_rejects_wrong_policy_version() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        MarketBookRateGateState("wrong", None, ())


def test_state_rejects_more_than_five_recent_reservations() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        MarketBookRateWindowState("1.1", (1, 2, 3, 4, 5, 6))


def test_state_rejects_unsorted_or_duplicate_market_windows() -> None:
    a = MarketBookRateWindowState("a", (1,))
    b = MarketBookRateWindowState("b", (1,))
    with pytest.raises(ValueError, match="sorted"):
        MarketBookRateGateState(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION, 1, (b, a))
    with pytest.raises(ValueError, match="duplicate"):
        MarketBookRateGateState(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION, 1, (a, a))


def test_state_rejects_reservation_after_last_scheduled_time() -> None:
    window = MarketBookRateWindowState("a", (2,))
    with pytest.raises(ValueError, match="after last"):
        MarketBookRateGateState(BETFAIR_MARKETBOOK_RATE_POLICY_VERSION, 1, (window,))


def test_denied_reservation_advances_causal_time_but_adds_no_call() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed
    assert not gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=10)).allowed
    state = gate.snapshot()
    hot = next(item for item in state.markets if item.market_id == "hot")
    assert len(hot.accepted_at_utc_us) == 5
    assert state.last_scheduled_at_utc_us == int(
        (T0 + timedelta(milliseconds=10) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        * 1_000_000
    )


def test_blocked_batch_does_not_consume_unblocked_market_capacity() -> None:
    gate = BetfairMarketBookPerMarketRateGate()
    for index in range(5):
        assert gate.reserve(("hot",), scheduled_at=T0 + timedelta(milliseconds=index)).allowed
    assert not gate.reserve(("cold", "hot"), scheduled_at=T0 + timedelta(milliseconds=10)).allowed

    for offset in range(5):
        assert gate.reserve(("cold",), scheduled_at=T0 + timedelta(milliseconds=20 + offset)).allowed
    assert not gate.reserve(("cold",), scheduled_at=T0 + timedelta(milliseconds=30)).allowed
