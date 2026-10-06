from __future__ import annotations

import dis
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
    lay_first: bool = False,
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
        is_lay = lay_first and suffix == "a"
        odds = Decimal("5") if is_lay else Decimal("2")
        exchange_side = "lay" if is_lay else None
        market_semantics_id = (
            "exchange.match.odds.v1"
            if is_lay
            else None
        )
        leg = TicketLeg(
            event_id=f"event-{suffix}",
            market_id=f"market-{suffix}",
            selection_id=f"selection-{suffix}",
            locked_odds=odds,
            sport="table_tennis",
            exchange_side=exchange_side,
            market_semantics_id=market_semantics_id,
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=odds,
            observed_ts=quote_ts,
            source_id="betfair_exchange_historical",
            sequence=1,
            market_type=MarketType.WINNER,
            source_ts=quote_ts,
            ingest_ts=quote_ts,
            metadata={},
            sport="table_tennis",
            exchange_side=exchange_side,
            market_semantics_id=market_semantics_id,
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
    setattr(test, "_proposal_terminal_authorities", tuple(terminal_authorities))

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
        executed_capital_at_risk=executed_stakes or precommit.evaluated_stakes,
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
                executed_capital_at_risk=(Decimal("4"), Decimal("1")),
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


import autosport.proposal_risk_terminal_state_mapping_authority as terminal_mapping_authority
from autosport.proposal_risk_terminal_state_mapping_authority import (
    ProductProposalRiskTerminalStateMapping,
    ProductProposalRiskTerminalStateMappingError,
    derive_product_proposal_terminal_scenario_binding,
    resolve_product_proposal_risk_terminal_state_mapping,
)


    def test_mapping_json_dumps_code_mutation_is_rejected(self) -> None:
        original_code = json.dumps.__code__

        def forged_dumps(*_args, **_kwargs):
            return "{}"

        try:
            json.dumps.__code__ = forged_dumps.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            json.dumps.__code__ = original_code

    def test_mapping_json_loads_code_mutation_is_rejected(self) -> None:
        original_code = json.loads.__code__

        def forged_loads(*_args, **_kwargs):
            return {}

        try:
            json.loads.__code__ = forged_loads.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            json.loads.__code__ = original_code

    def test_mapping_dispatch_guard_code_mutation_is_rejected(self) -> None:
        guard = terminal_mapping_authority._REQUIRE_DISPATCH_ORIGINAL
        original_code = guard.__code__

        def forged_guard(*_args, **_kwargs):
            return None

        try:
            guard.__code__ = forged_guard.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch guard root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            guard.__code__ = original_code

    def test_mapping_positive_capability_token_cell_mutation_is_rejected(
        self,
    ) -> None:
        getter = ProductProposalRiskTerminalStateMapping.__dict__[
            "terminal_mapping_proven"
        ].fget
        proof = getter.__defaults__[0]
        closure = proof.__closure__
        self.assertIsNotNone(closure)
        self.assertEqual(len(closure), 1)
        cell = closure[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "result authority surface changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            cell.cell_contents = original

    def test_mapping_public_binder_token_cell_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        resolver = (
            terminal_mapping_authority
            .resolve_product_proposal_risk_terminal_state_mapping
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        binder = binder_cells[0].cell_contents
        binder_closure = binder.__closure__
        self.assertIsNotNone(binder_closure)
        self.assertEqual(len(binder_closure), 1)
        token_cell = binder_closure[0]
        original = token_cell.cell_contents
        try:
            token_cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            token_cell.cell_contents = original

    def test_mapping_result_field_descriptor_rebind_is_rejected(self) -> None:
        original = ProductProposalRiskTerminalStateMapping.__dict__[
            "resolution_sha256"
        ]
        try:
            ProductProposalRiskTerminalStateMapping.resolution_sha256 = (
                property(lambda _self: "0" * 64)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "result field surface changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            ProductProposalRiskTerminalStateMapping.resolution_sha256 = original

    def test_mapping_public_result_type_closure_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        resolver = (
            terminal_mapping_authority
            .resolve_product_proposal_risk_terminal_state_mapping
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        result_type_cells = [
            cell
            for cell in closure
            if cell.cell_contents is ProductProposalRiskTerminalStateMapping
        ]
        self.assertEqual(len(result_type_cells), 1)
        cell = result_type_cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_mapping_public_resolver_snapshots_closure_before_core(
        self,
    ) -> None:
        resolver = (
            terminal_mapping_authority
            .resolve_product_proposal_risk_terminal_state_mapping
        )
        instructions = list(dis.get_instructions(resolver))
        values_store_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_FAST"
            and instruction.argval == "values"
        )
        snapshot_locals = (
            "bind_mapping",
            "resolve_core",
            "result_type",
            "result_fields",
            "expected_bind_code",
            "expected_core_code",
        )
        for local_name in snapshot_locals:
            with self.subTest(local_name=local_name):
                stores = [
                    index
                    for index, instruction in enumerate(instructions)
                    if instruction.opname == "STORE_FAST"
                    and instruction.argval == local_name
                ]
                self.assertEqual(len(stores), 1)
                self.assertLess(stores[0], values_store_index)

        mutable_cells = {
            "_bind_mapping",
            "_resolve_core",
            "_result_type",
            "_result_fields",
            "_bind_code",
            "_resolve_core_code",
        }
        late_reads = [
            instruction.argval
            for instruction in instructions[values_store_index + 1 :]
            if instruction.opname == "LOAD_DEREF"
            and instruction.argval in mutable_cells
        ]
        self.assertEqual(late_reads, [])

    def test_mapping_helper_witness_table_rebind_is_rejected(self) -> None:
        original = terminal_mapping_authority._HELPER_WITNESSES_EXPECTED
        try:
            terminal_mapping_authority._HELPER_WITNESSES_EXPECTED = tuple(
                list(original)
            )
            self.assertIsNot(
                terminal_mapping_authority._HELPER_WITNESSES_EXPECTED,
                original,
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            terminal_mapping_authority._HELPER_WITNESSES_EXPECTED = original

    def test_mapping_helper_kwdefault_mutation_is_rejected(self) -> None:
        kwdefaults = terminal_mapping_authority._text.__kwdefaults__
        self.assertIsNotNone(kwdefaults)
        original = kwdefaults["max_length"]
        try:
            kwdefaults["max_length"] = original + 1
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "helper root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            kwdefaults["max_length"] = original


import autosport.proposal_risk_terminal_payoff_authority as terminal_payoff_authority
from autosport.proposal_risk_terminal_payoff_authority import (
    ProductProposalRiskTerminalPayoffEvaluation,
    ProductProposalRiskTerminalPayoffEvaluationError,
    resolve_product_proposal_risk_terminal_payoff_evaluation,
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
        records = [
            json.loads(line)["record"]
            for line in (self.workspace / "decisions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        target_records = [
            record
            for record in records
            if record["action"] == "PROPOSAL_RISK_TARGET_PRECOMMIT"
        ]
        self.assertEqual(len(target_records), 1)
        self.assertEqual(
            target_records[0]["payload"]["schema"],
            "autosport.proposal-risk-target-precommit.v2",
        )
        self.assertEqual(
            scenario_population_authority._TARGET_SCHEMA,
            target_records[0]["payload"]["schema"],
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


    def test_result_capability_getter_defaults_rebind_is_rejected(self) -> None:
        getter = ProductProposalRiskScenarioPopulation.__dict__[
            "population_identity_proven"
        ].fget
        self.assertIsNotNone(getter)
        original_defaults = getter.__defaults__

        def forged_proof(_instance):
            return True

        try:
            getter.__defaults__ = (forged_proof,)
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            getter.__defaults__ = original_defaults

    def test_result_capability_proof_code_mutation_is_rejected(self) -> None:
        getter = ProductProposalRiskScenarioPopulation.__dict__[
            "population_identity_proven"
        ].fget
        self.assertIsNotNone(getter)
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            proof.__code__ = original_code

    def test_precommit_parent_capability_proof_code_mutation_is_rejected(self) -> None:
        getter = ProductProposalRiskEvaluationPrecommit.__dict__[
            "binding_identity_proven"
        ].fget
        self.assertIsNotNone(getter)
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            proof.__code__ = original_code

    def test_terminal_parent_capability_proof_code_mutation_is_rejected(self) -> None:
        getter = ProductProposalTargetTerminalPopulation.__dict__[
            "population_identity_proven"
        ].fget
        self.assertIsNotNone(getter)
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            proof.__code__ = original_code


    def test_dispatch_guard_code_mutation_is_rejected(self) -> None:
        guard = scenario_population_authority._REQUIRE_DISPATCH_ORIGINAL
        original_code = guard.__code__

        def forged_guard(*args, **kwargs):
            return None

        try:
            guard.__code__ = forged_guard.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch guard root changed",
            ):
                self._issue()
        finally:
            guard.__code__ = original_code

    def test_dispatch_guard_defaults_rebind_is_rejected(self) -> None:
        guard = scenario_population_authority._REQUIRE_DISPATCH_ORIGINAL
        original_defaults = guard.__defaults__
        try:
            guard.__defaults__ = tuple(list(original_defaults))
            self.assertIsNot(guard.__defaults__, original_defaults)
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch guard root changed",
            ):
                self._issue()
        finally:
            guard.__defaults__ = original_defaults

    def test_internal_unbound_resolution_cannot_mint_population_identity(self) -> None:
        issued = self._issue()
        self.assertTrue(issued.population_identity_proven)
        self.assertFalse(hasattr(scenario_population_authority, "_BIND_IDENTITY"))
        self.assertFalse(hasattr(scenario_population_authority, "_mint"))

        unbound = (
            scenario_population_authority
            ._resolve_product_proposal_risk_scenario_population_unbound(
                self.workspace,
                self.precommit,
                self.terminal_population,
            )
        )
        self.assertFalse(unbound.population_identity_proven)
        self.assertFalse(unbound.fixed_n_member_mapping_complete)
        self.assertFalse(unbound.provider_terminal_population_proven)
        self.assertFalse(unbound.terminal_mapping_proven)
        self.assertFalse(unbound.scenario_execution_proven)
        self.assertFalse(unbound.grants_ticket_authority)
        self.assertFalse(unbound.grants_real_money_authority)

    def test_public_issue_snapshots_binder_before_core(self) -> None:
        issue = (
            scenario_population_authority
            .issue_product_proposal_risk_scenario_population
        )
        instructions = list(dis.get_instructions(issue))
        instance_store_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_FAST"
            and instruction.argval == "instance"
        )
        for local_name in (
            "bind_identity",
            "issue_core",
            "expected_bind_code",
            "expected_issue_core_code",
        ):
            stores = [
                index
                for index, instruction in enumerate(instructions)
                if instruction.opname == "STORE_FAST"
                and instruction.argval == local_name
            ]
            self.assertEqual(len(stores), 1)
            self.assertLess(stores[0], instance_store_index)
        late_reads = [
            instruction.argval
            for instruction in instructions[instance_store_index + 1 :]
            if instruction.opname == "LOAD_DEREF"
            and instruction.argval in {
                "_bind_identity",
                "_issue_core",
                "_bind_code",
                "_issue_core_code",
            }
        ]
        self.assertEqual(late_reads, [])

    def test_public_resolve_snapshots_binder_before_core(self) -> None:
        resolve = (
            scenario_population_authority
            .resolve_product_proposal_risk_scenario_population
        )
        instructions = list(dis.get_instructions(resolve))
        instance_store_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_FAST"
            and instruction.argval == "instance"
        )
        for local_name in (
            "bind_identity",
            "resolve_core",
            "expected_bind_code",
            "expected_resolve_core_code",
        ):
            stores = [
                index
                for index, instruction in enumerate(instructions)
                if instruction.opname == "STORE_FAST"
                and instruction.argval == local_name
            ]
            self.assertEqual(len(stores), 1)
            self.assertLess(stores[0], instance_store_index)
        late_reads = [
            instruction.argval
            for instruction in instructions[instance_store_index + 1 :]
            if instruction.opname == "LOAD_DEREF"
            and instruction.argval in {
                "_bind_identity",
                "_resolve_core",
                "_bind_code",
                "_resolve_core_code",
            }
        ]
        self.assertEqual(late_reads, [])

    def test_public_issue_resolver_rebind_is_rejected_by_durable_core(self) -> None:
        original = (
            scenario_population_authority
            .issue_product_proposal_risk_scenario_population
        )
        try:
            scenario_population_authority.issue_product_proposal_risk_scenario_population = (
                lambda *args, **kwargs: None
            )
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                scenario_population_authority._issue_product_proposal_risk_scenario_population_unbound(
                    self.workspace,
                    self.precommit,
                    self.terminal_population,
                    self._members(),
                )
        finally:
            scenario_population_authority.issue_product_proposal_risk_scenario_population = (
                original
            )

    def test_public_issue_closure_binder_mutation_is_rejected(self) -> None:
        issue = (
            scenario_population_authority
            .issue_product_proposal_risk_scenario_population
        )
        closure = issue.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        cell = binder_cells[0]
        original = cell.cell_contents

        def forged_bind(_instance):
            return None

        try:
            cell.cell_contents = forged_bind
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "public issue closure changed",
            ):
                self._issue()
        finally:
            cell.cell_contents = original

    def test_public_issue_ignores_module_global_core_rebind(self) -> None:
        original = (
            scenario_population_authority
            ._issue_product_proposal_risk_scenario_population_unbound
        )
        try:
            scenario_population_authority._issue_product_proposal_risk_scenario_population_unbound = (
                lambda *args, **kwargs: object()
            )
            issued = self._issue()
            self.assertTrue(issued.population_identity_proven)
            self.assertTrue(issued.fixed_n_member_mapping_complete)
        finally:
            scenario_population_authority._issue_product_proposal_risk_scenario_population_unbound = (
                original
            )

    def test_public_issue_rejects_captured_core_code_mutation(self) -> None:
        issue = (
            scenario_population_authority
            .issue_product_proposal_risk_scenario_population
        )
        closure = issue.__closure__
        self.assertIsNotNone(closure)
        core_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", "").startswith(
                "_issue_product_proposal_risk_scenario_population_unbound"
            )
        ]
        self.assertEqual(len(core_cells), 1)
        core = core_cells[0].cell_contents
        original_code = core.__code__

        def forged_core(*_args, **_kwargs):
            return object.__new__(ProductProposalRiskScenarioPopulation)

        try:
            core.__code__ = forged_core.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "public issue closure changed",
            ):
                self._issue()
        finally:
            core.__code__ = original_code

    def test_text_kwdefault_mutation_is_rejected(self) -> None:
        helper = scenario_population_authority._text
        kwdefaults = helper.__kwdefaults__
        original = kwdefaults["max_length"]
        try:
            kwdefaults["max_length"] = 4096
            with self.assertRaisesRegex(
                ProductProposalRiskScenarioPopulationError,
                "dispatch changed",
            ):
                self._issue()
        finally:
            kwdefaults["max_length"] = original


class ProductProposalRiskTerminalStateMappingTests(unittest.TestCase):

    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.authorities = getattr(self, "_proposal_terminal_authorities")
        self.assertFalse(
            self.terminal_population.terminal_space_exact,
            "the fixture intentionally uses conservative provider terminal "
            "supersets and has no proven Cartesian joint support",
        )

    def _binding(
        self,
        state_ids: tuple[str, ...],
    ):
        return derive_product_proposal_terminal_scenario_binding(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            market_state_ids=state_ids,
        )

    def _bindings(self):
        return (
            self._binding(("canonical:win,loss", "canonical:loss,win")),
            self._binding(("canonical:loss,win", "canonical:win,loss")),
        )

    def _issue(
        self,
        bindings=None,
    ) -> ProductProposalRiskScenarioPopulation:
        first, second = bindings or self._bindings()
        return issue_product_proposal_risk_scenario_population(
            self.workspace,
            self.precommit,
            self.terminal_population,
            (
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[0],
                    scenario_id=first.scenario_id,
                    mapping_sha256=first.mapping_sha256,
                ),
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[1],
                    scenario_id=second.scenario_id,
                    mapping_sha256=second.mapping_sha256,
                ),
            ),
        )

    def _resolve(self, bindings=None) -> ProductProposalRiskTerminalStateMapping:
        first, second = bindings or self._bindings()
        return resolve_product_proposal_risk_terminal_state_mapping(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            member_market_state_ids=(
                first.market_state_ids,
                second.market_state_ids,
            ),
        )

    def test_precommitted_provider_terminal_vectors_prove_only_mapping(self) -> None:
        bindings = self._bindings()
        parent = self._issue(bindings)
        result = self._resolve(bindings)

        self.assertTrue(parent.population_identity_proven)
        self.assertFalse(parent.terminal_mapping_proven)
        self.assertTrue(result.mapping_identity_proven)
        self.assertTrue(result.provider_terminal_population_proven)
        self.assertTrue(result.fixed_n_member_mapping_complete)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.per_market_terminal_states_exact)
        self.assertTrue(result.terminal_mapping_proven)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.scenario_execution_proven)
        self.assertFalse(result.proposal_target_counterfactual_execution_proven)
        self.assertFalse(result.risk_upper_bound_for_target)
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_ticket_authority)
        self.assertFalse(result.grants_broker_execution_authority)
        self.assertFalse(result.grants_real_money_authority)
        self.assertFalse(result.grants_state_mutation_authority)
        self.assertEqual(
            result.member_state_vector_sha256s,
            tuple(binding.state_vector_sha256 for binding in bindings),
        )
        self.assertEqual(len(result.resolution_sha256), 64)
        self.assertEqual(self._resolve(bindings), result)

    def test_conservative_all_void_state_maps_without_support_authority(self) -> None:
        void_member = self._binding(
            ("canonical:void,void", "canonical:void,void")
        )
        bindings = (void_member, void_member)
        self._issue(bindings)
        result = self._resolve(bindings)

        self.assertTrue(result.mapping_identity_proven)
        self.assertTrue(result.terminal_mapping_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.per_market_terminal_states_exact)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.risk_upper_bound_for_target)
        self.assertEqual(
            result.member_state_vector_sha256s,
            (void_member.state_vector_sha256, void_member.state_vector_sha256),
        )

    def test_scenario_commitments_are_fixed_size_and_deterministic(self) -> None:
        first = self._binding(("canonical:win,loss", "canonical:loss,win"))
        again = self._binding(("canonical:win,loss", "canonical:loss,win"))
        changed = self._binding(("canonical:loss,win", "canonical:loss,win"))

        self.assertEqual(first, again)
        self.assertNotEqual(first.scenario_id, changed.scenario_id)
        self.assertNotEqual(first.mapping_sha256, changed.mapping_sha256)
        self.assertTrue(first.scenario_id.startswith("terminal-vector-v1:"))
        self.assertLessEqual(len(first.scenario_id), 96)
        self.assertEqual(len(first.mapping_sha256), 64)

    def test_direct_or_forged_positive_result_cannot_mint_mapping_authority(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskTerminalStateMapping()

        forged = object.__new__(ProductProposalRiskTerminalStateMapping)
        self.assertFalse(forged.mapping_identity_proven)
        self.assertFalse(forged.provider_terminal_population_proven)
        self.assertFalse(forged.fixed_n_member_mapping_complete)
        self.assertFalse(forged.per_market_terminal_states_exact)
        self.assertFalse(forged.terminal_mapping_proven)
        self.assertFalse(forged.product_scenario_source_provenance_proven)
        self.assertFalse(forged.scenario_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_wrong_precommitted_mapping_digest_fails_closed(self) -> None:
        first, second = self._bindings()
        self._issue(
            (
                type(first)(
                    terminal_population_sha256=first.terminal_population_sha256,
                    market_group_sha256s=first.market_group_sha256s,
                    market_state_ids=first.market_state_ids,
                    scenario_id=first.scenario_id,
                    state_vector_sha256=first.state_vector_sha256,
                    mapping_sha256="f" * 64,
                ),
                second,
            )
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalStateMappingError,
            "does not re-derive",
        ):
            self._resolve((first, second))

    def test_post_precommit_state_vector_substitution_fails_closed(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalStateMappingError,
            "scenario_id does not match",
        ):
            resolve_product_proposal_risk_terminal_state_mapping(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(
                    ("canonical:loss,win", "canonical:loss,win"),
                    bindings[1].market_state_ids,
                ),
            )

    def test_unknown_terminal_state_fails_before_positive_mapping(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalStateMappingError,
            "not a verified terminal state",
        ):
            resolve_product_proposal_risk_terminal_state_mapping(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(
                    ("canonical:missing", "canonical:loss,win"),
                    bindings[1].market_state_ids,
                ),
            )

    def test_fixed_n_state_vector_cardinality_is_exact(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalStateMappingError,
            "exact fixed-N cohort",
        ):
            resolve_product_proposal_risk_terminal_state_mapping(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(bindings[0].market_state_ids,),
            )

    def test_missing_reverified_provider_authority_fails_closed(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalStateMappingError,
            "provider terminal population cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_state_mapping(
                self.workspace,
                precommit=self.precommit,
                authorities=(self.authorities[0],),
                member_market_state_ids=(
                    bindings[0].market_state_ids,
                    bindings[1].market_state_ids,
                ),
            )

    def test_iid_multiplicity_can_map_but_does_not_prove_draw_law(self) -> None:
        repeated = self._binding(("canonical:win,loss", "canonical:win,loss"))
        bindings = (repeated, repeated)
        self._issue(bindings)
        result = self._resolve(bindings)

        self.assertTrue(result.terminal_mapping_proven)
        self.assertEqual(
            result.member_scenario_ids,
            (repeated.scenario_id, repeated.scenario_id),
        )
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertFalse(result.joint_scenario_support_proven)

    def test_superseded_target_invalidates_terminal_mapping(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.7")),
            contexts=getattr(self, "_proposal_risk_contexts"),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalStateMappingError,
            "proposal target cannot be re-resolved",
        ):
            self._resolve(bindings)

    def test_hash_dispatch_mutation_is_rejected_before_parent_resolution(self) -> None:
        original = terminal_mapping_authority.hashlib.sha256
        try:
            terminal_mapping_authority.hashlib.sha256 = lambda *args, **kwargs: None
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            terminal_mapping_authority.hashlib.sha256 = original

    def test_positive_capability_proof_code_mutation_is_rejected(self) -> None:
        getter = ProductProposalRiskTerminalStateMapping.__dict__[
            "terminal_mapping_proven"
        ].fget
        self.assertIsNotNone(getter)
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "result authority surface changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            proof.__code__ = original_code

    def test_hard_false_getter_code_mutation_is_rejected(self) -> None:
        getter = ProductProposalRiskTerminalStateMapping.__dict__[
            "scenario_execution_proven"
        ].fget
        self.assertIsNotNone(getter)
        original_code = getter.__code__

        def forged_execution(_self):
            return True

        try:
            getter.__code__ = forged_execution.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "result authority surface changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            getter.__code__ = original_code

    def test_validated_values_cannot_directly_mint_positive_mapping(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        self.assertFalse(hasattr(terminal_mapping_authority, "_BIND_MAPPING"))
        self.assertFalse(hasattr(terminal_mapping_authority, "_mint_result"))

        values = (
            terminal_mapping_authority
            ._resolve_product_proposal_risk_terminal_state_mapping_values(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(
                    bindings[0].market_state_ids,
                    bindings[1].market_state_ids,
                ),
            )
        )
        forged = object.__new__(ProductProposalRiskTerminalStateMapping)
        for name in terminal_mapping_authority._RESULT_FIELDS_EXPECTED:
            object.__setattr__(forged, name, values[name])

        self.assertFalse(forged.mapping_identity_proven)
        self.assertFalse(forged.terminal_mapping_proven)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_mapping_resolver_closure_binder_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        resolver = (
            terminal_mapping_authority
            .resolve_product_proposal_risk_terminal_state_mapping
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        cell = binder_cells[0]
        original = cell.cell_contents

        def forged_bind(_instance):
            return None

        try:
            cell.cell_contents = forged_bind
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "public resolver closure changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_mapping_resolver_rejects_module_global_core_rebind(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = (
            terminal_mapping_authority
            ._resolve_product_proposal_risk_terminal_state_mapping_values
        )
        try:
            terminal_mapping_authority._resolve_product_proposal_risk_terminal_state_mapping_values = (
                lambda *args, **kwargs: {
                    "workspace_instance_id": "forged",
                }
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "helper root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_mapping_authority._resolve_product_proposal_risk_terminal_state_mapping_values = (
                original
            )

    def test_mapping_resolver_rejects_captured_core_code_mutation(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        resolver = (
            terminal_mapping_authority
            .resolve_product_proposal_risk_terminal_state_mapping
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        core_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", "").startswith(
                "_resolve_product_proposal_risk_terminal_state_mapping_values"
            )
        ]
        self.assertEqual(len(core_cells), 1)
        core = core_cells[0].cell_contents
        original_code = core.__code__

        def forged_core(*_args, **_kwargs):
            return {
                "workspace_instance_id": self.precommit.workspace_instance_id,
            }

        try:
            core.__code__ = forged_core.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "public resolver closure changed",
            ):
                self._resolve(bindings)
        finally:
            core.__code__ = original_code

    def test_protocol_constant_rebind_is_rejected(self) -> None:
        original = terminal_mapping_authority._MAPPING_SCHEMA
        try:
            terminal_mapping_authority._MAPPING_SCHEMA = "forged.mapping.v999"
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            terminal_mapping_authority._MAPPING_SCHEMA = original

    def test_terminal_state_serializer_code_mutation_is_rejected(self) -> None:
        method = terminal_mapping_authority.MarketTerminalState.to_dict
        original_code = method.__code__

        def forged_to_dict(_self):
            return {"state_id": "forged", "settlements": []}

        try:
            method.__code__ = forged_to_dict.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalStateMappingError,
                "dispatch root changed",
            ):
                self._binding(("canonical:win,loss", "canonical:loss,win"))
        finally:
            method.__code__ = original_code



class ProductProposalRiskTerminalPayoffEvaluationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.authorities = getattr(self, "_proposal_terminal_authorities")
        self.assertFalse(self.terminal_population.terminal_space_exact)

    def _binding(
        self,
        state_ids: tuple[str, ...],
    ):
        return derive_product_proposal_terminal_scenario_binding(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            market_state_ids=state_ids,
        )

    def _bindings(self):
        return (
            self._binding(("canonical:win,loss", "canonical:loss,win")),
            self._binding(("canonical:loss,win", "canonical:win,loss")),
        )

    def _issue_mapping_parent(self, bindings=None) -> None:
        first, second = bindings or self._bindings()
        issue_product_proposal_risk_scenario_population(
            self.workspace,
            self.precommit,
            self.terminal_population,
            (
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[0],
                    scenario_id=first.scenario_id,
                    mapping_sha256=first.mapping_sha256,
                ),
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[1],
                    scenario_id=second.scenario_id,
                    mapping_sha256=second.mapping_sha256,
                ),
            ),
        )

    def _resolve(self, bindings=None) -> ProductProposalRiskTerminalPayoffEvaluation:
        first, second = bindings or self._bindings()
        return resolve_product_proposal_risk_terminal_payoff_evaluation(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            member_market_state_ids=(
                first.market_state_ids,
                second.market_state_ids,
            ),
        )

    def test_exact_terminal_payoffs_use_canonical_paperbook_economics_only(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        result = self._resolve(bindings)
        stake_a, stake_b = self.precommit.evaluated_stakes

        self.assertTrue(result.evaluation_identity_proven)
        self.assertTrue(result.terminal_mapping_consumed_proven)
        self.assertTrue(result.paperbook_settlement_arithmetic_proven)
        self.assertTrue(result.fixed_n_member_payoff_complete)
        self.assertTrue(result.target_terminal_payoff_evaluation_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertEqual(
            result.settlement_evaluator_protocol,
            (
                "portfolio-engine.scenario-profit-settlements+"
                "paperbook.settlement-result.v1"
            ),
        )
        self.assertEqual(len(result.scenario_population_sha256), 64)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertEqual(
            result.member_mapping_sha256s,
            tuple(binding.mapping_sha256 for binding in bindings),
        )
        self.assertEqual(
            result.member_candidate_paper_profit_vectors,
            (
                (-stake_a, stake_b),
                (stake_a, -stake_b),
            ),
        )
        self.assertEqual(
            result.member_paper_terminal_profits,
            (
                stake_b - stake_a,
                stake_a - stake_b,
            ),
        )
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.minimum_equity_path_proven)
        self.assertFalse(result.execution_costs_proven)
        self.assertFalse(result.slippage_realization_proven)
        self.assertFalse(result.net_execution_pnl_proven)
        self.assertFalse(result.cashflow_chronology_proven)
        self.assertFalse(result.scenario_execution_proven)
        self.assertFalse(result.proposal_target_counterfactual_execution_proven)
        self.assertFalse(result.risk_upper_bound_for_target)
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_ticket_authority)
        self.assertFalse(result.grants_broker_execution_authority)
        self.assertFalse(result.grants_real_money_authority)
        self.assertFalse(result.grants_state_mutation_authority)
        self.assertEqual(len(result.evaluation_sha256), 64)
        self.assertEqual(self._resolve(bindings), result)

    def test_lay_candidate_uses_semantic_settlement_key_and_liability_pnl(self) -> None:
        target = terminal_payoff_authority._TARGET_RESOLVER(
            self.workspace,
            self.precommit.target_sha256,
        )
        contexts = list(target.candidate_context_json)
        payload = json.loads(contexts[0])
        payload["legs"][0]["locked_odds"] = "5"
        payload["legs"][0]["exchange_side"] = "lay"
        payload["legs"][0]["market_semantics_id"] = "exchange.match.odds.v1"
        contexts[0] = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(target, "candidate_context_json", tuple(contexts))

        tickets = terminal_payoff_authority._candidate_tickets(target)
        lay_ticket = tickets[0]
        leg = lay_ticket.legs[0]
        liability = lay_ticket.stake * Decimal("4")

        self.assertEqual(leg.exchange_side, "lay")
        self.assertEqual(
            leg.market_semantics_id,
            "exchange.match.odds.v1",
        )
        self.assertNotEqual(leg.settlement_key, leg.quote_key)
        self.assertEqual(
            terminal_payoff_authority._PORTFOLIO_SCENARIO_PROFIT(
                [lay_ticket],
                {leg.settlement_key: "win"},
            ),
            -liability,
        )
        self.assertEqual(
            terminal_payoff_authority._PORTFOLIO_SCENARIO_PROFIT(
                [lay_ticket],
                {leg.settlement_key: "loss"},
            ),
            lay_ticket.stake,
        )
        self.assertEqual(
            terminal_payoff_authority._PORTFOLIO_SCENARIO_PROFIT(
                [lay_ticket],
                {leg.settlement_key: "void"},
            ),
            Decimal("0"),
        )
        with self.assertRaisesRegex(
            ValueError,
            "missing terminal settlement evidence",
        ):
            terminal_payoff_authority._PORTFOLIO_SCENARIO_PROFIT(
                [lay_ticket],
                {leg.quote_key: "win"},
            )

    def test_public_terminal_payoff_resolver_prices_lay_profit_and_liability_end_to_end(self) -> None:
        self.precommit = _canonical_precommit(self, lay_first=True)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.authorities = getattr(self, "_proposal_terminal_authorities")
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)

        result = self._resolve(bindings)
        stake_a, stake_b = self.precommit.evaluated_stakes
        liability_a = stake_a * Decimal("4")

        self.assertEqual(
            result.member_candidate_paper_profit_vectors,
            (
                (stake_a, stake_b),
                (-liability_a, -stake_b),
            ),
        )
        self.assertEqual(
            result.member_paper_terminal_profits,
            (
                stake_a + stake_b,
                -liability_a - stake_b,
            ),
        )
        self.assertTrue(result.paperbook_settlement_arithmetic_proven)
        self.assertTrue(result.target_terminal_payoff_evaluation_proven)
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_real_money_authority)

    def test_zero_stake_candidate_is_zero_exposure_even_when_state_wins(self) -> None:
        target = terminal_payoff_authority._TARGET_RESOLVER(
            self.workspace,
            self.precommit.target_sha256,
        )
        tickets = list(terminal_payoff_authority._candidate_tickets(target))
        tickets[0].stake = Decimal("0")
        authority_by_sha = terminal_payoff_authority._authority_map(
            self.authorities
        )
        groups = terminal_payoff_authority._terminal_groups(
            self.terminal_population,
            authority_by_sha,
        )
        candidate_profits, total_profit = terminal_payoff_authority._scenario_payoff(
            tickets=tuple(tickets),
            population=self.terminal_population,
            groups=groups,
            authority_by_sha=authority_by_sha,
            member_state_ids=(
                "canonical:loss,win",
                "canonical:loss,win",
            ),
        )

        self.assertEqual(candidate_profits[0], Decimal("0"))
        self.assertEqual(
            candidate_profits[1],
            self.precommit.evaluated_stakes[1],
        )
        self.assertEqual(
            total_profit,
            self.precommit.evaluated_stakes[1],
        )

    def test_zero_stake_still_requires_canonical_paperbook_leg_economics(self) -> None:
        target = terminal_payoff_authority._TARGET_RESOLVER(
            self.workspace,
            self.precommit.target_sha256,
        )
        contexts = list(target.candidate_context_json)
        self.assertIn('"locked_odds":"2"', contexts[0])
        contexts[0] = contexts[0].replace(
            '"locked_odds":"2"',
            '"locked_odds":"1"',
            1,
        )
        object.__setattr__(
            target,
            "candidate_context_json",
            tuple(contexts),
        )
        object.__setattr__(
            target,
            "evaluated_stakes",
            (
                Decimal("0"),
                target.evaluated_stakes[1],
            ),
        )

        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "outside canonical PaperBook settlement economics",
        ):
            terminal_payoff_authority._candidate_tickets(target)

    def test_scenario_payoff_refuses_candidate_vector_cardinality_drift(self) -> None:
        target = terminal_payoff_authority._TARGET_RESOLVER(
            self.workspace,
            self.precommit.target_sha256,
        )
        tickets = terminal_payoff_authority._candidate_tickets(target)
        authority_by_sha = terminal_payoff_authority._authority_map(
            self.authorities
        )
        groups = terminal_payoff_authority._terminal_groups(
            self.terminal_population,
            authority_by_sha,
        )

        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "candidate vector no longer matches",
        ):
            terminal_payoff_authority._scenario_payoff(
                tickets=tickets[:-1],
                population=self.terminal_population,
                groups=groups,
                authority_by_sha=authority_by_sha,
                member_state_ids=(
                    "canonical:loss,win",
                    "canonical:loss,win",
                ),
            )

    def test_all_void_terminal_member_uses_exact_paperbook_refund_semantics(self) -> None:
        void_member = self._binding(
            (
                "canonical:void,void",
                "canonical:void,void",
            )
        )
        baseline_second = self._bindings()[1]
        bindings = (void_member, baseline_second)
        self._issue_mapping_parent(bindings)

        result = self._resolve(bindings)
        stake_a, stake_b = self.precommit.evaluated_stakes

        self.assertEqual(
            result.member_candidate_paper_profit_vectors[0],
            (Decimal("0"), Decimal("0")),
        )
        self.assertEqual(
            result.member_paper_terminal_profits[0],
            Decimal("0"),
        )
        self.assertEqual(
            result.member_candidate_paper_profit_vectors[1],
            (stake_a, -stake_b),
        )
        self.assertEqual(
            result.member_paper_terminal_profits[1],
            stake_a - stake_b,
        )
        self.assertTrue(result.paperbook_settlement_arithmetic_proven)
        self.assertTrue(result.target_terminal_payoff_evaluation_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.risk_upper_bound_for_target)

    def test_negative_evaluated_stake_is_rejected_before_payoff(self) -> None:
        target = terminal_payoff_authority._TARGET_RESOLVER(
            self.workspace,
            self.precommit.target_sha256,
        )
        object.__setattr__(
            target,
            "evaluated_stakes",
            (
                Decimal("-1"),
                target.evaluated_stakes[1],
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "cannot be negative",
        ):
            terminal_payoff_authority._candidate_tickets(target)

    def test_repeated_terminal_member_preserves_multiplicity_not_iid_truth(self) -> None:
        repeated = self._binding(("canonical:loss,win", "canonical:loss,win"))
        bindings = (repeated, repeated)
        self._issue_mapping_parent(bindings)
        result = self._resolve(bindings)

        self.assertEqual(
            result.member_candidate_paper_profit_vectors[0],
            result.member_candidate_paper_profit_vectors[1],
        )
        self.assertEqual(
            result.member_paper_terminal_profits[0],
            result.member_paper_terminal_profits[1],
        )
        self.assertTrue(result.target_terminal_payoff_evaluation_proven)
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertFalse(result.proposal_target_counterfactual_execution_proven)

    def test_direct_or_forged_result_cannot_mint_payoff_authority(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskTerminalPayoffEvaluation()

        forged = object.__new__(ProductProposalRiskTerminalPayoffEvaluation)
        self.assertFalse(forged.evaluation_identity_proven)
        self.assertFalse(forged.terminal_mapping_consumed_proven)
        self.assertFalse(forged.paperbook_settlement_arithmetic_proven)
        self.assertFalse(forged.fixed_n_member_payoff_complete)
        self.assertFalse(forged.target_terminal_payoff_evaluation_proven)
        self.assertFalse(forged.execution_costs_proven)
        self.assertFalse(forged.slippage_realization_proven)
        self.assertFalse(forged.net_execution_pnl_proven)
        self.assertFalse(forged.cashflow_chronology_proven)
        self.assertFalse(forged.scenario_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_internal_validated_values_are_not_authority(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        self.assertFalse(hasattr(terminal_payoff_authority, "_BIND_IDENTITY"))
        self.assertFalse(hasattr(terminal_payoff_authority, "_mint"))

        values = terminal_payoff_authority._resolve_values(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            member_market_state_ids=(
                bindings[0].market_state_ids,
                bindings[1].market_state_ids,
            ),
        )
        self.assertEqual(type(values), dict)
        forged = object.__new__(ProductProposalRiskTerminalPayoffEvaluation)
        for name in terminal_payoff_authority._RESULT_FIELDS_EXPECTED:
            object.__setattr__(forged, name, values[name])
        self.assertFalse(forged.evaluation_identity_proven)
        self.assertFalse(forged.target_terminal_payoff_evaluation_proven)

    def test_post_precommit_terminal_state_substitution_fails_upstream(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_payoff_evaluation(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(
                    ("canonical:loss,win", "canonical:loss,win"),
                    bindings[1].market_state_ids,
                ),
            )

    def test_unknown_terminal_state_fails_before_payoff_arithmetic(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_payoff_evaluation(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(
                    ("canonical:missing", "canonical:loss,win"),
                    bindings[1].market_state_ids,
                ),
            )

    def test_missing_provider_authority_fails_before_payoff_arithmetic(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_payoff_evaluation(
                self.workspace,
                precommit=self.precommit,
                authorities=(self.authorities[0],),
                member_market_state_ids=(
                    bindings[0].market_state_ids,
                    bindings[1].market_state_ids,
                ),
            )

    def test_fixed_n_member_cardinality_is_not_shrunk(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_payoff_evaluation(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(bindings[0].market_state_ids,),
            )

    def test_superseded_target_invalidates_payoff_evaluation(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.7")),
            contexts=getattr(self, "_proposal_risk_contexts"),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalPayoffEvaluationError,
            "terminal mapping cannot be re-resolved",
        ):
            self._resolve(bindings)

    def test_portfolio_settlement_method_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        method = terminal_payoff_authority.PortfolioEngine.scenario_profit_settlements
        original_code = method.__code__

        def forged_profit(_tickets, _settlements):
            return Decimal("999999")

        try:
            method.__code__ = forged_profit.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            method.__code__ = original_code

    def test_portfolio_ticket_fingerprint_rebinding_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = (
            terminal_payoff_authority
            ._portfolio_module
            ._analysis_ticket_fingerprint
        )
        try:
            terminal_payoff_authority._portfolio_module._analysis_ticket_fingerprint = (
                lambda _ticket: ("forged",)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            (
                terminal_payoff_authority
                ._portfolio_module
                ._analysis_ticket_fingerprint
            ) = original

    def test_portfolio_ticket_fingerprint_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        helper = (
            terminal_payoff_authority
            ._portfolio_module
            ._analysis_ticket_fingerprint
        )
        original_code = helper.__code__

        def forged_fingerprint(_ticket):
            return ("forged",)

        try:
            helper.__code__ = forged_fingerprint.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            helper.__code__ = original_code

    def test_portfolio_locked_capital_rebinding_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = (
            terminal_payoff_authority
            ._portfolio_module
            ._CANONICAL_LOCKED_CAPITAL_FOR_TICKET
        )
        try:
            terminal_payoff_authority._portfolio_module._CANONICAL_LOCKED_CAPITAL_FOR_TICKET = (
                lambda _ticket: Decimal("0")
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            (
                terminal_payoff_authority
                ._portfolio_module
                ._CANONICAL_LOCKED_CAPITAL_FOR_TICKET
            ) = original

    def test_portfolio_locked_capital_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        helper = (
            terminal_payoff_authority
            ._portfolio_module
            ._CANONICAL_LOCKED_CAPITAL_FOR_TICKET
        )
        original_code = helper.__code__

        def forged_capital(_ticket):
            return Decimal("0")

        try:
            helper.__code__ = forged_capital.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            helper.__code__ = original_code

    def test_portfolio_locked_capital_calculator_cell_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        helper = (
            terminal_payoff_authority
            ._portfolio_module
            ._CANONICAL_LOCKED_CAPITAL_FOR_TICKET
        )
        calculator = (
            terminal_payoff_authority
            ._portfolio_module
            .locked_capital_for_exchange_side
        )
        closure = helper.__closure__
        self.assertIsNotNone(closure)
        cells = [
            cell
            for cell in closure
            if cell.cell_contents is calculator
        ]
        self.assertEqual(len(cells), 1)
        cell = cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = lambda **_kwargs: Decimal("0")
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_portfolio_exchange_multiply_cell_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        calculator = (
            terminal_payoff_authority
            ._portfolio_module
            .locked_capital_for_exchange_side
        )
        closure = calculator.__closure__
        self.assertIsNotNone(closure)
        cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "_multiply_exact"
        ]
        self.assertEqual(len(cells), 1)
        cell = cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = lambda _left, _right: Decimal("0")
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_paperbook_settlement_method_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        descriptor = terminal_payoff_authority.PaperBook.__dict__[
            "_settlement_result"
        ]
        method = descriptor.__func__
        original_code = method.__code__

        def forged_settlement(_cls, _ticket, balance, _winners, _voids):
            return (
                terminal_payoff_authority.TicketStatus.WON,
                Decimal("999999"),
                balance + Decimal("999999"),
            )

        try:
            method.__code__ = forged_settlement.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            method.__code__ = original_code

    def test_paperbook_leg_validator_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        descriptor = terminal_payoff_authority.PaperBook.__dict__[
            "_validate_ticket_leg"
        ]
        method = descriptor.__func__
        original_code = method.__code__

        def forged_validate(_cls, leg, *, ticket_id=None):
            return leg

        try:
            method.__code__ = forged_validate.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            method.__code__ = original_code

    def test_portfolio_decimal_context_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        context = terminal_payoff_authority._portfolio_module._PORTFOLIO_DECIMAL_CONTEXT
        original_precision = context.prec
        try:
            context.prec = 9
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            context.prec = original_precision

    def test_positive_capability_proof_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        getter = ProductProposalRiskTerminalPayoffEvaluation.__dict__[
            "target_terminal_payoff_evaluation_proven"
        ].fget
        self.assertIsNotNone(getter)
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            proof.__code__ = original_code

    def test_hard_false_execution_getter_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        getter = ProductProposalRiskTerminalPayoffEvaluation.__dict__[
            "scenario_execution_proven"
        ].fget
        self.assertIsNotNone(getter)
        original_code = getter.__code__

        def forged_execution(_self):
            return True

        try:
            getter.__code__ = forged_execution.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            getter.__code__ = original_code

    def test_public_resolver_closure_binder_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_payoff_authority
            .resolve_product_proposal_risk_terminal_payoff_evaluation
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        cell = binder_cells[0]
        original = cell.cell_contents

        def forged_bind(_instance):
            return None

        try:
            cell.cell_contents = forged_bind
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "public resolver closure changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_public_resolver_rejects_module_global_core_rebind(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_payoff_authority._resolve_values
        try:
            terminal_payoff_authority._resolve_values = (
                lambda *args, **kwargs: {
                    "workspace_instance_id": "forged",
                }
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "helper root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_payoff_authority._resolve_values = original

    def test_public_resolver_rejects_captured_core_code_mutation(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_payoff_authority
            .resolve_product_proposal_risk_terminal_payoff_evaluation
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        core_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None)
            == "_resolve_values"
        ]
        self.assertEqual(len(core_cells), 1)
        core = core_cells[0].cell_contents
        original_code = core.__code__

        def forged_core(*_args, **_kwargs):
            return {
                "workspace_instance_id": self.precommit.workspace_instance_id,
            }

        try:
            core.__code__ = forged_core.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "public resolver closure changed",
            ):
                self._resolve(bindings)
        finally:
            core.__code__ = original_code

    def test_protocol_constant_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_payoff_authority._SCHEMA
        try:
            terminal_payoff_authority._SCHEMA = "forged.payoff.v999"
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_payoff_authority._SCHEMA = original


    def test_payoff_json_dumps_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original_code = json.dumps.__code__

        def forged_dumps(*_args, **_kwargs):
            return "{}"

        try:
            json.dumps.__code__ = forged_dumps.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            json.dumps.__code__ = original_code

    def test_payoff_json_loads_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original_code = json.loads.__code__

        def forged_loads(*_args, **_kwargs):
            return {}

        try:
            json.loads.__code__ = forged_loads.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            json.loads.__code__ = original_code

    def test_payoff_dispatch_guard_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        guard = terminal_payoff_authority._REQUIRE_DISPATCH_ORIGINAL
        original_code = guard.__code__

        def forged_guard(*_args, **_kwargs):
            return None

        try:
            guard.__code__ = forged_guard.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch guard root changed",
            ):
                self._resolve(bindings)
        finally:
            guard.__code__ = original_code

    def test_payoff_positive_capability_token_cell_mutation_is_rejected(
        self,
    ) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        getter = ProductProposalRiskTerminalPayoffEvaluation.__dict__[
            "target_terminal_payoff_evaluation_proven"
        ].fget
        proof = getter.__defaults__[0]
        closure = proof.__closure__
        self.assertIsNotNone(closure)
        self.assertEqual(len(closure), 1)
        cell = closure[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_payoff_public_binder_token_cell_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_payoff_authority
            .resolve_product_proposal_risk_terminal_payoff_evaluation
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        binder = binder_cells[0].cell_contents
        binder_closure = binder.__closure__
        self.assertIsNotNone(binder_closure)
        self.assertEqual(len(binder_closure), 1)
        token_cell = binder_closure[0]
        original = token_cell.cell_contents
        try:
            token_cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            token_cell.cell_contents = original

    def test_payoff_result_field_descriptor_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = ProductProposalRiskTerminalPayoffEvaluation.__dict__[
            "evaluation_sha256"
        ]
        try:
            ProductProposalRiskTerminalPayoffEvaluation.evaluation_sha256 = (
                property(lambda _self: "0" * 64)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "result field surface changed",
            ):
                self._resolve(bindings)
        finally:
            ProductProposalRiskTerminalPayoffEvaluation.evaluation_sha256 = original

    def test_payoff_public_result_type_closure_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_payoff_authority
            .resolve_product_proposal_risk_terminal_payoff_evaluation
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        result_type_cells = [
            cell
            for cell in closure
            if cell.cell_contents is ProductProposalRiskTerminalPayoffEvaluation
        ]
        self.assertEqual(len(result_type_cells), 1)
        cell = result_type_cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_payoff_public_resolver_snapshots_closure_before_core(
        self,
    ) -> None:
        resolver = (
            terminal_payoff_authority
            .resolve_product_proposal_risk_terminal_payoff_evaluation
        )
        instructions = list(dis.get_instructions(resolver))
        values_store_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_FAST"
            and instruction.argval == "values"
        )
        snapshot_locals = (
            "bind_identity",
            "resolve_core",
            "result_type",
            "result_fields",
            "expected_bind_code",
            "expected_core_code",
        )
        for local_name in snapshot_locals:
            with self.subTest(local_name=local_name):
                stores = [
                    index
                    for index, instruction in enumerate(instructions)
                    if instruction.opname == "STORE_FAST"
                    and instruction.argval == local_name
                ]
                self.assertEqual(len(stores), 1)
                self.assertLess(stores[0], values_store_index)

        mutable_cells = {
            "_bind_identity",
            "_resolve_core",
            "_result_type",
            "_result_fields",
            "_bind_code",
            "_resolve_core_code",
        }
        late_reads = [
            instruction.argval
            for instruction in instructions[values_store_index + 1 :]
            if instruction.opname == "LOAD_DEREF"
            and instruction.argval in mutable_cells
        ]
        self.assertEqual(late_reads, [])

    def test_payoff_helper_witness_table_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_payoff_authority._HELPER_WITNESSES_EXPECTED
        try:
            terminal_payoff_authority._HELPER_WITNESSES_EXPECTED = tuple(
                list(original)
            )
            self.assertIsNot(
                terminal_payoff_authority._HELPER_WITNESSES_EXPECTED,
                original,
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_payoff_authority._HELPER_WITNESSES_EXPECTED = original

    def test_payoff_helper_kwdefault_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        kwdefaults = terminal_payoff_authority._text.__kwdefaults__
        self.assertIsNotNone(kwdefaults)
        original = kwdefaults["max_length"]
        try:
            kwdefaults["max_length"] = original + 1
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "helper root changed",
            ):
                self._resolve(bindings)
        finally:
            kwdefaults["max_length"] = original

    def test_payoff_paper_context_type_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_payoff_authority._paper_module.Context
        try:
            terminal_payoff_authority._paper_module.Context = (
                lambda *args, **kwargs: original(*args, **kwargs)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalPayoffEvaluationError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_payoff_authority._paper_module.Context = original


import autosport.proposal_risk_terminal_component_provenance_authority as terminal_component_provenance_authority
from autosport.proposal_risk_terminal_component_provenance_authority import (
    ProductProposalRiskTerminalComponentProvenance,
    ProductProposalRiskTerminalComponentProvenanceError,
    resolve_product_proposal_risk_terminal_component_provenance,
)


class ProductProposalRiskTerminalComponentProvenanceTests(unittest.TestCase):

    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.authorities = getattr(self, "_proposal_terminal_authorities")
        self.assertFalse(self.terminal_population.terminal_space_exact)

    def _binding(
        self,
        state_ids: tuple[str, ...],
    ):
        return derive_product_proposal_terminal_scenario_binding(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            market_state_ids=state_ids,
        )

    def _bindings(self):
        return (
            self._binding(("canonical:win,loss", "canonical:loss,win")),
            self._binding(("canonical:loss,win", "canonical:win,loss")),
        )

    def _issue_mapping_parent(self, bindings=None) -> None:
        first, second = bindings or self._bindings()
        issue_product_proposal_risk_scenario_population(
            self.workspace,
            self.precommit,
            self.terminal_population,
            (
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[0],
                    scenario_id=first.scenario_id,
                    mapping_sha256=first.mapping_sha256,
                ),
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[1],
                    scenario_id=second.scenario_id,
                    mapping_sha256=second.mapping_sha256,
                ),
            ),
        )

    def _resolve(
        self,
        bindings=None,
    ) -> ProductProposalRiskTerminalComponentProvenance:
        first, second = bindings or self._bindings()
        return resolve_product_proposal_risk_terminal_component_provenance(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            member_market_state_ids=(
                first.market_state_ids,
                second.market_state_ids,
            ),
        )

    def test_provider_terminal_component_provenance_is_positive_but_non_authorizing(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        result = self._resolve(bindings)

        self.assertTrue(result.provenance_identity_proven)
        self.assertTrue(result.provider_terminal_population_proven)
        self.assertTrue(result.terminal_mapping_proven)
        self.assertTrue(result.target_terminal_payoff_evaluation_proven)
        self.assertTrue(result.provider_terminal_component_provenance_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertEqual(
            result.source_protocol,
            (
                "provider-terminal-authority-as-of-target+"
                "durable-scenario-precommit+exact-terminal-mapping.v1"
            ),
        )
        self.assertEqual(
            result.terminal_population_sha256,
            self.terminal_population.population_sha256,
        )
        self.assertEqual(
            result.market_authority_sha256s,
            self.terminal_population.market_authority_sha256s,
        )
        self.assertEqual(
            result.market_group_sha256s,
            self.terminal_population.market_group_sha256s,
        )
        self.assertEqual(
            result.member_scenario_ids,
            tuple(binding.scenario_id for binding in bindings),
        )
        self.assertEqual(
            result.member_mapping_sha256s,
            tuple(binding.mapping_sha256 for binding in bindings),
        )
        self.assertEqual(
            result.member_state_vector_sha256s,
            tuple(binding.state_vector_sha256 for binding in bindings),
        )
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.scenario_selection_law_proven)
        self.assertFalse(result.minimum_equity_path_proven)
        self.assertFalse(result.execution_costs_proven)
        self.assertFalse(result.slippage_realization_proven)
        self.assertFalse(result.net_execution_pnl_proven)
        self.assertFalse(result.cashflow_chronology_proven)
        self.assertFalse(result.scenario_execution_proven)
        self.assertFalse(result.proposal_target_counterfactual_execution_proven)
        self.assertFalse(result.risk_upper_bound_for_target)
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_ticket_authority)
        self.assertFalse(result.grants_broker_execution_authority)
        self.assertFalse(result.grants_real_money_authority)
        self.assertFalse(result.grants_state_mutation_authority)
        self.assertEqual(len(result.provenance_sha256), 64)
        self.assertEqual(self._resolve(bindings), result)

    def test_conservative_all_void_components_do_not_mint_support_truth(self) -> None:
        void_member = self._binding(
            ("canonical:void,void", "canonical:void,void")
        )
        bindings = (void_member, void_member)
        self._issue_mapping_parent(bindings)
        result = self._resolve(bindings)

        self.assertTrue(result.provider_terminal_component_provenance_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.scenario_selection_law_proven)
        self.assertFalse(result.risk_upper_bound_for_target)

    def test_repeated_member_preserves_multiplicity_without_sampling_truth(self) -> None:
        repeated = self._binding(("canonical:loss,win", "canonical:win,loss"))
        bindings = (repeated, repeated)
        self._issue_mapping_parent(bindings)
        result = self._resolve(bindings)

        self.assertTrue(result.provider_terminal_component_provenance_proven)
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertEqual(
            result.member_scenario_ids,
            (repeated.scenario_id, repeated.scenario_id),
        )
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.scenario_selection_law_proven)

    def test_direct_or_forged_result_cannot_mint_provenance(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskTerminalComponentProvenance()

        forged = object.__new__(ProductProposalRiskTerminalComponentProvenance)
        self.assertFalse(forged.provenance_identity_proven)
        self.assertFalse(forged.provider_terminal_population_proven)
        self.assertFalse(forged.terminal_mapping_proven)
        self.assertFalse(forged.target_terminal_payoff_evaluation_proven)
        self.assertFalse(forged.product_scenario_source_provenance_proven)
        self.assertFalse(forged.scenario_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_post_precommit_state_vector_substitution_fails_upstream(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalComponentProvenanceError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_component_provenance(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(
                    ("canonical:loss,win", "canonical:loss,win"),
                    bindings[1].market_state_ids,
                ),
            )

    def test_missing_reverified_provider_authority_fails_closed(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalComponentProvenanceError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_component_provenance(
                self.workspace,
                precommit=self.precommit,
                authorities=(self.authorities[0],),
                member_market_state_ids=(
                    bindings[0].market_state_ids,
                    bindings[1].market_state_ids,
                ),
            )

    def test_fixed_n_member_cardinality_cannot_shrink(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalComponentProvenanceError,
            "terminal mapping cannot be re-resolved",
        ):
            resolve_product_proposal_risk_terminal_component_provenance(
                self.workspace,
                precommit=self.precommit,
                authorities=self.authorities,
                member_market_state_ids=(bindings[0].market_state_ids,),
            )

    def test_superseded_target_invalidates_provenance(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.7")),
            contexts=getattr(self, "_proposal_risk_contexts"),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTerminalComponentProvenanceError,
            "terminal mapping cannot be re-resolved",
        ):
            self._resolve(bindings)

    def test_protocol_constant_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_component_provenance_authority._SOURCE_PROTOCOL
        try:
            terminal_component_provenance_authority._SOURCE_PROTOCOL = (
                "forged-source-protocol"
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_component_provenance_authority._SOURCE_PROTOCOL = original

    def test_hash_dispatch_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_component_provenance_authority.hashlib.sha256
        try:
            terminal_component_provenance_authority.hashlib.sha256 = (
                lambda *args, **kwargs: None
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_component_provenance_authority.hashlib.sha256 = original

    def test_provider_authority_getter_alias_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_component_provenance_authority._AUTHORITY_SHA_GETTER
        try:
            terminal_component_provenance_authority._AUTHORITY_SHA_GETTER = (
                lambda _authority: "0" * 64
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_component_provenance_authority._AUTHORITY_SHA_GETTER = original

    def test_positive_capability_default_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        getter = ProductProposalRiskTerminalComponentProvenance.__dict__[
            "provider_terminal_component_provenance_proven"
        ].fget
        self.assertIsNotNone(getter)
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            proof.__code__ = original_code

    def test_hard_false_execution_getter_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        getter = ProductProposalRiskTerminalComponentProvenance.__dict__[
            "scenario_execution_proven"
        ].fget
        self.assertIsNotNone(getter)
        original_code = getter.__code__

        def forged_execution(_self):
            return True

        try:
            getter.__code__ = forged_execution.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            getter.__code__ = original_code

    def test_module_global_core_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_component_provenance_authority._resolve_values
        try:
            terminal_component_provenance_authority._resolve_values = (
                lambda *args, **kwargs: {"workspace_instance_id": "forged"}
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "helper root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_component_provenance_authority._resolve_values = original

    def test_public_resolver_closure_binder_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_component_provenance_authority
            .resolve_product_proposal_risk_terminal_component_provenance
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        cell = binder_cells[0]
        original = cell.cell_contents

        def forged_bind(_instance):
            return None

        try:
            cell.cell_contents = forged_bind
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "public resolver closure changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_public_resolver_rejects_captured_core_code_mutation(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_component_provenance_authority
            .resolve_product_proposal_risk_terminal_component_provenance
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        core_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "_resolve_values"
        ]
        self.assertEqual(len(core_cells), 1)
        core = core_cells[0].cell_contents
        original_code = core.__code__

        def forged_core(*_args, **_kwargs):
            return {"workspace_instance_id": self.precommit.workspace_instance_id}

        try:
            core.__code__ = forged_core.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "public resolver closure changed",
            ):
                self._resolve(bindings)
        finally:
            core.__code__ = original_code


    def test_component_json_dumps_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original_code = json.dumps.__code__

        def forged_dumps(*_args, **_kwargs):
            return "{}"

        try:
            json.dumps.__code__ = forged_dumps.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            json.dumps.__code__ = original_code

    def test_component_dispatch_guard_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        guard = terminal_component_provenance_authority._REQUIRE_DISPATCH_ORIGINAL
        original_code = guard.__code__

        def forged_guard(*_args, **_kwargs):
            return None

        try:
            guard.__code__ = forged_guard.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "dispatch guard root changed",
            ):
                self._resolve(bindings)
        finally:
            guard.__code__ = original_code

    def test_component_positive_capability_token_cell_mutation_is_rejected(
        self,
    ) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        getter = ProductProposalRiskTerminalComponentProvenance.__dict__[
            "provider_terminal_component_provenance_proven"
        ].fget
        proof = getter.__defaults__[0]
        closure = proof.__closure__
        self.assertIsNotNone(closure)
        self.assertEqual(len(closure), 1)
        cell = closure[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_component_public_binder_token_cell_mutation_is_rejected(
        self,
    ) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_component_provenance_authority
            .resolve_product_proposal_risk_terminal_component_provenance
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        binder = binder_cells[0].cell_contents
        binder_closure = binder.__closure__
        self.assertIsNotNone(binder_closure)
        self.assertEqual(len(binder_closure), 1)
        token_cell = binder_closure[0]
        original = token_cell.cell_contents
        try:
            token_cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            token_cell.cell_contents = original

    def test_component_result_field_descriptor_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = ProductProposalRiskTerminalComponentProvenance.__dict__[
            "provenance_sha256"
        ]
        try:
            ProductProposalRiskTerminalComponentProvenance.provenance_sha256 = (
                property(lambda _self: "0" * 64)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "result field surface changed",
            ):
                self._resolve(bindings)
        finally:
            ProductProposalRiskTerminalComponentProvenance.provenance_sha256 = (
                original
            )

    def test_component_public_result_type_closure_mutation_is_rejected(
        self,
    ) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        resolver = (
            terminal_component_provenance_authority
            .resolve_product_proposal_risk_terminal_component_provenance
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        result_type_cells = [
            cell
            for cell in closure
            if cell.cell_contents is ProductProposalRiskTerminalComponentProvenance
        ]
        self.assertEqual(len(result_type_cells), 1)
        cell = result_type_cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_component_public_resolver_snapshots_closure_before_core(
        self,
    ) -> None:
        resolver = (
            terminal_component_provenance_authority
            .resolve_product_proposal_risk_terminal_component_provenance
        )
        instructions = list(dis.get_instructions(resolver))
        values_store_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_FAST"
            and instruction.argval == "values"
        )
        snapshot_locals = (
            "bind_identity",
            "resolve_core",
            "result_type",
            "result_fields",
            "expected_bind_code",
            "expected_core_code",
        )
        for local_name in snapshot_locals:
            with self.subTest(local_name=local_name):
                stores = [
                    index
                    for index, instruction in enumerate(instructions)
                    if instruction.opname == "STORE_FAST"
                    and instruction.argval == local_name
                ]
                self.assertEqual(len(stores), 1)
                self.assertLess(stores[0], values_store_index)

        mutable_cells = {
            "_bind_identity",
            "_resolve_core",
            "_result_type",
            "_result_fields",
            "_bind_code",
            "_resolve_core_code",
        }
        late_reads = [
            instruction.argval
            for instruction in instructions[values_store_index + 1 :]
            if instruction.opname == "LOAD_DEREF"
            and instruction.argval in mutable_cells
        ]
        self.assertEqual(late_reads, [])

    def test_component_helper_witness_table_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        original = terminal_component_provenance_authority._HELPER_WITNESSES_EXPECTED
        try:
            terminal_component_provenance_authority._HELPER_WITNESSES_EXPECTED = (
                tuple(list(original))
            )
            self.assertIsNot(
                terminal_component_provenance_authority._HELPER_WITNESSES_EXPECTED,
                original,
            )
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            terminal_component_provenance_authority._HELPER_WITNESSES_EXPECTED = (
                original
            )

    def test_component_helper_kwdefault_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue_mapping_parent(bindings)
        kwdefaults = terminal_component_provenance_authority._text.__kwdefaults__
        self.assertIsNotNone(kwdefaults)
        original = kwdefaults["max_length"]
        try:
            kwdefaults["max_length"] = original + 1
            with self.assertRaisesRegex(
                ProductProposalRiskTerminalComponentProvenanceError,
                "helper root changed",
            ):
                self._resolve(bindings)
        finally:
            kwdefaults["max_length"] = original


import autosport.proposal_risk_counterfactual_cash_floor_authority as cash_floor_authority
from autosport.proposal_risk_counterfactual_cash_floor_authority import (
    ProductProposalRiskCounterfactualCashFloor,
    ProductProposalRiskCounterfactualCashFloorError,
    resolve_product_proposal_risk_counterfactual_cash_floor,
)
from autosport.proposal_risk_terminal_payoff_authority import (
    resolve_product_proposal_risk_terminal_payoff_evaluation,
)


class ProductProposalRiskCounterfactualCashFloorTests(unittest.TestCase):

    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.authorities = getattr(self, "_proposal_terminal_authorities")

    def _binding(self, state_ids: tuple[str, ...]):
        return derive_product_proposal_terminal_scenario_binding(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            market_state_ids=state_ids,
        )

    def _bindings(self):
        return (
            self._binding(("canonical:win,loss", "canonical:loss,win")),
            self._binding(("canonical:loss,win", "canonical:win,loss")),
        )

    def _issue(self, bindings=None) -> None:
        first, second = bindings or self._bindings()
        issue_product_proposal_risk_scenario_population(
            self.workspace,
            self.precommit,
            self.terminal_population,
            (
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[0],
                    scenario_id=first.scenario_id,
                    mapping_sha256=first.mapping_sha256,
                ),
                CounterfactualScenarioMemberBinding(
                    member_id=self.precommit.planned_member_ids[1],
                    scenario_id=second.scenario_id,
                    mapping_sha256=second.mapping_sha256,
                ),
            ),
        )

    def _resolve(self, bindings=None) -> ProductProposalRiskCounterfactualCashFloor:
        first, second = bindings or self._bindings()
        return resolve_product_proposal_risk_counterfactual_cash_floor(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            member_market_state_ids=(
                first.market_state_ids,
                second.market_state_ids,
            ),
        )

    def test_conservative_cash_floor_is_positive_but_not_execution_or_risk(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        payoff = resolve_product_proposal_risk_terminal_payoff_evaluation(
            self.workspace,
            precommit=self.precommit,
            authorities=self.authorities,
            member_market_state_ids=(
                bindings[0].market_state_ids,
                bindings[1].market_state_ids,
            ),
        )
        result = self._resolve(bindings)
        stake_a, stake_b = self.precommit.evaluated_stakes
        base = PaperBook.load(self.workspace / "paper_book.json").balance
        after_a = PaperBook._debit_balance(base, stake_a)
        post_open = PaperBook._debit_balance(after_a, stake_b)

        self.assertTrue(result.evaluation_identity_proven)
        self.assertTrue(result.base_portfolio_identity_proven)
        self.assertTrue(result.counterfactual_target_stake_reservation_proven)
        self.assertTrue(result.counterfactual_target_capital_reservation_proven)
        self.assertEqual(result.evaluated_capital_at_risk, result.evaluated_stakes)
        self.assertTrue(result.terminal_payout_reconstruction_proven)
        self.assertTrue(result.counterfactual_minimum_cash_floor_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertEqual(
            result.path_protocol,
            "paperbook.cash-floor.open-all-capital-before-settlement.v2",
        )
        self.assertEqual(result.base_cash_balance, base)
        self.assertEqual(
            result.candidate_open_cash_balances,
            (after_a, post_open),
        )
        self.assertEqual(result.post_open_cash_balance, post_open)
        self.assertEqual(
            result.member_candidate_payout_vectors,
            (
                (Decimal("0"), stake_b * Decimal("2")),
                (stake_a * Decimal("2"), Decimal("0")),
            ),
        )
        self.assertEqual(
            result.member_terminal_cash_balances,
            (
                base + payoff.member_paper_terminal_profits[0],
                base + payoff.member_paper_terminal_profits[1],
            ),
        )
        self.assertEqual(
            result.member_minimum_cash_floors,
            (post_open, post_open),
        )
        self.assertFalse(result.product_scenario_source_provenance_proven)
        self.assertFalse(result.scenario_selection_law_proven)
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.joint_scenario_support_proven)
        self.assertFalse(result.minimum_equity_path_proven)
        self.assertFalse(result.cashflow_chronology_proven)
        self.assertFalse(result.execution_costs_proven)
        self.assertFalse(result.slippage_realization_proven)
        self.assertFalse(result.net_execution_pnl_proven)
        self.assertFalse(result.scenario_execution_proven)
        self.assertFalse(result.proposal_target_counterfactual_execution_proven)
        self.assertFalse(result.risk_upper_bound_for_target)
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_ticket_authority)
        self.assertFalse(result.grants_broker_execution_authority)
        self.assertFalse(result.grants_real_money_authority)
        self.assertFalse(result.grants_state_mutation_authority)
        self.assertEqual(len(result.evaluation_sha256), 64)
        self.assertEqual(self._resolve(bindings), result)

    def test_public_cash_floor_resolver_reserves_lay_liability_end_to_end(self) -> None:
        self.precommit = _canonical_precommit(self, lay_first=True)
        self.workspace = getattr(self, "_proposal_risk_workspace")
        self.terminal_population = getattr(self, "_proposal_terminal_population")
        self.authorities = getattr(self, "_proposal_terminal_authorities")
        bindings = self._bindings()
        self._issue(bindings)

        result = self._resolve(bindings)
        stake_a, stake_b = self.precommit.evaluated_stakes
        liability_a = stake_a * Decimal("4")
        base = result.base_cash_balance
        post_open = base - liability_a - stake_b

        self.assertEqual(
            result.evaluated_capital_at_risk,
            (liability_a, stake_b),
        )
        self.assertTrue(result.counterfactual_target_capital_reservation_proven)
        self.assertEqual(
            result.candidate_open_cash_balances,
            (base - liability_a, post_open),
        )
        self.assertEqual(result.post_open_cash_balance, post_open)
        self.assertEqual(
            result.member_candidate_payout_vectors,
            (
                (liability_a + stake_a, stake_b * Decimal("2")),
                (Decimal("0"), Decimal("0")),
            ),
        )
        self.assertEqual(
            result.member_terminal_cash_balances,
            (
                base + stake_a + stake_b,
                post_open,
            ),
        )
        self.assertEqual(
            result.member_minimum_cash_floors,
            (post_open, post_open),
        )
        self.assertFalse(result.grants_risk_approval_authority)
        self.assertFalse(result.grants_real_money_authority)

    def test_all_void_refunds_restore_base_cash_but_floor_remains_post_open(self) -> None:
        void_member = self._binding(
            ("canonical:void,void", "canonical:void,void")
        )
        bindings = (void_member, void_member)
        self._issue(bindings)
        result = self._resolve(bindings)
        stake_a, stake_b = self.precommit.evaluated_stakes
        base = result.base_cash_balance

        self.assertEqual(
            result.member_candidate_payout_vectors,
            (
                (stake_a, stake_b),
                (stake_a, stake_b),
            ),
        )
        self.assertEqual(
            result.member_terminal_cash_balances,
            (base, base),
        )
        self.assertEqual(
            result.member_minimum_cash_floors,
            (result.post_open_cash_balance, result.post_open_cash_balance),
        )
        self.assertTrue(result.counterfactual_minimum_cash_floor_proven)
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertFalse(result.minimum_equity_path_proven)
        self.assertFalse(result.risk_upper_bound_for_target)

    def test_repeated_member_preserves_cash_arithmetic_without_iid_truth(self) -> None:
        repeated = self._binding(
            ("canonical:loss,win", "canonical:win,loss")
        )
        bindings = (repeated, repeated)
        self._issue(bindings)
        result = self._resolve(bindings)

        self.assertEqual(
            result.member_candidate_payout_vectors[0],
            result.member_candidate_payout_vectors[1],
        )
        self.assertEqual(
            result.member_terminal_cash_balances[0],
            result.member_terminal_cash_balances[1],
        )
        self.assertFalse(result.per_market_terminal_space_exact)
        self.assertFalse(result.joint_terminal_space_exact)
        self.assertFalse(result.iid_member_mapping_proven)
        self.assertFalse(result.scenario_selection_law_proven)
        self.assertFalse(result.risk_upper_bound_for_target)

    def test_lay_cash_floor_reserves_liability_not_stake(self) -> None:
        target = cash_floor_authority._TARGET_RESOLVER(
            self.workspace,
            self.precommit.target_sha256,
        )
        contexts = list(target.candidate_context_json)
        payload = json.loads(contexts[0])
        payload["legs"][0]["locked_odds"] = "5"
        payload["legs"][0]["exchange_side"] = "lay"
        payload["legs"][0]["market_semantics_id"] = "exchange.match.odds.v1"
        contexts[0] = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(target, "candidate_context_json", tuple(contexts))

        capital = cash_floor_authority._target_capital_vector(target)
        stake_a, stake_b = target.evaluated_stakes
        self.assertEqual(capital[0], stake_a * Decimal("4"))
        self.assertEqual(capital[1], stake_b)

        base = Decimal("100")
        balances, post_open = cash_floor_authority._open_target_stakes(
            base,
            capital,
        )
        self.assertEqual(balances[0], base - capital[0])
        self.assertEqual(post_open, base - capital[0] - capital[1])

        payouts, terminal, minimum = cash_floor_authority._member_cash_path(
            base_balance=base,
            post_open_balance=post_open,
            capital_at_risk=capital,
            candidate_profits=(-capital[0], Decimal("0")),
            total_profit=-capital[0],
            member_index=0,
        )
        self.assertEqual(payouts, (Decimal("0"), capital[1]))
        self.assertEqual(terminal, base - capital[0])
        self.assertEqual(minimum, post_open)

    def test_unfundable_target_stake_vector_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "not cash-fundable",
        ):
            cash_floor_authority._open_target_stakes(
                Decimal("1"),
                (Decimal("2"),),
            )

    def test_open_target_stakes_requires_nonempty_exact_tuple(self) -> None:
        for invalid in ([], ()):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProductProposalRiskCounterfactualCashFloorError,
                    "non-empty exact tuple",
                ):
                    cash_floor_authority._open_target_stakes(
                        Decimal("10"),
                        invalid,
                    )

    def test_open_target_stakes_rejects_negative_base_cash(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "base cash balance cannot be negative",
        ):
            cash_floor_authority._open_target_stakes(
                Decimal("-1"),
                (Decimal("0"),),
            )

    def test_zero_stakes_preserve_cash_reservation_and_floor(self) -> None:
        balances, post_open = cash_floor_authority._open_target_stakes(
            Decimal("10"),
            (Decimal("0"), Decimal("2"), Decimal("0")),
        )
        self.assertEqual(
            balances,
            (Decimal("10"), Decimal("8"), Decimal("8")),
        )
        self.assertEqual(post_open, Decimal("8"))

        payouts, terminal, minimum = cash_floor_authority._member_cash_path(
            base_balance=Decimal("10"),
            post_open_balance=post_open,
            capital_at_risk=(Decimal("0"), Decimal("2"), Decimal("0")),
            candidate_profits=(
                Decimal("0"),
                Decimal("-2"),
                Decimal("0"),
            ),
            total_profit=Decimal("-2"),
            member_index=0,
        )
        self.assertEqual(
            payouts,
            (Decimal("0"), Decimal("0"), Decimal("0")),
        )
        self.assertEqual(terminal, Decimal("8"))
        self.assertEqual(minimum, Decimal("8"))

    def test_exact_add_rejects_decimal_precision_loss(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "loses Decimal precision",
        ):
            cash_floor_authority._exact_add(
                Decimal("1.234567890123456789012345678"),
                Decimal("0.0000000000000000000000000001"),
                "precision-falsifier",
            )

    def test_cash_path_rejects_nonexact_member_index(self) -> None:
        for invalid in (True, 1.0):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProductProposalRiskCounterfactualCashFloorError,
                    "non-negative exact integer",
                ):
                    cash_floor_authority._member_cash_path(
                        base_balance=Decimal("10"),
                        post_open_balance=Decimal("9"),
                        capital_at_risk=(Decimal("1"),),
                        candidate_profits=(Decimal("-1"),),
                        total_profit=Decimal("-1"),
                        member_index=invalid,
                    )

    def test_cash_path_rejects_negative_member_stake(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "member target capital at risk cannot be negative",
        ):
            cash_floor_authority._member_cash_path(
                base_balance=Decimal("10"),
                post_open_balance=Decimal("9"),
                capital_at_risk=(Decimal("-1"),),
                candidate_profits=(Decimal("1"),),
                total_profit=Decimal("0"),
                member_index=0,
            )

    def test_cash_path_rejects_impossible_post_open_balance(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "invalid base/post-open cash boundary",
        ):
            cash_floor_authority._member_cash_path(
                base_balance=Decimal("10"),
                post_open_balance=Decimal("11"),
                capital_at_risk=(Decimal("1"),),
                candidate_profits=(Decimal("0"),),
                total_profit=Decimal("0"),
                member_index=0,
            )

    def test_cash_path_requires_exact_tuple_stakes(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "lost target cardinality",
        ):
            cash_floor_authority._member_cash_path(
                base_balance=Decimal("10"),
                post_open_balance=Decimal("9"),
                capital_at_risk=[Decimal("1")],
                candidate_profits=(Decimal("0"),),
                total_profit=Decimal("0"),
                member_index=0,
            )

    def test_negative_reconstructed_payout_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "negative PaperBook payout",
        ):
            cash_floor_authority._member_cash_path(
                base_balance=Decimal("10"),
                post_open_balance=Decimal("9"),
                capital_at_risk=(Decimal("1"),),
                candidate_profits=(Decimal("-2"),),
                total_profit=Decimal("-2"),
                member_index=0,
            )

    def test_direct_or_forged_result_cannot_mint_cash_floor_truth(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskCounterfactualCashFloor()

        forged = object.__new__(ProductProposalRiskCounterfactualCashFloor)
        self.assertFalse(forged.evaluation_identity_proven)
        self.assertFalse(forged.base_portfolio_identity_proven)
        self.assertFalse(forged.counterfactual_target_stake_reservation_proven)
        self.assertFalse(forged.terminal_payout_reconstruction_proven)
        self.assertFalse(forged.counterfactual_minimum_cash_floor_proven)
        self.assertFalse(forged.minimum_equity_path_proven)
        self.assertFalse(forged.proposal_target_counterfactual_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_superseded_target_invalidates_cash_floor(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.7")),
            contexts=getattr(self, "_proposal_risk_contexts"),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskCounterfactualCashFloorError,
            "terminal component provenance cannot be re-resolved",
        ):
            self._resolve(bindings)

    def test_protocol_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._PATH_PROTOCOL
        try:
            cash_floor_authority._PATH_PROTOCOL = "forged-path"
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._PATH_PROTOCOL = original

    def test_debit_function_alias_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._BOOK_DEBIT_FUNCTION
        try:
            cash_floor_authority._BOOK_DEBIT_FUNCTION = (
                lambda *_args, **_kwargs: Decimal("999")
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._BOOK_DEBIT_FUNCTION = original

    def test_positive_capability_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        getter = ProductProposalRiskCounterfactualCashFloor.__dict__[
            "counterfactual_minimum_cash_floor_proven"
        ].fget
        proof = getter.__defaults__[0]
        original_code = proof.__code__

        def forged_proof(_instance):
            return True

        try:
            proof.__code__ = forged_proof.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            proof.__code__ = original_code

    def test_hard_false_risk_getter_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        getter = ProductProposalRiskCounterfactualCashFloor.__dict__[
            "risk_upper_bound_for_target"
        ].fget
        original_code = getter.__code__

        def forged_risk(_instance):
            return True

        try:
            getter.__code__ = forged_risk.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            getter.__code__ = original_code

    def test_result_type_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._RESULT_TYPE
        try:
            cash_floor_authority._RESULT_TYPE = object
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._RESULT_TYPE = original

    def test_module_core_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._resolve_values
        try:
            cash_floor_authority._resolve_values = lambda *args, **kwargs: {}
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "helper root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._resolve_values = original

    def test_json_dumps_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original_code = json.dumps.__code__

        def forged_dumps(*_args, **_kwargs):
            return "{}"

        try:
            json.dumps.__code__ = forged_dumps.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            json.dumps.__code__ = original_code

    def test_dispatch_guard_code_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        guard = cash_floor_authority._REQUIRE_DISPATCH_ORIGINAL
        original_code = guard.__code__

        def forged_guard(*_args, **_kwargs):
            return None

        try:
            guard.__code__ = forged_guard.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch guard root changed",
            ):
                self._resolve(bindings)
        finally:
            guard.__code__ = original_code

    def test_positive_capability_token_cell_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        getter = ProductProposalRiskCounterfactualCashFloor.__dict__[
            "counterfactual_minimum_cash_floor_proven"
        ].fget
        proof = getter.__defaults__[0]
        closure = proof.__closure__
        self.assertIsNotNone(closure)
        self.assertEqual(len(closure), 1)
        cell = closure[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "result authority surface changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_public_resolver_binder_token_cell_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        resolver = (
            cash_floor_authority
            .resolve_product_proposal_risk_counterfactual_cash_floor
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        binder_cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "bind"
        ]
        self.assertEqual(len(binder_cells), 1)
        binder = binder_cells[0].cell_contents
        binder_closure = binder.__closure__
        self.assertIsNotNone(binder_closure)
        self.assertEqual(len(binder_closure), 1)
        token_cell = binder_closure[0]
        original = token_cell.cell_contents
        try:
            token_cell.cell_contents = object()
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            token_cell.cell_contents = original

    def test_result_field_descriptor_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = ProductProposalRiskCounterfactualCashFloor.__dict__[
            "evaluation_sha256"
        ]
        try:
            ProductProposalRiskCounterfactualCashFloor.evaluation_sha256 = (
                property(lambda _self: "0" * 64)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "result field surface changed",
            ):
                self._resolve(bindings)
        finally:
            ProductProposalRiskCounterfactualCashFloor.evaluation_sha256 = original

    def test_paper_decimal_precision_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._paper_module._PAPER_DECIMAL_PRECISION
        try:
            cash_floor_authority._paper_module._PAPER_DECIMAL_PRECISION = (
                original + 1
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._paper_module._PAPER_DECIMAL_PRECISION = original

    def test_paper_context_type_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._paper_module.Context
        try:
            cash_floor_authority._paper_module.Context = (
                lambda *args, **kwargs: original(*args, **kwargs)
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._paper_module.Context = original

    def test_public_resolver_result_type_closure_mutation_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        resolver = (
            cash_floor_authority
            .resolve_product_proposal_risk_counterfactual_cash_floor
        )
        closure = resolver.__closure__
        self.assertIsNotNone(closure)
        result_type_cells = [
            cell
            for cell in closure
            if cell.cell_contents is ProductProposalRiskCounterfactualCashFloor
        ]
        self.assertEqual(len(result_type_cells), 1)
        cell = result_type_cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = object
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "public resolver root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_public_resolver_snapshots_construction_closure_before_core(
        self,
    ) -> None:
        resolver = (
            cash_floor_authority
            .resolve_product_proposal_risk_counterfactual_cash_floor
        )
        instructions = list(dis.get_instructions(resolver))
        values_store_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_FAST"
            and instruction.argval == "values"
        )

        snapshot_locals = (
            "bind_identity",
            "resolve_core",
            "result_type",
            "result_fields",
            "expected_bind_code",
            "expected_core_code",
        )
        for local_name in snapshot_locals:
            with self.subTest(local_name=local_name):
                store_indices = [
                    index
                    for index, instruction in enumerate(instructions)
                    if instruction.opname == "STORE_FAST"
                    and instruction.argval == local_name
                ]
                self.assertEqual(len(store_indices), 1)
                self.assertLess(store_indices[0], values_store_index)

        mutable_construction_cells = {
            "_bind_identity",
            "_resolve_core",
            "_result_type",
            "_result_fields",
            "bind_code",
            "core_code",
        }
        late_closure_reads = [
            instruction.argval
            for instruction in instructions[values_store_index + 1 :]
            if instruction.opname == "LOAD_DEREF"
            and instruction.argval in mutable_construction_cells
        ]
        self.assertEqual(late_closure_reads, [])

    def test_helper_witness_table_rebind_is_rejected(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._HELPER_WITNESSES_EXPECTED
        try:
            cash_floor_authority._HELPER_WITNESSES_EXPECTED = tuple(
                list(original)
            )
            self.assertIsNot(
                cash_floor_authority._HELPER_WITNESSES_EXPECTED,
                original,
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._HELPER_WITNESSES_EXPECTED = original

    def test_cash_floor_rejects_risk_portfolio_hash_helper_substitution(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._risk_module._sha256_payload
        try:
            cash_floor_authority._risk_module._sha256_payload = (
                lambda payload: "0" * 64
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._risk_module._sha256_payload = original

    def test_cash_floor_rejects_paperbook_state_validator_substitution(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = PaperBook.__dict__["_validate_loaded_state"]
        try:
            PaperBook._validate_loaded_state = classmethod(
                lambda cls, book: None
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            setattr(PaperBook, "_validate_loaded_state", original)

    def test_cash_floor_rejects_lifecycle_validator_substitution(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = PaperBook.__dict__["_validate_lifecycle_entry"]
        try:
            PaperBook._validate_lifecycle_entry = classmethod(
                lambda cls, entry: entry
            )
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            setattr(PaperBook, "_validate_lifecycle_entry", original)

    def test_cash_floor_rejects_risk_module_paperbook_rebinding(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority._risk_module.PaperBook
        try:
            cash_floor_authority._risk_module.PaperBook = object
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority._risk_module.PaperBook = original


    def test_cash_floor_rejects_locked_capital_calculator_cell_mutation(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        helper = cash_floor_authority._risk_module._CANONICAL_LOCKED_CAPITAL_FOR_PROPOSAL
        calculator = cash_floor_authority._risk_module.locked_capital_for_exchange_side
        closure = helper.__closure__
        self.assertIsNotNone(closure)
        cells = [
            cell
            for cell in closure
            if cell.cell_contents is calculator
        ]
        self.assertEqual(len(cells), 1)
        cell = cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = lambda **_kwargs: Decimal("0")
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_cash_floor_rejects_exchange_multiply_cell_mutation(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        calculator = cash_floor_authority._risk_module.locked_capital_for_exchange_side
        closure = calculator.__closure__
        self.assertIsNotNone(closure)
        cells = [
            cell
            for cell in closure
            if callable(cell.cell_contents)
            and getattr(cell.cell_contents, "__name__", None) == "_multiply_exact"
        ]
        self.assertEqual(len(cells), 1)
        cell = cells[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = lambda _left, _right: Decimal("0")
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cell.cell_contents = original

    def test_cash_floor_rejects_json_encoder_substitution(self) -> None:
        bindings = self._bindings()
        self._issue(bindings)
        original = cash_floor_authority.json.JSONEncoder
        try:
            cash_floor_authority.json.JSONEncoder = object
            with self.assertRaisesRegex(
                ProductProposalRiskCounterfactualCashFloorError,
                "dispatch root changed",
            ):
                self._resolve(bindings)
        finally:
            cash_floor_authority.json.JSONEncoder = original

if __name__ == "__main__":
    unittest.main()
