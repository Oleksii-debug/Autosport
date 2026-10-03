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


def test_outcome_callback_cannot_replace_tuple_to_erase_provider_provenance(
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
        settlement_ref="provider-result:actual-builtins",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-actual-builtins",
        evidence_sha256="4" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    canonical_tuple = builtins.tuple
    erased_provenance_calls: list[object] = []

    def hostile_tuple(*args):
        # Delegate every unrelated tuple() call faithfully. The only semantic payload
        # is the JSON list that canonical _from_raw_snapshot() converts into durable
        # provider_source_ids. If reached, erase provenance and self-restore so a later
        # identity-only postcheck would see canonical builtins again.
        if (
            len(args) == 1
            and type(args[0]) is list
            and args[0] == ["provider-a"]
        ):
            erased_provenance_calls.append(args[0])
            builtins.tuple = canonical_tuple
            return ()
        return canonical_tuple(*args)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            builtins.tuple = hostile_tuple
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
        builtins.tuple = canonical_tuple

    assert erased_provenance_calls == []

def test_outcome_callback_cannot_retarget_prelearning_len_dispatch(
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
        settlement_ref="provider-result:prelearning-len",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-prelearning-len",
        evidence_sha256="5" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    canonical_len = builtins.len
    canonical_type = builtins.type
    canonical_all = builtins.all
    hostile_calls: list[str] = []

    def hostile_len(value):
        if (
            canonical_type(value) is tuple
            and value
            and canonical_all(canonical_type(item).__name__ == "cell" for item in value)
        ):
            hostile_calls.append("prelearning-closure")
            builtins.len = canonical_len
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
            match="PaperBook .*builtin|builtin.*authority|canonical.*builtin",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
        assert hostile_calls == []
    finally:
        builtins.len = canonical_len

