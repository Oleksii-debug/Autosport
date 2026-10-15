from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.provider_feed_continuity import (
    ContinuityFault,
    ContinuityStatus,
    FeedContinuityError,
    FeedContinuityJournal,
    FeedContinuityObservation,
    SequenceMode,
)


class FeedContinuityTests(unittest.TestCase):
    def _obs(
        self,
        *,
        event_id: str,
        sequence: int,
        received: int,
        source_id: str = "provider-a",
        stream_id: str = "market-1:generation-1",
        mode: SequenceMode = SequenceMode.CONTIGUOUS_INT,
    ) -> FeedContinuityObservation:
        return FeedContinuityObservation(
            source_id=source_id,
            stream_id=stream_id,
            event_id=event_id,
            sequence_mode=mode,
            sequence=sequence,
            received_monotonic_ns=received,
        )

    def test_first_observation_establishes_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            state = journal.observe(self._obs(event_id="e1", sequence=40, received=100))
            self.assertTrue(state.healthy)
            self.assertEqual(state.last_sequence, 40)
            self.assertEqual(state.accepted_event_count, 1)
            self.assertEqual(state.journal_record_count, 1)

    def test_contiguous_sequence_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="e1", sequence=40, received=100))
            state = journal.observe(self._obs(event_id="e2", sequence=41, received=101))
            self.assertEqual(state.last_sequence, 41)
            self.assertEqual(state.accepted_event_count, 2)

    def test_exact_event_replay_is_idempotent_without_new_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            obs = self._obs(event_id="e1", sequence=40, received=100)
            first = journal.observe(obs)
            before = path.read_bytes()
            second = journal.observe(obs)
            self.assertEqual(second.journal_record_count, first.journal_record_count)
            self.assertEqual(path.read_bytes(), before)

    def test_conflicting_event_replay_quarantines_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="e1", sequence=40, received=100))
            with self.assertRaisesRegex(FeedContinuityError, "event_id replay conflicts"):
                journal.observe(self._obs(event_id="e1", sequence=41, received=101))
            state = journal.state("provider-a", "market-1:generation-1")
            self.assertEqual(state.status, ContinuityStatus.RESYNC_REQUIRED)
            self.assertEqual(state.fault, ContinuityFault.EVENT_ID_CONFLICT)

    def test_sequence_gap_is_durable_fault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            with self.assertRaisesRegex(FeedContinuityError, "SEQUENCE_GAP"):
                journal.observe(self._obs(event_id="e2", sequence=12, received=101))
            reopened = FeedContinuityJournal(path)
            state = reopened.state("provider-a", "market-1:generation-1")
            self.assertEqual(state.status, ContinuityStatus.RESYNC_REQUIRED)
            self.assertEqual(state.fault, ContinuityFault.SEQUENCE_GAP)
            self.assertEqual(state.last_sequence, 10)

    def test_sequence_regression_is_durable_fault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            with self.assertRaisesRegex(FeedContinuityError, "SEQUENCE_REGRESSION"):
                journal.observe(self._obs(event_id="e2", sequence=10, received=101))
            self.assertEqual(
                FeedContinuityJournal(path).state("provider-a", "market-1:generation-1").fault,
                ContinuityFault.SEQUENCE_REGRESSION,
            )

    def test_receive_monotonic_regression_quarantines_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            with self.assertRaisesRegex(FeedContinuityError, "RECEIVE_MONOTONIC_REGRESSION"):
                journal.observe(self._obs(event_id="e2", sequence=11, received=99))

    def test_receive_gap_boundary_is_inclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(
                Path(tmp) / "continuity.jsonl",
                max_receive_gap_ns=5,
            )
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            state = journal.observe(self._obs(event_id="e2", sequence=11, received=105))
            self.assertTrue(state.healthy)

    def test_receive_gap_over_boundary_quarantines_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(
                Path(tmp) / "continuity.jsonl",
                max_receive_gap_ns=5,
            )
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            with self.assertRaisesRegex(FeedContinuityError, "RECEIVE_GAP"):
                journal.observe(self._obs(event_id="e2", sequence=11, received=106))

    def test_quarantined_stream_never_auto_heals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            with self.assertRaises(FeedContinuityError):
                journal.observe(self._obs(event_id="bad", sequence=12, received=101))
            before = journal.record_count
            with self.assertRaisesRegex(FeedContinuityError, "authoritative resync required"):
                journal.observe(self._obs(event_id="e2", sequence=11, received=102))
            self.assertEqual(journal.record_count, before)

    def test_new_stream_identity_is_independent_after_fault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="e1", sequence=10, received=100))
            with self.assertRaises(FeedContinuityError):
                journal.observe(self._obs(event_id="bad", sequence=12, received=101))
            fresh = journal.observe(
                self._obs(
                    event_id="g2-e1",
                    sequence=500,
                    received=200,
                    stream_id="market-1:generation-2",
                )
            )
            self.assertTrue(fresh.healthy)
            self.assertEqual(fresh.last_sequence, 500)

    def test_multiple_streams_do_not_share_sequence_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="a1", sequence=1, received=1, stream_id="a"))
            journal.observe(self._obs(event_id="b1", sequence=900, received=2, stream_id="b"))
            self.assertEqual(journal.state("provider-a", "a").last_sequence, 1)
            self.assertEqual(journal.state("provider-a", "b").last_sequence, 900)

    def test_restart_preserves_exact_head_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            journal.observe(self._obs(event_id="e1", sequence=1, received=10))
            journal.observe(self._obs(event_id="e2", sequence=2, received=11))
            before = journal.state("provider-a", "market-1:generation-1")
            reopened = FeedContinuityJournal(path)
            after = reopened.state("provider-a", "market-1:generation-1")
            self.assertEqual(after, before)

    def test_truncated_final_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            journal.observe(self._obs(event_id="e1", sequence=1, received=10))
            raw = path.read_bytes()
            path.write_bytes(raw[:-1])
            with self.assertRaisesRegex(FeedContinuityError, "truncated"):
                FeedContinuityJournal(path)

    def test_digest_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            journal.observe(self._obs(event_id="e1", sequence=1, received=10))
            line = json.loads(path.read_text(encoding="utf-8"))
            line["payload"]["observation"]["sequence"] = 2
            path.write_text(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(FeedContinuityError, "record digest mismatch"):
                FeedContinuityJournal(path)

    def test_hash_chain_reordering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path)
            journal.observe(self._obs(event_id="e1", sequence=1, received=10))
            journal.observe(self._obs(event_id="e2", sequence=2, received=11))
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[1] + "\n" + lines[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(FeedContinuityError, "record index|hash chain"):
                FeedContinuityJournal(path)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            path.write_text('{"schema":1,"schema":1}\n', encoding="utf-8")
            with self.assertRaisesRegex(FeedContinuityError, "duplicate JSON key"):
                FeedContinuityJournal(path)

    def test_invalid_utf8_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            path.write_bytes(b"\xff\n")
            with self.assertRaisesRegex(FeedContinuityError, "valid UTF-8"):
                FeedContinuityJournal(path)

    def test_non_regular_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FeedContinuityError, "regular"):
                FeedContinuityJournal(Path(tmp))

    def test_boolean_and_int_subclass_sequences_are_rejected(self) -> None:
        class EvilInt(int):
            pass

        for value in (True, EvilInt(1)):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    FeedContinuityObservation(
                        source_id="p",
                        stream_id="s",
                        event_id="e",
                        sequence_mode=SequenceMode.CONTIGUOUS_INT,
                        sequence=value,
                        received_monotonic_ns=0,
                    )

    def test_signed_int64_sequence_boundaries_are_accepted(self) -> None:
        for value in (-(1 << 63), (1 << 63) - 1):
            with self.subTest(value=value):
                obs = FeedContinuityObservation(
                    source_id="p",
                    stream_id="s",
                    event_id=f"e-{value}",
                    sequence_mode=SequenceMode.CONTIGUOUS_INT,
                    sequence=value,
                    received_monotonic_ns=0,
                )
                self.assertEqual(obs.sequence, value)

    def test_overflow_after_max_sequence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = FeedContinuityJournal(Path(tmp) / "continuity.jsonl")
            journal.observe(self._obs(event_id="e1", sequence=(1 << 63) - 1, received=1))
            with self.assertRaisesRegex(FeedContinuityError, "SEQUENCE_GAP"):
                journal.observe(self._obs(event_id="e2", sequence=(1 << 63) - 1, received=2))

    def test_identifier_control_character_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "control characters"):
            self._obs(event_id="bad\nline", sequence=1, received=1)

    def test_evidence_digest_is_stable_and_type_bound(self) -> None:
        obs = self._obs(event_id="e1", sequence=7, received=9)
        payload = {
            "source_id": "provider-a",
            "stream_id": "market-1:generation-1",
            "event_id": "e1",
            "sequence_mode": "CONTIGUOUS_INT",
            "sequence": 7,
            "received_monotonic_ns": 9,
        }
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(obs.evidence_sha256, expected)

    def test_stress_restart_multiple_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            journal = FeedContinuityJournal(path, max_receive_gap_ns=10)
            for generation in range(8):
                stream = f"stream-{generation}"
                for sequence in range(1, 126):
                    journal.observe(
                        self._obs(
                            event_id=f"{stream}-{sequence}",
                            sequence=sequence,
                            received=sequence,
                            stream_id=stream,
                        )
                    )
            head = journal.head_sha256
            reopened = FeedContinuityJournal(path, max_receive_gap_ns=10)
            self.assertEqual(reopened.record_count, 1000)
            self.assertEqual(reopened.head_sha256, head)
            for generation in range(8):
                state = reopened.state("provider-a", f"stream-{generation}")
                self.assertTrue(state.healthy)
                self.assertEqual(state.last_sequence, 125)
                self.assertEqual(state.accepted_event_count, 125)

    def test_broken_symlink_journal_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alias = root / "broken.jsonl"
            try:
                alias.symlink_to(root / "missing-target.jsonl")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaisesRegex(FeedContinuityError, "non-symlink"):
                FeedContinuityJournal(alias)

    def test_hard_link_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.jsonl"
            target.write_text("", encoding="utf-8")
            alias = root / "alias.jsonl"
            try:
                import os
                os.link(target, alias)
            except (OSError, NotImplementedError):
                self.skipTest("hard-link creation is unavailable")
            with self.assertRaisesRegex(FeedContinuityError, "hard-link"):
                FeedContinuityJournal(alias)

    def test_two_live_instances_detect_external_generation_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuity.jsonl"
            first = FeedContinuityJournal(path)
            second = FeedContinuityJournal(path)
            first.observe(self._obs(event_id="e1", sequence=1, received=1))
            with self.assertRaisesRegex(FeedContinuityError, "changed by another writer"):
                second.observe(self._obs(event_id="e1", sequence=1, received=1))
            reopened = FeedContinuityJournal(path)
            state = reopened.observe(self._obs(event_id="e2", sequence=2, received=2))
            self.assertEqual(state.last_sequence, 2)

    def test_symlink_journal_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.jsonl"
            target.write_text("", encoding="utf-8")
            alias = root / "alias.jsonl"
            try:
                alias.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaisesRegex(FeedContinuityError, "non-symlink"):
                FeedContinuityJournal(alias)


if __name__ == "__main__":
    unittest.main()
