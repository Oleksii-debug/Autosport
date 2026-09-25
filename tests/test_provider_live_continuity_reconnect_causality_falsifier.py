"""Reconnect/resync causality regressions for provider live continuity."""

import pytest

from autosport.provider_live_continuity import (
    ProviderContinuityState,
    ProviderContinuityStatus,
    ProviderResyncSnapshot,
    accept_authoritative_snapshot,
    begin_reconnect,
    continuity_gate,
    mark_disconnected,
)


def _synchronized_state() -> ProviderContinuityState:
    initial = ProviderContinuityState.initial(
        "provider-a",
        max_silence_ns=1_000,
    )
    return accept_authoritative_snapshot(
        initial,
        ProviderResyncSnapshot(
            source_id="provider-a",
            sequence_id=10,
            evidence_id="snapshot-generation-1",
            received_monotonic_ns=10,
        ),
    )


def _reconnecting_state() -> ProviderContinuityState:
    synchronized = _synchronized_state()
    disconnected = mark_disconnected(
        synchronized,
        observed_monotonic_ns=100,
    )
    reconnecting = begin_reconnect(
        disconnected,
        observed_monotonic_ns=110,
    )
    assert reconnecting.status is ProviderContinuityStatus.AWAITING_RESYNC
    assert reconnecting.resync_not_before_monotonic_ns == 110
    return reconnecting


def test_reconnect_rejects_snapshot_received_before_disconnect_boundary() -> None:
    reconnecting = _reconnecting_state()
    pre_disconnect_snapshot = ProviderResyncSnapshot(
        source_id="provider-a",
        sequence_id=20,
        evidence_id="snapshot-received-before-disconnect",
        received_monotonic_ns=90,
    )

    with pytest.raises((ValueError, RuntimeError), match="reconnect|regressed"):
        accept_authoritative_snapshot(reconnecting, pre_disconnect_snapshot)


def test_reconnect_rejects_snapshot_between_disconnect_and_reconnect() -> None:
    reconnecting = _reconnecting_state()
    stale_during_gap = ProviderResyncSnapshot(
        source_id="provider-a",
        sequence_id=20,
        evidence_id="snapshot-received-before-reconnect",
        received_monotonic_ns=105,
    )

    with pytest.raises(ValueError, match="reconnect resync boundary"):
        accept_authoritative_snapshot(reconnecting, stale_during_gap)


def test_reconnect_accepts_snapshot_received_at_reconnect_boundary() -> None:
    reconnecting = _reconnecting_state()
    repaired = accept_authoritative_snapshot(
        reconnecting,
        ProviderResyncSnapshot(
            source_id="provider-a",
            sequence_id=20,
            evidence_id="snapshot-received-at-reconnect",
            received_monotonic_ns=110,
        ),
    )

    gate = continuity_gate(repaired, now_monotonic_ns=111)
    assert repaired.status is ProviderContinuityStatus.SYNCHRONIZED
    assert repaired.resync_not_before_monotonic_ns is None
    assert gate.actionable is True


def test_reconnect_accepts_snapshot_received_after_reconnect_boundary() -> None:
    synchronized = _synchronized_state()
    disconnected = mark_disconnected(
        synchronized,
        observed_monotonic_ns=100,
    )
    reconnecting = begin_reconnect(
        disconnected,
        observed_monotonic_ns=110,
    )

    repaired = accept_authoritative_snapshot(
        reconnecting,
        ProviderResyncSnapshot(
            source_id="provider-a",
            sequence_id=20,
            evidence_id="snapshot-received-after-reconnect",
            received_monotonic_ns=120,
        ),
    )
    gate = continuity_gate(repaired, now_monotonic_ns=121)

    assert repaired.generation == synchronized.generation + 1
    assert repaired.status is ProviderContinuityStatus.SYNCHRONIZED
    assert gate.actionable is True
