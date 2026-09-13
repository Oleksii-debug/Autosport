from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from autosport.dataset import DatasetGovernance
from autosport.domain import MarketEvent, MarketType
from autosport.evaluation import EvaluationSummary
from autosport.portfolio import PortfolioReport
from autosport.price_truth import (
    classify_market_price_truth,
    market_price_truth_from_events,
    market_price_truth_from_run_summary,
)
from autosport.replay import ReplayRun
from autosport.session import AutosportSession, SessionResult
from autosport.ui_model import evaluation_lines


class MarketPriceTruthTests(unittest.TestCase):
    def _event(
        self,
        *,
        semantics: str,
        executable: bool,
        sequence: int = 1,
        source_id: str = "betfair_exchange_historical",
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id=f"selection-{sequence}",
            decimal_odds=Decimal("2.50"),
            observed_ts=f"2026-01-01T00:0{sequence}:00+00:00",
            source_id=source_id,
            sequence=sequence,
            market_type=MarketType.WINNER,
            status="open",
            source_ts=f"2026-01-01T00:0{sequence}:00+00:00",
            ingest_ts="2026-01-02T00:00:01+00:00",
            metadata={
                "price_semantics": semantics,
                "execution_quote_verified": executable,
            },
        )

    def test_legacy_betfair_provider_identity_is_only_conservative_fallback(self) -> None:
        truth = classify_market_price_truth(["betfair_exchange_historical"])

        self.assertEqual(truth.price_semantics, "legacy_betfair_last_traded_price_unverified")
        self.assertFalse(truth.executable_quote_verified)
        self.assertFalse(truth.paper_fill_fidelity_verified)

    def test_canonical_ltp_event_is_last_traded_not_executable(self) -> None:
        truth = market_price_truth_from_events(
            [self._event(semantics="betfair_last_traded_price", executable=False)]
        )

        self.assertEqual(truth.price_semantics, "betfair_last_traded_price")
        self.assertFalse(truth.executable_quote_verified)
        self.assertFalse(truth.paper_fill_fidelity_verified)

    def test_same_provider_can_expose_explicit_executable_offer_without_claiming_fill(self) -> None:
        truth = market_price_truth_from_events(
            [self._event(semantics="betfair_best_available_to_back", executable=True)]
        )

        self.assertEqual(truth.source_ids, ("betfair_exchange_historical",))
        self.assertEqual(truth.price_semantics, "betfair_best_available_to_back")
        self.assertTrue(truth.executable_quote_verified)
        self.assertFalse(truth.paper_fill_fidelity_verified)

    def test_mixed_or_incomplete_event_semantics_fail_closed(self) -> None:
        mixed = market_price_truth_from_events(
            [
                self._event(semantics="betfair_last_traded_price", executable=False, sequence=1),
                self._event(semantics="betfair_best_available_to_back", executable=True, sequence=2),
            ]
        )

        self.assertEqual(mixed.price_semantics, "unspecified_or_mixed_observation")
        self.assertFalse(mixed.executable_quote_verified)
        self.assertFalse(mixed.paper_fill_fidelity_verified)

    def test_unknown_or_mixed_sources_fail_closed_on_execution_fidelity(self) -> None:
        truth = classify_market_price_truth(["fixture", "betfair_exchange_historical"])

        self.assertEqual(truth.price_semantics, "unspecified_or_mixed_observation")
        self.assertFalse(truth.executable_quote_verified)
        self.assertFalse(truth.paper_fill_fidelity_verified)

    def test_run_summary_persists_canonical_event_price_truth_and_ui_reports_it(self) -> None:
        session = AutosportSession.__new__(AutosportSession)
        session.strategy_id = "baseline-v1"
        session.strategy = SimpleNamespace(
            strategy_id="baseline-v1",
            label="Baseline",
            agent_names=(),
            opens_paper_tickets=True,
        )
        session.research_plan = None
        governance = DatasetGovernance(
            source_identity="betfair-historical-files:" + "1" * 64,
            terms_reference="user-supplied terms reference",
            retention_basis="user-supplied local retention basis",
            redistribution_policy="prohibited",
            acquired_at="2026-01-02T00:00:00Z",
            imported_at="2026-01-02T00:00:01Z",
            coverage_start_ts="2026-01-01T00:00:00Z",
            coverage_end_ts="2026-01-01T00:10:00Z",
            source_ids=("betfair_exchange_historical",),
            market_types=("winner",),
            outcome_reveal_after="2026-01-01T00:11:00Z",
        )
        events = [self._event(semantics="betfair_last_traded_price", executable=False)]
        dataset = SimpleNamespace(
            name="betfair-table-tennis-history",
            sport="table_tennis",
            schema_version=2,
            import_identity="a" * 64,
            governance=governance,
            market_sha256="b" * 64,
            results_sha256="c" * 64,
            load_market_events=lambda: events,
        )
        result = SessionResult(
            replay=ReplayRun(
                run_id="12345678-1234-1234-1234-123456789abc",
                dataset_hash="d" * 64,
                event_count=1,
                started_at="2026-01-02T01:00:00Z",
                completed_at="2026-01-02T01:00:01Z",
            ),
            settled_ticket_ids=("ticket-1",),
            balance=Decimal("10001"),
            evaluation=EvaluationSummary(
                initial_bankroll=Decimal("10000"),
                final_balance=Decimal("10001"),
                committed_stake=Decimal("0"),
                settled_stake=Decimal("1"),
                net_profit=Decimal("1"),
                roi=Decimal("1"),
                won=1,
                lost=0,
                void=0,
            ),
            portfolio=PortfolioReport(
                mode="exact",
                scenario_count=1,
                worst_case=Decimal("0"),
                best_case=Decimal("0"),
                mean_case=Decimal("0"),
            ),
            experiment_key="experiment",
            result_path="",
        )

        payload = session._run_summary_payload(dataset, result)
        self.assertEqual(
            payload["market_price_truth"],
            {
                "price_semantics": "betfair_last_traded_price",
                "executable_quote_verified": False,
                "paper_fill_fidelity_verified": False,
                "source_ids": ["betfair_exchange_historical"],
            },
        )
        loaded = market_price_truth_from_run_summary(payload)
        self.assertEqual(loaded.price_semantics, "betfair_last_traded_price")

        with tempfile.TemporaryDirectory() as temporary:
            summary_path = Path(temporary) / "run.json"
            summary_path.write_text(json.dumps(payload), encoding="utf-8")
            ui_result = SessionResult(
                replay=result.replay,
                settled_ticket_ids=result.settled_ticket_ids,
                balance=result.balance,
                evaluation=result.evaluation,
                portfolio=result.portfolio,
                experiment_key=result.experiment_key,
                result_path=str(summary_path),
            )
            lines = evaluation_lines(ui_result)

        price_lines = [line for line in lines if line.startswith("Price truth |")]
        self.assertEqual(len(price_lines), 1)
        self.assertIn("last-traded/last-matched", price_lines[0])
        self.assertIn("executable quote verified=false", price_lines[0])
        self.assertIn("paper fill fidelity verified=false", price_lines[0])

    def test_explicit_invalid_fill_claim_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "fill fidelity"):
            market_price_truth_from_run_summary(
                {
                    "market_price_truth": {
                        "price_semantics": "last_traded_price",
                        "executable_quote_verified": False,
                        "paper_fill_fidelity_verified": True,
                        "source_ids": ["betfair_exchange_historical"],
                    }
                }
            )

    def test_malformed_explicit_run_truth_does_not_fall_back_silently(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed"):
            market_price_truth_from_run_summary(
                {
                    "market_price_truth": {
                        "price_semantics": "betfair_best_available_to_back",
                        "executable_quote_verified": "yes",
                        "paper_fill_fidelity_verified": False,
                        "source_ids": ["betfair_exchange_historical"],
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
