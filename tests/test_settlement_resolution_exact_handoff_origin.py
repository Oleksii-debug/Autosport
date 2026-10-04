from __future__ import annotations

import pytest

from autosport.continuous_session import (
    SettlementResolution,
    _canonical_settlement_handoff_snapshot,
)


class _HostileSettlementResolution(SettlementResolution):
    validate_calls = 0
    field_reads = 0

    def validate(self, *, as_of: str) -> None:
        type(self).validate_calls += 1

    def __getattribute__(self, name: str):
        if name in {
            "event_identity",
            "settlement_ref",
            "quote_outcomes",
            "evidence_id",
            "evidence_sha256",
            "available_at",
        }:
            type(self).field_reads += 1
        return super().__getattribute__(name)


def test_canonical_settlement_handoff_rejects_resolution_subclass_before_dispatch() -> None:
    _HostileSettlementResolution.validate_calls = 0
    _HostileSettlementResolution.field_reads = 0
    hostile = _HostileSettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-26T20:00:00Z",
    )

    with pytest.raises(TypeError, match="exact SettlementResolution"):
        _canonical_settlement_handoff_snapshot(
            (hostile,),
            as_of="2026-09-26T20:00:00Z",
        )

    assert _HostileSettlementResolution.validate_calls == 0
    assert _HostileSettlementResolution.field_reads == 0
