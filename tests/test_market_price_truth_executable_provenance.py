from __future__ import annotations

import unittest

from autosport.price_truth import (
    classify_market_price_truth,
    market_price_truth_from_run_summary,
)


class MarketPriceTruthExecutableProvenanceTests(unittest.TestCase):
    def test_explicit_executable_truth_requires_at_least_one_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one canonical source_id"):
            classify_market_price_truth(
                (),
                price_semantics="offered_quote",
                execution_quote_verified=True,
            )

    def test_run_summary_cannot_claim_executable_quote_without_source_provenance(self) -> None:
        payload = {
            "market_price_truth": {
                "price_semantics": "offered_quote",
                "executable_quote_verified": True,
                "paper_fill_fidelity_verified": False,
                "source_ids": [],
            }
        }

        with self.assertRaisesRegex(ValueError, "at least one canonical source_id"):
            market_price_truth_from_run_summary(payload)

    def test_schema_v2_empty_governance_cannot_authorize_executable_quote_claim(self) -> None:
        payload = {
            "dataset_schema_version": 2,
            "dataset_governance": {"source_ids": []},
            "market_price_truth": {
                "price_semantics": "offered_quote",
                "executable_quote_verified": True,
                "paper_fill_fidelity_verified": False,
                "source_ids": [],
            },
        }

        with self.assertRaisesRegex(ValueError, "at least one canonical source_id"):
            market_price_truth_from_run_summary(payload)

    def test_non_executable_empty_source_fallback_remains_conservative(self) -> None:
        truth = classify_market_price_truth(
            (),
            price_semantics="unspecified_observation",
            execution_quote_verified=False,
        )

        self.assertEqual(truth.source_ids, ())
        self.assertEqual(truth.price_semantics, "unspecified_observation")
        self.assertFalse(truth.executable_quote_verified)
        self.assertFalse(truth.paper_fill_fidelity_verified)


if __name__ == "__main__":
    unittest.main()
