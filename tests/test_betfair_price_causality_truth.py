from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical
from autosport.dataset import load_dataset


class BetfairPriceCausalityTruthTests(unittest.TestCase):
    def test_multiple_source_files_fail_closed_independent_of_cli_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first.write_text("{}\n", encoding="utf-8")
            second.write_text("{}\n", encoding="utf-8")

            for index, inputs in enumerate(((first, second), (second, first)), start=1):
                output = root / f"dataset-{index}"
                with self.assertRaisesRegex(ValueError, "cross-file source order is ambiguous"):
                    import_betfair_historical(
                        inputs,
                        output,
                        acquired_at="2026-01-03T00:00:00Z",
                        imported_at="2026-01-04T00:00:00Z",
                        terms_reference="local-entitlement-reference",
                        retention_basis="local research retention",
                    )
                self.assertFalse(output.exists())

    def test_ltp_truth_is_identity_bound_and_survives_canonical_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "betfair.jsonl"
            output = root / "dataset"
            messages = [
                {
                    "op": "mcm",
                    "pt": 1767225600000,
                    "mc": [
                        {
                            "id": "1.100",
                            "marketDefinition": {
                                "eventId": "event-1",
                                "eventTypeId": "2593174",
                                "marketType": "MATCH_ODDS",
                                "eventName": "Player A v Player B",
                                "status": "OPEN",
                                "runners": [
                                    {"id": 1, "name": "Player A"},
                                    {"id": 2, "name": "Player B"},
                                ],
                            },
                            "rc": [{"id": 1, "ltp": 2.5}],
                        }
                    ],
                },
                {
                    "op": "mcm",
                    "pt": 1767225660000,
                    "mc": [
                        {
                            "id": "1.100",
                            "marketDefinition": {
                                "eventId": "event-1",
                                "eventTypeId": "2593174",
                                "marketType": "MATCH_ODDS",
                                "status": "CLOSED",
                                "runners": [
                                    {"id": 1, "name": "Player A", "status": "WINNER"},
                                    {"id": 2, "name": "Player B", "status": "LOSER"},
                                ],
                            },
                        }
                    ],
                },
            ]
            source.write_text(
                "".join(json.dumps(message, separators=(",", ":")) + "\n" for message in messages),
                encoding="utf-8",
            )

            import_betfair_historical(
                [source],
                output,
                acquired_at="2026-01-03T00:00:00Z",
                imported_at="2026-01-04T00:00:00Z",
                terms_reference="local-entitlement-reference",
                retention_basis="local research retention",
            )

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            price_truth = manifest["governance"]["price_truth"]
            self.assertEqual(price_truth["price_semantics"], "last_traded_price")
            self.assertIs(price_truth["execution_quote_verified"], False)
            self.assertIs(price_truth["paper_fill_fidelity_verified"], False)
            self.assertIs(price_truth["complete_market_availability_history_verified"], False)
            self.assertIs(manifest["governance"]["causality"]["cross_file_source_order_verified"], False)
            self.assertIs(manifest["governance"]["causality"]["multi_file_composition_supported"], False)

            dataset = load_dataset(output)
            events = dataset.load_market_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].metadata["price_semantics"], "last_traded_price")
            self.assertIs(events[0].metadata["execution_quote_verified"], False)
            self.assertIs(events[0].metadata["paper_fill_fidelity_verified"], False)


if __name__ == "__main__":
    unittest.main()
