import unittest

from autosport.causal_collector import (
    CanonicalDesktopApplication,
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    RemoteCollectorAdapter,
    StreamCheckpoint,
)
from autosport.continuous_session import _ContinuousSessionState


class _HostileCollectorDelta(CollectorDelta):
    validate_calls = 0

    def validate(self) -> None:
        type(self).validate_calls += 1


class _HostileText(str):
    strip_calls = 0
    format_calls = 0

    def strip(self, *args, **kwargs):
        type(self).strip_calls += 1
        return super().strip(*args, **kwargs)

    def __format__(self, format_spec):
        type(self).format_calls += 1
        return super().__format__(format_spec)


class _HostileInt(int):
    compare_calls = 0

    def __lt__(self, other):
        type(self).compare_calls += 1
        return super().__lt__(other)


class _HostileReceipt(DesktopApplicationReceipt):
    validate_calls = 0

    def validate(self) -> None:
        type(self).validate_calls += 1


def _delta(**overrides):
    values = {
        "schema_version": 1,
        "delta_id": "delta-section2",
        "source_id": "source-section2",
        "lawful_terms_ref": "terms-section2",
        "retention_ref": "retention-section2",
        "stream_epoch": "epoch-section2",
        "source_cursor": "cursor-section2",
        "cursor_position": 1,
        "event_dedupe_key": "event-dedupe-section2",
        "event_id": "event-section2",
        "source_payload_digest": "0" * 64,
        "canonical_event_digest": "1" * 64,
        "source_observed_at": "2026-10-07T03:40:00+00:00",
        "collector_received_at": "2026-10-07T03:40:01+00:00",
        "collector_committed_at": "2026-10-07T03:40:02+00:00",
        "desktop_available_at": "2026-10-07T03:40:03+00:00",
    }
    values.update(overrides)
    return CollectorDelta(**values)


class Section2SourceProjectionDeltaExactTypeTests(unittest.TestCase):
    def test_collector_store_get_rejects_hostile_delta_id_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        store = object.__new__(CollectorDeltaStore)
        with self.assertRaisesRegex(
            TypeError,
            "delta_id must be exact string identity text",
        ):
            store.get(_HostileText("delta-section2"))
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_collector_store_feed_rejects_hostile_source_id_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        store = object.__new__(CollectorDeltaStore)
        with self.assertRaisesRegex(
            TypeError,
            "source_id must be exact string identity text",
        ):
            store.deltas_after_commit(source_id=_HostileText("source-section2"))
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_collector_store_checkpoint_rejects_hostile_stream_identity_before_format_dispatch(self) -> None:
        _HostileText.format_calls = 0
        store = object.__new__(CollectorDeltaStore)
        with self.assertRaisesRegex(
            TypeError,
            "stream_epoch must be exact string identity text",
        ):
            store.stream_checkpoint("source-section2", _HostileText("epoch-section2"))
        self.assertEqual(_HostileText.format_calls, 0)

    def test_collector_store_feed_rejects_hostile_after_delta_id_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        store = object.__new__(CollectorDeltaStore)
        with self.assertRaisesRegex(
            TypeError,
            "after_delta_id must be exact string identity text or None",
        ):
            store.deltas_after_commit(
                source_id="source-section2",
                after_delta_id=_HostileText("delta-section2"),
            )
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_stream_checkpoint_rejects_hostile_identity_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        checkpoint = StreamCheckpoint(
            source_id=_HostileText("source-section2"),
            stream_epoch="epoch-section2",
            last_cursor="cursor-section2",
            last_position=1,
            last_delta_id="delta-section2",
        )
        with self.assertRaisesRegex(
            TypeError,
            "source_id must be exact string identity text",
        ):
            checkpoint.validate()
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_application_receipt_rejects_hostile_identity_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        receipt = DesktopApplicationReceipt(
            delta_id=_HostileText("delta-section2"),
            canonical_event_digest="0" * 64,
            receipt_id="receipt-section2",
            applied_at="2026-10-07T03:40:04+00:00",
        )
        with self.assertRaisesRegex(
            TypeError,
            "delta_id must be exact string identity text",
        ):
            receipt.validate()
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_canonical_desktop_apply_rejects_delta_subclass_before_virtual_validation(self) -> None:
        _HostileCollectorDelta.validate_calls = 0
        app = object.__new__(CanonicalDesktopApplication)
        hostile = object.__new__(_HostileCollectorDelta)
        with self.assertRaisesRegex(TypeError, "delta must be exact CollectorDelta"):
            app.apply(hostile, object())
        self.assertEqual(_HostileCollectorDelta.validate_calls, 0)

    def test_canonical_desktop_lookup_rejects_delta_subclass_before_state_dispatch(self) -> None:
        app = object.__new__(CanonicalDesktopApplication)
        hostile = object.__new__(_HostileCollectorDelta)
        with self.assertRaisesRegex(TypeError, "delta must be exact CollectorDelta"):
            app.lookup_receipt(hostile)

    def test_checkpoint_store_has_ack_rejects_hostile_delta_id_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        store = object.__new__(DesktopDeltaCheckpointStore)
        with self.assertRaisesRegex(
            TypeError,
            "delta_id must be exact string identity text",
        ):
            store.has_ack(_HostileText("delta-section2"))
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_checkpoint_store_stream_rejects_hostile_epoch_before_format_dispatch(self) -> None:
        _HostileText.format_calls = 0
        store = object.__new__(DesktopDeltaCheckpointStore)
        with self.assertRaisesRegex(
            TypeError,
            "stream_epoch must be exact string identity text",
        ):
            store.stream_checkpoint("source-section2", _HostileText("epoch-section2"))
        self.assertEqual(_HostileText.format_calls, 0)

    def test_checkpoint_store_receipt_rejects_delta_subclass_before_read(self) -> None:
        store = object.__new__(DesktopDeltaCheckpointStore)
        hostile = object.__new__(_HostileCollectorDelta)
        with self.assertRaisesRegex(TypeError, "delta must be exact CollectorDelta"):
            store.application_receipt(hostile)

    def test_checkpoint_store_ack_rejects_receipt_subclass_before_virtual_validation(self) -> None:
        _HostileReceipt.validate_calls = 0
        store = object.__new__(DesktopDeltaCheckpointStore)
        receipt = _HostileReceipt(
            delta_id="delta-section2",
            canonical_event_digest="1" * 64,
            receipt_id="receipt-section2",
            applied_at="2026-10-07T03:40:04+00:00",
        )
        with self.assertRaisesRegex(
            TypeError,
            "application_receipt must be exact DesktopApplicationReceipt",
        ):
            store.ack(
                _delta(),
                application_receipt=receipt,
                acknowledged_at="2026-10-07T03:40:05+00:00",
            )
        self.assertEqual(_HostileReceipt.validate_calls, 0)

    def test_remote_adapter_rejects_delta_subclass_before_virtual_validation(self) -> None:
        _HostileCollectorDelta.validate_calls = 0
        adapter = RemoteCollectorAdapter(lambda _delta_value: True)
        hostile = object.__new__(_HostileCollectorDelta)
        with self.assertRaisesRegex(TypeError, "delta must be exact CollectorDelta"):
            adapter.submit_committed_delta(hostile)
        self.assertEqual(_HostileCollectorDelta.validate_calls, 0)

    def test_collector_store_rejects_delta_subclass_before_virtual_validation(self) -> None:
        _HostileCollectorDelta.validate_calls = 0
        hostile = object.__new__(_HostileCollectorDelta)
        store = object.__new__(CollectorDeltaStore)

        with self.assertRaisesRegex(
            TypeError,
            "delta must be exact CollectorDelta",
        ):
            store.append(hostile)

        self.assertEqual(_HostileCollectorDelta.validate_calls, 0)

    def test_projection_rejects_delta_subclass_before_virtual_validation(self) -> None:
        _HostileCollectorDelta.validate_calls = 0
        hostile = object.__new__(_HostileCollectorDelta)
        state = object.__new__(_ContinuousSessionState)

        with self.assertRaisesRegex(
            TypeError,
            "deltas must contain exact CollectorDelta values",
        ):
            state.record_source_projection(
                deltas=(hostile,),
                backlog=False,
            )

        self.assertEqual(_HostileCollectorDelta.validate_calls, 0)

    def test_projection_rejects_boolean_schema_version_before_legacy_validation(self) -> None:
        state = object.__new__(_ContinuousSessionState)
        with self.assertRaisesRegex(
            TypeError,
            "collector delta schema_version must be exact version 1",
        ):
            state.record_source_projection(
                deltas=(_delta(schema_version=True),),
                backlog=False,
            )

    def test_projection_rejects_hostile_identity_scalar_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        state = object.__new__(_ContinuousSessionState)
        with self.assertRaisesRegex(
            TypeError,
            "collector delta delta_id must be exact identity text",
        ):
            state.record_source_projection(
                deltas=(_delta(delta_id=_HostileText("delta-section2")),),
                backlog=False,
            )
        self.assertEqual(_HostileText.strip_calls, 0)


    def test_projection_rejects_hostile_provenance_ref_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        state = object.__new__(_ContinuousSessionState)
        with self.assertRaisesRegex(
            TypeError,
            "collector delta lawful_terms_ref must be exact identity text",
        ):
            state.record_source_projection(
                deltas=(_delta(lawful_terms_ref=_HostileText("terms-section2")),),
                backlog=False,
            )
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_projection_rejects_hostile_correction_counter_before_comparison_dispatch(self) -> None:
        _HostileInt.compare_calls = 0
        state = object.__new__(_ContinuousSessionState)
        with self.assertRaisesRegex(
            TypeError,
            "collector delta revision_number must be an exact integer",
        ):
            state.record_source_projection(
                deltas=(_delta(revision_number=_HostileInt(0)),),
                backlog=False,
            )
        self.assertEqual(_HostileInt.compare_calls, 0)

    def test_projection_rejects_hostile_quality_flag_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        state = object.__new__(_ContinuousSessionState)
        with self.assertRaisesRegex(
            TypeError,
            "collector delta quality_flags must be an exact tuple of exact strings",
        ):
            state.record_source_projection(
                deltas=(_delta(quality_flags=(_HostileText("verified"),)),),
                backlog=False,
            )
        self.assertEqual(_HostileText.strip_calls, 0)


    def test_collector_delta_root_rejects_hostile_provenance_before_strip_dispatch(self) -> None:
        _HostileText.strip_calls = 0
        delta = _delta(lawful_terms_ref=_HostileText("terms-section2"))
        with self.assertRaisesRegex(
            TypeError,
            "lawful_terms_ref must be exact string identity text",
        ):
            delta.validate()
        self.assertEqual(_HostileText.strip_calls, 0)

    def test_collector_delta_root_rejects_hostile_correction_counter_before_comparison(self) -> None:
        _HostileInt.compare_calls = 0
        delta = _delta(revision_number=_HostileInt(0))
        with self.assertRaisesRegex(
            TypeError,
            "revision_number must be an exact integer",
        ):
            delta.validate()
        self.assertEqual(_HostileInt.compare_calls, 0)


if __name__ == "__main__":
    unittest.main()
