from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from threading import Event, Thread, current_thread
from time import monotonic, sleep

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy


def _runtime(tmp_path, book: PaperBook) -> PaperExecutionAdoptionRuntime:
    config = PaperExecutionModelConfig(
        model_id="risk-admission-post-witness-serialization-test",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=config,
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )


def _seed_committed_exposure(
    book: PaperBook,
    policy: PaperRiskPolicy,
) -> None:
    for index in range(19):
        assert policy.evaluate(book, Decimal("1.00")).allowed
        book.open_ticket(
            (
                TicketLeg(
                    f"event-seed-{index}",
                    f"market-seed-{index}",
                    f"selection-seed-{index}",
                    Decimal("2.00"),
                    sport="football",
                ),
            ),
            Decimal("1.00"),
            reason="canonical-risk-admissible-seed",
            placed_at="2026-09-20T08:00:00+00:00",
        )


def _event(suffix: str) -> MarketEvent:
    return MarketEvent(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-20T09:00:00+00:00",
        source_id="provider-a",
        sequence=1,
        sport="football",
    )


def _agent(event: MarketEvent, policy: PaperRiskPolicy) -> PaperValueAgent:
    return PaperValueAgent(
        {
            event.quote_key: Forecast(
                quote_key=event.quote_key,
                probability=Decimal("0.75"),
                model_id="model-a",
                as_of_ts=event.observed_ts,
            )
        },
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )


def _run_agent(
    agent: PaperValueAgent,
    event: MarketEvent,
    context: AgentContext,
    *,
    done: Event,
    errors: list[BaseException],
) -> None:
    try:
        agent.on_market_event(event, context)
    except BaseException as exc:  # assertion inspects the exact propagated failure.
        errors.append(exc)
    finally:
        done.set()


class _PostWitnessBlockingPaperBook(PaperBook):
    """Block target materialization after the canonical risk witness commits."""

    def __init__(self, initial_balance: str, *, blocked: Event, release: Event) -> None:
        super().__init__(initial_balance)
        self.blocked = blocked
        self.release = release
        self.blocking_thread: Thread | None = None

    def open_ticket(self, *args, **kwargs):
        # Canonical PaperValue execution reaches PaperBook materialization only
        # after _issue_general_risk_admission has durably committed and while the
        # runtime's execution RLock is still held.  Blocking here preserves the
        # production authority graph and leaves the exact JsonlDecisionLedger in
        # place for decision-origin verification.
        if (
            self.blocking_thread is not None
            and current_thread() is self.blocking_thread
        ):
            self.blocked.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("target execution was not released")
        return super().open_ticket(*args, **kwargs)

def test_competing_canonical_execution_cannot_enter_after_general_risk_witness(
    tmp_path,
) -> None:
    target_post_witness = Event()
    release_target = Event()
    book = _PostWitnessBlockingPaperBook(
        "100.00",
        blocked=target_post_witness,
        release=release_target,
    )
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.02"),
        max_committed_fraction=Decimal("0.20"),
    )
    _seed_committed_exposure(book, policy)
    assert book.balance == Decimal("81.00")
    assert policy.evaluate(book, Decimal("1.00")).allowed

    runtime = _runtime(tmp_path, book)
    target_event = _event("target")
    competitor_event = _event("competitor")
    target_agent = _agent(target_event, policy)
    competitor_agent = _agent(competitor_event, policy)

    decision_ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    target_context = AgentContext(
        book,
        replay_run_id="replay-target",
        decision_ledger=decision_ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    competitor_context = AgentContext(
        book,
        replay_run_id="replay-competitor",
        decision_ledger=decision_ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )

    target_decision_id = target_agent._material_action_id(
        target_context,
        target_event,
    )
    competitor_decision_id = competitor_agent._material_action_id(
        competitor_context,
        competitor_event,
    )

    target_done = Event()
    competitor_done = Event()
    target_errors: list[BaseException] = []
    competitor_errors: list[BaseException] = []
    target_thread = Thread(
        target=_run_agent,
        args=(target_agent, target_event, target_context),
        kwargs={"done": target_done, "errors": target_errors},
        daemon=True,
    )
    competitor_thread = Thread(
        target=_run_agent,
        args=(competitor_agent, competitor_event, competitor_context),
        kwargs={"done": competitor_done, "errors": competitor_errors},
        daemon=True,
    )
    book.blocking_thread = target_thread

    target_thread.start()
    try:
        assert target_post_witness.wait(timeout=5)
        # Target has committed its GENERAL risk witness while exposure is still 19.
        # The competitor can still make its earlier proposal/decision durable, but
        # must block before the shared runtime authorization/execution section.
        competitor_thread.start()
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if any(
                record.decision_id == competitor_decision_id
                for record in decision_ledger.verified_records()
            ):
                break
            sleep(0.01)
        else:
            raise AssertionError("competing durable decision was not observed")
        assert not competitor_done.wait(timeout=0.25)
    finally:
        release_target.set()

    assert target_done.wait(timeout=5)
    assert competitor_done.wait(timeout=5)
    target_thread.join(timeout=1)
    competitor_thread.join(timeout=1)

    assert target_errors == []
    assert len(competitor_errors) == 1
    assert isinstance(competitor_errors[0], PaperExecutionAdoptionError)
    assert "no longer passes canonical risk evaluation" in str(competitor_errors[0])

    # The target occupies the exact 20% aggregate cap. The stale competitor cannot
    # create a second #623 run or materialized ticket after observing the old book.
    assert book.balance == Decimal("80.00")
    assert len(book.tickets) == 20
    assert not policy.evaluate(book, Decimal("1.00")).allowed

    events = runtime.ledger.events()
    reserved = [
        item
        for item in events
        if item.get("event_type") == "RUN_RESERVED"
    ]
    assert len(reserved) == 1
    assert reserved[0].get("payload", {}).get("trigger_id") == target_decision_id

    durable_records = tuple(decision_ledger.verified_records())
    assert len(durable_records) == 2
    assert {record.decision_id for record in durable_records} == {
        target_decision_id,
        competitor_decision_id,
    }
