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


    def test_source_result_issue_code_mutation_fails_before_hostile_issuer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            descriptor = vars(source_module.SourceUniverseCommitment)["_issue"]
            issue_function = descriptor.__func__
            original_code = issue_function.__code__

            def hostile_issue(cls, payload):
                raise AssertionError("hostile source-universe issuer executed")

            issue_function.__code__ = hostile_issue.__code__
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "result issuance surface is rebound or mutated",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                issue_function.__code__ = original_code

    def test_scheduled_result_issue_code_mutation_fails_before_hostile_issuer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            descriptor = vars(
                scheduled_module.ScheduledSourceUniverseResolution
            )["_issue"]
            issue_function = descriptor.__func__
            original_code = issue_function.__code__

            def hostile_issue(cls, payload):
                raise AssertionError("hostile scheduled-universe issuer executed")

            issue_function.__code__ = hostile_issue.__code__
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "result issuance surface is rebound or mutated",
                ):
                    _resolve(path, store, candidate)
            finally:
                issue_function.__code__ = original_code


    def test_source_result_field_descriptor_rebind_fails_before_spoofed_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            field_name = "provider_observation_complete"
            original = vars(source_module.SourceUniverseCommitment)[field_name]

            class SpoofedField:
                def __get__(self, instance, owner=None):
                    if instance is None:
                        return self
                    return True

                def __set__(self, instance, value):
                    original.__set__(instance, value)

            setattr(source_module.SourceUniverseCommitment, field_name, SpoofedField())
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "result field surface is rebound: provider_observation_complete",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                setattr(source_module.SourceUniverseCommitment, field_name, original)

    def test_scheduled_result_field_descriptor_rebind_fails_before_spoofed_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            field_name = "scheduled_provider_observation_complete"
            original = vars(
                scheduled_module.ScheduledSourceUniverseResolution
            )[field_name]

            class SpoofedField:
                def __get__(self, instance, owner=None):
                    if instance is None:
                        return self
                    return True

                def __set__(self, instance, value):
                    original.__set__(instance, value)

            setattr(
                scheduled_module.ScheduledSourceUniverseResolution,
                field_name,
                SpoofedField(),
            )
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "result field surface is rebound: "
                    "scheduled_provider_observation_complete",
                ):
                    _resolve(path, store, candidate)
            finally:
                setattr(
                    scheduled_module.ScheduledSourceUniverseResolution,
                    field_name,
                    original,
                )


    def test_canonical_json_code_mutation_fails_before_source_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            dumps = source_module.json.dumps
            original_code = dumps.__code__

            def hostile_dumps(*args, **kwargs):
                return "{}"

            dumps.__code__ = hostile_dumps.__code__
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "canonical JSON authority is rebound",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                dumps.__code__ = original_code

    def test_reflection_code_mutation_fails_before_scheduled_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            getattr_static = scheduled_module.inspect.getattr_static
            original_code = getattr_static.__code__

            def hostile_getattr_static(*args, **kwargs):
                return None

            getattr_static.__code__ = hostile_getattr_static.__code__
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "reflection authority is rebound",
                ):
                    _resolve(path, store, candidate)
            finally:
                getattr_static.__code__ = original_code


    def test_source_staticmethod_read_seam_code_mutation_fails_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            descriptor = vars(CollectorDeltaStore)["_path_file_identity"]
            path_identity = descriptor.__func__
            original_code = path_identity.__code__

            def hostile_path_identity(_path):
                raise AssertionError("hostile store path identity executed")

            path_identity.__code__ = hostile_path_identity.__code__
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "canonical durable read seam drifted: _path_file_identity",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                path_identity.__code__ = original_code

    def test_source_staticmethod_terminal_digest_code_mutation_fails_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, cycle_seq, _candidate = _ready_store(tmp)
            descriptor = vars(CollectorDeltaStore)["_cycle_terminal_payload_sha256"]
            digest = descriptor.__func__
            original_code = digest.__code__

            def hostile_digest(_payload_json):
                raise AssertionError("hostile terminal digest executed")

            digest.__code__ = hostile_digest.__code__
            try:
                with self.assertRaisesRegex(
                    source_module.SourceUniverseCommitmentError,
                    "canonical durable read seam drifted: _cycle_terminal_payload_sha256",
                ):
                    source_module.build_source_universe_commitment(
                        store,
                        expected_store_path=path,
                        source_id=SOURCE_ID,
                        start_cycle_seq=cycle_seq,
                        end_cycle_seq=cycle_seq,
                    )
            finally:
                digest.__code__ = original_code

    def test_schedule_staticmethod_due_at_code_mutation_fails_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            descriptor = vars(CollectorDeltaStore)["_collector_schedule_due_at"]
            due_at = descriptor.__func__
            original_code = due_at.__code__

            def hostile_due_at(*_args, **_kwargs):
                raise AssertionError("hostile schedule due_at executed")

            due_at.__code__ = hostile_due_at.__code__
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "canonical read seam drifted: _collector_schedule_due_at",
                ):
                    _resolve(path, store, candidate)
            finally:
                due_at.__code__ = original_code

    def test_schedule_classmethod_id_code_mutation_fails_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, store, _cycle_seq, candidate = _ready_store(tmp)
            descriptor = vars(CollectorDeltaStore)["_collector_schedule_id"]
            schedule_id = descriptor.__func__
            original_code = schedule_id.__code__

            def hostile_schedule_id(_cls, *_args, **_kwargs):
                raise AssertionError("hostile schedule id executed")

            schedule_id.__code__ = hostile_schedule_id.__code__
            try:
                with self.assertRaisesRegex(
                    scheduled_module.ScheduledSourceUniverseError,
                    "canonical read seam drifted: _collector_schedule_id",
                ):
                    _resolve(path, store, candidate)
            finally:
                schedule_id.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
