import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.risk_reporting import build_paper_risk_report


class PaperRiskReportGoalProvenanceFalsifierTests(unittest.TestCase):
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

    def test_report_binds_exact_bankroll_currency_and_goal_contract_digest(self) -> None:
        book = PaperBook("100")
        goal = self._goal()

        report = build_paper_risk_report(book, goal)
        provenance = provenance_for(goal)

        self.assertEqual(report.bankroll_id, goal.bankroll_id)
        self.assertEqual(report.currency, goal.currency)
        self.assertEqual(report.goal_contract_sha256, provenance.contract_sha256)

    def test_same_numeric_paper_state_cannot_alias_different_money_scope(self) -> None:
        book = PaperBook("100")
        usd_goal = self._goal(bankroll_id="bankroll-usd", currency="USD")
        eur_goal = self._goal(bankroll_id="bankroll-eur", currency="EUR")

        usd_report = build_paper_risk_report(book, usd_goal)
        eur_report = build_paper_risk_report(book, eur_goal)

        self.assertEqual(
            usd_report.portfolio_risk_state_sha256,
            eur_report.portfolio_risk_state_sha256,
        )
        self.assertNotEqual(
            (
                usd_report.bankroll_id,
                usd_report.currency,
                usd_report.goal_contract_sha256,
            ),
            (
                eur_report.bankroll_id,
                eur_report.currency,
                eur_report.goal_contract_sha256,
            ),
        )
        self.assertEqual(
            usd_report.goal_contract_sha256,
            provenance_for(usd_goal).contract_sha256,
        )
        self.assertEqual(
            eur_report.goal_contract_sha256,
            provenance_for(eur_goal).contract_sha256,
        )

    def test_same_human_goal_identity_cannot_alias_changed_goal_semantics(self) -> None:
        book = PaperBook("100")
        baseline = self._goal(max_drawdown_fraction=Decimal("0.20"))
        tightened = self._goal(max_drawdown_fraction=Decimal("0.10"))

        baseline_report = build_paper_risk_report(book, baseline)
        tightened_report = build_paper_risk_report(book, tightened)

        self.assertEqual(baseline_report.goal_id, tightened_report.goal_id)
        self.assertEqual(baseline_report.goal_revision, tightened_report.goal_revision)
        self.assertEqual(
            baseline_report.portfolio_risk_state_sha256,
            tightened_report.portfolio_risk_state_sha256,
        )
        self.assertNotEqual(
            baseline_report.drawdown_loss_room,
            tightened_report.drawdown_loss_room,
        )
        self.assertNotEqual(
            baseline_report.goal_contract_sha256,
            tightened_report.goal_contract_sha256,
        )
        self.assertEqual(
            baseline_report.goal_contract_sha256,
            provenance_for(baseline).contract_sha256,
        )
        self.assertEqual(
            tightened_report.goal_contract_sha256,
            provenance_for(tightened).contract_sha256,
        )

    def test_goal_mutation_during_projection_fails_closed_instead_of_mixing_revisions(self) -> None:
        book = PaperBook("100")
        goal = self._goal(currency="USD", max_drawdown_fraction=Decimal("0.20"))
        original_metrics = PaperRiskPolicy._historical_risk_metrics
        mutated = False

        def mutate_goal(current_book: PaperBook):
            nonlocal mutated
            if not mutated:
                mutated = True
                object.__setattr__(goal, "currency", "EUR")
                object.__setattr__(goal, "max_drawdown_fraction", Decimal("0.10"))
            return original_metrics(current_book)

        with patch.object(
            PaperRiskPolicy,
            "_historical_risk_metrics",
            side_effect=mutate_goal,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "canonical economic goal changed during reporting",
            ):
                build_paper_risk_report(book, goal)

        self.assertTrue(mutated)


if __name__ == "__main__":
    unittest.main()
