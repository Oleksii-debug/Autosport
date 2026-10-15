from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport._evaluation_universe_structural_gate import (
    _authorize_structural_intake_for_tests,
)
from autosport.domain import MarketEvent, MarketType
from autosport.evaluation_intake import (
    ObservationEnumerationWitness,
    ObservationIntakeLedger,
)
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverseLedger,
    EvaluationUniverseStore,
    FunnelStage,
    SlotState,
    build_frozen_universe,
)
from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    REQUIRED_BASELINE_KINDS,
    canonical_evaluation_contract,
)
from autosport.market_implied_baseline import (
    MarketImpliedBaselineEvidence,
    build_market_implied_baseline_evidence,
    market_implied_baseline_config_sha256,
    market_implied_evidence_manifest_sha256,
)
from autosport.market_implied_universe_binding import (
    MarketImpliedUniverseBindingError,
    bind_market_implied_baseline_to_evaluation_universe,
)
from autosport.market_mirror import MarketMirror
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.opportunity import StrategyClass
from autosport.storage import SQLiteMarketStore


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class _Resolver:
    def __init__(self, witness: ObservationEnumerationWitness) -> None:
        self._witness = witness

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        if enumeration_id != self._witness.enumeration_id:
            raise KeyError(enumeration_id)
        return self._witness

    def terminal_enumeration_id(self, **identity: str) -> str:
        del identity
        return self._witness.enumeration_id


class MarketImpliedUniverseBindingTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 18, 15, 5, tzinfo=timezone.utc)
    SOURCE_ID = "betfair_exchange_historical"
    SPORT = "table_tennis"
    H = "a" * 64
    H2 = "b" * 64
    H3 = "c" * 64

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.market_store = SQLiteMarketStore(self.workspace / "market.db")
        self.addCleanup(self.market_store.close)

    def authority(self, *, event_id: str = "event-1") -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
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
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        return assessment.authority  # type: ignore[return-value]

    def persist_market(self, *, event_id: str = "event-1") -> None:
        mirror = MarketMirror()
        for sequence, (selection, odds) in enumerate(
            (("away", "3.00"), ("draw", "3.00"), ("home", "3.00")),
            start=1,
        ):
            mirror.persist_and_apply(
                self.market_store,
                MarketEvent(
                    event_id=event_id,
                    market_id="match_odds",
                    selection_id=selection,
                    decimal_odds=Decimal(odds),
                    observed_ts="2026-09-18T15:04:00Z",
                    ingest_ts="2026-09-18T15:04:00Z",
                    source_id=self.SOURCE_ID,
                    sequence=sequence,
                    market_type=MarketType.WINNER,
                    status="open",
                    source_ts="2026-09-18T15:04:00Z",
                    sport=self.SPORT,
                ),
            )

    def evidence(
        self,
        *,
        cohort_key: str = "row-1",
        event_id: str = "event-1",
        cutoff: datetime | None = None,
    ) -> MarketImpliedBaselineEvidence:
        return build_market_implied_baseline_evidence(
            cohort_key=cohort_key,
            store=self.market_store,
            outcome_authority=self.authority(event_id=event_id),
            decision_cutoff=cutoff or self.CUTOFF,
            max_age=timedelta(minutes=10),
        )

    def evaluation_row(
        self,
        *,
        key: str = "row-1",
        event_id: str | None = "event-1",
        selection_id: str | None = "home",
        decision_at: str | None = "2026-09-18T15:05:00Z",
        slot_state: SlotState = SlotState.CANDIDATE,
    ) -> EvaluationRow:
        candidate = slot_state is SlotState.CANDIDATE
        return EvaluationRow(
            row_key=key,
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=self.H,
            universe_id="universe-1",
            slot_state=slot_state,
            decision_stage=(
                FunnelStage.EXECUTION_MODEL_ELIGIBLE
                if candidate
                else FunnelStage.OBSERVED_SLOT
            ),
            attrition_reason=(
                None
                if candidate
                else AttritionReason.SOURCE_OUTAGE
            ),
            sport=self.SPORT,
            provider_id="provider-1",
            source_id=self.SOURCE_ID,
            event_id=event_id,
            market_id="match_odds" if event_id is not None else None,
            selection_id=selection_id if event_id is not None else None,
            source_at="2026-09-18T14:59:00Z",
            received_at="2026-09-18T15:00:00Z",
            committed_at="2026-09-18T15:00:30Z",
            detection_at="2026-09-18T15:01:00Z" if candidate else None,
            decision_at=decision_at if candidate else None,
            quote_set_sha256=self.H2 if candidate else None,
            freshness_policy_sha256=self.H3,
            strategy_version_id="strategy-1",
            model_version_id="model-1" if candidate else None,
            config_sha256=self.H,
            portfolio_before_id="portfolio-1",
            economic_goal_id="goal-1",
            risk_policy_id="risk-1",
            terminal_space_proof_id="terminal-proof-1" if candidate else None,
            settlement_proof_id="settlement-proof-1" if candidate else None,
            execution_model_id="paper-model-1" if candidate else None,
            execution_run_id="paper-run-1" if candidate else None,
            execution_plan_id="paper-plan-1" if candidate else None,
            execution_action_id="paper-action-1" if candidate else None,
            decision_quote_id="decision-quote-1" if candidate else None,
            cost_contract_sha256=self.H2,
            outcome_reveal_not_before="2026-09-18T16:00:00Z",
            dependence_cluster_keys=("event:event-1",),
        )

    def evaluation_store(self, row: EvaluationRow) -> EvaluationUniverseStore:
        witness = ObservationEnumerationWitness(
            enumeration_id="enumeration-1",
            session_id="session-1",
            source_id=self.SOURCE_ID,
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=self.H,
            universe_id="universe-1",
            cycle_index=1,
            source_range_id="range-1",
            stream_epoch="epoch-1",
            start_cursor="cursor-0",
            end_cursor="cursor-1",
            acquisition_sha256=self.H3,
            row_keys=(row.row_key,),
            row_evidence_sha256=((row.row_key, row.row_id),),
            exhaustive=True,
            gap_free=True,
            committed_at="2026-09-18T15:00:40Z",
            evaluation_not_before="2026-09-18T15:00:45Z",
            outcome_reveal_not_before=row.outcome_reveal_not_before,
        )
        intake = ObservationIntakeLedger(
            self.workspace / "intake",
            authority_id="intake-1",
            enumeration_resolver=_Resolver(witness),
        )
        intake.append_cycle(enumeration_id=witness.enumeration_id)
        _authorize_structural_intake_for_tests(intake)
        universe = build_frozen_universe(
            intake_ledger=intake,
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=self.H,
            frozen_at="2026-09-18T15:05:01Z",
            rows=(row,),
        )
        store = EvaluationUniverseStore(
            self.workspace / "universe",
            intake_ledger=intake,
        )
        store.save(EvaluationUniverseLedger(universe))
        return store

    def definitions(self) -> tuple[BaselineDefinition, ...]:
        definitions: list[BaselineDefinition] = []
        for kind in REQUIRED_BASELINE_KINDS:
            supported = kind is BaselineKind.MARKET_IMPLIED_DEVIG
            definitions.append(
                BaselineDefinition(
                    kind=kind,
                    baseline_id="baseline:" + kind.value,
                    implementation_sha256=_hash("impl:" + kind.value),
                    config_sha256=(
                        market_implied_baseline_config_sha256()
                        if supported
                        else _hash("config:" + kind.value)
                    ),
                    supported=supported,
                    unsupported_reason=None if supported else "unsupported test fixture",
                )
            )
        return tuple(definitions)

    def protocol(self, evidence: MarketImpliedBaselineEvidence) -> FrozenBaselineProtocol:
        contract = canonical_evaluation_contract(
            EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
        )
        return FrozenBaselineProtocol(
            protocol_id="market-implied-universe-test",
            frozen_at="2026-09-18T14:30:00Z",
            evidence_scope=FrozenEvidenceScope(
                dataset_sha256=_hash("dataset"),
                dataset_cutoff="2026-09-18T14:00:00Z",
                cohort_keys=(evidence.cohort_key,),
                market_evidence_sha256=market_implied_evidence_manifest_sha256((evidence,)),
                outcome_evidence_sha256=_hash("outcomes"),
                cost_model_sha256=_hash("cost"),
                execution_model_sha256=_hash("execution"),
            ),
            candidate_id="candidate",
            candidate_artifact_sha256=_hash("candidate"),
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            evaluation_contract_family=EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
            evaluation_semantics=contract["evaluation_semantics"],
            evaluation_contract_sha256=contract["evaluation_contract_sha256"],
            primary_metric=contract["primary_metric"],
            uncertainty_method=contract["uncertainty_method"],
            baselines=self.definitions(),
        )

    def definition(self, protocol: FrozenBaselineProtocol) -> BaselineDefinition:
        return next(
            item
            for item in protocol.baselines
            if item.kind is BaselineKind.MARKET_IMPLIED_DEVIG
        )

    def test_exact_canonical_row_market_cutoff_and_selection_bind(self) -> None:
        self.persist_market()
        evidence = self.evidence()
        protocol = self.protocol(evidence)
        store = self.evaluation_store(self.evaluation_row())

        bound = bind_market_implied_baseline_to_evaluation_universe(
            protocol=protocol,
            baseline_definition=self.definition(protocol),
            evidence=(evidence,),
            evaluation_store=store,
        )

        loaded = store.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(bound.universe_sha256, loaded.universe.universe_sha256)
        self.assertEqual(bound.membership_sha256, loaded.universe.membership_sha256)
        self.assertEqual(bound.row_bindings[0].row_key, "row-1")
        self.assertEqual(bound.row_bindings[0].target_selection_id, "home")
        self.assertEqual(
            (
                bound.row_bindings[0].probability_numerator,
                bound.row_bindings[0].probability_denominator,
            ),
            (1, 3),
        )
        truth = bound.to_dict()["truth"]
        self.assertTrue(truth["canonical_evaluation_universe_bound"])
        self.assertTrue(truth["complete_universe_membership_required"])
        self.assertFalse(truth["source_stream_continuity_proven"])
        self.assertFalse(truth["promotion_authority"])
        self.assertFalse(truth["real_money_execution"])

    def test_unrelated_valid_market_cannot_be_relabelled_as_expected_row(self) -> None:
        self.persist_market(event_id="event-2")
        evidence = self.evidence(event_id="event-2")
        protocol = self.protocol(evidence)
        store = self.evaluation_store(self.evaluation_row(event_id="event-1"))

        with self.assertRaisesRegex(
            MarketImpliedUniverseBindingError,
            "identity does not match canonical evaluation row",
        ):
            bind_market_implied_baseline_to_evaluation_universe(
                protocol=protocol,
                baseline_definition=self.definition(protocol),
                evidence=(evidence,),
                evaluation_store=store,
            )

    def test_decision_cutoff_cannot_be_detached_from_canonical_row(self) -> None:
        self.persist_market()
        evidence = self.evidence()
        protocol = self.protocol(evidence)
        store = self.evaluation_store(
            self.evaluation_row(decision_at="2026-09-18T15:04:59Z")
        )

        with self.assertRaisesRegex(
            MarketImpliedUniverseBindingError,
            "cutoff does not equal canonical row decision time",
        ):
            bind_market_implied_baseline_to_evaluation_universe(
                protocol=protocol,
                baseline_definition=self.definition(protocol),
                evidence=(evidence,),
                evaluation_store=store,
            )

    def test_target_selection_must_exist_in_bound_probability_vector(self) -> None:
        self.persist_market()
        evidence = self.evidence()
        protocol = self.protocol(evidence)
        store = self.evaluation_store(
            self.evaluation_row(selection_id="not-a-market-selection")
        )

        with self.assertRaisesRegex(
            MarketImpliedUniverseBindingError,
            "selection is absent from market-implied probability vector",
        ):
            bind_market_implied_baseline_to_evaluation_universe(
                protocol=protocol,
                baseline_definition=self.definition(protocol),
                evidence=(evidence,),
                evaluation_store=store,
            )

    def test_source_outage_membership_cannot_disappear_from_positive_baseline(self) -> None:
        self.persist_market()
        evidence = self.evidence()
        protocol = self.protocol(evidence)
        store = self.evaluation_store(
            self.evaluation_row(
                event_id=None,
                selection_id=None,
                decision_at=None,
                slot_state=SlotState.SOURCE_OUTAGE,
            )
        )

        with self.assertRaisesRegex(
            MarketImpliedUniverseBindingError,
            "cannot erase zero/outage/missing denominator rows",
        ):
            bind_market_implied_baseline_to_evaluation_universe(
                protocol=protocol,
                baseline_definition=self.definition(protocol),
                evidence=(evidence,),
                evaluation_store=store,
            )


if __name__ == "__main__":
    unittest.main()
