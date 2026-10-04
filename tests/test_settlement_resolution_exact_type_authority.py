from __future__ import annotations

import pytest

from autosport.continuous_session import (
    SettlementResolution,
    _canonical_settlement_handoff_snapshot,
)


class _CallerSettlementResolution(SettlementResolution):
    def validate(self, *, as_of: str) -> None:  # pragma: no cover - must never dispatch
        raise AssertionError("caller-controlled SettlementResolution.validate dispatched")


def test_canonical_settlement_snapshot_rejects_resolution_subclass_before_virtual_dispatch() -> None:
    resolution = _CallerSettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-26T00:00:00+00:00",
    )

    with pytest.raises(TypeError, match="SettlementResolution"):
        _canonical_settlement_handoff_snapshot(
            (resolution,),
            as_of="2026-09-26T00:00:01+00:00",
        )
