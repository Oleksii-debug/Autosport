"""Plan 2 §1: provider and learning seams must not retarget PAPER outcome truth."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    SettlementResolution,
)
from autosport.event_lifecycle import EventPhase


_CUTOFF = "2026-10-08T06:00:00+00:00"
_OUTCOME_KEY = "quote-section2"


def _resolution() -> SettlementResolution:
    return SettlementResolution(
        event_identity="event-section2",
        settlement_ref="provider-result:section2",
        quote_outcomes={_OUTCOME_KEY: "loss"},
        evidence_id="evidence-section2",
        evidence_sha256="0" * 64,
        available_at=_CUTOFF,
    )


class _Lifecycle:
    def records(self):
        return (
            SimpleNamespace(
                phase=EventPhase.COMPLETED,
                identity="event-section2",
                settlement_ref="provider-result:section2",
            ),
        )


class _Authority:
    def __init__(self, resolution: SettlementResolution):
        self.resolution = resolution

    def resolve(self, record, *, as_of):
        return self.resolution


def test_outcome_provider_cannot_mutate_already_validated_product_snapshot():
    source_resolution = _resolution()
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.lifecycle = _Lifecycle()
    coordinator.outcome_authority = _Authority(source_resolution)

    selected = coordinator._settlement_resolutions(as_of=_CUTOFF)
    assert len(selected) == 1
    assert selected[0] is not source_resolution
    assert selected[0].quote_outcomes is not source_resolution.quote_outcomes

    # A provider-owned reference is not financial authority once selected.
    source_resolution.quote_outcomes[_OUTCOME_KEY] = "win"
    assert selected[0].quote_outcomes == {_OUTCOME_KEY: "loss"}
    assert selected[0].evidence_id == source_resolution.evidence_id


def test_learning_handoff_cannot_mutate_authoritative_settlement_resolution():
    coordinator = object.__new__(ContinuousSessionCoordinator)
    selected = (_resolution(),)
    handoff_view = coordinator._detached_settlement_resolutions(selected)
    assert handoff_view[0] is not selected[0]
    assert handoff_view[0].quote_outcomes is not selected[0].quote_outcomes

    # This is the exact mutation a buggy prepare_settlement hook could attempt.
    handoff_view[0].quote_outcomes[_OUTCOME_KEY] = "win"
    assert selected[0].quote_outcomes == {_OUTCOME_KEY: "loss"}

    reconciliation_view = coordinator._detached_settlement_resolutions(selected)
    reconciliation_view[0].quote_outcomes[_OUTCOME_KEY] = "void"
    assert selected[0].quote_outcomes == {_OUTCOME_KEY: "loss"}


@pytest.mark.parametrize("bad", [None, [_resolution()], "not-resolutions"])
def test_snapshot_seam_rejects_non_tuple_input_without_coercion(bad):
    with pytest.raises(TypeError, match="exact tuple"):
        ContinuousSessionCoordinator._detached_settlement_resolutions(bad)


def test_snapshot_seam_rejects_resolution_subclasses_before_virtual_dispatch():
    class HostileResolution(SettlementResolution):
        def validate(self, *, as_of):
            raise AssertionError("untrusted overridden validator must not run")

    source = _resolution()
    hostile = HostileResolution(
        event_identity=source.event_identity,
        settlement_ref=source.settlement_ref,
        quote_outcomes=source.quote_outcomes,
        evidence_id=source.evidence_id,
        evidence_sha256=source.evidence_sha256,
        available_at=source.available_at,
    )
    with pytest.raises(TypeError, match="exact SettlementResolution"):
        ContinuousSessionCoordinator._detached_settlement_resolutions((hostile,))


def test_snapshot_rejects_hostile_mapping_before_custom_copy_dispatch():
    class HostileMapping(dict):
        def copy(self):
            raise AssertionError("caller-owned mapping copy must not run")

    source = _resolution()
    from dataclasses import replace

    hostile = replace(source, quote_outcomes=HostileMapping(source.quote_outcomes))
    with pytest.raises(ValueError, match="quote_outcomes must be a non-empty exact dict"):
        ContinuousSessionCoordinator._detached_settlement_resolutions((hostile,))


def test_alias_mutation_does_not_flip_paper_cash_and_restart_is_idempotent(tmp_path):
    from decimal import Decimal

    from autosport.domain import TicketLeg, TicketStatus
    from autosport.paper import PaperBook

    book_path = tmp_path / "paper_book.json"
    book = PaperBook("100")
    leg = TicketLeg(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        locked_odds=Decimal("2"),
        sport="table_tennis",
    )
    ticket = book.open_ticket(
        (leg,), Decimal("10"), placed_at="2026-09-19T21:19:30+00:00"
    )
    book.save(book_path)

    source = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:section2",
        quote_outcomes={leg.quote_key: "loss"},
        evidence_id="evidence-cash-isolation",
        evidence_sha256="0" * 64,
        available_at=_CUTOFF,
    )
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = book_path
    coordinator.initial_bankroll = "100"

    selected = coordinator._detached_settlement_resolutions((source,))
    source.quote_outcomes[leg.quote_key] = "win"
    handoff_copy = coordinator._detached_settlement_resolutions(selected)
    handoff_copy[0].quote_outcomes[leg.quote_key] = "win"

    settled, ids = coordinator._settle(resolutions=selected)
    assert settled == (ticket.ticket_id,)
    assert ids == ("evidence-cash-isolation",)
    persisted = PaperBook.load(book_path)
    assert persisted.balance == Decimal("90")
    assert persisted.tickets[ticket.ticket_id].status is TicketStatus.LOST

    # A durable replay/restart of the same original evidence has no second debit.
    settled_again, _ = coordinator._settle(resolutions=selected)
    assert settled_again == ()
    reopened = PaperBook.load(book_path)
    assert reopened.balance == Decimal("90")
    assert reopened.tickets[ticket.ticket_id].status is TicketStatus.LOST


def test_reused_evidence_id_with_conflicting_payload_fails_before_paper_effect(tmp_path):
    """An identical evidence ID is not permission to replace a quote outcome."""
    from dataclasses import replace
    from decimal import Decimal

    from autosport.continuous_session import ContinuousSessionError
    from autosport.domain import TicketLeg, TicketStatus
    from autosport.paper import PaperBook

    path = tmp_path / "paper.json"
    book = PaperBook("100")
    leg = TicketLeg(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        locked_odds=Decimal("2"),
        sport="table_tennis",
    )
    ticket = book.open_ticket(
        (leg,), Decimal("10"), placed_at="2026-10-08T05:00:00+00:00"
    )
    book.save(path)
    before = path.read_bytes()
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = path
    coordinator.initial_bankroll = "100"
    losing = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="result:1",
        quote_outcomes={leg.quote_key: "loss"},
        evidence_id="same-evidence-id",
        evidence_sha256="0" * 64,
        available_at=_CUTOFF,
    )
    contradictory = replace(
        losing, quote_outcomes={leg.quote_key: "win"}
    )

    for ordered in ((losing, contradictory), (contradictory, losing)):
        # Earlier than any optional settlement/learning handoff.
        with pytest.raises(
            ContinuousSessionError,
            match="conflicting settlement payload for reused evidence_id",
        ):
            coordinator._detached_settlement_resolutions(ordered)
        with pytest.raises(
            ContinuousSessionError,
            match="conflicting settlement payload for reused evidence_id",
        ):
            coordinator._settle(resolutions=ordered)
        assert path.read_bytes() == before
        original = PaperBook.load(path)
        assert original.balance == Decimal("90")
        assert original.tickets[ticket.ticket_id].status is TicketStatus.OPEN

    # Identical duplicate delivery is idempotent, not a second settlement.
    settled, ids = coordinator._settle(resolutions=(losing, losing))
    assert settled == (ticket.ticket_id,)
    assert ids == ("same-evidence-id",)
    reloaded = PaperBook.load(path)
    assert reloaded.balance == Decimal("90")
    assert reloaded.tickets[ticket.ticket_id].status is TicketStatus.LOST

    replayed, ids_again = coordinator._settle(resolutions=(losing, losing))
    assert replayed == ()
    assert ids_again == ids
    assert PaperBook.load(path).balance == Decimal("90")
