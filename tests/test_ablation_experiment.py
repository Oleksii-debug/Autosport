from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, getcontext
import unittest

from autosport.ablation_experiment import (
    AblationCellObservation,
    AblationCellSpec,
    AblationFactor,
    AblationMode,
    AblationProtocol,
    FactorIdentitySet,
    evaluate_ablation,
)


def _sha(character: str) -> str:
    return character * 64


class AblationExperimentTests(unittest.TestCase):
    @staticmethod
    def _baseline_factors() -> FactorIdentitySet:
        return FactorIdentitySet(
            data_sha256=_sha("1"),
            model_sha256=_sha("2"),
            threshold_selection_sha256=_sha("3"),
            sizing_sha256=_sha("4"),
            execution_sha256=_sha("5"),
        )

    def _one_factor_protocol(self) -> AblationProtocol:
        baseline_factors = self._baseline_factors()
        baseline = AblationCellSpec(
            "baseline",
            baseline_factors,
            (),
        )
        model = AblationCellSpec(
            "model-v2",
            replace(baseline_factors, model_sha256=_sha("6")),
            (AblationFactor.MODEL,),
        )
        return AblationProtocol(
            protocol_id="ablation-model-v1",
            mode=AblationMode.ONE_FACTOR,
            scientific_protocol_sha256=_sha("a"),
            case_population_sha256=_sha("b"),
            causal_cutoff="2026-09-21T08:00:00Z",
            primary_metric="net_profit",
            baseline=baseline,
            interventions=(model,),
            declared_factor_sets=((AblationFactor.MODEL,),),
        )

    @staticmethod
    def _observation(
        cell: AblationCellSpec,
        *,
        evidence: str,
        value: str,
        available_at: str = "2026-09-21T09:00:00Z",
    ) -> AblationCellObservation:
        return AblationCellObservation(
            cell_id=cell.cell_id,
            cell_spec_sha256=cell.spec_sha256,
            run_evidence_sha256=_sha(evidence),
            metric_value=Decimal(value),
            evidence_available_at=available_at,
        )

    def test_one_factor_report_identifies_only_exact_changed_factor(self) -> None:
        protocol = self._one_factor_protocol()
        baseline, model = protocol.baseline, protocol.interventions[0]

        report = evaluate_ablation(
            protocol,
            (
                self._observation(baseline, evidence="c", value="10.00"),
                self._observation(model, evidence="d", value="12.50"),
            ),
            evaluated_at="2026-09-21T10:00:00Z",
        )

        self.assertEqual(len(report.contrasts), 1)
        contrast = report.contrasts[0]
        self.assertEqual(contrast.identified_factor_set, (AblationFactor.MODEL,))
        self.assertEqual(contrast.delta, Decimal("2.50"))
        payload = report.to_dict()
        self.assertFalse(payload["truth"]["factor_contributions_additive"])
        self.assertFalse(payload["truth"]["active_strategy_mutation"])
        self.assertFalse(payload["truth"]["real_money_execution"])
        self.assertFalse(
            payload["contrasts"][0]["additive_per_factor_contribution_claim"]
        )

    def test_one_factor_rejects_cell_that_changes_two_factor_identities(self) -> None:
        baseline_factors = self._baseline_factors()
        baseline = AblationCellSpec("baseline", baseline_factors, ())
        bundled = AblationCellSpec(
            "bundled-change",
            replace(
                baseline_factors,
                model_sha256=_sha("6"),
                sizing_sha256=_sha("7"),
            ),
            (AblationFactor.MODEL, AblationFactor.SIZING),
        )

        with self.assertRaisesRegex(
            ValueError, "ONE_FACTOR intervention must change exactly one factor"
        ):
            AblationProtocol(
                protocol_id="invalid-one-factor",
                mode=AblationMode.ONE_FACTOR,
                scientific_protocol_sha256=_sha("a"),
                case_population_sha256=_sha("b"),
                causal_cutoff="2026-09-21T08:00:00Z",
                primary_metric="net_profit",
                baseline=baseline,
                interventions=(bundled,),
                declared_factor_sets=(
                    (AblationFactor.MODEL, AblationFactor.SIZING),
                ),
            )

    def test_declared_factor_set_must_match_actual_identity_delta(self) -> None:
        baseline_factors = self._baseline_factors()
        baseline = AblationCellSpec("baseline", baseline_factors, ())
        mislabeled = AblationCellSpec(
            "mislabeled",
            replace(baseline_factors, sizing_sha256=_sha("7")),
            (AblationFactor.MODEL,),
        )

        with self.assertRaisesRegex(
            ValueError, "declared changed factors do not match"
        ):
            AblationProtocol(
                protocol_id="mislabeled-ablation",
                mode=AblationMode.ONE_FACTOR,
                scientific_protocol_sha256=_sha("a"),
                case_population_sha256=_sha("b"),
                causal_cutoff="2026-09-21T08:00:00Z",
                primary_metric="roi",
                baseline=baseline,
                interventions=(mislabeled,),
                declared_factor_sets=((AblationFactor.MODEL,),),
            )

    def test_factorial_preserves_interaction_set_without_additive_claim(self) -> None:
        baseline_factors = self._baseline_factors()
        baseline = AblationCellSpec("baseline", baseline_factors, ())
        model = AblationCellSpec(
            "model",
            replace(baseline_factors, model_sha256=_sha("6")),
            (AblationFactor.MODEL,),
        )
        model_sizing = AblationCellSpec(
            "model-sizing",
            replace(
                baseline_factors,
                model_sha256=_sha("6"),
                sizing_sha256=_sha("7"),
            ),
            (AblationFactor.MODEL, AblationFactor.SIZING),
        )
        protocol = AblationProtocol(
            protocol_id="factorial-v1",
            mode=AblationMode.FACTORIAL,
            scientific_protocol_sha256=_sha("a"),
            case_population_sha256=_sha("b"),
            causal_cutoff="2026-09-21T08:00:00Z",
            primary_metric="net_profit",
            baseline=baseline,
            interventions=(model, model_sizing),
            declared_factor_sets=(
                (AblationFactor.MODEL,),
                (AblationFactor.MODEL, AblationFactor.SIZING),
            ),
        )

        report = evaluate_ablation(
            protocol,
            (
                self._observation(baseline, evidence="c", value="10"),
                self._observation(model, evidence="d", value="11"),
                self._observation(model_sizing, evidence="e", value="14"),
            ),
            evaluated_at="2026-09-21T10:00:00Z",
        )

        interaction = report.contrasts[1]
        self.assertEqual(
            interaction.identified_factor_set,
            (AblationFactor.MODEL, AblationFactor.SIZING),
        )
        self.assertEqual(interaction.delta, Decimal("4"))
        self.assertFalse(
            interaction.to_dict()["additive_per_factor_contribution_claim"]
        )
        self.assertNotIn("model_contribution", interaction.to_dict())
        self.assertNotIn("sizing_contribution", interaction.to_dict())

    def test_missing_preregistered_cell_fails_closed(self) -> None:
        protocol = self._one_factor_protocol()
        with self.assertRaisesRegex(
            ValueError, "observation matrix is incomplete or unexpected"
        ):
            evaluate_ablation(
                protocol,
                (
                    self._observation(
                        protocol.baseline,
                        evidence="c",
                        value="10",
                    ),
                ),
                evaluated_at="2026-09-21T10:00:00Z",
            )

    def test_rebound_cell_specification_fails_closed(self) -> None:
        protocol = self._one_factor_protocol()
        baseline, model = protocol.baseline, protocol.interventions[0]
        forged = AblationCellObservation(
            cell_id=model.cell_id,
            cell_spec_sha256=_sha("f"),
            run_evidence_sha256=_sha("d"),
            metric_value=Decimal("12"),
            evidence_available_at="2026-09-21T09:00:00Z",
        )

        with self.assertRaisesRegex(
            ValueError, "cell specification mismatch"
        ):
            evaluate_ablation(
                protocol,
                (
                    self._observation(baseline, evidence="c", value="10"),
                    forged,
                ),
                evaluated_at="2026-09-21T10:00:00Z",
            )

    def test_run_evidence_cannot_be_reused_across_cells(self) -> None:
        protocol = self._one_factor_protocol()
        baseline, model = protocol.baseline, protocol.interventions[0]

        with self.assertRaisesRegex(ValueError, "evidence cannot be reused"):
            evaluate_ablation(
                protocol,
                (
                    self._observation(baseline, evidence="c", value="10"),
                    self._observation(model, evidence="c", value="12"),
                ),
                evaluated_at="2026-09-21T10:00:00Z",
            )

    def test_future_evidence_cannot_leak_into_evaluation(self) -> None:
        protocol = self._one_factor_protocol()
        baseline, model = protocol.baseline, protocol.interventions[0]

        with self.assertRaisesRegex(ValueError, "not available at evaluation time"):
            evaluate_ablation(
                protocol,
                (
                    self._observation(baseline, evidence="c", value="10"),
                    self._observation(
                        model,
                        evidence="d",
                        value="12",
                        available_at="2026-09-21T10:00:01Z",
                    ),
                ),
                evaluated_at="2026-09-21T10:00:00Z",
            )

    def test_delta_is_independent_of_low_ambient_decimal_precision(self) -> None:
        protocol = self._one_factor_protocol()
        baseline, model = protocol.baseline, protocol.interventions[0]
        previous_precision = getcontext().prec
        try:
            getcontext().prec = 3
            report = evaluate_ablation(
                protocol,
                (
                    self._observation(
                        baseline,
                        evidence="c",
                        value="123456789.123456789",
                    ),
                    self._observation(
                        model,
                        evidence="d",
                        value="123456790.123456789",
                    ),
                ),
                evaluated_at="2026-09-21T10:00:00Z",
            )
        finally:
            getcontext().prec = previous_precision

        self.assertEqual(report.contrasts[0].delta, Decimal("1.000000000"))


if __name__ == "__main__":
    unittest.main()
