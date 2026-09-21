from __future__ import annotations

import json
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
from autosport.settlement import SettlementEngine


_AT = "2026-09-21T08:40:00+00:00"


def _resolution(*, outcome: str = "win", settlement_ref: str = "provider-result:1"):
    return SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref=settlement_ref,
        quote_outcomes={"receipt-quote-1": outcome},
        evidence_id="receipt-1",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )


def _state(path: Path) -> _ContinuousSessionState:
    return _ContinuousSessionState(
        path,
        session_id="session-1",
        source_id="provider-a",
        clock=lambda: _AT,
    )


class SettlementReceiptIdentityTests(unittest.TestCase):
    def test_same_batch_receipt_id_cannot_rebind_outcome_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory) / "continuous_session.json")
            with self.assertRaisesRegex(
                ContinuousSessionError,
                "settlement evidence id conflicts",
            ):
                state.validate_settlement_evidence(
                    settlement_evidence=(
                        _resolution(outcome="win"),
                        _resolution(outcome="loss"),
                    )
                )

    def test_restart_receipt_id_cannot_rebind_outcome_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuous_session.json"
            state = _state(path)
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(_resolution(outcome="win"),),
            )
            restarted = _state(path)
            with self.assertRaisesRegex(
                ContinuousSessionError,
                "settlement evidence id conflicts",
            ):
                restarted.validate_settlement_evidence(
                    settlement_evidence=(_resolution(outcome="void"),)
                )

    def test_legacy_receipt_is_readable_but_cannot_be_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuous_session.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "autosport.continuous_session",
                        "schema_version": 2,
                        "session_id": "session-1",
                        "source_id": "provider-a",
                        "state": "RUNNING",
                        "started_at": _AT,
                        "cycles_completed": 1,
                        "last_success_at": _AT,
                        "last_error_code": None,
                        "last_full_refresh_at": None,
                        "settlement_evidence": [
                            {
                                "event_identity": "provider-a:event-1",
                                "settlement_ref": "provider-result:1",
                                "evidence_id": "receipt-1",
                                "evidence_sha256": "a" * 64,
                                "available_at": _AT,
                            }
                        ],
                        "source_gap_state": None,
                        "source_sync_state": None,
                        "source_state_delta_id": None,
                        "source_unresolved_gap_delta_ids": [],
                        "source_projection_stream_epoch": None,
                        "source_state_projection_backlog": False,
                    }
                ),
                encoding="utf-8",
            )
            state = _state(path)
            self.assertIsNone(
                state.snapshot().settlement_evidence[0]["quote_outcomes_sha256"]
            )
            with self.assertRaisesRegex(
                ContinuousSessionError,
                "legacy settlement evidence cannot be safely rebound",
            ):
                state.validate_settlement_evidence(
                    settlement_evidence=(_resolution(outcome="win"),)
                )

    def test_outcome_fingerprint_is_durable_and_order_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuous_session.json"
            state = _state(path)
            first = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="provider-result:1",
                quote_outcomes={
                    "quote-b": "void",
                    "quote-a": "win",
                },
                evidence_id="receipt-order",
                evidence_sha256="b" * 64,
                available_at=_AT,
            )
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(first,),
            )
            raw_state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw_state["schema_version"], 3)
            fingerprint = raw_state["settlement_evidence"][0][
                "quote_outcomes_sha256"
            ]
            self.assertIsInstance(fingerprint, str)
            self.assertEqual(len(fingerprint), 64)

            reordered = SettlementResolution(
                event_identity=first.event_identity,
                settlement_ref=first.settlement_ref,
                quote_outcomes={
                    "quote-a": "win",
                    "quote-b": "void",
                },
                evidence_id=first.evidence_id,
                evidence_sha256=first.evidence_sha256,
                available_at=first.available_at,
            )
            _state(path).validate_settlement_evidence(
                settlement_evidence=(reordered,)
            )

    def test_duplicate_durable_receipt_ids_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuous_session.json"
            state = _state(path)
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(_resolution(),),
            )
            raw_state = json.loads(path.read_text(encoding="utf-8"))
            duplicate = dict(raw_state["settlement_evidence"][0])
            duplicate["quote_outcomes_sha256"] = "c" * 64
            raw_state["settlement_evidence"].append(duplicate)
            path.write_text(json.dumps(raw_state), encoding="utf-8")
            with self.assertRaisesRegex(
                ContinuousSessionError,
                "evidence_id values must be unique",
            ):
                _state(path)

    def test_push_is_not_a_canonical_receipt_outcome(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported outcome"):
            _resolution(outcome="push").validate(as_of=_AT)

    def test_all_void_reuses_paper_semantics_and_returns_stake(self) -> None:
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
            placed_at="2026-09-21T08:39:00+00:00",
        )
        engine = SettlementEngine()
        engine.record({leg.quote_key: "void"})
        self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
        self.assertIs(ticket.status, TicketStatus.VOID)
        self.assertEqual(ticket.payout, Decimal("10"))
        self.assertEqual(book.balance, Decimal("100"))

    def test_validation_detaches_outcome_authority_owned_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book_path = root / "paper_book.json"
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
                placed_at="2026-09-21T08:39:00+00:00",
            )
            book.save(book_path)

            authority_outcomes = {leg.quote_key: "win"}
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="provider-result:alias",
                quote_outcomes=authority_outcomes,
                evidence_id="receipt-alias",
                evidence_sha256="d" * 64,
                available_at=_AT,
            )
            resolution.validate(as_of=_AT)
            authority_outcomes[leg.quote_key] = "loss"

            coordinator = ContinuousSessionCoordinator.__new__(
                ContinuousSessionCoordinator
            )
            coordinator.workspace = root
            coordinator.paper_book_path = book_path
            coordinator.initial_bankroll = "100"
            settled, evidence_ids = coordinator._settle(resolutions=(resolution,))

            reopened = PaperBook.load(book_path)
            self.assertEqual(settled, (ticket.ticket_id,))
            self.assertEqual(evidence_ids, ("receipt-alias",))
            self.assertIs(reopened.tickets[ticket.ticket_id].status, TicketStatus.WON)
            self.assertEqual(reopened.balance, Decimal("110"))

    def test_settlement_rejects_post_validation_outcome_mutation_before_book_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book_path = root / "paper_book.json"
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
                placed_at="2026-09-21T08:39:00+00:00",
            )
            book.save(book_path)

            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="provider-result:mutated",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="receipt-mutated",
                evidence_sha256="e" * 64,
                available_at=_AT,
            )
            resolution.validate(as_of=_AT)
            resolution.quote_outcomes[leg.quote_key] = "loss"

            coordinator = ContinuousSessionCoordinator.__new__(
                ContinuousSessionCoordinator
            )
            coordinator.workspace = root
            coordinator.paper_book_path = book_path
            coordinator.initial_bankroll = "100"
            with self.assertRaisesRegex(ValueError, "changed after validation"):
                coordinator._settle(resolutions=(resolution,))

            reopened = PaperBook.load(book_path)
            self.assertIs(reopened.tickets[ticket.ticket_id].status, TicketStatus.OPEN)
            self.assertEqual(reopened.balance, Decimal("90"))

    def test_same_outcomes_cannot_hide_settlement_reference_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory) / "continuous_session.json")
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(_resolution(),),
            )
            with self.assertRaisesRegex(
                ContinuousSessionError,
                "settlement evidence id conflicts",
            ):
                state.validate_settlement_evidence(
                    settlement_evidence=(
                        _resolution(settlement_ref="provider-result:2"),
                    )
                )


if __name__ == "__main__":
    unittest.main()
