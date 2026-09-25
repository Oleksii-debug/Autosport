from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext

import pytest

from autosport.execution_timing_evidence import (
    ExecutionTimingEvidenceError,
    LocalMonotonicTimingSession,
    MonotonicTimingIntervalEvidence,
    MonotonicTimingMarker,
)


def _sequence(*values: int):
    iterator = iter(values)
    return lambda: next(iterator)


def _wall_sequence(*values: datetime):
    iterator = iter(values)
    return lambda: next(iterator)


def _session(*, monotonic_ns, wall_now=None, suffix: str = "a"):
    return LocalMonotonicTimingSession(
        monotonic_ns=monotonic_ns,
        wall_now=wall_now or (lambda: datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)),
        clock_domain_id=f"perf-counter:{suffix}",
        session_id=f"execution-session:{suffix}",
        issuer_epoch_id=f"issuer-epoch:{suffix}",
    )


def test_same_session_interval_uses_exact_monotonic_duration() -> None:
    session = _session(monotonic_ns=_sequence(1_000_000_000, 1_002_500_000))

    start = session.mark("submit_start")
    end = session.mark("local_response")
    evidence = session.interval(start, end)

    assert evidence.duration_ns == 2_500_000
    assert evidence.duration_ms == Decimal("2.5")
    assert evidence.timing_class == "LOCAL_MONOTONIC_SAME_SESSION"
    assert len(evidence.evidence_id) == 64
    assert evidence.start_marker_id == start.marker_id
    assert evidence.end_marker_id == end.marker_id
    assert session.require_issued_interval(evidence) is evidence


def test_duration_ms_is_exact_independent_of_ambient_decimal_context() -> None:
    evidence = MonotonicTimingIntervalEvidence(
        clock_domain_id="perf-counter:a",
        session_id="execution-session:a",
        issuer_epoch_id="issuer-epoch:a",
        start_marker_id="start-marker",
        end_marker_id="end-marker",
        start_sequence=1,
        end_sequence=2,
        duration_ns=123_456_789,
    )

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        rounded_down_context = evidence.duration_ms

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        rounded_up_context = evidence.duration_ms

    assert rounded_down_context == Decimal("123.456789")
    assert rounded_up_context == Decimal("123.456789")


def test_wall_clock_regression_cannot_create_negative_or_favorable_latency() -> None:
    wall_now = _wall_sequence(
        datetime(2026, 9, 23, 9, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 23, 8, 59, 59, tzinfo=timezone.utc),
    )
    session = _session(
        monotonic_ns=_sequence(5_000_000, 8_000_000),
        wall_now=wall_now,
    )

    start = session.mark("request_start")
    end = session.mark("response_end")
    evidence = session.interval(start, end)

    assert start.wall_anchor_at > end.wall_anchor_at
    assert evidence.duration_ns == 3_000_000


def test_monotonic_clock_regression_fails_instead_of_clamping() -> None:
    session = _session(monotonic_ns=_sequence(100, 99))
    session.mark("first")

    with pytest.raises(ExecutionTimingEvidenceError, match="monotonic clock regressed"):
        session.mark("second")


def test_concurrent_marker_commit_cannot_lower_monotonic_high_water() -> None:
    high_wall_entered = threading.Event()
    low_wall_entered = threading.Event()
    release_high = threading.Event()
    release_low = threading.Event()

    def monotonic_ns() -> int:
        name = threading.current_thread().name
        if name == "high-worker":
            return 200
        if name == "low-worker":
            return 100
        return 150

    def wall_now() -> datetime:
        name = threading.current_thread().name
        if name == "high-worker":
            high_wall_entered.set()
            assert release_high.wait(timeout=2)
        elif name == "low-worker":
            low_wall_entered.set()
            assert release_low.wait(timeout=2)
        return datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)

    session = _session(monotonic_ns=monotonic_ns, wall_now=wall_now)
    outcomes: dict[str, object] = {}

    def issue(key: str) -> None:
        try:
            outcomes[key] = session.mark(key)
        except Exception as exc:
            outcomes[key] = exc

    high = threading.Thread(target=issue, args=("high",), name="high-worker")
    low = threading.Thread(target=issue, args=("low",), name="low-worker")

    high.start()
    assert high_wall_entered.wait(timeout=2)
    low.start()
    assert low_wall_entered.wait(timeout=2)

    # Both calls have sampled their counters before either can commit. Force
    # the higher sample to commit first, then let the stale lower sample try.
    release_high.set()
    high.join(timeout=2)
    assert not high.is_alive()

    release_low.set()
    low.join(timeout=2)
    assert not low.is_alive()

    issued_high = outcomes["high"]
    assert isinstance(issued_high, MonotonicTimingMarker)
    assert issued_high.sequence == 1
    assert issued_high.monotonic_ns == 200

    rejected_low = outcomes["low"]
    assert isinstance(rejected_low, ExecutionTimingEvidenceError)
    assert "monotonic clock regressed" in str(rejected_low)

    # The rejected lower sample must not lower the committed high-water mark.
    with pytest.raises(ExecutionTimingEvidenceError, match="monotonic clock regressed"):
        session.mark("after-race")


def test_cross_session_markers_cannot_be_subtracted() -> None:
    first = _session(monotonic_ns=_sequence(100), suffix="first")
    second = _session(monotonic_ns=_sequence(200), suffix="second")

    start = first.mark("start")
    end = second.mark("end")

    with pytest.raises(ExecutionTimingEvidenceError, match="another clock/session"):
        first.interval(start, end)


def test_restart_cannot_reuse_old_marker_even_with_same_public_ids() -> None:
    old = _session(monotonic_ns=_sequence(100, 200), suffix="stable")
    old_start = old.mark("start")
    old.mark("end")

    restarted = _session(monotonic_ns=_sequence(300), suffix="stable")
    new_end = restarted.mark("post_restart_end")

    with pytest.raises(ExecutionTimingEvidenceError, match="not product-issued"):
        restarted.interval(old_start, new_end)


def test_tampered_marker_cannot_mint_interval() -> None:
    session = _session(monotonic_ns=_sequence(100, 200))
    start = session.mark("start")
    end = session.mark("end")
    forged = replace(end, monotonic_ns=150)

    with pytest.raises(ExecutionTimingEvidenceError, match="not product-issued"):
        session.interval(start, forged)


def test_caller_constructed_marker_is_not_issuance_authority() -> None:
    session = _session(monotonic_ns=_sequence(100, 200))
    start = session.mark("start")
    end = MonotonicTimingMarker(
        clock_domain_id=session.clock_domain_id,
        session_id=session.session_id,
        issuer_epoch_id=session.issuer_epoch_id,
        sequence=2,
        label="end",
        monotonic_ns=200,
        wall_anchor_at="2026-09-23T09:00:00+00:00",
    )

    with pytest.raises(ExecutionTimingEvidenceError, match="not product-issued"):
        session.interval(start, end)


def test_caller_constructed_interval_is_not_positive_authority() -> None:
    session = _session(monotonic_ns=_sequence(100, 200))
    start = session.mark("start")
    end = session.mark("end")
    forged = MonotonicTimingIntervalEvidence(
        clock_domain_id=session.clock_domain_id,
        session_id=session.session_id,
        issuer_epoch_id=session.issuer_epoch_id,
        start_marker_id=start.marker_id,
        end_marker_id=end.marker_id,
        start_sequence=start.sequence,
        end_sequence=end.sequence,
        duration_ns=1,
    )

    with pytest.raises(ExecutionTimingEvidenceError, match="not product-issued"):
        session.require_issued_interval(forged)


def test_tampered_issued_interval_cannot_be_re_resolved() -> None:
    session = _session(monotonic_ns=_sequence(100, 200))
    start = session.mark("start")
    end = session.mark("end")
    issued = session.interval(start, end)

    with pytest.raises(ExecutionTimingEvidenceError, match="not product-issued"):
        session.require_issued_interval(replace(issued, duration_ns=99))


def test_reversed_markers_fail_causal_order() -> None:
    session = _session(monotonic_ns=_sequence(100, 200))
    first = session.mark("first")
    second = session.mark("second")

    with pytest.raises(ExecutionTimingEvidenceError, match="causally follow"):
        session.interval(second, first)


def test_zero_elapsed_time_is_exact_when_clock_counter_is_equal() -> None:
    session = _session(monotonic_ns=_sequence(42, 42))

    start = session.mark("start")
    end = session.mark("end")

    assert session.interval(start, end).duration_ns == 0


def test_interval_identity_is_deterministic_for_same_issued_markers() -> None:
    session = _session(monotonic_ns=_sequence(10, 20))
    start = session.mark("start")
    end = session.mark("end")

    first = session.interval(start, end)
    second = session.interval(start, end)

    assert first == second
    assert first.evidence_id == second.evidence_id
    assert session.require_issued_interval(second) == first


def test_bool_is_not_accepted_as_monotonic_integer() -> None:
    session = _session(monotonic_ns=lambda: True)

    with pytest.raises(ExecutionTimingEvidenceError, match="non-negative integer"):
        session.mark("start")


def test_naive_wall_anchor_fails_closed() -> None:
    session = _session(
        monotonic_ns=_sequence(100),
        wall_now=lambda: datetime(2026, 9, 23, 9, 0),
    )

    with pytest.raises(ExecutionTimingEvidenceError, match="timezone-aware"):
        session.mark("start")
