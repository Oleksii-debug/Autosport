from __future__ import annotations

import argparse
import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.dataset_calculation_cli import calculate_dataset_quote, render_result, run
from autosport.domain import MarketEvent, MarketType


class _FakeDataset:
    def __init__(self, events: list[MarketEvent]) -> None:
        self.name = "sealed-fixture"
        self.sport = "motorsport"
        self.schema_version = 3
        self.market_sha256 = "a" * 64
        self.import_identity = "b" * 64
        self._events = events

    def load_market_events(self) -> list[MarketEvent]:
        return list(self._events)

    def load_results_after_replay(self):  # pragma: no cover - must never be called
        raise AssertionError("selected-quote CLI must not access sealed outcomes")


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "path": "unused-dataset",
        "event_id": "race-1",
        "market_id": "winner",
        "selection_id": "driver-a",
        "source_id": "provider-a",
        "sequence": 7,
        "cutoff": "2026-09-19T12:00:00+00:00",
        "operation": "implied-probability",
        "probability": None,
        "stake": "1",
        "fraction": "1",
        "cap": "1",
        "format": "json",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _event(
    *,
    status: str = "open",
    observed_ts: str = "2026-09-19T11:59:00+00:00",
    source_ts: str | None = "2026-09-19T11:58:00+00:00",
    ingest_ts: str = "2026-09-19T11:59:30+00:00",
    decimal_odds: str = "2.50",
    selection_id: str = "driver-a",
    source_id: str = "provider-a",
    sequence: int = 7,
) -> MarketEvent:
    return MarketEvent(
        event_id="race-1",
        market_id="winner",
        selection_id=selection_id,
        decimal_odds=Decimal(decimal_odds),
        observed_ts=observed_ts,
        source_id=source_id,
        sequence=sequence,
        market_type=MarketType.WINNER,
        status=status,
        source_ts=source_ts,
        ingest_ts=ingest_ts,
    )


class DatasetQuoteCalculationCliTests(unittest.TestCase):
    def test_valid_exact_quote_is_calculated_without_outcome_access(self) -> None:
        dataset = _FakeDataset([_event()])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ):
            result = calculate_dataset_quote(_args())

        self.assertEqual(result["selected_quote_identity"]["selection_id"], "driver-a")
        self.assertEqual(result["selected_quote_identity"]["sequence"], 7)
        self.assertEqual(result["dataset"]["market_sha256"], "a" * 64)
        self.assertEqual(result["dataset"]["import_identity"], "b" * 64)
        self.assertTrue(result["causal_available_through_cutoff"])
        self.assertFalse(result["outcomes_accessed"])
        self.assertTrue(result["paper_only"])
        self.assertEqual(result["result"]["calculation_id"], "implied_probability")

    def test_exact_identity_does_not_substitute_a_different_sequence(self) -> None:
        dataset = _FakeDataset([_event(sequence=8)])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "exact selected quote was not found"):
            calculate_dataset_quote(_args())

    def test_duplicate_exact_identity_is_rejected_as_ambiguous(self) -> None:
        dataset = _FakeDataset([_event(), _event()])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "identity is ambiguous"):
            calculate_dataset_quote(_args())

    def test_closed_quote_is_rejected_before_calculation(self) -> None:
        dataset = _FakeDataset([_event(status="closed")])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "closed or non-actionable"):
            calculate_dataset_quote(_args())

    def test_future_observed_quote_is_rejected(self) -> None:
        dataset = _FakeDataset([_event(observed_ts="2026-09-19T12:00:01+00:00")])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "observed_ts is after"):
            calculate_dataset_quote(_args())

    def test_late_source_timestamp_is_rejected(self) -> None:
        dataset = _FakeDataset([_event(source_ts="2026-09-19T12:00:01+00:00")])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "source_ts is after"):
            calculate_dataset_quote(_args())

    def test_late_ingest_timestamp_is_rejected(self) -> None:
        dataset = _FakeDataset([_event(ingest_ts="2026-09-19T12:00:01+00:00")])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "ingest_ts is after"):
            calculate_dataset_quote(_args())

    def test_missing_source_timestamp_is_rejected(self) -> None:
        dataset = _FakeDataset([_event(source_ts=None)])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "lacks source availability"):
            calculate_dataset_quote(_args())

    def test_invalid_probability_is_rejected_before_service(self) -> None:
        dataset = _FakeDataset([_event()])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), self.assertRaisesRegex(ValueError, "probability must be a valid Decimal"):
            calculate_dataset_quote(
                _args(operation="expected-return", probability="not-a-number")
            )

    def test_rendered_json_is_deterministic(self) -> None:
        dataset = _FakeDataset([_event()])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ):
            first = calculate_dataset_quote(_args())
            second = calculate_dataset_quote(_args())

        self.assertEqual(
            render_result(first, "json"),
            render_result(second, "json"),
        )
        self.assertNotIn("settled", render_result(first, "json"))
        self.assertNotIn("winner", render_result(first, "json"))

    def test_run_fail_closed_returns_nonzero_without_partial_result(self) -> None:
        dataset = _FakeDataset([_event(observed_ts="2026-09-19T12:01:00+00:00")])
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ):
            rc = run(_args())

        self.assertEqual(rc, 3)

if __name__ == "__main__":
    unittest.main()
