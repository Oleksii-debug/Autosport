from __future__ import annotations

import unittest
from dataclasses import replace

from autosport.provider_live_continuity import (
    ProviderContinuityOutcome,
    ProviderContinuityState,
    ProviderContinuityStatus,
    ProviderResyncSnapshot,
    ProviderStreamDelta,
    accept_authoritative_snapshot,
    accept_delta,
    begin_reconnect,
    continuity_gate,
    mark_disconnected,
)


class ProviderLiveContinuityTests(unittest.TestCase):
    def initial(self) -> ProviderContinuityState:
        return ProviderContinuityState.initial("provider-a", max_silence_ns=1_000)

    @staticmethod
    def snapshot(
        *,
        sequence_id: int = 10,
        evidence_id: str = "snapshot-10",
        received_monotonic_ns: int = 100,
        source_id: str = "provider-a",
    ) -> ProviderResyncSnapshot:
        return ProviderResyncSnapshot(
            source_id=source_id,
            sequence_id=sequence_id,
            evidence_id=evidence_id,
            received_monotonic_ns=received_monotonic_ns,
        )

    @staticmethod
    def delta(
        *,
        sequence_id: int,
        evidence_id: str,
        received_monotonic_ns: int,
        source_id: str = "provider-a",
    ) -> ProviderStreamDelta:
        return ProviderStreamDelta(
            source_id=source_id,
            sequence_id=sequence_id,
            evidence_id=evidence_id,
            received_monotonic_ns=received_monotonic_ns,
        )

    def synchronized(self) -> ProviderContinuityState:
        return accept_authoritative_snapshot(self.initial(), self.snapshot())

    def test_initial_state_blocks_delta_until_authoritative_snapshot(self) -> None:
        state = self.initial()
        transition = accept_delta(
            state,
            self.delta(sequence_id=1, evidence_id="delta-1", received_monotonic_ns=1),
        )

        self.assertEqual(transition.outcome, ProviderContinuityOutcome.RESYNC_REQUIRED)
        self.assertIs(transition.state, state)
        self.assertFalse(continuity_gate(state, now_monotonic_ns=1).actionable)

    def test_snapshot_starts_generation_and_makes_bounded_state_actionable(self) -> None:
        state = self.synchronized()
        gate = continuity_gate(state, now_monotonic_ns=1_100)

        self.assertEqual(state.generation, 1)
        self.assertEqual(state.status, ProviderContinuityStatus.SYNCHRONIZED)
        self.assertEqual(state.last_sequence_id, 10)
        self.assertEqual(state.last_evidence_kind, "snapshot")
        self.assertTrue(gate.actionable)
        self.assertEqual(gate.silence_ns, 1_000)

    def test_sequential_delta_advances_exact_provider_sequence(self) -> None:
        transition = accept_delta(
            self.synchronized(),
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=200),
        )

        self.assertEqual(transition.outcome, ProviderContinuityOutcome.APPLIED)
        self.assertEqual(transition.expected_sequence_id, 11)
        self.assertEqual(transition.state.last_sequence_id, 11)
        self.assertEqual(transition.state.last_evidence_id, "delta-11")
        self.assertEqual(transition.state.last_received_monotonic_ns, 200)
        self.assertEqual(transition.state.generation, 1)

    def test_exact_duplicate_is_idempotent_and_cannot_refresh_staleness_clock(self) -> None:
        applied = accept_delta(
            self.synchronized(),
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=200),
        ).state
        duplicate = accept_delta(
            applied,
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=900),
        )

        self.assertEqual(duplicate.outcome, ProviderContinuityOutcome.DUPLICATE)
        self.assertIs(duplicate.state, applied)
        self.assertEqual(duplicate.state.last_received_monotonic_ns, 200)
        gate = continuity_gate(duplicate.state, now_monotonic_ns=1_201)
        self.assertEqual(gate.status, ProviderContinuityStatus.STALE)
        self.assertFalse(gate.actionable)

    def test_same_sequence_with_different_evidence_requires_resync(self) -> None:
        applied = accept_delta(
            self.synchronized(),
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=200),
        ).state
        conflict = accept_delta(
            applied,
            self.delta(sequence_id=11, evidence_id="changed-11", received_monotonic_ns=250),
        )

        self.assertEqual(conflict.outcome, ProviderContinuityOutcome.SEQUENCE_CONFLICT)
        self.assertEqual(conflict.state.status, ProviderContinuityStatus.SEQUENCE_CONFLICT)
        blocked = accept_delta(
            conflict.state,
            self.delta(sequence_id=12, evidence_id="delta-12", received_monotonic_ns=300),
        )
        self.assertEqual(blocked.outcome, ProviderContinuityOutcome.RESYNC_REQUIRED)

    def test_lower_sequence_is_out_of_order_and_cannot_self_heal(self) -> None:
        state = accept_delta(
            self.synchronized(),
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=200),
        ).state
        out_of_order = accept_delta(
            state,
            self.delta(sequence_id=9, evidence_id="delta-9", received_monotonic_ns=250),
        )

        self.assertEqual(out_of_order.outcome, ProviderContinuityOutcome.OUT_OF_ORDER)
        self.assertEqual(out_of_order.state.status, ProviderContinuityStatus.OUT_OF_ORDER)
        self.assertEqual(out_of_order.state.last_sequence_id, 11)
        self.assertFalse(continuity_gate(out_of_order.state, now_monotonic_ns=260).actionable)

    def test_sequence_gap_degrades_until_full_resync(self) -> None:
        state = self.synchronized()
        gap = accept_delta(
            state,
            self.delta(sequence_id=12, evidence_id="delta-12", received_monotonic_ns=200),
        )

        self.assertEqual(gap.outcome, ProviderContinuityOutcome.GAP_DETECTED)
        self.assertEqual(gap.expected_sequence_id, 11)
        self.assertEqual(gap.state.status, ProviderContinuityStatus.GAP_DETECTED)
        self.assertEqual(gap.state.last_sequence_id, 10)

        late_missing = accept_delta(
            gap.state,
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=250),
        )
        self.assertEqual(late_missing.outcome, ProviderContinuityOutcome.RESYNC_REQUIRED)
        self.assertIs(late_missing.state, gap.state)

        resynced = accept_authoritative_snapshot(
            gap.state,
            self.snapshot(
                sequence_id=20,
                evidence_id="resync-20",
                received_monotonic_ns=300,
            ),
        )
        self.assertEqual(resynced.status, ProviderContinuityStatus.SYNCHRONIZED)
        self.assertEqual(resynced.generation, 2)
        self.assertEqual(resynced.last_sequence_id, 20)
        self.assertTrue(continuity_gate(resynced, now_monotonic_ns=300).actionable)

    def test_reconnect_rejects_snapshot_received_before_latest_boundary(self) -> None:
        synchronized = accept_authoritative_snapshot(
            self.initial(),
            self.snapshot(
                sequence_id=10,
                evidence_id="provider-at-10",
                received_monotonic_ns=10,
            ),
        )
        disconnected = mark_disconnected(
            synchronized,
            observed_monotonic_ns=100,
        )
        reconnecting = begin_reconnect(
            disconnected,
            observed_monotonic_ns=110,
        )

        self.assertEqual(disconnected.resync_not_before_monotonic_ns, 100)
        self.assertEqual(reconnecting.resync_not_before_monotonic_ns, 110)
        for received_at in (90, 105):
            with self.subTest(received_at=received_at):
                with self.assertRaisesRegex(ValueError, "reconnect resync boundary"):
                    accept_authoritative_snapshot(
                        reconnecting,
                        self.snapshot(
                            sequence_id=20,
                            evidence_id=f"cached-before-reconnect-{received_at}",
                            received_monotonic_ns=received_at,
                        ),
                    )

        at_boundary = accept_authoritative_snapshot(
            reconnecting,
            self.snapshot(
                sequence_id=20,
                evidence_id="snapshot-at-reconnect-boundary",
                received_monotonic_ns=110,
            ),
        )
        self.assertEqual(at_boundary.status, ProviderContinuityStatus.SYNCHRONIZED)
        self.assertTrue(continuity_gate(at_boundary, now_monotonic_ns=111).actionable)

        resynced = accept_authoritative_snapshot(
            reconnecting,
            self.snapshot(
                sequence_id=20,
                evidence_id="post-reconnect-full-snapshot",
                received_monotonic_ns=120,
            ),
        )
        self.assertEqual(resynced.status, ProviderContinuityStatus.SYNCHRONIZED)
        self.assertEqual(resynced.generation, 2)
        self.assertEqual(resynced.last_received_monotonic_ns, 120)
        self.assertIsNone(resynced.resync_not_before_monotonic_ns)
        self.assertTrue(continuity_gate(resynced, now_monotonic_ns=121).actionable)


    def test_disconnect_reconnect_requires_snapshot_before_new_delta(self) -> None:
        state = self.synchronized()
        disconnected = mark_disconnected(state, observed_monotonic_ns=150)
        reconnecting = begin_reconnect(disconnected, observed_monotonic_ns=175)
        blocked = accept_delta(
            reconnecting,
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=200),
        )

        self.assertEqual(disconnected.status, ProviderContinuityStatus.DISCONNECTED)
        self.assertEqual(reconnecting.status, ProviderContinuityStatus.AWAITING_RESYNC)
        self.assertEqual(blocked.outcome, ProviderContinuityOutcome.RESYNC_REQUIRED)
        self.assertFalse(continuity_gate(reconnecting, now_monotonic_ns=200).actionable)

        resynced = accept_authoritative_snapshot(
            reconnecting,
            self.snapshot(
                sequence_id=2,
                evidence_id="new-stream-snapshot",
                received_monotonic_ns=225,
            ),
        )
        self.assertEqual(resynced.generation, 2)
        self.assertEqual(resynced.last_sequence_id, 2)
        self.assertTrue(continuity_gate(resynced, now_monotonic_ns=225).actionable)

    def test_long_silence_is_not_continuity_and_late_delta_cannot_clear_it(self) -> None:
        state = self.synchronized()
        gate = continuity_gate(state, now_monotonic_ns=1_101)
        late = accept_delta(
            state,
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=1_101),
        )

        self.assertEqual(gate.status, ProviderContinuityStatus.STALE)
        self.assertFalse(gate.actionable)
        self.assertEqual(late.outcome, ProviderContinuityOutcome.STALE)
        self.assertEqual(late.state.status, ProviderContinuityStatus.STALE)
        self.assertEqual(late.state.last_sequence_id, 10)

        resynced = accept_authoritative_snapshot(
            late.state,
            self.snapshot(
                sequence_id=30,
                evidence_id="resync-after-silence",
                received_monotonic_ns=1_200,
            ),
        )
        self.assertTrue(continuity_gate(resynced, now_monotonic_ns=1_200).actionable)

    def test_fresh_synchronized_state_cannot_be_silently_replaced_by_snapshot(self) -> None:
        state = self.synchronized()
        with self.assertRaisesRegex(RuntimeError, "cannot be replaced"):
            accept_authoritative_snapshot(
                state,
                self.snapshot(
                    sequence_id=99,
                    evidence_id="unexpected-snapshot",
                    received_monotonic_ns=150,
                ),
            )

    def test_stale_synchronized_state_may_resync_directly_at_snapshot_boundary(self) -> None:
        state = self.synchronized()
        resynced = accept_authoritative_snapshot(
            state,
            self.snapshot(
                sequence_id=50,
                evidence_id="stale-resync",
                received_monotonic_ns=1_101,
            ),
        )

        self.assertEqual(resynced.generation, 2)
        self.assertEqual(resynced.last_sequence_id, 50)
        self.assertEqual(resynced.status, ProviderContinuityStatus.SYNCHRONIZED)

    def test_monotonic_regression_degrades_and_requires_resync(self) -> None:
        state = self.synchronized()
        regressed = accept_delta(
            state,
            self.delta(sequence_id=11, evidence_id="delta-11", received_monotonic_ns=99),
        )

        self.assertEqual(
            regressed.outcome,
            ProviderContinuityOutcome.MONOTONIC_REGRESSION,
        )
        self.assertEqual(
            regressed.state.status,
            ProviderContinuityStatus.MONOTONIC_REGRESSION,
        )
        self.assertFalse(continuity_gate(regressed.state, now_monotonic_ns=101).actionable)

    def test_gate_detects_observer_monotonic_regression_without_mutating_state(self) -> None:
        state = self.synchronized()
        gate = continuity_gate(state, now_monotonic_ns=99)

        self.assertEqual(gate.status, ProviderContinuityStatus.MONOTONIC_REGRESSION)
        self.assertFalse(gate.actionable)
        self.assertEqual(state.status, ProviderContinuityStatus.SYNCHRONIZED)

    def test_cross_provider_delta_or_snapshot_is_rejected(self) -> None:
        state = self.synchronized()
        with self.assertRaisesRegex(ValueError, "source_id does not match"):
            accept_delta(
                state,
                self.delta(
                    sequence_id=11,
                    evidence_id="other",
                    received_monotonic_ns=200,
                    source_id="provider-b",
                ),
            )
        with self.assertRaisesRegex(ValueError, "source_id does not match"):
            accept_authoritative_snapshot(
                mark_disconnected(state, observed_monotonic_ns=150),
                self.snapshot(
                    sequence_id=1,
                    evidence_id="other-snapshot",
                    received_monotonic_ns=200,
                    source_id="provider-b",
                ),
            )

    def test_snapshot_receive_time_cannot_regress_across_generation(self) -> None:
        disconnected = mark_disconnected(self.synchronized(), observed_monotonic_ns=150)
        with self.assertRaisesRegex(ValueError, "snapshot receive monotonic time regressed"):
            accept_authoritative_snapshot(
                disconnected,
                self.snapshot(
                    sequence_id=1,
                    evidence_id="bad-clock",
                    received_monotonic_ns=99,
                ),
            )

    def test_invalid_types_ranges_and_partial_state_fail_closed(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            ProviderContinuityState.initial("provider-a", max_silence_ns=True)
        with self.assertRaises((TypeError, ValueError)):
            ProviderContinuityState.initial("provider-a", max_silence_ns=0)
        with self.assertRaises((TypeError, ValueError)):
            self.delta(sequence_id=True, evidence_id="x", received_monotonic_ns=1)
        with self.assertRaises((TypeError, ValueError)):
            self.delta(sequence_id=1 << 63, evidence_id="x", received_monotonic_ns=1)
        with self.assertRaises((TypeError, ValueError)):
            self.delta(sequence_id=1, evidence_id=" x ", received_monotonic_ns=1)
        with self.assertRaises((TypeError, ValueError)):
            self.delta(sequence_id=1, evidence_id="x", received_monotonic_ns=-1)
        with self.assertRaises(ValueError):
            replace(
                self.synchronized(),
                last_evidence_id=None,
            )
        for impossible_status in (
            ProviderContinuityStatus.DISCONNECTED,
            ProviderContinuityStatus.STALE,
            ProviderContinuityStatus.GAP_DETECTED,
        ):
            with self.subTest(impossible_status=impossible_status), self.assertRaisesRegex(
                ValueError, "generation zero must be uninitialized"
            ):
                ProviderContinuityState(
                    source_id="provider-a",
                    max_silence_ns=1_000,
                    generation=0,
                    status=impossible_status,
                )
        with self.assertRaisesRegex(ValueError, "positive generation cannot be uninitialized"):
            replace(self.synchronized(), status=ProviderContinuityStatus.UNINITIALIZED)


if __name__ == "__main__":
    unittest.main()
