import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, getcontext, setcontext
from pathlib import Path
from unittest.mock import patch

import autosport.risk_reporting as risk_reporting

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.risk_reporting import (
    DRAWDOWN_EVIDENCE_SCHEMA,
    DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
    EQUITY_PATH_SCHEMA,
    HISTORY_VIEW_RESTATED_CURRENT,
    RISK_OF_RUIN_STATUS_UNKNOWN,
    RISK_REPORT_SCHEMA,
    RISK_REPORT_SCOPE_PAPER_ONLY,
    build_paper_risk_report,
    build_product_issued_paper_drawdown_evidence,
    build_product_issued_paper_equity_path,
    resolve_durable_product_issued_paper_drawdown_evidence,
    resolve_durable_product_issued_paper_equity_path,
    resolve_durable_verified_settled_minimum_equity,
    verified_settled_minimum_equity,
    verify_durable_product_issued_paper_drawdown_evidence,
    verify_durable_product_issued_paper_equity_path,
    verify_product_issued_paper_drawdown_evidence,
    verify_product_issued_paper_equity_path,
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
        self.assertEqual(
            report.historical_max_drawdown_fraction,
            Decimal("0.1818181818181818181818181818"),
        )
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
            report.historical_max_drawdown_fraction,
            Decimal("0.3333333333333333333333333333"),
        )
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

    def test_product_issued_equity_path_binds_canonical_goal_and_lifecycle(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(50),),
            Decimal("25"),
            placed_at="2026-09-21T11:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T11:05:00+00:00",
        )
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)

        self.assertEqual(path.schema, EQUITY_PATH_SCHEMA)
        self.assertEqual(path.goal_id, goal.goal_id)
        self.assertEqual(path.goal_revision, goal.revision)
        self.assertEqual(path.bankroll_id, goal.bankroll_id)
        self.assertEqual(path.currency, goal.currency)
        self.assertEqual(path.point_count, 3)
        self.assertEqual(path.points[0].point_id, "paper-initial-bankroll")
        self.assertEqual(path.points[0].equity, Decimal("100"))
        self.assertEqual(path.points[1].action, "open")
        self.assertEqual(path.points[1].equity, Decimal("100"))
        self.assertEqual(path.points[2].action, "settle")
        self.assertEqual(path.points[2].equity, Decimal("75"))
        self.assertEqual(path.minimum_equity, Decimal("75"))
        self.assertEqual(path.minimum_equity_point_id, path.points[2].point_id)
        self.assertTrue(path.availability_complete)
        self.assertTrue(path.settled_history_complete)
        self.assertEqual(len(path.path_sha256), 64)

    def test_product_issued_equity_path_restart_reresolves_identically(self) -> None:
        book = PaperBook("100")
        winner = book.open_ticket(
            (self._leg(51, odds="3"),),
            Decimal("10"),
            placed_at="2026-09-21T11:10:00+00:00",
        )
        book.settle(
            winner.ticket_id,
            {winner.legs[0].quote_key},
            settled_at="2026-09-21T11:15:00+00:00",
        )
        loser = book.open_ticket(
            (self._leg(52),),
            Decimal("30"),
            placed_at="2026-09-21T11:20:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T11:25:00+00:00",
        )
        goal = self._goal()
        expected = build_product_issued_paper_equity_path(book, goal)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-equity-path.json"
            book.save(path)
            loaded = PaperBook.load(path)
            actual = build_product_issued_paper_equity_path(loaded, goal)

        self.assertEqual(actual, expected)
        self.assertEqual(actual.path_sha256, expected.path_sha256)

    def test_open_position_is_visible_but_not_falsely_called_settled_history(self) -> None:
        book = PaperBook("100")
        book.open_ticket(
            (self._leg(53),),
            Decimal("40"),
            placed_at="2026-09-21T11:30:00+00:00",
        )

        path = build_product_issued_paper_equity_path(book, self._goal())
        report = build_paper_risk_report(book, self._goal())

        self.assertEqual(path.current_equity, Decimal("100"))
        self.assertEqual(path.minimum_equity, Decimal("100"))
        self.assertFalse(path.settled_history_complete)
        self.assertFalse(report.settled_history_complete)
        self.assertEqual(report.current_drawdown_amount, Decimal("0"))
        self.assertEqual(report.committed_stake, Decimal("40"))

    def test_missing_settlement_availability_keeps_current_path_but_marks_chronology_incomplete(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(54),),
            Decimal("10"),
            placed_at="2026-09-21T11:40:00+00:00",
        )
        book.settle(ticket.ticket_id, set())

        path = build_product_issued_paper_equity_path(book, self._goal())

        self.assertEqual(path.minimum_equity, Decimal("90"))
        self.assertFalse(path.availability_complete)
        self.assertTrue(path.settled_history_complete)

    def test_report_references_exact_re_resolved_equity_path_identity(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(55),),
            Decimal("15"),
            placed_at="2026-09-21T11:50:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T11:55:00+00:00",
        )
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)
        report = build_paper_risk_report(book, goal)

        self.assertEqual(report.equity_path_sha256, path.path_sha256)
        self.assertEqual(report.equity_path_point_count, path.point_count)
        self.assertEqual(
            report.equity_path_availability_complete,
            path.availability_complete,
        )
        self.assertEqual(report.settled_history_complete, path.settled_history_complete)

    def test_equity_path_identity_changes_when_causal_history_changes(self) -> None:
        baseline = PaperBook("100")
        baseline_ticket = baseline.open_ticket(
            (self._leg(56),),
            Decimal("10"),
            placed_at="2026-09-21T12:00:00+00:00",
        )
        baseline.settle(
            baseline_ticket.ticket_id,
            set(),
            settled_at="2026-09-21T12:05:00+00:00",
        )

        alternative = PaperBook("100")
        winning_ticket = alternative.open_ticket(
            (self._leg(57),),
            Decimal("10"),
            placed_at="2026-09-21T12:00:00+00:00",
        )
        alternative.settle(
            winning_ticket.ticket_id,
            {winning_ticket.legs[0].quote_key},
            settled_at="2026-09-21T12:05:00+00:00",
        )
        recovery_ticket = alternative.open_ticket(
            (self._leg(58),),
            Decimal("20"),
            placed_at="2026-09-21T12:10:00+00:00",
        )
        alternative.settle(
            recovery_ticket.ticket_id,
            set(),
            settled_at="2026-09-21T12:15:00+00:00",
        )
        goal = self._goal()

        first = build_product_issued_paper_equity_path(baseline, goal)
        second = build_product_issued_paper_equity_path(alternative, goal)

        self.assertNotEqual(first.path_sha256, second.path_sha256)
        self.assertNotEqual(first.points, second.points)

    def test_drawdown_evidence_is_bound_to_equity_path_and_re_resolves(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(59),),
            Decimal("30"),
            placed_at="2026-09-21T12:20:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T12:25:00+00:00",
        )
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)
        evidence = build_product_issued_paper_drawdown_evidence(book, goal)

        self.assertEqual(evidence.schema, DRAWDOWN_EVIDENCE_SCHEMA)
        self.assertEqual(evidence.equity_path_sha256, path.path_sha256)
        self.assertEqual(evidence.max_drawdown_amount, Decimal("30"))
        self.assertEqual(evidence.minimum_equity, Decimal("70"))
        self.assertEqual(
            verify_product_issued_paper_drawdown_evidence(book, goal, evidence),
            evidence,
        )
        self.assertEqual(
            verify_product_issued_paper_equity_path(book, goal, path),
            path,
        )

    def test_caller_replaced_drawdown_scalar_cannot_mint_product_authority(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(60),),
            Decimal("20"),
            placed_at="2026-09-21T12:30:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T12:35:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_drawdown_evidence(book, goal)
        forged = replace(
            evidence,
            max_drawdown_amount=Decimal("0"),
            max_drawdown_fraction=Decimal("0"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "drawdown evidence does not match canonical product state",
        ):
            verify_product_issued_paper_drawdown_evidence(book, goal, forged)

        self.assertEqual(evidence.max_drawdown_amount, Decimal("20"))

    def test_caller_replaced_minimum_equity_cannot_mint_equity_path_authority(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(61),),
            Decimal("25"),
            placed_at="2026-09-21T12:40:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T12:45:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_equity_path(book, goal)
        forged = replace(
            evidence,
            minimum_equity=Decimal("100"),
            minimum_equity_point_id="paper-initial-bankroll",
        )

        with self.assertRaisesRegex(
            ValueError,
            "equity-path evidence does not match canonical product state",
        ):
            verify_product_issued_paper_equity_path(book, goal, forged)

        self.assertEqual(evidence.minimum_equity, Decimal("75"))

    def test_drawdown_evidence_restart_identity_is_stable(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(62),),
            Decimal("10"),
            placed_at="2026-09-21T12:50:00+00:00",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T12:55:00+00:00",
        )
        goal = self._goal()
        expected = build_product_issued_paper_drawdown_evidence(book, goal)

        with tempfile.TemporaryDirectory() as tmp:
            paper_path = Path(tmp) / "paper-drawdown-evidence.json"
            book.save(paper_path)
            loaded = PaperBook.load(paper_path)
            actual = build_product_issued_paper_drawdown_evidence(loaded, goal)

        self.assertEqual(actual, expected)
        self.assertEqual(actual.evidence_sha256, expected.evidence_sha256)

    def test_report_binds_drawdown_evidence_digest(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(63),),
            Decimal("12"),
            placed_at="2026-09-21T13:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T13:05:00+00:00",
        )
        goal = self._goal()

        evidence = build_product_issued_paper_drawdown_evidence(book, goal)
        report = build_paper_risk_report(book, goal)

        self.assertEqual(report.drawdown_evidence_sha256, evidence.evidence_sha256)
        self.assertEqual(report.equity_path_sha256, evidence.equity_path_sha256)

    def test_durable_resolver_reissues_same_equity_and_drawdown_evidence(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(64),),
            Decimal("18"),
            placed_at="2026-09-21T13:10:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T13:15:00+00:00",
        )
        goal = self._goal()
        expected_path = build_product_issued_paper_equity_path(book, goal)
        expected_drawdown = build_product_issued_paper_drawdown_evidence(book, goal)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_path = workspace / "paper-book.json"
            book.save(paper_path)
            EconomicGoalStore(workspace).initialize_owner(goal)

            actual_path = resolve_durable_product_issued_paper_equity_path(
                paper_book_path=str(paper_path),
                workspace=str(workspace),
            )
            actual_drawdown = resolve_durable_product_issued_paper_drawdown_evidence(
                paper_book_path=str(paper_path),
                workspace=str(workspace),
            )

        self.assertEqual(actual_path, expected_path)
        self.assertEqual(actual_drawdown, expected_drawdown)

    def test_durable_resolver_rejects_noncanonical_path_argument_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "paper_book_path"):
            resolve_durable_product_issued_paper_equity_path(
                paper_book_path=Path("paper-book.json"),  # type: ignore[arg-type]
                workspace="workspace",
            )
        with self.assertRaisesRegex(TypeError, "workspace"):
            resolve_durable_product_issued_paper_equity_path(
                paper_book_path="paper-book.json",
                workspace=Path("workspace"),  # type: ignore[arg-type]
            )

    def test_verified_minimum_equity_requires_complete_settled_causal_path(self) -> None:
        settled = PaperBook("100")
        loser = settled.open_ticket(
            (self._leg(65),),
            Decimal("35"),
            placed_at="2026-09-21T13:20:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        settled.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T13:25:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_equity_path(settled, goal)

        minimum, point_id = verified_settled_minimum_equity(
            settled,
            goal,
            evidence,
        )

        self.assertEqual(minimum, Decimal("65"))
        self.assertEqual(point_id, evidence.minimum_equity_point_id)

    def test_verified_minimum_equity_rejects_open_exposure(self) -> None:
        book = PaperBook("100")
        book.open_ticket(
            (self._leg(66),),
            Decimal("10"),
            placed_at="2026-09-21T13:30:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_equity_path(book, goal)

        with self.assertRaisesRegex(
            ValueError,
            "all economically material PAPER tickets settled",
        ):
            verified_settled_minimum_equity(book, goal, evidence)

    def test_verified_minimum_equity_rejects_missing_settlement_availability(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(67),),
            Decimal("10"),
            placed_at="2026-09-21T13:40:00+00:00",
        )
        book.settle(ticket.ticket_id, set())
        goal = self._goal()
        evidence = build_product_issued_paper_equity_path(book, goal)

        with self.assertRaisesRegex(
            ValueError,
            "complete durable availability chronology",
        ):
            verified_settled_minimum_equity(book, goal, evidence)

    def test_equity_path_is_explicitly_restated_current_not_as_known_history(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(68),),
            Decimal("10"),
            placed_at="2026-09-21T14:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T14:05:00+00:00",
        )
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)
        drawdown = build_product_issued_paper_drawdown_evidence(book, goal)
        report = build_paper_risk_report(book, goal)

        self.assertEqual(path.history_view, HISTORY_VIEW_RESTATED_CURRENT)
        self.assertFalse(path.historical_as_known_supported)
        self.assertEqual(drawdown.history_view, HISTORY_VIEW_RESTATED_CURRENT)
        self.assertFalse(drawdown.historical_as_known_supported)
        self.assertEqual(report.history_view, HISTORY_VIEW_RESTATED_CURRENT)
        self.assertFalse(report.historical_as_known_supported)

    def test_missing_settlement_timestamp_never_upgrades_to_as_known_history(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(69),),
            Decimal("10"),
            placed_at="2026-09-21T14:10:00+00:00",
        )
        book.settle(ticket.ticket_id, set())
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)

        self.assertFalse(path.availability_complete)
        self.assertEqual(path.history_view, HISTORY_VIEW_RESTATED_CURRENT)
        self.assertFalse(path.historical_as_known_supported)

    def test_same_numeric_book_state_has_distinct_path_identity_across_money_scope(self) -> None:
        book = PaperBook("100")
        usd_goal = self._goal(bankroll_id="bankroll-usd", currency="USD")
        eur_goal = self._goal(bankroll_id="bankroll-eur", currency="EUR")

        usd = build_product_issued_paper_equity_path(book, usd_goal)
        eur = build_product_issued_paper_equity_path(book, eur_goal)

        self.assertEqual(usd.current_equity, eur.current_equity)
        self.assertNotEqual(usd.path_sha256, eur.path_sha256)
        self.assertNotEqual(
            (usd.bankroll_id, usd.currency, usd.goal_contract_sha256),
            (eur.bankroll_id, eur.currency, eur.goal_contract_sha256),
        )

    def test_same_goal_id_revision_with_changed_semantics_changes_path_identity(self) -> None:
        book = PaperBook("100")
        baseline = self._goal(max_drawdown_fraction=Decimal("0.20"))
        tightened = self._goal(max_drawdown_fraction=Decimal("0.10"))

        first = build_product_issued_paper_equity_path(book, baseline)
        second = build_product_issued_paper_equity_path(book, tightened)

        self.assertEqual(first.goal_id, second.goal_id)
        self.assertEqual(first.goal_revision, second.goal_revision)
        self.assertNotEqual(first.goal_contract_sha256, second.goal_contract_sha256)
        self.assertNotEqual(first.path_sha256, second.path_sha256)

    def test_durable_verifier_rejects_copied_forged_equity_evidence(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(70),),
            Decimal("20"),
            placed_at="2026-09-21T14:20:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T14:25:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_equity_path(book, goal)
        forged = replace(evidence, minimum_equity=Decimal("100"))

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_path = workspace / "paper-book.json"
            book.save(paper_path)
            EconomicGoalStore(workspace).initialize_owner(goal)

            with self.assertRaisesRegex(
                ValueError,
                "equity-path evidence does not match durable product state",
            ):
                verify_durable_product_issued_paper_equity_path(
                    paper_book_path=str(paper_path),
                    workspace=str(workspace),
                    evidence=forged,
                )

    def test_durable_verifier_rejects_copied_forged_drawdown_evidence(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(71),),
            Decimal("20"),
            placed_at="2026-09-21T14:30:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T14:35:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_drawdown_evidence(book, goal)
        forged = replace(evidence, max_drawdown_amount=Decimal("0"))

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_path = workspace / "paper-book.json"
            book.save(paper_path)
            EconomicGoalStore(workspace).initialize_owner(goal)

            with self.assertRaisesRegex(
                ValueError,
                "drawdown evidence does not match durable product state",
            ):
                verify_durable_product_issued_paper_drawdown_evidence(
                    paper_book_path=str(paper_path),
                    workspace=str(workspace),
                    evidence=forged,
                )

    def test_durable_minimum_equity_returns_path_identity_for_downstream_binding(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(72),),
            Decimal("40"),
            placed_at="2026-09-21T14:40:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T14:45:00+00:00",
        )
        goal = self._goal()
        expected = build_product_issued_paper_equity_path(book, goal)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_path = workspace / "paper-book.json"
            book.save(paper_path)
            EconomicGoalStore(workspace).initialize_owner(goal)

            minimum, point_id, path_sha256 = (
                resolve_durable_verified_settled_minimum_equity(
                    paper_book_path=str(paper_path),
                    workspace=str(workspace),
                )
            )

        self.assertEqual(minimum, Decimal("60"))
        self.assertEqual(point_id, expected.minimum_equity_point_id)
        self.assertEqual(path_sha256, expected.path_sha256)

    def test_durable_minimum_equity_rejects_open_exposure(self) -> None:
        book = PaperBook("100")
        book.open_ticket(
            (self._leg(73),),
            Decimal("10"),
            placed_at="2026-09-21T14:50:00+00:00",
        )
        goal = self._goal()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_path = workspace / "paper-book.json"
            book.save(paper_path)
            EconomicGoalStore(workspace).initialize_owner(goal)

            with self.assertRaisesRegex(
                ValueError,
                "all economically material PAPER tickets settled",
            ):
                resolve_durable_verified_settled_minimum_equity(
                    paper_book_path=str(paper_path),
                    workspace=str(workspace),
                )

    def test_evidence_subclasses_cannot_cross_verification_boundary(self) -> None:
        book = PaperBook("100")
        goal = self._goal()
        path = build_product_issued_paper_equity_path(book, goal)
        drawdown = build_product_issued_paper_drawdown_evidence(book, goal)

        PathSubclass = type("PathSubclass", (type(path),), {})
        DrawdownSubclass = type("DrawdownSubclass", (type(drawdown),), {})
        path_subclass = PathSubclass(**{
            field: getattr(path, field)
            for field in path.__dataclass_fields__
        })
        drawdown_subclass = DrawdownSubclass(**{
            field: getattr(drawdown, field)
            for field in drawdown.__dataclass_fields__
        })

        with self.assertRaisesRegex(TypeError, "exact ProductIssuedPaperEquityPath"):
            verify_product_issued_paper_equity_path(book, goal, path_subclass)
        with self.assertRaisesRegex(
            TypeError,
            "exact ProductIssuedPaperDrawdownEvidence",
        ):
            verify_product_issued_paper_drawdown_evidence(
                book,
                goal,
                drawdown_subclass,
            )

    def test_money_scope_incompleteness_is_visible_on_legacy_unscoped_tickets(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(74),),
            Decimal("10"),
            placed_at="2026-09-21T15:00:00+00:00",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T15:05:00+00:00",
        )
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)
        drawdown = build_product_issued_paper_drawdown_evidence(book, goal)
        report = build_paper_risk_report(book, goal)

        self.assertFalse(path.money_scope_complete)
        self.assertFalse(drawdown.money_scope_complete)
        self.assertFalse(report.money_scope_complete)

    def test_verified_minimum_equity_rejects_wrong_bankroll_or_currency_scope(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(75),),
            Decimal("10"),
            placed_at="2026-09-21T15:10:00+00:00",
            bankroll_id="different-bankroll",
            currency="EUR",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T15:15:00+00:00",
        )
        goal = self._goal()
        evidence = build_product_issued_paper_equity_path(book, goal)

        self.assertFalse(evidence.money_scope_complete)
        with self.assertRaisesRegex(
            ValueError,
            "exact bankroll and currency provenance",
        ):
            verified_settled_minimum_equity(book, goal, evidence)

    def test_durable_minimum_equity_rejects_wrong_money_scope(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            (self._leg(76),),
            Decimal("10"),
            placed_at="2026-09-21T15:20:00+00:00",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )
        book.settle(
            ticket.ticket_id,
            set(),
            settled_at="2026-09-21T15:25:00+00:00",
        )
        goal = self._goal()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            paper_path = workspace / "paper-book.json"
            book.save(paper_path)
            EconomicGoalStore(workspace).initialize_owner(goal)

            with self.assertRaisesRegex(
                ValueError,
                "exact bankroll and currency provenance",
            ):
                resolve_durable_verified_settled_minimum_equity(
                    paper_book_path=str(paper_path),
                    workspace=str(workspace),
                )

    def test_deleting_old_loss_from_canonical_history_fails_closed(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(77),),
            Decimal("25"),
            placed_at="2026-09-21T15:30:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T15:35:00+00:00",
        )
        self.assertEqual(
            build_product_issued_paper_equity_path(book, self._goal()).minimum_equity,
            Decimal("75"),
        )

        book._lifecycle.pop()

        with self.assertRaises(ValueError):
            build_product_issued_paper_equity_path(book, self._goal())

    def test_same_opening_and_closing_equity_different_causal_paths_do_not_alias(self) -> None:
        loss_then_recovery = PaperBook("100")
        loser = loss_then_recovery.open_ticket(
            (self._leg(78),),
            Decimal("50"),
            placed_at="2026-09-21T15:40:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        loss_then_recovery.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T15:45:00+00:00",
        )
        recovery = loss_then_recovery.open_ticket(
            (self._leg(79, odds="6"),),
            Decimal("10"),
            placed_at="2026-09-21T15:50:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        loss_then_recovery.settle(
            recovery.ticket_id,
            {recovery.legs[0].quote_key},
            settled_at="2026-09-21T15:55:00+00:00",
        )

        gain_then_loss = PaperBook("100")
        winner = gain_then_loss.open_ticket(
            (self._leg(80, odds="6"),),
            Decimal("10"),
            placed_at="2026-09-21T15:40:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        gain_then_loss.settle(
            winner.ticket_id,
            {winner.legs[0].quote_key},
            settled_at="2026-09-21T15:45:00+00:00",
        )
        second_loser = gain_then_loss.open_ticket(
            (self._leg(81),),
            Decimal("50"),
            placed_at="2026-09-21T15:50:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        gain_then_loss.settle(
            second_loser.ticket_id,
            set(),
            settled_at="2026-09-21T15:55:00+00:00",
        )

        goal = self._goal()
        first_path = build_product_issued_paper_equity_path(loss_then_recovery, goal)
        second_path = build_product_issued_paper_equity_path(gain_then_loss, goal)
        first_drawdown = build_product_issued_paper_drawdown_evidence(
            loss_then_recovery,
            goal,
        )
        second_drawdown = build_product_issued_paper_drawdown_evidence(
            gain_then_loss,
            goal,
        )

        self.assertEqual(first_path.current_equity, Decimal("100"))
        self.assertEqual(second_path.current_equity, Decimal("100"))
        self.assertNotEqual(first_path.path_sha256, second_path.path_sha256)
        self.assertEqual(first_drawdown.max_drawdown_amount, Decimal("50"))
        self.assertEqual(second_drawdown.max_drawdown_amount, Decimal("50"))
        self.assertNotEqual(
            first_drawdown.max_drawdown_peak_id,
            second_drawdown.max_drawdown_peak_id,
        )

    def test_recovery_keeps_historical_loss_bound_to_drawdown_evidence_digest(self) -> None:
        book = PaperBook("100")
        loser = book.open_ticket(
            (self._leg(82),),
            Decimal("40"),
            placed_at="2026-09-21T16:00:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        book.settle(
            loser.ticket_id,
            set(),
            settled_at="2026-09-21T16:05:00+00:00",
        )
        winner = book.open_ticket(
            (self._leg(83, odds="5"),),
            Decimal("10"),
            placed_at="2026-09-21T16:10:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
        )
        book.settle(
            winner.ticket_id,
            {winner.legs[0].quote_key},
            settled_at="2026-09-21T16:15:00+00:00",
        )
        goal = self._goal()

        path = build_product_issued_paper_equity_path(book, goal)
        evidence = build_product_issued_paper_drawdown_evidence(book, goal)

        self.assertEqual(path.current_equity, Decimal("100"))
        self.assertEqual(evidence.max_drawdown_amount, Decimal("40"))
        self.assertEqual(
            evidence.max_drawdown_peak_id,
            "paper-initial-bankroll",
        )
        self.assertEqual(evidence.equity_path_sha256, path.path_sha256)
        self.assertEqual(len(evidence.evidence_sha256), 64)

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

        original_drawdown = risk_reporting._historical_max_drawdown
        mutated = False

        def mutate_state(current_book):
            nonlocal mutated
            if not mutated:
                mutated = True
                current_book.open_ticket(
                    [TicketLeg("event-race", "market-race", "selection-race", Decimal("2"))],
                    Decimal("1"),
                )
            return original_drawdown(current_book)

        with patch.object(
            risk_reporting,
            "_historical_max_drawdown",
            side_effect=mutate_state,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "canonical PAPER risk state changed during reporting",
            ):
                build_paper_risk_report(book, self._goal())

        self.assertTrue(mutated)

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
