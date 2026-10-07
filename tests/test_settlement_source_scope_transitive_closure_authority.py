from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_HOSTILE_CLASSIFIER_CALLS: list[str] = []


def _hostile_classifier_with_matching_closure_shape():
    exact_any = any
    exact_type = type
    session_module = object()

    def hostile(_ticket, _leg, event_identity):
        _ = (exact_any, exact_type, session_module)
        _HOSTILE_CLASSIFIER_CALLS.append(event_identity)
        return "sourced"

    return hostile


def _scope_target():
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    descriptor = coordinator_dict["_open_quote_keys_for_book"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    target = descriptor_get(descriptor, None, coordinator_type)
    closure = target.__closure__ or ()
    freevars = target.__code__.co_freevars
    assert "scoped_open_quote_keys" in freevars
    scope_cell = closure[freevars.index("scoped_open_quote_keys")]
    return target, scope_cell


def test_economic_entry_rejects_source_scope_closure_retarget_before_dispatch(
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

    _target, scope_cell = _scope_target()
    canonical_scope = scope_cell.cell_contents
    hostile_calls: list[str] = []

    def hostile_scope(_book, event_identity, *, candidate_quote_keys=None):
        hostile_calls.append(event_identity)
        return set()

    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        scope_cell.cell_contents = hostile_scope
        with pytest.raises(
            ContinuousSessionError,
            match="settlement coordinator dispatch changed|source-scope helper authority changed",
        ):
            coordinator._settle(resolutions=(resolution,))
        assert hostile_calls == []
    finally:
        scope_cell.cell_contents = canonical_scope


def test_economic_entry_rejects_nested_source_classifier_code_swap() -> None:
    _HOSTILE_CLASSIFIER_CALLS.clear()
    _target, scope_cell = _scope_target()
    scoped = scope_cell.cell_contents
    scoped_closure = scoped.__closure__ or ()
    scoped_freevars = scoped.__code__.co_freevars
    assert "leg_event_scope_match_kind" in scoped_freevars
    classifier = scoped_closure[
        scoped_freevars.index("leg_event_scope_match_kind")
    ].cell_contents
    canonical_code = classifier.__code__
    hostile = _hostile_classifier_with_matching_closure_shape()
    assert len(hostile.__code__.co_freevars) == len(canonical_code.co_freevars)

    coordinator = object.__new__(ContinuousSessionCoordinator)
    try:
        classifier.__code__ = hostile.__code__
        with pytest.raises(
            ContinuousSessionError,
            match="settlement coordinator dispatch changed",
        ):
            _ = coordinator._settle
        assert _HOSTILE_CLASSIFIER_CALLS == []
    finally:
        classifier.__code__ = canonical_code
        _HOSTILE_CLASSIFIER_CALLS.clear()
