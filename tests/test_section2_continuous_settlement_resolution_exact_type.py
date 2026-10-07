import unittest
from types import SimpleNamespace

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.event_lifecycle import EventPhase


class _Lifecycle:
    def records(self):
        return (
            SimpleNamespace(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:section2",
                identity="event-section2",
            ),
        )


class _OutcomeAuthority:
    def __init__(self, resolution):
        self.resolution = resolution

    def resolve(self, record, *, as_of: str):
        return self.resolution


class _HostileSettlementResolution(SettlementResolution):
    validate_calls = 0

    def validate(self, *, as_of: str) -> None:
        type(self).validate_calls += 1


class Section2ContinuousSettlementIdentityTests(unittest.TestCase):
    def test_outcome_resolution_subclass_is_rejected_before_virtual_validation(self) -> None:
        _HostileSettlementResolution.validate_calls = 0
        resolution = _HostileSettlementResolution(
            event_identity="event-section2",
            settlement_ref="provider-result:section2",
            quote_outcomes={"quote-section2": "win"},
            evidence_id="evidence-section2",
            evidence_sha256="0" * 64,
            available_at="2099-01-01T00:00:00+00:00",
        )
        coordinator = object.__new__(ContinuousSessionCoordinator)
        coordinator.lifecycle = _Lifecycle()
        coordinator.outcome_authority = _OutcomeAuthority(resolution)

        with self.assertRaisesRegex(
            ContinuousSessionError,
            "outcome authority must return exact SettlementResolution or None",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-10-07T03:45:00+00:00"
            )

        self.assertEqual(_HostileSettlementResolution.validate_calls, 0)


if __name__ == "__main__":
    unittest.main()
