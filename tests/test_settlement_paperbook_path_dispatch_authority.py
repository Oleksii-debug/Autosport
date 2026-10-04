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


def _paperbook_load_authority_globals() -> dict[str, object]:
    load_target = vars(PaperBook)["load"].__func__
    if "_FROZEN_LOAD" in load_target.__globals__:
        return load_target.__globals__
    closure = load_target.__closure__
    if closure is None:
        raise AssertionError("guarded PaperBook.load closure is unavailable")
    freevars = load_target.__code__.co_freevars
    if "trusted_globals" not in freevars:
        raise AssertionError("guarded PaperBook.load trusted globals are unavailable")
    mapping = closure[freevars.index("trusted_globals")].cell_contents
    if type(mapping) is not dict:
        raise AssertionError("guarded PaperBook.load trusted globals must be an exact dict")
    return mapping


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def _coordinator(tmp_path: Path, resolution: SettlementResolution):
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = tmp_path / "paper_book.json"
    coordinator.initial_bankroll = "100"
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )
    return coordinator


def _book_and_resolution(tmp_path: Path):
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    book.save(tmp_path / "paper_book.json")
    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    return resolution


def test_outcome_callback_cannot_retarget_paperbook_path_reader(
    tmp_path: Path,
) -> None:
    resolution = _book_and_resolution(tmp_path)
    load_authority_globals = _paperbook_load_authority_globals()
    frozen_load = load_authority_globals["_FROZEN_LOAD"]
    frozen_load_globals = frozen_load.__globals__
    canonical_path = frozen_load_globals["_PATH"]
    hostile_calls: list[object] = []

    class HostilePath:
        def __init__(self, value: object) -> None:
            hostile_calls.append(value)

        def read_bytes(self) -> bytes:
            raise AssertionError("hostile PaperBook path reader executed")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            frozen_load_globals["_PATH"] = HostilePath
            return resolution

    coordinator = _coordinator(tmp_path, resolution)
    coordinator.outcome_authority = OutcomeAuthority()

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|path|dependency.*changed|dispatch.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        frozen_load_globals["_PATH"] = canonical_path

    assert hostile_calls == []


def test_outcome_callback_cannot_retarget_guarded_paperbook_frozen_load_delegate(
    tmp_path: Path,
) -> None:
    resolution = _book_and_resolution(tmp_path)
    load_globals = _paperbook_load_authority_globals()
    canonical_frozen_load = load_globals["_FROZEN_LOAD"]
    hostile_calls: list[object] = []

    def hostile_frozen_load(cls, path):
        hostile_calls.append((cls, path))
        raise AssertionError("hostile frozen PaperBook load executed")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            load_globals["_FROZEN_LOAD"] = hostile_frozen_load
            return resolution

    coordinator = _coordinator(tmp_path, resolution)
    coordinator.outcome_authority = OutcomeAuthority()

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|load|dependency.*changed|executable.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        load_globals["_FROZEN_LOAD"] = canonical_frozen_load

    assert hostile_calls == []


def test_outcome_callback_cannot_retarget_paperbook_read_bytes_class_slot(
    tmp_path: Path,
) -> None:
    resolution = _book_and_resolution(tmp_path)
    canonical_path = paper_module.Path
    concrete_path_type = type(canonical_path(tmp_path / "paper_book.json"))
    read_owner = next(
        owner for owner in concrete_path_type.__mro__ if "read_bytes" in owner.__dict__
    )
    read_owner_dict = type.__getattribute__(read_owner, "__dict__")
    canonical_read_bytes = read_owner_dict["read_bytes"]
    hostile_calls: list[str] = []

    def hostile_read_bytes(self):
        hostile_calls.append("called")
        raise AssertionError("hostile PaperBook read_bytes executed")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            type.__setattr__(read_owner, "read_bytes", hostile_read_bytes)
            return resolution

    coordinator = _coordinator(tmp_path, resolution)
    coordinator.outcome_authority = OutcomeAuthority()

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|path|dependency.*changed|dispatch.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(read_owner, "read_bytes", canonical_read_bytes)

    assert hostile_calls == []


def test_outcome_callback_cannot_retarget_paperbook_path_io_dependency(
    tmp_path: Path,
) -> None:
    resolution = _book_and_resolution(tmp_path)
    canonical_path = paper_module.Path
    concrete_path_type = type(canonical_path(tmp_path / "paper_book.json"))
    open_owner = next(
        owner for owner in concrete_path_type.__mro__ if "open" in owner.__dict__
    )
    open_target = type.__getattribute__(open_owner, "__dict__")["open"]
    open_globals = open_target.__globals__
    canonical_io = open_globals["io"]
    hostile_calls: list[object] = []

    class HostileIO:
        @staticmethod
        def open(*args, **kwargs):
            hostile_calls.append((args, kwargs))
            raise AssertionError("hostile PaperBook io.open executed")

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            open_globals["io"] = HostileIO
            return resolution

    coordinator = _coordinator(tmp_path, resolution)
    coordinator.outcome_authority = OutcomeAuthority()

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="PaperBook|paper book|path|io|dependency.*changed|dispatch.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        open_globals["io"] = canonical_io

    assert hostile_calls == []
