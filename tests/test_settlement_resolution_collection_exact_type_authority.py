from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    EventPhase,
    SettlementResolution,
)


_AT = "2026-09-26T20:00:00Z"


class _HostileSettlementResolution(SettlementResolution):
    validate_calls = 0
    field_reads = 0

    def validate(self, *, as_of: str) -> None:
        type(self).validate_calls += 1

    def __getattribute__(self, name: str):
        if name in {"event_identity", "settlement_ref"}:
            type(self).field_reads += 1
        return super().__getattribute__(name)


class _OutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self._resolution = resolution

    def resolve(self, record: object, *, as_of: str) -> SettlementResolution:
        del record, as_of
        return self._resolution


class _Lifecycle:
    def records(self) -> tuple[object, ...]:
        return (
            SimpleNamespace(
                phase=EventPhase.COMPLETED,
                settlement_ref="settlement-1",
                identity="event-1",
            ),
        )


def test_settlement_collection_rejects_resolution_subclass_before_dispatch() -> None:
    _HostileSettlementResolution.validate_calls = 0
    _HostileSettlementResolution.field_reads = 0
    hostile = _HostileSettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )
    fake = SimpleNamespace(
        outcome_authority=_OutcomeAuthority(hostile),
        lifecycle=_Lifecycle(),
    )

    with pytest.raises(
        ContinuousSessionError,
        match="exact SettlementResolution|SettlementResolution.*exact|outcome authority",
    ):
        ContinuousSessionCoordinator._settlement_resolutions(fake, as_of=_AT)

    assert _HostileSettlementResolution.field_reads == 0
    assert _HostileSettlementResolution.validate_calls == 0
