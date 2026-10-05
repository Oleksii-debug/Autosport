from __future__ import annotations

import copy
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
    DecisionRecord,
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
from autosport.market_mirror_runtime import FocusedMirrorDependencyChurnError
from autosport.market_state_identity import PROPHETX_REST_MARKET_STATE_CONTRACT
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
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
from autosport.providers import ProviderBatch, ProviderUnavailableError
from autosport.scientific_registry import ScientificRegistry, StrategyVersion
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.storage import SQLiteMarketStore
from autosport.workspace_lock import WorkspaceEconomicLockBusyError


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


class _EmptyProvider:
    source_id = "provider-a"

    def __init__(self) -> None:
        self.calls = 0
        self.on_read = None
        self.error: Exception | None = None

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        del max_items
        self.calls += 1
        hook = self.on_read
        if hook is not None:
            self.on_read = None
            hook()
        error = self.error
        if error is not None:
            self.error = None
            raise error
        return ProviderBatch(source_id=self.source_id, quotes=())


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
    ) -> None:
        self.strategy_version_id = strategy_version_id
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
    def _prophetx_refresh_event(
        *,
        sequence: int,
        observed: datetime,
        odds: str = "2.00",
    ) -> MarketEvent:
        timestamp = observed.isoformat()
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-a",
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id="prophetx:sandbox",
            sequence=sequence,
            status="open",
            ingest_ts=timestamp,
            metadata={
                "provider": "prophetx",
                "environment": "sandbox",
                "transport_surface": "v3_affiliate_get_markets",
                "request_fingerprint_sha256": "a" * 64,
                "product_acquisition_sequence": sequence,
                "response_sha256": f"{sequence:x}".rjust(64, "0"),
                "snapshot_fingerprint_sha256": (
                    f"{sequence + 100:x}".rjust(64, "0")
                ),
                "sequence_authority_id": "prophetx-rest-test-authority",
                "sequence_source_id": (
                    "prophetx:sandbox:rest:v3-affiliate-get-markets"
                ),
                "semantic_state_contract": PROPHETX_REST_MARKET_STATE_CONTRACT,
            },
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
        max_quote_age: timedelta | None = timedelta(seconds=5),
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
            max_quote_age=max_quote_age,
            clock=clock,
            post_append_hook=post_append_hook,
            paper_execution=paper_execution,
            catalog_lifecycle=catalog_lifecycle,
            catalog_fetch_page=catalog_fetch_page,
            catalog_source_id=catalog_source_id,
            catalog_required_history=catalog_required_history,
        )

    def test_long_lived_default_observer_reconciles_cross_process_market_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            provider = _EmptyProvider()
            strategy = self._strategy_version()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(workspace, strategy),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )

            live_store = loop._default_market_store
            self.assertIsNotNone(live_store)
            with (
                patch.object(
                    live_store,
                    "current_by_source_with_append_generation",
                    wraps=live_store.current_by_source_with_append_generation,
                ) as proven_current,
                patch.object(
                    live_store,
                    "require_current_append_authority_with_boundary",
                    wraps=live_store.require_current_append_authority_with_boundary,
                ) as proven_frontier,
            ):
                clock.value = self.START + timedelta(seconds=2)
                idle = loop.run_cycle()
                self.assertEqual(idle.status, LiveCycleStatus.NO_CHANGE)
                self.assertEqual(proven_current.call_count, 0)
                self.assertEqual(proven_frontier.call_count, 0)

                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=3),
                            ),
                        )
                    )
                finally:
                    peer_store.close()

                clock.value = self.START + timedelta(seconds=3)
                second = loop.run_cycle()
                self.assertEqual(proven_current.call_count, 1)
                self.assertEqual(proven_frontier.call_count, 1)

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(provider.calls, 3)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            loop.close()

    def test_default_observer_reconciles_peer_append_committed_during_provider_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            provider = _EmptyProvider()
            strategy = self._strategy_version()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(workspace, strategy),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            def publish_from_peer() -> None:
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    )
                finally:
                    peer_store.close()

            provider.on_read = publish_from_peer
            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            loop.close()

    def test_provider_gap_reconciles_peer_append_committed_during_failed_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            def publish_from_peer() -> None:
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    )
                finally:
                    peer_store.close()

            provider.on_read = publish_from_peer
            provider.error = ProviderUnavailableError("simulated provider outage")
            clock.value = self.START + timedelta(seconds=2)
            gap = loop.run_cycle()

            self.assertEqual(gap.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertEqual(provider.calls, 2)
            visible = loop.dependencies.decision_view(
                "input-a",
                as_of=clock.value,
                max_age=timedelta(seconds=5),
            )
            self.assertEqual(
                tuple((event.selection_id, event.sequence) for event in visible.events),
                (("selection-a", 2),),
            )
            latest = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[-1]
            self.assertEqual(latest.payload["gate"], "provider_gap")
            self.assertEqual(
                latest.payload["market_state_sha256"],
                loop._market_state_sha256(),
            )
            loop.close()

    def test_provider_gap_pending_restart_uses_frozen_market_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            strategy = self._strategy_version()
            registry = self._scientific_registry(workspace, strategy)
            provider = _EmptyProvider()
            provider.error = ProviderUnavailableError("simulated provider outage")
            first = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=registry,
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with patch.object(
                first,
                "_persist_plan",
                side_effect=RuntimeError("simulated loss after provider-gap pending"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated loss after provider-gap pending",
                ):
                    first.run_cycle()

            pending = json.loads(first.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(pending["gate"], "provider_gap")
            self.assertEqual(pending["market_append_generation"], 1)
            first.close()

            peer_store = SQLiteMarketStore(workspace / "market.db")
            try:
                peer_store.append(
                    self._event(
                        sequence=2,
                        odds="2.10",
                        observed=self.START + timedelta(milliseconds=500),
                    )
                )
            finally:
                peer_store.close()

            resumed_provider = _EmptyProvider()
            resumed_factory = _EmptyIntentFactory()
            resumed = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=resumed_factory,
                scientific_registry=registry,
                provider=resumed_provider,
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_provider.calls, 0)
            self.assertEqual(resumed_factory.calls, [])
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(record.payload["gate"], "provider_gap")
            self.assertNotIn("market_append_generation", record.payload)
            resumed.close()

    def test_decision_frontier_reconciles_peer_append_after_observer_returns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            refresh = loop._refresh_cycle_authorities
            refresh_calls = 0

            def refresh_and_publish_after_observation() -> None:
                nonlocal refresh_calls
                refresh()
                refresh_calls += 1
                if refresh_calls != 2:
                    return
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    )
                finally:
                    peer_store.close()

            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                loop,
                "_refresh_cycle_authorities",
                side_effect=refresh_and_publish_after_observation,
            ):
                second = loop.run_cycle()

            self.assertEqual(refresh_calls, 2)
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            loop.close()

    def test_decision_frontier_retries_peer_append_after_candidate_clock_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            sample_clock = loop._sample_clock
            sample_calls = 0

            def sample_and_publish_after_candidate() -> datetime:
                nonlocal sample_calls
                sampled = sample_clock()
                sample_calls += 1
                if sample_calls != 2:
                    return sampled
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    )
                finally:
                    peer_store.close()
                return sampled

            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                loop,
                "_sample_clock",
                side_effect=sample_and_publish_after_candidate,
            ):
                second = loop.run_cycle()

            self.assertGreaterEqual(sample_calls, 3)
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            loop.close()

    def test_provider_gap_frontier_reconciles_peer_append_after_failed_observer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            refresh = loop._refresh_cycle_authorities
            refresh_calls = 0

            def refresh_and_publish_after_failed_observation() -> None:
                nonlocal refresh_calls
                refresh()
                refresh_calls += 1
                if refresh_calls != 2:
                    return
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    )
                finally:
                    peer_store.close()

            provider.error = ProviderUnavailableError("simulated provider outage")
            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                loop,
                "_refresh_cycle_authorities",
                side_effect=refresh_and_publish_after_failed_observation,
            ):
                gap = loop.run_cycle()

            self.assertEqual(refresh_calls, 2)
            self.assertEqual(gap.status, LiveCycleStatus.PROVIDER_GAP)
            visible = loop.dependencies.decision_view(
                "input-a",
                as_of=clock.value,
                max_age=timedelta(seconds=5),
            )
            self.assertEqual(
                tuple((event.selection_id, event.sequence) for event in visible.events),
                (("selection-a", 2),),
            )
            latest = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[-1]
            self.assertEqual(latest.payload["gate"], "provider_gap")
            self.assertEqual(
                latest.payload["market_state_sha256"],
                loop._market_state_sha256(),
            )
            loop.close()

    def test_duplicate_visible_state_keeps_market_frontier_out_of_ledger_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)

            first_record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertNotIn("market_append_generation", first_record.payload)

            peer_store = SQLiteMarketStore(workspace / "market.db")
            try:
                peer_store.append(
                    MarketEvent(
                        event_id="event-1",
                        market_id="market-1",
                        selection_id="selection-a",
                        decimal_odds=Decimal("2.10"),
                        observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                        source_id="provider-a",
                        sequence=2,
                        status="open",
                        source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                        ingest_ts=(self.START + timedelta(seconds=2)).isoformat(),
                    )
                )
            finally:
                peer_store.close()

            duplicate = loop.run_cycle()
            self.assertEqual(duplicate.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(duplicate.decision_id, first.decision_id)
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 1)
            self.assertNotIn("market_append_generation", records[0].payload)

            committed = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(committed["phase"], "committed")
            self.assertEqual(committed["market_append_generation"], 2)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )
            loop.close()

    def test_duplicate_append_pending_restart_tolerates_recovery_frontier_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            provider = _EmptyProvider()
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
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)

            peer_store = SQLiteMarketStore(workspace / "market.db")
            try:
                peer_store.append(
                    MarketEvent(
                        event_id="event-1",
                        market_id="market-1",
                        selection_id="selection-a",
                        decimal_odds=Decimal("2.10"),
                        observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                        source_id="provider-a",
                        sequence=2,
                        status="open",
                        source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                        ingest_ts=(self.START + timedelta(seconds=2)).isoformat(),
                    )
                )
            finally:
                peer_store.close()

            with patch.object(
                loop,
                "_verified_ledger_record_at_offset",
                side_effect=RuntimeError(
                    "simulated loss after duplicate APPEND_PENDING publication"
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "duplicate APPEND_PENDING publication",
                ):
                    loop.run_cycle()

            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "append_pending")
            self.assertEqual(pending["market_append_generation"], 2)
            loop.close()

            resumed_provider = _EmptyProvider()
            resumed = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=registry,
                provider=resumed_provider,
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            recovered = resumed.run_cycle()

            self.assertEqual(
                recovered.status,
                LiveCycleStatus.DUPLICATE_DECISION,
            )
            self.assertEqual(recovered.decision_id, first.decision_id)
            self.assertEqual(resumed_provider.calls, 0)
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 1)
            self.assertNotIn("market_append_generation", records[0].payload)
            committed = json.loads(
                resumed.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["phase"], "committed")
            self.assertEqual(committed["market_append_generation"], 2)
            resumed.close()

    def test_decision_frontier_leaves_post_cutoff_peer_commit_for_next_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            live_store = loop._default_market_store
            self.assertIsNotNone(live_store)
            external_change_token = live_store.external_change_token
            token_calls = 0

            def publish_after_final_token_read() -> int:
                nonlocal token_calls
                token = external_change_token()
                token_calls += 1
                if token_calls != 2:
                    return token
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    MarketEventBus(peer_store).publish_many(
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    )
                finally:
                    peer_store.close()
                return token

            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                live_store,
                "external_change_token",
                side_effect=publish_after_final_token_read,
            ):
                cutoff = loop._sample_decision_market_frontier()

            self.assertEqual(token_calls, 2)
            visible_at_cutoff = loop.dependencies.decision_view(
                "input-a",
                as_of=cutoff,
                max_age=timedelta(seconds=5),
            )
            self.assertEqual(
                tuple(
                    (event.selection_id, event.sequence)
                    for event in visible_at_cutoff.events
                ),
                (("selection-a", 1),),
            )

            next_cycle = loop.run_cycle()
            self.assertEqual(next_cycle.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            loop.close()

    def test_post_cutoff_abandoned_append_is_not_retroactively_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=_EmptyProvider(),
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            # Force one material recomputation whose exact market frontier remains
            # generation 1. The peer append lands only after that frontier is frozen.
            loop._pending_affected["input-a"] = None
            clock.value = self.START + timedelta(seconds=2)
            drain = loop.mirror_updates.drain
            original_recover = MonotonicWorkspaceAuthority.recover
            injected = False

            def fail_newer_append_commit_once(authority, **kwargs):
                nonlocal injected
                tx_id = kwargs.get("tx_id")
                if (
                    not injected
                    and isinstance(tx_id, str)
                    and tx_id.startswith("append-")
                ):
                    injected = True
                    raise RuntimeError("simulated post-cutoff append PREPARE")
                return original_recover(authority, **kwargs)

            def append_after_frontier(*, max_items: int):
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    with patch.object(
                        MonotonicWorkspaceAuthority,
                        "recover",
                        new=fail_newer_append_commit_once,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "simulated post-cutoff append PREPARE",
                        ):
                            peer_store.append(
                                self._event(
                                    sequence=2,
                                    odds="2.10",
                                    observed=self.START + timedelta(seconds=2),
                                )
                            )
                finally:
                    peer_store.close()
                return drain(max_items=max_items)

            with patch.object(
                loop.mirror_updates,
                "drain",
                side_effect=append_after_frontier,
            ):
                second = loop.run_cycle()

            self.assertTrue(injected)
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )
            committed = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(committed["market_append_generation"], 1)

            live_store = loop._default_market_store
            self.assertIsNotNone(live_store)
            append_history = live_store._market_append_authority().read_history()
            self.assertEqual(append_history[-1].phase.value, "COMMIT")
            self.assertEqual(live_store.append_generation_hint(), 2)

            # Once the next cycle reconciles peer durability, generation 2 becomes
            # decision-visible instead of being retroactively inserted into cycle 2.
            clock.value = self.START + timedelta(seconds=3)
            third = loop.run_cycle()
            self.assertEqual(third.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            loop.close()

    def test_decision_frontier_rejects_peer_projection_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            provider = _EmptyProvider()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=provider,
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger_path = workspace / "decisions.jsonl"
            ledger_bytes = ledger_path.read_bytes()
            progress_bytes = loop.progress_path.read_bytes()
            refresh = loop._refresh_cycle_authorities
            refresh_calls = 0

            def refresh_and_tamper_projection() -> None:
                nonlocal refresh_calls
                refresh()
                refresh_calls += 1
                if refresh_calls != 2:
                    return
                peer_store = SQLiteMarketStore(workspace / "market.db")
                try:
                    peer_store.connection.execute("DELETE FROM current_quotes")
                    peer_store.connection.commit()
                finally:
                    peer_store.close()

            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                loop,
                "_refresh_cycle_authorities",
                side_effect=refresh_and_tamper_projection,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "current quote projection diverges from canonical market history",
                ):
                    loop.run_cycle()

            self.assertEqual(refresh_calls, 2)
            self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
            self.assertEqual(loop.progress_path.read_bytes(), progress_bytes)
            loop.close()

    def test_decision_frontier_fails_closed_under_continuous_peer_churn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many((self._event(sequence=1),))
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=_EmptyIntentFactory(),
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=_EmptyProvider(),
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            live_store = loop._default_market_store
            self.assertIsNotNone(live_store)
            token_values = iter(range(100, 116))
            with patch.object(
                live_store,
                "external_change_token",
                side_effect=lambda: next(token_values),
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "market truth changed continuously across decision cutoff",
                ):
                    loop._sample_decision_market_frontier()

            loop.close()

    def test_decision_frontier_freezes_history_before_post_cutoff_peer_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            predecessor = self._event(
                sequence=1,
                observed=self.START + timedelta(seconds=1),
            )
            future_successor = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.10"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many(
                    (predecessor, future_successor)
                )
            finally:
                seed_store.close()

            clock = _ManualClock(self.START + timedelta(seconds=2))
            factory = _EmptyIntentFactory()
            loop = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=factory,
                scientific_registry=self._scientific_registry(
                    workspace,
                    self._strategy_version(),
                ),
                provider=_EmptyProvider(),
                max_quote_age=timedelta(seconds=5),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            drain = loop.mirror_updates.drain
            peer_published = False

            def publish_after_frontier(*, max_items: int):
                nonlocal peer_published
                if not peer_published:
                    peer_store = SQLiteMarketStore(workspace / "market.db")
                    try:
                        MarketEventBus(peer_store).publish_many(
                            (
                                self._event(
                                    sequence=3,
                                    odds="2.20",
                                    observed=self.START + timedelta(seconds=2),
                                ),
                            )
                        )
                    finally:
                        peer_store.close()
                    peer_published = True
                return drain(max_items=max_items)

            with patch.object(
                loop.mirror_updates,
                "drain",
                side_effect=publish_after_frontier,
            ):
                first = loop.run_cycle()

            self.assertTrue(peer_published)
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                self.START + timedelta(seconds=4),
            )

            second = loop.run_cycle()
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls[-1],
                ("input-a", (("selection-a", 3, "open"),)),
            )
            loop.close()

    def test_stale_instance_cannot_overwrite_newer_pending_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            winner = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_progress = stale._progress
            self.assertEqual(stale_progress, winner._progress)

            winner._write_pending(
                decision_ts=(self.START + timedelta(seconds=2)).isoformat(),
                market_state_sha256="a" * 64,
                affected_input_ids=("input-a",),
                gate="normal",
            )
            progress_bytes = winner.progress_path.read_bytes()
            pre_action_bytes = winner.pre_action_book_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "changed concurrently before pending publication",
            ):
                stale._write_pending(
                    decision_ts=(self.START + timedelta(seconds=3)).isoformat(),
                    market_state_sha256="b" * 64,
                    affected_input_ids=("input-a",),
                    gate="normal",
                )

            self.assertEqual(winner.progress_path.read_bytes(), progress_bytes)
            self.assertEqual(winner.pre_action_book_path.read_bytes(), pre_action_bytes)
            self.assertEqual(stale._progress, stale_progress)
            winner.close()
            stale.close()

    def test_stale_instance_cannot_overwrite_newer_committed_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            active = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        )
                    ],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_progress = stale._progress
            self.assertEqual(stale_progress, active._progress)

            self.assertEqual(active.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertNotEqual(active._progress, stale_progress)
            progress_bytes = active.progress_path.read_bytes()
            pre_action_bytes = active.pre_action_book_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "changed concurrently before pending publication",
            ):
                stale._write_pending(
                    decision_ts=(self.START + timedelta(seconds=3)).isoformat(),
                    market_state_sha256="c" * 64,
                    affected_input_ids=("input-a",),
                    gate="normal",
                )

            self.assertEqual(active.progress_path.read_bytes(), progress_bytes)
            self.assertEqual(active.pre_action_book_path.read_bytes(), pre_action_bytes)
            self.assertEqual(stale._progress, stale_progress)
            active.close()
            stale.close()

    def test_stale_instance_cannot_publish_after_dependency_registry_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            active = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_progress = stale._progress
            active.register_input("input-b", selection_ids="selection-b")
            self.assertEqual(
                active.dependencies.input_ids,
                ("input-a", "input-b"),
            )
            self.assertEqual(stale.dependencies.input_ids, ("input-a",))
            progress_bytes = active.progress_path.read_bytes()
            pre_action_bytes = active.pre_action_book_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "dependency registry changed concurrently before pending publication",
            ):
                stale._write_pending(
                    decision_ts=(self.START + timedelta(seconds=2)).isoformat(),
                    market_state_sha256="d" * 64,
                    affected_input_ids=("input-a",),
                    gate="normal",
                )

            self.assertEqual(active.progress_path.read_bytes(), progress_bytes)
            self.assertEqual(active.pre_action_book_path.read_bytes(), pre_action_bytes)
            self.assertEqual(stale._progress, stale_progress)
            active.close()
            stale.close()

    def test_durable_stop_preempts_stale_cycle_before_pending_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_progress = stale._progress
            controller.stop()

            self.assertEqual(stale.run_cycle().status, LiveCycleStatus.STOPPED)

            self.assertEqual(stale_observer.calls, 0)
            self.assertEqual(stale._progress, stale_progress)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )

            stopped_observer = _DurableObserver(workspace, [()])
            stopped = self._loop(
                workspace,
                observer=stopped_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )
            self.assertEqual(stopped.run_cycle().status, LiveCycleStatus.STOPPED)
            self.assertEqual(stopped_observer.calls, 0)
            controller.close()
            stale.close()
            stopped.close()

    def test_durable_pause_preempts_stale_cycle_before_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            stale_observer = _DurableObserver(workspace, [()])
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            controller.pause()
            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.PAUSED)
            self.assertTrue(stale.paused)
            self.assertEqual(stale_observer.calls, 0)
            self.assertFalse(stale.progress_path.exists())
            controller.close()
            stale.close()

    def test_peer_resume_is_observed_by_stale_paused_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            controller.pause()

            stale_observer = _DurableObserver(workspace, [()])
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            self.assertTrue(stale.paused)

            controller.resume()
            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.NO_CHANGE)
            self.assertFalse(stale.paused)
            self.assertEqual(stale_observer.calls, 1)
            controller.close()
            stale.close()

    def test_stale_resume_cannot_clear_peer_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            stale_observer = _DurableObserver(workspace, [()])
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            controller.stop()
            with self.assertRaisesRegex(RuntimeError, "durable STOP cannot be cleared"):
                stale.resume()

            self.assertEqual(stale.run_cycle().status, LiveCycleStatus.STOPPED)
            self.assertTrue(stale.stopped)
            self.assertEqual(stale_observer.calls, 0)
            controller.close()
            stale.close()

    def test_stale_pause_cannot_replace_peer_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            stale_observer = _DurableObserver(workspace, [()])
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            controller.stop()
            with self.assertRaisesRegex(RuntimeError, "durable STOP cannot be cleared"):
                stale.pause()

            self.assertEqual(stale.run_cycle().status, LiveCycleStatus.STOPPED)
            self.assertTrue(stale.stopped)
            self.assertEqual(stale_observer.calls, 0)
            controller.close()
            stale.close()

    def test_stale_cycle_rejects_newer_progress_before_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            active = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            active._write_pending(
                decision_ts=(self.START + timedelta(seconds=2)).isoformat(),
                market_state_sha256="d" * 64,
                affected_input_ids=("input-a",),
                gate="normal",
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "progress changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(stale_observer.calls, 0)
            active.close()
            stale.close()

    def test_stale_cycle_rejects_registry_change_before_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            active = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            active.register_input("input-b", selection_ids="selection-b")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "dependency registry changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(stale_observer.calls, 0)
            active.close()
            stale.close()

    def test_stop_race_during_observation_blocks_pending_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            durable_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )

            def stop_during_observation(updates):
                result = durable_observer(updates)
                controller.stop()
                return result

            stale = self._loop(
                workspace,
                observer=stop_during_observation,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            progress_before = stale.progress_path.read_bytes()

            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.STOPPED)
            self.assertTrue(stale.stopped)
            self.assertEqual(durable_observer.calls, 1)
            self.assertEqual(stale.progress_path.read_bytes(), progress_before)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )
            controller.close()
            stale.close()

    def test_pause_race_during_observation_blocks_pending_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            durable_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )

            def pause_during_observation(updates):
                result = durable_observer(updates)
                controller.pause()
                return result

            stale = self._loop(
                workspace,
                observer=pause_during_observation,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            progress_before = stale.progress_path.read_bytes()

            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.PAUSED)
            self.assertTrue(stale.paused)
            self.assertEqual(durable_observer.calls, 1)
            self.assertEqual(stale.progress_path.read_bytes(), progress_before)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )
            controller.close()
            stale.close()

    def test_registry_change_during_observation_fences_decision_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            durable_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )

            def mutate_registry_during_observation(updates):
                result = durable_observer(updates)
                controller.register_input(
                    "input-b",
                    selection_ids="selection-b",
                )
                return result

            stale = self._loop(
                workspace,
                observer=mutate_registry_during_observation,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            progress_before = stale.progress_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "dependency registry changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(durable_observer.calls, 1)
            self.assertEqual(stale.progress_path.read_bytes(), progress_before)
            self.assertEqual(stale.dependencies.input_ids, ("input-a",))
            self.assertEqual(
                controller.dependencies.input_ids,
                ("input-a", "input-b"),
            )
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )
            controller.close()
            stale.close()

    def test_progress_change_during_observation_fences_decision_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            durable_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )

            def publish_pending_during_observation(updates):
                result = durable_observer(updates)
                controller._write_pending(
                    decision_ts=(self.START + timedelta(seconds=2)).isoformat(),
                    market_state_sha256="b" * 64,
                    affected_input_ids=("input-a",),
                    gate="normal",
                )
                return result

            stale = self._loop(
                workspace,
                observer=publish_pending_during_observation,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "progress changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(durable_observer.calls, 1)
            self.assertEqual(
                json.loads(stale.progress_path.read_text(encoding="utf-8"))["phase"],
                "pending",
            )
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )
            controller.close()
            stale.close()

    def test_stop_race_during_failed_observation_suppresses_provider_gap_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            calls = {"count": 0}

            def fail_after_stop(_updates):
                calls["count"] += 1
                controller.stop()
                raise ProviderUnavailableError("provider unavailable")

            stale = self._loop(
                workspace,
                observer=fail_after_stop,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.STOPPED)
            self.assertEqual(calls["count"], 1)
            self.assertTrue(stale.stopped)
            self.assertFalse(stale.progress_path.exists())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            controller.close()
            stale.close()

    def test_pause_race_during_failed_observation_suppresses_provider_gap_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            calls = {"count": 0}

            def fail_after_pause(_updates):
                calls["count"] += 1
                controller.pause()
                raise ProviderUnavailableError("provider unavailable")

            stale = self._loop(
                workspace,
                observer=fail_after_pause,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.PAUSED)
            self.assertEqual(calls["count"], 1)
            self.assertTrue(stale.paused)
            self.assertFalse(stale.progress_path.exists())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            controller.close()
            stale.close()

    def test_registry_change_during_failed_observation_blocks_provider_gap_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            calls = {"count": 0}

            def fail_after_registry_change(_updates):
                calls["count"] += 1
                controller.register_input(
                    "peer-input",
                    selection_ids="selection-peer",
                )
                raise ProviderUnavailableError("provider unavailable")

            stale = self._loop(
                workspace,
                observer=fail_after_registry_change,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "dependency registry changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(calls["count"], 1)
            self.assertFalse(stale.progress_path.exists())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertEqual(controller.dependencies.input_ids, ("peer-input",))
            controller.close()
            stale.close()

    def test_progress_change_during_failed_observation_blocks_provider_gap_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            calls = {"count": 0}

            def fail_after_pending(_updates):
                calls["count"] += 1
                controller._write_pending(
                    decision_ts=self.START.isoformat(),
                    market_state_sha256="c" * 64,
                    affected_input_ids=(),
                    gate="normal",
                )
                raise ProviderUnavailableError("provider unavailable")

            stale = self._loop(
                workspace,
                observer=fail_after_pending,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "progress changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(calls["count"], 1)
            self.assertTrue(stale.progress_path.exists())
            self.assertEqual(
                json.loads(stale.progress_path.read_text(encoding="utf-8"))["phase"],
                "pending",
            )
            self.assertFalse((workspace / "decisions.jsonl").exists())
            controller.close()
            stale.close()

    def test_stop_during_catalog_refresh_blocks_market_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            market_observer = _DurableObserver(workspace, [()])
            lifecycle = ContinuousEventLifecycle(workspace / "catalog_lifecycle.json")
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(),
            )

            def fetch_page(_checkpoint):
                controller.stop()
                return page

            stale = self._loop(
                workspace,
                observer=market_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
                catalog_lifecycle=lifecycle,
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )

            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.STOPPED)
            self.assertEqual(market_observer.calls, 0)
            self.assertFalse(stale.progress_path.exists())
            self.assertTrue(stale.stopped)
            controller.close()
            stale.close()

    def test_pause_during_catalog_refresh_blocks_market_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            market_observer = _DurableObserver(workspace, [()])
            lifecycle = ContinuousEventLifecycle(workspace / "catalog_lifecycle.json")
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(),
            )

            def fetch_page(_checkpoint):
                controller.pause()
                return page

            stale = self._loop(
                workspace,
                observer=market_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
                catalog_lifecycle=lifecycle,
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )

            result = stale.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.PAUSED)
            self.assertEqual(market_observer.calls, 0)
            self.assertFalse(stale.progress_path.exists())
            self.assertTrue(stale.paused)
            controller.close()
            stale.close()

    def test_registry_change_during_catalog_refresh_fences_market_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            market_observer = _DurableObserver(workspace, [()])
            lifecycle = ContinuousEventLifecycle(workspace / "catalog_lifecycle.json")
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(),
            )

            def fetch_page(_checkpoint):
                controller.register_input(
                    "peer-input",
                    selection_ids="selection-peer",
                )
                return page

            stale = self._loop(
                workspace,
                observer=market_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
                catalog_lifecycle=lifecycle,
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "dependency registry changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(market_observer.calls, 0)
            self.assertFalse(stale.progress_path.exists())
            self.assertEqual(controller.dependencies.input_ids, ("peer-input",))
            self.assertEqual(stale.dependencies.input_ids, ())
            controller.close()
            stale.close()

    def test_progress_change_during_catalog_refresh_fences_market_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            controller = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            market_observer = _DurableObserver(workspace, [()])
            lifecycle = ContinuousEventLifecycle(workspace / "catalog_lifecycle.json")
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(),
            )

            def fetch_page(_checkpoint):
                controller._write_pending(
                    decision_ts=self.START.isoformat(),
                    market_state_sha256="a" * 64,
                    affected_input_ids=(),
                    gate="normal",
                )
                return page

            stale = self._loop(
                workspace,
                observer=market_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
                catalog_lifecycle=lifecycle,
                catalog_fetch_page=fetch_page,
                catalog_source_id="provider-a",
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "progress changed concurrently before cycle",
            ):
                stale.run_cycle()

            self.assertEqual(market_observer.calls, 0)
            self.assertTrue(stale.progress_path.exists())
            self.assertEqual(
                json.loads(stale.progress_path.read_text(encoding="utf-8"))["phase"],
                "pending",
            )
            controller.close()
            stale.close()

    def test_unfinished_pending_recovers_before_durable_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(_input_id, _snapshot):
                raise RuntimeError("simulated process loss after pending publication")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "loss after pending publication"):
                first.run_cycle()
            self.assertEqual(
                json.loads(first.progress_path.read_text(encoding="utf-8"))["phase"],
                "pending",
            )
            first.pause()
            first.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                json.loads(resumed.progress_path.read_text(encoding="utf-8"))["phase"],
                "committed",
            )
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.PAUSED)
            self.assertEqual(resumed_observer.calls, 0)
            resumed.close()

    def test_unfinished_append_pending_recovers_before_durable_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_append() -> None:
                raise RuntimeError("simulated process loss after ledger append")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                post_append_hook=fail_after_append,
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "process loss after ledger append"):
                first.run_cycle()

            progress_path = (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            )
            self.assertEqual(
                json.loads(progress_path.read_text(encoding="utf-8"))["phase"],
                "append_pending",
            )
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )
            first.stop()
            first.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                json.loads(progress_path.read_text(encoding="utf-8"))["phase"],
                "committed",
            )
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.STOPPED)
            self.assertEqual(resumed_observer.calls, 0)
            resumed.close()

    def test_dependency_registry_mutation_rejected_while_pending_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            loop._write_pending(
                decision_ts=(self.START + timedelta(seconds=1)).isoformat(),
                market_state_sha256="a" * 64,
                affected_input_ids=("input-a",),
                gate="normal",
            )
            inputs_before = loop.inputs_path.read_bytes()
            progress_before = loop.progress_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "cannot mutate live dependency registry while a decision is unfinished",
            ):
                loop.register_input("input-b", selection_ids="selection-b")

            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            self.assertEqual(loop.inputs_path.read_bytes(), inputs_before)
            self.assertEqual(loop.progress_path.read_bytes(), progress_before)
            loop.close()

    def test_stale_dependency_mutation_cannot_publish_after_newer_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bootstrap = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            bootstrap.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(bootstrap.run_cycle().status, LiveCycleStatus.DECIDED)
            bootstrap.close()

            active = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            stale = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            active._write_pending(
                decision_ts=(self.START + timedelta(seconds=2)).isoformat(),
                market_state_sha256="b" * 64,
                affected_input_ids=("input-a",),
                gate="normal",
            )
            inputs_before = active.inputs_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "progress changed concurrently before dependency publication",
            ):
                stale.register_input("input-b", selection_ids="selection-b")

            self.assertEqual(stale.dependencies.input_ids, ("input-a",))
            self.assertEqual(active.inputs_path.read_bytes(), inputs_before)
            active.close()
            stale.close()

    def test_failed_unregister_restores_exact_dependency_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            loop.register_input("input-b", selection_ids="selection-b")
            loop.register_input("input-c", selection_ids="selection-c")
            expected_ids = ("input-a", "input-b", "input-c")
            self.assertEqual(loop.dependencies.input_ids, expected_ids)

            loop._write_pending(
                decision_ts=(self.START + timedelta(seconds=1)).isoformat(),
                market_state_sha256="c" * 64,
                affected_input_ids=expected_ids,
                gate="normal",
            )
            inputs_before = loop.inputs_path.read_bytes()

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "cannot mutate live dependency registry while a decision is unfinished",
            ):
                loop.unregister_input("input-b")

            self.assertEqual(loop.dependencies.input_ids, expected_ids)
            self.assertEqual(tuple(loop._input_specs), expected_ids)
            self.assertEqual(loop.inputs_path.read_bytes(), inputs_before)
            loop.close()

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

    def test_semantic_refresh_still_recomputes_until_evidence_rebind_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_time = self.START + timedelta(seconds=1)
            refresh_time = self.START + timedelta(seconds=2)
            clock = _ManualClock(first_time)
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._prophetx_refresh_event(
                            sequence=1,
                            observed=first_time,
                        ),
                    ),
                    (
                        self._prophetx_refresh_event(
                            sequence=2,
                            observed=refresh_time,
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
            loop.register_input(
                "input-a",
                source_ids="prophetx:sandbox",
                event_ids="event-1",
                market_ids="market-1",
                selection_ids="selection-a",
            )

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls[-1][1], (("selection-a", 1, "open"),))

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(second.affected_input_ids, ("input-a",))
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 2, "open"),))],
            )
            current = loop.dependencies.decision_view(
                "input-a",
                as_of=clock.value,
                max_age=loop.max_quote_age,
            )
            self.assertEqual(current.events[0].sequence, 2)

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

    def test_distinct_equal_time_pending_decision_recovers_after_factory_crash(self) -> None:
        class CrashOnSecondFactory(_EmptyIntentFactory):
            def __call__(self, input_id, snapshot):
                if len(self.calls) == 1:
                    self.calls.append(
                        (
                            input_id,
                            tuple(
                                (event.selection_id, event.sequence, event.status)
                                for event in snapshot.events
                            ),
                        )
                    )
                    raise RuntimeError("simulated crash after pending publication")
                return super().__call__(input_id, snapshot)

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
                            odds="2.10",
                            observed=self.START,
                        ),
                    ),
                ],
            )
            factory = CrashOnSecondFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            with self.assertRaisesRegex(
                RuntimeError,
                "simulated crash after pending publication",
            ):
                loop.run_cycle()
            progress = json.loads(
                (
                    workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(progress["phase"], "pending")
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )

            recovered = loop.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                2,
            )
            loop.close()

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

    def test_same_process_recovery_clears_and_rebuilds_availability_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            future_ingest = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=self.START.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=self.START.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=3)).isoformat(),
            )
            crashes = {"remaining": 1}

            def crash_after_append() -> None:
                if crashes["remaining"]:
                    crashes["remaining"] -= 1
                    raise RuntimeError("simulated process loss after ledger append")

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(future_ingest,), ()]),
                factory=factory,
                clock=clock,
                post_append_hook=crash_after_append,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                loop.run_cycle()
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                self.START + timedelta(seconds=3),
            )
            self.assertTrue(loop._availability_heap)

            clock.value = self.START + timedelta(seconds=2)
            recovered = loop.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(loop._availability_deadlines, {})
            self.assertEqual(loop._availability_generations, {})
            self.assertEqual(loop._availability_heap, [])

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            rebuilt = loop.run_cycle()
            self.assertEqual(rebuilt.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            loop.close()

    def test_append_pending_blocks_dependency_registry_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def crash_after_append() -> None:
                raise RuntimeError("simulated process loss after ledger append")

            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                post_append_hook=crash_after_append,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                loop.run_cycle()

            self.assertIsNotNone(loop._progress)
            self.assertEqual(loop._progress.phase, "append_pending")
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "cannot mutate live dependency registry while a decision is unfinished",
            ):
                loop.unregister_input("input-a")
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "cannot mutate live dependency registry while a decision is unfinished",
            ):
                loop.register_input("input-b", selection_ids="selection-b")
            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            loop.close()

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

    def test_append_pending_restart_ignores_malformed_later_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def crash_after_append() -> None:
                raise RuntimeError("simulated process loss after ledger append")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                post_append_hook=crash_after_append,
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()
            pending = json.loads(first.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "append_pending")
            self.assertEqual(pending["market_append_generation"], 1)
            first.close()

            path = workspace / "market.db"
            corruptor = SQLiteMarketStore(path)
            try:
                corruptor.connection.execute(
                    "INSERT INTO market_event_commit_order "
                    "(dedupe_key, append_generation) VALUES (?, ?)",
                    ("post-append-pending-orphan", 3),
                )
                corruptor.connection.commit()
            finally:
                corruptor.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()

            self.assertEqual(
                recovered.status,
                LiveCycleStatus.DUPLICATE_DECISION,
            )
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )
            committed = json.loads(
                resumed.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["phase"], "committed")
            self.assertEqual(committed["market_append_generation"], 1)
            resumed.close()

            with self.assertRaises(ValueError):
                SQLiteMarketStore(path)

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

    def test_committed_positive_requires_execution_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)
            model = PaperExecutionModelConfig(
                model_id="live-paper-committed-test",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="live-paper-committed-test",
                seed="live-paper-committed",
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
            goal = EconomicGoalContract(
                goal_id="goal-live-committed-execution",
                revision=1,
                bankroll_id="bankroll-live-test",
                currency="EUR",
                max_risk_of_ruin=Decimal("1"),
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                book=book,
                authority=authority,
                paper_execution=execution,
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)
            first.close()

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "requires canonical #623 execution runtime",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=PaperBook.load(workspace / "paper_book.json"),
                    authority=authority,
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_committed_positive_requires_quote_in_proven_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)
            model = PaperExecutionModelConfig(
                model_id="live-paper-market-prefix-test",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="live-paper-market-prefix-test",
                seed="live-paper-market-prefix",
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
            goal = EconomicGoalContract(
                goal_id="goal-live-market-prefix",
                revision=1,
                bankroll_id="bankroll-live-test",
                currency="EUR",
                max_risk_of_ruin=Decimal("1"),
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                book=book,
                authority=authority,
                paper_execution=execution,
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)
            first.close()

            resumed_book = PaperBook.load(workspace / "paper_book.json")
            resumed_execution = PaperExecutionAdoptionRuntime(
                book=resumed_book,
                ledger=execution_ledger,
                config=model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            resumed_observer = _DurableObserver(workspace, [()])
            with (
                patch.object(
                    SQLiteMarketStore,
                    "_events_at_append_boundary_unlocked",
                    return_value=[],
                ),
                self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "market state conflicts with proven append prefix",
                ),
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_committed_positive_rejects_rehashed_execution_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)
            model = PaperExecutionModelConfig(
                model_id="live-paper-committed-tamper-test",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="live-paper-committed-tamper-test",
                seed="live-paper-committed-tamper",
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
            goal = EconomicGoalContract(
                goal_id="goal-live-committed-execution-tamper",
                revision=1,
                bankroll_id="bankroll-live-test",
                currency="EUR",
                max_risk_of_ruin=Decimal("1"),
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                book=book,
                authority=authority,
                paper_execution=execution,
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)
            first.close()

            ledger_path = workspace / "decisions.jsonl"
            envelope = json.loads(ledger_path.read_text(encoding="utf-8"))
            original_envelope = json.loads(json.dumps(envelope))
            record = envelope["record"]
            del record["payload"]["paper_execution"]
            canonical = JsonlDecisionLedger._canonical_record(record)
            envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            JsonlDecisionLedger(ledger_path).verify_integrity()

            resumed_book = PaperBook.load(workspace / "paper_book.json")
            resumed_execution = PaperExecutionAdoptionRuntime(
                book=resumed_book,
                ledger=execution_ledger,
                config=model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "execution-adoption evidence is invalid",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(resumed_observer.calls, 0)

            extra_payload_envelope = json.loads(
                json.dumps(original_envelope)
            )
            extra_payload_record = extra_payload_envelope["record"]
            extra_payload_record["payload"]["forged_authority"] = {
                "claim": "alternate execution truth"
            }
            canonical = JsonlDecisionLedger._canonical_record(
                extra_payload_record
            )
            extra_payload_envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    extra_payload_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            JsonlDecisionLedger(ledger_path).verify_integrity()

            extra_payload_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "payload schema is noncanonical",
            ):
                self._loop(
                    workspace,
                    observer=extra_payload_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(extra_payload_observer.calls, 0)

            model_envelope = json.loads(json.dumps(original_envelope))
            model_record = model_envelope["record"]
            model_record["payload"]["paper_execution"]["model_fingerprint"] = (
                "0" * 64
            )
            canonical = JsonlDecisionLedger._canonical_record(model_record)
            model_envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    model_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            JsonlDecisionLedger(ledger_path).verify_integrity()

            model_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "execution model conflicts with runtime",
            ):
                self._loop(
                    workspace,
                    observer=model_observer,
                    factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(model_observer.calls, 0)

            opportunity_envelope = json.loads(
                json.dumps(original_envelope)
            )
            opportunity_record = opportunity_envelope["record"]
            opportunity_item = opportunity_record["payload"][
                "paper_execution"
            ]["intent_evidence_json"]
            opportunity_payload = json.loads(opportunity_item)
            quote_payload = opportunity_payload["intents"][0][
                "opportunity"
            ]["quotes"][0]
            quote_payload["decimal_odds"] = "9.99"
            opportunity_record["payload"]["paper_execution"][
                "intent_evidence_json"
            ] = json.dumps(
                opportunity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            canonical = JsonlDecisionLedger._canonical_record(
                opportunity_record
            )
            opportunity_envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    opportunity_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            JsonlDecisionLedger(ledger_path).verify_integrity()

            opportunity_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "intent execution item is invalid",
            ):
                self._loop(
                    workspace,
                    observer=opportunity_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(opportunity_observer.calls, 0)

            risk_envelope = json.loads(json.dumps(original_envelope))
            risk_record = risk_envelope["record"]
            risk_evidence_json = risk_record["payload"][
                "paper_execution"
            ]["intent_evidence_json"]
            risk_evidence = json.loads(risk_evidence_json)
            risk_evidence["intents"][0]["risk_context"][
                "proposal_ts"
            ] = None
            risk_record["payload"]["paper_execution"][
                "intent_evidence_json"
            ] = json.dumps(
                risk_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            canonical = JsonlDecisionLedger._canonical_record(risk_record)
            risk_envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    risk_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            JsonlDecisionLedger(ledger_path).verify_integrity()

            risk_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "risk candidate conflicts with proven market/economic evidence",
            ):
                self._loop(
                    workspace,
                    observer=risk_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(risk_observer.calls, 0)

            ledger_path.write_text(
                json.dumps(
                    original_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            execution_events = list(execution_ledger.events())
            self.assertEqual(
                execution_events[-1]["event_type"],
                "RUN_COMPLETED",
            )

            canonical_observer = _DurableObserver(workspace, [()])
            canonical_resume = self._loop(
                workspace,
                observer=canonical_observer,
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
                book=resumed_book,
                authority=authority,
                paper_execution=resumed_execution,
            )
            self.assertEqual(canonical_observer.calls, 0)
            canonical_resume.close()

            def rewrite_execution_events(
                source_events: list[dict[str, object]],
            ) -> list[dict[str, object]]:
                rewritten = json.loads(json.dumps(source_events))
                previous_sha256 = None
                for sequence, item in enumerate(rewritten):
                    item["sequence"] = sequence
                    item["previous_sha256"] = previous_sha256
                    event_body = {
                        key: value
                        for key, value in item.items()
                        if key != "event_sha256"
                    }
                    item["event_sha256"] = hashlib.sha256(
                        json.dumps(
                            event_body,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    previous_sha256 = item["event_sha256"]
                execution_ledger.path.write_text(
                    "".join(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                        for item in rewritten
                    ),
                    encoding="utf-8",
                )
                execution_ledger._write_anchor_unlocked(rewritten)
                execution_ledger.events()
                return rewritten

            observation_events = json.loads(json.dumps(execution_events))
            reservation_index = next(
                index
                for index, item in enumerate(observation_events)
                if item["event_type"] == "RUN_RESERVED"
            )
            observation_events[reservation_index]["payload"][
                "observation_evidence_ids"
            ] = {"forged-action": "forged-evidence"}
            rewrite_execution_events(observation_events)

            reservation_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "reservation conflicts with decision evidence",
            ):
                self._loop(
                    workspace,
                    observer=reservation_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(reservation_observer.calls, 0)

            attempt_events = json.loads(json.dumps(execution_events))
            attempt_index = next(
                index
                for index, item in enumerate(attempt_events)
                if item["event_type"] == "ATTEMPT_RECORDED"
            )
            attempt_events[attempt_index]["payload"]["reason"] = (
                "forged synthetic execution reason"
            )
            rewrite_execution_events(attempt_events)

            synthetic_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "attempt conflicts with canonical synthetic execution",
            ):
                self._loop(
                    workspace,
                    observer=synthetic_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(synthetic_observer.calls, 0)

            event_key_events = json.loads(json.dumps(execution_events))
            completion_index = next(
                index
                for index, item in enumerate(event_key_events)
                if item["event_type"] == "RUN_COMPLETED"
            )
            event_key_events[completion_index]["event_key"] = (
                event_key_events[completion_index]["run_id"]
                + ":forged-complete"
            )
            rewrite_execution_events(event_key_events)

            identity_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "event identity is invalid",
            ):
                self._loop(
                    workspace,
                    observer=identity_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(identity_observer.calls, 0)

            extra_event_events = json.loads(json.dumps(execution_events))
            forged_event = json.loads(json.dumps(execution_events[-1]))
            forged_event["event_type"] = "FORGED_COMMITTED_RUN_EVENT"
            forged_event["event_key"] = (
                forged_event["run_id"] + ":forged-event"
            )
            forged_event["payload"] = {}
            extra_event_events.append(forged_event)
            rewrite_execution_events(extra_event_events)

            extra_event_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "run contains noncanonical events",
            ):
                self._loop(
                    workspace,
                    observer=extra_event_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(extra_event_observer.calls, 0)

            rewrite_execution_events(execution_events)
            truncated_execution = execution_events[:-1]
            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in truncated_execution
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(
                truncated_execution
            )
            self.assertNotIn(
                "RUN_COMPLETED",
                tuple(
                    item["event_type"]
                    for item in execution_ledger.events()
                ),
            )

            incomplete_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "lacks terminal #623 completion",
            ):
                self._loop(
                    workspace,
                    observer=incomplete_observer,
                    factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(incomplete_observer.calls, 0)

            tampered_completion = json.loads(
                json.dumps(execution_events[-1])
            )
            tampered_completion["payload"]["worst_case_exposure"] = "999"
            completion_body = {
                key: value
                for key, value in tampered_completion.items()
                if key != "event_sha256"
            }
            tampered_completion["event_sha256"] = hashlib.sha256(
                json.dumps(
                    completion_body,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            tampered_execution = [
                *truncated_execution,
                tampered_completion,
            ]
            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in tampered_execution
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(
                tampered_execution
            )
            execution_ledger.events()

            economics_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "completion conflicts with durable attempt economics",
            ):
                self._loop(
                    workspace,
                    observer=economics_observer,
                    factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(economics_observer.calls, 0)

            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in execution_events
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(execution_events)
            execution_ledger.events()

            reordered_events = json.loads(json.dumps(execution_events))
            scope_index = next(
                index
                for index, item in enumerate(reordered_events)
                if item["event_type"] == "PAPER_EXPOSURE_SCOPE_BOUND"
            )
            reserve_index = next(
                index
                for index, item in enumerate(reordered_events)
                if item["event_type"] == "RUN_RESERVED"
            )
            reordered_events[scope_index], reordered_events[reserve_index] = (
                reordered_events[reserve_index],
                reordered_events[scope_index],
            )
            previous_sha256 = None
            for sequence, item in enumerate(reordered_events):
                item["sequence"] = sequence
                item["previous_sha256"] = previous_sha256
                event_body = {
                    key: value
                    for key, value in item.items()
                    if key != "event_sha256"
                }
                item["event_sha256"] = hashlib.sha256(
                    json.dumps(
                        event_body,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                previous_sha256 = item["event_sha256"]
            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in reordered_events
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(reordered_events)
            execution_ledger.events()

            chronology_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "scope/reservation chronology is invalid",
            ):
                self._loop(
                    workspace,
                    observer=chronology_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(chronology_observer.calls, 0)

            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in execution_events
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(execution_events)
            execution_ledger.events()

            scope_events = json.loads(json.dumps(execution_events))
            scope_index = next(
                index
                for index, item in enumerate(scope_events)
                if item["event_type"] == "PAPER_EXPOSURE_SCOPE_BOUND"
            )
            scope_payload = scope_events[scope_index]["payload"]
            scope_payload["bindings"][0]["bankroll_id"] = "forged-bankroll"
            scope_body = {
                key: value
                for key, value in scope_payload.items()
                if key != "binding_sha256"
            }
            scope_payload["binding_sha256"] = hashlib.sha256(
                json.dumps(
                    scope_body,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()

            previous_sha256 = None
            for sequence, item in enumerate(scope_events):
                item["sequence"] = sequence
                item["previous_sha256"] = previous_sha256
                event_body = {
                    key: value
                    for key, value in item.items()
                    if key != "event_sha256"
                }
                item["event_sha256"] = hashlib.sha256(
                    json.dumps(
                        event_body,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                previous_sha256 = item["event_sha256"]

            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in scope_events
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(scope_events)
            execution_ledger.events()

            scope_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "exposure binding conflicts with canonical intent evidence",
            ):
                self._loop(
                    workspace,
                    observer=scope_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(scope_observer.calls, 0)

            execution_ledger.path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in execution_events
                ),
                encoding="utf-8",
            )
            execution_ledger._write_anchor_unlocked(execution_events)
            execution_ledger.events()

            original_tickets = dict(resumed_book.tickets)
            self.assertTrue(original_tickets)
            resumed_book.tickets.clear()
            missing_ticket_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "PaperBook does not bind exact #623 execution attempt",
            ):
                self._loop(
                    workspace,
                    observer=missing_ticket_observer,
                    factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(missing_ticket_observer.calls, 0)
            resumed_book.tickets.update(original_tickets)

            source_ticket = next(iter(original_tickets.values()))
            forged_ticket = copy.deepcopy(source_ticket)
            forged_ticket.ticket_id = "forged-execution-ticket"
            reason_prefix, _, _ = source_ticket.strategy_reason.partition(
                "paper_execution_attempt_id="
            )
            forged_ticket.strategy_reason = (
                reason_prefix
                + "paper_execution_attempt_id=forged-attempt"
            )
            resumed_book.tickets[forged_ticket.ticket_id] = forged_ticket

            extra_ticket_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "#623 marker set conflicts with terminal attempts",
            ):
                self._loop(
                    workspace,
                    observer=extra_ticket_observer,
                    factory=_PositiveIntentFactory(
                        self.INTENT_CONFIG_SHA256
                    ),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                    book=resumed_book,
                    authority=authority,
                    paper_execution=resumed_execution,
                )
            self.assertEqual(extra_ticket_observer.calls, 0)
            del resumed_book.tickets[forged_ticket.ticket_id]

    def test_intent_factory_cannot_inject_stale_quote_outside_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            current = self._event(
                selection="selection-a",
                sequence=2,
                odds="2.10",
                observed=self.START + timedelta(seconds=1),
            )
            stale = self._event(
                selection="selection-a",
                sequence=1,
                odds="2.00",
                observed=self.START,
            )
            base_factory = _PositiveIntentFactory(
                self.INTENT_CONFIG_SHA256
            )

            def stale_factory(input_id, snapshot):
                stale_snapshot = MirrorSnapshot(
                    revision=snapshot.revision,
                    events=(stale,),
                )
                return base_factory(input_id, stale_snapshot)

            stale_factory.strategy_version_id = "live-test-strategy-v1"

            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(current,)]),
                factory=stale_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "intent quote is not bound to focused market snapshot",
            ):
                loop.run_cycle()

            progress = json.loads(
                loop.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(progress["phase"], "pending")
            self.assertFalse((workspace / "decisions.jsonl").exists())
            loop.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=stale_factory,
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "intent quote is not bound to focused market snapshot",
            ):
                resumed.run_cycle()
            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())
            resumed.close()

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

    def test_custom_observer_cannot_publish_mirror_only_market_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)

            def mirror_only_observer(updates):
                updates.accept_persisted(event)
                return object()

            loop = self._loop(
                workspace,
                observer=mirror_only_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "market state is not durable at sampled append frontier",
            ):
                loop.run_cycle()

            self.assertFalse(loop.progress_path.exists())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            store = SQLiteMarketStore(workspace / "market.db")
            try:
                self.assertEqual(store.events(), [])
                self.assertEqual(store.append_generation_hint(), 0)
            finally:
                store.close()
            loop.close()

    def test_custom_observer_pending_restart_uses_append_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
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

            pending = json.loads(first.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(pending["market_append_generation"], 1)
            first.close()

            peer_store = SQLiteMarketStore(workspace / "market.db")
            try:
                peer_store.append(
                    self._event(
                        selection="selection-a",
                        sequence=2,
                        odds="2.10",
                        observed=self.START + timedelta(milliseconds=500),
                    )
                )
            finally:
                peer_store.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                resumed_factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )
            committed = json.loads(
                resumed.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["market_append_generation"], 1)

            advanced = resumed.run_cycle()
            self.assertEqual(advanced.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 1)
            self.assertEqual(
                resumed_factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            resumed.close()

    def test_pending_restart_uses_frontier_after_backdated_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many(
                    (self._event(selection="selection-a", sequence=1),)
                )
            finally:
                seed_store.close()

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            strategy = self._strategy_version()
            registry = self._scientific_registry(workspace, strategy)
            first_provider = _EmptyProvider()
            first = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=fail_after_pending,
                scientific_registry=registry,
                provider=first_provider,
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            pending = json.loads(first.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["schema_version"], 2)
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(pending["market_append_generation"], 1)
            first.close()

            peer_store = SQLiteMarketStore(workspace / "market.db")
            try:
                peer_store.append(
                    self._event(
                        selection="selection-a",
                        sequence=2,
                        odds="2.10",
                        observed=self.START + timedelta(milliseconds=500),
                    )
                )
            finally:
                peer_store.close()

            resumed_provider = _EmptyProvider()
            resumed_factory = _EmptyIntentFactory()
            resumed = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=resumed_factory,
                scientific_registry=registry,
                provider=resumed_provider,
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_provider.calls, 0)
            self.assertEqual(
                resumed_factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )
            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertNotIn("market_append_generation", records[0].payload)
            committed = json.loads(
                resumed.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["market_append_generation"], 1)

            advanced = resumed.run_cycle()
            self.assertEqual(advanced.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_provider.calls, 1)
            self.assertEqual(
                resumed_factory.calls[-1],
                ("input-a", (("selection-a", 2, "open"),)),
            )
            resumed.close()

    def test_pending_publication_holds_market_append_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            peer_store = SQLiteMarketStore(workspace / "market.db")
            blocked = {"value": False}
            attempted = {"value": False}

            import autosport.live_decision_loop as live_loop_module

            real_atomic_write_json = live_loop_module.atomic_write_json

            def append_during_pending(path, payload):
                if (
                    Path(path)
                    == workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
                    and payload.get("phase") == "pending"
                    and not attempted["value"]
                ):
                    attempted["value"] = True
                    try:
                        peer_store.append(
                            self._event(
                                selection="selection-a",
                                sequence=2,
                                odds="2.10",
                                observed=self.START
                                + timedelta(milliseconds=500),
                            )
                        )
                    except WorkspaceEconomicLockBusyError:
                        blocked["value"] = True
                    else:
                        self.fail(
                            "peer append entered market authority during "
                            "PENDING publication"
                        )
                return real_atomic_write_json(path, payload)

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
            try:
                with patch(
                    "autosport.live_decision_loop.atomic_write_json",
                    side_effect=append_during_pending,
                ):
                    decided = loop.run_cycle()

                self.assertEqual(decided.status, LiveCycleStatus.DECIDED)
                self.assertTrue(attempted["value"])
                self.assertTrue(blocked["value"])
                progress = json.loads(
                    loop.progress_path.read_text(encoding="utf-8")
                )
                self.assertEqual(progress["market_append_generation"], 1)

                self.assertTrue(
                    peer_store.append(
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(milliseconds=500),
                        )
                    )
                )
            finally:
                loop.close()
                peer_store.close()

    def test_pending_restart_ignores_malformed_later_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
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
            pending = json.loads(first.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["market_append_generation"], 1)
            first.close()

            path = workspace / "market.db"
            corruptor = SQLiteMarketStore(path)
            try:
                tail = self._event(
                    selection="selection-a",
                    sequence=2,
                    odds="2.10",
                    observed=self.START + timedelta(milliseconds=500),
                )
                payload = json.dumps(
                    tail.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                corruptor.connection.execute(
                    """INSERT INTO market_events
                       (dedupe_key,quote_key,event_id,market_id,selection_id,
                        decimal_odds,observed_ts,source_id,sequence,payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tail.dedupe_key,
                        tail.quote_key,
                        tail.event_id,
                        tail.market_id,
                        tail.selection_id,
                        str(tail.decimal_odds),
                        tail.observed_ts,
                        tail.source_id,
                        tail.sequence,
                        payload,
                    ),
                )
                corruptor.connection.execute(
                    """INSERT INTO market_event_commit_order
                       (dedupe_key, append_generation)
                       VALUES (?, ?)""",
                    (tail.dedupe_key, 3),
                )
                corruptor.connection.commit()
            finally:
                corruptor.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                resumed_factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )
            committed = json.loads(
                resumed.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["market_append_generation"], 1)
            resumed.close()

            with self.assertRaises(ValueError):
                SQLiteMarketStore(path)

    def test_pending_restart_rejects_rolled_back_market_append_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            seed_store = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed_store).publish_many(
                    (self._event(selection="selection-a", sequence=1),)
                )
            finally:
                seed_store.close()

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            strategy = self._strategy_version()
            registry = self._scientific_registry(workspace, strategy)
            first = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=fail_after_pending,
                scientific_registry=registry,
                provider=_EmptyProvider(),
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()
            first.close()

            progress_path = workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            rolled = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(rolled["market_append_generation"], 1)
            rolled["market_append_generation"] = 0
            progress_path.write_text(
                json.dumps(rolled, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            resumed_provider = _EmptyProvider()
            resumed_factory = _EmptyIntentFactory()
            resumed = PersistentLiveDecisionLoop(
                workspace,
                loop_id="live-test-loop",
                mode=LiveDecisionMode.PAPER,
                book=PaperBook("1000"),
                authority=self._authority(),
                intent_factory=resumed_factory,
                scientific_registry=registry,
                provider=resumed_provider,
                max_quote_age=timedelta(seconds=5),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "replayed market state changed",
            ):
                resumed.run_cycle()

            self.assertEqual(resumed_provider.calls, 0)
            self.assertEqual(resumed_factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())
            resumed.close()

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

    def test_provider_gap_rejects_registry_change_after_snapshot_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [ProviderUnavailableError("provider unavailable")],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            original_market_sha = loop._market_state_sha256
            mutated = [False]

            def mutate_registry_after_gap_capture():
                value = original_market_sha()
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.unregister_input("input-a"))
                    loop.register_input(
                        "input-a",
                        selection_ids="selection-b",
                    )
                return value

            with (
                patch.object(
                    loop,
                    "_market_state_sha256",
                    side_effect=mutate_registry_after_gap_capture,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "registry changed after snapshot capture",
                ),
            ):
                loop.run_cycle()

            self.assertFalse(loop.progress_path.exists())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            loop.close()

    def test_provider_gap_rejects_same_selector_reincarnation_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [ProviderUnavailableError("provider unavailable")],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            original_market_sha = loop._market_state_sha256
            mutated = [False]

            def reincarnate_index_after_gap_capture():
                value = original_market_sha()
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        selection_ids="selection-a",
                    )
                return value

            with (
                patch.object(
                    loop,
                    "_market_state_sha256",
                    side_effect=reincarnate_index_after_gap_capture,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "focused dependency registry changed after snapshot capture",
                ),
            ):
                loop.run_cycle()

            self.assertTrue(mutated[0])
            self.assertFalse(loop.progress_path.exists())
            self.assertFalse((workspace / "decisions.jsonl").exists())
            loop.close()

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

    def test_pending_frontier_rejects_truncated_prior_ledger_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (self._event(sequence=1),),
                        ProviderUnavailableError("provider unavailable"),
                    ],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger_path = workspace / "decisions.jsonl"
            prior_size = ledger_path.stat().st_size
            self.assertGreater(prior_size, 0)
            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                loop,
                "_persist_plan",
                side_effect=RuntimeError("simulated loss after pending frontier"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "loss after pending frontier",
                ):
                    loop.run_cycle()

            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(pending["gate"], "provider_gap")
            self.assertEqual(pending["ledger_offset"], prior_size)
            ledger_path.write_bytes(b"")
            loop.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "ledger frontier was truncated",
            ):
                resumed.run_cycle()

            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(ledger_path.read_bytes(), b"")
            self.assertEqual(
                json.loads(resumed.progress_path.read_text(encoding="utf-8"))["phase"],
                "pending",
            )
            resumed.close()

    def test_direct_selector_swap_after_capture_blocks_pending_publication(self) -> None:
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
            original_market_sha = loop._market_state_sha256
            mutated = [False]

            def mutate_index_after_capture():
                value = original_market_sha()
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        selection_ids="selection-b",
                    )
                return value

            with (
                patch.object(
                    loop,
                    "_market_state_sha256",
                    side_effect=mutate_index_after_capture,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "focused dependency registry changed after snapshot capture",
                ),
            ):
                loop.run_cycle()

            self.assertFalse(loop.progress_path.exists())
            loop.close()

    def test_pending_process_state_is_published_before_mutation_guard_releases(self) -> None:
        from contextlib import contextmanager

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
            real_guard = loop.dependencies.registry_mutation_guard
            observed_pending_guard_exit = [False]

            @contextmanager
            def observed_guard():
                with real_guard():
                    yield
                    if not loop.progress_path.exists():
                        return
                    raw = json.loads(
                        loop.progress_path.read_text(encoding="utf-8")
                    )
                    if raw["phase"] != "pending":
                        return
                    observed_pending_guard_exit[0] = True
                    self.assertIsNotNone(loop._progress)
                    self.assertEqual(loop._progress.phase, "pending")
                    self.assertIsNotNone(loop._pending_dependency_revisions)
                    self.assertEqual(
                        loop._pending_dependency_revisions,
                        tuple(
                            (dependency.input_id, revision)
                            for dependency, revision
                            in loop.dependencies.registry_state_snapshot()
                        ),
                    )

            with patch.object(
                loop.dependencies,
                "registry_mutation_guard",
                side_effect=observed_guard,
            ):
                result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertTrue(observed_pending_guard_exit[0])
            loop.close()

    def test_same_selector_reincarnation_during_pending_preaction_blocks_publication(self) -> None:
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
            original_shadow = loop.authority.risk_policy._shadow_book_for_allocation
            mutated = [False]

            def shadow_then_reincarnate(book):
                shadow = original_shadow(book)
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        selection_ids="selection-a",
                    )
                return shadow

            with (
                patch.object(
                    loop.authority.risk_policy,
                    "_shadow_book_for_allocation",
                    side_effect=shadow_then_reincarnate,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "focused dependency registry changed concurrently before pending publication",
                ),
            ):
                loop.run_cycle()

            self.assertTrue(mutated[0])
            self.assertFalse(loop.progress_path.exists())
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            loop.close()

    def test_selector_replacement_during_pending_preaction_blocks_publication(self) -> None:
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
            original_shadow = loop.authority.risk_policy._shadow_book_for_allocation
            mutated = [False]

            def shadow_then_replace(book):
                shadow = original_shadow(book)
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        selection_ids="selection-b",
                    )
                return shadow

            with (
                patch.object(
                    loop.authority.risk_policy,
                    "_shadow_book_for_allocation",
                    side_effect=shadow_then_replace,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "focused dependency registry changed concurrently before pending publication",
                ),
            ):
                loop.run_cycle()

            self.assertTrue(mutated[0])
            self.assertFalse(loop.progress_path.exists())
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            loop.close()

    def test_same_selector_reincarnation_after_capture_blocks_pending_publication(self) -> None:
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
            original_market_sha = loop._market_state_sha256
            mutated = [False]

            def reincarnate_index_after_capture():
                value = original_market_sha()
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        selection_ids="selection-a",
                    )
                return value

            with (
                patch.object(
                    loop,
                    "_market_state_sha256",
                    side_effect=reincarnate_index_after_capture,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "focused dependency registry changed after snapshot capture",
                ),
            ):
                loop.run_cycle()

            self.assertTrue(mutated[0])
            self.assertFalse(loop.progress_path.exists())
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            loop.close()

    def test_registry_change_after_snapshot_capture_blocks_pending_publication(self) -> None:
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
            original_market_sha = loop._market_state_sha256
            mutated = [False]

            def mutate_registry_after_capture():
                value = original_market_sha()
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(loop.unregister_input("input-a"))
                    loop.register_input(
                        "input-a",
                        selection_ids="selection-b",
                    )
                return value

            with (
                patch.object(
                    loop,
                    "_market_state_sha256",
                    side_effect=mutate_registry_after_capture,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "registry changed after snapshot capture",
                ),
            ):
                loop.run_cycle()

            self.assertFalse(loop.progress_path.exists())
            loop.close()

    def test_unfinished_progress_blocks_dependency_registry_mutation(self) -> None:
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

            def fail_after_pending(_input_id, _snapshot):
                raise RuntimeError("stop after pending")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            loop.intent_factory = fail_after_pending
            with self.assertRaisesRegex(RuntimeError, "stop after pending"):
                loop.run_cycle()

            self.assertIsNotNone(loop._progress)
            self.assertEqual(loop._progress.phase, "pending")
            # Exact idempotent re-registration is a no-op and remains allowed;
            # only mutations are fenced while recovery is unfinished.
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            self.assertFalse(loop.unregister_input("missing-input"))
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "cannot mutate live dependency registry while a decision is unfinished",
            ):
                loop.unregister_input("input-a")
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "cannot mutate live dependency registry while a decision is unfinished",
            ):
                loop.register_input("input-b", selection_ids="selection-b")
            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            loop.close()

    def test_pending_frontier_allows_prior_same_time_live_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (self._event(sequence=1, observed=self.START),),
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START,
                        ),
                    ),
                ],
            )
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            def fail_after_pending(_input_id, _snapshot):
                raise RuntimeError("simulated loss after second pending publication")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
            loop.intent_factory = fail_after_pending
            with self.assertRaisesRegex(
                RuntimeError,
                "loss after second pending publication",
            ):
                loop.run_cycle()
            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertIsInstance(pending["ledger_offset"], int)
            self.assertGreater(pending["ledger_offset"], 0)
            loop.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            recovered = resumed.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].observed_ts, records[1].observed_ts)
            self.assertNotEqual(records[0].decision_id, records[1].decision_id)
            resumed.close()

    def test_pending_frontier_rejects_rolled_back_same_time_provider_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [ProviderUnavailableError("provider unavailable")],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            first.register_input("input-a", selection_ids="selection-a")
            with patch.object(
                first,
                "_persist_plan",
                side_effect=RuntimeError("simulated loss after gap pending"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "loss after gap pending",
                ):
                    first.run_cycle()

            pending_path = (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            )
            rolled_pending = pending_path.read_bytes()
            pending = json.loads(rolled_pending)
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(pending["gate"], "provider_gap")
            self.assertEqual(pending["ledger_offset"], 0)
            first.close()

            observer = _DurableObserver(
                workspace,
                [(self._event(sequence=1, observed=self.START),)],
            )
            active = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            self.assertEqual(active.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(active.run_cycle().status, LiveCycleStatus.DECIDED)
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].observed_ts, records[1].observed_ts)
            self.assertNotEqual(records[0].decision_id, records[1].decision_id)
            active.close()

            pending_path.write_bytes(rolled_pending)
            stale_observer = _DurableObserver(workspace, [()])
            stale = self._loop(
                workspace,
                observer=stale_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "superseded after publication",
            ):
                stale.run_cycle()
            self.assertEqual(stale_observer.calls, 0)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                2,
            )
            stale.close()

    def test_distinct_same_time_market_states_append_distinct_live_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (self._event(sequence=1, observed=self.START),),
                    (
                        self._event(
                            sequence=2,
                            odds="2.10",
                            observed=self.START,
                        ),
                    ),
                ],
            )
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()
            second = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].observed_ts, records[1].observed_ts)
            self.assertNotEqual(records[0].decision_id, records[1].decision_id)
            loop.close()

    def test_identical_same_time_provider_gap_reuses_last_durable_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    ProviderUnavailableError("provider unavailable"),
                    ProviderUnavailableError("provider unavailable"),
                ],
            )
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            first = loop.run_cycle()
            second = loop.run_cycle()

            self.assertEqual(first.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertEqual(second.status, LiveCycleStatus.PROVIDER_GAP)
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 1)
            progress = json.loads(
                (
                    workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(progress["phase"], "committed")
            self.assertEqual(progress["decision_id"], records[0].decision_id)
            loop.close()

    def test_same_time_live_duplicate_skips_unrelated_trailing_ledger_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    ProviderUnavailableError("provider unavailable"),
                    ProviderUnavailableError("provider unavailable"),
                ],
            )
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.PROVIDER_GAP)

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            ledger.append(
                DecisionRecord(
                    replay_run_id="diagnostic:test",
                    agent="test",
                    observed_ts=clock.value.isoformat(),
                    action="DIAGNOSTIC",
                    payload={
                        "kind": "unrelated",
                        "blob": "x" * 20_000,
                    },
                    context_hash="diagnostic-context",
                    decision_id="diagnostic-unrelated-record",
                )
            )
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.PROVIDER_GAP)
            records = ledger.verified_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(
                sum(
                    record.replay_run_id == "live:live-test-loop"
                    for record in records
                ),
                1,
            )
            self.assertEqual(records[-1].decision_id, "diagnostic-unrelated-record")
            loop.close()

    def test_midprocess_torn_ledger_tail_blocks_next_material_decision_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    ProviderUnavailableError("provider unavailable"),
                    ProviderUnavailableError("provider unavailable"),
                ],
            )
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(
                loop.run_cycle().status,
                LiveCycleStatus.PROVIDER_GAP,
            )

            ledger_path = workspace / "decisions.jsonl"
            with ledger_path.open("ab") as handle:
                handle.write(b'{"torn":')
            before = ledger_path.read_bytes()

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "unterminated final record",
            ):
                loop.run_cycle()

            self.assertEqual(ledger_path.read_bytes(), before)
            self.assertEqual(observer.calls, 2)
            loop.close()

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

    def test_clean_restart_rejects_same_id_selector_swap_after_capture(self) -> None:
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
            first.close()

            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            original_market_sha = resumed._market_state_sha256
            mutated = [False]

            def swap_selector_after_capture():
                value = original_market_sha()
                if not mutated[0]:
                    mutated[0] = True
                    self.assertTrue(resumed.unregister_input("input-a"))
                    resumed.register_input(
                        "input-a",
                        selection_ids="selection-b",
                    )
                return value

            with (
                patch.object(
                    resumed,
                    "_market_state_sha256",
                    side_effect=swap_selector_after_capture,
                ),
                self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "registry changed after snapshot capture",
                ),
            ):
                resumed.run_cycle()

            self.assertEqual(
                resumed.dependencies.input_ids,
                ("input-a",),
            )
            resumed.close()

    def test_future_successor_preserves_visible_predecessor_across_restart_until_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_clock = _ManualClock(self.START + timedelta(seconds=1))
            predecessor = self._event(sequence=1, odds="2.00")
            successor = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.10"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            first_factory = _EmptyIntentFactory()
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(predecessor,), (successor,)],
                ),
                factory=first_factory,
                clock=first_clock,
            )
            first.register_input("input-a", selection_ids="selection-a")

            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                first_factory.calls[-1],
                ("input-a", (("selection-a", 1, "open"),)),
            )

            first_factory.calls.clear()
            first_clock.value = self.START + timedelta(seconds=2)
            first.run_cycle()
            self.assertEqual(
                first_factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(
                first._availability_deadlines["input-a"],
                self.START + timedelta(seconds=4),
            )
            first.close()

            resumed_clock = _ManualClock(self.START + timedelta(seconds=3))
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(), ()]),
                factory=resumed_factory,
                clock=resumed_clock,
            )
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(
                resumed_factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(
                resumed._availability_deadlines["input-a"],
                self.START + timedelta(seconds=4),
            )

            resumed_factory.calls.clear()
            resumed_clock.value = self.START + timedelta(seconds=4)
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                resumed_factory.calls,
                [("input-a", (("selection-a", 2, "open"),))],
            )
            resumed.close()

    def test_market_frontier_retries_history_classification_after_selector_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=2)
            provider_a = self._event(sequence=1)
            provider_b_predecessor = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-b",
                decimal_odds=Decimal("3.00"),
                observed_ts=(self.START + timedelta(seconds=1)).isoformat(),
                source_id="provider-b",
                sequence=1,
                status="open",
                source_ts=(self.START + timedelta(seconds=1)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=1)).isoformat(),
            )
            provider_b_future = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-b",
                decimal_odds=Decimal("3.20"),
                observed_ts=decision_time.isoformat(),
                source_id="provider-b",
                sequence=2,
                status="open",
                source_ts=decision_time.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            seed = SQLiteMarketStore(workspace / "market.db")
            try:
                MarketEventBus(seed).publish_many(
                    (provider_a, provider_b_predecessor, provider_b_future)
                )
            finally:
                seed.close()

            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop.register_input("input-a", source_ids="provider-a")
            original_requires = (
                loop.dependencies.requires_current_history_fallback
            )
            calls = [0]

            def replace_after_classification(input_id, *, as_of):
                result = original_requires(input_id, as_of=as_of)
                calls[0] += 1
                if calls[0] == 1:
                    self.assertFalse(result)
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        source_ids="provider-b",
                    )
                return result

            with patch.object(
                loop.dependencies,
                "requires_current_history_fallback",
                side_effect=replace_after_classification,
            ):
                sampled = loop._sample_decision_market_frontier()

            self.assertEqual(sampled, decision_time)
            self.assertGreaterEqual(calls[0], 2)
            self.assertTrue(loop._decision_market_history_frozen)
            self.assertIsNotNone(loop._decision_market_history)
            self.assertIn(
                provider_b_predecessor.dedupe_key,
                {
                    event.dedupe_key
                    for event, _generation in loop._decision_market_history
                },
            )
            loop.close()

    def test_capture_retries_dependency_replacement_during_availability_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=2)
            provider_a = self._event(sequence=1)
            provider_b = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-b",
                decimal_odds=Decimal("3.00"),
                observed_ts=self.START.isoformat(),
                source_id="provider-b",
                sequence=1,
                status="open",
                source_ts=self.START.isoformat(),
                ingest_ts=self.START.isoformat(),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(provider_a, provider_b)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop.register_input("input-a", source_ids="provider-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            original_causal = loop.dependencies.causal_view
            calls = [0]

            def replace_during_availability(input_id):
                snapshot = original_causal(input_id)
                calls[0] += 1
                if calls[0] == 1:
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        source_ids="provider-b",
                    )
                return snapshot

            with patch.object(
                loop.dependencies,
                "causal_view",
                side_effect=replace_during_availability,
            ):
                snapshots = loop._capture_input_views(
                    ("input-a",),
                    decision_time,
                    incremental=False,
                )

            self.assertGreaterEqual(calls[0], 2)
            self.assertEqual(
                tuple(
                    (event.source_id, event.selection_id, event.sequence)
                    for event in snapshots["input-a"].events
                ),
                (("provider-b", "selection-b", 1),),
            )
            loop.close()

    def test_capture_retries_history_classification_after_dependency_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            decision_time = self.START + timedelta(seconds=2)
            provider_a = self._event(sequence=1)
            provider_b_predecessor = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-b",
                decimal_odds=Decimal("3.00"),
                observed_ts=(self.START + timedelta(seconds=1)).isoformat(),
                source_id="provider-b",
                sequence=1,
                status="open",
                source_ts=(self.START + timedelta(seconds=1)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=1)).isoformat(),
            )
            provider_b_future = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-b",
                decimal_odds=Decimal("3.20"),
                observed_ts=decision_time.isoformat(),
                source_id="provider-b",
                sequence=2,
                status="open",
                source_ts=decision_time.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(provider_a, provider_b_predecessor, provider_b_future)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(decision_time),
            )
            loop.register_input("input-a", source_ids="provider-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            original_requires = (
                loop.dependencies.requires_current_history_fallback
            )
            calls = [0]

            def replace_after_classification(input_id, *, as_of):
                result = original_requires(input_id, as_of=as_of)
                calls[0] += 1
                if calls[0] == 1:
                    self.assertFalse(result)
                    self.assertTrue(loop.dependencies.unregister("input-a"))
                    loop.dependencies.register(
                        "input-a",
                        source_ids="provider-b",
                    )
                return result

            with patch.object(
                loop.dependencies,
                "requires_current_history_fallback",
                side_effect=replace_after_classification,
            ):
                snapshots = loop._capture_input_views(
                    ("input-a",),
                    decision_time,
                    incremental=False,
                )

            self.assertGreaterEqual(calls[0], 2)
            self.assertEqual(
                tuple(
                    (event.source_id, event.selection_id, event.sequence)
                    for event in snapshots["input-a"].events
                ),
                (("provider-b", "selection-b", 1),),
            )
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                self.START + timedelta(seconds=4),
            )
            loop.close()

    def test_multiple_future_successors_schedule_each_causal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            predecessor = self._event(sequence=1, odds="2.00")
            first_future = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.10"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            second_future = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.20"),
                observed_ts=(self.START + timedelta(seconds=3)).isoformat(),
                source_id="provider-a",
                sequence=3,
                status="open",
                source_ts=(self.START + timedelta(seconds=3)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=6)).isoformat(),
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(predecessor,), (first_future, second_future), (), ()],
                ),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            loop.run_cycle()
            self.assertEqual(factory.calls, [("input-a", (("selection-a", 1, "open"),))])
            self.assertEqual(loop._availability_deadlines["input-a"], self.START + timedelta(seconds=4))

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=4)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", (("selection-a", 2, "open"),))])
            self.assertEqual(loop._availability_deadlines["input-a"], self.START + timedelta(seconds=6))

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=6)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", (("selection-a", 3, "open"),))])
            loop.close()

    def test_future_suspension_invalidates_visible_predecessor_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            predecessor = self._event(sequence=1, odds="2.00")
            suspended = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="suspended",
                source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(predecessor,), (suspended,), ()]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=2)
            loop.run_cycle()
            self.assertEqual(factory.calls, [("input-a", (("selection-a", 1, "open"),))])
            self.assertEqual(loop._availability_deadlines["input-a"], self.START + timedelta(seconds=4))

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=4)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])
            loop.close()

    def test_future_source_timestamp_preserves_predecessor_until_source_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            predecessor = self._event(sequence=1, odds="2.00")
            future_source = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.10"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=(self.START + timedelta(seconds=4)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=2)).isoformat(),
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(predecessor,), (future_source,), ()]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=2)
            loop.run_cycle()
            self.assertEqual(factory.calls, [("input-a", (("selection-a", 1, "open"),))])
            self.assertEqual(loop._availability_deadlines["input-a"], self.START + timedelta(seconds=4))

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=4)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", (("selection-a", 2, "open"),))])
            loop.close()

    def test_multiple_future_inputs_share_one_verified_history_scan_per_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            first_a = self._event(selection="selection-a", sequence=1)
            first_b = self._event(selection="selection-b", sequence=1)
            future_a = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.10"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=4)).isoformat(),
            )
            future_b = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-b",
                decimal_odds=Decimal("2.20"),
                observed_ts=(self.START + timedelta(seconds=2)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=(self.START + timedelta(seconds=2)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=5)).isoformat(),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(first_a, first_b), (future_a, future_b)],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            self._register_two(loop)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            original = SQLiteMarketStore.events_with_append_generation
            calls = {"count": 0}

            def counted(store, *args, **kwargs):
                calls["count"] += 1
                return original(store, *args, **kwargs)

            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                SQLiteMarketStore,
                "events_with_append_generation",
                new=counted,
            ):
                loop.run_cycle()

            self.assertEqual(calls["count"], 1)
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                self.START + timedelta(seconds=4),
            )
            self.assertEqual(
                loop._availability_deadlines["input-b"],
                self.START + timedelta(seconds=5),
            )
            loop.close()

    def test_normal_live_hot_path_does_not_scan_durable_history(self) -> None:
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

            with patch.object(
                SQLiteMarketStore,
                "events_with_append_generation",
                side_effect=AssertionError(
                    "ordinary current live decision must not scan durable history"
                ),
            ):
                self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            loop.close()

    def test_future_stale_successor_supersedes_still_fresh_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=9))
            predecessor = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=(self.START + timedelta(seconds=8)).isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=(self.START + timedelta(seconds=8)).isoformat(),
                ingest_ts=(self.START + timedelta(seconds=8)).isoformat(),
            )
            stale_successor = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.10"),
                observed_ts=(self.START + timedelta(seconds=9)).isoformat(),
                source_id="provider-a",
                sequence=2,
                status="open",
                source_ts=self.START.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=10)).isoformat(),
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(predecessor,), (stale_successor,), ()],
                ),
                factory=factory,
                clock=clock,
                max_quote_age=timedelta(seconds=5),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=9, microseconds=500000)
            loop.run_cycle()
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                self.START + timedelta(seconds=10),
            )

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=10)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])
            loop.close()

    def test_future_local_availability_recomputes_at_exact_boundary_without_new_market_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            future_ingest = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=self.START.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=self.START.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=3)).isoformat(),
            )
            observer = _DurableObserver(workspace, [(future_ingest,), (), ()])
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
            self.assertEqual(factory.calls, [("input-a", ())])
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                self.START + timedelta(seconds=3),
            )

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=2)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(factory.calls, [])

            clock.value = self.START + timedelta(seconds=3)
            third = loop.run_cycle()
            self.assertEqual(third.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertIsNone(loop._availability_deadlines["input-a"])
            loop.close()

    def test_future_local_availability_is_rebuilt_across_clean_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            future_ingest = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=self.START.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=self.START.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=3)).isoformat(),
            )
            first_clock = _ManualClock(self.START + timedelta(seconds=1))
            first_factory = _EmptyIntentFactory()
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(future_ingest,)]),
                factory=first_factory,
                clock=first_clock,
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                first._availability_deadlines["input-a"],
                self.START + timedelta(seconds=3),
            )
            first.close()

            resumed_clock = _ManualClock(self.START + timedelta(seconds=2))
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(), ()]),
                factory=resumed_factory,
                clock=resumed_clock,
            )
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(
                resumed._availability_deadlines["input-a"],
                self.START + timedelta(seconds=3),
            )

            resumed_factory.calls.clear()
            resumed_clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(resumed.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                resumed_factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            resumed.close()

    def test_far_future_causal_timestamp_does_not_overflow_transition_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START)
            far_future = datetime.max.replace(tzinfo=timezone.utc)
            event = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=far_future.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=far_future.isoformat(),
                ingest_ts=far_future.isoformat(),
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])
            self.assertEqual(
                loop._availability_deadlines["input-a"],
                far_future,
            )
            loop.close()

    def test_future_local_availability_after_quote_expiry_does_not_schedule_false_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            too_late = MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="selection-a",
                decimal_odds=Decimal("2.00"),
                observed_ts=self.START.isoformat(),
                source_id="provider-a",
                sequence=1,
                status="open",
                source_ts=self.START.isoformat(),
                ingest_ts=(self.START + timedelta(seconds=7)).isoformat(),
            )
            observer = _DurableObserver(workspace, [(too_late,), (), ()])
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
            self.assertEqual(factory.calls, [("input-a", ())])
            self.assertIsNone(loop._availability_deadlines["input-a"])

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=7)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(factory.calls, [])
            loop.close()

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

    def test_saturated_economic_quote_age_has_no_unrepresentable_freshness_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            goal = EconomicGoalContract(
                goal_id="goal-live-saturated-age",
                revision=1,
                bankroll_id="bankroll-live-test",
                currency="EUR",
                max_quote_age_seconds=Decimal("999999999999999999"),
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            clock = _ManualClock(self.START + timedelta(seconds=1))
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=factory,
                clock=clock,
                authority=authority,
                max_quote_age=None,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(loop.max_quote_age, timedelta.max)
            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertIsNone(loop._freshness_deadlines["input-a"])
            loop.close()

    def test_clock_regression_after_committed_decision_fails_before_provider_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [(self._event(sequence=1),), ()],
            )
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertEqual(observer.calls, 1)

            clock.value = self.START
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "clock moved backwards",
            ):
                loop.run_cycle()

            self.assertEqual(observer.calls, 1)
            loop.close()

    def test_clock_regression_floor_is_restored_from_durable_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_clock = _ManualClock(self.START + timedelta(seconds=1))
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=first_clock,
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)
            first.close()

            regressed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=regressed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "clock moved backwards",
            ):
                resumed.run_cycle()
            self.assertEqual(regressed_observer.calls, 0)
            resumed.close()

    def test_clock_regression_inside_one_cycle_fails_before_decision_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))

            def regress_clock(_updates):
                clock.value = self.START
                return object()

            loop = self._loop(
                workspace,
                observer=regress_clock,
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "clock moved backwards",
            ):
                loop.run_cycle()

            self.assertFalse(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )
            loop.close()

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

    def test_committed_zero_stake_rejects_forged_execution_evidence(self) -> None:
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
            first.close()

            ledger_path = workspace / "decisions.jsonl"
            envelope = json.loads(ledger_path.read_text(encoding="utf-8"))
            record = envelope["record"]
            record["payload"]["paper_execution"] = {
                "schema": "autosport.paper_execution_adoption",
                "schema_version": 1,
                "plan_id": "forged-plan",
                "plan_fingerprint": "forged-plan-fingerprint",
                "model_fingerprint": "forged-model-fingerprint",
                "run_id": "forged-run",
                "intent_evidence_json": "{}",
            }
            canonical = JsonlDecisionLedger._canonical_record(record)
            envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            JsonlDecisionLedger(ledger_path).verify_integrity()

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "unexpected execution evidence",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_restart_rejects_semantically_rehashed_committed_plan_tamper(self) -> None:
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
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            loop.close()

            ledger_path = workspace / "decisions.jsonl"
            envelope = json.loads(ledger_path.read_text(encoding="utf-8"))
            record = envelope["record"]
            record["payload"]["plan"]["reason"] = "semantically rewritten plan"
            canonical = JsonlDecisionLedger._canonical_record(record)
            envelope["sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            # Envelope integrity alone is deliberately insufficient: restart must
            # re-enter the canonical PortfolioPlan parser and reject the stale inner
            # plan_sha256 instead of trusting the separately copied top-level digest.
            JsonlDecisionLedger(ledger_path).verify_integrity()
            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "PortfolioPlan is invalid",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                )
            self.assertEqual(resumed_observer.calls, 0)

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

    def test_rolled_back_pending_progress_cannot_remint_existing_live_decision(self) -> None:
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
            progress_path = (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            )
            rolled = json.loads(progress_path.read_text(encoding="utf-8"))
            rolled["phase"] = "pending"
            rolled["decision_id"] = None
            rolled["plan_sha256"] = None
            rolled["ledger_offset"] = None

            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            loop.close()
            progress_path.write_text(
                json.dumps(rolled, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "predates an already durable live decision",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                2,
            )

    def test_rolled_back_append_pending_progress_cannot_hide_newer_live_decision(self) -> None:
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
            progress_path = (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            )
            rolled = json.loads(progress_path.read_text(encoding="utf-8"))
            rolled["phase"] = "append_pending"

            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            loop.close()
            progress_path.write_text(
                json.dumps(rolled, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "not the latest durable live decision",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_rolled_back_committed_progress_fails_closed_against_newer_live_ledger(self) -> None:
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
            progress_path = (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            )
            first_progress = progress_path.read_bytes()

            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            loop.close()
            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            self.assertEqual(len(ledger.verified_records()), 2)
            ledger.append(
                DecisionRecord(
                    replay_run_id="diagnostic:test",
                    agent="test",
                    observed_ts=clock.value.isoformat(),
                    action="DIAGNOSTIC",
                    payload={"kind": "trailing-unrelated"},
                    context_hash="diagnostic-context",
                    decision_id="diagnostic-after-newer-live-decision",
                )
            )

            progress_path.write_bytes(first_progress)

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "not the latest durable live decision",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )
            self.assertEqual(resumed_observer.calls, 0)

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

    def test_missing_progress_with_durable_live_decision_history_fails_closed_on_restart(self) -> None:
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
            loop.close()

            progress_path = (
                workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            )
            self.assertTrue(progress_path.exists())
            progress_path.unlink()

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "progress is missing",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                )
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )

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
            loop._availability_generations["input-a"] = 11
            loop._availability_deadlines["input-a"] = old_deadline
            loop._availability_heap.append((old_deadline, "input-a", 11))

            self.assertTrue(loop.unregister_input("input-a"))
            self.assertEqual(loop._freshness_generations["input-a"], 8)
            self.assertNotIn("input-a", loop._freshness_deadlines)
            self.assertEqual(loop._availability_generations["input-a"], 12)
            self.assertNotIn("input-a", loop._availability_deadlines)

            loop.register_input("input-a", selection_ids="selection-a")
            expired = loop._expire_freshness_inputs(old_deadline + timedelta(seconds=1))
            activated = loop._activate_available_inputs(
                old_deadline + timedelta(seconds=1)
            )
            self.assertEqual(expired, ())
            self.assertEqual(activated, ())
            self.assertEqual(loop._freshness_generations["input-a"], 8)
            self.assertNotIn("input-a", loop._freshness_deadlines)
            self.assertEqual(loop._availability_generations["input-a"], 12)
            self.assertNotIn("input-a", loop._availability_deadlines)
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

    def test_non_integer_progress_schema_version_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
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
            first.close()

            progress_path = workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME
            malformed = json.loads(progress_path.read_text(encoding="utf-8"))
            malformed["schema_version"] = 2.0
            progress_path.write_text(
                json.dumps(malformed, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
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


    def test_same_id_selector_replacement_after_pending_blocks_ledger_promotion(self) -> None:
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
            refresh = loop._refresh_intents_from_snapshots

            def refresh_then_replace(snapshots):
                refresh(snapshots)
                self.assertTrue(loop.dependencies.unregister("input-a"))
                loop.dependencies.register(
                    "input-a",
                    selection_ids="selection-b",
                )

            with patch.object(
                loop,
                "_refresh_intents_from_snapshots",
                side_effect=refresh_then_replace,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "changed before durable ledger publication",
                ):
                    loop.run_cycle()

            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(pending["registered_input_ids"], ["input-a"])
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            loop.close()

    def test_pending_recovery_rejects_same_id_selector_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
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
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                loop.run_cycle()

            self.assertTrue(loop.dependencies.unregister("input-a"))
            loop.dependencies.register(
                "input-a",
                selection_ids="selection-b",
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "requires exact durable dependency registry",
            ):
                loop.run_cycle()

            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            loop.close()


    def test_same_selector_reincarnation_after_pending_blocks_ledger_promotion(self) -> None:
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
            refresh = loop._refresh_intents_from_snapshots

            def refresh_then_reincarnate(snapshots):
                refresh(snapshots)
                self.assertTrue(loop.dependencies.unregister("input-a"))
                loop.dependencies.register(
                    "input-a",
                    selection_ids="selection-a",
                )

            with patch.object(
                loop,
                "_refresh_intents_from_snapshots",
                side_effect=refresh_then_reincarnate,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "changed before durable ledger publication",
                ):
                    loop.run_cycle()

            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            loop.close()

    def test_same_selector_reincarnation_after_ledger_append_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            mutated = [False]
            loop = None

            def reincarnate_after_append():
                if mutated[0]:
                    return
                mutated[0] = True
                assert loop is not None
                self.assertTrue(loop.dependencies.unregister("input-a"))
                loop.dependencies.register(
                    "input-a",
                    selection_ids="selection-a",
                )

            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                post_append_hook=reincarnate_after_append,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "focused dependency registry changed during durable ledger publication",
            ):
                loop.run_cycle()

            self.assertTrue(mutated[0])
            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "append_pending")
            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 1)

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "changed before durable ledger publication",
            ):
                loop.run_cycle()

            self.assertEqual(
                json.loads(loop.progress_path.read_text(encoding="utf-8"))["phase"],
                "append_pending",
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

    def test_pending_recovery_rejects_same_selector_reincarnation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
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
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                loop.run_cycle()

            self.assertTrue(loop.dependencies.unregister("input-a"))
            loop.dependencies.register(
                "input-a",
                selection_ids="selection-a",
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "requires exact durable dependency registry",
            ):
                loop.run_cycle()

            pending = json.loads(loop.progress_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["phase"], "pending")
            loop.close()


    def test_snapshot_capture_fails_closed_on_continuous_dependency_churn(self) -> None:
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
            loop._observe(loop.mirror_updates)
            original_deadline = loop._next_availability_deadline
            reads = [0]

            def reincarnate_after_deadline(input_id, as_of):
                deadline = original_deadline(input_id, as_of)
                reads[0] += 1
                self.assertTrue(loop.dependencies.unregister(input_id))
                loop.dependencies.register(
                    input_id,
                    selection_ids="selection-a",
                )
                return deadline

            with patch.object(
                loop,
                "_next_availability_deadline",
                side_effect=reincarnate_after_deadline,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "changed continuously during snapshot capture",
                ):
                    loop._capture_input_views(
                        ("input-a",),
                        self.START + timedelta(seconds=1),
                    )

            self.assertEqual(reads[0], 8)
            loop.close()


    def test_restart_recovers_pending_after_transient_same_selector_reincarnation(self) -> None:
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
            refresh = loop._refresh_intents_from_snapshots

            def refresh_then_reincarnate(snapshots):
                refresh(snapshots)
                self.assertTrue(loop.dependencies.unregister("input-a"))
                loop.dependencies.register(
                    "input-a",
                    selection_ids="selection-a",
                )

            with patch.object(
                loop,
                "_refresh_intents_from_snapshots",
                side_effect=refresh_then_reincarnate,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "changed before durable ledger publication",
                ):
                    loop.run_cycle()
            self.assertEqual(
                json.loads(loop.progress_path.read_text(encoding="utf-8"))["phase"],
                "pending",
            )
            loop.close()

            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )
            result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            committed = json.loads(
                resumed.progress_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed["phase"], "committed")
            self.assertEqual(
                resumed.dependencies.registry_snapshot()[0].selection_ids,
                frozenset({"selection-a"}),
            )
            resumed.close()


    def test_idempotent_register_rejects_focused_selector_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertTrue(loop.dependencies.unregister("input-a"))
            loop.dependencies.register("input-a", selection_ids="selection-b")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "inconsistent during idempotent registration",
            ):
                loop.register_input("input-a", selection_ids="selection-a")
            loop.close()

    def test_unregister_rejects_undurable_ghost_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.dependencies.register("ghost-input", selection_ids="selection-a")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "undurable ghost registration",
            ):
                loop.unregister_input("ghost-input")
            loop.close()


    def test_failed_register_publication_restores_exact_pre_mutation_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            inputs_before = loop.inputs_path.read_bytes()

            with patch.object(
                loop,
                "_persist_input_registry",
                side_effect=RuntimeError("simulated dependency publication failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated dependency publication failure",
                ):
                    loop.register_input("input-b", selection_ids="selection-b")

            self.assertEqual(loop.dependencies.input_ids, ("input-a",))
            self.assertEqual(tuple(loop._input_specs), ("input-a",))
            self.assertEqual(
                loop.dependencies.registry_snapshot()[0].selection_ids,
                frozenset({"selection-a"}),
            )
            self.assertEqual(loop.inputs_path.read_bytes(), inputs_before)
            loop.close()

    def test_failed_unregister_publication_restores_exact_pre_mutation_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            loop.register_input("input-b", selection_ids="selection-b")
            loop.register_input("input-c", selection_ids="selection-c")
            inputs_before = loop.inputs_path.read_bytes()

            with patch.object(
                loop,
                "_persist_input_registry",
                side_effect=RuntimeError("simulated dependency publication failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated dependency publication failure",
                ):
                    loop.unregister_input("input-b")

            self.assertEqual(
                loop.dependencies.input_ids,
                ("input-a", "input-b", "input-c"),
            )
            self.assertEqual(
                tuple(loop._input_specs),
                ("input-a", "input-b", "input-c"),
            )
            self.assertEqual(
                tuple(
                    dependency.selection_ids
                    for dependency in loop.dependencies.registry_snapshot()
                ),
                (
                    frozenset({"selection-a"}),
                    frozenset({"selection-b"}),
                    frozenset({"selection-c"}),
                ),
            )
            self.assertEqual(loop.inputs_path.read_bytes(), inputs_before)
            loop.close()


    def test_registry_rollback_forces_next_cycle_cache_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (self._event(selection="selection-a", sequence=1),),
                        (),
                    ],
                ),
                factory=factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            factory.calls.clear()

            with patch.object(
                loop,
                "_persist_input_registry",
                side_effect=RuntimeError("simulated dependency publication failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated dependency publication failure",
                ):
                    loop.register_input("input-b", selection_ids="selection-b")

            self.assertTrue(loop._needs_cache_rebuild)
            rebuilt = loop.run_cycle()

            self.assertEqual(rebuilt.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            self.assertFalse(loop._needs_cache_rebuild)
            loop.close()


    def test_runtime_dependency_churn_is_normalized_to_live_progress_error(self) -> None:
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
            loop._observe(loop.mirror_updates)

            with patch.object(
                loop.dependencies,
                "incremental_decision_view",
                side_effect=FocusedMirrorDependencyChurnError(
                    "focused mirror dependency changed continuously during stable read"
                ),
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "changed continuously during snapshot capture",
                ):
                    loop._capture_input_views(
                        ("input-a",),
                        self.START + timedelta(seconds=1),
                    )
            loop.close()


    def test_dependency_publication_rejects_focused_registry_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            previous = tuple(loop._input_specs.values())
            inputs_before = loop.inputs_path.read_bytes()

            self.assertTrue(loop.dependencies.unregister("input-a"))
            loop.dependencies.register("input-a", selection_ids="selection-b")

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "focused dependency registry changed before dependency publication",
            ):
                loop._persist_input_registry(expected_previous=previous)

            self.assertEqual(loop.inputs_path.read_bytes(), inputs_before)
            loop.close()


    def test_live_promotion_holds_execution_guard_across_context_and_adoption(self) -> None:
        from contextlib import contextmanager

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            event = self._event(selection="selection-a", sequence=1)
            model = PaperExecutionModelConfig(
                model_id="live-coherent-cut-test",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="live-coherent-cut-test",
                seed="live-coherent-cut",
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
            execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=PaperExecutionLedger(workspace / "paper-execution.jsonl"),
                config=model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            goal = EconomicGoalContract(
                goal_id="goal-live-coherent-cut",
                revision=1,
                bankroll_id="bankroll-live-test",
                currency="EUR",
                max_risk_of_ruin=Decimal("1"),
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            loop = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [(event,)]),
                factory=_PositiveIntentFactory(self.INTENT_CONFIG_SHA256),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
                book=book,
                authority=authority,
                paper_execution=execution,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            real_guard = execution.execution_guard
            real_context = loop._decision_context_sha256
            real_execute = execution.execute
            guard_active = {"value": False}
            observed = {"context": False, "execute": False}

            @contextmanager
            def observed_guard():
                with real_guard():
                    self.assertFalse(guard_active["value"])
                    guard_active["value"] = True
                    try:
                        yield
                    finally:
                        guard_active["value"] = False

            def observed_context():
                if guard_active["value"]:
                    observed["context"] = True
                return real_context()

            def observed_execute(**kwargs):
                self.assertTrue(guard_active["value"])
                observed["execute"] = True
                return real_execute(**kwargs)

            with (
                patch.object(execution, "execution_guard", side_effect=observed_guard),
                patch.object(
                    loop,
                    "_decision_context_sha256",
                    side_effect=observed_context,
                ),
                patch.object(execution, "execute", side_effect=observed_execute),
            ):
                result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertTrue(observed["context"])
            self.assertTrue(observed["execute"])
            self.assertFalse(guard_active["value"])
            loop.close()




    def test_pending_recovery_revalidates_paperbook_after_replay_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
                raise RuntimeError("simulated process loss after pending cursor")

            fail_after_pending.strategy_version_id = "live-test-strategy-v1"
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
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                loop.run_cycle()

            pending_before = loop.progress_path.read_bytes()
            replay = loop._refresh_intents_from_replay
            loop.intent_factory = _EmptyIntentFactory()

            def replay_then_change_portfolio(*args, **kwargs):
                result = replay(*args, **kwargs)
                loop.book.open_ticket(
                    (
                        TicketLeg(
                            "event-concurrent",
                            "market-concurrent",
                            "selection-concurrent",
                            Decimal("2.00"),
                        ),
                    ),
                    Decimal("1"),
                    reason="concurrent canonical PAPER exposure",
                    placed_at=(self.START + timedelta(seconds=1)).isoformat(),
                )
                return result

            with patch.object(
                loop,
                "_refresh_intents_from_replay",
                side_effect=replay_then_change_portfolio,
            ):
                with self.assertRaisesRegex(
                    LiveDecisionProgressError,
                    "PaperBook/runtime context changed before promotion lock",
                ):
                    loop.run_cycle()

            self.assertEqual(loop.progress_path.read_bytes(), pending_before)
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            self.assertEqual(len(loop.book.tickets), 1)
            loop.close()




    def test_live_decision_context_binds_paper_execution_model_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            book = PaperBook("1000")
            ledger = PaperExecutionLedger(workspace / "paper-execution.jsonl")
            first_model = PaperExecutionModelConfig(
                model_id="context-model",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="context-model",
                seed="context-seed-1",
                max_quote_age_ms=5_000,
                min_delay_ms=0,
                max_delay_ms=0,
                rejected_bps=0,
                partial_bps=0,
                unknown_bps=0,
                partial_fill_bps=5000,
                max_slippage_bps=0,
            )
            first_execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=first_model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
                book=book,
                paper_execution=first_execution,
            )
            first_context = first._decision_context_sha256()
            first.close()

            second_model = PaperExecutionModelConfig(
                model_id="context-model",
                model_version="2",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="context-model",
                seed="context-seed-2",
                max_quote_age_ms=5_000,
                min_delay_ms=0,
                max_delay_ms=0,
                rejected_bps=0,
                partial_bps=0,
                unknown_bps=0,
                partial_fill_bps=5000,
                max_slippage_bps=0,
            )
            second_execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=second_model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            second = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
                book=book,
                paper_execution=second_execution,
            )
            self.assertNotEqual(first_context, second._decision_context_sha256())
            second.close()

    def test_pending_restart_rejects_changed_paper_execution_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            ledger = PaperExecutionLedger(workspace / "paper-execution.jsonl")
            first_model = PaperExecutionModelConfig(
                model_id="pending-model",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="pending-model",
                seed="pending-seed-1",
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
            first_execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=ledger,
                config=first_model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )

            def fail_after_pending(input_id, snapshot):
                del input_id, snapshot
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
                book=book,
                paper_execution=first_execution,
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()
            first.close()

            resumed_book = PaperBook.load(workspace / "paper_book.json")
            second_model = PaperExecutionModelConfig(
                model_id="pending-model",
                model_version="2",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="pending-model",
                seed="pending-seed-2",
                max_quote_age_ms=5_000,
                min_delay_ms=0,
                max_delay_ms=0,
                rejected_bps=0,
                partial_bps=0,
                unknown_bps=0,
                partial_fill_bps=5000,
                max_slippage_bps=0,
            )
            second_execution = PaperExecutionAdoptionRuntime(
                book=resumed_book,
                ledger=ledger,
                config=second_model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )
            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
                book=resumed_book,
                paper_execution=second_execution,
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "runtime context changed across restart",
            ):
                resumed.run_cycle()

            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(
                JsonlDecisionLedger(
                    workspace / "decisions.jsonl"
                ).verified_records(),
                (),
            )
            resumed.close()




    def test_constructor_rejects_noncanonical_paper_execution_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            book = PaperBook("1000")
            model = PaperExecutionModelConfig(
                model_id="wrong-ledger-test",
                model_version="1",
                evidence_grade=EvidenceGrade.SYNTHETIC,
                evidence_source="wrong-ledger-test",
                seed="wrong-ledger",
                max_quote_age_ms=5_000,
                min_delay_ms=0,
                max_delay_ms=0,
                rejected_bps=0,
                partial_bps=0,
                unknown_bps=0,
                partial_fill_bps=5000,
                max_slippage_bps=0,
            )
            execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=PaperExecutionLedger(workspace / "other-execution.jsonl"),
                config=model,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=workspace / "paper_book.json",
            )

            with self.assertRaisesRegex(
                ValueError,
                "canonical live workspace execution ledger",
            ):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                    book=book,
                    paper_execution=execution,
                )


if __name__ == "__main__":
    unittest.main()
