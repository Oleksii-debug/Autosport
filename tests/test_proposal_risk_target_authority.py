from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.market_mirror import MarketMirror
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.storage import SQLiteMarketStore
import autosport.proposal_risk_outcome_input_authority as outcome_input_authority
from autosport.proposal_risk_outcome_input_authority import (
    ProductProposalRiskOutcomeInputMapping,
    ProductProposalRiskOutcomeInputMappingError,
    ProposalRiskMarketInput,
    issue_product_proposal_risk_outcome_input_mapping,
    resolve_product_proposal_risk_outcome_input_mapping,
)
import autosport.proposal_risk_target_authority as proposal_target_authority
from autosport.proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    issue_product_proposal_risk_target,
    resolve_product_proposal_risk_target,
)
from autosport.proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
    ProductProposalRiskEvaluationPrecommitError,
    issue_product_proposal_risk_evaluation_precommit,
    resolve_product_proposal_risk_evaluation_precommit,
)
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
import autosport.risk as risk_module
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class ProductProposalRiskTargetTests(unittest.TestCase):
    DECISION_TS = "2026-09-18T13:20:00+00:00"
    QUOTE_TS = "2026-09-18T13:19:59+00:00"

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.authority_root = root / "machine-authority"
        self._env = patch.dict(
            os.environ,
            {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(self.authority_root)},
        )
        self._env.start()
        self.goal = self._goal()
        EconomicGoalStore(self.workspace).initialize_owner(self.goal)
        PaperBook(Decimal("1000")).save(self.workspace / "paper_book.json")

    def tearDown(self) -> None:
        self._env.stop()
        self._temp.cleanup()

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-proposal-target",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("0.10"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_event_concentration_fraction": Decimal("1"),
            "max_market_concentration_fraction": Decimal("1"),
            "max_provider_concentration_fraction": Decimal("1"),
            "max_sport_concentration_fraction": Decimal("1"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("0.01"),
            "max_execution_slippage_fraction": Decimal("1"),
            "max_quote_age_seconds": Decimal("3600"),
            "minimum_data_quality": Decimal("0"),
            "max_concurrent_positions": 10,
            "max_parlay_legs": 1,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @classmethod
    def _context(
        cls,
        goal: EconomicGoalContract,
        suffix: str,
        *,
        proposal_ts: str | None = None,
        scalar_ruin_bound: Decimal | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ProposedTicketRiskContext:
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
            observed_ts=cls.QUOTE_TS,
            source_id=f"provider-{suffix}",
            sequence=1,
            source_ts=cls.QUOTE_TS,
            ingest_ts=cls.QUOTE_TS,
            metadata=metadata or {},
            sport="soccer",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts=proposal_ts or cls.DECISION_TS,
            risk_of_ruin_upper_bound=scalar_ruin_bound,
        )

    def _contexts(self) -> tuple[ProposedTicketRiskContext, ...]:
        return (
            self._context(self.goal, "a"),
            self._context(self.goal, "b"),
        )

    def _issue(self) -> ProductProposalRiskTarget:
        return issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"), Decimal("0.8")),
            contexts=self._contexts(),
        )

    def _science_state(self):
        protocol_id = "proposal-risk-fixed-n-protocol-v2"
        dataset_id = "proposal-risk-fixed-n-dataset-v2"
        dataset_manifest_sha = "3" * 64
        frame_sha = "4" * 64
        capital_sha = "5" * 64
        stake_sha = "6" * 64
        horizon_sha = "7" * 64
        members = ("proposal-run-001", "proposal-run-002", "proposal-run-003")
        cutoff = "2026-09-01T00:00:00+00:00"
        dataset_available = "2026-09-02T00:00:00+00:00"
        frozen_at = "2026-09-02T12:00:00+00:00"
        protocol_available = "2026-09-02T12:05:00+00:00"
        outcome_reveal_after = "2026-09-10T00:00:00+00:00"

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
                "ruin_threshold": "0",
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
            research_question_id="proposal-risk-question",
            research_question_sha256="c" * 64,
            hypothesis_id="proposal-risk-hypothesis",
            hypothesis_sha256="d" * 64,
            inclusion_criteria="exact frozen run cohort",
            exclusion_criteria="no post-freeze cohort edits",
            lawful_source_requirements="canonical product evidence only",
            causal_cutoff=cutoff,
            evaluation_design=design,
            feature_set_version="proposal-risk-path-v2",
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

        RunRegistry.initialize_pristine(self.workspace / "run_registry.json")
        registry = ScientificRegistry.initialize_pristine(
            self.workspace / "scientific_registry.json"
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
            workspace=self.workspace,
            research_protocol_id=protocol_id,
            dataset_snapshot_id=dataset_id,
            authority_root=self.authority_root,
        )
        issued_randomization = issue_risk_randomization_precommit(
            registry.path,
            workspace=self.workspace,
            research_protocol_id=protocol_id,
            dataset_snapshot_id=dataset_id,
            experiment_id="proposal-iid-risk-exp-v2",
            authority_root=self.authority_root,
        )
        manifest = json.dumps(
            {
                "kind": "autosport-risk-iid-resample-with-replacement-v1",
                "experiment_id": "proposal-iid-risk-exp-v2",
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
                "randomization_root_sha256": (
                    issued_randomization.randomization_root_sha256
                ),
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
        return membership, registry.path, manifest

    def test_product_issues_and_reresolves_target_science_join_without_result_authority(
        self,
    ) -> None:
        membership, registry_path, manifest = self._science_state()
        target = self._issue()

        joined = issue_product_proposal_risk_evaluation_precommit(
            self.workspace,
            target_sha256=target.target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=manifest,
            authority_root=self.authority_root,
        )

        self.assertIs(type(joined), ProductProposalRiskEvaluationPrecommit)
        self.assertTrue(joined.binding_identity_proven)
        self.assertTrue(joined.proposal_target_identity_proven)
        self.assertTrue(joined.scientific_precommit_proven)
        self.assertTrue(joined.scientific_preoutcome_chronology_proven)
        self.assertTrue(joined.proposal_target_bound_after_scientific_precommit)
        self.assertFalse(joined.scientific_precommit_proves_proposal_execution_scope)
        self.assertFalse(joined.proposal_target_counterfactual_execution_proven)
        self.assertFalse(joined.risk_upper_bound_for_target)
        self.assertFalse(joined.grants_ticket_authority)
        self.assertFalse(joined.grants_real_money_authority)
        self.assertEqual(joined.target_sha256, target.target_sha256)
        self.assertEqual(joined.target_decision_ts, self.DECISION_TS)
        self.assertEqual(
            joined.membership_protocol_record_sha256,
            membership.protocol_record_sha256,
        )
        self.assertEqual(
            joined.membership_dataset_record_sha256,
            membership.dataset_record_sha256,
        )
        self.assertEqual(
            joined.membership_precommitted_at,
            membership.precommitted_at,
        )
        self.assertEqual(
            joined.membership_outcome_reveal_after,
            membership.outcome_reveal_after,
        )
        self.assertEqual(
            joined.scientific_risk_target_scope,
            "FROZEN_STAKE_POLICY",
        )
        self.assertEqual(joined.scientific_stake_policy_sha256, "6" * 64)
        self.assertEqual(
            joined.scientific_initial_capital_state_sha256,
            "5" * 64,
        )
        self.assertEqual(
            joined.proposal_evaluation_scope,
            "EXACT_PROPOSAL_TARGET_FIXED_STAKE_VECTOR_COUNTERFACTUAL_V1",
        )

        # This proposal is intentionally later than the historical cohort reveal.
        # That is lawful for a newly bound counterfactual target; it must not be
        # relabeled as evidence that the target was already executed on that cohort.
        self.assertGreater(
            joined.target_decision_ts,
            joined.membership_outcome_reveal_after,
        )

        resolved = resolve_product_proposal_risk_evaluation_precommit(
            self.workspace,
            binding_sha256=joined.binding_sha256,
            target_sha256=target.target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=manifest,
            authority_root=self.authority_root,
        )
        self.assertEqual(resolved, joined)

        retried = issue_product_proposal_risk_evaluation_precommit(
            self.workspace,
            target_sha256=target.target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=manifest,
            authority_root=self.authority_root,
        )
        self.assertEqual(retried, joined)

        records = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()
        join_records = [
            record
            for record in records
            if record.action == "PROPOSAL_RISK_EVALUATION_PRECOMMIT"
        ]
        self.assertEqual(len(join_records), 1)
        payload = join_records[0].payload
        self.assertNotIn("upper_bound", payload)
        self.assertNotIn("ruin_count", payload)
        self.assertNotIn("result_sha256", payload)
        self.assertIs(payload["scientific_preoutcome_chronology_proven"], True)
        self.assertIs(
            payload["proposal_target_bound_after_scientific_precommit"],
            True,
        )
        self.assertIs(
            payload["scientific_precommit_proves_proposal_execution_scope"],
            False,
        )
        self.assertIs(payload["proposal_target_counterfactual_execution_proven"], False)
        self.assertIs(payload["risk_upper_bound_for_target"], False)

    def test_deleted_join_record_cannot_be_recreated_from_target_and_science(self) -> None:
        membership, registry_path, manifest = self._science_state()
        target = self._issue()
        joined = issue_product_proposal_risk_evaluation_precommit(
            self.workspace,
            target_sha256=target.target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=manifest,
            authority_root=self.authority_root,
        )

        ledger_path = self.workspace / "decisions.jsonl"
        lines = ledger_path.read_bytes().splitlines(keepends=True)
        self.assertGreaterEqual(len(lines), 2)
        ledger_path.write_bytes(b"".join(lines[:-1]))

        with self.assertRaisesRegex(
            ProductProposalRiskEvaluationPrecommitError,
            "missing from the canonical Decision Ledger",
        ):
            resolve_product_proposal_risk_evaluation_precommit(
                self.workspace,
                binding_sha256=joined.binding_sha256,
                target_sha256=target.target_sha256,
                membership=membership,
                registry_path=registry_path,
                sampling_manifest_json=manifest,
                authority_root=self.authority_root,
            )

    def test_scientific_registry_tamper_invalidates_join_after_restart(self) -> None:
        membership, registry_path, manifest = self._science_state()
        target = self._issue()
        joined = issue_product_proposal_risk_evaluation_precommit(
            self.workspace,
            target_sha256=target.target_sha256,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=manifest,
            authority_root=self.authority_root,
        )

        registry_path.write_text("{}", encoding="utf-8")

        with self.assertRaises(ProductProposalRiskEvaluationPrecommitError):
            resolve_product_proposal_risk_evaluation_precommit(
                self.workspace,
                binding_sha256=joined.binding_sha256,
                target_sha256=target.target_sha256,
                membership=membership,
                registry_path=registry_path,
                sampling_manifest_json=manifest,
                authority_root=self.authority_root,
            )

    def test_native_path_workspace_is_accepted_without_subclass_widening(self) -> None:
        self.assertIs(type(self.workspace), proposal_target_authority._PATH_TYPE)
        self.assertEqual(
            proposal_target_authority._workspace_path(self.workspace),
            self.workspace,
        )

        class ForgedPath(type(self.workspace)):
            pass

        forged = ForgedPath(str(self.workspace))
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "exact absolute pathlib.Path",
        ):
            proposal_target_authority._workspace_path(forged)

    def test_workspace_lock_transitive_dispatch_rebinding_fails_closed(self) -> None:
        for name in (
            "__init__",
            "acquire",
            "release",
            "__enter__",
            "__exit__",
            "_open_lock_handle",
            "_open_new_lock_handle",
            "_validate_existing_lock_path",
            "_validate_open_handle_identity",
            "_require_regular_file",
            "_require_single_link",
            "_lock_handle",
            "_unlock_handle",
        ):
            with self.subTest(name=name):
                original = proposal_target_authority.WorkspaceEconomicLock.__dict__[name]
                try:
                    setattr(
                        proposal_target_authority.WorkspaceEconomicLock,
                        name,
                        lambda *args, **kwargs: None,
                    )
                    with self.assertRaisesRegex(
                        ProductProposalRiskTargetError,
                        "WorkspaceEconomicLock",
                    ):
                        proposal_target_authority._require_dispatch()
                finally:
                    setattr(
                        proposal_target_authority.WorkspaceEconomicLock,
                        name,
                        original,
                    )

    def test_constructor_dispatch_rebinding_fails_closed(self) -> None:
        owners = (
            ("EconomicGoalStore", proposal_target_authority.EconomicGoalStore),
            ("JsonlDecisionLedger", proposal_target_authority.JsonlDecisionLedger),
            (
                "MonotonicWorkspaceAuthority",
                proposal_target_authority.MonotonicWorkspaceAuthority,
            ),
        )
        for label, owner in owners:
            with self.subTest(label=label):
                original = owner.__init__
                try:
                    owner.__init__ = lambda *args, **kwargs: None
                    with self.assertRaisesRegex(
                        ProductProposalRiskTargetError,
                        rf"{label}\.__init__",
                    ):
                        proposal_target_authority._require_dispatch()
                finally:
                    owner.__init__ = original

    def test_constructor_witness_root_rebinding_fails_closed(self) -> None:
        original = proposal_target_authority._CONSTRUCTOR_WITNESSES
        try:
            proposal_target_authority._CONSTRUCTOR_WITNESSES = ()
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "constructor witness root",
            ):
                proposal_target_authority._require_dispatch()
        finally:
            proposal_target_authority._CONSTRUCTOR_WITNESSES = original

    def test_direct_construction_is_not_authority(self) -> None:
        with self.assertRaisesRegex(TypeError, "product-issued"):
            ProductProposalRiskTarget()

    def test_product_issues_and_reresolves_exact_non_authorizing_target(self) -> None:
        issued = self._issue()

        self.assertTrue(issued.proposal_target_identity_proven)
        self.assertFalse(issued.proposal_target_counterfactual_execution_proven)
        self.assertFalse(issued.risk_upper_bound_for_target)
        self.assertFalse(issued.grants_ticket_authority)
        self.assertFalse(issued.grants_real_money_authority)
        self.assertEqual(issued.decision_ts, self.DECISION_TS)
        self.assertEqual(issued.bankroll_id, self.goal.bankroll_id)
        self.assertEqual(issued.currency, self.goal.currency)
        self.assertEqual(len(issued.candidate_sha256s), 2)
        expected_context_payloads = tuple(
            proposal_target_authority._context_payload(context)
            for context in self._contexts()
        )
        expected_context_json = tuple(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for payload in expected_context_payloads
        )
        self.assertEqual(issued.candidate_context_json, expected_context_json)
        self.assertEqual(
            tuple(json.loads(value) for value in issued.candidate_context_json),
            expected_context_payloads,
        )
        self.assertTrue(
            all(type(value) is str for value in issued.candidate_context_json)
        )
        self.assertTrue(
            all(
                "risk_of_ruin_upper_bound" not in payload
                and "risk_of_ruin_evidence" not in payload
                for payload in expected_context_payloads
            )
        )
        self.assertEqual(len(issued.evaluated_stakes), 2)
        self.assertTrue(any(stake > 0 for stake in issued.evaluated_stakes))

        resolved = resolve_product_proposal_risk_target(
            self.workspace, issued.target_sha256
        )
        self.assertEqual(resolved, issued)

    def test_retry_is_idempotent_and_does_not_append_duplicate_target(self) -> None:
        first = self._issue()
        second = self._issue()

        self.assertEqual(second, first)
        records = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()
        target_records = [
            record
            for record in records
            if record.action == "PROPOSAL_RISK_TARGET_PRECOMMIT"
        ]
        self.assertEqual(len(target_records), 1)
        self.assertEqual(
            target_records[0].payload["target_sha256"],
            first.target_sha256,
        )

    def test_ledger_precommit_contains_no_positive_ruin_result(self) -> None:
        issued = self._issue()
        record = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()[0]

        self.assertEqual(record.payload["target_sha256"], issued.target_sha256)
        self.assertNotIn("upper_bound", record.payload)
        self.assertNotIn("risk_of_ruin_evidence", record.payload)
        self.assertIs(record.payload["risk_upper_bound_for_target"], False)
        self.assertIs(record.payload["grants_ticket_authority"], False)
        self.assertIs(record.payload["grants_real_money_authority"], False)

    def test_scalar_or_preexisting_ruin_evidence_cannot_enter_target_inputs(self) -> None:
        contexts = (
            self._context(
                self.goal,
                "a",
                scalar_ruin_bound=Decimal("0"),
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "exclude risk-of-ruin result evidence",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("1"),),
                contexts=contexts,
            )

    def test_target_requires_positive_vector_from_existing_allocator(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "no positive canonical pre-risk stake vector",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("0"), Decimal("-1")),
                contexts=self._contexts(),
            )

    def test_target_requires_one_exact_shared_proposal_timestamp(self) -> None:
        contexts = (
            self._context(self.goal, "a"),
            self._context(
                self.goal,
                "b",
                proposal_ts="2026-09-18T13:20:01+00:00",
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "one exact shared proposal timestamp",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("1"), Decimal("0.8")),
                contexts=contexts,
            )

    def test_target_is_stale_after_paperbook_mutation(self) -> None:
        issued = self._issue()
        context = self._contexts()[0]
        book = PaperBook.load(self.workspace / "paper_book.json")
        book.open_ticket(
            context.legs,
            Decimal("1"),
            reason="post-target-mutation",
            placed_at=context.proposal_ts,
            provider_source_ids=tuple(sorted(context.source_ids)),
            bankroll_id=context.bankroll_id,
            currency=context.currency,
        )
        book.save(self.workspace / "paper_book.json")

        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "base portfolio is stale",
        ):
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            )

    def test_target_is_stale_after_owner_goal_tightens(self) -> None:
        issued = self._issue()
        successor = replace(
            self.goal,
            revision=2,
            max_risk_of_ruin=Decimal("0.005"),
        )
        EconomicGoalStore(self.workspace).persist_automatic_successor(successor)

        with self.assertRaises(ProductProposalRiskTargetError):
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            )

    def test_copied_workspace_cannot_reuse_original_machine_authority(self) -> None:
        issued = self._issue()
        copied = self.workspace.parent / "copied-workspace"
        shutil.copytree(self.workspace, copied)

        with self.assertRaises(ProductProposalRiskTargetError):
            resolve_product_proposal_risk_target(copied, issued.target_sha256)

    def test_deleted_ledger_record_cannot_be_bootstrapped_from_target_dto(self) -> None:
        issued = self._issue()
        (self.workspace / "decisions.jsonl").write_bytes(b"")

        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "missing from the canonical Decision Ledger",
        ):
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            )

    def test_future_result_metadata_cannot_cross_precommit_ledger_boundary(self) -> None:
        contexts = (
            self._context(
                self.goal,
                "a",
                metadata={"outcome": "future"},
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "Decision Ledger append failed",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("1"),),
                contexts=contexts,
            )

    def test_risk_policy_provenance_global_rebinding_fails_closed(self) -> None:
        with patch.object(
            risk_module,
            "_sha256_payload",
            lambda _payload: "0" * 64,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "risk-policy provenance hash helper",
            ):
                self._issue()

        with patch.object(
            risk_module,
            "provenance_for",
            lambda _goal: object(),
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "risk-policy provenance dependency",
            ):
                self._issue()

    def test_risk_policy_provenance_descriptor_rebinding_fails_closed(self) -> None:
        forged = property(lambda _policy: "0" * 64)
        with patch.object(
            PaperRiskPolicy,
            "provenance_sha256",
            forged,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "risk-policy provenance digest",
            ):
                self._issue()

    def test_dispatch_rebinding_of_existing_allocator_fails_closed(self) -> None:
        original = PaperRiskPolicy.derive_goal_stake_vector

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(
            PaperRiskPolicy,
            "derive_goal_stake_vector",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "dispatch authority changed",
            ):
                self._issue()

    def test_dispatch_guard_root_rebinding_fails_closed(self) -> None:
        with patch.object(
            proposal_target_authority,
            "_require_dispatch",
            lambda: None,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "dispatch guard root changed",
            ):
                self._issue()

    def test_internal_helper_witness_table_substitution_fails_closed(self) -> None:
        original = proposal_target_authority._derive

        def fake(policy: object, book: object, signals: object, contexts: object) -> object:
            return original(policy, book, signals, contexts)

        substituted = tuple(
            (
                name,
                fake if name == "_derive" else expected,
                getattr(fake, "__code__", None) if name == "_derive" else code,
            )
            for name, expected, code in (
                proposal_target_authority._PROPOSAL_TARGET_HELPER_WITNESSES
            )
        )
        with (
            patch.object(proposal_target_authority, "_derive", fake),
            patch.object(
                proposal_target_authority,
                "_PROPOSAL_TARGET_HELPER_WITNESSES",
                substituted,
            ),
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "helper witness root changed",
            ):
                self._issue()

    def test_monotonic_witness_table_substitution_fails_closed(self) -> None:
        original = MonotonicWorkspaceAuthority.prepare

        def fake(instance: object, *args: object, **kwargs: object) -> object:
            return original(instance, *args, **kwargs)

        substituted = tuple(
            (
                name,
                fake if name == "prepare" else expected,
                getattr(fake, "__code__", None) if name == "prepare" else code,
            )
            for name, expected, code in (
                proposal_target_authority._AUTHORITY_METHOD_WITNESSES
            )
        )
        with (
            patch.object(MonotonicWorkspaceAuthority, "prepare", fake),
            patch.object(
                proposal_target_authority,
                "_AUTHORITY_METHOD_WITNESSES",
                substituted,
            ),
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "witness root changed",
            ):
                self._issue()

    def test_decision_ledger_integrity_dispatch_rebinding_fails_closed(self) -> None:
        original = JsonlDecisionLedger.verify_integrity

        def fake(instance: JsonlDecisionLedger) -> object:
            return original(instance)

        with patch.object(JsonlDecisionLedger, "verify_integrity", fake):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "Decision Ledger verify_integrity",
            ):
                self._issue()

    def test_market_event_serializer_rebinding_fails_closed(self) -> None:
        original = MarketEvent.to_dict

        def fake(instance: MarketEvent) -> object:
            return original(instance)

        with patch.object(MarketEvent, "to_dict", fake):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "MarketEvent to_dict",
            ):
                self._issue()

    def test_market_event_parser_rebinding_fails_closed(self) -> None:
        issued = self._issue()
        descriptor = MarketEvent.__dict__["from_dict"]
        if isinstance(descriptor, classmethod):
            original = descriptor.__func__

            def fake(cls: type[MarketEvent], *args: object, **kwargs: object) -> object:
                return original(cls, *args, **kwargs)

            replacement = classmethod(fake)
        elif isinstance(descriptor, staticmethod):
            original = descriptor.__func__

            def fake(*args: object, **kwargs: object) -> object:
                return original(*args, **kwargs)

            replacement = staticmethod(fake)
        else:
            original = descriptor

            def fake(*args: object, **kwargs: object) -> object:
                return original(*args, **kwargs)

            replacement = fake

        with patch.object(MarketEvent, "from_dict", replacement):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "MarketEvent from_dict",
            ):
                resolve_product_proposal_risk_target(
                    self.workspace, issued.target_sha256
                )

    def test_decision_record_constructor_rebinding_fails_closed(self) -> None:
        original = proposal_target_authority.DecisionRecord

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(proposal_target_authority, "DecisionRecord", fake):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "DecisionRecord type",
            ):
                self._issue()

    def test_sha256_dispatch_rebinding_fails_closed(self) -> None:
        original = proposal_target_authority.hashlib.sha256

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(proposal_target_authority.hashlib, "sha256", fake):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "SHA-256 implementation",
            ):
                self._issue()

    def test_sha256_captured_root_cannot_be_rebound_with_live_dispatch(self) -> None:
        original = proposal_target_authority.hashlib.sha256

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        proposal_target_authority._require_dispatch()
        with (
            patch.object(proposal_target_authority, "_HASHLIB_SHA256", fake),
            patch.object(proposal_target_authority.hashlib, "sha256", fake),
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "SHA-256 implementation",
            ):
                proposal_target_authority._digest({"safe": True})

    def test_canonical_json_dispatch_rebinding_fails_closed(self) -> None:
        original = proposal_target_authority.json.dumps

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(proposal_target_authority.json, "dumps", fake):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "canonical JSON serializer",
            ):
                self._issue()

    def test_canonical_json_captured_root_cannot_be_rebound_with_live_dispatch(
        self,
    ) -> None:
        original = proposal_target_authority.json.dumps

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        proposal_target_authority._require_dispatch()
        with (
            patch.object(proposal_target_authority, "_JSON_DUMPS", fake),
            patch.object(proposal_target_authority.json, "dumps", fake),
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "canonical JSON serializer",
            ):
                proposal_target_authority._canonical_json({"safe": True})

    def test_canonical_json_in_place_code_mutation_after_dispatch_fails_closed(
        self,
    ) -> None:
        serializer = proposal_target_authority.json.dumps
        original_code = serializer.__code__

        def forged(*args: object, **kwargs: object) -> str:
            return '{"forged":true}'

        proposal_target_authority._require_dispatch()
        try:
            serializer.__code__ = forged.__code__
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "canonical JSON serializer",
            ):
                proposal_target_authority._canonical_json({"safe": True})
        finally:
            serializer.__code__ = original_code

    def test_nested_decision_ledger_snapshot_rebinding_fails_closed(self) -> None:
        original = JsonlDecisionLedger.verified_snapshot

        def fake(self: JsonlDecisionLedger) -> object:
            return original(self)

        with patch.object(
            JsonlDecisionLedger,
            "verified_snapshot",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "Decision Ledger verified_snapshot",
            ):
                self._issue()

    def test_decision_record_serializer_rebinding_fails_closed(self) -> None:
        original = proposal_target_authority.DecisionRecord.to_dict

        def fake(self: object) -> object:
            return original(self)

        with patch.object(
            proposal_target_authority.DecisionRecord,
            "to_dict",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "DecisionRecord to_dict",
            ):
                self._issue()

    def test_internal_product_state_helper_rebinding_fails_closed(self) -> None:
        original = proposal_target_authority._current_product_state

        def fake(workspace: Path) -> object:
            return original(workspace)

        with patch.object(
            proposal_target_authority,
            "_current_product_state",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "internal helper authority changed",
            ):
                self._issue()

    def test_monotonic_authority_recover_rebinding_fails_closed(self) -> None:
        original = MonotonicWorkspaceAuthority.recover

        def fake(self: object, **kwargs: object) -> object:
            return original(self, **kwargs)

        with patch.object(
            MonotonicWorkspaceAuthority,
            "recover",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "monotonic authority recover",
            ):
                self._issue()

    def test_relative_workspace_path_is_not_product_authority(self) -> None:
        relative = Path(os.path.relpath(self.workspace, Path.cwd()))
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "exact absolute pathlib.Path",
        ):
            issue_product_proposal_risk_target(
                relative,
                signal_strengths=(Decimal("1"), Decimal("0.8")),
                contexts=self._contexts(),
            )

    def test_signal_vector_is_bound_into_target_identity(self) -> None:
        first = self._issue()
        second = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.8")),
            contexts=self._contexts(),
        )
        self.assertNotEqual(first.target_sha256, second.target_sha256)
        self.assertNotEqual(first.signal_strengths, second.signal_strengths)
        self.assertEqual(
            resolve_product_proposal_risk_target(
                self.workspace, second.target_sha256
            ),
            second,
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "superseded",
        ):
            resolve_product_proposal_risk_target(
                self.workspace, first.target_sha256
            )

    def test_target_chain_blocks_new_issue_after_latest_target_ledger_rollback(
        self,
    ) -> None:
        first = self._issue()
        (self.workspace / "decisions.jsonl").write_bytes(b"")

        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "chain tip is missing",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("0.9"), Decimal("0.8")),
                contexts=self._contexts(),
            )

        with self.assertRaises(ProductProposalRiskTargetError):
            resolve_product_proposal_risk_target(
                self.workspace, first.target_sha256
            )

    def test_missing_append_prepare_is_aborted_before_real_target_issue(self) -> None:
        workspace_authority = proposal_target_authority._authority_for_workspace(
            self.workspace
        )
        chain = proposal_target_authority._target_authority(
            self.workspace, workspace_authority.workspace_instance_id
        )
        interrupted = "a" * 64
        chain.prepare(
            tx_id="interrupted-before-ledger-append",
            observed_state_sha256=None,
            intended_state_sha256=interrupted,
            semantic_binding_sha256=interrupted,
        )

        issued = self._issue()

        self.assertEqual(
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            ),
            issued,
        )
        phases = tuple(record.phase.value for record in chain.read_history())
        self.assertIn("ABORT", phases)
        self.assertEqual(phases[-1], "COMMIT")

    def test_superseded_target_cannot_be_reissued_as_current(self) -> None:
        first = self._issue()
        second = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.8")),
            contexts=self._contexts(),
        )
        self.assertNotEqual(first.target_sha256, second.target_sha256)

        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "superseded and cannot be reissued",
        ):
            self._issue()

    def test_target_chain_keeps_exact_append_history_while_only_latest_is_current(
        self,
    ) -> None:
        first = self._issue()
        second = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.8")),
            contexts=self._contexts(),
        )

        records = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()
        target_ids = tuple(
            record.payload["target_sha256"]
            for record in records
            if record.action == "PROPOSAL_RISK_TARGET_PRECOMMIT"
        )
        self.assertEqual(target_ids, (first.target_sha256, second.target_sha256))
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "superseded",
        ):
            resolve_product_proposal_risk_target(
                self.workspace, first.target_sha256
            )
        self.assertEqual(
            resolve_product_proposal_risk_target(
                self.workspace, second.target_sha256
            ).target_sha256,
            second.target_sha256,
        )


    def _winner_target_market_fixture(
        self,
    ) -> tuple[
        ProductProposalRiskTarget,
        ProposalRiskMarketInput,
        SQLiteMarketStore,
    ]:
        decision_ts = "2026-09-18T15:05:00Z"
        quote_ts = "2026-09-18T15:04:00Z"
        source_id = "betfair_exchange_historical"
        sport = "table_tennis"
        event_id = "event-outcome-map"
        market_id = "match_odds"

        contexts: list[ProposedTicketRiskContext] = []
        for selection_id in ("away", "home"):
            leg = TicketLeg(
                event_id=event_id,
                market_id=market_id,
                selection_id=selection_id,
                locked_odds=Decimal("3"),
                sport=sport,
            )
            quote = MarketEvent(
                event_id=event_id,
                market_id=market_id,
                selection_id=selection_id,
                decimal_odds=Decimal("3"),
                observed_ts=quote_ts,
                source_id=source_id,
                sequence=1 if selection_id == "away" else 3,
                market_type=MarketType.WINNER,
                source_ts=quote_ts,
                ingest_ts=quote_ts,
                metadata={},
                sport=sport,
            )
            contexts.append(
                ProposedTicketRiskContext(
                    legs=(leg,),
                    quotes=(quote,),
                    bankroll_id=self.goal.bankroll_id,
                    currency=self.goal.currency,
                    proposal_ts=decision_ts,
                )
            )

        target = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"), Decimal("0.8")),
            contexts=tuple(contexts),
        )

        store = SQLiteMarketStore(self.workspace / "proposal_outcome_inputs.db")
        self.addCleanup(store.close)
        mirror = MarketMirror()
        for sequence, selection_id in enumerate(("away", "draw", "home"), start=1):
            mirror.persist_and_apply(
                store,
                MarketEvent(
                    event_id=event_id,
                    market_id=market_id,
                    selection_id=selection_id,
                    decimal_odds=Decimal("3"),
                    observed_ts=quote_ts,
                    source_id=source_id,
                    sequence=sequence,
                    market_type=MarketType.WINNER,
                    source_ts=quote_ts,
                    ingest_ts=quote_ts,
                    metadata={},
                    sport=sport,
                ),
            )

        assessment = assess_betfair_historical_market_definition_authority(
            market_id=market_id,
            market_definition={
                "eventId": event_id,
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [
                    {"id": "away"},
                    {"id": "draw"},
                    {"id": "home"},
                ],
            },
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        market_input = ProposalRiskMarketInput(
            store=store,
            outcome_authority=assessment.authority,  # type: ignore[arg-type]
            max_age=timedelta(minutes=10),
        )
        return target, market_input, store

    def test_outcome_input_mapping_is_durable_exact_and_non_authorizing(self) -> None:
        target, market_input, _store = self._winner_target_market_fixture()

        mapping = issue_product_proposal_risk_outcome_input_mapping(
            self.workspace,
            target_sha256=target.target_sha256,
            market_inputs=(market_input,),
        )

        self.assertIs(type(mapping), ProductProposalRiskOutcomeInputMapping)
        self.assertTrue(mapping.mapping_identity_proven)
        self.assertTrue(mapping.exact_target_market_coverage_proven)
        self.assertTrue(mapping.decision_time_market_inputs_rebuilt)
        self.assertFalse(mapping.provider_outcome_origin_independently_proven)
        self.assertFalse(mapping.joint_probability_model_proven)
        self.assertFalse(mapping.proposal_target_counterfactual_execution_proven)
        self.assertFalse(mapping.risk_upper_bound_for_target)
        self.assertFalse(mapping.proposal_target_risk_qualified)
        self.assertFalse(mapping.grants_risk_approval_authority)
        self.assertFalse(mapping.grants_ticket_authority)
        self.assertFalse(mapping.grants_broker_execution_authority)
        self.assertFalse(mapping.grants_real_money_authority)
        self.assertFalse(mapping.grants_state_mutation_authority)
        self.assertEqual(mapping.target_sha256, target.target_sha256)
        self.assertEqual(mapping.candidate_vector_sha256, target.candidate_vector_sha256)
        self.assertEqual(len(mapping.market_identity_json), 1)
        self.assertEqual(len(mapping.outcome_authority_sha256s), 1)
        self.assertEqual(len(mapping.baseline_evidence_sha256s), 1)
        probabilities = json.loads(mapping.probability_vector_json[0])
        self.assertEqual(
            tuple(
                (
                    row["selection_id"],
                    row["numerator"],
                    row["denominator"],
                )
                for row in probabilities
            ),
            (
                ("away", 1, 3),
                ("draw", 1, 3),
                ("home", 1, 3),
            ),
        )

        resolved = resolve_product_proposal_risk_outcome_input_mapping(
            self.workspace,
            mapping_sha256=mapping.mapping_sha256,
            target_sha256=target.target_sha256,
            market_inputs=(market_input,),
        )
        self.assertEqual(resolved, mapping)

        records = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()
        mapping_records = tuple(
            record
            for record in records
            if record.action == "PROPOSAL_RISK_OUTCOME_INPUT_MAPPING"
        )
        self.assertEqual(len(mapping_records), 1)
        self.assertEqual(
            mapping_records[0].payload["mapping_sha256"],
            mapping.mapping_sha256,
        )
        self.assertFalse(
            mapping_records[0].payload["joint_probability_model_proven"]
        )
        self.assertFalse(
            mapping_records[0].payload[
                "proposal_target_counterfactual_execution_proven"
            ]
        )

    def test_outcome_input_mapping_requires_exact_target_market_coverage(self) -> None:
        target, market_input, _store = self._winner_target_market_fixture()

        with self.assertRaisesRegex(
            ProductProposalRiskOutcomeInputMappingError,
            "non-empty exact tuple",
        ):
            issue_product_proposal_risk_outcome_input_mapping(
                self.workspace,
                target_sha256=target.target_sha256,
                market_inputs=(),
            )

        duplicate = ProposalRiskMarketInput(
            store=market_input.store,
            outcome_authority=market_input.outcome_authority,
            max_age=market_input.max_age,
        )
        with self.assertRaisesRegex(
            ProductProposalRiskOutcomeInputMappingError,
            "duplicate market authority identity",
        ):
            issue_product_proposal_risk_outcome_input_mapping(
                self.workspace,
                target_sha256=target.target_sha256,
                market_inputs=(market_input, duplicate),
            )

    def test_outcome_input_mapping_rebuild_detects_changed_predecision_history(
        self,
    ) -> None:
        target, market_input, store = self._winner_target_market_fixture()
        mapping = issue_product_proposal_risk_outcome_input_mapping(
            self.workspace,
            target_sha256=target.target_sha256,
            market_inputs=(market_input,),
        )

        mirror = MarketMirror.from_store(store)
        mirror.persist_and_apply(
            store,
            MarketEvent(
                event_id="event-outcome-map",
                market_id="match_odds",
                selection_id="home",
                decimal_odds=Decimal("2"),
                observed_ts="2026-09-18T15:04:30Z",
                source_id="betfair_exchange_historical",
                sequence=10,
                market_type=MarketType.WINNER,
                source_ts="2026-09-18T15:04:30Z",
                ingest_ts="2026-09-18T15:04:30Z",
                metadata={},
                sport="table_tennis",
            ),
        )

        with self.assertRaisesRegex(
            ProductProposalRiskOutcomeInputMappingError,
            "differs from current target/market input roots",
        ):
            resolve_product_proposal_risk_outcome_input_mapping(
                self.workspace,
                mapping_sha256=mapping.mapping_sha256,
                target_sha256=target.target_sha256,
                market_inputs=(market_input,),
            )

    def test_outcome_input_mapping_rejects_unsupported_target_market_semantics(
        self,
    ) -> None:
        target = self._issue()
        store = SQLiteMarketStore(self.workspace / "unsupported_outcome_inputs.db")
        self.addCleanup(store.close)
        forged_authority = object.__new__(
            outcome_input_authority.MarketSettlementOutcomeAuthority
        )
        with self.assertRaisesRegex(
            ProductProposalRiskOutcomeInputMappingError,
            "winner markets only",
        ):
            issue_product_proposal_risk_outcome_input_mapping(
                self.workspace,
                target_sha256=target.target_sha256,
                market_inputs=(
                    ProposalRiskMarketInput(
                        store=store,
                        outcome_authority=forged_authority,
                        max_age=timedelta(minutes=10),
                    ),
                ),
            )

    def test_outcome_input_mapping_direct_construction_and_dispatch_rebind_fail_closed(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskOutcomeInputMapping()

        target, market_input, _store = self._winner_target_market_fixture()
        original = outcome_input_authority.build_market_implied_baseline_evidence

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(
            outcome_input_authority,
            "build_market_implied_baseline_evidence",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch authority changed",
            ):
                issue_product_proposal_risk_outcome_input_mapping(
                    self.workspace,
                    target_sha256=target.target_sha256,
                    market_inputs=(market_input,),
                )


    def test_outcome_input_mapping_guard_helper_and_descriptor_rebinding_fail_closed(
        self,
    ) -> None:
        target, market_input, _store = self._winner_target_market_fixture()

        original_guard = outcome_input_authority._require_dispatch
        with patch.object(
            outcome_input_authority,
            "_require_dispatch",
            lambda: None,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch guard root changed",
            ):
                issue_product_proposal_risk_outcome_input_mapping(
                    self.workspace,
                    target_sha256=target.target_sha256,
                    market_inputs=(market_input,),
                )
        self.assertIs(outcome_input_authority._require_dispatch, original_guard)

        original_rows = outcome_input_authority._market_rows

        def fake_rows(*args: object, **kwargs: object) -> object:
            return original_rows(*args, **kwargs)

        with patch.object(outcome_input_authority, "_market_rows", fake_rows):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch authority changed",
            ):
                issue_product_proposal_risk_outcome_input_mapping(
                    self.workspace,
                    target_sha256=target.target_sha256,
                    market_inputs=(market_input,),
                )

        original_evidence_to_dict = (
            outcome_input_authority.MarketImpliedBaselineEvidence.to_dict
        )

        def fake_evidence_to_dict(self: object) -> object:
            return original_evidence_to_dict(self)

        with patch.object(
            outcome_input_authority.MarketImpliedBaselineEvidence,
            "to_dict",
            fake_evidence_to_dict,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch authority changed",
            ):
                issue_product_proposal_risk_outcome_input_mapping(
                    self.workspace,
                    target_sha256=target.target_sha256,
                    market_inputs=(market_input,),
                )

        original_record_to_dict = outcome_input_authority.DecisionRecord.to_dict

        def fake_record_to_dict(self: object) -> object:
            return original_record_to_dict(self)

        with patch.object(
            outcome_input_authority.DecisionRecord,
            "to_dict",
            fake_record_to_dict,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch authority changed",
            ):
                issue_product_proposal_risk_outcome_input_mapping(
                    self.workspace,
                    target_sha256=target.target_sha256,
                    market_inputs=(market_input,),
                )

    def test_outcome_input_mapping_result_class_rebinding_fails_closed(self) -> None:
        with patch.object(
            outcome_input_authority,
            "ProductProposalRiskOutcomeInputMapping",
            object,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch authority changed",
            ):
                outcome_input_authority._require_dispatch()

    def test_outcome_input_mapping_authority_property_rebinding_fails_closed(
        self,
    ) -> None:
        forged = property(lambda _self: True)
        with patch.object(
            ProductProposalRiskOutcomeInputMapping,
            "risk_upper_bound_for_target",
            forged,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskOutcomeInputMappingError,
                "dispatch authority changed",
            ):
                outcome_input_authority._require_dispatch()

    def test_outcome_input_mapping_false_authority_getter_code_mutation_fails_closed(
        self,
    ) -> None:
        false_authority_names = (
            "provider_outcome_origin_independently_proven",
            "joint_probability_model_proven",
            "proposal_target_counterfactual_execution_proven",
            "risk_upper_bound_for_target",
            "proposal_target_risk_qualified",
            "grants_risk_approval_authority",
            "grants_ticket_authority",
            "grants_broker_execution_authority",
            "grants_real_money_authority",
            "grants_state_mutation_authority",
        )

        def forged(_self: object) -> bool:
            return True

        for name in false_authority_names:
            descriptor = ProductProposalRiskOutcomeInputMapping.__dict__[name]
            getter = descriptor.fget
            self.assertIsNotNone(getter)
            original_code = getter.__code__
            try:
                getter.__code__ = forged.__code__
                with self.assertRaisesRegex(
                    ProductProposalRiskOutcomeInputMappingError,
                    "dispatch authority changed",
                    msg=name,
                ):
                    outcome_input_authority._require_dispatch()
            finally:
                getter.__code__ = original_code


    def test_outcome_input_mapping_rejects_exchange_side_identity_collapse(
        self,
    ) -> None:
        decision_ts = "2026-09-18T15:05:00Z"
        quote_ts = "2026-09-18T15:04:00Z"
        leg = TicketLeg(
            event_id="event-sided",
            market_id="match_odds",
            selection_id="home",
            locked_odds=Decimal("2"),
            sport="table_tennis",
            exchange_side="back",
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
            exchange_side="back",
        )
        target = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"),),
            contexts=(
                ProposedTicketRiskContext(
                    legs=(leg,),
                    quotes=(quote,),
                    bankroll_id=self.goal.bankroll_id,
                    currency=self.goal.currency,
                    proposal_ts=decision_ts,
                ),
            ),
        )
        store = SQLiteMarketStore(self.workspace / "sided_outcome_inputs.db")
        self.addCleanup(store.close)
        forged_exact_type = object.__new__(
            outcome_input_authority.MarketSettlementOutcomeAuthority
        )
        market_input = ProposalRiskMarketInput(
            store=store,
            outcome_authority=forged_exact_type,
            max_age=timedelta(minutes=10),
        )

        with self.assertRaisesRegex(
            ProductProposalRiskOutcomeInputMappingError,
            "does not yet support exchange-side target identities",
        ):
            issue_product_proposal_risk_outcome_input_mapping(
                self.workspace,
                target_sha256=target.target_sha256,
                market_inputs=(market_input,),
            )


if __name__ == "__main__":
    unittest.main()
