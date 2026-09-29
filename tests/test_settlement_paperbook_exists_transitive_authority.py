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


def _book_and_resolution(tmp_path: Path) -> SettlementResolution:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    book.save(tmp_path / "paper_book.json")
    return SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )


def _coordinator(tmp_path: Path, resolution: SettlementResolution, authority):
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = tmp_path / "paper_book.json"
    coordinator.initial_bankroll = "100"
    coordinator.outcome_authority = authority
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )
    return coordinator


def _path_slot(name: str, path: Path):
    concrete_path_type = type(paper_module.Path(path))
    owner = next(
        candidate for candidate in concrete_path_type.__mro__ if name in candidate.__dict__
    )
    slot = type.__getattribute__(owner, "__dict__")[name]
    return owner, slot


def test_outcome_callback_cannot_hide_durable_book_via_self_restoring_stat_slot(
    tmp_path: Path,
) -> None:
    resolution = _book_and_resolution(tmp_path)
    stat_owner, canonical_stat = _path_slot("stat", tmp_path / "paper_book.json")
    hostile_calls: list[str] = []

    def hostile_stat(self, *args, **kwargs):
        del self, args, kwargs
        hostile_calls.append("called")
        type.__setattr__(stat_owner, "stat", canonical_stat)
        raise OSError("hide durable PaperBook")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            type.__setattr__(stat_owner, "stat", hostile_stat)
            return resolution

    coordinator = _coordinator(tmp_path, resolution, OutcomeAuthority())
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|path|stat|dispatch.*changed|authority.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(stat_owner, "stat", canonical_stat)

    assert hostile_calls == []


def test_outcome_callback_cannot_hide_durable_book_via_self_restoring_os_stat(
    tmp_path: Path,
) -> None:
    resolution = _book_and_resolution(tmp_path)
    _stat_owner, stat_target = _path_slot("stat", tmp_path / "paper_book.json")
    stat_globals = stat_target.__globals__
    canonical_os = stat_globals["os"]
    hostile_calls: list[str] = []

    class HostileOS:
        def __getattr__(self, name: str):
            return getattr(canonical_os, name)

        def stat(self, *args, **kwargs):
            del args, kwargs
            hostile_calls.append("called")
            stat_globals["os"] = canonical_os
            raise OSError("hide durable PaperBook")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            stat_globals["os"] = HostileOS()
            return resolution

    coordinator = _coordinator(tmp_path, resolution, OutcomeAuthority())
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|path|stat|os|dispatch.*changed|authority.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        stat_globals["os"] = canonical_os

    assert hostile_calls == []
