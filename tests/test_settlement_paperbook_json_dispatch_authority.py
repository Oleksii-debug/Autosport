from __future__ import annotations

import json as canonical_json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.paper as paper_module
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg
from autosport.event_lifecycle import EventPhase
from autosport.paper import PaperBook


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def test_outcome_callback_cannot_substitute_self_restoring_json_parser(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    canonical_json_dependency = paper_module.json
    hostile_calls: list[str] = []

    class HostileJSON:
        @staticmethod
        def loads(text, **kwargs):
            hostile_calls.append("called")
            paper_module.json = canonical_json_dependency
            raw = canonical_json.loads(text, **kwargs)
            raw["tickets"] = []
            raw["lifecycle"] = []
            raw["balance"] = raw["initial_bankroll"]
            return raw

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            paper_module.json = HostileJSON
            return resolution

    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|json|parser|dependency.*changed|dispatch.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        paper_module.json = canonical_json_dependency

    assert hostile_calls == []
