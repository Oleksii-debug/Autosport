from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
    _ContinuousSessionState,
)
from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


_AT = "2026-09-21T14:50:00+00:00"


class _MutatingLearningHandoff:
    def __init__(self, quote_key: str) -> None:
        self.quote_key = quote_key

    def prepare_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        at: str,
    ) -> tuple[str, ...]:
        resolutions[0].quote_outcomes[self.quote_key] = "loss"
        return ()

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        settled_ticket_ids: tuple[str, ...],
        at: str,
    ) -> tuple[str, ...]:
        return ()


def _state(path: Path) -> _ContinuousSessionState:
    return _ContinuousSessionState(
        path,
        session_id="session-toctou",
        source_id="provider-a",
        clock=lambda: _AT,
    )


def _settler(root: Path) -> ContinuousSessionCoordinator:
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator.workspace = root
    coordinator.paper_book_path = root / "paper_book.json"
    coordinator.initial_bankroll = "100"
    return coordinator


def _open_book(root: Path) -> tuple[str, str]:
    book = PaperBook("100")
    leg = TicketLeg(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        locked_odds=Decimal("2.00"),
        sport="table_tennis",
    )
    ticket = book.open_ticket(
        (leg,),
        Decimal("10"),
        placed_at="2026-09-21T14:49:00+00:00",
    )
    book.save(root / "paper_book.json")
    return ticket.ticket_id, leg.quote_key


def _resolution(quote_key: str, outcomes: dict[str, str]) -> SettlementResolution:
    return SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:toctou",
        quote_outcomes=outcomes,
        evidence_id="outcome-toctou",
        evidence_sha256="a" * 64,
        available_at="2026-09-21T14:49:30+00:00",
    )


class SettlementReceiptFirstUseAliasTOCTOUFalsifierTests(unittest.TestCase):
    def _assert_validated_win_cannot_become_loss(
        self,
        *,
        root: Path,
        resolution: SettlementResolution,
    ) -> None:
        coordinator = _settler(root)
        try:
            coordinator._settle(resolutions=(resolution,))
        except (ContinuousSessionError, ValueError):
            unchanged = PaperBook.load(root / "paper_book.json")
            ticket = next(iter(unchanged.tickets.values()))
            self.assertIs(ticket.status, TicketStatus.OPEN)
            self.assertEqual(unchanged.balance, Decimal("90"))
            return

        settled = PaperBook.load(root / "paper_book.json")
        ticket = next(iter(settled.tickets.values()))
        self.assertIs(
            ticket.status,
            TicketStatus.WON,
            "a resolution validated and fingerprinted as WIN must never settle as LOSS "
            "after mutable-alias drift",
        )
        self.assertEqual(settled.balance, Decimal("110"))

    def test_learning_handoff_cannot_change_validated_outcome_before_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ticket_id, quote_key = _open_book(root)
            resolution = _resolution(quote_key, {quote_key: "win"})
            resolution.validate(as_of=_AT)
            _state(root / "continuous_session.json").validate_settlement_evidence(
                settlement_evidence=(resolution,)
            )

            handoff = _MutatingLearningHandoff(quote_key)
            handoff.prepare_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=(resolution,),
                at=_AT,
            )

            self._assert_validated_win_cannot_become_loss(
                root=root,
                resolution=resolution,
            )

    def test_outcome_authority_retained_alias_cannot_change_validated_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ticket_id, quote_key = _open_book(root)
            authority_owned_outcomes = {quote_key: "win"}
            resolution = _resolution(quote_key, authority_owned_outcomes)
            resolution.validate(as_of=_AT)
            _state(root / "continuous_session.json").validate_settlement_evidence(
                settlement_evidence=(resolution,)
            )

            authority_owned_outcomes[quote_key] = "loss"

            self._assert_validated_win_cannot_become_loss(
                root=root,
                resolution=resolution,
            )


if __name__ == "__main__":
    unittest.main()
