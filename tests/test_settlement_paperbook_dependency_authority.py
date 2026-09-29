from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.continuous_session as continuous_session
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


def test_outcome_callback_cannot_retarget_paperbook_before_scope_preflight(
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
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-b",),
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
    hostile_load_calls: list[Path] = []
    canonical_paper_book = continuous_session.PaperBook

    class HostilePaperBook:
        @classmethod
        def load(cls, path):
            hostile_load_calls.append(Path(path))
            return PaperBook("100")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            continuous_session.PaperBook = HostilePaperBook
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
            match="PaperBook|paper book|book.*authority|dependency.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        continuous_session.PaperBook = canonical_paper_book

    assert hostile_load_calls == []


def test_outcome_callback_cannot_retarget_paperbook_load_class_slot(
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
    paper_book_dict = type.__getattribute__(PaperBook, "__dict__")
    canonical_load_descriptor = paper_book_dict["load"]
    hostile_load_calls: list[Path] = []

    def hostile_load(cls, path):
        hostile_load_calls.append(Path(path))
        return PaperBook("100")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            type.__setattr__(PaperBook, "load", classmethod(hostile_load))
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
            match="PaperBook|paper book|book.*authority|dependency.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(PaperBook, "load", canonical_load_descriptor)

    assert hostile_load_calls == []


def test_outcome_callback_cannot_retarget_fresh_paperbook_init_class_slot(
    tmp_path: Path,
) -> None:
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    paper_book_dict = type.__getattribute__(PaperBook, "__dict__")
    canonical_init = paper_book_dict["__init__"]
    hostile_init_calls: list[str] = []

    def hostile_init(self, initial_bankroll=Decimal("10000")):
        hostile_init_calls.append(str(initial_bankroll))
        canonical_init(self, initial_bankroll)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            type.__setattr__(PaperBook, "__init__", hostile_init)
            return resolution

    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = tmp_path / "paper_book.json"
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
            match="PaperBook|paper book|book.*authority|dependency.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(PaperBook, "__init__", canonical_init)

    assert hostile_init_calls == []


def test_outcome_callback_cannot_retarget_fresh_paperbook_new_class_slot(
    tmp_path: Path,
) -> None:
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    paper_book_dict = type.__getattribute__(PaperBook, "__dict__")
    canonical_new_descriptor = paper_book_dict.get("__new__")
    had_own_new = "__new__" in paper_book_dict
    hostile_new_calls: list[str] = []

    def hostile_new(cls, *args, **kwargs):
        hostile_new_calls.append("called")
        return object.__new__(cls)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            type.__setattr__(PaperBook, "__new__", staticmethod(hostile_new))
            return resolution

    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = tmp_path / "paper_book.json"
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
            match="PaperBook|paper book|book.*authority|dependency.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        if had_own_new:
            type.__setattr__(PaperBook, "__new__", canonical_new_descriptor)
        else:
            type.__delattr__(PaperBook, "__new__")

    assert hostile_new_calls == []


def test_outcome_callback_cannot_retarget_paperbook_load_bytes_transitively(
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
    paper_book_dict = type.__getattribute__(PaperBook, "__dict__")
    canonical_descriptor = paper_book_dict["load_bytes"]
    hostile_calls: list[bytes] = []

    def hostile_load_bytes(cls, payload):
        hostile_calls.append(payload)
        return PaperBook("100")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            type.__setattr__(PaperBook, "load_bytes", classmethod(hostile_load_bytes))
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
            match="PaperBook|paper book|book.*authority|dependency.*changed|transitive",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(PaperBook, "load_bytes", canonical_descriptor)

    assert hostile_calls == []


def test_outcome_callback_cannot_retarget_paperbook_snapshot_parser_transitively(
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
    paper_book_dict = type.__getattribute__(PaperBook, "__dict__")
    canonical_descriptor = paper_book_dict["_from_raw_snapshot"]
    hostile_calls: list[object] = []

    def hostile_from_raw_snapshot(cls, raw):
        hostile_calls.append(raw)
        return PaperBook("100")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            type.__setattr__(
                PaperBook,
                "_from_raw_snapshot",
                classmethod(hostile_from_raw_snapshot),
            )
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
            match="PaperBook|paper book|book.*authority|dependency.*changed|transitive",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(PaperBook, "_from_raw_snapshot", canonical_descriptor)

    assert hostile_calls == []
