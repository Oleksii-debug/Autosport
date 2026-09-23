from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
    GapState,
    GapStateError,
    SyncState,
    canonical_event_digest,
)
from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
from autosport.collector_service import HeadlessCollectorService
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SessionPausedError,
    SettlementResolution,
    SessionState,
)
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.event_lifecycle import CatalogEvent, CatalogPage, ContinuousEventLifecycle, EventPhase
from autosport.market_bus import MarketEventBus
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MarketMirror,
)
from autosport.learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)
from autosport.paper import PaperBook
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.providers import ProviderUnavailableError
from autosport.risk import PaperRiskPolicy
from autosport.storage import SQLiteMarketStore


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-19T21:20:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def __init__(self, page: CatalogPage) -> None:
        self.page = page
        self.catalog_calls = 0

    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        return self.page

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()


class _UnavailableSource(_Source):
    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        raise ProviderUnavailableError("provider unavailable")


class _DeltaSource(_Source):
    def __init__(self, page: CatalogPage, batches: list[tuple[CollectorDelta, ...]]) -> None:
        super().__init__(page)
        self.batches = list(batches)
        self.delta_calls = 0

    def fetch_deltas(self, checkpoint, records, max_items):
        index = min(self.delta_calls, len(self.batches) - 1)
        self.delta_calls += 1
        return self.batches[index]


class _OutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(self, record, *, as_of: str):
        self.calls += 1
        return self.resolution


class _MappedOutcomeAuthority:
    def __init__(self, resolutions: dict[str, SettlementResolution]) -> None:
        self.resolutions = dict(resolutions)

    def resolve(self, record, *, as_of: str):
        return self.resolutions.get(record.identity)


def _event(
    *,
    phase: EventPhase,
    settlement_ref: str | None = None,
    event_id: str = "event-1",
) -> CatalogEvent:
    return CatalogEvent(
        source_id="provider-a",
        sport="table_tennis",
        event_id=event_id,
        phase=phase,
        available_at="2026-09-19T21:19:00+00:00",
        scheduled_start_at="2026-09-19T21:00:00+00:00",
        settlement_ref=settlement_ref,
    )


def _market_event(*, event_id: str = "event-1") -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-19T21:19:00+00:00",
        source_id="provider-a",
        sequence=1,
        market_type=MarketType.WINNER,
        status="open",
        source_ts="2026-09-19T21:19:00+00:00",
        ingest_ts="2026-09-19T21:19:00+00:00",
        sport="table_tennis",
    )


def _collector_delta(
    *,
    delta_id: str,
    gap_state: GapState,
    sync_state: SyncState,
    revision_of: str | None = None,
    revision_number: int = 0,
) -> CollectorDelta:
    event = _market_event(event_id="provider-a:event-1")
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="provider-a",
        lawful_terms_ref="terms:provider-a:v1",
        retention_ref="retention:provider-a:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest="a" * 64,
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-09-19T21:19:00+00:00",
        collector_received_at="2026-09-19T21:19:01+00:00",
        collector_committed_at="2026-09-19T21:19:02+00:00",
        desktop_available_at="2026-09-19T21:19:03+00:00",
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=gap_state,
        sync_state=sync_state,
        gap_from_cursor=(
            "0" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None
        ),
        gap_to_cursor=(
            "1" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None
        ),
    )


def _build_coordinator(
    root: Path,
    source: _Source,
    clock: _Clock,
    *,
    outcome_authority=None,
    settlement_learning_handoff=None,
):
    market_store = SQLiteMarketStore(root / "market.db")
    lifecycle = ContinuousEventLifecycle(root / "catalog.json")
    mirror = MarketMirror()
    invalidations = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=1)
    dependencies = FocusedMirrorDependencyIndex(mirror)
    collector_store = CollectorDeltaStore(root / "collector_deltas.json")
    collector = HeadlessCollectorService(
        delta_store=collector_store,
        lifecycle=lifecycle,
        source=source,
        state_path=root / "collector_state.json",
        run_id="collector-run-1",
        clock=clock,
        sleep=lambda _: None,
    )
    desktop = DesktopDeltaConsumer(
        collector_store,
        DesktopDeltaCheckpointStore(root / "desktop_acks.json"),
        resolve_event=lambda delta: _market_event(event_id=delta.event_id),
        apply_event=lambda delta, event: DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=canonical_event_digest(event),
            receipt_id=f"test-receipt:{delta.delta_id}",
            applied_at=clock(),
        ),
        lookup_application_receipt=lambda delta: None,
    )
    coordinator = ContinuousSessionCoordinator(
        workspace=root,
        collector=collector,
        lifecycle=lifecycle,
        market_store=market_store,
        desktop_consumer=desktop,
        invalidation_buffer=invalidations,
        dependency_index=dependencies,
        outcome_authority=outcome_authority,
        settlement_learning_handoff=settlement_learning_handoff,
        session_id="session-1",
        clock=clock,
        initial_bankroll="100",
    )
    return coordinator, market_store, lifecycle, mirror, invalidations, dependencies


class ContinuousSessionCoordinatorTests(unittest.TestCase):
    def test_tick_registers_new_event_and_persists_checkpoint_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(_event(phase=EventPhase.PRE_MATCH),),
            )
            source = _Source(page)
            coordinator, store, _lifecycle, _mirror, _invalidations, dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                store.append(_market_event(event_id="provider-a:event-1"))
                result = coordinator.tick()
                self.assertEqual(result.cycle_index, 1)
                self.assertEqual(result.registered_input_ids, ("catalog:provider-a:event-1",))
                self.assertEqual(dependencies.input_ids, ("catalog:provider-a:event-1",))
                self.assertEqual(coordinator.status().cycles_completed, 1)

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock
                )
                try:
                    self.assertEqual(restarted.session_id, "session-1")
                    self.assertEqual(restarted.status().cycles_completed, 1)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_mixed_pre_match_live_and_late_events_share_one_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(
                        _event(phase=EventPhase.PRE_MATCH, event_id="event-1"),
                        _event(phase=EventPhase.LIVE, event_id="event-2"),
                    ),
                )
            )
            coordinator, store, lifecycle, _mirror, _invalidations, dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                for event_id in ("event-1", "event-2", "event-3"):
                    store.append(_market_event(event_id=f"provider-a:{event_id}"))

                first = coordinator.tick()
                self.assertIn("catalog:provider-a:event-1", first.registered_input_ids)
                self.assertIn("catalog:provider-a:event-2", first.registered_input_ids)
                self.assertEqual(lifecycle.get("provider-a:event-2").phase, EventPhase.LIVE)

                source.page = CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-2",
                    position=2,
                    events=(
                        _event(phase=EventPhase.LIVE, event_id="event-1"),
                        _event(phase=EventPhase.LIVE, event_id="event-2"),
                        _event(phase=EventPhase.PRE_MATCH, event_id="event-3"),
                    ),
                )
                second = coordinator.tick()
                self.assertIn("catalog:provider-a:event-3", second.registered_input_ids)
                records = lifecycle.records()
                self.assertEqual(len(records), 3)
                self.assertEqual(len({item.identity for item in records}), 3)
                self.assertEqual(lifecycle.get("provider-a:event-1").phase, EventPhase.LIVE)

                restarted, restarted_store, restarted_lifecycle, *_rest = _build_coordinator(
                    root, source, clock
                )
                try:
                    third = restarted.tick()
                    self.assertEqual(restarted.session_id, "session-1")
                    self.assertEqual(len(restarted_lifecycle.records()), 3)
                    self.assertEqual(
                        len({item.identity for item in restarted_lifecycle.records()}),
                        3,
                    )
                    self.assertFalse(third.source_provider_unavailable)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_provider_unavailable_is_reported_without_fresh_session_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _UnavailableSource(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                result = coordinator.tick()
                self.assertTrue(result.source_provider_unavailable)
                self.assertEqual(result.cycle_index, 0)
                self.assertIsNone(result.last_success_at)
                self.assertEqual(result.committed_delta_ids, ())
                self.assertEqual(result.delivered_delta_ids, ())

                status = coordinator.status()
                self.assertEqual(status.cycles_completed, 0)
                self.assertTrue(status.source_provider_unavailable)
                self.assertEqual(status.source_last_error_code, "ProviderUnavailableError")
                self.assertIsNone(status.source_last_success_at)
            finally:
                store.close()

    def test_pause_is_durable_and_resume_continues_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, _lifecycle, _mirror, _invalidations, _dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                coordinator.pause()
                self.assertEqual(coordinator.status().state, SessionState.PAUSED)
                with self.assertRaises(SessionPausedError):
                    coordinator.tick()

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock
                )
                try:
                    self.assertEqual(restarted.status().state, SessionState.PAUSED)
                    restarted.resume()
                    self.assertEqual(restarted.status().state, SessionState.RUNNING)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_completed_event_waits_for_external_outcome_and_settles_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:1",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )

            book = PaperBook("100")
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book.open_ticket((leg,), Decimal("10"), placed_at="2026-09-19T21:19:30+00:00")
            book.save(root / "paper_book.json")

            quote_key = leg.quote_key
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:1",
                quote_outcomes={quote_key: "win"},
                evidence_id="outcome-1",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)

            coordinator, store, _lifecycle, _mirror, _invalidations, _dependencies = _build_coordinator(
                root, source, clock, outcome_authority=authority
            )
            try:
                first = coordinator.tick()
                self.assertEqual(first.settled_ticket_ids, tuple(book.tickets))
                settled = PaperBook.load(root / "paper_book.json")
                self.assertEqual(settled.balance, Decimal("110"))

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock, outcome_authority=authority
                )
                try:
                    second = restarted.tick()
                    self.assertEqual(second.settled_ticket_ids, ())
                    settled_again = PaperBook.load(root / "paper_book.json")
                    self.assertEqual(settled_again.balance, Decimal("110"))
                    self.assertEqual(authority.calls, 2)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_settled_bound_ticket_reaches_agent_loop_once_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:learning-1",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-learning-1",
                    position=1,
                    events=(event,),
                )
            )

            goal = EconomicGoalContract(
                goal_id="paper-learning-goal",
                revision=1,
                bankroll_id="paper-bankroll",
                currency="USD",
            )
            risk = PaperRiskPolicy(economic_goal=goal)
            book = PaperBook("100")
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
            )
            book.save(root / "paper_book.json")

            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            decision_action = "OPEN_PAPER_VALUE_TICKET"

            identity = EnvironmentIdentity(
                source_id="paper-learning-source",
                config_id="paper-learning-config",
                data_id="paper-learning-data",
                protocol_id="paper-learning-protocol",
                cutoff_ts="2026-09-19T21:20:00+00:00",
                seed=17,
            )
            environment = CausalLearningEnvironment(
                identity,
                episode_key="paper-learning-episode",
                policy_id="paper-learning-policy",
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
            )
            baseline = environment.checkpoint()
            runtime = AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="paper-learning-loop",
                environment_checkpoint=baseline,
                policy_id=environment.episode.policy_id,
                economic_goal_fingerprint=provenance_for(goal).contract_sha256,
                risk_fingerprint=risk.provenance_sha256,
                source_sha256="a" * 64,
                config_sha256="b" * 64,
                at="2026-09-19T21:18:59+00:00",
            )
            observation = Observation(
                environment_id=environment.environment_id,
                observed_at="2026-09-19T21:19:00+00:00",
                available_at="2026-09-19T21:19:01+00:00",
                evidence=(("market_state", "paper-learning-snapshot"),),
            )
            decision = DecisionRecord(
                replay_run_id="paper-learning-run",
                agent="paper-learning-fixture",
                observed_ts=observation.observed_at,
                action=decision_action,
                payload={
                    "ticket_id": ticket.ticket_id,
                    "quote_key": leg.quote_key,
                    "stake": str(ticket.stake),
                },
                context_hash=observation.observation_id,
                decision_id="paper-learning-decision-1",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(
                decision,
                EconomicDecisionAuthority(goal, risk),
            )
            runtime.begin_observation(
                observation,
                environment_identity=environment.identity,
                at="2026-09-19T21:19:01+00:00",
            )
            for phase in (
                AgentLoopPhase.OBSERVE,
                AgentLoopPhase.ASSESS,
                AgentLoopPhase.PLAN,
                AgentLoopPhase.DECIDE,
            ):
                runtime.advance(expected=phase, at="2026-09-19T21:19:02+00:00")
            action = environment.act(
                observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-19T21:19:05+00:00",
                parameters=(
                    ("economic_decision_id", decision.decision_id),
                    ("paper_ticket_id", ticket.ticket_id),
                ),
            )
            runtime.commit_action(
                action,
                episode=environment.episode,
                observation=observation,
                effect_state=ExternalEffectState.PAPER_ONLY,
                at="2026-09-19T21:19:05+00:00",
            )

            bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=ledger,
                agent_loop=runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )

            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:learning-1",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="paper-learning-outcome-1",
                evidence_sha256="c" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
                settlement_learning_handoff=bridge,
            )
            try:
                first = coordinator.tick()
                self.assertEqual(first.settled_ticket_ids, (ticket.ticket_id,))
                self.assertEqual(PaperBook.load(root / "paper_book.json").balance, Decimal("110"))
                first_snapshot = runtime.snapshot()
                self.assertIs(first_snapshot.phase, AgentLoopPhase.EVALUATE)
                self.assertIsNotNone(first_snapshot.transition_id)
                self.assertIsNotNone(first_snapshot.reward_id)
                bridge_state = json.loads(
                    (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
                )
                self.assertIsNotNone(
                    bridge_state["bindings"][ticket.ticket_id]["settlement_intent"]
                )

                raw_loop = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
                self.assertEqual(len(raw_loop["resolutions"]), 1)
                self.assertEqual(raw_loop["resolutions"][0]["reward_value"], "10.00")
                state_before_restart = first_snapshot.state_sha256

                restarted_runtime = AgentLoopRuntime(root / "agent-loop.json")
                restarted_bridge = PaperSettlementLearningBridge(
                    root / "paper_learning_bridge.json",
                    paper_book_path=root / "paper_book.json",
                    decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                    agent_loop=restarted_runtime,
                    economic_goal=goal,
                    risk_policy=risk,
                )
                restarted, restarted_store, *_ = _build_coordinator(
                    root,
                    source,
                    clock,
                    outcome_authority=authority,
                    settlement_learning_handoff=restarted_bridge,
                )
                try:
                    second = restarted.tick()
                    self.assertEqual(second.settled_ticket_ids, ())
                    self.assertEqual(
                        restarted_runtime.snapshot().state_sha256,
                        state_before_restart,
                    )
                    raw_after = json.loads(
                        (root / "agent-loop.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(len(raw_after["resolutions"]), 1)
                    self.assertEqual(
                        restarted_bridge.next_checkpoint(ticket.ticket_id).last_transition_id,
                        first_snapshot.transition_id,
                    )
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_same_tick_conflicting_settlement_evidence_fails_before_book_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            first_event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:1",
                event_id="event-1",
            )
            second_event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:2",
                event_id="event-2",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(first_event, second_event),
                )
            )

            book = PaperBook("100")
            first_leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            second_leg = TicketLeg(
                event_id="event-2",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book.open_ticket(
                (first_leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
            )
            book.open_ticket(
                (second_leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
            )
            book.save(root / "paper_book.json")
            before = PaperBook.load(root / "paper_book.json")
            before_statuses = {
                ticket_id: ticket.status.value
                for ticket_id, ticket in before.tickets.items()
            }

            reused_evidence_id = "outcome-conflict-same-tick"
            resolutions = {
                first_event.identity: SettlementResolution(
                    event_identity=first_event.identity,
                    settlement_ref="provider-result:1",
                    quote_outcomes={first_leg.quote_key: "win"},
                    evidence_id=reused_evidence_id,
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                ),
                second_event.identity: SettlementResolution(
                    event_identity=second_event.identity,
                    settlement_ref="provider-result:2",
                    quote_outcomes={second_leg.quote_key: "loss"},
                    evidence_id=reused_evidence_id,
                    evidence_sha256="1" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                ),
            }
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=_MappedOutcomeAuthority(resolutions),
            )
            try:
                with self.assertRaises(ContinuousSessionError):
                    coordinator.tick()

                after = PaperBook.load(root / "paper_book.json")
                self.assertEqual(after.balance, before.balance)
                self.assertEqual(
                    {
                        ticket_id: ticket.status.value
                        for ticket_id, ticket in after.tickets.items()
                    },
                    before_statuses,
                )
                self.assertEqual(coordinator.status().cycles_completed, 0)
            finally:
                store.close()

    def test_conflicting_settlement_evidence_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:1",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:1",
                quote_outcomes={"provider-a:event-1:winner:home": "win"},
                evidence_id="outcome-conflict",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, _lifecycle, _mirror, _invalidations, _dependencies = _build_coordinator(
                root, source, clock, outcome_authority=authority
            )
            try:
                coordinator.tick()
                authority.resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:1",
                    quote_outcomes=dict(resolution.quote_outcomes),
                    evidence_id="outcome-conflict",
                    evidence_sha256="1" * 64,
                    available_at=resolution.available_at,
                )
                with self.assertRaises(ContinuousSessionError):
                    coordinator.tick()
            finally:
                store.close()

    def test_unresolved_gap_is_durable_in_status_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            detected = _collector_delta(
                delta_id="gap-detected",
                gap_state=GapState.DETECTED,
                sync_state=SyncState.GAP_DETECTED,
            )
            source = _DeltaSource(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.LIVE),),
                ),
                [(detected,)],
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaises(GapStateError):
                    coordinator.tick()

                status = coordinator.status()
                self.assertEqual(status.source_gap_state, GapState.DETECTED.value)
                self.assertEqual(status.source_sync_state, SyncState.GAP_DETECTED.value)
                self.assertEqual(status.source_state_delta_id, "gap-detected")
                self.assertEqual(
                    status.source_unresolved_gap_delta_ids,
                    ("gap-detected",),
                )

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock
                )
                try:
                    restarted_status = restarted.status()
                    self.assertEqual(
                        restarted_status.source_gap_state,
                        GapState.DETECTED.value,
                    )
                    self.assertEqual(
                        restarted_status.source_sync_state,
                        SyncState.GAP_DETECTED.value,
                    )
                    self.assertEqual(
                        restarted_status.source_unresolved_gap_delta_ids,
                        ("gap-detected",),
                    )
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_gap_recovery_and_no_new_delta_keep_truthful_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            detected = _collector_delta(
                delta_id="gap-detected",
                gap_state=GapState.DETECTED,
                sync_state=SyncState.GAP_DETECTED,
            )
            recovered = _collector_delta(
                delta_id="gap-recovered",
                gap_state=GapState.RECOVERED,
                sync_state=SyncState.RECOVERED,
                revision_of="gap-detected",
                revision_number=1,
            )
            source = _DeltaSource(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.LIVE),),
                ),
                [(detected,), (recovered,), ()],
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaises(GapStateError):
                    coordinator.tick()

                recovered_result = coordinator.tick()
                self.assertEqual(
                    recovered_result.source_gap_states,
                    (GapState.RECOVERED.value,),
                )
                self.assertEqual(
                    recovered_result.source_sync_states,
                    (SyncState.RECOVERED.value,),
                )
                recovered_status = coordinator.status()
                self.assertEqual(
                    recovered_status.source_gap_state,
                    GapState.RECOVERED.value,
                )
                self.assertEqual(
                    recovered_status.source_sync_state,
                    SyncState.RECOVERED.value,
                )
                self.assertEqual(
                    recovered_status.source_state_delta_id,
                    "gap-recovered",
                )
                self.assertEqual(
                    recovered_status.source_unresolved_gap_delta_ids,
                    (),
                )

                no_new_delta = coordinator.tick()
                self.assertEqual(
                    no_new_delta.source_gap_states,
                    (GapState.RECOVERED.value,),
                )
                self.assertEqual(
                    no_new_delta.source_sync_states,
                    (SyncState.RECOVERED.value,),
                )
                self.assertFalse(
                    coordinator.status().source_state_projection_backlog
                )
            finally:
                store.close()

    def test_invalidation_overflow_becomes_full_refresh_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, _lifecycle, mirror, invalidations, dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                dependencies.register(
                    "catalog:provider-a:event-1",
                    source_ids="provider-a",
                    sports="table_tennis",
                    event_ids="event-1",
                )
                event = _market_event()
                invalidations.accept_persisted(event)
                second = MarketEvent(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="home",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-19T21:19:01+00:00",
                    source_id="provider-a",
                    sequence=2,
                    market_type=MarketType.WINNER,
                    status="open",
                    source_ts="2026-09-19T21:19:01+00:00",
                    ingest_ts="2026-09-19T21:19:01+00:00",
                    sport="table_tennis",
                )
                invalidations.accept_persisted(second)
                status = coordinator.status()
                self.assertTrue(status.invalidation_full_refresh_required)
                self.assertEqual(status.invalidation_pending_count, 0)
                result = coordinator._drain_invalidations()
                self.assertEqual(result[1], True)
                self.assertFalse(result[2])
                self.assertEqual(result[0], ("catalog:provider-a:event-1",))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
