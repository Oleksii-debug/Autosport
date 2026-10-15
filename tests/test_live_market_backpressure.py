from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.live_market_backpressure import (
    CompleteMarketSnapshot,
    LiveMarketBackpressure,
    LiveMarketBackpressureError,
    MarketStreamKey,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)


def key(provider: str = "betfair", market: str = "1.234") -> MarketStreamKey:
    return MarketStreamKey(provider, "prices", market)


def snap(
    sequence: int,
    *,
    generation: str = "g1",
    provider: str = "betfair",
    market: str = "1.234",
    state: str = "OPEN",
    digit: str | None = None,
    available_at: datetime | None = None,
) -> CompleteMarketSnapshot:
    return CompleteMarketSnapshot(
        key=key(provider, market),
        generation=generation,
        sequence=sequence,
        source_available_at=available_at or (T0 + timedelta(seconds=sequence)),
        payload_sha256=(digit or f"{sequence % 10:x}") * 64,
        market_state=state,
    )


def test_complete_same_generation_snapshots_latest_win_under_burst() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    for sequence in range(10_000):
        result = queue.submit_snapshot(snap(sequence))
        assert result.accepted is True
        assert result.gap is False
    views = queue.drain()
    assert len(views) == 1
    assert views[0].snapshot.sequence == 9_999
    assert views[0].trusted_current_view is True
    assert views[0].coalesced_snapshot_count == 9_999
    assert queue.stats.coalesced_snapshots == 9_999
    assert queue.stats.provider_gap_events == 0


def test_delta_is_never_latest_win_coalesced_and_gap_is_sticky() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    assert queue.submit_snapshot(snap(10)).accepted

    delta = queue.submit_delta(
        key=key(), generation="g1", sequence=11, delta_sha256="a" * 64
    )
    assert delta.accepted is False
    assert delta.resnapshot_required is True
    assert queue.pending_count == 0
    assert queue.requires_resnapshot(key()) is True

    later = queue.submit_snapshot(snap(12))
    assert later.accepted is False
    assert later.resnapshot_required is True
    assert queue.pending_count == 0

    recovered = queue.submit_snapshot(snap(13), recovery=True)
    assert recovered.accepted is True
    assert recovered.resnapshot_required is False
    assert queue.requires_resnapshot(key()) is False
    assert queue.drain()[0].snapshot.sequence == 13


def test_generation_change_requires_explicit_recovery_snapshot() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(100, generation="g1"))
    queue.drain()

    changed = queue.submit_snapshot(snap(1, generation="g2"))
    assert changed.accepted is False
    assert changed.gap is True

    recovered = queue.submit_snapshot(snap(1, generation="g2"), recovery=True)
    assert recovered.accepted is True
    view = queue.drain()[0]
    assert view.snapshot.generation == "g2"
    assert view.trusted_current_view is True


def test_out_of_order_complete_snapshot_cannot_regress_projection() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(10))
    old = queue.submit_snapshot(snap(9))
    assert old.accepted is False
    assert "out-of-order" in old.reason
    assert queue.drain()[0].snapshot.sequence == 10
    assert queue.stats.out_of_order_rejections == 1


def test_same_sequence_different_atomic_payload_forces_gap() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(10, digit="a"))
    conflict = queue.submit_snapshot(snap(10, digit="b"))
    assert conflict.accepted is False
    assert conflict.resnapshot_required is True
    assert queue.requires_resnapshot(key()) is True
    assert queue.stats.conflicting_sequence_events == 1


def test_open_suspended_open_burst_does_not_claim_causal_replay_safety() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(1, state="OPEN"))
    queue.submit_snapshot(snap(2, state="SUSPENDED"))
    queue.submit_snapshot(snap(3, state="OPEN"))
    view = queue.drain()[0]
    assert view.snapshot.market_state == "OPEN"
    assert view.trusted_current_view is True
    assert view.causal_replay_safe is False
    assert view.safety_transition_count == 2
    assert queue.stats.safety_transitions == 2


def test_local_capacity_drop_is_not_fabricated_provider_gap() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    assert queue.submit_snapshot(snap(1, market="1.1")).accepted
    dropped = queue.submit_snapshot(snap(1, market="1.2"))
    assert dropped.accepted is False
    assert dropped.local_capacity_drop is True
    assert dropped.gap is False
    assert queue.stats.local_capacity_drops == 1
    assert queue.stats.provider_gap_events == 0
    views = queue.drain()
    assert [view.snapshot.key.market_id for view in views] == ["1.1"]


def test_freshness_uses_source_time_not_queue_drain_time() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(1, available_at=T0))
    view = queue.drain()[0]
    assert view.is_fresh(now=T0 + timedelta(seconds=5), max_age_seconds=5)
    assert not view.is_fresh(now=T0 + timedelta(seconds=6), max_age_seconds=5)
    assert not view.is_fresh(now=T0 - timedelta(seconds=1), max_age_seconds=5)


def test_cross_provider_streams_are_independently_keyed() -> None:
    queue = LiveMarketBackpressure(capacity=2)
    queue.submit_snapshot(snap(7, provider="betfair"))
    queue.submit_snapshot(snap(1, provider="otherbook"))
    views = queue.drain()
    assert [(v.snapshot.key.provider_id, v.snapshot.sequence) for v in views] == [
        ("betfair", 7),
        ("otherbook", 1),
    ]


def test_invalid_boundary_values_fail_closed() -> None:
    with pytest.raises(LiveMarketBackpressureError):
        LiveMarketBackpressure(capacity=0)
    with pytest.raises(LiveMarketBackpressureError):
        MarketStreamKey(" betfair", "prices", "1.2")
    with pytest.raises(LiveMarketBackpressureError):
        snap(1, digit="Z")
    queue = LiveMarketBackpressure(capacity=1)
    with pytest.raises(LiveMarketBackpressureError):
        queue.submit_delta(
            key=key(), generation="g1", sequence=1, delta_sha256="not-a-digest"
        )


def test_drain_order_is_deterministic_and_does_not_make_replay_authority() -> None:
    queue = LiveMarketBackpressure(capacity=3)
    queue.submit_snapshot(snap(1, provider="z", market="2"))
    queue.submit_snapshot(snap(1, provider="a", market="9"))
    queue.submit_snapshot(snap(1, provider="a", market="1"))
    views = queue.drain()
    assert [(v.snapshot.key.provider_id, v.snapshot.key.market_id) for v in views] == [
        ("a", "1"),
        ("a", "9"),
        ("z", "2"),
    ]
    assert all(v.causal_replay_safe is False for v in views)


def test_existing_key_capacity_drop_requires_explicit_recovery_without_provider_gap() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(1, market="1.1", state="OPEN"))
    queue.drain()

    queue.submit_snapshot(snap(1, market="1.2"))
    dropped = queue.submit_snapshot(snap(2, market="1.1", state="SUSPENDED"))
    assert dropped.accepted is False
    assert dropped.local_capacity_drop is True
    assert dropped.gap is False
    assert dropped.resnapshot_required is True
    assert queue.requires_resnapshot(key(market="1.1")) is True
    assert queue.stats.local_capacity_drops == 1
    assert queue.stats.provider_gap_events == 0
    queue.drain()

    ordinary = queue.submit_snapshot(snap(3, market="1.1", state="OPEN"))
    assert ordinary.accepted is False
    assert ordinary.local_capacity_drop is False
    assert ordinary.gap is False
    assert ordinary.resnapshot_required is True
    assert queue.pending_count == 0

    recovered = queue.submit_snapshot(
        snap(3, market="1.1", state="OPEN"), recovery=True
    )
    assert recovered.accepted is True
    assert recovered.resnapshot_required is False
    assert queue.requires_resnapshot(key(market="1.1")) is False
    view = queue.drain()[0]
    assert view.snapshot.sequence == 3
    assert view.trusted_current_view is True
    assert view.resnapshot_required is False
    assert queue.stats.provider_gap_events == 0


def test_recovery_generation_change_respects_capacity_without_clearing_gap() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(5, market="1.1"))
    queue.drain()
    queue.submit_snapshot(snap(1, generation="g2", market="1.1"))
    assert queue.requires_resnapshot(key(market="1.1"))

    queue.submit_snapshot(snap(1, market="1.2"))
    dropped = queue.submit_snapshot(
        snap(2, generation="g2", market="1.1"), recovery=True
    )
    assert dropped.local_capacity_drop is True
    assert queue.requires_resnapshot(key(market="1.1"))
    queue.drain()

    recovered = queue.submit_snapshot(
        snap(2, generation="g2", market="1.1"), recovery=True
    )
    assert recovered.accepted is True
    assert queue.requires_resnapshot(key(market="1.1")) is False


def test_delta_generation_transition_resets_sequence_but_stays_gap() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(100, generation="g1"))
    queue.drain()
    queue.submit_delta(
        key=key(), generation="g2", sequence=1, delta_sha256="d" * 64
    )
    assert queue.requires_resnapshot(key())
    recovered = queue.submit_snapshot(snap(2, generation="g2"), recovery=True)
    assert recovered.accepted is True
    assert queue.drain()[0].snapshot.sequence == 2


def test_gap_can_recover_with_complete_snapshot_at_same_sequence() -> None:
    queue = LiveMarketBackpressure(capacity=1)
    queue.submit_snapshot(snap(10, digit="a"))
    queue.drain()
    queue.submit_delta(
        key=key(), generation="g1", sequence=11, delta_sha256="b" * 64
    )
    recovered = queue.submit_snapshot(snap(11, digit="c"), recovery=True)
    assert recovered.accepted is True
    assert recovered.gap is False
    view = queue.drain()[0]
    assert view.snapshot.sequence == 11
    assert view.snapshot.payload_sha256 == "c" * 64
    assert view.trusted_current_view is True
