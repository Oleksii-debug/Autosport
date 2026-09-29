from __future__ import annotations

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


def test_outcome_callback_cannot_retarget_paperbook_decimal_context_before_load(
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
        settlement_ref="provider-result:decimal-context",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-decimal-context",
        evidence_sha256="3" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    canonical_context = paper_module._paper_decimal_context
    hostile_calls = 0

    def hostile_context():
        nonlocal hostile_calls
        hostile_calls += 1
        # Self-restore so a post-load identity check alone cannot observe that hostile
        # replay arithmetic already ran inside the canonical PaperBook validator.
        paper_module._paper_decimal_context = canonical_context
        return canonical_context()

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            paper_module._paper_decimal_context = hostile_context
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
            match="PaperBook.*materialization|materialization dependency|_paper_decimal_context",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        paper_module._paper_decimal_context = canonical_context

    assert hostile_calls == 0
