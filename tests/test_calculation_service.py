from __future__ import annotations

import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.calculation import CalculationEngine
from autosport.calculation_service import CalculationService
from autosport.domain import MarketEvent, MarketType


class CalculationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CalculationService()

    def _event(
        self,
        *,
        selection_id: str = "driver-a",
        decimal_odds: str = "2.50",
        source_id: str = "provider-a",
        sequence: int = 1,
        observed_ts: str = "2026-09-14T12:00:00+00:00",
        source_ts: str | None = "2026-09-14T11:59:59+00:00",
        metadata: dict[str, object] | None = None,
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
            source_ts=source_ts,
            ingest_ts="2026-09-14T12:05:00+00:00",
            metadata={} if metadata is None else metadata,
        )

    def test_rejects_noncanonical_engine_collaborators_before_calculation(self) -> None:
        class SubstitutingEngine(CalculationEngine):
            def implied_probability(self, decimal_odds: object) -> object:  # type: ignore[override]
                raise AssertionError("subclass formula authority must never be invoked")

        with self.assertRaisesRegex(ValueError, "engine must be an exact CalculationEngine"):
            CalculationService(SubstitutingEngine())
        with self.assertRaisesRegex(ValueError, "engine must be an exact CalculationEngine"):
            CalculationService(object())  # type: ignore[arg-type]

    def test_rejects_noncanonical_engine_result_before_evidence_publication(self) -> None:
        event = self._event()
        with patch.object(CalculationEngine, "implied_probability", return_value=object()):
            with self.assertRaisesRegex(
                ValueError,
                "calculation result must be an exact CalculationResult",
            ):
                self.service.implied_probability_for_event(
                    event,
                    causal_cutoff_ts="2026-09-14T12:00:00Z",
                )

    def test_quote_bound_calculations_delegate_to_canonical_engine(self) -> None:
        event = self._event()
        cutoff = "2026-09-14T12:00:00Z"

        cases = (
            self.service.odds_conversion_for_event(event, causal_cutoff_ts=cutoff),
            self.service.implied_probability_for_event(event, causal_cutoff_ts=cutoff),
            self.service.expected_return_for_event(
                event,
                Decimal("0.5"),
                stake=Decimal("10"),
                causal_cutoff_ts=cutoff,
            ),
            self.service.paper_payout_for_event(
                event,
                Decimal("10"),
                causal_cutoff_ts=cutoff,
            ),
            self.service.fractional_kelly_for_event(
                event,
                Decimal("0.5"),
                fraction=Decimal("0.5"),
                cap=Decimal("0.1"),
                causal_cutoff_ts=cutoff,
            ),
        )

        self.assertEqual(
            [evidence.result.calculation_id for evidence in cases],
            [
                "odds_conversion",
                "implied_probability",
                "expected_return",
                "paper_payout",
                "fractional_kelly",
            ],
        )
        for evidence in cases:
            self.assertEqual(evidence.service_version, "calculation-service-v1")
            self.assertFalse(evidence.real_money_execution)
            self.assertEqual(evidence.causal_cutoff_ts, "2026-09-14T12:00:00+00:00")
            self.assertEqual(len(evidence.quote_sources), 1)
            self.assertEqual(evidence.quote_sources[0].source_id, "provider-a")
            self.assertEqual(evidence.quote_sources[0].decimal_odds, "2.50")

    def test_same_quote_and_equivalent_cutoff_have_stable_evidence_hash(self) -> None:
        event = self._event()
        first = self.service.implied_probability_for_event(
            event,
            causal_cutoff_ts="2026-09-14T12:00:00Z",
        )
        second = self.service.implied_probability_for_event(
            event,
            causal_cutoff_ts="2026-09-14T14:00:00+02:00",
        )

        self.assertEqual(first.result.result_hash, second.result.result_hash)
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(
            first.quote_sources[0].quote_evidence_sha256,
            second.quote_sources[0].quote_evidence_sha256,
        )

    def test_text_export_is_complete_deterministic_and_round_trippable(self) -> None:
        event = self._event(metadata={"future_quote": "never serialize this"})
        first = self.service.expected_return_for_event(
            event,
            Decimal("0.5"),
            stake=Decimal("10"),
            causal_cutoff_ts="2026-09-14T12:00:00Z",
        )
        second = self.service.expected_return_for_event(
            event,
            Decimal("0.5"),
            stake=Decimal("10"),
            causal_cutoff_ts="2026-09-14T14:00:00+02:00",
        )

        text = first.to_text()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text, second.to_text())
        self.assertEqual(json.loads(text), first.as_dict())
        self.assertIn('"method": "bernoulli_expected_return"', text)
        self.assertIn('"input_units"', text)
        self.assertIn('"assumptions"', text)
        self.assertIn('"quote_evidence_sha256"', text)
        self.assertIn('"real_money_execution": false', text)
        self.assertNotIn("never serialize this", text)

    def test_source_identity_changes_evidence_even_when_numeric_result_is_same(self) -> None:
        first = self.service.implied_probability_for_event(
            self._event(source_id="provider-a"),
            causal_cutoff_ts="2026-09-14T12:00:00+00:00",
        )
        second = self.service.implied_probability_for_event(
            self._event(source_id="provider-b"),
            causal_cutoff_ts="2026-09-14T12:00:00+00:00",
        )

        self.assertEqual(first.result.result_hash, second.result.result_hash)
        self.assertNotEqual(
            first.quote_sources[0].quote_evidence_sha256,
            second.quote_sources[0].quote_evidence_sha256,
        )
        self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)

    def test_changed_quote_changes_result_and_bound_evidence_identity(self) -> None:
        first = self.service.implied_probability_for_event(
            self._event(decimal_odds="2.50", sequence=1),
            causal_cutoff_ts="2026-09-14T12:01:00+00:00",
        )
        second = self.service.implied_probability_for_event(
            self._event(
                decimal_odds="3.00",
                sequence=2,
                observed_ts="2026-09-14T12:00:30+00:00",
            ),
            causal_cutoff_ts="2026-09-14T12:01:00+00:00",
        )

        self.assertNotEqual(first.result.result_hash, second.result.result_hash)
        self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)

    def test_future_quote_is_rejected_instead_of_substituted(self) -> None:
        cutoff = "2026-09-14T12:00:30+00:00"
        accepted = self.service.implied_probability_for_event(
            self._event(decimal_odds="2.50", sequence=1),
            causal_cutoff_ts=cutoff,
        )
        self.assertEqual(accepted.quote_sources[0].decimal_odds, "2.50")

        with self.assertRaisesRegex(ValueError, "after the calculation causal cutoff"):
            self.service.implied_probability_for_event(
                self._event(
                    decimal_odds="3.00",
                    sequence=2,
                    observed_ts="2026-09-14T12:01:00+00:00",
                ),
                causal_cutoff_ts=cutoff,
            )

    def test_mutable_metadata_and_future_outcome_fields_are_not_part_of_evidence(self) -> None:
        clean = self.service.implied_probability_for_event(
            self._event(metadata={}),
            causal_cutoff_ts="2026-09-14T12:00:00+00:00",
        )
        contaminated = self.service.implied_probability_for_event(
            self._event(
                metadata={
                    "final_result": "driver-a",
                    "winner": "driver-a",
                    "future_quote": "99",
                }
            ),
            causal_cutoff_ts="2026-09-14T12:00:00+00:00",
        )

        self.assertEqual(clean.evidence_sha256, contaminated.evidence_sha256)
        self.assertNotIn("metadata", contaminated.quote_sources[0].as_dict())
        self.assertNotIn("status", contaminated.quote_sources[0].as_dict())
        self.assertNotIn("score_state", contaminated.quote_sources[0].as_dict())

    def test_market_devig_binds_exact_source_quotes_and_is_order_invariant(self) -> None:
        first = self._event(selection_id="driver-a", decimal_odds="2.10", sequence=1)
        second = self._event(
            selection_id="driver-b",
            decimal_odds="2.05",
            sequence=2,
            observed_ts="2026-09-14T12:00:01+00:00",
        )
        cutoff = "2026-09-14T12:00:01+00:00"

        forward = self.service.multiplicative_devig_for_market(
            (first, second),
            causal_cutoff_ts=cutoff,
        )
        reverse = self.service.multiplicative_devig_for_market(
            (second, first),
            causal_cutoff_ts=cutoff,
        )

        self.assertEqual(forward.result.calculation_id, "multiplicative_devig")
        self.assertEqual(forward.result.result_hash, reverse.result.result_hash)
        self.assertEqual(forward.evidence_sha256, reverse.evidence_sha256)
        self.assertEqual(
            [source.selection_id for source in forward.quote_sources],
            ["driver-a", "driver-b"],
        )

    def test_market_devig_rejects_cross_source_or_duplicate_selection_mix(self) -> None:
        first = self._event(selection_id="driver-a", sequence=1)
        cross_source = self._event(
            selection_id="driver-b",
            source_id="provider-b",
            sequence=2,
        )
        duplicate = self._event(
            selection_id="driver-a",
            decimal_odds="2.60",
            sequence=2,
        )
        cutoff = "2026-09-14T12:01:00+00:00"

        with self.assertRaisesRegex(ValueError, "same event, market, source"):
            self.service.multiplicative_devig_for_market(
                (first, cross_source),
                causal_cutoff_ts=cutoff,
            )
        with self.assertRaisesRegex(ValueError, "one exact quote per selection"):
            self.service.multiplicative_devig_for_market(
                (first, duplicate),
                causal_cutoff_ts=cutoff,
            )

    def test_quote_timestamp_fields_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_ts must be a timezone-aware"):
            self.service.implied_probability_for_event(
                self._event(source_ts="2026-09-14T11:59:59"),
                causal_cutoff_ts="2026-09-14T12:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "causal_cutoff_ts must be a timezone-aware"):
            self.service.implied_probability_for_event(
                self._event(),
                causal_cutoff_ts="2026-09-14T12:00:00",
            )

    def test_noncanonical_market_event_quote_fields_fail_before_calculation(self) -> None:
        invalid = self._event()
        object.__setattr__(invalid, "decimal_odds", Decimal("NaN"))

        with self.assertRaisesRegex(ValueError, "quote fields are not canonical"):
            self.service.implied_probability_for_event(
                invalid,
                causal_cutoff_ts="2026-09-14T12:00:00+00:00",
            )

    def test_non_utf8_quote_identity_fails_closed_before_calculation(self) -> None:
        for field in ("event_id", "market_id", "selection_id", "source_id"):
            with self.subTest(field=field):
                invalid = self._event()
                object.__setattr__(invalid, field, "invalid-" + chr(0xD800))

                with self.assertRaisesRegex(
                    ValueError,
                    "calculation evidence text must be valid UTF-8",
                ):
                    self.service.implied_probability_for_event(
                        invalid,
                        causal_cutoff_ts="2026-09-14T12:00:00+00:00",
                    )


if __name__ == "__main__":
    unittest.main()
