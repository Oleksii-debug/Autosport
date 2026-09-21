import tempfile
import unittest
from decimal import Decimal, getcontext, setcontext
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.risk_reporting import (
    RISK_OF_RUIN_STATUS_UNKNOWN,
    RISK_REPORT_SCHEMA,
    build_paper_risk_report,
)


class PaperRiskReportingTests(unittest.TestCase):
    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "risk-report-goal",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("0.20"),
            "max_turnover_fraction": Decimal("10"),
            "max_risk_of_ruin": Decimal("0.05"),
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @staticmethod
    def _leg(index: int, odds: str = "2") -> TicketLeg:
        return TicketLeg(
            event_id=f"event-{index}",
            market_id=f"market-{index}",
            selection_id=f"selection-{index}",
            locked_odds=Decimal(odds),
        )

    def test_pristine_report_uses_canonical_risk_replay_and_keeps_ruin_unknown(self) -> None:
        book = PaperBook("100")
        goal = self._goal()

        report = build_paper_risk_report(book, goal)

        self.assertEqual(report.schema, RISK_REPORT_SCHEMA)
        self.assertEqual(
            report.portfolio_risk_state_sha256,
            PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book),
        )
        self.assertEqual(report.initial_bankroll, Decimal("100"))
        self.assertEqual(report.current_equity, Decimal("100"))
        self.assertEqual(report.peak_equity, Decimal("100"))
        self.assertEqual(report.committed_stake, Decimal("0"))
        self.assertEqual(report.realized_gross_loss, Decimal("0"))
        self.assertEqual(report.turnover, Decimal("0"))
        self.assertEqual(report.current_drawdown_amount, Decimal("0"))
        self.assertEqual(report.drawdown_loss_room, Decimal("20.00"))
        self.assertEqual(report.risk_of_ruin_limit, Decimal("0.05"))
        self.assertIsNone(report.risk_of_ruin_upper_bound)
        self.assertEqual(report.risk_of_ruin_status, RISK_OF_RUIN_STATUS_UNKNOWN)

    def test_open_stake_reduces_drawdown_room_without_fabricating_drawdown(self) -> None:
        book = PaperBook("100")
        book.open_ticket(
            (self._leg(1),),
            Decimal("10"),
            placed_at="2026-09-21T08:00:00+00:00",
        )

        report = build_paper_risk_report(book, self._goal())

        self.assertEqual(report.current_equity, Decimal("100"))
        self.assertEqual(report.peak_equity, Decimal("100"))
        self.assertEqual(report.current_drawdown_amount, Decimal("0"))
        self.assertEqual(report.committed_stake, Decimal("10"))
        self.assertEqual(report.drawdown_loss_room, Decimal("10.00"))

    def test_settled_loss_reports_exact_drawdown_and_same_enforcement_headroom(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(2),),
            Decimal("10"),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T08:05:00+00:00",
        )
        goal = self._goal()

        report = build_paper_risk_report(book, goal)
        rooms = PaperRiskPolicy._goal_history_rooms(book, goal)

        self.assertIsNotNone(rooms)
        assert rooms is not None
        self.assertEqual(report.current_equity, Decimal("90"))
        self.assertEqual(report.peak_equity, Decimal("100"))
        self.assertEqual(report.current_drawdown_amount, Decimal("10"))
        self.assertEqual(report.realized_gross_loss, Decimal("10"))
        self.assertEqual(report.turnover, Decimal("10"))
        self.assertEqual(report.drawdown_loss_room, rooms[2])
        self.assertEqual(report.drawdown_loss_room, Decimal("10.00"))

    def test_peak_then_loss_preserves_peak_and_reports_remaining_drawdown_room(self) -> None:
        book = PaperBook("100")
        winner = book.open_ticket(
            (self._leg(3),),
            Decimal("10"),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        book.settle(
            winner.ticket_id,
            {winner.legs[0].quote_key},
            settled_at="2026-09-21T08:05:00+00:00",
        )
        loser = book.open_ticket(
            (self._leg(4),),
            Decimal("20"),
            placed_at="2026-09-21T08:10:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T08:15:00+00:00",
        )

        report = build_paper_risk_report(book, self._goal())

        self.assertEqual(report.peak_equity, Decimal("110"))
        self.assertEqual(report.current_equity, Decimal("90"))
        self.assertEqual(report.current_drawdown_amount, Decimal("20"))
        self.assertEqual(report.drawdown_loss_room, Decimal("2.00"))

    def test_over_limit_drawdown_stays_negative_and_reporting_is_read_only(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(7),),
            Decimal("30"),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T08:05:00+00:00",
        )
        before_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)

        report = build_paper_risk_report(book, self._goal())

        after_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        self.assertEqual(report.current_drawdown_amount, Decimal("30"))
        self.assertEqual(report.drawdown_loss_room, Decimal("-10.00"))
        self.assertEqual(report.portfolio_risk_state_sha256, before_sha256)
        self.assertEqual(after_sha256, before_sha256)

    def test_restart_preserves_exact_report_identity_and_values(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(5),),
            Decimal("10"),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T08:05:00+00:00",
        )
        goal = self._goal()
        expected = build_paper_risk_report(book, goal)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book.save(path)
            loaded = PaperBook.load(path)
            actual = build_paper_risk_report(loaded, goal)

        self.assertEqual(actual, expected)

    def test_corrupted_paper_state_fails_closed_instead_of_reporting_metrics(self) -> None:
        book = PaperBook("100")
        book.balance = Decimal("999")

        with self.assertRaises(ValueError):
            build_paper_risk_report(book, self._goal())

    def test_report_is_independent_of_caller_decimal_context(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(6),),
            Decimal("10"),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T08:05:00+00:00",
        )
        goal = self._goal()
        expected = build_paper_risk_report(book, goal)
        original = getcontext().copy()
        try:
            getcontext().prec = 3
            actual = build_paper_risk_report(book, goal)
        finally:
            setcontext(original)

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
