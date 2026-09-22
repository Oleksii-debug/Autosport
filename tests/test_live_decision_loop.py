from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventPhase,
)
from autosport.live_decision_loop import (
    LiveCycleStatus,
    LiveDecisionMode,
    LiveDecisionProgressError,
    LiveLoopBounds,
    PersistentLiveDecisionLoop,
)
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MirrorSnapshot
from autosport.opportunity import Opportunity, OpportunityDecision, QuoteRef, StrategyClass
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.portfolio_plan import (
    EvidenceTruth,
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioAction,
    PortfolioDependencyGraph,
    PortfolioPlan,
)
from autosport.providers import ProviderUnavailableError
from autosport.scientific_registry import ScientificRegistry, StrategyVersion
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.storage import SQLiteMarketStore


class _ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _DurableObserver:
    def __init__(
        self,
        workspace: Path,
        batches: list[tuple[MarketEvent, ...] | Exception],
    ) -> None:
        self.workspace = workspace
        self.batches = list(batches)
        self.calls = 0

    def __call__(self, updates) -> object:
        self.calls += 1
        item: tuple[MarketEvent, ...] | Exception
        if self.batches:
            item = self.batches.pop(0)
        else:
            item = ()
        if isinstance(item, Exception):
            raise item

        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            bus = MarketEventBus(store)
            bus.subscribe(updates.accept_persisted)
            bus.publish_many(item)
        finally:
            store.close()
        return object()


class _EmptyIntentFactory:
    def __init__(
        self,
        strategy_version_id: str = "live-test-strategy-v1",
    ) -> None:
        self.strategy_version_id = strategy_version_id
        self.calls: list[tuple[str, tuple[tuple[str, int, str], ...]]] = []

    def __call__(self, input_id, snapshot):
        self.calls.append(
            (
                input_id,
                tuple(
                    (event.selection_id, event.sequence, event.status)
                    for event in snapshot.events
                ),
            )
        )
        return ()


class _PositiveIntentFactory:
    def __init__(
        self,
        config_sha256: str,
        strategy_version_id: str = "live-test-strategy-v1",
        snapshot_hash_override: str | None = None,
    ) -> None:
        self.strategy_version_id = strategy_version_id
        self.config_sha256 = config_sha256
        self.snapshot_hash_override = snapshot_hash_override
        self.calls = 0

    def __call__(self, input_id, snapshot):
        del input_id
        self.calls += 1
        if not snapshot.events:
            return ()
        event = snapshot.events[0]
        leg = TicketLeg(
            event.event_id,
            event.market_id,
            event.selection_id,
            event.decimal_odds,
            sport=event.sport,
        )
        risk_context = ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(event,),
            provider_accounts=((event.source_id, "paper-account"),),
            bankroll_id="bankroll-live-test",
            currency="EUR",
            proposal_ts=event.observed_ts,
        )
        snapshot_hash = hashlib.sha256(
            json.dumps(
                [visible.to_dict() for visible in snapshot.events],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash=(
                snapshot_hash
                if self.snapshot_hash_override is None
                else self.snapshot_hash_override
            ),
        )
        opportunity = Opportunity(
            strategy_class=StrategyClass.LIVE_PRICE_MOVEMENT,
            decision=OpportunityDecision.ACTIONABLE,
            quotes=(quote,),
        )
        evidence = OpportunityEvidence(
            evidence_id=f"live-evidence-{event.sequence}",
            observed_at=event.observed_ts,
            causal_cutoff=event.source_ts or event.observed_ts,
            reproducibility_sha256="a" * 64,
            truth=EvidenceTruth.EXACT,
            execution_feasible=True,
        )
        return (
            OpportunityIntent(
                intent_id=f"live-intent-{event.sequence}",
                opportunity=opportunity,
                evidence=evidence,
                risk_context=risk_context,
                signal_strength=Decimal("1"),
                strategy_id=self.strategy_version_id,
                config_sha256=self.config_sha256,
            ),
        )


class PersistentLiveDecisionLoopTests(unittest.TestCase):
    START = datetime(2026, 9, 18, 18, 0, 0, tzinfo=timezone.utc)
    INTENT_SOURCE_SHA256 = hashlib.sha256(
        b"tests.test_live_decision_loop:_EmptyIntentFactory:v1"
    ).hexdigest()
    INTENT_ENVIRONMENT_SHA256 = hashlib.sha256(
        b"tests.test_live_decision_loop:environment:v1"
    ).hexdigest()
    INTENT_CONFIG_SHA256 = hashlib.sha256(
        b"tests.test_live_decision_loop:empty-intent-config:v1"
    ).hexdigest()
    ALT_INTENT_CONFIG_SHA256 = hashlib.sha256(
        b"tests.test_live_decision_loop:empty-intent-config:v2"
    ).hexdigest()

    @staticmethod
    def _event(
        *,
        selection: str = "selection-a",
        sequence: int = 1,
        odds: str = "2.00",
        status: str = "open",
        observed: datetime | None = None,
    ) -> MarketEvent:
        timestamp = (observed or PersistentLiveDecisionLoopTests.START).isoformat()
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id=selection,
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id="provider-a",
            sequence=sequence,
            status=status,
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    @staticmethod
    def _authority() -> EconomicDecisionAuthority:
        goal = EconomicGoalContract(
            goal_id="goal-live-test",
            revision=1,
            bankroll_id="bankroll-live-test",
            currency="EUR",
        )
        return EconomicDecisionAuthority(
            goal,
            PaperRiskPolicy(economic_goal=goal),
        )

    @classmethod
    def _strategy_version(
        cls,
        *,
        strategy_version_id: str = "live-test-strategy-v1",
        config_sha256: str | None = None,
        created_at: datetime | None = None,
    ) -> StrategyVersion:
        return StrategyVersion(
            strategy_version_id=strategy_version_id,
            canonical_strategy_id="live-test-strategy",
            source_sha256=cls.INTENT_SOURCE_SHA256,
            environment_sha256=cls.INTENT_ENVIRONMENT_SHA256,
            config_sha256=(
                cls.INTENT_CONFIG_SHA256
                if config_sha256 is None
                else config_sha256
            ),
            created_at=(cls.START if created_at is None else created_at).isoformat(),
        )

    @classmethod
    def _scientific_registry(
        cls,
        workspace: Path,
        strategy_version: StrategyVersion,
    ) -> ScientificRegistry:
        registry = ScientificRegistry.initialize_pristine(
            workspace / "scientific_registry.json"
        )
        registry.append(strategy_version)
        return registry

    def _loop(
        self,
        workspace: Path,
        *,
        observer: _DurableObserver,
        factory,
        clock: _ManualClock,
        bounds: LiveLoopBounds | None = None,
        post_append_hook=None,
        strategy_version: StrategyVersion | None = None,
        book: PaperBook | None = None,
        authority: EconomicDecisionAuthority | None = None,
        catalog_lifecycle: ContinuousEventLifecycle | None = None,
        catalog_fetch_page=None,
        catalog_source_id: str | None = None,
        catalog_required_history: timedelta = timedelta(0),
        paper_execution: PaperExecutionAdoptionRuntime | None = None,
    ) -> PersistentLiveDecisionLoop:
        selected_strategy = strategy_version or self._strategy_version()
        registry = self._scientific_registry(workspace, selected_strategy)
        return PersistentLiveDecisionLoop(
            workspace,
            loop_id="live-test-loop",
            mode=LiveDecisionMode.PAPER,
            book=PaperBook("1000") if book is None else book,
            authority=self._authority() if authority is None else authority,
            intent_factory=factory,
            scientific_registry=registry,
            observation_runner=observer,
            bounds=bounds,
            max_quote_age=timedelta(seconds=5),
            clock=clock,
            post_append_hook=post_append_hook,
            paper_execution=paper_execution,
            catalog_lifecycle=catalog_lifecycle,
            catalog_fetch_page=catalog_fetch_page,
            catalog_source_id=catalog_source_id,
            catalog_required_history=catalog_required_history,
        )

    def test_constructor_requires_durable_registered_intent_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            registry = ScientificRegistry.initialize_pristine(
                workspace / "scientific_registry.json"
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "StrategyVersion is missing",
            ):
                PersistentLiveDecisionLoop(
                    workspace,
                    loop_id="missing-provenance",
                    mode=LiveDecisionMode.PAPER,
                    book=PaperBook("1000"),
                    authority=self._authority(),
                    intent_factory=_EmptyIntentFactory(
                        "caller-minted-arbitrary-digest"
                    ),
                    scientific_registry=registry,
                    observation_runner=_DurableObserver(workspace, [()]),
                    max_quote_age=timedelta(seconds=5),
                    clock=_ManualClock(self.START),
                )

    def test_constructor_rejects_future_registered_intent_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "not causally available",
            ):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                    strategy_version=self._strategy_version(
                        created_at=self.START + timedelta(seconds=1),
                    ),
                )

    def test_factory_cannot_relabel_itself_after_provenance_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=factory,
                clock=_ManualClock(self.START),
            )
            factory.strategy_version_id = "live-test-strategy-v2"

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "factory strategy-version provenance changed",
            ):
                loop._decision_context_sha256()

    def test_emitted_intent_cannot_relabel_registered_strategy_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            forged = object.__new__(OpportunityIntent)
            object.__setattr__(forged, "strategy_id", "relabelled-strategy-v9")
            object.__setattr__(forged, "config_sha256", self.INTENT_CONFIG_SHA256)
            object.__setattr__(forged, "model_id", None)

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "strategy identity does not match registered StrategyVersion",
            ):
                loop._validated_intents(
                    (forged,),
                    snapshot=MirrorSnapshot(revision=0, events=()),
                )

    def test_positive_paper_plan_without_execution_adoption_fails_closed_before_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            intent_sha = "1" * 64
            candidate_sha = "2" * 64
            portfolio_sha = "3" * 64
            dependency_graph = PortfolioDependencyGraph(
                portfolio_sha256=portfolio_sha,
                intent_sha256s=(intent_sha,),
                candidate_sha256s=(candidate_sha,),
            )
            plan = PortfolioPlan(
                decision_ts=self.START.isoformat(),
                action=PortfolioAction.PAPER_PLAN,
                stakes=(Decimal("1"),),
                intent_ids=("intent-1",),
                intent_sha256s=(intent_sha,),
                opportunity_classes=("predictive_edge",),
                portfolio_sha256=portfolio_sha,
                dependency_graph=dependency_graph,
                terminal_economics=None,
                economic_goal_contract_sha256="4" * 64,
                risk_policy_sha256="5" * 64,
                portfolio_truth=EvidenceTruth.EXACT,
                reason="test positive plan must not bypass execution reality",
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "requires canonical #623 execution adoption",
            ):
                loop._persist_plan(
                    plan=plan,
                    intents=(),
                    market_state_sha256="6" * 64,
                    affected_input_ids=(),
                    gate="normal",
                )

            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(loop.book.tickets, {})

    def test_decision_ledger_binds_exact_registered_intent_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(record.payload["schema_version"], 2)
            self.assertEqual(
                record.payload["intent_strategy_version_id"],
                loop.intent_provenance.strategy_version_id,
            )
            self.assertIsNone(record.payload["intent_model_version_id"])
            self.assertEqual(
                record.payload["intent_provenance_sha256"],
                loop.intent_provenance.provenance_sha256,
            )

    @staticmethod
    def _register_two(loop: PersistentLiveDecisionLoop) -> None:
        loop.register_input(
            "input-a",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-a",
        )
        loop.register_input(
            "input-b",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-b",
        )

    def test_first_cycle_rebuilds_all_then_only_affected_input_recomputes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(selection="selection-a", sequence=1),
                        self._event(selection="selection-b", sequence=1),
                    ),
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    ),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            self._register_two(loop)

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(first.plan.action.value, "zero")
            self.assertEqual([item[0] for item in factory.calls], ["input-a", "input-b"])

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual([item[0] for item in factory.calls], ["input-a"])
            self.assertEqual(second.affected_input_ids, ("input-a",))
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                2,
            )

    def test_single_dirty_and_no_change_cycles_avoid_unrelated_mirror_scans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            unrelated = tuple(
                self._event(
                    selection=f"unrelated-{index}",
                    sequence=1,
                    odds="3.00",
                )
                for index in range(64)
            )
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(selection="selection-a", sequence=1),
                        self._event(selection="selection-b", sequence=1),
                        *unrelated,
                    ),
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    ),
                    (),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            self._register_two(loop)
            for index in range(32):
                loop.register_input(
                    f"extra-input-{index}",
                    selection_ids=f"registered-but-absent-{index}",
                )

            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)

            with (
                patch.object(
                    loop.mirror_updates.mirror,
                    "snapshot",
                    side_effect=AssertionError("whole mirror snapshot is forbidden"),
                ),
                patch.object(
                    loop.mirror_updates.mirror,
                    "view",
                    side_effect=AssertionError("whole mirror view is forbidden"),
                ),
            ):
                second = loop.run_cycle()
                self.assertEqual(second.status, LiveCycleStatus.DECIDED)
                self.assertEqual(second.affected_input_ids, ("input-a",))
                self.assertEqual([item[0] for item in factory.calls], ["input-a"])

                factory.calls.clear()
                clock.value = self.START + timedelta(seconds=4)
                third = loop.run_cycle()

            self.assertEqual(third.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(factory.calls, [])

    def test_decision_timestamp_follows_observation_receipt_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            durable = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=1,
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )

            def observer(updates):
                result = durable(updates)
                clock.value = self.START + timedelta(seconds=3)
                return result

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(
                record.observed_ts,
                (self.START + timedelta(seconds=3)).isoformat(),
            )

    def test_duplicate_redelivery_does_not_emit_second_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            duplicate = self._event(selection="selection-a", sequence=1)
            observer = _DurableObserver(workspace, [(duplicate,), (duplicate,)])
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(factory.calls, [])
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )

    def test_crash_after_ledger_append_recovers_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [(self._event(selection="selection-a", sequence=1),)],
            )
            factory = _EmptyIntentFactory()
            crashes = {"remaining": 1}

            def crash_after_append() -> None:
                if crashes["remaining"]:
                    crashes["remaining"] -= 1
                    raise RuntimeError("simulated process loss after ledger append")

            first_loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
                post_append_hook=crash_after_append,
            )
            first_loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first_loop.run_cycle()

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_records = ledger.verified_records()
            self.assertEqual(len(first_records), 1)
            first_id = first_records[0].decision_id

            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=4)),
            )
            self.assertEqual(resumed.dependencies.input_ids, ("input-a",))

            with patch.object(
                JsonlDecisionLedger,
                "verified_records",
                side_effect=AssertionError("full Decision Ledger scan is forbidden"),
            ):
                result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(result.decision_id, first_id)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual([item[0] for item in resumed_factory.calls], ["input-a"])

    def test_append_pending_restart_recovers_before_polling_new_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            crashes = {"remaining": 1}

            def crash_after_append() -> None:
                if crashes["remaining"]:
                    crashes["remaining"] -= 1
                    raise RuntimeError("simulated process loss after ledger append")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
                post_append_hook=crash_after_append,
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_id = ledger.verified_records()[0].decision_id
            resumed_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(recovered.decision_id, first_id)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(len(ledger.verified_records()), 1)

            advanced = resumed.run_cycle()
            self.assertEqual(advanced.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 1)
            self.assertEqual(len(ledger.verified_records()), 2)

    def test_post_execution_book_publish_restart_reuses_durable_plan_and_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            model = PaperExecutionModelConfig(
                model_id="live-paper-recovery-test",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="live-paper-recovery-test",
                seed="live-paper-recovery",
                max_quote_age_ms=5_000,
                min_delay_ms=0,
                max_delay_ms=0,
                rejected_bps=0,
                partial_bps=0,
                unknown_bps=0,
                partial_fill_bps=5000,
                max_slippage_bps=0,
            )
            book = PaperBook("1000")
            execution_ledger = PaperExecutionLedger(
                workspace / "paper-execution.jsonl"
            )
            execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=execution_ledger,
                config=model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            # This regression isolates crash ordering around a positive #623 PAPER
            # action. The current default owner goal requires external research
            # evidence for any non-trivial risk-of-ruin ceiling; that separate
            # admission contract is not what this recovery test exercises.
            recovery_goal = EconomicGoalContract(
                goal_id="goal-live-execution-recovery",
                revision=1,
                bankroll_id="bankroll-live-test",
                currency="EUR",
                max_risk_of_ruin=Decimal("1"),
            )
            recovery_authority = EconomicDecisionAuthority(
                recovery_goal,
                PaperRiskPolicy(economic_goal=recovery_goal),
            )
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=clock,
                book=book,
                authority=recovery_authority,
                paper_execution=execution,
            )
            first.register_input("input-a", selection_ids="selection-a")

            import autosport.live_decision_loop as live_loop_module

            real_atomic_write_json = live_loop_module.atomic_write_json
            crashed = {"value": False}

            def crash_before_committed(path, payload):
                if (
                    Path(path) == workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
                    and payload.get("phase") == "committed"
                    and not crashed["value"]
                ):
                    crashed["value"] = True
                    raise RuntimeError("simulated process loss before COMMITTED")
                return real_atomic_write_json(path, payload)

            with patch(
                "autosport.live_decision_loop.atomic_write_json",
                side_effect=crash_before_committed,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated process loss before COMMITTED",
                ):
                    first.run_cycle()

            durable_after_crash = PaperBook.load(workspace / "paper_book.json")
            self.assertEqual(len(durable_after_crash.tickets), 1)
            balance_after_crash = durable_after_crash.balance
            ticket_ids_after_crash = tuple(durable_after_crash.tickets)
            execution_event_count = len(execution_ledger.events())
            decision = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]

            resumed_book = PaperBook.load(workspace / "paper_book.json")
            resumed_execution = PaperExecutionAdoptionRuntime(
                book=resumed_book,
                ledger=execution_ledger,
                config=model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            resumed_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
                book=resumed_book,
                authority=recovery_authority,
                paper_execution=resumed_execution,
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(recovered.decision_id, decision.decision_id)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(resumed_book.balance, balance_after_crash)
            self.assertEqual(tuple(resumed_book.tickets), ticket_ids_after_crash)
            self.assertEqual(len(execution_ledger.events()), execution_event_count)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_pending_restart_recovers_before_polling_new_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_observer = _DurableObserver(
                workspace,
                [(self._event(selection="selection-a", sequence=1),)],
            )

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"

            first = self._loop(
                workspace,
                observer=first_observer,
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()
            self.assertFalse((workspace / "decisions.jsonl").exists())

            resumed_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            self.assertEqual(len(ledger.verified_records()), 1)

            advanced = resumed.run_cycle()
            self.assertEqual(advanced.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 1)
            self.assertEqual(len(ledger.verified_records()), 2)

    def test_pending_restart_rejects_replayed_market_state_mismatch_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            store = SQLiteMarketStore(workspace / "market.db")
            try:
                store.append(
                    self._event(
                        selection="selection-a",
                        sequence=2,
                        odds="2.10",
                        observed=self.START + timedelta(milliseconds=500),
                    )
                )
            finally:
                store.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "replayed market state changed",
            ):
                resumed.run_cycle()

            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(resumed_factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_pending_restart_rejects_changed_intent_provenance_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory("live-test-strategy-v2"),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
                strategy_version=self._strategy_version(
                    strategy_version_id="live-test-strategy-v2",
                    config_sha256=self.ALT_INTENT_CONFIG_SHA256,
                ),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "runtime context changed",
            ):
                resumed.run_cycle()
            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_pending_restart_rejects_provenance_unavailable_at_original_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            strategy = self._strategy_version(
                created_at=self.START + timedelta(seconds=2),
            )
            seed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
                strategy_version=strategy,
            )
            decision_context_sha256 = seed._decision_context_sha256()
            seed.close()

            legacy_pending = {
                "schema": "autosport.live_decision_progress",
                "schema_version": 1,
                "loop_id": "live-test-loop",
                "phase": "pending",
                "decision_ts": (self.START + timedelta(seconds=1)).isoformat(),
                "market_state_sha256": hashlib.sha256(
                    b"legacy-pre-causal-provenance"
                ).hexdigest(),
                "decision_context_sha256": decision_context_sha256,
                "affected_input_ids": [],
                "registered_input_ids": [],
                "decision_id": None,
                "plan_sha256": None,
                "ledger_offset": None,
                "gate": "normal",
            }
            (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            ).write_text(
                json.dumps(legacy_pending, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
                strategy_version=strategy,
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "not causally available",
            ):
                resumed.run_cycle()

            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_pending_restart_rejects_changed_paper_book_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
                book=PaperBook("900"),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "runtime context changed",
            ):
                resumed.run_cycle()
            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_multi_input_crash_rebuilds_all_but_preserves_affected_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(selection="selection-a", sequence=1),
                        self._event(selection="selection-b", sequence=1),
                    ),
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    ),
                ],
            )
            factory = _EmptyIntentFactory()
            append_count = {"value": 0}

            def crash_on_second_append() -> None:
                append_count["value"] += 1
                if append_count["value"] == 2:
                    raise RuntimeError("simulated second-cycle process loss")

            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
                post_append_hook=crash_on_second_append,
            )
            self._register_two(loop)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            with self.assertRaisesRegex(RuntimeError, "second-cycle process loss"):
                loop.run_cycle()

            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1].payload["affected_input_ids"], ("input-a",))

            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=4)),
            )
            self.assertEqual(
                resumed.dependencies.input_ids,
                ("input-a", "input-b"),
            )
            with patch.object(
                JsonlDecisionLedger,
                "verified_records",
                side_effect=AssertionError("full Decision Ledger scan is forbidden"),
            ):
                result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(result.affected_input_ids, ("input-a",))
            self.assertEqual(
                [item[0] for item in resumed_factory.calls],
                ["input-a", "input-b"],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                2,
            )

    def test_clean_restart_rebuilds_cache_without_duplicate_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_factory = _EmptyIntentFactory()
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=first_factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_id = ledger.verified_records()[0].decision_id
            resumed_factory = _EmptyIntentFactory()
            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )
            self.assertEqual(resumed.dependencies.input_ids, ("input-a",))

            result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual(ledger.verified_records()[0].decision_id, first_id)
            self.assertEqual([item[0] for item in resumed_factory.calls], ["input-a"])

    def test_clean_restart_ignores_unrelated_persisted_market_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)

            store = SQLiteMarketStore(workspace / "market.db")
            try:
                store.append(
                    self._event(
                        selection="selection-b",
                        sequence=1,
                        odds="3.00",
                        observed=self.START + timedelta(seconds=2),
                    )
                )
            finally:
                store.close()

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_id = ledger.verified_records()[0].decision_id
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )

            result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual(ledger.verified_records()[0].decision_id, first_id)
            self.assertEqual([item[0] for item in resumed_factory.calls], ["input-a"])

    def test_backpressure_never_emits_from_partial_dirty_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(selection="selection-a", sequence=1),
                        self._event(selection="selection-b", sequence=1),
                    ),
                    (),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
                bounds=LiveLoopBounds(max_dirty_per_cycle=1),
            )
            self._register_two(loop)

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(set(second.affected_input_ids), {"input-a", "input-b"})
            self.assertEqual([item[0] for item in factory.calls], ["input-a", "input-b"])

    def test_quote_age_cannot_exceed_owner_economic_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            goal = EconomicGoalContract(
                goal_id="goal-live-age",
                revision=1,
                bankroll_id="bankroll-live-age",
                currency="EUR",
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            strategy_version = self._strategy_version()
            registry = self._scientific_registry(workspace, strategy_version)
            with self.assertRaisesRegex(ValueError, "cannot exceed EconomicGoalContract"):
                PersistentLiveDecisionLoop(
                    workspace,
                    loop_id="live-age-test",
                    mode=LiveDecisionMode.PAPER,
                    book=PaperBook("1000"),
                    authority=authority,
                    intent_factory=_EmptyIntentFactory(),
                    scientific_registry=registry,
                    observation_runner=_DurableObserver(workspace, [()]),
                    max_quote_age=timedelta(seconds=6),
                    clock=_ManualClock(self.START),
                )

    def test_provider_gap_persists_zero_and_forces_rebuild_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    ProviderUnavailableError("provider unavailable"),
                    (self._event(selection="selection-a", sequence=1),),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            gap = loop.run_cycle()
            self.assertEqual(gap.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertEqual(gap.plan.action.value, "zero")
            self.assertIn("ProviderUnavailableError", gap.detail)
            records = JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()
            self.assertEqual(records[-1].payload["gate"], "provider_gap")

            clock.value = self.START + timedelta(seconds=2)
            recovered = loop.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual([item[0] for item in factory.calls], ["input-a"])

    def test_catalog_provider_gap_preserves_checkpoint_and_recovers_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            lifecycle_path = workspace / "catalog_lifecycle.json"
            lifecycle = ContinuousEventLifecycle(lifecycle_path)
            clock = _ManualClock(self.START + timedelta(seconds=2))
            quote_time = self.START + timedelta(seconds=1)
            quote = MarketEvent(
                event_id="provider-a:event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=quote_time.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=quote_time.isoformat(),
                ingest_ts=quote_time.isoformat(),
                sport="table_tennis",
            )
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                store.append(quote)
            finally:
                store.close()

            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(
                    CatalogEvent(
                        source_id="provider-a",
                        sport="table_tennis",
                        event_id="event-1",
                        phase=EventPhase.PRE_MATCH,
                        available_at=quote_time.isoformat(),
                    ),
                ),
            )
            seen_positions: list[int | None] = []

            def fetch_page(checkpoint):
                seen_positions.append(
                    None if checkpoint is None else checkpoint.position
                )
                if len(seen_positions) == 1:
                    raise ProviderUnavailableError("catalog unavailable")
                return page

            observer = _DurableObserver(workspace, [(), ()])
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
                catalog_lifecycle=lifecycle,
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )
            input_id = "catalog:provider-a:event-1"

            gap = loop.run_cycle()
            self.assertEqual(gap.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertEqual(gap.plan.action.value, "zero")
            self.assertIn("ProviderUnavailableError", gap.detail)
            self.assertEqual(observer.calls, 0)
            self.assertIsNone(lifecycle.checkpoint("provider-a"))
            self.assertEqual(seen_positions, [None])

            clock.value = self.START + timedelta(seconds=3)
            recovered = loop.run_cycle()
            self.assertNotEqual(recovered.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertEqual(observer.calls, 1)
            self.assertEqual(lifecycle.checkpoint("provider-a").position, 1)
            self.assertEqual(loop.dependencies.input_ids, (input_id,))
            self.assertEqual(len(lifecycle.records()), 1)
            self.assertEqual(seen_positions, [None, None])

            clock.value = self.START + timedelta(seconds=4)
            loop.run_cycle()
            self.assertEqual(observer.calls, 2)
            self.assertEqual(loop.dependencies.input_ids, (input_id,))
            self.assertEqual(len(lifecycle.records()), 1)
            self.assertEqual(seen_positions, [None, None, 1])
            loop.close()

    def test_local_observation_failure_propagates_without_provider_gap_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [RuntimeError("local SQLite/integrity failure")],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(RuntimeError, "SQLite/integrity failure"):
                loop.run_cycle()

            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )

    def test_suspended_quote_recomputes_input_with_empty_active_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (self._event(sequence=1),),
                    (
                        self._event(
                            sequence=2,
                            status="suspended",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    ),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            loop.run_cycle()
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])

    def test_freshness_expiry_recomputes_without_market_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [(self._event(sequence=1),), ()],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            loop.run_cycle()
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=7)
            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])

    def test_stale_snapshot_cannot_reintroduce_cached_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            stale_event = self._event(sequence=1)
            seed_factory = _PositiveIntentFactory(self.INTENT_CONFIG_SHA256)
            cached_intents = seed_factory(
                "input-a",
                MirrorSnapshot(revision=1, events=(stale_event,)),
            )
            self.assertEqual(len(cached_intents), 1)

            def stale_factory(input_id, snapshot):
                self.assertEqual(input_id, "input-a")
                self.assertEqual(snapshot.events, ())
                return cached_intents

            stale_factory.strategy_version_id = "live-test-strategy-v1"
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(stale_event,)]),
                factory=stale_factory,
                clock=_ManualClock(self.START + timedelta(seconds=7)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "outside the decision-visible snapshot",
            ):
                loop.run_cycle()

            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(loop.book.tickets, {})

    def test_exact_snapshot_allows_intent_bound_to_visible_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(sequence=1)
            snapshot = MirrorSnapshot(revision=1, events=(event,))
            factory = _PositiveIntentFactory(self.INTENT_CONFIG_SHA256)
            intents = factory("input-a", snapshot)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )

            self.assertEqual(
                loop._validated_intents(intents, snapshot=snapshot),
                intents,
            )

    def test_visible_quote_with_foreign_snapshot_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(sequence=1)
            snapshot = MirrorSnapshot(revision=1, events=(event,))
            factory = _PositiveIntentFactory(
                self.INTENT_CONFIG_SHA256,
                snapshot_hash_override="9" * 64,
            )
            intents = factory("input-a", snapshot)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "snapshot identity does not match the decision-visible snapshot",
            ):
                loop._validated_intents(intents, snapshot=snapshot)

    def test_cached_intent_cannot_relabel_unchanged_quote_as_new_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            quoted = self._event(selection="selection-a", sequence=1)
            context_before = self._event(
                selection="selection-b",
                sequence=1,
                odds="3.00",
            )
            context_after = self._event(
                selection="selection-b",
                sequence=2,
                odds="3.10",
            )
            old_snapshot = MirrorSnapshot(
                revision=1,
                events=(quoted, context_before),
            )
            new_snapshot = MirrorSnapshot(
                revision=2,
                events=(quoted, context_after),
            )
            factory = _PositiveIntentFactory(self.INTENT_CONFIG_SHA256)
            cached_intents = factory("input-a", old_snapshot)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "snapshot identity does not match the decision-visible snapshot",
            ):
                loop._validated_intents(cached_intents, snapshot=new_snapshot)

    def test_pause_and_stop_are_durable_and_do_not_poll_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            observer = _DurableObserver(workspace, [()])
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            loop.pause()
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.PAUSED)
            self.assertEqual(observer.calls, 0)

            paused_observer = _DurableObserver(workspace, [()])
            paused_restart = self._loop(
                workspace,
                observer=paused_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            self.assertEqual(paused_restart.dependencies.input_ids, ("input-a",))
            self.assertEqual(
                paused_restart.run_cycle().status,
                LiveCycleStatus.PAUSED,
            )
            self.assertEqual(paused_observer.calls, 0)

            paused_restart.resume()
            paused_restart.stop()
            self.assertEqual(
                paused_restart.run_cycle().status,
                LiveCycleStatus.STOPPED,
            )
            stopped_observer = _DurableObserver(workspace, [()])
            stopped_restart = self._loop(
                workspace,
                observer=stopped_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            self.assertEqual(stopped_restart.dependencies.input_ids, ("input-a",))
            self.assertEqual(
                stopped_restart.run_cycle().status,
                LiveCycleStatus.STOPPED,
            )
            self.assertEqual(stopped_observer.calls, 0)
            with self.assertRaises(RuntimeError):
                stopped_restart.resume()

    def test_durable_dependency_registry_restores_exact_selectors_without_manual_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            first.register_input(
                "exact-input",
                source_ids="provider-a",
                event_ids="event-1",
                market_ids="market-1",
                selection_ids="selection-a",
            )

            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )

            self.assertEqual(resumed.dependencies.input_ids, ("exact-input",))
            result = resumed.run_cycle()
            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                resumed_factory.calls,
                [("exact-input", (("selection-a", 1, "open"),))],
            )

    def test_hot_path_does_not_scan_complete_historical_decision_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            batches = []
            for sequence in range(1, 17):
                batches.append(
                    (
                        self._event(
                            sequence=sequence,
                            odds=f"2.{sequence:02d}",
                            observed=self.START + timedelta(milliseconds=sequence),
                        ),
                    )
                )
            observer = _DurableObserver(workspace, batches)
            factory = _EmptyIntentFactory()
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            for index in range(15):
                clock.value = self.START + timedelta(
                    seconds=1,
                    milliseconds=index,
                )
                self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            before_size = (workspace / "decisions.jsonl").stat().st_size
            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                JsonlDecisionLedger,
                "verified_records",
                side_effect=AssertionError("full Decision Ledger scan is forbidden"),
            ):
                final = loop.run_cycle()

            self.assertEqual(final.status, LiveCycleStatus.DECIDED)
            self.assertGreater((workspace / "decisions.jsonl").stat().st_size, before_size)

    def test_restart_verifies_complete_historical_decision_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (self._event(sequence=1),),
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        ),
                    ],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger_path = workspace / "decisions.jsonl"
            lines = ledger_path.read_bytes().splitlines(keepends=True)
            self.assertEqual(len(lines), 2)
            ledger_path.write_bytes(b"".join(lines + [lines[0]]))

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "duplicate decision_id",
            ):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )

    def test_truncated_committed_decision_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (self._event(sequence=1),),
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        ),
                    ],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger_path = workspace / "decisions.jsonl"
            lines = ledger_path.read_bytes().splitlines(keepends=True)
            self.assertEqual(len(lines), 2)
            ledger_path.write_bytes(lines[0])

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "committed live decision is missing",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_missing_decision_ledger_with_durable_progress_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertTrue(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )
            (workspace / "decisions.jsonl").unlink()

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "missing or unreadable",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_catalog_runtime_discovers_retires_and_restart_preserves_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            lifecycle_path = workspace / "catalog_lifecycle.json"
            lifecycle = ContinuousEventLifecycle(lifecycle_path)
            clock = _ManualClock(self.START + timedelta(seconds=2))
            quote_time = self.START + timedelta(seconds=1)
            quote = MarketEvent(
                event_id="provider-a:event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=quote_time.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=quote_time.isoformat(),
                ingest_ts=quote_time.isoformat(),
                sport="table_tennis",
            )
            pre_match = CatalogEvent(
                source_id="provider-a",
                sport="table_tennis",
                event_id="event-1",
                phase=EventPhase.PRE_MATCH,
                available_at=quote_time.isoformat(),
            )
            completed = CatalogEvent(
                source_id="provider-a",
                sport="table_tennis",
                event_id="event-1",
                phase=EventPhase.COMPLETED,
                available_at=(self.START + timedelta(seconds=4)).isoformat(),
                completion_ref="provider-result:rev-1",
            )
            pre_page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(pre_match,),
            )
            completed_page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-2",
                position=2,
                events=(completed,),
            )
            catalog_state = {"completed": False}

            def fetch_page(checkpoint):
                if catalog_state["completed"]:
                    return completed_page
                return pre_page

            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(quote,), (), (), ()]),
                factory=_EmptyIntentFactory(),
                clock=clock,
                catalog_lifecycle=lifecycle,
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )
            input_id = "catalog:provider-a:event-1"

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(loop.dependencies.input_ids, ())

            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(loop.dependencies.input_ids, (input_id,))
            self.assertIn(input_id, loop._freshness_deadlines)

            catalog_state["completed"] = True
            clock.value = self.START + timedelta(seconds=4)
            third = loop.run_cycle()
            self.assertEqual(third.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(loop.dependencies.input_ids, ())
            self.assertNotIn(input_id, loop._freshness_deadlines)

            clock.value = self.START + timedelta(seconds=10)
            fourth = loop.run_cycle()
            self.assertEqual(fourth.status, LiveCycleStatus.NO_CHANGE)
            self.assertNotIn(input_id, loop._pending_affected)
            loop.close()

            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=11)),
                catalog_lifecycle=ContinuousEventLifecycle(lifecycle_path),
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )
            self.assertEqual(resumed.dependencies.input_ids, ())
            self.assertEqual(
                resumed.catalog_lifecycle.checkpoint("provider-a").position,
                2,
            )
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(resumed.dependencies.input_ids, ())
            resumed.close()

    def test_retired_input_tombstone_blocks_stale_freshness_after_reregister(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            old_deadline = self.START + timedelta(seconds=3)
            loop._freshness_generations["input-a"] = 7
            loop._freshness_deadlines["input-a"] = old_deadline
            loop._freshness_heap.append((old_deadline, "input-a", 7))

            self.assertTrue(loop.unregister_input("input-a"))
            self.assertEqual(loop._freshness_generations["input-a"], 8)
            self.assertNotIn("input-a", loop._freshness_deadlines)

            loop.register_input("input-a", selection_ids="selection-a")
            expired = loop._expire_freshness_inputs(old_deadline + timedelta(seconds=1))
            self.assertEqual(expired, ())
            self.assertEqual(loop._freshness_generations["input-a"], 8)
            self.assertNotIn("input-a", loop._freshness_deadlines)
            loop.close()

    def test_corrupted_dependency_registry_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / PersistentLiveDecisionLoop.INPUTS_FILE_NAME).write_text(
                '{"schema":"autosport.live_decision_inputs","schema":"duplicate"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                )

    def test_corrupted_control_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / PersistentLiveDecisionLoop.CONTROL_FILE_NAME).write_text(
                '{"schema":"autosport.live_decision_control","schema":"duplicate"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                )

    def test_corrupted_progress_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).write_text(
                '{"schema":"autosport.live_decision_progress","schema":"duplicate"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                )


if __name__ == "__main__":
    unittest.main()
