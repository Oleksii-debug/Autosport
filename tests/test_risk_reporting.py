import tempfile
import unittest
from decimal import Decimal, getcontext, setcontext
from pathlib import Path
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.risk_reporting import (
    DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
    RISK_OF_RUIN_STATUS_UNKNOWN,
    RISK_REPORT_SCHEMA,
    RISK_REPORT_SCOPE_PAPER_ONLY,
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
        self.assertEqual(report.scope, RISK_REPORT_SCOPE_PAPER_ONLY)
        self.assertEqual(
            report.drawdown_metric_class,
            DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
        )
        self.assertFalse(report.includes_live_execution_exposure)
        self.assertFalse(report.live_execution_headroom_authoritative)
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
        self.assertEqual(report.historical_max_drawdown_amount, Decimal("0"))
        self.assertEqual(report.historical_max_drawdown_fraction, Decimal("0"))
        self.assertIsNone(report.historical_max_drawdown_peak_id)
        self.assertIsNone(report.historical_max_drawdown_trough_id)
        self.assertEqual(report.drawdown_loss_room, Decimal("20.00"))
        self.assertEqual(report.risk_of_ruin_limit, Decimal("0.05"))
        self.assertIsNone(report.risk_of_ruin_upper_bound)
        self.assertEqual(report.risk_of_ruin_status, RISK_OF_RUIN_STATUS_UNKNOWN)

    def test_paper_report_never_claims_live_execution_headroom(self) -> None:
        book = PaperBook("100")
        book.open_ticket(
            (self._leg(99),),
            Decimal("25"),
            placed_at="2026-09-21T07:00:00+00:00",
        )

        report = build_paper_risk_report(book, self._goal())

        self.assertEqual(report.scope, RISK_REPORT_SCOPE_PAPER_ONLY)
        self.assertEqual(
            report.drawdown_metric_class,
            DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
        )
        self.assertEqual(report.committed_stake, Decimal("25"))
        self.assertFalse(report.includes_live_execution_exposure)
        self.assertFalse(report.live_execution_headroom_authoritative)


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
        self.assertEqual(report.historical_max_drawdown_amount, Decimal("10"))
        self.assertEqual(report.historical_max_drawdown_fraction, Decimal("0.1"))
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

    def test_recovery_does_not_erase_historical_max_drawdown_episode(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(20),),
            Decimal("50"),
            placed_at="2026-09-21T08:00:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T08:05:00+00:00",
        )
        winner = book.open_ticket(
            (self._leg(21, odds="8"),),
            Decimal("10"),
            placed_at="2026-09-21T08:10:00+00:00",
        )
        book.settle(
            winner.ticket_id,
            {winner.legs[0].quote_key},
            settled_at="2026-09-21T08:15:00+00:00",
        )

        report = build_paper_risk_report(book, self._goal())

        self.assertEqual(report.current_equity, Decimal("120"))
        self.assertEqual(report.peak_equity, Decimal("120"))
        self.assertEqual(report.current_drawdown_amount, Decimal("0"))
        self.assertEqual(report.historical_max_drawdown_amount, Decimal("50"))
        self.assertEqual(report.historical_max_drawdown_fraction, Decimal("0.5"))
        self.assertEqual(report.max_drawdown_fraction, Decimal("0.20"))
        self.assertNotEqual(
            report.historical_max_drawdown_fraction,
            report.max_drawdown_fraction,
        )
        self.assertEqual(
            report.historical_max_drawdown_peak_id,
            "paper-initial-bankroll",
        )
        self.assertEqual(
            report.historical_max_drawdown_trough_id,
            f"paper-lifecycle:1:settle:{loser.ticket_id}",
        )

    def test_later_larger_episode_uses_new_all_time_high_as_peak_identity(self) -> None:
        book = PaperBook("100")
        first_loser = book.open_ticket(
            (self._leg(30),),
            Decimal("30"),
            placed_at="2026-09-21T09:00:00+00:00",
        )
        book.settle(
            first_loser.ticket_id,
            set(),
            settled_at="2026-09-21T09:05:00+00:00",
        )
        winner = book.open_ticket(
            (self._leg(31, odds="6"),),
            Decimal("10"),
            placed_at="2026-09-21T09:10:00+00:00",
        )
        book.settle(
            winner.ticket_id,
            {winner.legs[0].quote_key},
            settled_at="2026-09-21T09:15:00+00:00",
        )
        second_loser = book.open_ticket(
            (self._leg(32),),
            Decimal("40"),
            placed_at="2026-09-21T09:20:00+00:00",
        )
        book.settle(
            second_loser.ticket_id,
            set(),
            settled_at="2026-09-21T09:25:00+00:00",
        )

        report = build_paper_risk_report(book, self._goal())

        self.assertEqual(report.current_equity, Decimal("80"))
        self.assertEqual(report.peak_equity, Decimal("120"))
        self.assertEqual(report.current_drawdown_amount, Decimal("40"))
        self.assertEqual(report.historical_max_drawdown_amount, Decimal("40"))
        self.assertEqual(
            report.historical_max_drawdown_peak_id,
            f"paper-lifecycle:3:settle:{winner.ticket_id}",
        )
        self.assertEqual(
            report.historical_max_drawdown_trough_id,
            f"paper-lifecycle:5:settle:{second_loser.ticket_id}",
        )

    def test_equal_max_drawdowns_keep_earliest_causal_episode_across_restart(self) -> None:
        book = PaperBook("100")
        first_loser = book.open_ticket(
            (self._leg(40),),
            Decimal("20"),
            placed_at="2026-09-21T10:00:00+00:00",
        )
        book.settle(
            first_loser.ticket_id,
            set(),
            settled_at="2026-09-21T10:05:00+00:00",
        )
        recovery = book.open_ticket(
            (self._leg(41, odds="3"),),
            Decimal("10"),
            placed_at="2026-09-21T10:10:00+00:00",
        )
        book.settle(
            recovery.ticket_id,
            {recovery.legs[0].quote_key},
            settled_at="2026-09-21T10:15:00+00:00",
        )
        second_loser = book.open_ticket(
            (self._leg(42),),
            Decimal("20"),
            placed_at="2026-09-21T10:20:00+00:00",
        )
        book.settle(
            second_loser.ticket_id,
            set(),
            settled_at="2026-09-21T10:25:00+00:00",
        )
        goal = self._goal()
        expected = build_paper_risk_report(book, goal)

        self.assertEqual(expected.historical_max_drawdown_amount, Decimal("20"))
        self.assertEqual(
            expected.historical_max_drawdown_peak_id,
            "paper-initial-bankroll",
        )
        self.assertEqual(
            expected.historical_max_drawdown_trough_id,
            f"paper-lifecycle:1:settle:{first_loser.ticket_id}",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-max-drawdown.json"
            book.save(path)
            reopened = PaperBook.load(path)
            actual = build_paper_risk_report(reopened, goal)

        self.assertEqual(actual, expected)

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

    def test_risk_state_change_during_projection_fails_closed(self) -> None:
        book = PaperBook("100")

        with patch.object(
            PaperRiskPolicy,
            "risk_of_ruin_portfolio_sha256",
            side_effect=("a" * 64, "b" * 64),
        ) as digest:
            with self.assertRaisesRegex(
                ValueError,
                "canonical PAPER risk state changed during reporting",
            ):
                build_paper_risk_report(book, self._goal())

        self.assertEqual(digest.call_count, 2)

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
