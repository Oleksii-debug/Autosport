import hashlib
import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.calibration_diagnostics import (
    CalibrationDependenceAssumption,
    CalibrationPopulationEntry,
    CalibrationPopulationManifest,
    evaluate_calibration_coverage,
    evaluate_calibration_diagnostics,
)
from autosport.domain import TicketLeg
from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
)


class CalibrationDiagnosticsTests(unittest.TestCase):
    def _record(
        self,
        forecast_id: str,
        probability: str,
        *,
        generated_at: str = "2026-02-10T12:00:00+00:00",
        input_cutoff: str = "2026-02-10T11:59:00+00:00",
        training_cutoff: str = "2026-01-31T23:59:59+00:00",
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key=f"event-{forecast_id}|winner|selection-{forecast_id}",
            probability=Decimal(probability),
            model_id="calibration-baseline",
            model_version="1.0.0",
            strategy_version="calibration-diagnostics-v1",
            model_training_cutoff_ts=training_cutoff,
            input_cutoff_ts=input_cutoff,
            generated_at=generated_at,
            uncertainty=Decimal("0.05"),
            evidence_hashes=(
                hashlib.sha256(f"evidence:{forecast_id}".encode("utf-8")).hexdigest(),
            ),
            market_snapshot_hash=hashlib.sha256(
                f"snapshot:{forecast_id}".encode("utf-8")
            ).hexdigest(),
            provenance={"dataset": "causal-holdout"},
            forecast_id=forecast_id,
        )

    def _window(self) -> TemporalEvaluationWindow:
        return TemporalEvaluationWindow(
            "feb-holdout",
            "2026-01-31T23:59:59+00:00",
            "2026-02-01T00:00:00+00:00",
            "2026-02-28T23:59:59+00:00",
            "holdout",
        )

    def _manifest(
        self,
        records,
        *,
        selected_ids=None,
        source_snapshot_sha256="c" * 64,
        selection_policy_sha256="d" * 64,
    ):
        record_values = tuple(records)
        selected = (
            {record.forecast_id for record in record_values}
            if selected_ids is None
            else set(selected_ids)
        )
        return CalibrationPopulationManifest(
            window_id=self._window().window_id,
            source_snapshot_sha256=source_snapshot_sha256,
            selection_policy_sha256=selection_policy_sha256,
            entries=tuple(
                CalibrationPopulationEntry(
                    forecast_id=record.forecast_id,
                    forecast_sha256=record.canonical_hash,
                    selected=record.forecast_id in selected,
                )
                for record in record_values
            ),
        )

    def _evaluate(self, records, outcomes, window=None, **kwargs):
        record_values = tuple(records)
        population_manifest = kwargs.pop(
            "population_manifest",
            self._manifest(record_values),
        )
        return evaluate_calibration_diagnostics(
            record_values,
            outcomes,
            window or self._window(),
            dependence_assumption=(
                CalibrationDependenceAssumption.INDEPENDENT_BERNOULLI
            ),
            population_manifest=population_manifest,
            **kwargs,
        )

    def _cohort(self):
        records = (
            self._record("f-1", "0.90"),
            self._record(
                "f-2",
                "0.80",
                generated_at="2026-02-11T12:00:00+00:00",
                input_cutoff="2026-02-11T11:59:00+00:00",
            ),
            self._record(
                "f-3",
                "0.20",
                generated_at="2026-02-12T12:00:00+00:00",
                input_cutoff="2026-02-12T11:59:00+00:00",
            ),
            self._record(
                "f-4",
                "0.10",
                generated_at="2026-02-13T12:00:00+00:00",
                input_cutoff="2026-02-13T11:59:00+00:00",
            ),
        )
        outcomes = (
            ForecastOutcomeFact("f-1", 1, "2026-02-10T14:00:00+00:00"),
            ForecastOutcomeFact("f-2", 1, "2026-02-11T14:00:00+00:00"),
            ForecastOutcomeFact("f-3", 0, "2026-02-12T14:00:00+00:00"),
            ForecastOutcomeFact("f-4", 0, "2026-02-13T14:00:00+00:00"),
        )
        return records, outcomes

    def test_preserves_canonical_point_metrics_and_adds_bounded_uncertainty(self):
        records, outcomes = self._cohort()
        window = self._window()
        base = evaluate_forecast_window(records, outcomes, window, bins=5)
        report = self._evaluate(
            records,
            outcomes,
            window,
            bins=5,
            confidence_level=0.95,
        )

        self.assertEqual(report.count, base.count)
        self.assertEqual(report.brier_score.point, base.brier_score)
        self.assertEqual(report.log_loss.point, base.log_loss)
        self.assertLessEqual(report.brier_score.lower, report.brier_score.point)
        self.assertGreaterEqual(report.brier_score.upper, report.brier_score.point)
        self.assertLessEqual(report.log_loss.lower, report.log_loss.point)
        self.assertGreaterEqual(report.log_loss.upper, report.log_loss.point)
        self.assertEqual(sum(item.count for item in report.calibration), report.count)
        self.assertTrue(
            all(
                item.observed_rate_lower
                <= item.observed_rate
                <= item.observed_rate_upper
                for item in report.calibration
            )
        )
        self.assertLessEqual(
            report.expected_calibration_error.lower,
            report.expected_calibration_error.point,
        )
        self.assertGreaterEqual(
            report.expected_calibration_error.upper,
            report.expected_calibration_error.point,
        )
        self.assertFalse(report.promotion_authorized)
        self.assertFalse(report.real_money_execution)
        self.assertEqual(
            report.dependence_assumption,
            CalibrationDependenceAssumption.INDEPENDENT_BERNOULLI,
        )
        self.assertEqual(report.raw_sample_count, report.count)
        self.assertEqual(report.effective_sample_count, report.count)
        self.assertEqual(len(report.report_sha256), 64)

    def test_complete_cohort_is_required(self):
        records, outcomes = self._cohort()
        with self.assertRaisesRegex(ValueError, "complete calibration cohort required"):
            self._evaluate(
                records,
                outcomes[:-1],
                self._window(),
                bins=5,
            )

    def test_input_order_does_not_change_evidence_identity(self):
        records, outcomes = self._cohort()
        forward = self._evaluate(
            records,
            outcomes,
            self._window(),
            bins=5,
        )
        reverse = self._evaluate(
            tuple(reversed(records)),
            tuple(reversed(outcomes)),
            self._window(),
            bins=5,
        )
        self.assertEqual(forward.cohort_sha256, reverse.cohort_sha256)
        self.assertEqual(forward.config_sha256, reverse.config_sha256)
        self.assertEqual(forward.report_sha256, reverse.report_sha256)

    def test_outcome_change_changes_cohort_and_report_identity(self):
        records, outcomes = self._cohort()
        original = self._evaluate(
            records,
            outcomes,
            self._window(),
            bins=5,
        )
        changed_outcomes = outcomes[:-1] + (
            ForecastOutcomeFact("f-4", 1, "2026-02-13T14:00:00+00:00"),
        )
        changed = self._evaluate(
            records,
            changed_outcomes,
            self._window(),
            bins=5,
        )
        self.assertNotEqual(original.cohort_sha256, changed.cohort_sha256)
        self.assertNotEqual(original.report_sha256, changed.report_sha256)

    def test_duplicate_outcome_and_bad_config_fail_closed(self):
        records, outcomes = self._cohort()
        duplicate = outcomes + (outcomes[0],)
        with self.assertRaisesRegex(ValueError, "duplicate outcome"):
            self._evaluate(
                records,
                duplicate,
                self._window(),
                bins=5,
            )
        with self.assertRaisesRegex(ValueError, "bins must be a positive integer"):
            self._evaluate(
                records,
                outcomes,
                self._window(),
                bins=True,
            )
        with self.assertRaisesRegex(ValueError, "confidence_level"):
            self._evaluate(
                records,
                outcomes,
                self._window(),
                confidence_level=1,
            )

    def test_supported_boundaries_follow_canonical_bin_membership(self):
        records = (
            self._record("f-lower", "1e-15"),
            self._record(
                "f-upper",
                "0.999999999999999",
                generated_at="2026-02-11T12:00:00+00:00",
                input_cutoff="2026-02-11T11:59:00+00:00",
            ),
        )
        outcomes = (
            ForecastOutcomeFact("f-lower", 0, "2026-02-10T14:00:00+00:00"),
            ForecastOutcomeFact("f-upper", 1, "2026-02-11T14:00:00+00:00"),
        )
        report = self._evaluate(
            records,
            outcomes,
            self._window(),
            bins=2,
        )
        self.assertEqual(
            tuple(
                (item.bin_lower, item.bin_upper, item.count)
                for item in report.calibration
            ),
            ((0.0, 0.5, 1), (0.5, 1.0, 1)),
        )

    def test_bounded_log_loss_matches_canonical_scoring_inside_support(self):
        cases = (
            ("f-upper-wrong", "0.999999999999999", 0),
            ("f-lower-wrong", "1e-15", 1),
        )
        for forecast_id, probability, outcome in cases:
            with self.subTest(probability=probability, outcome=outcome):
                record = (self._record(forecast_id, probability),)
                facts = (
                    ForecastOutcomeFact(
                        forecast_id,
                        outcome,
                        "2026-02-10T14:00:00+00:00",
                    ),
                )
                canonical = evaluate_forecast_window(
                    record,
                    facts,
                    self._window(),
                    bins=1,
                )
                report = self._evaluate(
                    record,
                    facts,
                    self._window(),
                    bins=1,
                )

                self.assertEqual(report.log_loss.point, canonical.log_loss)
                self.assertLessEqual(report.log_loss.point, report.log_loss.upper)

    def test_wilson_extreme_observed_rates_preserve_exact_zero_one_bounds(self):
        zero_record = (self._record("f-zero-only", "0.20"),)
        zero_outcome = (
            ForecastOutcomeFact("f-zero-only", 0, "2026-02-10T14:00:00+00:00"),
        )
        zero_report = self._evaluate(
            zero_record,
            zero_outcome,
            self._window(),
            bins=1,
            confidence_level=0.80,
        )
        self.assertEqual(zero_report.calibration[0].observed_rate, 0.0)
        self.assertEqual(zero_report.calibration[0].observed_rate_lower, 0.0)

        one_record = (self._record("f-one-only", "0.80"),)
        one_outcome = (
            ForecastOutcomeFact("f-one-only", 1, "2026-02-10T14:00:00+00:00"),
        )
        one_report = self._evaluate(
            one_record,
            one_outcome,
            self._window(),
            bins=1,
            confidence_level=0.80,
        )
        self.assertEqual(one_report.calibration[0].observed_rate, 1.0)
        self.assertEqual(one_report.calibration[0].observed_rate_upper, 1.0)

    def test_post_cutoff_outcome_cannot_backfill_frozen_window(self):
        record = self._record("late-label", "0.80")
        late_outcome = ForecastOutcomeFact(
            "late-label",
            1,
            "2026-03-01T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "evaluation cutoff"):
            self._evaluate(
                (record,),
                (late_outcome,),
                self._window(),
                bins=2,
            )

    def test_multi_row_uncertainty_requires_explicit_dependence_assumption(self):
        records, outcomes = self._cohort()
        with self.assertRaisesRegex(ValueError, "INDEPENDENT_BERNOULLI"):
            evaluate_calibration_diagnostics(
                records,
                outcomes,
                self._window(),
                bins=5,
                population_manifest=self._manifest(records),
            )

    def test_iid_diagnostic_rejects_repeated_exact_quote_key(self):
        records, outcomes = self._cohort()
        repeated = (
            records[0],
            ForecastRecord(
                quote_key=records[0].quote_key,
                probability=records[1].probability,
                model_id=records[1].model_id,
                model_version=records[1].model_version,
                strategy_version=records[1].strategy_version,
                model_training_cutoff_ts=records[1].model_training_cutoff_ts,
                input_cutoff_ts=records[1].input_cutoff_ts,
                generated_at=records[1].generated_at,
                uncertainty=records[1].uncertainty,
                evidence_hashes=records[1].evidence_hashes,
                market_snapshot_hash=records[1].market_snapshot_hash,
                provenance=records[1].provenance,
                forecast_id=records[1].forecast_id,
            ),
        )
        with self.assertRaisesRegex(ValueError, "repeated quote_key"):
            self._evaluate(
                repeated,
                outcomes[:2],
                self._window(),
                bins=2,
            )

    def test_iid_diagnostic_rejects_distinct_quotes_from_same_event_cluster(self):
        records, outcomes = self._cohort()
        same_event = replace(
            records[1],
            quote_key="event-f-1|total|selection-f-2",
            probability=Decimal("0.9"),
        )

        with self.assertRaisesRegex(ValueError, "repeated canonical event cluster"):
            self._evaluate(
                (records[0], same_event),
                outcomes[:2],
                self._window(),
                bins=2,
            )

    def test_iid_screen_rejects_twelve_aliases_and_accepts_twelve_distinct_clusters(self):
        records = tuple(
            self._record(
                f"cluster-{index}",
                "0.2" if index % 2 == 0 else "0.8",
            )
            for index in range(12)
        )
        outcomes = tuple(
            ForecastOutcomeFact(
                record.forecast_id,
                index % 2,
                "2026-02-20T14:00:00+00:00",
            )
            for index, record in enumerate(records)
        )

        distinct = self._evaluate(
            records,
            outcomes,
            self._window(),
            bins=4,
        )
        self.assertEqual(distinct.count, 12)
        self.assertEqual(distinct.raw_sample_count, 12)
        self.assertEqual(distinct.effective_sample_count, 12)

        aliased = tuple(
            replace(
                record,
                quote_key=f"shared-event|market-{index}|selection-{index}",
            )
            for index, record in enumerate(records)
        )
        with self.assertRaisesRegex(ValueError, "repeated canonical event cluster"):
            self._evaluate(
                aliased,
                outcomes,
                self._window(),
                bins=4,
            )

    def test_iid_screen_supports_sport_v2_and_clusters_same_event(self):
        records, outcomes = self._cohort()
        first = replace(
            records[0],
            quote_key=TicketLeg(
                "sport-event-1",
                "winner",
                "selection-a",
                Decimal("2"),
                sport="soccer",
            ).quote_key,
        )
        second = replace(
            records[1],
            quote_key=TicketLeg(
                "sport-event-2",
                "winner",
                "selection-b",
                Decimal("2"),
                sport="soccer",
            ).quote_key,
        )

        report = self._evaluate(
            (first, second),
            outcomes[:2],
            self._window(),
            bins=2,
        )
        self.assertEqual(report.count, 2)

        same_event = replace(
            second,
            quote_key=TicketLeg(
                "sport-event-1",
                "total",
                "selection-c",
                Decimal("2"),
                sport="soccer",
            ).quote_key,
        )
        with self.assertRaisesRegex(ValueError, "repeated canonical event cluster"):
            self._evaluate(
                (first, same_event),
                outcomes[:2],
                self._window(),
                bins=2,
            )

    def test_iid_diagnostic_rejects_shared_market_snapshot(self):
        records, outcomes = self._cohort()
        shared_snapshot = replace(
            records[1],
            market_snapshot_hash=records[0].market_snapshot_hash,
        )

        with self.assertRaisesRegex(ValueError, "repeated market_snapshot_hash"):
            self._evaluate(
                (records[0], shared_snapshot),
                outcomes[:2],
                self._window(),
                bins=2,
            )

    def test_iid_diagnostic_rejects_shared_source_evidence(self):
        records, outcomes = self._cohort()
        shared_evidence = replace(
            records[1],
            evidence_hashes=records[0].evidence_hashes,
        )

        with self.assertRaisesRegex(ValueError, "shared source evidence"):
            self._evaluate(
                (records[0], shared_evidence),
                outcomes[:2],
                self._window(),
                bins=2,
            )

    def test_iid_diagnostic_rejects_noncanonical_quote_alias(self):
        records, outcomes = self._cohort()
        aliased = replace(records[1], quote_key="event-f-2-alias")

        with self.assertRaisesRegex(ValueError, "canonical quote_key identity"):
            self._evaluate(
                (records[0], aliased),
                outcomes[:2],
                self._window(),
                bins=2,
            )

    def test_selective_coverage_binds_full_eligible_denominator(self):
        records, outcomes = self._cohort()
        selected_ids = ("f-1", "f-2")
        full_manifest = self._manifest(records, selected_ids=selected_ids)
        selective = self._evaluate(
            records,
            outcomes,
            self._window(),
            bins=5,
            population_manifest=full_manifest,
        )

        narrow_records = records[:2]
        narrow_outcomes = outcomes[:2]
        narrow = self._evaluate(
            narrow_records,
            narrow_outcomes,
            self._window(),
            bins=5,
            population_manifest=self._manifest(narrow_records),
        )

        self.assertEqual(selective.count, 2)
        self.assertEqual(selective.coverage.eligible_count, 4)
        self.assertEqual(selective.coverage.selected_count, 2)
        self.assertEqual(selective.coverage.resolved_count, 2)
        self.assertEqual(selective.coverage.pending_outcome_count, 0)
        self.assertEqual(selective.coverage.missing_forecast_count, 0)
        self.assertEqual(selective.coverage.selection_coverage, 0.5)
        self.assertEqual(narrow.coverage.selection_coverage, 1.0)
        self.assertEqual(selective.cohort_sha256, narrow.cohort_sha256)
        self.assertNotEqual(
            selective.population_manifest_sha256,
            narrow.population_manifest_sha256,
        )
        self.assertNotEqual(selective.config_sha256, narrow.config_sha256)
        self.assertNotEqual(selective.report_sha256, narrow.report_sha256)

    def test_population_manifest_rejects_missing_and_out_of_universe_records(self):
        records, outcomes = self._cohort()
        full_manifest = self._manifest(records)
        with self.assertRaisesRegex(ValueError, "requires forecast records"):
            self._evaluate(
                records[:-1],
                outcomes[:-1],
                self._window(),
                bins=5,
                population_manifest=full_manifest,
            )

        narrow_manifest = self._manifest(records[:-1])
        with self.assertRaisesRegex(ValueError, "outside declared population"):
            self._evaluate(
                records,
                outcomes,
                self._window(),
                bins=5,
                population_manifest=narrow_manifest,
            )

    def test_coverage_projection_keeps_pending_and_empty_selection_explicit(self):
        records, outcomes = self._cohort()
        manifest = self._manifest(records, selected_ids=("f-1", "f-2"))
        coverage = evaluate_calibration_coverage(
            records,
            outcomes[:1],
            self._window(),
            population_manifest=manifest,
        )
        self.assertEqual(coverage.eligible_count, 4)
        self.assertEqual(coverage.selected_count, 2)
        self.assertEqual(coverage.forecasted_count, 4)
        self.assertEqual(coverage.resolved_count, 1)
        self.assertEqual(coverage.pending_outcome_count, 1)
        self.assertEqual(coverage.missing_forecast_count, 0)
        self.assertEqual(coverage.selection_coverage, 0.5)
        self.assertEqual(coverage.resolution_coverage, 0.5)

        empty_manifest = self._manifest(records, selected_ids=())
        empty = evaluate_calibration_coverage(
            records,
            (),
            self._window(),
            population_manifest=empty_manifest,
        )
        self.assertEqual(empty.selected_count, 0)
        self.assertEqual(empty.selection_coverage, 0.0)
        self.assertEqual(empty.resolution_coverage, 0.0)
        with self.assertRaisesRegex(ValueError, "selected no forecasts"):
            self._evaluate(
                records,
                (),
                self._window(),
                bins=5,
                population_manifest=empty_manifest,
            )

    def test_population_manifest_hashes_exact_forecast_bytes_and_is_order_stable(self):
        records, outcomes = self._cohort()
        forward_manifest = self._manifest(records, selected_ids=("f-1", "f-3"))
        reverse_manifest = self._manifest(
            tuple(reversed(records)),
            selected_ids=("f-3", "f-1"),
        )
        self.assertEqual(
            forward_manifest.manifest_sha256,
            reverse_manifest.manifest_sha256,
        )

        tampered_entry = CalibrationPopulationEntry(
            forecast_id=records[0].forecast_id,
            forecast_sha256="e" * 64,
            selected=True,
        )
        tampered_manifest = CalibrationPopulationManifest(
            window_id=self._window().window_id,
            source_snapshot_sha256="c" * 64,
            selection_policy_sha256="d" * 64,
            entries=(tampered_entry,) + forward_manifest.entries[1:],
        )
        with self.assertRaisesRegex(ValueError, "forecast hash mismatch"):
            self._evaluate(
                records,
                outcomes,
                self._window(),
                bins=5,
                population_manifest=tampered_manifest,
            )

    def test_population_manifest_duplicate_identity_fails_closed(self):
        record = self._record("dup-pop", "0.5")
        entry = CalibrationPopulationEntry(
            forecast_id=record.forecast_id,
            forecast_sha256=record.canonical_hash,
            selected=True,
        )
        with self.assertRaisesRegex(ValueError, "duplicate forecast_id"):
            CalibrationPopulationManifest(
                window_id=self._window().window_id,
                source_snapshot_sha256="c" * 64,
                selection_policy_sha256="d" * 64,
                entries=(entry, entry),
            )

    def test_causal_and_training_boundary_checks_are_preserved(self):
        records, outcomes = self._cohort()
        leaked = (
            self._record(
                "f-1",
                "0.90",
                training_cutoff="2026-02-02T00:00:00+00:00",
            ),
        ) + records[1:]
        with self.assertRaisesRegex(ValueError, "holdout/walk-forward leakage"):
            self._evaluate(
                leaked,
                outcomes,
                self._window(),
                bins=5,
            )

        early_reveal = (
            ForecastOutcomeFact("f-1", 1, "2026-02-10T11:00:00+00:00"),
        ) + outcomes[1:]
        with self.assertRaisesRegex(ValueError, "outcome reveal must be after forecast generation"):
            self._evaluate(
                records,
                early_reveal,
                self._window(),
                bins=5,
            )


if __name__ == "__main__":
    unittest.main()
