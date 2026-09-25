from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from autosport.live_stream_continuity import (
    ContinuityStatus,
    LiveStreamContinuityJournal,
    StreamIdentity,
    StreamObservation,
    StreamObservationKind,
)
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


IDENTITY = StreamIdentity(
    provider_id="provider-a",
    account_id="account-a",
    adapter_id="adapter-a",
    source_id="source-a",
    sport="table_tennis",
    event_id="event-a",
    market_id="market-a",
    stream_family="market_data",
)


def _observation(
    kind: StreamObservationKind,
    *,
    sequence: int | None = None,
    cursor: str | None = None,
    resume_from: str | None = None,
    semantic: str | None = None,
    identity: StreamIdentity = IDENTITY,
    evidence: str | None = None,
) -> StreamObservation:
    label = semantic or f"{kind.value}:{sequence}:{cursor}:{resume_from}"
    raw_label = evidence or f"raw:{label}"
    return StreamObservation(
        identity=identity,
        kind=kind,
        observed_at="2026-09-21T10:00:00+00:00",
        raw_evidence_id=f"evidence:{raw_label}",
        raw_evidence_sha256=_digest(raw_label),
        semantic_payload_sha256=_digest(label),
        provider_cursor=cursor,
        resume_from_cursor=resume_from,
        provider_sequence=sequence,
    )


class LiveStreamContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior_authority_root = os.environ.get(
            "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT"
        )

    def tearDown(self) -> None:
        if self._prior_authority_root is None:
            os.environ.pop("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", None)
        else:
            os.environ["AUTOSPORT_MONOTONIC_AUTHORITY_ROOT"] = (
                self._prior_authority_root
            )

    def _journal(self, directory: str) -> LiveStreamContinuityJournal:
        root = Path(directory)
        os.environ["AUTOSPORT_MONOTONIC_AUTHORITY_ROOT"] = str(
            (root / "machine-state").resolve()
        )
        return LiveStreamContinuityJournal(
            root / "workspace" / "continuity.jsonl",
            IDENTITY,
        )

    def test_caller_constructed_positive_shape_never_grants_provider_or_decision_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            snapshot = journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="caller-shaped-c1",
                    semantic="caller-shaped-provider-baseline",
                )
            )

            self.assertTrue(snapshot.continuity_qualified)
            self.assertFalse(snapshot.provider_origin_verified)
            self.assertFalse(snapshot.decision_eligible)
            self.assertFalse(snapshot.provider_write_authorized)
            self.assertFalse(snapshot.execution_authorized)
            self.assertFalse(snapshot.real_money_execution)

    def test_delta_first_startup_never_becomes_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            snapshot = journal.apply(
                _observation(
                    StreamObservationKind.DELTA,
                    sequence=1,
                    cursor="c1",
                )
            )
            self.assertEqual(
                snapshot.continuity_status,
                ContinuityStatus.WAIT_NO_BASELINE,
            )
            self.assertFalse(snapshot.decision_eligible)
            self.assertTrue(snapshot.gapped)
            self.assertEqual(snapshot.record_count, 1)

    def test_heartbeat_after_reconnect_is_liveness_not_continuity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=10,
                    cursor="c10",
                )
            )
            self.assertTrue(journal.snapshot().continuity_qualified)
            self.assertFalse(journal.snapshot().decision_eligible)
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            snapshot = journal.apply(_observation(StreamObservationKind.HEARTBEAT))
            self.assertTrue(snapshot.connected)
            self.assertTrue(snapshot.gapped)
            self.assertEqual(
                snapshot.continuity_status,
                ContinuityStatus.WAIT_GAPPED,
            )

    def test_exact_resume_proof_closes_reconnect_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=10,
                    cursor="c10",
                )
            )
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            resumed = journal.apply(
                _observation(
                    StreamObservationKind.RESUME_PROOF,
                    sequence=11,
                    cursor="c11",
                    resume_from="c10",
                )
            )
            self.assertTrue(resumed.continuity_qualified)
            self.assertFalse(resumed.decision_eligible)
            self.assertIsNone(resumed.resume_anchor_cursor)
            self.assertEqual(resumed.last_provider_cursor, "c11")
            self.assertEqual(resumed.last_provider_sequence, 11)

    def test_same_cursor_resume_proof_can_close_gap_without_fake_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=10,
                    cursor="c10",
                )
            )
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            resumed = journal.apply(
                _observation(
                    StreamObservationKind.RESUME_PROOF,
                    sequence=10,
                    cursor="c10",
                    resume_from="c10",
                    semantic="provider-confirms-no-gap",
                )
            )
            self.assertTrue(resumed.continuity_qualified)
            self.assertFalse(resumed.decision_eligible)
            self.assertEqual(resumed.last_provider_cursor, "c10")
            self.assertEqual(resumed.last_provider_sequence, 10)
            self.assertIsNone(resumed.resume_anchor_cursor)

    def test_same_cursor_resume_proof_cannot_invent_sequence_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=10,
                    cursor="c10",
                )
            )
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            path = Path(directory) / "workspace" / "continuity.jsonl"
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "cannot change provider_sequence"):
                journal.apply(
                    _observation(
                        StreamObservationKind.RESUME_PROOF,
                        sequence=11,
                        cursor="c10",
                        resume_from="c10",
                        semantic="impossible-advance",
                    )
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(journal.snapshot().decision_eligible)

    def test_wrong_resume_anchor_fails_before_journal_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=10,
                    cursor="c10",
                )
            )
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            before = (Path(directory) / "workspace" / "continuity.jsonl").read_bytes()
            with self.assertRaisesRegex(ValueError, "persisted cursor anchor"):
                journal.apply(
                    _observation(
                        StreamObservationKind.RESUME_PROOF,
                        sequence=11,
                        cursor="c11",
                        resume_from="foreign-cursor",
                    )
                )
            after = (Path(directory) / "workspace" / "continuity.jsonl").read_bytes()
            self.assertEqual(after, before)
            self.assertFalse(journal.snapshot().decision_eligible)

    def test_fresh_baseline_after_reconnect_establishes_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            first = journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=99,
                    cursor="old",
                )
            )
            self.assertEqual(first.generation, 1)
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            second = journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="new",
                    semantic="new-image",
                )
            )
            self.assertEqual(second.generation, 2)
            self.assertEqual(second.last_provider_sequence, 1)
            self.assertEqual(second.last_provider_cursor, "new")
            self.assertTrue(second.continuity_qualified)
            self.assertFalse(second.decision_eligible)

    def test_suspension_is_orthogonal_and_survives_reconnect_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            suspended = journal.apply(
                _observation(StreamObservationKind.SUSPENDED)
            )
            self.assertEqual(
                suspended.continuity_status,
                ContinuityStatus.WAIT_SUSPENDED,
            )
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            refreshed = journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c2",
                    semantic="fresh-image",
                )
            )
            self.assertTrue(refreshed.suspended)
            self.assertFalse(refreshed.decision_eligible)
            unsuspended = journal.apply(
                _observation(StreamObservationKind.UNSUSPENDED)
            )
            self.assertTrue(unsuspended.continuity_qualified)
            self.assertFalse(unsuspended.decision_eligible)

    def test_restart_preserves_gap_and_cursor_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=5,
                    cursor="c5",
                )
            )
            journal.apply(_observation(StreamObservationKind.DISCONNECTED))
            journal.apply(_observation(StreamObservationKind.RECONNECTED))
            path = Path(directory) / "workspace" / "continuity.jsonl"

            reopened = LiveStreamContinuityJournal(path, IDENTITY)
            snapshot = reopened.snapshot()
            self.assertTrue(snapshot.connected)
            self.assertTrue(snapshot.gapped)
            self.assertEqual(snapshot.resume_anchor_cursor, "c5")
            self.assertFalse(snapshot.decision_eligible)

    def test_cross_stream_identity_is_rejected_without_persistence(self) -> None:
        foreign = StreamIdentity(
            provider_id="provider-a",
            account_id="account-a",
            adapter_id="adapter-a",
            source_id="source-a",
            sport="table_tennis",
            event_id="event-a",
            market_id="market-b",
            stream_family="market_data",
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            path = Path(directory) / "workspace" / "continuity.jsonl"
            with self.assertRaisesRegex(ValueError, "stream identity"):
                journal.apply(
                    _observation(
                        StreamObservationKind.BASELINE,
                        sequence=1,
                        cursor="c1",
                        identity=foreign,
                    )
                )
            self.assertFalse(path.exists())
            self.assertEqual(journal.snapshot().record_count, 0)

    def test_exact_observation_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            observation = _observation(
                StreamObservationKind.BASELINE,
                sequence=1,
                cursor="c1",
            )
            first = journal.apply(observation)
            path = Path(directory) / "workspace" / "continuity.jsonl"
            before = path.read_bytes()
            second = journal.apply(observation)
            after = path.read_bytes()
            self.assertEqual(first, second)
            self.assertEqual(after, before)
            self.assertEqual(second.record_count, 1)

    def test_conflicting_same_provider_sequence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            journal.apply(
                _observation(
                    StreamObservationKind.DELTA,
                    sequence=2,
                    cursor="c2",
                    semantic="delta-a",
                )
            )
            path = Path(directory) / "workspace" / "continuity.jsonl"
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "provider_sequence"):
                journal.apply(
                    _observation(
                        StreamObservationKind.DELTA,
                        sequence=2,
                        cursor="c2b",
                        semantic="delta-b",
                    )
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(journal.snapshot().last_provider_sequence, 2)

    def test_unseen_sequence_regression_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=10,
                    cursor="c10",
                )
            )
            journal.apply(
                _observation(
                    StreamObservationKind.DELTA,
                    sequence=12,
                    cursor="c12",
                )
            )
            with self.assertRaisesRegex(ValueError, "regression"):
                journal.apply(
                    _observation(
                        StreamObservationKind.DELTA,
                        sequence=11,
                        cursor="c11",
                    )
                )

    def test_same_cursor_with_conflicting_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            journal.apply(
                _observation(
                    StreamObservationKind.DELTA,
                    sequence=2,
                    cursor="shared",
                    semantic="payload-a",
                )
            )
            with self.assertRaisesRegex(ValueError, "provider cursor"):
                journal.apply(
                    _observation(
                        StreamObservationKind.DELTA,
                        sequence=3,
                        cursor="shared",
                        semantic="payload-b",
                    )
                )

    def test_stale_second_instance_refreshes_under_cross_process_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary = self._journal(directory)
            duplicate = LiveStreamContinuityJournal(primary.path, IDENTITY)

            primary.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            advanced = duplicate.apply(
                _observation(
                    StreamObservationKind.DELTA,
                    sequence=2,
                    cursor="c2",
                )
            )

            self.assertTrue(advanced.continuity_qualified)
            self.assertFalse(advanced.decision_eligible)
            self.assertEqual(advanced.record_count, 2)
            self.assertEqual(advanced.last_provider_sequence, 2)
            reopened = LiveStreamContinuityJournal(primary.path, IDENTITY)
            self.assertEqual(reopened.snapshot(), advanced)

    def test_crash_before_journal_append_aborts_prepare_and_exact_retry_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            before = journal.path.read_bytes()
            observation = _observation(
                StreamObservationKind.DELTA,
                sequence=2,
                cursor="c2",
            )
            original_append = journal._append_durable

            def fail_before_append(_encoded: bytes) -> None:
                raise OSError("injected crash before continuity journal append")

            journal._append_durable = fail_before_append
            with self.assertRaisesRegex(OSError, "injected crash"):
                journal.apply(observation)
            journal._append_durable = original_append
            self.assertEqual(journal.path.read_bytes(), before)

            reopened = LiveStreamContinuityJournal(journal.path, IDENTITY)
            self.assertEqual(reopened.snapshot().last_provider_sequence, 1)
            retried = reopened.apply(observation)
            self.assertEqual(retried.last_provider_sequence, 2)
            self.assertTrue(retried.continuity_qualified)
            self.assertFalse(retried.decision_eligible)

    def test_crash_after_journal_append_before_authority_commit_recovers_intended_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            observation = _observation(
                StreamObservationKind.DELTA,
                sequence=2,
                cursor="c2",
            )
            with mock.patch.object(
                MonotonicWorkspaceAuthority,
                "commit",
                side_effect=RuntimeError(
                    "injected crash after continuity journal append"
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected crash"):
                    journal.apply(observation)

            reopened = LiveStreamContinuityJournal(journal.path, IDENTITY)
            recovered = reopened.snapshot()
            self.assertEqual(recovered.last_provider_sequence, 2)
            self.assertTrue(recovered.continuity_qualified)
            self.assertFalse(recovered.decision_eligible)

            # The recovered observation is exact-idempotent and must not append again.
            before = journal.path.read_bytes()
            replayed = reopened.apply(observation)
            self.assertEqual(replayed, recovered)
            self.assertEqual(journal.path.read_bytes(), before)

    def test_valid_old_journal_rollback_is_rejected_after_newer_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            old_valid_bytes = journal.path.read_bytes()
            latest = journal.apply(
                _observation(
                    StreamObservationKind.DELTA,
                    sequence=2,
                    cursor="c2",
                )
            )
            latest_valid_bytes = journal.path.read_bytes()
            self.assertTrue(latest.continuity_qualified)
            self.assertFalse(latest.decision_eligible)

            journal.path.write_bytes(old_valid_bytes)
            with self.assertRaisesRegex(
                RuntimeError,
                "rolled back|monotonic|authority|workspace state",
            ):
                LiveStreamContinuityJournal(journal.path, IDENTITY)

            journal.path.write_bytes(latest_valid_bytes)
            restored = LiveStreamContinuityJournal(journal.path, IDENTITY)
            self.assertEqual(restored.snapshot().last_provider_sequence, 2)

    def test_identity_change_cannot_reset_deleted_path_rollback_history(self) -> None:
        foreign = StreamIdentity(
            provider_id="provider-b",
            account_id="account-b",
            adapter_id="adapter-b",
            source_id="source-b",
            sport="tennis",
            event_id="event-b",
            market_id="market-b",
            stream_family="market_data",
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            journal.path.unlink()

            with self.assertRaisesRegex(
                RuntimeError,
                "rolled back|monotonic|authority|workspace state",
            ):
                LiveStreamContinuityJournal(journal.path, foreign)

    def test_journal_deletion_cannot_rebootstrap_after_authority_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            journal.path.unlink()

            with self.assertRaisesRegex(
                RuntimeError,
                "rolled back|monotonic|authority|workspace state",
            ):
                LiveStreamContinuityJournal(journal.path, IDENTITY)

    def test_truncated_journal_fails_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            path = Path(directory) / "workspace" / "continuity.jsonl"
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, "truncated"):
                LiveStreamContinuityJournal(path, IDENTITY)

    def test_tampered_journal_row_fails_hash_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            path = Path(directory) / "workspace" / "continuity.jsonl"
            row = json.loads(path.read_text(encoding="utf-8"))
            row["observation"]["semantic_payload_sha256"] = _digest("tampered")
            path.write_text(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "row hash mismatch"):
                LiveStreamContinuityJournal(path, IDENTITY)

    def test_persisted_row_exists_before_snapshot_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal(directory)
            snapshot = journal.apply(
                _observation(
                    StreamObservationKind.BASELINE,
                    sequence=1,
                    cursor="c1",
                )
            )
            path = Path(directory) / "workspace" / "continuity.jsonl"
            self.assertTrue(path.exists())
            rows = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            row = json.loads(rows[0])
            self.assertEqual(row["record_sha256"], snapshot.last_record_sha256)
            self.assertEqual(row["ordinal"], snapshot.record_count)
            self.assertFalse(snapshot.provider_write_authorized)
            self.assertFalse(snapshot.execution_authorized)
            self.assertFalse(snapshot.real_money_execution)


if __name__ == "__main__":
    unittest.main()
