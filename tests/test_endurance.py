import json
import tempfile
import unittest
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from unittest.mock import patch

from autosport.domain import TicketStatus
from autosport.endurance import EnduranceConfig, run_endurance
from autosport.providers import CanonicalNormalizer, ProviderQuote


class EnduranceTests(unittest.TestCase):
    def test_provider_normalization_uses_observed_receive_time_as_ingest_time(self):
        quote = ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id="a",
            decimal_odds=Decimal("1.80"),
            observed_ts="2026-01-01T00:00:01+00:00",
            source_ts="2026-01-01T00:00:00+00:00",
            sequence=1,
        )
        event = CanonicalNormalizer().normalize("fixture", quote)
        self.assertEqual(event.ingest_ts, quote.observed_ts)
        self.assertEqual(event.source_ts, quote.source_ts)

    def test_v1_endurance_gate_covers_thousands_of_active_selections(self):
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "endurance.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        marker = "--quote-keys "
        self.assertIn(marker, workflow)
        quote_keys = int(workflow.split(marker, 1)[1].split()[0])
        self.assertGreaterEqual(quote_keys, 2_000)
        self.assertIn("os: [ubuntu-latest, windows-latest]", workflow)

    def test_small_endurance_run_proves_idempotency_restart_and_cross_workspace_determinism(self):
        config = EnduranceConfig(
            event_count=1_200,
            quote_keys=40,
            batch_size=100,
            restart_cycles=2,
            paper_tickets=10,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_a_path = root / "report-a.json"
            report_b_path = root / "report-b.json"
            first = run_endurance(root / "run-a", config, output_path=report_a_path)
            second = run_endurance(root / "run-b", config, output_path=report_b_path)

            self.assertEqual(first.status, "PASS", first.failures)
            self.assertEqual(second.status, "PASS", second.failures)
            self.assertEqual(first.history_events, 1_200)
            self.assertEqual(first.current_quotes, 40)
            self.assertEqual(first.accepted_first_pass, 1_200)
            self.assertEqual(first.accepted_duplicate_pass, 0)
            self.assertEqual(first.replay_event_count, 1_200)
            self.assertTrue(first.independent_reingest_hash_match)
            self.assertEqual(first.replay_dataset_hash, second.replay_dataset_hash)
            self.assertEqual(first.stable_invariant_fingerprint, second.stable_invariant_fingerprint)
            self.assertEqual(first.restart_hashes, (first.replay_dataset_hash, first.replay_dataset_hash))
            self.assertEqual(first.restart_projection_counts, (40, 40))
            self.assertEqual(first.paper_tickets_settled_first_pass, 10)
            self.assertEqual(first.paper_tickets_settled_second_pass, 0)
            self.assertEqual(first.paper_tickets_won, 10)
            self.assertEqual(first.paper_payout_total, "16.41")
            self.assertEqual(first.paper_expected_balance, "100006.41")
            self.assertEqual(first.paper_balance_after_restart, first.paper_expected_balance)
            self.assertTrue(first.paper_economics_verified)
            self.assertTrue(first.corrupt_health_rejected)
            self.assertTrue(first.corrupt_paper_book_rejected)
            self.assertFalse(first.real_money_execution)
            self.assertGreater(first.accepted_events_per_second, 0)
            self.assertGreater(first.peak_traced_memory_bytes, 0)

            stored = json.loads(report_a_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "PASS")
            self.assertEqual(stored["stable_invariant_fingerprint"], first.stable_invariant_fingerprint)
            self.assertEqual(stored["paper_tickets_won"], 10)
            self.assertEqual(stored["paper_expected_balance"], "100006.41")
            self.assertTrue(stored["paper_economics_verified"])
            self.assertFalse(stored["real_money_execution"])

    def test_endurance_fails_if_win_outcomes_settle_as_losses(self):
        config = EnduranceConfig(
            event_count=20,
            quote_keys=10,
            batch_size=10,
            restart_cycles=1,
            paper_tickets=5,
        )

        def settle_as_losses(_engine, book):
            settled = []
            for ticket in list(book.tickets.values()):
                if ticket.status is TicketStatus.OPEN:
                    book.settle(ticket.ticket_id, set())
                    settled.append(ticket.ticket_id)
            return settled

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "autosport.endurance.SettlementEngine.settle_ready",
                new=settle_as_losses,
            ):
                report = run_endurance(Path(tmp), config)

        self.assertEqual(report.status, "FAIL")
        self.assertEqual(report.paper_tickets_settled_first_pass, 5)
        self.assertEqual(report.paper_tickets_settled_second_pass, 0)
        self.assertEqual(report.paper_tickets_won, 0)
        self.assertFalse(report.paper_economics_verified)
        self.assertIn("PaperBook endurance tickets were not all WON", report.failures)
        self.assertIn("PaperBook winning payouts do not match locked odds", report.failures)

    def test_endurance_paper_economic_oracle_is_independent_of_caller_decimal_context(self):
        config = EnduranceConfig(
            event_count=20,
            quote_keys=10,
            batch_size=10,
            restart_cycles=1,
            paper_tickets=5,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = run_endurance(root / "baseline", config)
            with localcontext() as caller_context:
                caller_context.prec = 6
                caller_context.rounding = ROUND_DOWN
                constrained = run_endurance(root / "constrained", config)

        self.assertEqual(baseline.status, "PASS", baseline.failures)
        self.assertEqual(constrained.status, "PASS", constrained.failures)
        self.assertEqual(constrained.paper_payout_total, baseline.paper_payout_total)
        self.assertEqual(constrained.paper_expected_balance, baseline.paper_expected_balance)
        self.assertEqual(constrained.paper_balance_after_restart, baseline.paper_balance_after_restart)
        self.assertEqual(
            constrained.stable_invariant_fingerprint,
            baseline.stable_invariant_fingerprint,
        )
        self.assertTrue(constrained.paper_economics_verified)

    def test_config_and_workspace_are_bounded_fail_closed(self):
        with self.assertRaises(ValueError):
            EnduranceConfig(event_count=0)
        with self.assertRaises(ValueError):
            EnduranceConfig(event_count=10, quote_keys=11)
        with self.assertRaises(ValueError):
            EnduranceConfig(event_count=10, quote_keys=10, batch_size=5_001)
        with self.assertRaises(ValueError):
            EnduranceConfig(event_count=10, quote_keys=10, paper_tickets=11)

        config = EnduranceConfig(event_count=20, quote_keys=10, batch_size=10, restart_cycles=1, paper_tickets=5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_endurance(root, config)
            with self.assertRaisesRegex(ValueError, "workspace must be fresh"):
                run_endurance(root, config)


if __name__ == "__main__":
    unittest.main()
