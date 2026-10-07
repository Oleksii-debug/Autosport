import unittest

from autosport.causal_collector import CollectorDelta
from autosport.continuous_session import _ContinuousSessionState


class _HostileCollectorDelta(CollectorDelta):
    validate_calls = 0

    def validate(self) -> None:
        type(self).validate_calls += 1


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


if __name__ == "__main__":
    unittest.main()
