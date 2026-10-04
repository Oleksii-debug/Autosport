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
from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.proposal_target_terminal_population_authority import (
    ProductProposalTargetTerminalPopulation,
    issue_product_proposal_target_terminal_population,
)
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
    setattr(test, "_proposal_risk_workspace", workspace)
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
            sport="table_tennis",
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=Decimal("2"),
            observed_ts=quote_ts,
            source_id="betfair_exchange_historical",
            sequence=1,
            market_type=MarketType.WINNER,
            source_ts=quote_ts,
            ingest_ts=quote_ts,
            metadata={},
            sport="table_tennis",
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
    setattr(test, "_proposal_risk_contexts", tuple(contexts))
    target = issue_product_proposal_risk_target(
        workspace,
        signal_strengths=(Decimal("1"), Decimal("0.8")),
        contexts=tuple(contexts),
    )
    terminal_authorities = []
    for suffix in ("a", "b"):
        assessment = assess_betfair_historical_market_definition_authority(
            market_id=f"market-{suffix}",
            market_definition={
                "eventId": f"event-{suffix}",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [
                    {"id": f"other-{suffix}"},
                    {"id": f"selection-{suffix}"},
                ],
            },
            provider_publish_at="2026-09-18T13:19:57+00:00",
            observed_at="2026-09-18T13:19:58+00:00",
        )
        test.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        test.assertIsNotNone(assessment.authority)
        terminal_authorities.append(assessment.authority)
    terminal_population = issue_product_proposal_target_terminal_population(
        workspace,
        target_sha256=target.target_sha256,
        authorities=tuple(terminal_authorities),
    )
    setattr(test, "_proposal_terminal_population", terminal_population)

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
                observed_at="2026-09-09T23:59:59+00:00",
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
                evaluated_at="2026-09-09T23:59:59+00:00",
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



import autosport.proposal_risk_scenario_population_authority as scenario_population_authority
from autosport.monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from autosport.proposal_risk_scenario_population_authority import (
    CounterfactualScenarioMemberBinding,
    ProductProposalRiskScenarioPopulation,
    ProductProposalRiskScenarioPopulationError,
    issue_product_proposal_risk_scenario_population,
    resolve_product_proposal_risk_scenario_population,
)


class ProductProposalRiskScenarioPopulationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.assertIs(
            type(self.terminal_population),
            ProductProposalTargetTerminalPopulation,
        )

    def _members(
        self,
        *,
        scenario_a: str = "scenario-a",
        scenario_b: str = "scenario-b",
        mapping_a: str = "mapping-a",
        mapping_b: str = "mapping-b",
    ) -> tuple[CounterfactualScenarioMemberBinding, ...]:
        return (
            CounterfactualScenarioMemberBinding(
                member_id=self.precommit.planned_member_ids[0],
                scenario_id=scenario_a,
                mapping_sha256=_sha(mapping_a),
            ),
            CounterfactualScenarioMemberBinding(
                member_id=self.precommit.planned_member_ids[1],
                scenario_id=scenario_b,
                mapping_sha256=_sha(mapping_b),
            ),
        )

    def _issue(
        self,
        members: tuple[CounterfactualScenarioMemberBinding, ...] | None = None,
    ) -> ProductProposalRiskScenarioPopulation:
        return issue_product_proposal_risk_scenario_population(
            self.workspace,
            self.precommit,
            self.terminal_population,
            members or self._members(),
        )

    def _resolve(self) -> ProductProposalRiskScenarioPopulation:
        return resolve_product_proposal_risk_scenario_population(
            self.workspace,
            self.precommit,
            self.terminal_population,
        )

    def test_exact_population_is_durable_provider_bound_and_non_authorizing(self) -> None:
        issued = self._issue()

        self.assertTrue(issued.population_identity_proven)
        self.assertTrue(issued.fixed_n_member_mapping_complete)
        self.assertTrue(issued.provider_terminal_population_proven)
        self.assertEqual(issued.planned_member_ids, self.precommit.planned_member_ids)
        self.assertEqual(issued.member_scenario_ids, ("scenario-a", "scenario-b"))
        self.assertEqual(issued.evaluated_stakes, self.precommit.evaluated_stakes)
        self.assertEqual(
            issued.terminal_population_sha256,
            self.terminal_population.population_sha256,
        )
        self.assertEqual(
            issued.terminal_market_group_sha256s,
            self.terminal_population.market_group_sha256s,
        )
        self.assertEqual(
            issued.terminal_market_count,
            self.terminal_population.terminal_market_count,
        )
        self.assertEqual(
            issued.terminal_state_count,
            self.terminal_population.terminal_state_count,
        )
        self.assertIs(
            issued.terminal_space_exact,
            self.terminal_population.terminal_space_exact,
        )
        self.assertGreater(issued.bound_at, self.precommit.target_decision_ts)
        last_record = json.loads(
            (self.workspace / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )["record"]
        self.assertEqual(last_record["recorded_at"], issued.bound_at)
        self.assertTrue(last_record["payload"]["provider_terminal_population_proven"])
        self.assertEqual(
            last_record["payload"]["terminal_population_sha256"],
            self.terminal_population.population_sha256,
        )
        self.assertFalse(issued.product_scenario_source_provenance_proven)
        self.assertFalse(issued.terminal_mapping_proven)
        self.assertFalse(issued.scenario_execution_proven)
        self.assertFalse(issued.proposal_target_counterfactual_execution_proven)
        self.assertFalse(issued.risk_upper_bound_for_target)
        self.assertFalse(issued.grants_risk_approval_authority)
        self.assertFalse(issued.grants_ticket_authority)
        self.assertFalse(issued.grants_broker_execution_authority)
        self.assertFalse(issued.grants_real_money_authority)
        self.assertFalse(issued.grants_state_mutation_authority)

        resolved = self._resolve()
        self.assertEqual(resolved, issued)

    def test_direct_or_forged_result_cannot_mint_authority(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskScenarioPopulation()

        forged = object.__new__(ProductProposalRiskScenarioPopulation)
        self.assertFalse(forged.population_identity_proven)
        self.assertFalse(forged.fixed_n_member_mapping_complete)
        self.assertFalse(forged.provider_terminal_population_proven)
        self.assertFalse(forged.product_scenario_source_provenance_proven)
        self.assertFalse(forged.terminal_mapping_proven)
        self.assertFalse(forged.scenario_execution_proven)
        self.assertFalse(forged.proposal_target_counterfactual_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_risk_approval_authority)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_broker_execution_authority)
        self.assertFalse(forged.grants_real_money_authority)
        self.assertFalse(forged.grants_state_mutation_authority)

    def test_exact_reissue_is_idempotent(self) -> None:
        first = self._issue()
        second = self._issue()
        self.assertEqual(second, first)

    def test_post_hoc_population_substitution_is_rejected(self) -> None:
        self._issue()
        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "different scenario population",
        ):
            self._issue(
                self._members(
                    scenario_a="replacement-scenario-a",
                    mapping_a="replacement-mapping-a",
                )
            )

    def test_fixed_n_order_and_cardinality_are_exact(self) -> None:
        members = self._members()
        for bad in ((members[0],), (members[1], members[0])):
            with self.subTest(member_ids=tuple(item.member_id for item in bad)):
                with self.assertRaises(ProductProposalRiskScenarioPopulationError):
                    self._issue(bad)

    def test_terminal_population_is_required_and_part_of_durable_identity(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "terminal_population must be exact",
        ):
            issue_product_proposal_risk_scenario_population(
                self.workspace,
                self.precommit,
                object(),  # type: ignore[arg-type]
                self._members(),
            )

        object.__setattr__(
            self.terminal_population,
            "population_sha256",
            "f" * 64,
        )
        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "provider terminal population ledger identity changed",
        ):
            self._issue()

    def test_caller_cannot_backdate_population_timestamp(self) -> None:
        with self.assertRaises(TypeError):
            issue_product_proposal_risk_scenario_population(
                self.workspace,
                self.precommit,
                self.terminal_population,
                self._members(),
                bound_at="2026-09-18T13:19:59+00:00",
            )

    def test_iid_with_replacement_scenario_multiplicity_is_preserved(self) -> None:
        members = self._members(
            scenario_a="same-scenario",
            scenario_b="same-scenario",
            mapping_a="same-mapping",
            mapping_b="same-mapping",
        )
        issued = self._issue(members)
        self.assertEqual(
            issued.member_scenario_ids,
            ("same-scenario", "same-scenario"),
        )
        self.assertEqual(
            issued.member_mapping_sha256s,
            (_sha("same-mapping"), _sha("same-mapping")),
        )
        self.assertFalse(issued.terminal_mapping_proven)

    def test_superseded_target_invalidates_population(self) -> None:
        issued = self._issue()
        self.assertTrue(issued.population_identity_proven)

        issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.7")),
            contexts=getattr(self, "_proposal_risk_contexts"),
        )

        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "no longer the current target",
        ):
            self._resolve()
        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "no longer the current target",
        ):
            self._issue()

    def test_independent_authority_detects_ledger_rollback(self) -> None:
        self._issue()
        ledger_path = self.workspace / "decisions.jsonl"
        original = ledger_path.read_bytes()
        try:
            lines = original.splitlines(keepends=True)
            self.assertGreaterEqual(len(lines), 2)
            ledger_path.write_bytes(b"".join(lines[:-1]))
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "ledger record is missing|missing from the canonical Decision Ledger",
            ):
                self._resolve()
        finally:
            ledger_path.write_bytes(original)

    def test_missing_parent_precommit_or_target_ledger_root_blocks_issue(self) -> None:
        ledger_path = self.workspace / "decisions.jsonl"
        original = ledger_path.read_bytes()
        targets = (
            (
                self.precommit.decision_id,
                "evaluation precommit is missing|parent ledger roots cannot be re-resolved",
            ),
            (
                "target-v1:" + self.precommit.target_sha256,
                "risk target is missing|parent ledger roots cannot be re-resolved",
            ),
            (
                self.terminal_population.decision_id,
                "terminal population is missing|parent ledger roots cannot be re-resolved",
            ),
        )
        for decision_id, message in targets:
            with self.subTest(decision_id=decision_id):
                rows = []
                for raw in original.splitlines():
                    envelope = json.loads(raw.decode("utf-8"))
                    if envelope["record"]["decision_id"] != decision_id:
                        rows.append(raw)
                self.assertLess(len(rows), len(original.splitlines()))
                try:
                    ledger_path.write_bytes(b"\n".join(rows) + b"\n")
                    with self.assertRaisesRegex(
                        ProductProposalRiskScenarioPopulationError,
                        message,
                    ):
                        self._issue()
                finally:
                    ledger_path.write_bytes(original)

    def test_commit_crash_prefix_recovers_without_population_switch(self) -> None:
        original_commit = MonotonicWorkspaceAuthority.commit
        with patch.object(
            MonotonicWorkspaceAuthority,
            "commit",
            side_effect=MonotonicWorkspaceAuthorityError("injected commit crash"),
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "COMMIT failed",
            ):
                self._issue()

        resolved = self._resolve()
        self.assertTrue(resolved.population_identity_proven)
        self.assertTrue(resolved.provider_terminal_population_proven)
        self.assertEqual(resolved.member_scenario_ids, ("scenario-a", "scenario-b"))
        self.assertEqual(
            resolved.terminal_population_sha256,
            self.terminal_population.population_sha256,
        )
        self.assertIs(
            MonotonicWorkspaceAuthority.commit,
            original_commit,
        )

    def test_module_authority_alias_rebind_is_rejected_before_write(self) -> None:
        original = scenario_population_authority._LEDGER_APPEND
        try:
            scenario_population_authority._LEDGER_APPEND = lambda *args, **kwargs: None
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "authority dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority._LEDGER_APPEND = original


    def test_result_type_rebind_is_rejected_before_write(self) -> None:
        before = (self.workspace / "decisions.jsonl").read_bytes()
        original = scenario_population_authority.ProductProposalRiskScenarioPopulation
        try:
            scenario_population_authority.ProductProposalRiskScenarioPopulation = object
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority.ProductProposalRiskScenarioPopulation = original
        self.assertEqual((self.workspace / "decisions.jsonl").read_bytes(), before)

    def test_result_hard_false_getter_code_mutation_is_rejected(self) -> None:
        descriptor = ProductProposalRiskScenarioPopulation.__dict__[
            "scenario_execution_proven"
        ]
        getter = descriptor.fget
        self.assertIsNotNone(getter)
        original_code = getter.__code__

        def forged_scenario_execution_proven(self) -> bool:
            return True

        try:
            getter.__code__ = forged_scenario_execution_proven.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            getter.__code__ = original_code

    def test_internal_semantic_helper_rebind_is_rejected_on_resolve(self) -> None:
        issued = self._issue()
        self.assertTrue(issued.population_identity_proven)
        original = scenario_population_authority._population_material
        try:
            scenario_population_authority._population_material = (
                lambda *args, **kwargs: {"forged": True}
            )
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._resolve()
        finally:
            scenario_population_authority._population_material = original

    def test_json_dumps_in_place_code_mutation_is_rejected_before_write(self) -> None:
        before = (self.workspace / "decisions.jsonl").read_bytes()
        original_code = scenario_population_authority.json.dumps.__code__

        def forged_dumps(
            obj,
            *,
            skipkeys=False,
            ensure_ascii=True,
            check_circular=True,
            allow_nan=True,
            cls=None,
            indent=None,
            separators=None,
            default=None,
            sort_keys=False,
            **kw,
        ):
            return "{}"

        try:
            scenario_population_authority.json.dumps.__code__ = forged_dumps.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority.json.dumps.__code__ = original_code
        self.assertEqual((self.workspace / "decisions.jsonl").read_bytes(), before)


    def test_member_binding_type_rebind_is_rejected_before_write(self) -> None:
        before = (self.workspace / "decisions.jsonl").read_bytes()
        original = scenario_population_authority.CounterfactualScenarioMemberBinding
        try:
            scenario_population_authority.CounterfactualScenarioMemberBinding = object
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority.CounterfactualScenarioMemberBinding = original
        self.assertEqual((self.workspace / "decisions.jsonl").read_bytes(), before)

    def test_member_binding_is_revalidated_after_construction(self) -> None:
        members = list(self._members())
        object.__setattr__(members[0], "mapping_sha256", "not-a-sha256")
        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "mapping_sha256 must be lowercase SHA-256 hex",
        ):
            self._issue(tuple(members))

    def test_protocol_constant_rebind_is_rejected_before_write(self) -> None:
        before = (self.workspace / "decisions.jsonl").read_bytes()
        original = scenario_population_authority._BINDING_SCOPE
        try:
            scenario_population_authority._BINDING_SCOPE = "FORGED_SCOPE"
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority._BINDING_SCOPE = original
        self.assertEqual((self.workspace / "decisions.jsonl").read_bytes(), before)

    def test_monotonic_authority_internal_code_mutation_is_rejected(self) -> None:
        method = MonotonicWorkspaceAuthority._load_history
        original_code = method.__code__

        def forged_load_history(self, *args, **kwargs):
            return None

        try:
            method.__code__ = forged_load_history.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            method.__code__ = original_code

    def test_decision_ledger_internal_code_mutation_is_rejected(self) -> None:
        ledger_type = scenario_population_authority.JsonlDecisionLedger
        method = ledger_type._verify_bytes
        original_code = method.__code__

        def forged_verify_bytes(self, *args, **kwargs):
            return ()

        try:
            method.__code__ = forged_verify_bytes.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            method.__code__ = original_code


    def test_member_binding_type_rebind_is_rejected_before_write(self) -> None:
        before = (self.workspace / "decisions.jsonl").read_bytes()
        original = scenario_population_authority.CounterfactualScenarioMemberBinding
        try:
            scenario_population_authority.CounterfactualScenarioMemberBinding = object
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority.CounterfactualScenarioMemberBinding = original
        self.assertEqual((self.workspace / "decisions.jsonl").read_bytes(), before)

    def test_member_binding_is_revalidated_after_construction(self) -> None:
        members = list(self._members())
        object.__setattr__(members[0], "mapping_sha256", "not-a-sha256")
        with self.assertRaisesRegex(
            ProductProposalRiskScenarioPopulationError,
            "mapping_sha256 must be lowercase SHA-256 hex",
        ):
            self._issue(tuple(members))

    def test_protocol_constant_rebind_is_rejected_before_write(self) -> None:
        before = (self.workspace / "decisions.jsonl").read_bytes()
        original = scenario_population_authority._BINDING_SCOPE
        try:
            scenario_population_authority._BINDING_SCOPE = "FORGED_SCOPE"
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            scenario_population_authority._BINDING_SCOPE = original
        self.assertEqual((self.workspace / "decisions.jsonl").read_bytes(), before)

    def test_monotonic_authority_internal_code_mutation_is_rejected(self) -> None:
        method = MonotonicWorkspaceAuthority._load_history
        original_code = method.__code__

        def forged_load_history(self, *args, **kwargs):
            return None

        try:
            method.__code__ = forged_load_history.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            method.__code__ = original_code

    def test_decision_ledger_internal_code_mutation_is_rejected(self) -> None:
        ledger_type = scenario_population_authority.JsonlDecisionLedger
        method = ledger_type._verify_bytes
        original_code = method.__code__

        def forged_verify_bytes(self, *args, **kwargs):
            return ()

        try:
            method.__code__ = forged_verify_bytes.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            method.__code__ = original_code

if __name__ == "__main__":
    unittest.main()
