from __future__ import annotations

import hashlib
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
from autosport.live_decision_loop import (
    LiveCycleStatus,
    LiveDecisionMode,
    LiveDecisionProgressError,
    PersistentLiveDecisionLoop,
)
from autosport.market_bus import MarketEventBus
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
)
from autosport.product_paper_decision_cycle import (
    ProductPaperDecisionCycleError,
    run_product_paper_decision_cycle,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.scientific_registry import ScientificRegistry, StrategyVersion
from autosport.storage import SQLiteMarketStore


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _EmptyIntentFactory:
    def __init__(self, strategy_version_id: str) -> None:
        self.strategy_version_id = strategy_version_id
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, input_id, snapshot):
        self.calls.append(
            (
                input_id,
                tuple(event.selection_id for event in snapshot.events),
            )
        )
        return ()


class _PositiveIntentFactory:
    def __init__(self, *, strategy_version_id: str, config_sha256: str) -> None:
        self.strategy_version_id = strategy_version_id
        self.config_sha256 = config_sha256

    def __call__(self, input_id, snapshot):
        del input_id
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
            bankroll_id="bankroll-product-cycle-test",
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
            evidence_id=f"cycle-evidence-{event.sequence}",
            observed_at=event.observed_ts,
            causal_cutoff=event.source_ts or event.observed_ts,
            reproducibility_sha256="a" * 64,
            truth=EvidenceTruth.EXACT,
            execution_feasible=True,
        )
        return (
            OpportunityIntent(
                intent_id=f"cycle-intent-{event.sequence}",
                opportunity=opportunity,
                evidence=evidence,
                risk_context=risk_context,
                signal_strength=Decimal("1"),
                strategy_id=self.strategy_version_id,
                config_sha256=self.config_sha256,
            ),
        )


class ProductPaperDecisionCycleTests(unittest.TestCase):
    START = datetime(2026, 9, 22, 5, 0, 0, tzinfo=timezone.utc)
    LOOP_ID = "product-paper-cycle-test"
    STRATEGY_ID = "product-paper-strategy-v1"
    ALT_STRATEGY_ID = "product-paper-strategy-v2"
    SOURCE_SHA = hashlib.sha256(b"product-paper-cycle-test-source").hexdigest()
    ENV_SHA = hashlib.sha256(b"product-paper-cycle-test-environment").hexdigest()
    CONFIG_SHA = hashlib.sha256(b"product-paper-cycle-test-config-v1").hexdigest()
    ALT_CONFIG_SHA = hashlib.sha256(b"product-paper-cycle-test-config-v2").hexdigest()

    @classmethod
    def _event(cls, *, sequence: int = 1) -> MarketEvent:
        timestamp = cls.START.isoformat()
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-a",
            decimal_odds=Decimal("2.00"),
            observed_ts=timestamp,
            source_id="provider-a",
            sequence=sequence,
            status="open",
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    @classmethod
    def _strategy(
        cls,
        *,
        strategy_version_id: str = STRATEGY_ID,
        config_sha256: str = CONFIG_SHA,
    ) -> StrategyVersion:
        return StrategyVersion(
            strategy_version_id=strategy_version_id,
            canonical_strategy_id="product-paper-strategy",
            source_sha256=cls.SOURCE_SHA,
            environment_sha256=cls.ENV_SHA,
            config_sha256=config_sha256,
            created_at=cls.START.isoformat(),
        )

    @staticmethod
    def _authority(*, permit_positive: bool = False) -> EconomicDecisionAuthority:
        goal = EconomicGoalContract(
            goal_id="goal-product-paper-cycle",
            revision=1,
            bankroll_id="bankroll-product-cycle-test",
            currency="EUR",
            max_risk_of_ruin=(Decimal("1") if permit_positive else Decimal("0.01")),
        )
        return EconomicDecisionAuthority(
            goal,
            PaperRiskPolicy(economic_goal=goal),
        )

    @staticmethod
    def _model(*, version: str = "1", seed: str = "cycle-seed") -> PaperExecutionModelConfig:
        return PaperExecutionModelConfig(
            model_id="product-paper-cycle-model",
            model_version=version,
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="product-paper-cycle-test",
            seed=seed,
            max_quote_age_ms=5_000,
            min_delay_ms=0,
            max_delay_ms=0,
            rejected_bps=0,
            partial_bps=0,
            unknown_bps=0,
            partial_fill_bps=5_000,
            max_slippage_bps=0,
        )

    def _workspace(
        self,
        root: Path,
        *,
        strategy: StrategyVersion | None = None,
    ) -> ScientificRegistry:
        root.mkdir(parents=True, exist_ok=True)
        PaperBook("1000").save(root / "paper_book.json")
        store = SQLiteMarketStore(root / "market.db")
        try:
            MarketEventBus(store).publish_many((self._event(),))
        finally:
            store.close()
        registry = ScientificRegistry.initialize_pristine(
            root / "scientific_registry.json"
        )
        registry.append(strategy or self._strategy())
        return registry

    def _bootstrap_input(
        self,
        root: Path,
        *,
        registry: ScientificRegistry,
        authority: EconomicDecisionAuthority,
        factory,
    ) -> None:
        loop = PersistentLiveDecisionLoop(
            root,
            loop_id=self.LOOP_ID,
            mode=LiveDecisionMode.PAPER,
            book=PaperBook.load(root / "paper_book.json"),
            authority=authority,
            intent_factory=factory,
            scientific_registry=registry,
            observation_runner=lambda _updates: None,
            max_quote_age=timedelta(seconds=5),
            clock=_Clock(self.START + timedelta(seconds=1)),
        )
        try:
            loop.register_input("input-a", selection_ids="selection-a")
        finally:
            loop.close()

    def test_consumes_current_market_without_provider_and_restart_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._workspace(root)
            authority = self._authority()
            factory = _EmptyIntentFactory(self.STRATEGY_ID)
            self._bootstrap_input(
                root,
                registry=registry,
                authority=authority,
                factory=factory,
            )

            with patch(
                "autosport.live_decision_loop.poll_open_market_store_once",
                side_effect=AssertionError("provider polling is forbidden"),
            ):
                first = run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=factory,
                    scientific_registry=registry,
                    paper_execution_config=self._model(),
                    clock=_Clock(self.START + timedelta(seconds=1)),
                )
                second = run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=factory,
                    scientific_registry=registry,
                    paper_execution_config=self._model(),
                    clock=_Clock(self.START + timedelta(seconds=1)),
                )

            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(second.status, LiveCycleStatus.NO_CHANGE)
            self.assertIn(("input-a", ("selection-a",)), factory.calls)
            records = JsonlDecisionLedger(root / "decisions.jsonl").verified_records()
            self.assertEqual(len(records), 1)

    def test_durable_paper_book_change_forces_local_redecision_without_new_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._workspace(root)
            authority = self._authority()
            factory = _EmptyIntentFactory(self.STRATEGY_ID)
            self._bootstrap_input(
                root,
                registry=registry,
                authority=authority,
                factory=factory,
            )

            first = run_product_paper_decision_cycle(
                workspace=root,
                loop_id=self.LOOP_ID,
                authority=authority,
                intent_factory=factory,
                scientific_registry=registry,
                paper_execution_config=self._model(),
                clock=_Clock(self.START + timedelta(seconds=1)),
            )
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            first_record = JsonlDecisionLedger(
                root / "decisions.jsonl"
            ).verified_records()[0]

            changed_book = PaperBook.load(root / "paper_book.json")
            changed_book.open_ticket(
                (
                    TicketLeg(
                        "external-event",
                        "external-market",
                        "external-selection",
                        Decimal("2.00"),
                    ),
                ),
                Decimal("10"),
                placed_at=(self.START + timedelta(milliseconds=500)).isoformat(),
            )
            changed_book.save(root / "paper_book.json")

            with patch(
                "autosport.live_decision_loop.poll_open_market_store_once",
                side_effect=AssertionError("provider polling is forbidden"),
            ):
                second = run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=factory,
                    scientific_registry=registry,
                    paper_execution_config=self._model(),
                    clock=_Clock(self.START + timedelta(seconds=2)),
                )

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            records = JsonlDecisionLedger(root / "decisions.jsonl").verified_records()
            self.assertEqual(len(records), 2)
            self.assertNotEqual(
                first_record.payload["decision_context_sha256"],
                records[-1].payload["decision_context_sha256"],
            )
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, Decimal("990"))

    def test_unfinished_append_rejects_strategy_drift_then_recovers_exact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._workspace(root)
            authority = self._authority()
            original_factory = _EmptyIntentFactory(self.STRATEGY_ID)
            self._bootstrap_input(
                root,
                registry=registry,
                authority=authority,
                factory=original_factory,
            )

            def crash_after_append() -> None:
                raise RuntimeError("simulated crash after decision append")

            first = PersistentLiveDecisionLoop(
                root,
                loop_id=self.LOOP_ID,
                mode=LiveDecisionMode.PAPER,
                book=PaperBook.load(root / "paper_book.json"),
                authority=authority,
                intent_factory=original_factory,
                scientific_registry=registry,
                observation_runner=lambda _updates: None,
                max_quote_age=timedelta(seconds=5),
                clock=_Clock(self.START + timedelta(seconds=1)),
                post_append_hook=crash_after_append,
            )
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash after decision append",
                ):
                    first.run_cycle()
            finally:
                first.close()

            original_record = JsonlDecisionLedger(
                root / "decisions.jsonl"
            ).verified_records()[0]
            registry.append(
                self._strategy(
                    strategy_version_id=self.ALT_STRATEGY_ID,
                    config_sha256=self.ALT_CONFIG_SHA,
                )
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "runtime context changed across restart",
            ):
                run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=_EmptyIntentFactory(self.ALT_STRATEGY_ID),
                    scientific_registry=registry,
                    paper_execution_config=self._model(),
                    clock=_Clock(self.START + timedelta(seconds=2)),
                )

            recovered = run_product_paper_decision_cycle(
                workspace=root,
                loop_id=self.LOOP_ID,
                authority=authority,
                intent_factory=original_factory,
                scientific_registry=registry,
                paper_execution_config=self._model(),
                clock=_Clock(self.START + timedelta(seconds=2)),
            )
            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(recovered.decision_id, original_record.decision_id)
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        root / "decisions.jsonl"
                    ).verified_records()
                ),
                1,
            )

    def test_unfinished_positive_decision_rejects_execution_model_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._workspace(root)
            authority = self._authority(permit_positive=True)
            factory = _PositiveIntentFactory(
                strategy_version_id=self.STRATEGY_ID,
                config_sha256=self.CONFIG_SHA,
            )
            self._bootstrap_input(
                root,
                registry=registry,
                authority=authority,
                factory=factory,
            )
            model_v1 = self._model(version="1", seed="model-v1")
            book = PaperBook.load(root / "paper_book.json")
            execution = PaperExecutionAdoptionRuntime(
                book=book,
                ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
                config=model_v1,
                max_quote_age=timedelta(seconds=5),
                paper_book_path=root / "paper_book.json",
            )

            def crash_after_append() -> None:
                raise RuntimeError("simulated crash before PAPER execution")

            first = PersistentLiveDecisionLoop(
                root,
                loop_id=self.LOOP_ID,
                mode=LiveDecisionMode.PAPER,
                book=book,
                authority=authority,
                intent_factory=factory,
                scientific_registry=registry,
                observation_runner=lambda _updates: None,
                max_quote_age=timedelta(seconds=5),
                clock=_Clock(self.START + timedelta(seconds=1)),
                post_append_hook=crash_after_append,
                paper_execution=execution,
            )
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash before PAPER execution",
                ):
                    first.run_cycle()
            finally:
                first.close()

            self.assertEqual(
                len(JsonlDecisionLedger(root / "decisions.jsonl").verified_records()),
                1,
            )
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 0)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "execution-adoption evidence changed",
            ):
                run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=factory,
                    scientific_registry=registry,
                    paper_execution_config=self._model(
                        version="2",
                        seed="model-v2",
                    ),
                    clock=_Clock(self.START + timedelta(seconds=2)),
                )

            recovered = run_product_paper_decision_cycle(
                workspace=root,
                loop_id=self.LOOP_ID,
                authority=authority,
                intent_factory=factory,
                scientific_registry=registry,
                paper_execution_config=model_v1,
                clock=_Clock(self.START + timedelta(seconds=2)),
            )
            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)
            self.assertEqual(
                len(JsonlDecisionLedger(root / "decisions.jsonl").verified_records()),
                1,
            )

    def test_rejects_noncanonical_registry_and_missing_market_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "product"
            registry = self._workspace(root)
            authority = self._authority()
            other = Path(directory) / "other"
            other.mkdir()
            other_registry = ScientificRegistry.initialize_pristine(
                other / "scientific_registry.json"
            )

            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "canonical product workspace registry",
            ):
                run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=_EmptyIntentFactory(self.STRATEGY_ID),
                    scientific_registry=other_registry,
                    paper_execution_config=self._model(),
                    clock=_Clock(self.START + timedelta(seconds=1)),
                )

            (root / "market.db").unlink()
            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "market.db is missing",
            ):
                run_product_paper_decision_cycle(
                    workspace=root,
                    loop_id=self.LOOP_ID,
                    authority=authority,
                    intent_factory=_EmptyIntentFactory(self.STRATEGY_ID),
                    scientific_registry=registry,
                    paper_execution_config=self._model(),
                    clock=_Clock(self.START + timedelta(seconds=1)),
                )


if __name__ == "__main__":
    unittest.main()
