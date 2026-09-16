from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autosport.price_truth import (
    classify_market_price_truth,
    market_price_truth_from_run_summary,
)
from autosport.ui_model import _market_price_truth_line


class MarketPriceTruthAdversarialTests(unittest.TestCase):
    def test_executable_quote_cannot_self_promote_fill_fidelity(self) -> None:
        with self.assertRaisesRegex(ValueError, "independent fill evidence"):
            market_price_truth_from_run_summary(
                {
                    "market_price_truth": {
                        "price_semantics": "betfair_available_to_back",
                        "executable_quote_verified": True,
                        "paper_fill_fidelity_verified": True,
                        "source_ids": ["betfair_exchange_historical"],
                    }
                }
            )

    def test_governed_source_ids_bind_explicit_price_truth_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not match dataset_governance.source_ids"):
            market_price_truth_from_run_summary(
                {
                    "dataset_governance": {
                        "source_ids": ["betfair_exchange_historical"],
                    },
                    "market_price_truth": {
                        "price_semantics": "betfair_available_to_back",
                        "executable_quote_verified": True,
                        "paper_fill_fidelity_verified": False,
                        "source_ids": ["forged-provider"],
                    },
                }
            )

    def test_betfair_last_traded_price_cannot_be_marked_executable(self) -> None:
        with self.assertRaisesRegex(ValueError, "observational"):
            classify_market_price_truth(
                ["betfair_exchange_historical"],
                price_semantics="betfair_last_traded_price",
                execution_quote_verified=True,
            )

    def test_evaluation_surface_exposes_malformed_explicit_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-summary.json"
            path.write_text(
                json.dumps(
                    {
                        "market_price_truth": {
                            "price_semantics": "betfair_available_to_back",
                            "executable_quote_verified": "yes",
                            "paper_fill_fidelity_verified": False,
                            "source_ids": ["betfair_exchange_historical"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            line = _market_price_truth_line(SimpleNamespace(result_path=str(path)))

        self.assertTrue(line.startswith("Істина ціни | ПОМИЛКА —"))
        self.assertIn("malformed", line)
        self.assertNotIn("unspecified_or_mixed_observation", line)


if __name__ == "__main__":
    unittest.main()
