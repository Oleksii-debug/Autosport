from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autosport.cli import main as cli_main
from autosport.dataset import ReplayDataset
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


def _cli_argv(path: str | Path = "unused-dataset") -> list[str]:
    return [
        "calculate-dataset-quote",
        str(path),
        "--event-id",
        "race-1",
        "--market-id",
        "winner",
        "--selection-id",
        "driver-a",
        "--source-id",
        "provider-a",
        "--sequence",
        "7",
        "--cutoff",
        "2026-09-19T12:00:00+00:00",
        "--operation",
        "implied-probability",
        "--format",
        "json",
    ]


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
    def test_production_cli_routes_selected_quote_command(self) -> None:
        dataset = _FakeDataset([_event()])
        output = io.StringIO()
        with patch(
            "autosport.dataset_calculation_cli.load_dataset",
            return_value=dataset,
        ), redirect_stdout(output):
            rc = cli_main(_cli_argv())

        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["selected_quote_identity"]["selection_id"], "driver-a")
        self.assertEqual(payload["result"]["calculation_id"], "implied_probability")
        self.assertFalse(payload["outcomes_accessed"])
        self.assertTrue(payload["paper_only"])

    def test_production_cli_fails_closed_on_tampered_market_hash(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            market_path = root / "market.jsonl"
            results_path = root / "results.json"
            market_path.write_text(
                json.dumps(
                    {
                        "event_id": "race-1",
                        "market_id": "winner",
                        "selection_id": "driver-a",
                        "decimal_odds": "2.50",
                        "observed_ts": "2026-09-19T11:59:00+00:00",
                        "source_id": "provider-a",
                        "sequence": 7,
                        "market_type": "winner",
                        "status": "open",
                        "source_ts": "2026-09-19T11:58:00+00:00",
                        "ingest_ts": "2026-09-19T11:59:30+00:00",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            results_path.write_text("{}", encoding="utf-8")
            dataset = ReplayDataset(
                root=root,
                name="tampered-fixture",
                sport="unknown",
                market_path=market_path,
                results_path=results_path,
                market_sha256="0" * 64,
                results_sha256="0" * 64,
            )
            output = io.StringIO()
            with patch(
                "autosport.dataset_calculation_cli.load_dataset",
                return_value=dataset,
            ), redirect_stdout(output):
                rc = cli_main(_cli_argv(root))

        self.assertEqual(rc, 3)
        self.assertIn("market dataset hash changed after verification", output.getvalue())
        self.assertNotIn('"result"', output.getvalue())

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

        rendered = render_result(first, "json")
        self.assertEqual(rendered, render_result(second, "json"))
        payload = json.loads(rendered)
        self.assertFalse(payload["outcomes_accessed"])
        self.assertNotIn("settled", payload)
        self.assertNotIn("settlement", payload)
        self.assertNotIn("outcome", payload)
        # "winner" is a valid market_type/market_id fixture value and is not
        # evidence of reading sealed settlement/outcome payloads.
        self.assertEqual(payload["selected_quote_identity"]["market_type"], MarketType.WINNER.value)

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
