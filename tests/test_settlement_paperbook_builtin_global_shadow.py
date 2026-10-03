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


def test_outcome_callback_cannot_shadow_tuple_in_paper_globals(
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
        settlement_ref="provider-result:builtin-global-shadow",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-builtin-global-shadow",
        evidence_sha256="5" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    canonical_tuple = tuple
    shadow_calls: list[object] = []
    sentinel = object()
    previous_tuple_global = paper_module.__dict__.get("tuple", sentinel)

    def hostile_tuple(*args):
        if (
            len(args) == 1
            and type(args[0]) is list
            and args[0] == ["provider-a"]
        ):
            shadow_calls.append(args[0])
            paper_module.__dict__.pop("tuple", None)
            return ()
        return canonical_tuple(*args)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            # LOAD_GLOBAL resolves a function's globals before its builtins mapping.
            # Adding this name therefore bypasses a witness that checks only
            # function.__builtins__["tuple"].
            paper_module.__dict__["tuple"] = hostile_tuple
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
            match="PaperBook builtin authority changed: tuple|materialization preflight changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        if previous_tuple_global is sentinel:
            paper_module.__dict__.pop("tuple", None)
        else:
            paper_module.__dict__["tuple"] = previous_tuple_global

    assert shadow_calls == []
