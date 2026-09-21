from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.continuous_session import (
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
