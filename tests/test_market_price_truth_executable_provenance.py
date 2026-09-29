from __future__ import annotations

import unittest

from autosport.price_truth import (
    classify_market_price_truth,
    market_price_truth_from_run_summary,
)


class _HostileEmptySourceId(str):
    def __new__(cls) -> "_HostileEmptySourceId":
        return super().__new__(cls, "")

    def __init__(self) -> None:
        self.operations = 0

    def __bool__(self) -> bool:
        self.operations += 1
        return True

    def strip(self, chars: str | None = None) -> "_HostileEmptySourceId":
        del chars
        self.operations += 1
        return self

    def __hash__(self) -> int:
        self.operations += 1
        return str.__hash__(self)


class _HostileObservationalSemantic(str):
    def __new__(cls) -> "_HostileObservationalSemantic":
        return super().__new__(cls, "betfair_last_traded_price")

    def __init__(self) -> None:
        self.operations = 0

    def strip(self, chars: str | None = None) -> "_HostileObservationalSemantic":
        del chars
        self.operations += 1
        return self

    def __hash__(self) -> int:
        self.operations += 1
        return str.__hash__("hostile-semantic-membership-bypass")


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

    def test_executable_truth_rejects_empty_str_subclass_before_string_operations(self) -> None:
        source_id = _HostileEmptySourceId()

        with self.assertRaisesRegex(ValueError, "non-empty trimmed strings"):
            classify_market_price_truth(
                (source_id,),
                price_semantics="offered_quote",
                execution_quote_verified=True,
            )

        self.assertEqual(source_id.operations, 0)

    def test_observational_semantic_str_subclass_is_not_executable(self) -> None:
        semantics = _HostileObservationalSemantic()

        with self.assertRaisesRegex(ValueError, "price_semantics must be a non-empty string"):
            classify_market_price_truth(
                ("fixture",),
                price_semantics=semantics,
                execution_quote_verified=True,
            )

        self.assertEqual(semantics.operations, 0)

    def test_run_summary_rejects_semantic_str_subclass_before_string_operations(self) -> None:
        semantics = _HostileObservationalSemantic()
        payload = {
            "market_price_truth": {
                "price_semantics": semantics,
                "executable_quote_verified": True,
                "paper_fill_fidelity_verified": False,
                "source_ids": ["fixture"],
            }
        }

        with self.assertRaisesRegex(ValueError, "market_price_truth is malformed"):
            market_price_truth_from_run_summary(payload)

        self.assertEqual(semantics.operations, 0)


if __name__ == "__main__":
    unittest.main()
