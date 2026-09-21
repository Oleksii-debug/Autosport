"""Dependent falsifier for PR #986 reconnect/resync causality."""

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


def test_reconnect_rejects_snapshot_received_before_disconnect_boundary() -> None:
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

    pre_disconnect_snapshot = ProviderResyncSnapshot(
        source_id="provider-a",
        sequence_id=20,
        evidence_id="snapshot-received-before-disconnect",
        received_monotonic_ns=90,
    )

    # A full snapshot that was already received before the disconnect/reconnect
    # boundary cannot prove that the post-disconnect provider gap has been healed.
    with pytest.raises((ValueError, RuntimeError)):
        accept_authoritative_snapshot(reconnecting, pre_disconnect_snapshot)


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
