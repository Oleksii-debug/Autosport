from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .agents import AgentContext, AgentOrchestrator
from .dataset import ReplayDataset
from .decision_ledger import JsonlDecisionLedger, VerifiedDecisionLedgerSnapshot
from .domain import MarketEvent
from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import EconomicGoalProvenance, provenance_for
from .economic_goal_store import EconomicGoalStore
from .evaluation import EvaluationSummary, evaluate
from .ingestion import IngestionEngine, IngestionStats
from .ingestion_health import IngestionPolicy, SourceHealthState, SourceHealthStore
from .integrity import ensure_durable_file, sha256_file
from .market_bus import MarketEventBus
from .outcome_trust import (
    OutcomeLineageBinding,
    outcome_lineage_binding_from_dataset,
    outcome_lineage_payload,
)
from .paper import PaperBook
from .portfolio import PortfolioEngine, PortfolioReport
from .price_truth import market_price_truth_from_events
from .providers import MarketProvider
from .recovery import transaction_history_requires_recovery
from .replay import ReplayEngine, ReplayRun
from .research_strategy import ResearchStrategyPlan
from .risk import PaperRiskPolicy
from .run_registry import MixedStrategyWorkspaceError, RunRegistry, UnresolvedExperimentError
from .run_transaction import RunTransaction
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore
from .strategies import (
    StrategySpec,
    build_strategy_agents,
    experiment_strategy_id,
    validate_strategy_configuration,
)
from .workspace_lock import WorkspaceEconomicLock


def _bind_canonical_settlement_engine(method):
    """Inject the import-time exact SettlementEngine through a closure-owned seam."""

    canonical_engine_type = SettlementEngine

    def guarded(self, *args, **kwargs):
        if "_settlement_engine_type" in kwargs:
            raise TypeError("settlement engine origin is internal product authority")
        kwargs["_settlement_engine_type"] = canonical_engine_type
        return method(self, *args, **kwargs)

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = method.__annotations__
    return guarded


def _seal_settlement_consumer_entry(method):
    """Return an immutable built-in descriptor with a closure-owned dispatch target."""

    def resolve(instance):
        return method.__get__(instance, type(instance))

    def reject_set(_instance, _value) -> None:
        raise TypeError("canonical settlement consumer entry binding is immutable")

    def reject_delete(_instance) -> None:
        raise TypeError("canonical settlement consumer entry binding is immutable")

    return property(resolve, reject_set, reject_delete, method.__doc__)


def _build_settlement_consumer_class_guard(name: str):
    """Keep type-level replacement from bypassing the installed data descriptor."""

    class SettlementConsumerClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            binding = instance.__dict__[name]
            return binding.__get__(None, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError("canonical settlement consumer entry binding is immutable")

        def __delete__(self, _instance) -> None:
            raise TypeError("canonical settlement consumer entry binding is immutable")

    return SettlementConsumerClassGuard()


class _AutosportSessionMeta(type):
    """Seal the trusted settlement consumer entry inside the process TCB."""

    def __setattr__(cls, name: str, value: object) -> None:
        if (
            cls.__dict__.get("_settlement_consumer_bindings_sealed", False)
            and name in {"_run_dataset_locked", "_settlement_consumer_bindings_sealed"}
        ):
            raise TypeError("canonical settlement consumer entry binding is immutable")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if (
            cls.__dict__.get("_settlement_consumer_bindings_sealed", False)
            and name in {"_run_dataset_locked", "_settlement_consumer_bindings_sealed"}
        ):
            raise TypeError("canonical settlement consumer entry binding is immutable")
        super().__delattr__(name)


@dataclass(frozen=True, slots=True)
class SessionResult:
    replay: ReplayRun
    settled_ticket_ids: tuple[str, ...]
    balance: Decimal
    evaluation: EvaluationSummary
    portfolio: PortfolioReport
    experiment_key: str
    result_path: str


@dataclass(frozen=True, slots=True)
class ObservationResult:
    stats: IngestionStats
    health: SourceHealthState
    current_quotes: tuple[MarketEvent, ...]


class AutosportSession(metaclass=_AutosportSessionMeta):
    """V1 runtime for causal replay, paper simulation and read-only market observation."""

    _settlement_consumer_bindings_sealed = False

    def __init__(
        self,
        workspace: str | Path,
        initial_bankroll: Decimal | str = "10000",
        strategy_id: str = "baseline-v1",
        research_plan: ResearchStrategyPlan | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Bind experiment identity to an actual runtime implementation and, for
        # research replay, the exact canonical plan hash before durable state opens.
        self.strategy: StrategySpec = validate_strategy_configuration(strategy_id, research_plan)
        self.research_plan = research_plan
        self.strategy_id = experiment_strategy_id(strategy_id, research_plan)
        self.store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            self.source_health = SourceHealthStore(self.workspace / "source_health.json")
            self.book_path = self.workspace / "paper_book.json"
            self.book = PaperBook.load(self.book_path) if self.book_path.exists() else PaperBook(initial_bankroll)
            self.ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
            self.registry = RunRegistry.initialize_pristine(self.workspace / "run_registry.json")
            self.portfolio_engine = PortfolioEngine()
        except BaseException as initialization_error:
            # SQLiteMarketStore owns an OS file handle after construction. If any
            # later durable/session component fails to initialize, the partially
            # constructed session must make a best-effort close without replacing
            # the primary initialization failure with a secondary cleanup failure.
            try:
                self.store.close()
            except BaseException as cleanup_error:
                initialization_error.add_note(
                    "SQLiteMarketStore cleanup also failed during session initialization: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    def _runtime(
        self,
        run_id: str,
        *,
        book: PaperBook | None = None,
        ledger: JsonlDecisionLedger | None = None,
        risk_policy: PaperRiskPolicy | None = None,
    ) -> AgentOrchestrator:
        context = AgentContext(
            book or self.book,
            replay_run_id=run_id,
            decision_ledger=ledger or self.ledger,
        )
        return AgentOrchestrator(
            build_strategy_agents(
                self.strategy.strategy_id,
                research_plan=self.research_plan,
                risk_policy=risk_policy,
            ),
            context,
        )

    def observe_provider_once(
        self,
        provider: MarketProvider,
        *,
        max_items: int = 1000,
        policy: IngestionPolicy | None = None,
    ) -> ObservationResult:
        """Acquire one bounded read-only provider snapshot into the canonical Market Store."""
        engine = IngestionEngine(
            MarketEventBus(self.store),
            policy=policy,
            health_store=self.source_health,
        )
        stats = engine.poll_once(provider, max_items=max_items)
        source_id = stats.source_id
        current = tuple(
            sorted(
                (event for event in self.store.current().values() if event.source_id == source_id),
                key=lambda event: (event.event_id, event.market_id, event.selection_id),
            )
        )
        return ObservationResult(stats, self.source_health.get(source_id), current)

    def _capture_economic_authority(
        self,
    ) -> tuple[EconomicGoalContract | None, PaperRiskPolicy]:
        store = EconomicGoalStore(self.workspace)
        goal = store.load() if store.path.exists() else None
        policy = PaperRiskPolicy(economic_goal=goal)
        return goal, policy

    def _runtime_strategy_identity(
        self,
        goal: EconomicGoalContract | None,
        policy: PaperRiskPolicy,
    ) -> str:
        if goal is None:
            return self.strategy_id
        goal_provenance = provenance_for(goal)
        return (
            f"{self.strategy_id}::economic:"
            f"{goal_provenance.contract_sha256}:{policy.provenance_sha256}"
        )

    @staticmethod
    def _economic_runtime_provenance(
        goal: EconomicGoalContract | None,
        policy: PaperRiskPolicy,
    ) -> tuple[EconomicGoalProvenance | None, dict[str, object] | None]:
        if goal is None:
            return None, None
        goal_provenance = provenance_for(goal)
        return (
            goal_provenance,
            {**policy.provenance_payload(), "sha256": policy.provenance_sha256},
        )

    def run_dataset(self, dataset: ReplayDataset, speed: float = 0.0, allow_repeat: bool = False) -> SessionResult:
        with WorkspaceEconomicLock(self.workspace):
            economic_goal, risk_policy = self._capture_economic_authority()
            prior_strategy_ids = self.registry.strategy_ids()
            if economic_goal is None and any(
                value.startswith(self.strategy_id + "::economic:")
                for value in prior_strategy_ids
            ):
                raise ValueError(
                    "persisted EconomicGoal authority is missing for a workspace "
                    "with economic-goal runtime history"
                )
            if economic_goal is not None and self.strategy.strategy_id == "baseline-v1":
                raise ValueError(
                    "baseline-v1 does not have proven EconomicGoal-aware sizing semantics"
                )
            runtime_strategy_id = self._runtime_strategy_identity(
                economic_goal,
                risk_policy,
            )
            # Recover the exact checksum-bound schema-v2 outcome chain and reject a
            # previously accepted restart/fork before PaperBook/ledger mutation.
            # The same binding is then persisted on the canonical RunRegistry item.
            outcome_lineage = outcome_lineage_binding_from_dataset(dataset)
            if outcome_lineage is not None:
                self.registry.assert_outcome_lineage_compatible(outcome_lineage)
            # Load and validate the exact causal market bytes, including schema-v3
            # event-level sport scope, before registry/PaperBook/economic mutation.
            # Reuse this verified snapshot for the whole run so a later path swap
            # cannot change sport/quote identity after preflight.
            market_events = dataset.load_market_events()
            verified_sports = dataset._assert_sport_scope(market_events)
            # Research-plan market binding is deterministic from the sealed causal
            # stream, so reject a stale/forged plan before registry/PaperBook mutation.
            if self.research_plan is not None:
                self.research_plan.preflight(market_events)
            return self._run_dataset_locked(
                dataset,
                market_events=market_events,
                verified_sports=verified_sports,
                speed=speed,
                allow_repeat=allow_repeat,
                outcome_lineage=outcome_lineage,
                economic_goal=economic_goal,
                risk_policy=risk_policy,
                runtime_strategy_id=runtime_strategy_id,
            )

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_engine
    def _run_dataset_locked(
        self,
        dataset: ReplayDataset,
        *,
        market_events: list[MarketEvent],
        verified_sports: tuple[str, ...],
        speed: float = 0.0,
        allow_repeat: bool = False,
        outcome_lineage: OutcomeLineageBinding | None = None,
        economic_goal: EconomicGoalContract | None = None,
        risk_policy: PaperRiskPolicy | None = None,
        runtime_strategy_id: str | None = None,
        _settlement_engine_type: type[SettlementEngine],
    ) -> SessionResult:
        if SettlementEngine is not _settlement_engine_type:
            raise RuntimeError("settlement engine constructor origin changed")
        if risk_policy is None:
            risk_policy = PaperRiskPolicy(economic_goal=economic_goal)
        runtime_strategy_id = runtime_strategy_id or self._runtime_strategy_identity(
            economic_goal,
            risk_policy,
        )
        base_ledger_snapshot = self._ensure_canonical_economic_base()
        base_book_hash = sha256_file(self.book_path)
        base_ledger_hash = base_ledger_snapshot.sha256

        run_id = str(uuid.uuid4())
        experiment_key = self.registry.begin(
            dataset.market_sha256,
            dataset.results_sha256,
            runtime_strategy_id,
            run_id,
            allow_repeat=allow_repeat,
            base_paper_book_sha256=base_book_hash,
            base_decision_ledger_sha256=base_ledger_hash,
            outcome_lineage=outcome_lineage,
        )
        try:
            transaction = RunTransaction.start(
                self.workspace,
                run_id=run_id,
                experiment_key=experiment_key,
                market_sha256=dataset.market_sha256,
                results_sha256=dataset.results_sha256,
                strategy_id=runtime_strategy_id,
                base_paper_book_sha256=base_book_hash,
                base_decision_ledger_sha256=base_ledger_hash,
            )
        except Exception:
            # No economic mutation occurs before the transaction object exists.
            # Do not turn an ordinary start failure into an in-progress workspace
            # that falsely requires crash recovery.
            self.registry.abort_uncommitted(
                experiment_key,
                reason="transaction start failed before durable precommit",
                paper_book_sha256=sha256_file(self.book_path),
                decision_ledger_sha256=self.ledger.verified_snapshot().sha256,
            )
            raise

        try:
            working_book = PaperBook.load(self.book_path)
            staged_ledger = JsonlDecisionLedger(transaction.run_ledger_path)
            orchestrator = self._runtime(
                run_id,
                book=working_book,
                ledger=staged_ledger,
                risk_policy=risk_policy,
            )
            engine = ReplayEngine(market_events)

            def consume(event) -> None:
                self.store.append(event)
                orchestrator.on_market_event(event)

            replay = engine.run(consume, speed=speed, run_id=run_id)
            # Fail closed on any planned causal decision that did not execute before
            # loading sealed outcome facts into settlement.
            orchestrator.finalize_replay()
            settlement = _settlement_engine_type()
            if type(settlement) is not _settlement_engine_type:
                raise RuntimeError(
                    "settlement engine constructor returned non-canonical type"
                )
            settlement.record(dataset.load_results_after_replay())
            settled = tuple(settlement.settle_ready(working_book))
            evaluation = evaluate(working_book)
            portfolio = self.portfolio_engine.analyse(list(working_book.tickets.values()))
            destination = self.workspace / f"run-{replay.run_id}.json"
            result = SessionResult(
                replay,
                settled,
                working_book.balance,
                evaluation,
                portfolio,
                experiment_key,
                str(destination),
            )

            transaction.stage_outputs(working_book, self.ledger.path)
        except Exception:
            # A normal strategy/replay/staging failure before PRECOMMIT is not an
            # ambiguous crash. Recovery must first prove canonical PaperBook and
            # Decision Ledger are still exactly BASE; only then may the registry
            # record an explicit aborted retry.
            recovery = RunTransaction.recover(
                self.workspace,
                run_id=run_id,
                registry_item=self.registry.get(experiment_key),
                experiment_key=experiment_key,
            )
            if recovery.disposition != "aborted_uncommitted":
                raise RuntimeError(
                    "precommit failure unexpectedly crossed the durable commit boundary"
                )
            self.registry.abort_uncommitted(
                experiment_key,
                reason=(
                    "strategy/replay/staging failed before durable precommit; "
                    "canonical economic state remained BASE"
                ),
                paper_book_sha256=sha256_file(self.book_path),
                decision_ledger_sha256=self.ledger.verified_snapshot().sha256,
            )
            raise

        # After durable PRECOMMIT begins, failures deliberately remain unresolved.
        # Recovery owns deciding whether BASE or NEW is canonical and must never
        # downgrade an uncertain commit into an ordinary strategy rejection.
        summary = transaction.precommit(
            self._run_summary_payload(
                dataset,
                result,
                market_events=market_events,
                verified_sports=verified_sports,
                outcome_lineage=outcome_lineage,
                economic_goal=economic_goal,
                risk_policy=risk_policy,
                runtime_strategy_id=runtime_strategy_id,
            )
        )
        transaction.commit()

        # From this point canonical PaperBook is NEW. Keep in-memory state aligned
        # before touching the registry so finally/close can never rewrite OLD state.
        self.book = working_book
        self.registry.complete(
            experiment_key,
            str(destination),
            paper_book_sha256=str(summary["paper_book_sha256"]),
            decision_ledger_sha256=str(summary["decision_ledger_sha256"]),
        )
        transaction.mark_registry_completed()
        return result

    def _ensure_canonical_economic_base(self) -> VerifiedDecisionLedgerSnapshot:
        if self.registry.in_progress():
            raise UnresolvedExperimentError(
                "Workspace has an unresolved economic run; repair it before starting another paper experiment."
            )
        if transaction_history_requires_recovery(self.workspace):
            raise UnresolvedExperimentError(
                "Workspace has unresolved transaction history; repair it before starting another paper experiment."
            )
        prior_strategy_ids = self.registry.strategy_ids()
        economic_prefix = self.strategy_id + "::economic:"
        foreign_strategy_ids = tuple(
            value
            for value in prior_strategy_ids
            if value != self.strategy_id and not value.startswith(economic_prefix)
        )
        if foreign_strategy_ids:
            raise MixedStrategyWorkspaceError(
                "Workspace already contains economic runs for another strategy identity "
                f"({', '.join(foreign_strategy_ids)}). Use a separate workspace per strategy/plan "
                "so PaperBook, decision-ledger, portfolio and evaluation evidence cannot be mixed."
            )
        # Validate the append-only audit evidence under the economic lock and bind
        # its semantic proof to the exact bytes whose hash becomes transaction BASE.
        ensure_durable_file(self.ledger.path)
        ledger_snapshot = self.ledger.verified_snapshot()
        # Session construction happens outside WorkspaceEconomicLock. Another process
        # or session may therefore have committed a newer canonical PaperBook while
        # this instance was waiting for the lock. Refresh under the lock instead of
        # writing a stale in-memory snapshot over newer economic state.
        if self.book_path.exists():
            self.book = PaperBook.load(self.book_path)
        else:
            self.book.save(self.book_path)
        return ledger_snapshot

    def _run_summary_payload(
        self,
        dataset: ReplayDataset,
        result: SessionResult,
        *,
        market_events: list[MarketEvent] | None = None,
        verified_sports: tuple[str, ...] | None = None,
        outcome_lineage: OutcomeLineageBinding | None = None,
        economic_goal: EconomicGoalContract | None = None,
        risk_policy: PaperRiskPolicy | None = None,
        runtime_strategy_id: str | None = None,
    ) -> dict:
        # Runtime callers pass the already-verified snapshot to preserve the run-level
        # TOCTOU boundary. Direct/reporting callers retain backward compatibility and
        # may load once here when no snapshot was supplied.
        if market_events is None:
            market_events = dataset.load_market_events()
        if verified_sports is None:
            if dataset.schema_version >= 3 and hasattr(dataset, "_assert_sport_scope"):
                verified_sports = dataset._assert_sport_scope(market_events)
            else:
                verified_sports = ()
        market_price_truth = market_price_truth_from_events(market_events)
        sport_identity_proven = dataset.schema_version >= 3 and bool(verified_sports)
        if risk_policy is None:
            risk_policy = PaperRiskPolicy(economic_goal=economic_goal)
        runtime_strategy_id = runtime_strategy_id or self._runtime_strategy_identity(
            economic_goal,
            risk_policy,
        )
        goal_provenance, policy_provenance = self._economic_runtime_provenance(
            economic_goal,
            risk_policy,
        )

        # Preserve exact causal observation membership inside the existing
        # transaction-bound run summary. This is not a second authority: the
        # same market_events snapshot already drives ReplayEngine and
        # replay_dataset_hash. Campaign evidence may later consume this compact
        # projection, but cannot mint or rewrite it.
        normalized_observations: list[tuple[str, int, str]] = []
        for event in market_events:
            try:
                observed = datetime.fromisoformat(
                    event.observed_ts.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError(
                    f"invalid market event observed_ts: {event.observed_ts}"
                ) from exc
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("market event observed_ts must include timezone")
            normalized_observations.append(
                (
                    observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    event.sequence,
                    event.dedupe_key,
                )
            )
        normalized_observations.sort(key=lambda item: (item[0], item[1], item[2]))
        observation_timestamps = tuple(item[0] for item in normalized_observations)
        observation_identity_payload = [
            {
                "observed_ts": observed_ts,
                "sequence": sequence,
                "dedupe_key": dedupe_key,
            }
            for observed_ts, sequence, dedupe_key in normalized_observations
        ]
        observation_membership_payload = {
            "schema_version": 1,
            "replay_dataset_hash": result.replay.dataset_hash,
            "event_count": result.replay.event_count,
            "observations": observation_identity_payload,
        }
        observation_membership_sha256 = (
            hashlib.sha256(
                json.dumps(
                    observation_membership_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if observation_identity_payload
            else None
        )
        campaign_causal_membership = (
            None
            if not observation_timestamps
            else {
                "schema_version": 2,
                "replay_dataset_hash": result.replay.dataset_hash,
                "event_count": result.replay.event_count,
                "evaluation_window_start": observation_timestamps[0],
                "evaluation_window_end": observation_timestamps[-1],
                "observation_timestamps": list(observation_timestamps),
                "observation_membership_sha256": observation_membership_sha256,
            }
        )
        payload = {
            "schema_version": 2,
            "dataset_name": dataset.name,
            "sport": dataset.sport if sport_identity_proven else "unknown",
            "sport_scope": list(verified_sports),
            "sport_identity_proven": sport_identity_proven,
            "dataset_schema_version": dataset.schema_version,
            "historical_import_identity": dataset.import_identity,
            "dataset_governance": asdict(dataset.governance) if dataset.governance is not None else None,
            "market_price_truth": market_price_truth.to_dict(),
            "strategy_id": runtime_strategy_id,
            "strategy_runtime": {
                "strategy_id": runtime_strategy_id,
                "canonical_strategy_id": self.strategy.strategy_id,
                "label": self.strategy.label,
                "agent_names": list(self.strategy.agent_names),
                "opens_paper_tickets": self.strategy.opens_paper_tickets,
                "research_plan_sha256": (
                    self.research_plan.source_sha256 if self.research_plan is not None else None
                ),
                "economic_goal_provenance": (
                    None
                    if goal_provenance is None
                    else {
                        "schema": goal_provenance.schema,
                        "schema_version": goal_provenance.schema_version,
                        "goal_id": goal_provenance.goal_id,
                        "revision": goal_provenance.revision,
                        "bankroll_id": goal_provenance.bankroll_id,
                        "contract_sha256": goal_provenance.contract_sha256,
                    }
                ),
                "risk_policy_provenance": policy_provenance,
            },
            "experiment_key": result.experiment_key,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
            "run_id": result.replay.run_id,
            "event_count": result.replay.event_count,
            "replay_dataset_hash": result.replay.dataset_hash,
            "campaign_causal_membership": campaign_causal_membership,
            "settled_ticket_ids": list(result.settled_ticket_ids),
            "balance": str(result.balance),
            "evaluation": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(result.evaluation).items()
            },
            "portfolio": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(result.portfolio).items()
            },
            "real_money_execution": False,
        }
        if outcome_lineage is not None:
            # Duplicate only the compact canonical trust binding into the existing
            # checksum-bound run summary. This makes prior schema-v2 activation
            # discoverable after restart even if the mutable registry is rewritten
            # to look like a never-upgraded schema-v1 workspace.
            payload["outcome_lineage_trust"] = outcome_lineage_payload(outcome_lineage)
        return payload

    def close(self) -> None:
        # Canonical PaperBook persistence is owned by the workspace-locked run
        # transaction path. Teardown must never write an unlocked in-memory snapshot:
        # this session may have been constructed before another process committed a
        # newer canonical book, and saving here would silently roll that commit back.
        self.store.close()

# Seal the consumer entry after class creation. The metaclass data descriptor also
# makes direct type.__setattr__/type.__delattr__ respect the same class-level fence.
_AutosportSessionMeta._run_dataset_locked = _build_settlement_consumer_class_guard(
    "_run_dataset_locked"
)
AutosportSession._settlement_consumer_bindings_sealed = True
