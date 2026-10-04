from __future__ import annotations

import hashlib
import unittest
from decimal import Decimal

import autosport.proposal_risk_evaluation_precommit_authority as precommit_authority
import autosport.proposal_risk_execution_evidence_authority as evidence_authority
from autosport.proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from autosport.proposal_risk_execution_evidence_authority import (
    CounterfactualMemberExecutionEvidence,
    ProductProposalRiskExecutionEvidence,
    ProductProposalRiskExecutionEvidenceError,
    derive_product_proposal_risk_execution_evidence,
)
from autosport.risk_of_ruin_evaluator import (
    clopper_pearson_upper_bound,
    evaluator_source_sha256,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _precommit(
    *, threshold: Decimal = Decimal("10")
) -> ProductProposalRiskEvaluationPrecommit:
    value = object.__new__(ProductProposalRiskEvaluationPrecommit)
    attrs = {
        "workspace_instance_id": "workspace-test",
        "binding_sha256": _sha("binding"),
        "target_sha256": _sha("target"),
        "candidate_vector_sha256": _sha("candidate-vector"),
        "evaluated_stakes": (Decimal("3.00"), Decimal("2.00")),
        "planned_member_ids": ("member-a", "member-b"),
        "membership_outcome_reveal_after": "2026-10-04T10:00:00+00:00",
        "confidence_level": Decimal("0.95"),
        "ruin_threshold": threshold,
        "proposal_evaluation_scope": (
            "EXACT_PROPOSAL_TARGET_FIXED_STAKE_VECTOR_COUNTERFACTUAL_V1"
        ),
    }
    for name, item in attrs.items():
        object.__setattr__(value, name, item)
    precommit_authority._BIND_IDENTITY(value)
    return value


def _row(
    member_id: str,
    *,
    source: str,
    binding_sha256: str | None = None,
    target_sha256: str | None = None,
    candidate_vector_sha256: str | None = None,
    executed_stakes: tuple[Decimal, ...] | None = None,
    engine: str = "engine",
    observed_at: str = "2026-10-04T10:01:00+00:00",
    starting: Decimal = Decimal("100"),
    minimum: Decimal = Decimal("80"),
    terminal: Decimal = Decimal("105"),
    gross: Decimal = Decimal("6"),
    costs: Decimal = Decimal("1"),
    net: Decimal = Decimal("5"),
) -> CounterfactualMemberExecutionEvidence:
    precommit = _precommit()
    return CounterfactualMemberExecutionEvidence(
        member_id=member_id,
        binding_sha256=binding_sha256 or precommit.binding_sha256,
        target_sha256=target_sha256 or precommit.target_sha256,
        candidate_vector_sha256=(
            candidate_vector_sha256 or precommit.candidate_vector_sha256
        ),
        executed_stakes=executed_stakes or precommit.evaluated_stakes,
        execution_engine_sha256=_sha(engine),
        source_sha256=_sha(source),
        observed_at=observed_at,
        starting_equity=starting,
        minimum_equity=minimum,
        terminal_equity=terminal,
        gross_pnl=gross,
        costs=costs,
        net_pnl=net,
    )


class ProductProposalRiskExecutionEvidenceTests(unittest.TestCase):
    def test_direct_product_result_construction_is_closed(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskExecutionEvidence()

    def test_object_new_cannot_mint_positive_authority(self) -> None:
        forged = object.__new__(ProductProposalRiskExecutionEvidence)
        self.assertFalse(forged.execution_evidence_identity_proven)
        self.assertFalse(forged.fixed_n_cohort_complete)
        self.assertFalse(forged.statistical_bound_computed)
        self.assertFalse(forged.product_execution_provenance_proven)
        self.assertFalse(forged.proposal_target_counterfactual_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.proposal_target_risk_qualified)
        self.assertFalse(forged.grants_risk_approval_authority)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_broker_execution_authority)
        self.assertFalse(forged.grants_real_money_authority)
        self.assertFalse(forged.grants_state_mutation_authority)

    def test_complete_assertion_cohort_computes_canonical_bound_but_no_execution_authority(self) -> None:
        precommit = _precommit()
        result = derive_product_proposal_risk_execution_evidence(
            precommit,
            (
                _row("member-a", source="source-a"),
                _row("member-b", source="source-b"),
            ),
            evaluated_at="2026-10-04T10:02:00+00:00",
        )

        self.assertTrue(result.execution_evidence_identity_proven)
        self.assertTrue(result.fixed_n_cohort_complete)
        self.assertTrue(result.statistical_bound_computed)
        self.assertFalse(result.product_execution_provenance_proven)
        self.assertFalse(result.proposal_target_counterfactual_execution_proven)
        self.assertFalse(result.risk_upper_bound_for_target)
        self.assertFalse(result.proposal_target_risk_qualified)
        self.assertEqual(result.sample_size, 2)
        self.assertEqual(result.ruin_count, 0)
        self.assertEqual(result.ruin_observations, (False, False))
        self.assertEqual(
            result.ruin_probability_upper_bound,
            clopper_pearson_upper_bound(
                ruin_count=0,
                independent_units=2,
                confidence_level=Decimal("0.95"),
            ),
        )
        self.assertEqual(
            result.bound_method,
            "CLOPPER_PEARSON_EXACT_ONE_SIDED_BERNOULLI_V1",
        )
        self.assertEqual(result.evaluator_source_sha256, evaluator_source_sha256())
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_ticket_authority)
        self.assertFalse(result.grants_broker_execution_authority)
        self.assertFalse(result.grants_real_money_authority)
        self.assertFalse(result.grants_state_mutation_authority)

    def test_ruin_threshold_is_equity_boundary_not_probability_cutoff(self) -> None:
        precommit = _precommit(threshold=Decimal("10"))
        ruined = _row(
            "member-a",
            source="source-a",
            minimum=Decimal("5"),
            terminal=Decimal("95"),
            gross=Decimal("-4"),
            costs=Decimal("1"),
            net=Decimal("-5"),
        )
        result = derive_product_proposal_risk_execution_evidence(
            precommit,
            (ruined, _row("member-b", source="source-b")),
            evaluated_at="2026-10-04T10:02:00+00:00",
        )
        self.assertEqual(result.ruin_observations, (True, False))
        self.assertEqual(result.ruin_count, 1)
        self.assertEqual(
            result.ruin_probability_upper_bound,
            clopper_pearson_upper_bound(
                ruin_count=1,
                independent_units=2,
                confidence_level=Decimal("0.95"),
            ),
        )
        self.assertFalse(result.proposal_target_risk_qualified)

    def test_large_equity_threshold_cannot_accidentally_qualify_probability(self) -> None:
        precommit = _precommit(threshold=Decimal("99"))
        result = derive_product_proposal_risk_execution_evidence(
            precommit,
            (
                _row("member-a", source="source-a"),
                _row("member-b", source="source-b"),
            ),
            evaluated_at="2026-10-04T10:02:00+00:00",
        )
        self.assertTrue(result.statistical_bound_computed)
        self.assertFalse(result.proposal_target_risk_qualified)
        self.assertFalse(result.grants_risk_approval_authority)

    def test_estimator_dispatch_substitution_is_rejected(self) -> None:
        precommit = _precommit()
        original = evidence_authority.clopper_pearson_upper_bound
        try:
            evidence_authority.clopper_pearson_upper_bound = lambda **_: Decimal("0")
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "estimator dispatch changed",
            ):
                derive_product_proposal_risk_execution_evidence(
                    precommit,
                    (
                        _row("member-a", source="source-a"),
                        _row("member-b", source="source-b"),
                    ),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            evidence_authority.clopper_pearson_upper_bound = original

    def test_member_order_and_fixed_n_count_are_exact(self) -> None:
        precommit = _precommit()
        a = _row("member-a", source="source-a")
        b = _row("member-b", source="source-b")
        for rows in ((a,), (b, a)):
            with self.subTest(rows=tuple(row.member_id for row in rows)):
                with self.assertRaises(ProductProposalRiskExecutionEvidenceError):
                    derive_product_proposal_risk_execution_evidence(
                        precommit,
                        rows,
                        evaluated_at="2026-10-04T10:02:00+00:00",
                    )

    def test_duplicate_source_evidence_is_rejected(self) -> None:
        precommit = _precommit()
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "distinct source evidence",
        ):
            derive_product_proposal_risk_execution_evidence(
                precommit,
                (
                    _row("member-a", source="same-source"),
                    _row("member-b", source="same-source"),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )

    def test_binding_target_vector_and_stakes_cannot_change_after_precommit(self) -> None:
        precommit = _precommit()
        bad_rows = (
            _row("member-a", source="a", binding_sha256=_sha("other-binding")),
            _row("member-a", source="a", target_sha256=_sha("other-target")),
            _row(
                "member-a",
                source="a",
                candidate_vector_sha256=_sha("other-vector"),
            ),
            _row(
                "member-a",
                source="a",
                executed_stakes=(Decimal("4"), Decimal("1")),
            ),
        )
        for bad in bad_rows:
            with self.subTest(bad=bad):
                with self.assertRaises(ProductProposalRiskExecutionEvidenceError):
                    derive_product_proposal_risk_execution_evidence(
                        precommit,
                        (bad, _row("member-b", source="b")),
                        evaluated_at="2026-10-04T10:02:00+00:00",
                    )

    def test_pre_reveal_or_future_member_evidence_is_rejected(self) -> None:
        precommit = _precommit()
        cases = (
            _row(
                "member-a",
                source="a",
                observed_at="2026-10-04T09:59:59+00:00",
            ),
            _row(
                "member-a",
                source="a",
                observed_at="2026-10-04T10:03:00+00:00",
            ),
        )
        for bad in cases:
            with self.subTest(observed_at=bad.observed_at):
                with self.assertRaises(ProductProposalRiskExecutionEvidenceError):
                    derive_product_proposal_risk_execution_evidence(
                        precommit,
                        (bad, _row("member-b", source="b")),
                        evaluated_at="2026-10-04T10:02:00+00:00",
                    )

    def test_evaluation_itself_cannot_predate_reveal_boundary(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "cannot precede",
        ):
            derive_product_proposal_risk_execution_evidence(
                _precommit(),
                (
                    _row("member-a", source="a"),
                    _row("member-b", source="b"),
                ),
                evaluated_at="2026-10-04T09:59:59+00:00",
            )

    def test_member_economics_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "net_pnl must equal",
        ):
            _row("member-a", source="a", net=Decimal("4"))
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "terminal_equity must equal",
        ):
            _row("member-a", source="a", terminal=Decimal("104"))
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "costs must be non-negative",
        ):
            _row(
                "member-a",
                source="a",
                costs=Decimal("-1"),
                net=Decimal("7"),
                terminal=Decimal("107"),
            )

    def test_mixed_execution_engine_or_starting_equity_is_rejected(self) -> None:
        precommit = _precommit()
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "one exact execution engine",
        ):
            derive_product_proposal_risk_execution_evidence(
                precommit,
                (
                    _row("member-a", source="a", engine="engine-a"),
                    _row("member-b", source="b", engine="engine-b"),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "one exact starting equity",
        ):
            derive_product_proposal_risk_execution_evidence(
                precommit,
                (
                    _row("member-a", source="a"),
                    _row(
                        "member-b",
                        source="b",
                        starting=Decimal("101"),
                        terminal=Decimal("106"),
                    ),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )

    def test_evidence_digest_is_stable_and_seals_canonical_evaluator(self) -> None:
        precommit = _precommit()
        rows = (
            _row("member-a", source="source-a"),
            _row("member-b", source="source-b"),
        )
        first = derive_product_proposal_risk_execution_evidence(
            precommit,
            rows,
            evaluated_at="2026-10-04T10:02:00+00:00",
        )
        second = derive_product_proposal_risk_execution_evidence(
            precommit,
            rows,
            evaluated_at="2026-10-04T10:02:00+00:00",
        )
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(len(first.evidence_sha256), 64)
        self.assertEqual(first.evaluator_source_sha256, evaluator_source_sha256())

    def test_unproven_precommit_identity_is_rejected(self) -> None:
        forged = object.__new__(ProductProposalRiskEvaluationPrecommit)
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "identity is not product-proven",
        ):
            derive_product_proposal_risk_execution_evidence(
                forged,
                (
                    _row("member-a", source="a"),
                    _row("member-b", source="b"),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
