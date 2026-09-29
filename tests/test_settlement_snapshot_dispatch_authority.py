from __future__ import annotations

from types import SimpleNamespace

import pytest

import autosport.continuous_session as continuous_session
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    EventPhase,
    SettlementResolution,
)


_AT = "2026-09-26T20:00:00Z"


def _resolution(*, outcome: str, evidence_id: str) -> SettlementResolution:
    return SettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": outcome},
        evidence_id=evidence_id,
        evidence_sha256=("a" if outcome == "win" else "b") * 64,
        available_at=_AT,
    )


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


def _forged_snapshot(resolutions, *, as_of=None):
    global _FORGED_SNAPSHOT_CALLS
    _FORGED_SNAPSHOT_CALLS += 1
    del resolutions, as_of
    return _FORGED_SNAPSHOT_RESULT


def test_settlement_resolution_collection_rejects_rebound_snapshot_dispatch() -> None:
    authoritative = _resolution(outcome="win", evidence_id="authority-evidence")
    forged = _resolution(outcome="loss", evidence_id="forged-evidence")

    fake = SimpleNamespace(
        outcome_authority=_OutcomeAuthority(authoritative),
        lifecycle=_Lifecycle(),
        _settlement_handoff_snapshot=lambda resolutions, *, as_of=None: (forged,),
    )

    with pytest.raises(
        ContinuousSessionError,
        match="settlement.*snapshot|settlement.*origin|settlement.*dispatch",
    ):
        ContinuousSessionCoordinator._settlement_resolutions(fake, as_of=_AT)


def test_settlement_resolution_collection_rejects_snapshot_code_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authoritative = _resolution(outcome="win", evidence_id="authority-evidence")
    forged = _resolution(outcome="loss", evidence_id="forged-evidence")
    canonical_snapshot = continuous_session._canonical_settlement_handoff_snapshot

    monkeypatch.setattr(continuous_session, "_FORGED_SNAPSHOT_CALLS", 0, raising=False)
    monkeypatch.setattr(
        continuous_session,
        "_FORGED_SNAPSHOT_RESULT",
        (forged,),
        raising=False,
    )
    monkeypatch.setattr(canonical_snapshot, "__code__", _forged_snapshot.__code__)

    fake = SimpleNamespace(
        outcome_authority=_OutcomeAuthority(authoritative),
        lifecycle=_Lifecycle(),
        _settlement_handoff_snapshot=canonical_snapshot,
    )

    with pytest.raises(
        ContinuousSessionError,
        match="settlement.*snapshot|settlement.*origin|settlement.*dispatch|executable",
    ):
        ContinuousSessionCoordinator._settlement_resolutions(fake, as_of=_AT)

    assert continuous_session._FORGED_SNAPSHOT_CALLS == 0
