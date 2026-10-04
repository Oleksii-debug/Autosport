from __future__ import annotations

import builtins
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_outcome_callback_cannot_retarget_len_to_hide_multisource_ticket(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a", "provider-b"),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:multi-source-builtins",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-multi-source-builtins",
        evidence_sha256="3" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    canonical_len = builtins.len
    hidden_scope_calls: list[tuple[str, ...]] = []

    def hostile_len(value):
        # Earlier canonical post-callback graph checks also use len(). Delegate every
        # unrelated call faithfully. Only the multi-source provenance tuple would be
        # forged, then self-restore before a later postcheck could observe the binding.
        if type(value) is tuple and value == ("provider-a", "provider-b"):
            hidden_scope_calls.append(value)
            builtins.len = canonical_len
            return 1
        return canonical_len(value)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            builtins.len = hostile_len
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
            match="multi-source builtin authority changed|builtin authority changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        builtins.len = canonical_len

    assert hidden_scope_calls == []
