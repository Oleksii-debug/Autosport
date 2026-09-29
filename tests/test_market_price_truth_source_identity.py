from __future__ import annotations

import unittest
from types import SimpleNamespace

from autosport.price_truth import (
    classify_market_price_truth,
    market_price_truth_from_events,
    market_price_truth_from_run_summary,
)


class MarketPriceTruthSourceIdentityTests(unittest.TestCase):
    def test_source_id_collection_cannot_be_a_scalar_string(self) -> None:
        with self.assertRaisesRegex(ValueError, "iterable of canonical source-id strings"):
            classify_market_price_truth("fixture")  # type: ignore[arg-type]

    def test_source_ids_reject_type_coercion_and_whitespace_normalization(self) -> None:
        for source_ids in ([7], [True], [" fixture"], ["fixture "], [""]):
            with self.subTest(source_ids=source_ids):
                with self.assertRaisesRegex(ValueError, "non-empty trimmed strings"):
                    classify_market_price_truth(source_ids)  # type: ignore[arg-type]

    def test_event_source_identity_must_already_be_canonical(self) -> None:
        event = SimpleNamespace(
            source_id=" fixture",
            metadata={
                "price_semantics": "offered_quote",
                "execution_quote_verified": True,
            },
        )
        with self.assertRaisesRegex(ValueError, "event source_ids"):
            market_price_truth_from_events([event])

    def test_governance_source_identity_is_not_silently_trimmed(self) -> None:
        payload = {
            "dataset_governance": {"source_ids": [" fixture"]},
            "market_price_truth": {
                "price_semantics": "offered_quote",
                "executable_quote_verified": True,
                "paper_fill_fidelity_verified": False,
                "source_ids": ["fixture"],
            },
        }
        with self.assertRaisesRegex(ValueError, "dataset_governance.source_ids is malformed"):
            market_price_truth_from_run_summary(payload)

    def test_explicit_truth_source_identity_is_not_silently_trimmed(self) -> None:
        payload = {
            "dataset_governance": {"source_ids": ["fixture"]},
            "market_price_truth": {
                "price_semantics": "offered_quote",
                "executable_quote_verified": True,
                "paper_fill_fidelity_verified": False,
                "source_ids": ["fixture "],
            },
        }
        with self.assertRaisesRegex(ValueError, "market_price_truth is malformed"):
            market_price_truth_from_run_summary(payload)

    def test_valid_source_ids_keep_deterministic_set_semantics(self) -> None:
        truth = classify_market_price_truth(["source-b", "source-a", "source-b"])
        self.assertEqual(truth.source_ids, ("source-a", "source-b"))
        self.assertEqual(truth.price_semantics, "unspecified_or_mixed_observation")
        self.assertFalse(truth.executable_quote_verified)
        self.assertFalse(truth.paper_fill_fidelity_verified)


if __name__ == "__main__":
    unittest.main()
