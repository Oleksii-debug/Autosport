from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.ingestion import IngestionStats
from autosport.ingestion_health import SourceHealthStore
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
from autosport.market_mirror_health import ProviderHealthReplayBoundary
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
from autosport.providers import InMemoryProvider, ProviderQuote, ProviderUnavailableError
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
        self.source_sha256 = hashlib.sha256(
            b"tests.test_live_decision_loop:_EmptyIntentFactory:v1"
        ).hexdigest()
        self.environment_sha256 = hashlib.sha256(
            b"tests.test_live_decision_loop:environment:v1"
        ).hexdigest()
        self.config_sha256 = hashlib.sha256(
            b"tests.test_live_decision_loop:empty-intent-config:v1"
        ).hexdigest()
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
    ) -> None:
        self.strategy_version_id = strategy_version_id
        self.source_sha256 = hashlib.sha256(
            b"tests.test_live_decision_loop:_EmptyIntentFactory:v1"
        ).hexdigest()
        self.environment_sha256 = hashlib.sha256(
            b"tests.test_live_decision_loop:environment:v1"
        ).hexdigest()
        self.config_sha256 = config_sha256
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
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash="9" * 64,
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
        if book is None:
            canonical_book_path = workspace / "paper_book.json"
            selected_book = (
                PaperBook.load(canonical_book_path)
                if canonical_book_path.exists()
                else PaperBook("1000")
            )
        else:
            selected_book = book
        return PersistentLiveDecisionLoop(
            workspace,
            loop_id="live-test-loop",
            mode=LiveDecisionMode.PAPER,
            book=selected_book,
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
                "factory strategy_version_id provenance changed",
            ):
                loop._decision_context_sha256()

    def test_factory_source_environment_and_config_must_match_registry(self) -> None:
        for attribute in (
            "source_sha256",
            "environment_sha256",
            "config_sha256",
        ):
            with self.subTest(attribute=attribute):
                with tempfile.TemporaryDirectory() as directory:
                    workspace = Path(directory)
                    factory = _EmptyIntentFactory()
                    setattr(factory, attribute, "f" * 64)
                    with self.assertRaisesRegex(
                        LiveDecisionProgressError,
                        rf"factory {attribute} provenance changed",
                    ):
                        self._loop(
                            workspace,
                            observer=_DurableObserver(workspace, [()]),
                            factory=factory,
                            clock=_ManualClock(self.START),
                        )

    def test_factory_scientific_provenance_cannot_mutate_after_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=factory,
                clock=_ManualClock(self.START),
            )

            for attribute in (
                "source_sha256",
                "environment_sha256",
                "config_sha256",
            ):
                original = getattr(factory, attribute)
                setattr(factory, attribute, "f" * 64)
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    rf"factory {attribute} provenance changed",
                ):
                    loop._decision_context_sha256()
                setattr(factory, attribute, original)

    def test_intent_factory_cannot_observe_ephemeral_mirror_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seen_revisions = []

            def revision_sensitive_factory(input_id, snapshot):
                del input_id
                seen_revisions.append(snapshot.revision)
                if snapshot.revision != 0:
                    raise AssertionError(
                        "ephemeral mirror revision reached economic strategy input"
                    )
                return ()

            revision_sensitive_factory.strategy_version_id = "live-test-strategy-v1"
            revision_sensitive_factory.source_sha256 = self.INTENT_SOURCE_SHA256
            revision_sensitive_factory.environment_sha256 = (
                self.INTENT_ENVIRONMENT_SHA256
            )
            revision_sensitive_factory.config_sha256 = self.INTENT_CONFIG_SHA256
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=revision_sensitive_factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(seen_revisions, [0])
            self.assertGreater(loop.mirror_updates.mirror.revision, 0)

    def test_intent_factories_receive_isolated_market_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            base = self._event(selection="selection-a", sequence=1)
            event = MarketEvent.from_dict(
                {
                    **base.to_dict(),
                    "metadata": {"nested": {"origin": "canonical"}},
                }
            )
            seen = []

            def mutating_factory(input_id, snapshot):
                self.assertEqual(len(snapshot.events), 1)
                metadata = snapshot.events[0].metadata
                seen.append((input_id, metadata["nested"]["origin"]))
                metadata["nested"]["origin"] = f"mutated-by-{input_id}"
                return ()

            mutating_factory.strategy_version_id = "live-test-strategy-v1"
            mutating_factory.source_sha256 = self.INTENT_SOURCE_SHA256
            mutating_factory.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            mutating_factory.config_sha256 = self.INTENT_CONFIG_SHA256
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=mutating_factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            loop.register_input("input-b", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                seen,
                [
                    ("input-a", "canonical"),
                    ("input-b", "canonical"),
                ],
            )
            durable = loop.mirror_updates.mirror.snapshot()
            self.assertEqual(
                durable[0].metadata,
                {"nested": {"origin": "canonical"}},
            )

    def test_pending_replay_hides_reconstructed_mirror_revision_from_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seen_revisions = []

            def fail_after_pending(input_id, snapshot):
                del input_id
                seen_revisions.append(snapshot.revision)
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256
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
            self.assertEqual(seen_revisions, [0])

            replay_revisions = []

            def replay_factory(input_id, snapshot):
                del input_id
                replay_revisions.append(snapshot.revision)
                return ()

            replay_factory.strategy_version_id = "live-test-strategy-v1"
            replay_factory.source_sha256 = self.INTENT_SOURCE_SHA256
            replay_factory.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            replay_factory.config_sha256 = self.INTENT_CONFIG_SHA256
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=replay_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(replay_revisions, [0])

    def test_factory_provenance_is_checked_before_live_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=factory,
                clock=_ManualClock(self.START),
            )
            factory.source_sha256 = "f" * 64
            snapshot = loop.mirror_updates.mirror.view()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "factory source_sha256 provenance changed",
            ):
                loop._refresh_intents_from_snapshots(
                    {"input-a": snapshot},
                )

            self.assertEqual(factory.calls, [])
            self.assertNotIn("input-a", loop._intent_cache)

    def test_factory_self_provenance_mutation_is_rejected_before_cache_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            calls = []

            def self_mutating_factory(input_id, snapshot):
                calls.append(input_id)
                self_mutating_factory.source_sha256 = "f" * 64
                return ()

            self_mutating_factory.strategy_version_id = "live-test-strategy-v1"
            self_mutating_factory.source_sha256 = self.INTENT_SOURCE_SHA256
            self_mutating_factory.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            self_mutating_factory.config_sha256 = self.INTENT_CONFIG_SHA256
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=self_mutating_factory,
                clock=_ManualClock(self.START),
            )
            snapshot = loop.mirror_updates.mirror.view()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "factory source_sha256 provenance changed",
            ):
                loop._refresh_intents_from_snapshots(
                    {"input-a": snapshot},
                )

            self.assertEqual(calls, ["input-a"])
            self.assertNotIn("input-a", loop._intent_cache)

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
                loop._validated_intents((forged,))

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
                    provider_health_boundaries=(),
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
            self.assertEqual(record.payload["schema_version"], 3)
            self.assertEqual(
                record.payload["intent_strategy_version_id"],
                loop.intent_provenance.strategy_version_id,
            )
            self.assertIsNone(record.payload["intent_model_version_id"])
            self.assertEqual(
                record.payload["intent_provenance_sha256"],
                loop.intent_provenance.provenance_sha256,
            )

    def test_health_bound_live_decision_persists_exact_horizon_in_progress_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_time = self.START + timedelta(milliseconds=500)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            health_store = SourceHealthStore(workspace / "source_health.json")
            health_store.record_success(
                "provider-a",
                now=health_time.isoformat(),
                received=1,
                accepted=1,
                rejected=0,
                cursor="1",
                latest_source_ts=self.START.isoformat(),
                quality_flags=(),
            )
            loop._default_health_store = health_store
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            expected_boundary = {
                "source_id": "provider-a",
                "recorded_at": health_time.isoformat(),
                "transition_order": 1,
            }
            progress = json.loads(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(progress["schema_version"], 2)
            self.assertEqual(
                progress["provider_health_boundaries"],
                [expected_boundary],
            )
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            detached_payload = record.to_dict()["payload"]
            self.assertEqual(detached_payload["schema_version"], 3)
            self.assertEqual(
                detached_payload["provider_health_boundaries"],
                [expected_boundary],
            )

    def test_custom_observer_adopts_existing_canonical_health_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_time = self.START + timedelta(milliseconds=500)
            health_store = SourceHealthStore(workspace / "source_health.json")
            health_store.record_success(
                "provider-a",
                now=health_time.isoformat(),
                received=1,
                accepted=1,
                rejected=0,
                cursor="1",
                latest_source_ts=self.START.isoformat(),
                quality_flags=(),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            progress = loop._load_progress()
            self.assertIsNotNone(progress)
            self.assertEqual(
                progress.provider_health_boundaries,
                (
                    ProviderHealthReplayBoundary(
                        source_id="provider-a",
                        recorded_at=health_time.isoformat(),
                        transition_order=1,
                    ),
                ),
            )
            self.assertIsNotNone(loop._default_health_store)
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(
                record.to_dict()["payload"]["provider_health_boundaries"],
                [
                    {
                        "source_id": "provider-a",
                        "recorded_at": health_time.isoformat(),
                        "transition_order": 1,
                    }
                ],
            )

    def test_custom_observer_cannot_bypass_failed_canonical_health_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_store = SourceHealthStore(workspace / "source_health.json")
            health_store.record_failure(
                "provider-a",
                now=(self.START + timedelta(milliseconds=500)).isoformat(),
                error=TimeoutError("durable provider failure"),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("provider health", result.detail)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )
            self.assertIsNotNone(loop._default_health_store)
            self.assertEqual(
                loop._default_health_store.get("provider-a").status,
                "failed",
            )

    def test_custom_observer_provider_gap_binds_canonical_failed_health_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_time = self.START + timedelta(milliseconds=500)
            health_store = SourceHealthStore(workspace / "source_health.json")

            def failing_observer(updates):
                del updates
                health_store.record_failure(
                    "provider-a",
                    now=health_time.isoformat(),
                    error=ProviderUnavailableError("provider offline"),
                )
                raise ProviderUnavailableError("provider offline")

            loop = self._loop(
                workspace,
                observer=failing_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.PROVIDER_GAP)
            progress = loop._load_progress()
            self.assertIsNotNone(progress)
            self.assertEqual(progress.gate, "provider_gap")
            self.assertEqual(
                progress.provider_health_boundaries,
                (
                    ProviderHealthReplayBoundary(
                        source_id="provider-a",
                        recorded_at=health_time.isoformat(),
                        transition_order=1,
                    ),
                ),
            )
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            payload = record.to_dict()["payload"]
            self.assertEqual(payload["gate"], "provider_gap")
            self.assertEqual(
                payload["provider_health_boundaries"],
                [
                    {
                        "source_id": "provider-a",
                        "recorded_at": health_time.isoformat(),
                        "transition_order": 1,
                    }
                ],
            )

    def test_custom_observer_corrupt_canonical_health_authority_fails_closed(self) -> None:
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
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )
            (workspace / "source_health.json").write_text(
                '{"schema_version":3,"sources":',
                encoding="utf-8",
            )

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("health authority is unreadable", result.detail)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )

    def test_health_advance_after_capture_backpressures_before_pending_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_store = SourceHealthStore(workspace / "source_health.json")
            health_store.record_success(
                "provider-a",
                now=(self.START + timedelta(milliseconds=500)).isoformat(),
                received=1,
                accepted=1,
                rejected=0,
                cursor="1",
                latest_source_ts=self.START.isoformat(),
                quality_flags=(),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop._default_health_store = health_store
            loop.register_input("input-a", selection_ids="selection-a")
            original_capture = loop._capture_provider_health_boundaries

            def capture_then_advance(*args, **kwargs):
                boundaries = original_capture(*args, **kwargs)
                health_store.record_failure(
                    "provider-a",
                    now=(decision_time + timedelta(milliseconds=250)).isoformat(),
                    error=TimeoutError("health advanced after decision capture"),
                )
                return boundaries

            with patch.object(
                loop,
                "_capture_provider_health_boundaries",
                side_effect=capture_then_advance,
            ):
                result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("provider health advanced", result.detail)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )
            self.assertEqual(health_store.get("provider-a").status, "failed")

    def test_pending_restart_replays_bound_health_horizon_after_later_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_time = self.START + timedelta(milliseconds=500)
            health_store = SourceHealthStore(workspace / "source_health.json")
            health_store.record_success(
                "provider-a",
                now=health_time.isoformat(),
                received=1,
                accepted=1,
                rejected=0,
                cursor="1",
                latest_source_ts=self.START.isoformat(),
                quality_flags=(),
            )

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
                raise RuntimeError("simulated process loss after health-bound pending")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(decision_time),
            )
            first._default_health_store = health_store
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(
                RuntimeError,
                "health-bound pending",
            ):
                first.run_cycle()

            pending = json.loads(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(
                pending["provider_health_boundaries"][0]["transition_order"],
                1,
            )

            health_store.record_failure(
                "provider-a",
                now=(self.START + timedelta(milliseconds=750)).isoformat(),
                error=TimeoutError("later durable provider failure"),
            )
            self.assertEqual(health_store.get("provider-a").status, "failed")

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            resumed._default_health_store = SourceHealthStore(
                workspace / "source_health.json"
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(
                record.to_dict()["payload"]["provider_health_boundaries"][0][
                    "transition_order"
                ],
                1,
            )
            self.assertEqual(
                resumed._default_health_store.get("provider-a").status,
                "failed",
            )

    def test_empty_default_provider_snapshot_still_binds_health_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            registry = self._scientific_registry(
                workspace,
                self._strategy_version(),
            )
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=registry,
                provider=InMemoryProvider("provider-a", []),
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            progress = loop._load_progress()
            self.assertIsNotNone(progress)
            self.assertEqual(len(progress.provider_health_boundaries), 1)
            self.assertEqual(
                progress.provider_health_boundaries[0].source_id,
                "provider-a",
            )
            self.assertEqual(
                progress.provider_health_boundaries[0].transition_order,
                1,
            )
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(
                record.to_dict()["payload"]["provider_health_boundaries"][0][
                    "source_id"
                ],
                "provider-a",
            )
            loop.close()

    def test_all_registered_provider_dependencies_require_health_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=1)
            health_store = SourceHealthStore(workspace / "source_health.json")
            health_store.record_success(
                "provider-a",
                now=(self.START + timedelta(milliseconds=500)).isoformat(),
                received=1,
                accepted=1,
                rejected=0,
                cursor="1",
                latest_source_ts=self.START.isoformat(),
                quality_flags=(),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),), ()],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop._default_health_store = health_store
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )
            loop.register_input(
                "input-b",
                source_ids="provider-b",
                selection_ids="selection-b",
            )

            blocked = loop.run_cycle()

            self.assertEqual(blocked.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("provider health", blocked.detail)
            self.assertFalse((workspace / "decisions.jsonl").exists())

            health_store.record_success(
                "provider-b",
                now=(self.START + timedelta(milliseconds=750)).isoformat(),
                received=0,
                accepted=0,
                rejected=0,
                cursor="0",
                latest_source_ts=None,
                quality_flags=(),
            )
            decided = loop.run_cycle()

            self.assertEqual(decided.status, LiveCycleStatus.DECIDED)
            progress = loop._load_progress()
            self.assertEqual(
                tuple(
                    boundary.source_id
                    for boundary in progress.provider_health_boundaries
                ),
                ("provider-a", "provider-b"),
            )

    def test_provider_gap_zero_decision_preserves_failed_health_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            registry = self._scientific_registry(
                workspace,
                self._strategy_version(),
            )

            class FailingProvider:
                source_id = "provider-a"

                def read_batch(self, max_items=1000):
                    del max_items
                    raise ProviderUnavailableError("provider offline")

            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=registry,
                provider=FailingProvider(),
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input(
                "input-a",
                source_ids="provider-a",
                selection_ids="selection-a",
            )

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.PROVIDER_GAP)
            progress = loop._load_progress()
            self.assertEqual(progress.gate, "provider_gap")
            self.assertEqual(len(progress.provider_health_boundaries), 1)
            boundary = progress.provider_health_boundaries[0]
            self.assertEqual(boundary.source_id, "provider-a")
            self.assertEqual(boundary.transition_order, 1)
            self.assertEqual(
                SourceHealthStore(
                    workspace / "source_health.json"
                ).get("provider-a").status,
                "failed",
            )
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            payload = record.to_dict()["payload"]
            self.assertEqual(payload["gate"], "provider_gap")
            self.assertEqual(
                payload["provider_health_boundaries"][0]["transition_order"],
                1,
            )
            self.assertFalse(any(stake > 0 for stake in result.plan.stakes))
            loop.close()

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

    def test_input_registration_permutation_has_one_canonical_decision_identity(self) -> None:
        with tempfile.TemporaryDirectory() as left_directory, tempfile.TemporaryDirectory() as right_directory:
            left_workspace = Path(left_directory)
            right_workspace = Path(right_directory)
            clock_left = _ManualClock(self.START + timedelta(seconds=1))
            clock_right = _ManualClock(self.START + timedelta(seconds=1))
            batch = (
                self._event(selection="selection-a", sequence=1),
                self._event(selection="selection-b", sequence=1),
            )
            left = self._loop(
                left_workspace,
                observer=_DurableObserver(left_workspace, [batch]),
                factory=_EmptyIntentFactory(),
                clock=clock_left,
            )
            right = self._loop(
                right_workspace,
                observer=_DurableObserver(right_workspace, [batch]),
                factory=_EmptyIntentFactory(),
                clock=clock_right,
            )

            left.register_input("input-b", selection_ids="selection-b")
            left.register_input("input-a", selection_ids="selection-a")
            right.register_input("input-a", selection_ids="selection-a")
            right.register_input("input-b", selection_ids="selection-b")

            self.assertEqual(left.dependencies.input_ids, ("input-a", "input-b"))
            self.assertEqual(right.dependencies.input_ids, ("input-a", "input-b"))

            left_result = left.run_cycle()
            right_result = right.run_cycle()

            self.assertEqual(left_result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(right_result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(left_result.affected_input_ids, ("input-a", "input-b"))
            self.assertEqual(right_result.affected_input_ids, ("input-a", "input-b"))
            self.assertEqual(left_result.plan.plan_sha256, right_result.plan.plan_sha256)
            self.assertEqual(left_result.decision_id, right_result.decision_id)

            left_record = JsonlDecisionLedger(
                left_workspace / "decisions.jsonl"
            ).verified_records()[0]
            right_record = JsonlDecisionLedger(
                right_workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(
                left_record.payload["market_state_sha256"],
                right_record.payload["market_state_sha256"],
            )
            self.assertEqual(
                left_record.payload["decision_context_sha256"],
                right_record.payload["decision_context_sha256"],
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

    def test_concurrent_update_during_multi_input_cut_retries_before_decision(self) -> None:
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
            )
            self._register_two(loop)

            real_capture = loop.dependencies.coherent_decision_views
            injected = {"done": False}
            concurrent = self._event(
                selection="selection-b",
                sequence=2,
                odds="3.20",
                observed=self.START + timedelta(milliseconds=1500),
            )

            def capture_then_advance(*args, **kwargs):
                snapshots = real_capture(*args, **kwargs)
                if not injected["done"]:
                    injected["done"] = True
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(concurrent)
                    finally:
                        store.close()
                    loop.mirror_updates.accept_persisted(concurrent)
                return snapshots

            with patch.object(
                loop.dependencies,
                "coherent_decision_views",
                side_effect=capture_then_advance,
            ):
                first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertEqual(first.affected_input_ids, ("input-a", "input-b"))
            self.assertIn("revision advanced", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [
                    ("input-a", (("selection-a", 1, "open"),)),
                    ("input-b", (("selection-b", 2, "open"),)),
                ],
            )
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 1)

    def test_drained_unrouted_key_retries_before_decision_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (self._event(selection="selection-a", sequence=1),),
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
            loop.register_input("input-all", source_ids="provider-a")

            real_capture = loop.dependencies.coherent_decision_views
            real_write_pending = loop._write_pending
            state = {"injected": False, "routed": False, "batch": None}
            concurrent = self._event(
                selection="selection-b",
                sequence=2,
                odds="3.20",
                observed=self.START + timedelta(milliseconds=500),
            )

            def inject_and_drain_before_capture(*args, **kwargs):
                if not state["injected"]:
                    state["injected"] = True
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(concurrent)
                    finally:
                        store.close()
                    loop.mirror_updates.accept_persisted(concurrent)
                    state["batch"] = loop.mirror_updates.drain()
                    self.assertEqual(loop.mirror_updates.pending_count, 0)
                return real_capture(*args, **kwargs)

            def route_before_pending_publication(*args, **kwargs):
                if not state["routed"]:
                    state["routed"] = True
                    affected = loop.dependencies.affected_inputs(state["batch"])
                    self.assertEqual(affected, ("input-all",))
                return real_write_pending(*args, **kwargs)

            with (
                patch.object(
                    loop.dependencies,
                    "coherent_decision_views",
                    side_effect=inject_and_drain_before_capture,
                ),
                patch.object(
                    loop,
                    "_write_pending",
                    side_effect=route_before_pending_publication,
                ),
            ):
                first = loop.run_cycle()

            self.assertTrue(state["injected"])
            self.assertTrue(state["routed"])
            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("dependency registry/routing changed", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(loop.progress_path.exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(len(factory.calls), 1)
            self.assertEqual(factory.calls[0][0], "input-all")
            self.assertEqual(
                {item[0] for item in factory.calls[0][1]},
                {"selection-a", "selection-b"},
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )


    def test_external_canonical_paperbook_advance_retries_before_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            book = PaperBook("1000")
            ledger = PaperExecutionLedger(workspace / "paper-execution.jsonl")
            execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=PaperExecutionModelConfig(
                    model_id="live-book-currentness-test",
                    model_version="1",
                    evidence_grade=EvidenceGrade.SYNTHETIC,
                    evidence_source="live-book-currentness-test",
                    seed="live-book-currentness-test",
                    max_quote_age_ms=5_000,
                    min_delay_ms=0,
                    max_delay_ms=0,
                    rejected_bps=0,
                    partial_bps=0,
                    unknown_bps=0,
                    partial_fill_bps=5000,
                    max_slippage_bps=0,
                ),
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
                book=book,
                paper_execution=execution,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            externally_advanced = PaperBook.load(workspace / "paper_book.json")
            externally_advanced.open_ticket(
                [
                    TicketLeg(
                        event_id="external-event",
                        market_id="external-market",
                        selection_id="external-selection",
                        locked_odds=Decimal("2"),
                        sport="table_tennis",
                        exchange_side="back",
                    )
                ],
                Decimal("10"),
                placed_at=(
                    self.START + timedelta(milliseconds=500)
                ).isoformat(),
            )
            externally_advanced.save(workspace / "paper_book.json")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("canonical PaperBook advanced", result.detail)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(loop.progress_path.exists())

    def test_portfolio_change_during_market_cut_retries_before_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            book = PaperBook("1000")
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),), ()],
                ),
                factory=factory,
                clock=clock,
                book=book,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            real_capture = loop.dependencies.coherent_decision_views
            injected = {"done": False}

            def capture_then_change_portfolio(*args, **kwargs):
                snapshots = real_capture(*args, **kwargs)
                if not injected["done"]:
                    injected["done"] = True
                    book.open_ticket(
                        [
                            TicketLeg(
                                event_id="existing-event",
                                market_id="existing-market",
                                selection_id="existing-selection",
                                locked_odds=Decimal("2"),
                                sport="table_tennis",
                                exchange_side="back",
                            )
                        ],
                        Decimal("10"),
                        placed_at=(
                            clock.value + timedelta(microseconds=1)
                        ).isoformat(),
                    )
                return snapshots

            with patch.object(
                loop.dependencies,
                "coherent_decision_views",
                side_effect=capture_then_change_portfolio,
            ):
                first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("portfolio/risk/dependency context advanced", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(loop.progress_path.exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )
            self.assertEqual(
                second.plan.portfolio_sha256,
                loop.authority.risk_policy.risk_of_ruin_portfolio_sha256(book),
            )

    def test_dependency_registry_change_during_market_cut_retries_before_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (
                            self._event(selection="selection-a", sequence=1),
                            self._event(selection="selection-b", sequence=1),
                        ),
                        (),
                    ],
                ),
                factory=factory,
                clock=clock,
            )
            self._register_two(loop)

            real_capture = loop.dependencies.coherent_decision_views
            injected = {"done": False}

            def capture_then_retire_input(*args, **kwargs):
                snapshots = real_capture(*args, **kwargs)
                if not injected["done"]:
                    injected["done"] = True
                    self.assertTrue(loop.unregister_input("input-b"))
                return snapshots

            with patch.object(
                loop.dependencies,
                "coherent_decision_views",
                side_effect=capture_then_retire_input,
            ):
                first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("dependency registry advanced", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(loop.progress_path.exists())
            self.assertEqual(loop.dependencies.input_ids, ("input-a",))

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_same_id_dependency_registry_aba_retries_before_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),), ()],
                ),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            real_capture = loop.dependencies.coherent_decision_views
            injected = {"done": False}

            def capture_then_rebind_same_id(*args, **kwargs):
                snapshots = real_capture(*args, **kwargs)
                if not injected["done"]:
                    injected["done"] = True
                    self.assertTrue(loop.unregister_input("input-a"))
                    loop.register_input("input-a", selection_ids="selection-b")
                return snapshots

            with patch.object(
                loop.dependencies,
                "coherent_decision_views",
                side_effect=capture_then_rebind_same_id,
            ):
                first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("portfolio/risk/dependency context advanced", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(loop.progress_path.exists())
            self.assertEqual(loop.dependencies.input_ids, ("input-a",))

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_update_during_final_coherence_getter_retries_before_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [(self._event(selection="selection-a", sequence=1),), ()],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            concurrent = self._event(
                selection="selection-a",
                sequence=2,
                odds="2.20",
                observed=self.START + timedelta(milliseconds=500),
            )
            descriptor = type(loop.mirror_updates).full_refresh_required
            injected = {"done": False}

            def final_gate_then_advance(instance):
                prior = descriptor.__get__(instance, type(instance))
                if not injected["done"]:
                    injected["done"] = True
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(concurrent)
                    finally:
                        store.close()
                    instance.accept_persisted(concurrent)
                return prior

            with patch.object(
                type(loop.mirror_updates),
                "full_refresh_required",
                property(final_gate_then_advance),
            ):
                first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("revision advanced", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 2, "open"),))],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_future_local_availability_cannot_enter_old_decision_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            late = self._event(
                selection="selection-a",
                sequence=1,
                observed=self.START + timedelta(seconds=2),
            )
            late = MarketEvent.from_dict(
                {
                    **late.to_dict(),
                    "source_ts": self.START.isoformat(),
                }
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(late,), ()]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("not causally available", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_future_source_time_waits_until_causal_without_new_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            future_source = self._event(
                selection="selection-a",
                sequence=1,
                observed=self.START,
            )
            future_source = MarketEvent.from_dict(
                {
                    **future_source.to_dict(),
                    "source_ts": (
                        self.START + timedelta(seconds=2)
                    ).isoformat(),
                    "ingest_ts": (
                        self.START + timedelta(seconds=1)
                    ).isoformat(),
                }
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(future_source,), ()]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("source evidence from the future", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_future_observation_fallback_waits_without_new_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            future_observed = self._event(
                selection="selection-a",
                sequence=1,
                observed=self.START + timedelta(seconds=2),
            )
            future_observed = MarketEvent.from_dict(
                {
                    **future_observed.to_dict(),
                    "source_ts": None,
                    "ingest_ts": (
                        self.START + timedelta(seconds=1)
                    ).isoformat(),
                }
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(future_observed,), ()]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("source evidence from the future", first.detail)
            self.assertEqual(factory.calls, [])

            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )

    def test_future_local_stale_provider_evidence_does_not_backpressure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=10))
            stale = self._event(
                selection="selection-a",
                sequence=1,
                observed=self.START + timedelta(seconds=11),
            )
            stale = MarketEvent.from_dict(
                {
                    **stale.to_dict(),
                    "source_ts": (
                        self.START - timedelta(seconds=10)
                    ).isoformat(),
                }
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(stale,)]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_future_local_closed_evidence_does_not_backpressure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            closed = self._event(
                selection="selection-a",
                sequence=1,
                observed=self.START + timedelta(seconds=2),
                status="closed",
            )
            closed = MarketEvent.from_dict(
                {
                    **closed.to_dict(),
                    "source_ts": self.START.isoformat(),
                }
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(closed,)]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_wrapped_observation_health_disagreement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)

            def observer(updates):
                store = SQLiteMarketStore(workspace / "market.db")
                try:
                    bus = MarketEventBus(store)
                    bus.subscribe(updates.accept_persisted)
                    bus.publish(event)
                finally:
                    store.close()
                return SimpleNamespace(
                    stats=IngestionStats(
                        source_id=event.source_id,
                        received=1,
                        accepted=1,
                        rejected=0,
                        elapsed_seconds=0.01,
                        cursor="1",
                        quality_flags=(),
                        health_status="healthy",
                    ),
                    health=SimpleNamespace(status="degraded"),
                )

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("quality is degraded", result.detail)
            self.assertEqual(factory.calls, [])
            self.assertEqual(loop.mirror_updates.pending_count, 1)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_degraded_observation_blocks_economic_cut_until_healthy_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)
            calls = {"value": 0}

            def observer(updates):
                calls["value"] += 1
                if calls["value"] == 1:
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        bus = MarketEventBus(store)
                        bus.subscribe(updates.accept_persisted)
                        bus.publish(event)
                    finally:
                        store.close()
                    return SimpleNamespace(
                        stats=IngestionStats(
                            source_id=event.source_id,
                            received=1,
                            accepted=1,
                            rejected=0,
                            elapsed_seconds=0.01,
                            cursor="1",
                            quality_flags=("PROVIDER_SEQUENCE_GAP",),
                            health_status="degraded",
                        )
                    )
                return IngestionStats(
                    source_id=event.source_id,
                    received=0,
                    accepted=0,
                    rejected=0,
                    elapsed_seconds=0.01,
                    cursor="1",
                    quality_flags=(),
                    health_status="healthy",
                )

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            degraded = loop.run_cycle()

            self.assertEqual(degraded.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("quality is degraded", degraded.detail)
            self.assertEqual(factory.calls, [])
            self.assertTrue(loop._needs_cache_rebuild)
            self.assertEqual(loop.mirror_updates.pending_count, 1)
            self.assertFalse((workspace / "decisions.jsonl").exists())

            healthy = loop.run_cycle()

            self.assertEqual(healthy.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(loop.mirror_updates.pending_count, 0)
            self.assertFalse(loop._needs_cache_rebuild)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_default_provider_tolerated_future_skew_waits_then_activates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            registry = self._scientific_registry(
                workspace,
                self._strategy_version(),
            )
            provider_time = self.START + timedelta(seconds=3)
            provider = InMemoryProvider(
                "provider-a",
                [
                    ProviderQuote(
                        provider_event_id="event-1",
                        provider_market_id="winner",
                        provider_selection_id="selection-a",
                        decimal_odds=Decimal("2.00"),
                        observed_ts=self.START.isoformat(),
                        sequence=1,
                        source_ts=provider_time.isoformat(),
                    )
                ],
            )
            factory = _EmptyIntentFactory()
            decision_clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=registry,
                provider=provider,
                ingestion_policy=IngestionPolicy(
                    max_batch_size=100,
                    stale_after_seconds=60,
                    max_future_skew_seconds=5,
                ),
                max_quote_age=timedelta(seconds=5),
                clock=decision_clock,
            )
            loop.register_input("input-a", source_ids="provider-a")
            receipt_clock = {
                "value": (self.START + timedelta(seconds=1)).isoformat()
            }

            with patch(
                "autosport.ingestion._utc_now_iso",
                side_effect=lambda: receipt_clock["value"],
            ):
                first = loop.run_cycle()

                self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
                self.assertIn("source evidence from the future", first.detail)
                self.assertEqual(factory.calls, [])
                self.assertFalse((workspace / "decisions.jsonl").exists())
                self.assertEqual(loop.mirror_updates.pending_count, 0)

                decision_clock.value = self.START + timedelta(seconds=4)
                receipt_clock["value"] = (
                    self.START + timedelta(seconds=4)
                ).isoformat()
                second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("provider-a:selection-a", 1, "open"),))],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )
            loop.close()

    def test_default_provider_large_future_skew_survives_degrade_then_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            registry = self._scientific_registry(
                workspace,
                self._strategy_version(),
            )
            provider_time = self.START + timedelta(seconds=10)
            provider = InMemoryProvider(
                "provider-a",
                [
                    ProviderQuote(
                        provider_event_id="event-1",
                        provider_market_id="winner",
                        provider_selection_id="selection-a",
                        decimal_odds=Decimal("2.00"),
                        observed_ts=self.START.isoformat(),
                        sequence=1,
                        source_ts=provider_time.isoformat(),
                    )
                ],
            )
            factory = _EmptyIntentFactory()
            decision_clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=registry,
                provider=provider,
                ingestion_policy=IngestionPolicy(
                    max_batch_size=100,
                    stale_after_seconds=60,
                    max_future_skew_seconds=5,
                ),
                max_quote_age=timedelta(seconds=5),
                clock=decision_clock,
            )
            loop.register_input("input-a", source_ids="provider-a")
            receipt_clock = {
                "value": (self.START + timedelta(seconds=1)).isoformat()
            }

            with patch(
                "autosport.ingestion._utc_now_iso",
                side_effect=lambda: receipt_clock["value"],
            ):
                degraded = loop.run_cycle()
                self.assertEqual(degraded.status, LiveCycleStatus.BACKPRESSURE)
                self.assertIn("quality is degraded", degraded.detail)
                self.assertEqual(loop.mirror_updates.pending_count, 1)
                self.assertEqual(factory.calls, [])

                decision_clock.value = self.START + timedelta(seconds=6)
                receipt_clock["value"] = (
                    self.START + timedelta(seconds=6)
                ).isoformat()
                still_future = loop.run_cycle()
                self.assertEqual(
                    still_future.status,
                    LiveCycleStatus.BACKPRESSURE,
                )
                self.assertIn(
                    "source evidence from the future",
                    still_future.detail,
                )
                self.assertEqual(loop.mirror_updates.pending_count, 0)
                self.assertEqual(factory.calls, [])

                decision_clock.value = self.START + timedelta(seconds=11)
                receipt_clock["value"] = (
                    self.START + timedelta(seconds=11)
                ).isoformat()
                recovered = loop.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("provider-a:selection-a", 1, "open"),))],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )
            loop.close()

    def test_default_provider_degraded_quality_blocks_economic_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            registry = self._scientific_registry(
                workspace,
                self._strategy_version(),
            )
            receive_time = self.START + timedelta(seconds=1)
            provider = InMemoryProvider(
                "provider-a",
                [
                    ProviderQuote(
                        provider_event_id="event-1",
                        provider_market_id="winner",
                        provider_selection_id="selection-a",
                        decimal_odds=Decimal("2.00"),
                        observed_ts=self.START.isoformat(),
                        sequence=1,
                        source_ts=self.START.isoformat(),
                    )
                ],
                quality_flags=("PROVIDER_SEQUENCE_GAP",),
            )
            factory = _EmptyIntentFactory()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=registry,
                provider=provider,
                ingestion_policy=IngestionPolicy(
                    max_batch_size=100,
                    stale_after_seconds=60,
                    max_future_skew_seconds=5,
                ),
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(receive_time + timedelta(seconds=1)),
            )
            loop.register_input("input-a", source_ids="provider-a")

            with patch(
                "autosport.ingestion._utc_now_iso",
                return_value=receive_time.isoformat(),
            ):
                result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("quality is degraded", result.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(loop.mirror_updates.pending_count, 1)
            health = SourceHealthStore(
                workspace / "source_health.json"
            ).get("provider-a")
            self.assertEqual(health.status, "degraded")
            self.assertEqual(
                health.quality_flags,
                ("PROVIDER_SEQUENCE_GAP",),
            )
            persisted = SQLiteMarketStore(workspace / "market.db")
            try:
                events = persisted.events()
            finally:
                persisted.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].ingest_ts, receive_time.isoformat())
            loop.close()

    def test_market_update_after_coherent_capture_cannot_publish_stale_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            first_event = self._event(selection="selection-a", sequence=1)
            concurrent = self._event(
                selection="selection-a",
                sequence=2,
                odds="2.20",
                observed=self.START + timedelta(milliseconds=1500),
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(first_event,), ()]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            real_write_pending = loop._write_pending
            injected = {"done": False}

            def advance_after_capture(*args, **kwargs):
                if not injected["done"]:
                    injected["done"] = True
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(concurrent)
                    finally:
                        store.close()
                    loop.mirror_updates.accept_persisted(concurrent)
                return real_write_pending(*args, **kwargs)

            with patch.object(
                loop,
                "_write_pending",
                side_effect=advance_after_capture,
            ):
                first = loop.run_cycle()

            self.assertTrue(injected["done"])
            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn(
                "market revision advanced after decision snapshot capture",
                first.detail,
            )
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertIsNone(loop._load_progress())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_reentrant_market_mutation_is_blocked_before_pending_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            first_event = self._event(selection="selection-a", sequence=1)
            concurrent = self._event(
                selection="selection-a",
                sequence=2,
                odds="2.20",
                observed=self.START + timedelta(milliseconds=1500),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(first_event,)]),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            attempted = {"value": False}

            def mutate_during_progress_write(path, payload):
                self.assertEqual(Path(path), loop.progress_path)
                attempted["value"] = True
                loop.mirror_updates.mirror.apply(concurrent)
                self.fail("reentrant mirror mutation unexpectedly passed revision guard")

            with patch(
                "autosport.live_decision_loop.atomic_write_json",
                side_effect=mutate_during_progress_write,
            ):
                result = loop.run_cycle()

            self.assertTrue(attempted["value"])
            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn(
                "market revision advanced after decision snapshot capture",
                result.detail,
            )
            self.assertIsNone(loop._load_progress())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(loop.mirror_updates.mirror.revision, 1)

    def test_reentrant_dependency_registration_is_blocked_before_pending_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            first_event = self._event(selection="selection-a", sequence=1)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(first_event,)]),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            attempted = {"value": False}

            def mutate_during_progress_write(path, payload):
                self.assertEqual(Path(path), loop.progress_path)
                attempted["value"] = True
                loop.dependencies.register(
                    "input-b",
                    selection_ids="selection-b",
                )
                self.fail("reentrant dependency mutation unexpectedly passed registry guard")

            with patch(
                "autosport.live_decision_loop.atomic_write_json",
                side_effect=mutate_during_progress_write,
            ):
                result = loop.run_cycle()

            self.assertTrue(attempted["value"])
            self.assertEqual(result.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn(
                "dependency registry advanced after decision snapshot capture",
                result.detail,
            )
            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            self.assertIsNone(loop._load_progress())
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_provider_gap_full_cut_rejects_concurrent_update_before_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            first_event = self._event(selection="selection-a", sequence=1)
            observer_calls = {"count": 0}

            def observer(updates):
                observer_calls["count"] += 1
                if observer_calls["count"] == 1:
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(first_event)
                    finally:
                        store.close()
                    updates.accept_persisted(first_event)
                    return
                raise ProviderUnavailableError("simulated provider gap")

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

            concurrent = self._event(
                selection="selection-a",
                sequence=2,
                odds="2.20",
                observed=self.START + timedelta(milliseconds=1500),
            )
            real_capture = loop.dependencies.coherent_decision_views
            injected = {"done": False}

            def full_capture_then_advance(*args, **kwargs):
                snapshots = real_capture(*args, **kwargs)
                if kwargs.get("incremental") is False and not injected["done"]:
                    injected["done"] = True
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(concurrent)
                    finally:
                        store.close()
                    loop.mirror_updates.accept_persisted(concurrent)
                return snapshots

            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                loop.dependencies,
                "coherent_decision_views",
                side_effect=full_capture_then_advance,
            ):
                second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIsNone(second.plan)
            self.assertIn(
                "market revision advanced during full decision snapshot capture",
                second.detail,
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

            # The update is already in canonical mirror truth on the next full cut.
            # Pending incremental invalidation must not starve provider-gap ZERO.
            clock.value = self.START + timedelta(seconds=3)
            third = loop.run_cycle()

            self.assertEqual(third.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertIsNotNone(third.plan)
            self.assertEqual(third.plan.action, PortfolioAction.ZERO)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                2,
            )

    def test_provider_gap_rejects_portfolio_change_during_full_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            book = PaperBook("1000")
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        ProviderUnavailableError("simulated provider gap"),
                        ProviderUnavailableError("simulated provider gap"),
                    ],
                ),
                factory=factory,
                clock=clock,
                book=book,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            real_capture = loop.dependencies.coherent_decision_views
            injected = {"done": False}

            def full_capture_then_change_portfolio(*args, **kwargs):
                snapshots = real_capture(*args, **kwargs)
                if kwargs.get("incremental") is False and not injected["done"]:
                    injected["done"] = True
                    book.open_ticket(
                        [
                            TicketLeg(
                                event_id="existing-event",
                                market_id="existing-market",
                                selection_id="existing-selection",
                                locked_odds=Decimal("2"),
                                sport="table_tennis",
                                exchange_side="back",
                            )
                        ],
                        Decimal("10"),
                        placed_at=(
                            clock.value + timedelta(microseconds=1)
                        ).isoformat(),
                    )
                return snapshots

            with patch.object(
                loop.dependencies,
                "coherent_decision_views",
                side_effect=full_capture_then_change_portfolio,
            ):
                first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIsNone(first.plan)
            self.assertIn("PaperBook/risk context advanced", first.detail)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(loop.progress_path.exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertIsNotNone(second.plan)
            self.assertEqual(second.plan.action, PortfolioAction.ZERO)
            self.assertEqual(
                second.plan.portfolio_sha256,
                loop.authority.risk_policy.risk_of_ruin_portfolio_sha256(book),
            )

    def test_provider_gap_does_not_publish_zero_from_future_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            late = self._event(
                selection="selection-a",
                sequence=1,
                observed=self.START + timedelta(seconds=2),
            )
            late = MarketEvent.from_dict(
                {
                    **late.to_dict(),
                    "source_ts": self.START.isoformat(),
                }
            )
            published = {"done": False}

            def failing_observer(updates):
                if not published["done"]:
                    published["done"] = True
                    store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        store.append(late)
                    finally:
                        store.close()
                    updates.accept_persisted(late)
                raise ProviderUnavailableError("simulated provider gap")

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=failing_observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("provider gap decision cut was not causally coherent", first.detail)
            self.assertEqual(factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertIsNotNone(second.plan)
            self.assertEqual(second.plan.action, PortfolioAction.ZERO)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
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

    def test_paper_execution_model_swap_invalidates_pending_decision_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            book = PaperBook("1000")
            ledger = PaperExecutionLedger(workspace / "paper-execution.jsonl")

            def model(model_id, seed):
                return PaperExecutionModelConfig(
                    model_id=model_id,
                    model_version="1",
                    evidence_grade=EvidenceGrade.SYNTHETIC,
                    evidence_source=model_id,
                    seed=seed,
                    max_quote_age_ms=5_000,
                    min_delay_ms=0,
                    max_delay_ms=0,
                    rejected_bps=0,
                    partial_bps=0,
                    unknown_bps=0,
                    partial_fill_bps=5000,
                    max_slippage_bps=0,
                )

            execution_a = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=model("live-model-a", "seed-a"),
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            execution_b = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=model("live-model-b", "seed-b"),
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),), ()],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
                book=book,
                paper_execution=execution_a,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            context_a = loop._decision_context_sha256()
            loop.paper_execution = execution_b
            context_b = loop._decision_context_sha256()
            self.assertNotEqual(context_a, context_b)
            loop.paper_execution = execution_a

            real_refresh = loop._refresh_intents_from_snapshots

            def refresh_then_swap_execution(snapshots):
                real_refresh(snapshots)
                loop.paper_execution = execution_b

            with patch.object(
                loop,
                "_refresh_intents_from_snapshots",
                side_effect=refresh_then_swap_execution,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "progress changed before durable ledger publication",
                ):
                    loop.run_cycle()

            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(loop._load_progress().phase, "pending")

            loop.paper_execution = execution_a
            clock.value = self.START + timedelta(seconds=2)
            recovered = loop.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_live_loop_rejects_noncanonical_paper_execution_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            book = PaperBook("1000")
            execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=PaperExecutionLedger(workspace / "alternate-paper-execution.jsonl"),
                config=PaperExecutionModelConfig(
                    model_id="alternate-ledger-test",
                    model_version="1",
                    evidence_grade=EvidenceGrade.SYNTHETIC,
                    evidence_source="alternate-ledger-test",
                    seed="alternate-ledger-test",
                    max_quote_age_ms=5_000,
                    min_delay_ms=0,
                    max_delay_ms=0,
                    rejected_bps=0,
                    partial_bps=0,
                    unknown_bps=0,
                    partial_fill_bps=5000,
                    max_slippage_bps=0,
                ),
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )

            with self.assertRaisesRegex(
                ValueError,
                "canonical live workspace PAPER execution ledger",
            ):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=1)),
                    book=book,
                    paper_execution=execution,
                )

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

    def test_stale_peer_cannot_overwrite_another_pending_decision_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256
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

            # Construct the peer before the first writer publishes PENDING so its
            # in-memory progress generation is deliberately stale (None).
            peer_observer = _DurableObserver(workspace, [()])
            peer = self._loop(
                workspace,
                observer=peer_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            self.assertIsNone(peer._progress)

            with self.assertRaisesRegex(
                RuntimeError,
                "simulated process loss after pending cursor",
            ):
                first.run_cycle()

            durable_before = first._load_progress()
            self.assertIsNotNone(durable_before)
            self.assertEqual(durable_before.phase, "pending")

            raced = peer.run_cycle()

            self.assertEqual(raced.status, LiveCycleStatus.BACKPRESSURE)
            self.assertIn("progress advanced during snapshot assembly", raced.detail)
            self.assertEqual(peer_observer.calls, 1)
            self.assertEqual(peer._load_progress(), durable_before)
            self.assertFalse((workspace / "decisions.jsonl").exists())

            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )
            recovered = resumed.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
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
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256

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
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256

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

    def test_pending_restart_rejects_factory_source_swap_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256

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
            resumed_factory = _EmptyIntentFactory()
            resumed_factory.source_sha256 = "f" * 64
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "factory source_sha256 provenance changed",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=resumed_factory,
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                )
            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_pending_recovery_rechecks_factory_provenance_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256
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
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            original_refresh = resumed._refresh_intents_from_replay

            def refresh_then_mutate(*args, **kwargs):
                original_refresh(*args, **kwargs)
                resumed_factory.source_sha256 = "f" * 64

            with patch.object(
                resumed,
                "_refresh_intents_from_replay",
                side_effect=refresh_then_mutate,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "factory source_sha256 provenance changed",
                ):
                    resumed.run_cycle()

            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(resumed._load_progress().phase, "pending")

    def test_pending_restart_rejects_changed_intent_provenance_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256

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

    def test_dependency_registry_mutation_is_blocked_while_decision_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(
                RuntimeError,
                "simulated process loss after pending cursor",
            ):
                loop.run_cycle()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "economic decision is unfinished",
            ):
                loop.unregister_input("input-a")
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "economic decision is unfinished",
            ):
                loop.register_input("input-b", selection_ids="selection-b")

            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            self.assertEqual(
                tuple(loop._input_specs),
                ("input-a",),
            )
            self.assertTrue(loop.progress_path.exists())
            self.assertEqual(loop._load_progress().phase, "pending")

    def test_pending_restart_rejects_changed_paper_book_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            fail_after_pending.source_sha256 = self.INTENT_SOURCE_SHA256
            fail_after_pending.environment_sha256 = self.INTENT_ENVIRONMENT_SHA256
            fail_after_pending.config_sha256 = self.INTENT_CONFIG_SHA256

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

    def test_legacy_committed_record_cannot_authorize_current_progress(self) -> None:
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
            progress = loop._progress
            self.assertIsNotNone(progress)
            self.assertEqual(progress.phase, "committed")

            class LegacyRecord:
                payload = {"schema_version": 1}

            with patch(
                "autosport.live_decision_loop.verify_economic_goal_binding",
                return_value=None,
            ), patch.object(
                loop,
                "_verified_ledger_record_at_offset",
                return_value=LegacyRecord(),
            ):
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "lacks canonical intent provenance",
                ):
                    loop._verify_committed_progress_ledger_binding(progress)

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
