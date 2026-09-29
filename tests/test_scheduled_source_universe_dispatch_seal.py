from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import autosport.scheduled_source_universe as scheduled_module
import autosport.source_universe_commitment as source_module
from autosport.causal_collector import CollectorDeltaStore


SOURCE_ID = "source-dispatch-seal"
RUN_ID = "run-dispatch-seal"
STREAM_EPOCH = "epoch-dispatch-seal"
ANCHOR = "2026-09-29T12:00:00+00:00"


def _ready_store(tmp: str):
    path = Path(tmp) / "collector.db"
    store = CollectorDeltaStore(path)
    store._ensure_collector_schedule(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        anchor_at=ANCHOR,
        interval_seconds=10,
        max_items=25,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=0,
    )
    slot = store._next_collector_schedule_slot(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
    )
    cycle_seq = store._begin_scheduled_collector_cycle(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        max_items=25,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at=slot["due_at"],
    )
    store._finish_collector_cycle(
        source_id=SOURCE_ID,
        cycle_seq=cycle_seq,
        status="SUCCESS",
        completed_at="2026-09-29T12:00:01+00:00",
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
        error_code=None,
    )
    candidate = source_module.build_source_universe_commitment(
        store,
        expected_store_path=path,
        source_id=SOURCE_ID,
        start_cycle_seq=cycle_seq,
        end_cycle_seq=cycle_seq,
    )
    return path, store, cycle_seq, candidate


def _resolve(path: Path, store: CollectorDeltaStore, candidate):
    return scheduled_module.resolve_scheduled_source_universe(
        store,
        candidate,
        expected_store_path=path,
        expected_source_id=SOURCE_ID,
        expected_run_id=RUN_ID,
        expected_start_slot_ordinal=0,
        expected_end_slot_ordinal=0,
    )


class ScheduledSourceUniverseDispatchSealTests(unittest.TestCase):
    def test_exact_canonical_graph_still_resolves_positive_schedule_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)

            resolved = _resolve(path, store, candidate)

            self.assertTrue(resolved.scheduled_start_coverage_complete)
            self.assertTrue(resolved.observation_ledger_complete)
            self.assertTrue(resolved.scheduled_provider_observation_complete)
            self.assertFalse(resolved.external_provider_universe_complete)
            self.assertFalse(resolved.promotion_ready)

    def test_schedule_evidence_module_rebind_fails_before_hostile_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            original = scheduled_module._CANONICAL_SCHEDULE_EVIDENCE
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                raise AssertionError("hostile schedule evidence executed")

            scheduled_module._CANONICAL_SCHEDULE_EVIDENCE = hostile
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "schedule evidence authority is rebound",
                ):
                    _resolve(path, store, candidate)
            finally:
                scheduled_module._CANONICAL_SCHEDULE_EVIDENCE = original

            self.assertEqual(called, [])

    def test_schedule_verifier_module_rebind_fails_before_hostile_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            original = scheduled_module.verify_source_universe_commitment
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                return candidate

            scheduled_module.verify_source_universe_commitment = hostile
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "verifier authority is rebound",
                ):
                    _resolve(path, store, candidate)
            finally:
                scheduled_module.verify_source_universe_commitment = original

            self.assertEqual(called, [])

    def test_schedule_witness_map_in_place_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            witnesses = scheduled_module._CANONICAL_SCHEDULE_CLASS_READ_SEAMS
            original = witnesses["collector_schedule_evidence"]
            witnesses["collector_schedule_evidence"] = object()
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "canonical read seam drifted",
                ):
                    _resolve(path, store, candidate)
            finally:
                witnesses["collector_schedule_evidence"] = original

    def test_source_cycle_evidence_module_rebind_fails_before_hostile_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            original = source_module._CANONICAL_COLLECTOR_CYCLE_EVIDENCE
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                raise AssertionError("hostile cycle evidence executed")

            source_module._CANONICAL_COLLECTOR_CYCLE_EVIDENCE = hostile
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "collector-cycle evidence authority is rebound",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                source_module._CANONICAL_COLLECTOR_CYCLE_EVIDENCE = original

            self.assertEqual(called, [])

    def test_saved_source_verifier_rejects_public_builder_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, candidate = _ready_store(tmp)
            verifier = source_module.verify_source_universe_commitment
            original = source_module.build_source_universe_commitment
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                return candidate

            source_module.build_source_universe_commitment = hostile
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "verifier builder authority is rebound",
                ):
                    verifier(
                        store,
                        candidate,
                        expected_store_path=path,
                        expected_source_id=SOURCE_ID,
                        expected_start_cycle_seq=cycle_seq,
                        expected_end_cycle_seq=cycle_seq,
                    )
            finally:
                source_module.build_source_universe_commitment = original

            self.assertEqual(called, [])

    def test_verification_field_list_rebind_cannot_skip_payload_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, candidate = _ready_store(tmp)
            original = source_module._COMMITMENT_FIELD_NAMES
            source_module._COMMITMENT_FIELD_NAMES = ()
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "verification field authority is rebound",
                ):
                    source_module.verify_source_universe_commitment(
                        store,
                        candidate,
                        expected_store_path=path,
                        expected_source_id=SOURCE_ID,
                        expected_start_cycle_seq=cycle_seq,
                        expected_end_cycle_seq=cycle_seq,
                    )
            finally:
                source_module._COMMITMENT_FIELD_NAMES = original

    def test_result_issue_surface_rebind_fails_before_positive_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            result_type = scheduled_module.ScheduledSourceUniverseResolution
            original = vars(result_type)["_issue"]

            def hostile_issue(cls, payload):
                raise AssertionError("hostile result issuer executed")

            setattr(result_type, "_issue", classmethod(hostile_issue))
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "result issuance surface is rebound",
                ):
                    _resolve(path, store, candidate)
            finally:
                setattr(result_type, "_issue", original)


    def test_source_result_issue_surface_rebind_fails_before_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            result_type = source_module.SourceUniverseCommitment
            original = vars(result_type)["_issue"]

            def hostile_issue(cls, payload):
                raise AssertionError("hostile source-universe issuer executed")

            setattr(result_type, "_issue", classmethod(hostile_issue))
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "result issuance surface is rebound",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                setattr(result_type, "_issue", original)


if __name__ == "__main__":
    unittest.main()
