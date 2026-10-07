import unittest

from autosport.causal_collector import CollectorDelta
from autosport.continuous_session import _ContinuousSessionState


class _HostileCollectorDelta(CollectorDelta):
    validate_calls = 0

    def validate(self) -> None:
        type(self).validate_calls += 1


class _HostileText(str):
    strip_calls = 0

    def strip(self, *args, **kwargs):
        type(self).strip_calls += 1
        return super().strip(*args, **kwargs)


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


if __name__ == "__main__":
    unittest.main()
