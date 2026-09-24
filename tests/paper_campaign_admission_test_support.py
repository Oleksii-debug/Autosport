from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_instance_guard as origin_instance_guard
from autosport.agent_loop import AgentLoopRuntime
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    JsonlDecisionLedger,
)
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


class _CanonicalOriginLedger(PaperExecutionLedger):
    """Test-only adapter: reserve through #727, delegate all later state to target."""

    def __init__(
        self,
        target: PaperExecutionLedger,
        origin,
        runtime: PaperExecutionAdoptionRuntime,
    ) -> None:
        self._target = target
        self._origin = origin
        self._runtime = runtime

    @property
    def path(self):
        return self._target.path

    def reserve_run(self, **kwargs):
        token = origin_instance_guard._PRODUCT_ORIGIN_RUNTIME.set(self._runtime)
        try:
            return origin_instance_guard._STABLE_RESERVE_RUN(
                origin_instance_guard._CanonicalReservationView(self._target, self._origin),
                **kwargs,
            )
        finally:
            origin_instance_guard._PRODUCT_ORIGIN_RUNTIME.reset(token)

    def load_run(self, **kwargs):
        token = origin_module._DECISION_ORIGIN.set(self._origin)
        try:
            return self._target.load_run(**kwargs)
        finally:
            origin_module._DECISION_ORIGIN.reset(token)

    def record_attempt(self, attempt):
        return self._target.record_attempt(attempt)

    def complete_run(self, **kwargs):
        return self._target.complete_run(**kwargs)

    def events(self, run_id=None):
        return self._target.events(run_id)


def _durable_learning_observation(
    ledger: PaperExecutionLedger,
    run_id: str,
) -> Observation:
    events = ledger.events(run_id)
    reservation = next(event for event in events if event["event_type"] == "RUN_RESERVED")
    origin = reservation["payload"]["decision_origin"]
    raw = origin["learning_observation"]
    evidence = tuple((item[0], item[1]) for item in raw["evidence"])
    observation = Observation(
        environment_id=raw["environment_id"],
        observed_at=raw["observed_at"],
        available_at=raw["available_at"],
        evidence=evidence,
    )
    if observation.observation_id != raw["observation_id"]:
        raise AssertionError("fixture durable learning Observation identity changed")
    return observation


class AdmissionFixture:
    def __init__(
        self,
        base: Path,
        *,
        outcome: PaperAttemptOutcome = PaperAttemptOutcome.ACCEPTED,
        execution_odds: str = "2.00",
        execution_stake: str = "10.00",
        seed_execution_decision: bool = True,
        decision_plan_fingerprint: str | None = None,
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
        # Originless negative fixtures retain a non-authoritative caller observation.
        # Positive fixtures replace this below with exact pre-execution durable bytes.
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
        with patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(self.authority)},
        ):
            execution_ledger = PaperExecutionLedger(self.execution_ledger_path)
        execution_config = _config()
        execution_runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=execution_ledger,
            config=execution_config,
            max_quote_age=timedelta(seconds=60),
            paper_book_path=self.workspace / "paper_book.json",
            learning_environment=self.environment,
        )
        if seed_execution_decision:
            issuer = getattr(
                execution_runtime,
                "_autosport_issue_predecision_learning_observation",
            )
            self.learning_observation_payload = issuer(
                observed_at=T0,
                available_at=T1,
                evidence=(
                    ("decision_context_sha256", "a" * 64),
                    ("intent_evidence_sha256", "b" * 64),
                    ("intent_provenance_sha256", "c" * 64),
                    ("intent_vector_sha256", "d" * 64),
                    ("market_state_sha256", "e" * 64),
                ),
            )
            if self.learning_observation_payload is None:
                raise AssertionError("fixture learning Observation was not issued")
            raw_evidence = self.learning_observation_payload["evidence"]
            self.observation = Observation(
                environment_id=self.learning_observation_payload["environment_id"],
                observed_at=self.learning_observation_payload["observed_at"],
                available_at=self.learning_observation_payload["available_at"],
                evidence=tuple((item[0], item[1]) for item in raw_evidence),
            )
            if (
                self.observation.observation_id
                != self.learning_observation_payload["observation_id"]
            ):
                raise AssertionError("fixture learning Observation identity changed")
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
        self._prepared = prepared
        self._execution_config = execution_config
        self.execution_run_id = execution_runtime.expected_run_id(
            prepared, self.execution_decision_id
        )
        decision_ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        if seed_execution_decision:
            self.append_execution_decision(plan_fingerprint=decision_plan_fingerprint)

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
            accepted_odds=(
                execution_odds
                if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
                else None
            ),
            accepted_stake=(
                execution_stake
                if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
                else None
            ),
        )
        registry = PaperExecutionEvidenceRegistry(execution_ledger)
        registry.register(evidence)
        observed = evidence.as_observation()

        if seed_execution_decision:
            origin = origin_instance_guard._verified_decision_origin_without_instance_dispatch(
                decision_ledger,
                self.execution_decision_id,
            )
            canonical_ledger = _CanonicalOriginLedger(
                execution_ledger,
                origin,
                execution_runtime,
            )
            original_ledger = execution_runtime.ledger
            execution_runtime.ledger = canonical_ledger
            try:
                result = origin_module._ORIGINAL_RUNTIME_EXECUTE(
                    execution_runtime,
                    prepared=prepared,
                    trigger_id=self.execution_decision_id,
                    started_at=T2,
                    materialize_exposure=True,
                    observations={action.action_id: observed},
                    evidence_registry=registry,
                )
            finally:
                execution_runtime.ledger = original_ledger
            durable_observation = _durable_learning_observation(
                execution_ledger,
                self.execution_run_id,
            )
            if durable_observation != self.observation:
                raise AssertionError(
                    "RUN_RESERVED did not copy the pre-published learning Observation"
                )
        else:
            # Deliberately originless: used to prove a later matching decision cannot
            # retroactively bless already-started PAPER execution.
            result = origin_module._ORIGINAL_RUNTIME_EXECUTE(
                execution_runtime,
                prepared=prepared,
                trigger_id=self.execution_decision_id,
                started_at=T2,
                materialize_exposure=True,
                observations={action.action_id: observed},
                evidence_registry=registry,
            )

        if result.run.run_id != self.execution_run_id:
            raise AssertionError("fixture expected deterministic PAPER execution run identity")
        self.execution_attempt_id = result.run.attempts[0].attempt_id
        self.execution_ticket_id = (
            result.ticket_ids[0] if result.ticket_ids else "missing-execution-ticket"
        )
        self.execution_outcome = outcome

    def append_execution_decision(self, *, plan_fingerprint: str | None = None) -> str:
        decision_ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        return decision_ledger.append_economic(
            DecisionRecord(
                replay_run_id="live:admission-fixture",
                agent="persistent-live-decision-loop",
                observed_ts=T2,
                action="LIVE_OPEN",
                payload={
                    "schema": "autosport.persistent_live_decision",
                    "schema_version": 2,
                    "material_action_id": self.execution_decision_id,
                    **(
                        {"learning_observation": self.learning_observation_payload}
                        if hasattr(self, "learning_observation_payload")
                        else {}
                    ),
                    "paper_execution": {
                        "schema": "autosport.paper_execution_adoption",
                        "schema_version": 1,
                        "plan_id": self._prepared.execution_plan.plan_id,
                        "plan_fingerprint": (
                            plan_fingerprint
                            if plan_fingerprint is not None
                            else self._prepared.execution_plan.fingerprint
                        ),
                        "model_fingerprint": self._execution_config.fingerprint,
                        "run_id": self.execution_run_id,
                        "intent_evidence_json": self._prepared.intent_evidence_json,
                    },
                },
                context_hash="a" * 64,
                decision_id=self.execution_decision_id,
                decision_kind=ECONOMIC_DECISION_KIND,
            ),
            self.goal,
            risk_policy=self.risk,
        )

    def coordinator(
        self,
        *,
        resumed: bool = False,
        execution_ledger: PaperExecutionLedger | None = None,
        decision_ledger: JsonlDecisionLedger | None = None,
    ) -> PaperCampaignAdmissionCoordinator:
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
        canonical_decision_ledger = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        )
        bridge = PaperSettlementLearningBridge(
            self.workspace / "paper-learning-bridge.json",
            paper_book_path=self.workspace / "paper_book.json",
            decision_ledger=canonical_decision_ledger,
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
                decision_ledger=(
                    decision_ledger
                    if decision_ledger is not None
                    else canonical_decision_ledger
                ),
                runtime=runtime,
                execution_ledger=(
                    execution_ledger
                    if execution_ledger is not None
                    else PaperExecutionLedger(self.execution_ledger_path)
                ),
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
