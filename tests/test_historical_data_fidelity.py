from __future__ import annotations

import unittest

from autosport.historical_data_fidelity import (
    CommonInformationProjection,
    DatasetFidelityEvidence,
    ExecutionEvidenceCapability,
    FidelityTransform,
    FidelityTransformKind,
    FidelityUseCase,
    HistoricalDataFidelity,
    qualify_comparison,
    qualify_use_case,
)


BASIC = HistoricalDataFidelity.BETFAIR_BASIC_1M_LTP
ADVANCED = HistoricalDataFidelity.BETFAIR_ADVANCED_1S_BEST_OFFERS
PRO = HistoricalDataFidelity.BETFAIR_PRO_TICK_FULL_LADDER


def evidence(
    dataset_id: str,
    fidelity: HistoricalDataFidelity,
    *,
    execution_capabilities=frozenset(),
) -> DatasetFidelityEvidence:
    return DatasetFidelityEvidence(
        provider="betfair",
        dataset_id=dataset_id,
        native_fidelity=fidelity,
        analysis_fidelity=fidelity,
        execution_capabilities=execution_capabilities,
    )


class HistoricalDataFidelityTests(unittest.TestCase):
    def test_basic_rejects_sub_minute_strategy(self) -> None:
        result = qualify_use_case(evidence("basic", BASIC), FidelityUseCase.SUB_MINUTE_MOVEMENT)
        self.assertFalse(result.qualified)
        self.assertIn("one_second_resolution", result.reasons[0])

    def test_advanced_allows_sub_minute_but_rejects_sub_second(self) -> None:
        current = evidence("advanced", ADVANCED)
        self.assertTrue(qualify_use_case(current, FidelityUseCase.SUB_MINUTE_MOVEMENT).qualified)
        self.assertFalse(qualify_use_case(current, FidelityUseCase.SUB_SECOND_ORDERING).qualified)

    def test_advanced_rejects_full_depth_liquidity(self) -> None:
        result = qualify_use_case(evidence("advanced", ADVANCED), FidelityUseCase.FULL_DEPTH_LIQUIDITY)
        self.assertFalse(result.qualified)
        self.assertIn("full_available_ladder", result.reasons[0])

    def test_pro_allows_tick_and_full_depth_market_analysis(self) -> None:
        current = evidence("pro", PRO)
        self.assertTrue(qualify_use_case(current, FidelityUseCase.SUB_SECOND_ORDERING).qualified)
        self.assertTrue(qualify_use_case(current, FidelityUseCase.FULL_DEPTH_LIQUIDITY).qualified)

    def test_pro_without_execution_receipts_cannot_prove_fill_quality(self) -> None:
        current = evidence("pro", PRO)
        self.assertTrue(qualify_use_case(current, FidelityUseCase.MARKET_PERFORMANCE).qualified)
        result = qualify_use_case(current, FidelityUseCase.EXECUTION_FILL_QUALITY)
        self.assertFalse(result.qualified)
        self.assertEqual(
            {item.value for item in result.missing_execution_capabilities},
            {"accepted_price", "order_receipt", "realized_fill"},
        )

    def test_execution_receipts_are_independent_of_market_data_tier(self) -> None:
        current = evidence(
            "basic-with-receipts",
            BASIC,
            execution_capabilities=frozenset(
                {
                    ExecutionEvidenceCapability.ORDER_RECEIPT,
                    ExecutionEvidenceCapability.ACCEPTED_PRICE,
                    ExecutionEvidenceCapability.REALIZED_FILL,
                }
            ),
        )
        self.assertTrue(qualify_use_case(current, FidelityUseCase.EXECUTION_FILL_QUALITY).qualified)
        self.assertFalse(qualify_use_case(current, FidelityUseCase.SUB_SECOND_ORDERING).qualified)

    def test_basic_interpolation_to_one_second_does_not_upgrade_truth(self) -> None:
        transform = FidelityTransform(
            kind=FidelityTransformKind.INTERPOLATE,
            source_fidelity=BASIC,
            output_fidelity=ADVANCED,
            transform_id="interp-basic-to-1s-v1",
            feature_set_id="features-interpolated-v1",
            input_cutoff_ts="2026-09-20T12:00:00Z",
        )
        current = DatasetFidelityEvidence(
            provider="betfair",
            dataset_id="basic-interpolated",
            native_fidelity=BASIC,
            analysis_fidelity=ADVANCED,
            transform=transform,
        )
        self.assertEqual(current.truth_fidelity, BASIC)
        self.assertTrue(current.interpolated)
        result = qualify_use_case(current, FidelityUseCase.SUB_MINUTE_MOVEMENT)
        self.assertFalse(result.qualified)
        self.assertIn("interpolation cannot upgrade", result.reasons[-1])

    def test_pro_downsample_requires_explicit_transform_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires explicit transform provenance"):
            DatasetFidelityEvidence(
                provider="betfair",
                dataset_id="pro-as-advanced",
                native_fidelity=PRO,
                analysis_fidelity=ADVANCED,
            )

    def test_pro_downsample_has_new_feature_identity_and_lower_truth(self) -> None:
        transform = FidelityTransform(
            kind=FidelityTransformKind.DOWNSAMPLE,
            source_fidelity=PRO,
            output_fidelity=ADVANCED,
            transform_id="downsample-pro-to-1s-v1",
            feature_set_id="features-common-1s-v3",
            input_cutoff_ts="2026-09-20T12:00:00+00:00",
        )
        current = DatasetFidelityEvidence(
            provider="betfair",
            dataset_id="pro-downsampled",
            native_fidelity=PRO,
            analysis_fidelity=ADVANCED,
            transform=transform,
        )
        self.assertEqual(current.truth_fidelity, ADVANCED)
        self.assertEqual(current.transform.feature_set_id, "features-common-1s-v3")
        self.assertEqual(current.transform.input_cutoff_ts, "2026-09-20T12:00:00+00:00")
        self.assertFalse(qualify_use_case(current, FidelityUseCase.FULL_DEPTH_LIQUIDITY).qualified)

    def test_cross_tier_comparison_fails_without_common_projection(self) -> None:
        result = qualify_comparison(evidence("basic", BASIC), evidence("pro", PRO))
        self.assertFalse(result.comparable)
        self.assertIn("common-information projection", result.reasons[0])

    def test_cross_tier_comparison_allows_explicit_lower_common_projection(self) -> None:
        left = evidence("basic", BASIC)
        right = evidence("pro", PRO)
        projection = CommonInformationProjection(
            projection_id="common-basic-v1",
            feature_set_id="features-basic-common-v1",
            target_fidelity=BASIC,
            source_dataset_ids=("basic", "pro"),
            input_cutoff_ts="2026-09-20T12:00:00Z",
        )
        result = qualify_comparison(left, right, projection=projection)
        self.assertTrue(result.comparable)
        self.assertEqual(result.comparison_fidelity, BASIC)
        self.assertEqual(result.projection_id, "common-basic-v1")

    def test_cross_tier_projection_cannot_exceed_lower_truth(self) -> None:
        left = evidence("basic", BASIC)
        right = evidence("pro", PRO)
        projection = CommonInformationProjection(
            projection_id="invalid-advanced-common-v1",
            feature_set_id="features-invalid-common-v1",
            target_fidelity=ADVANCED,
            source_dataset_ids=("basic", "pro"),
            input_cutoff_ts="2026-09-20T12:00:00Z",
        )
        result = qualify_comparison(left, right, projection=projection)
        self.assertFalse(result.comparable)
        self.assertIn("lower-fidelity", result.reasons[0])

    def test_projection_must_bind_exact_compared_dataset_ids(self) -> None:
        projection = CommonInformationProjection(
            projection_id="common-v1",
            feature_set_id="features-v1",
            target_fidelity=BASIC,
            source_dataset_ids=("basic", "other"),
            input_cutoff_ts="2026-09-20T12:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "exact compared datasets"):
            qualify_comparison(evidence("basic", BASIC), evidence("pro", PRO), projection=projection)

    def test_transform_rejects_naive_cutoff_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            FidelityTransform(
                kind=FidelityTransformKind.DOWNSAMPLE,
                source_fidelity=PRO,
                output_fidelity=ADVANCED,
                transform_id="downsample-v1",
                feature_set_id="features-v1",
                input_cutoff_ts="2026-09-20T12:00:00",
            )

    def test_downsample_direction_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "downsample must lower"):
            FidelityTransform(
                kind=FidelityTransformKind.DOWNSAMPLE,
                source_fidelity=BASIC,
                output_fidelity=ADVANCED,
                transform_id="bad-downsample",
                feature_set_id="features-v1",
                input_cutoff_ts="2026-09-20T12:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
