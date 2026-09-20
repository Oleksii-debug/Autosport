from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopRuntime
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.learning_environment import CausalLearningEnvironment, EnvironmentIdentity, Observation
from autosport.paper import PaperBook
from autosport.paper_campaign_admission import PaperCampaignAdmissionCoordinator
from autosport.paper_campaign_runtime import PaperCampaignRuntime
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan
from autosport.risk import PaperRiskPolicy

T0 = "2026-09-20T05:00:00+00:00"
T1 = "2026-09-20T05:00:01+00:00"
T2 = "2026-09-20T05:00:05+00:00"
T3 = "2026-09-20T05:00:10+00:00"
T4 = "2026-09-20T05:01:00+00:00"


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="admission-execution-model",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="admission-fixture",
        seed="admission-fixed-seed",
        max_quote_age_ms=60_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


class AdmissionFixture:
    def __init__(
        self,
        base: Path,
        *,
        outcome: PaperAttemptOutcome = PaperAttemptOutcome.ACCEPTED,
        execution_odds: str = "2.00",
        execution_stake: str = "10.00",
    ) -> None:
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.authority = base / "authority"
        self.goal = EconomicGoalContract(
            goal_id="admission-goal",
            revision=1,
            bankroll_id="admission-bankroll",
            currency="USD",
        )
        self.risk = PaperRiskPolicy(economic_goal=self.goal)
        self.identity = EnvironmentIdentity(
            source_id="admission-source",
            config_id="admission-config",
            data_id="admission-data",
            protocol_id="admission-protocol",
            cutoff_ts="2026-09-20T05:02:00+00:00",
            seed=41,
        )
        self.environment = CausalLearningEnvironment(
            self.identity,
            episode_key="admission-episode",
            policy_id="admission-policy",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        )
        self.observation = Observation(
            environment_id=self.environment.environment_id,
            observed_at=T0,
            available_at=T1,
            evidence=(("market_state", "admission-snapshot"),),
        )
        self.baseline = self.environment.checkpoint()
        AgentLoopRuntime.initialize_pristine(
            self.workspace / "agent-loop.json",
            loop_id="admission-loop",
            environment_checkpoint=self.baseline,
            policy_id=self.environment.episode.policy_id,
            economic_goal_fingerprint=provenance_for(self.goal).contract_sha256,
            risk_fingerprint=self.risk.provenance_sha256,
            source_sha256="e" * 64,
            config_sha256="f" * 64,
            at=T0,
        )
        (self.workspace / "decisions.jsonl").touch()

        self.execution_decision_id = "execution-decision-1"
        book = PaperBook("100")
        book.save(self.workspace / "paper_book.json")
        self.execution_ledger_path = self.workspace / "paper-execution.jsonl"
        execution_ledger = PaperExecutionLedger(self.execution_ledger_path)
        execution_runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=execution_ledger,
            config=_config(),
            max_quote_age=timedelta(seconds=60),
            paper_book_path=self.workspace / "paper_book.json",
        )
        action = ExecutionAction(
            action_id="admission-execution-action",
            bookmaker_id="paper-venue",
            account_id="paper-account",
            event_id="admission-event",
            market_id="winner",
            selection_id="home",
            side="BACK",
            requested_odds=Decimal("2.50"),
            requested_stake=Decimal("10.00"),
            quote_id="admission-quote",
            quote_observed_at=T2,
            expires_at=T4,
        )
        prepared = execution_runtime._mint_prepared(
            PreparedPaperExecution(
                execution_plan=ExecutionPlan(
                    plan_id="admission-execution-plan",
                    bookmaker_profile_version="paper-profile-v1",
                    decision_id=self.execution_decision_id,
                    approval_id="paper-only-no-real-money",
                    created_at=T2,
                    actions=(action,),
                ),
                exposure_bindings=(
                    PaperExposureBinding(
                        action_id=action.action_id,
                        sport="table_tennis",
                        bankroll_id=self.goal.bankroll_id,
                        currency=self.goal.currency,
                    ),
                ),
                intent_evidence_json='{"schema":"admission-test-execution"}',
            )
        )
        evidence = PaperExecutionEvidenceRecord(
            action_id=action.action_id,
            bookmaker_id=action.bookmaker_id,
            account_id=action.account_id,
            event_id=action.event_id,
            market_id=action.market_id,
            selection_id=action.selection_id,
            side=action.side,
            quote_id=action.quote_id,
            outcome=outcome,
            observed_at=T3,
            evidence_grade=EvidenceGrade.CONFIGURED,
            evidence_source="admission-fixture-evidence",
            accepted_odds=(execution_odds if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL} else None),
            accepted_stake=(execution_stake if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL} else None),
        )
        registry = PaperExecutionEvidenceRegistry(execution_ledger)
        registry.register(evidence)
        result = execution_runtime.execute(
            prepared=prepared,
            trigger_id="admission-execution-trigger",
            started_at=T3,
            materialize_exposure=True,
            observations={action.action_id: evidence.as_observation()},
            evidence_registry=registry,
        )
        self.execution_run_id = result.run.run_id
        self.execution_attempt_id = result.run.attempts[0].attempt_id
        self.execution_ticket_id = (
            result.ticket_ids[0] if result.ticket_ids else "missing-execution-ticket"
        )
        self.execution_outcome = outcome

    def coordinator(self, *, resumed: bool = False) -> PaperCampaignAdmissionCoordinator:
        environment = (
            CausalLearningEnvironment.resume(
                self.identity,
                episode_key="admission-episode",
                policy_id="admission-policy",
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
                checkpoint=self.baseline,
            )
            if resumed
            else self.environment
        )
        decision_ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        bridge = PaperSettlementLearningBridge(
            self.workspace / "paper-learning-bridge.json",
            paper_book_path=self.workspace / "paper_book.json",
            decision_ledger=decision_ledger,
            agent_loop=AgentLoopRuntime(self.workspace / "agent-loop.json"),
            economic_goal=self.goal,
            risk_policy=self.risk,
        )
        runtime = PaperCampaignRuntime(environment=environment, settlement_bridge=bridge)
        with patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(self.authority)},
        ):
            return PaperCampaignAdmissionCoordinator(
                self.workspace / "paper-campaign-admission.json",
                paper_book_path=self.workspace / "paper_book.json",
                decision_ledger=decision_ledger,
                runtime=runtime,
                execution_ledger=PaperExecutionLedger(self.execution_ledger_path),
            )

    def admit(self, coordinator: PaperCampaignAdmissionCoordinator, **overrides):
        values = {
            "admission_id": "admission-1",
            "observation": self.observation,
            "action_type": "PAPER_PROPOSAL",
            "decision_action": "OPEN_PAPER_TICKET",
            "decision_at": T2,
            "at": T2,
            "replay_run_id": "admission-run",
            "agent": "admission-test",
            "execution_decision_id": self.execution_decision_id,
            "execution_run_id": self.execution_run_id,
            "execution_attempt_id": self.execution_attempt_id,
            "execution_ticket_id": self.execution_ticket_id,
        }
        values.update(overrides)
        return coordinator.admit(**values)
