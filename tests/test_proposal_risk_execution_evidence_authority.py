from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.proposal_risk_execution_evidence_authority as evidence_authority
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.proposal_risk_target_authority import issue_product_proposal_risk_target
from autosport.proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
    issue_product_proposal_risk_evaluation_precommit,
)
from autosport.risk import ProposedTicketRiskContext
from autosport.risk_membership_publication import publish_fixed_n_membership_structure
from autosport.risk_randomization_precommit import issue_risk_randomization_precommit
from autosport.risk_sampling_membership import inspect_fixed_n_risk_membership_structure
from autosport.run_registry import RunRegistry
from autosport.scientific_registry import (
    DatasetSnapshot,
    ResearchProtocol,
    ScientificRegistry,
)
from autosport.strategy_experiment import ScientificProtocolBinding
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


def _canonical_precommit(
    test: unittest.TestCase,
    *,
    threshold: Decimal = Decimal("10"),
) -> ProductProposalRiskEvaluationPrecommit:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    root = Path(temp.name).resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    authority_root = root / "machine-authority"
    env = patch.dict(
        os.environ,
        {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(authority_root)},
    )
    env.start()
    test.addCleanup(env.stop)

    goal = EconomicGoalContract(
        goal_id="goal-proposal-execution-evidence",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("0.10"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_event_concentration_fraction=Decimal("1"),
        max_market_concentration_fraction=Decimal("1"),
        max_provider_concentration_fraction=Decimal("1"),
        max_sport_concentration_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("0.01"),
        max_execution_slippage_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("3600"),
        minimum_data_quality=Decimal("0"),
        max_concurrent_positions=10,
        max_parlay_legs=1,
    )
    EconomicGoalStore(workspace).initialize_owner(goal)
    PaperBook(Decimal("1000")).save(workspace / "paper_book.json")

    decision_ts = "2026-09-18T13:20:00+00:00"
    quote_ts = "2026-09-18T13:19:59+00:00"
    contexts = []
    for suffix in ("a", "b"):
        leg = TicketLeg(
            event_id=f"event-{suffix}",
            market_id=f"market-{suffix}",
            selection_id=f"selection-{suffix}",
            locked_odds=Decimal("2"),
            sport="soccer",
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=Decimal("2"),
            observed_ts=quote_ts,
            source_id=f"provider-{suffix}",
            sequence=1,
            source_ts=quote_ts,
            ingest_ts=quote_ts,
            metadata={},
            sport="soccer",
        )
        contexts.append(
            ProposedTicketRiskContext(
                legs=(leg,),
                quotes=(quote,),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                proposal_ts=decision_ts,
            )
        )
    target = issue_product_proposal_risk_target(
        workspace,
        signal_strengths=(Decimal("1"), Decimal("0.8")),
        contexts=tuple(contexts),
    )

    protocol_id = "proposal-risk-execution-fixed-n-v1"
    dataset_id = "proposal-risk-execution-dataset-v1"
    dataset_manifest_sha = "3" * 64
    frame_sha = "4" * 64
    capital_sha = "5" * 64
    stake_sha = "6" * 64
    horizon_sha = "7" * 64
    members = ("member-a", "member-b")
    cutoff = "2026-09-01T00:00:00+00:00"
    dataset_available = "2026-09-02T00:00:00+00:00"
    frozen_at = "2026-09-02T12:00:00+00:00"
    protocol_available = "2026-09-02T12:05:00+00:00"
    outcome_reveal_after = "2026-09-10T00:00:00+00:00"

    threshold_text = format(threshold, "f")
    design = json.dumps(
        {
            "kind": "autosport-risk-fixed-n-run-membership-v2",
            "dataset_snapshot_id": dataset_id,
            "planned_run_ids": list(members),
            "planned_n": len(members),
            "sampling_frame_sha256": frame_sha,
            "risk_method": "CLOPPER_PEARSON_ONE_SIDED",
            "dependence_qualification": "SEPARATE_REQUIRED",
            "confidence_level": "0.95",
            "ruin_threshold": threshold_text,
            "risk_target_scope": "FROZEN_STAKE_POLICY",
            "initial_capital_state_sha256": capital_sha,
            "stake_policy_sha256": stake_sha,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    binding = ScientificProtocolBinding(
        research_protocol_id=protocol_id,
        research_question_id="proposal-risk-execution-question",
        research_question_sha256="c" * 64,
        hypothesis_id="proposal-risk-execution-hypothesis",
        hypothesis_sha256="d" * 64,
        inclusion_criteria="exact frozen run cohort",
        exclusion_criteria="no post-freeze cohort edits",
        lawful_source_requirements="canonical product evidence only",
        causal_cutoff=cutoff,
        evaluation_design=design,
        feature_set_version="proposal-risk-execution-v1",
        uncertainty_method="one-sided exact Clopper-Pearson",
        multiple_comparison_control="separate familywise authority required",
        robustness_checks=("restart re-resolution",),
        random_seed_policy="product randomization precommit",
        stopping_rule="fixed N; no early stopping",
        promotion_rule="risk evidence only; no direct promotion",
        expected_artifacts=("target-specific risk-path observations",),
        code_config_sha256="e" * 64,
        frozen_at_utc=frozen_at,
    )

    RunRegistry.initialize_pristine(workspace / "run_registry.json")
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific_registry.json"
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=dataset_id,
            manifest_sha256=dataset_manifest_sha,
            source_identity="canonical-proposal-risk-run-cohort",
            license_identity="internal-product-evidence",
            causal_cutoff=cutoff,
            available_at_utc=dataset_available,
            outcome_reveal_after=outcome_reveal_after,
        )
    )
    registry.append(
        ResearchProtocol(
            binding=binding,
            source_sha256="f" * 64,
            environment_sha256="1" * 64,
            dataset_manifest_sha256=dataset_manifest_sha,
            available_at_utc=protocol_available,
        )
    )
    membership = inspect_fixed_n_risk_membership_structure(
        registry.path,
        research_protocol_id=protocol_id,
        dataset_snapshot_id=dataset_id,
    )
    publish_fixed_n_membership_structure(
        registry.path,
        workspace=workspace,
        research_protocol_id=protocol_id,
        dataset_snapshot_id=dataset_id,
        authority_root=authority_root,
    )
    issued_randomization = issue_risk_randomization_precommit(
        registry.path,
        workspace=workspace,
        research_protocol_id=protocol_id,
        dataset_snapshot_id=dataset_id,
        experiment_id="proposal-risk-execution-iid-v1",
        authority_root=authority_root,
    )
    manifest = json.dumps(
        {
            "kind": "autosport-risk-iid-resample-with-replacement-v1",
            "experiment_id": "proposal-risk-execution-iid-v1",
            "membership_design_sha256": membership.design_sha256,
            "membership_protocol_record_sha256": membership.protocol_record_sha256,
            "membership_dataset_record_sha256": membership.dataset_record_sha256,
            "membership_causal_cutoff": membership.causal_cutoff,
            "membership_precommitted_at": membership.precommitted_at,
            "membership_outcome_reveal_after": membership.outcome_reveal_after,
            "research_protocol_id": membership.research_protocol_id,
            "protocol_sha256": membership.protocol_sha256,
            "dataset_snapshot_id": membership.dataset_snapshot_id,
            "dataset_manifest_sha256": membership.dataset_manifest_sha256,
            "sampling_frame_sha256": membership.sampling_frame_sha256,
            "initial_capital_state_sha256": capital_sha,
            "stake_policy_sha256": stake_sha,
            "horizon_sha256": horizon_sha,
            "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
            "with_replacement": True,
            "rng_algorithm": "PCG64",
            "rng_version": "numpy-compatible-contract-v1",
            "randomization_root_sha256": issued_randomization.randomization_root_sha256,
            "planned_n": len(members),
            "planned_member_ids": list(members),
            "stopping_rule": "FIXED_N_NO_EARLY_STOP",
            "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
            "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result = issue_product_proposal_risk_evaluation_precommit(
        workspace,
        target_sha256=target.target_sha256,
        membership=membership,
        registry_path=registry.path,
        sampling_manifest_json=manifest,
        authority_root=authority_root,
    )
    if not result.binding_identity_proven:
        raise AssertionError("canonical proposal risk precommit fixture is not authoritative")
    return result

def _row_impl(
    precommit: ProductProposalRiskEvaluationPrecommit,
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
    def setUp(self) -> None:
        self.precommit = self._precommit()

    def _precommit(
        self,
        *,
        threshold: Decimal = Decimal("10"),
    ) -> ProductProposalRiskEvaluationPrecommit:
        self.precommit = _canonical_precommit(self, threshold=threshold)
        return self.precommit

    def _row(self, member_id: str, **kwargs: object) -> CounterfactualMemberExecutionEvidence:
        return _row_impl(self.precommit, member_id, **kwargs)
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
        precommit = self._precommit()
        result = derive_product_proposal_risk_execution_evidence(
            precommit,
            (
                self._row("member-a", source="source-a"),
                self._row("member-b", source="source-b"),
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
        precommit = self._precommit(threshold=Decimal("10"))
        ruined = self._row(
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
            (ruined, self._row("member-b", source="source-b")),
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
        precommit = self._precommit(threshold=Decimal("99"))
        result = derive_product_proposal_risk_execution_evidence(
            precommit,
            (
                self._row("member-a", source="source-a"),
                self._row("member-b", source="source-b"),
            ),
            evaluated_at="2026-10-04T10:02:00+00:00",
        )
        self.assertTrue(result.statistical_bound_computed)
        self.assertFalse(result.proposal_target_risk_qualified)
        self.assertFalse(result.grants_risk_approval_authority)

    def test_estimator_dispatch_substitution_is_rejected(self) -> None:
        precommit = self._precommit()
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
                        self._row("member-a", source="source-a"),
                        self._row("member-b", source="source-b"),
                    ),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            evidence_authority.clopper_pearson_upper_bound = original

    def test_member_order_and_fixed_n_count_are_exact(self) -> None:
        precommit = self._precommit()
        a = self._row("member-a", source="source-a")
        b = self._row("member-b", source="source-b")
        for rows in ((a,), (b, a)):
            with self.subTest(rows=tuple(row.member_id for row in rows)):
                with self.assertRaises(ProductProposalRiskExecutionEvidenceError):
                    derive_product_proposal_risk_execution_evidence(
                        precommit,
                        rows,
                        evaluated_at="2026-10-04T10:02:00+00:00",
                    )

    def test_duplicate_source_evidence_is_rejected(self) -> None:
        precommit = self._precommit()
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "distinct source evidence",
        ):
            derive_product_proposal_risk_execution_evidence(
                precommit,
                (
                    self._row("member-a", source="same-source"),
                    self._row("member-b", source="same-source"),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )

    def test_binding_target_vector_and_stakes_cannot_change_afterself._precommit(self) -> None:
        precommit = self._precommit()
        bad_rows = (
            self._row("member-a", source="a", binding_sha256=_sha("other-binding")),
            self._row("member-a", source="a", target_sha256=_sha("other-target")),
            self._row(
                "member-a",
                source="a",
                candidate_vector_sha256=_sha("other-vector"),
            ),
            self._row(
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
                        (bad, self._row("member-b", source="b")),
                        evaluated_at="2026-10-04T10:02:00+00:00",
                    )

    def test_pre_reveal_or_future_member_evidence_is_rejected(self) -> None:
        precommit = self._precommit()
        cases = (
            self._row(
                "member-a",
                source="a",
                observed_at="2026-10-04T09:59:59+00:00",
            ),
            self._row(
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
                        (bad, self._row("member-b", source="b")),
                        evaluated_at="2026-10-04T10:02:00+00:00",
                    )

    def test_evaluation_itself_cannot_predate_reveal_boundary(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "cannot precede",
        ):
            derive_product_proposal_risk_execution_evidence(
                self._precommit(),
                (
                    self._row("member-a", source="a"),
                    self._row("member-b", source="b"),
                ),
                evaluated_at="2026-10-04T09:59:59+00:00",
            )

    def test_member_economics_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "net_pnl must equal",
        ):
            self._row("member-a", source="a", net=Decimal("4"))
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "terminal_equity must equal",
        ):
            self._row("member-a", source="a", terminal=Decimal("104"))
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "costs must be non-negative",
        ):
            self._row(
                "member-a",
                source="a",
                costs=Decimal("-1"),
                net=Decimal("7"),
                terminal=Decimal("107"),
            )

    def test_mixed_execution_engine_or_starting_equity_is_rejected(self) -> None:
        precommit = self._precommit()
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "one exact execution engine",
        ):
            derive_product_proposal_risk_execution_evidence(
                precommit,
                (
                    self._row("member-a", source="a", engine="engine-a"),
                    self._row("member-b", source="b", engine="engine-b"),
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
                    self._row("member-a", source="a"),
                    self._row(
                        "member-b",
                        source="b",
                        starting=Decimal("101"),
                        terminal=Decimal("106"),
                    ),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )

    def test_evidence_digest_is_stable_and_seals_canonical_evaluator(self) -> None:
        precommit = self._precommit()
        rows = (
            self._row("member-a", source="source-a"),
            self._row("member-b", source="source-b"),
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
                    self._row("member-a", source="a"),
                    self._row("member-b", source="b"),
                ),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
