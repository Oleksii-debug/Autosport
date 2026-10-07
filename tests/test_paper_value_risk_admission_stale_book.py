from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.economic_goal import EconomicGoalContract
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
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


def _runtime(tmp_path, book: PaperBook) -> PaperExecutionAdoptionRuntime:
    config = PaperExecutionModelConfig(
        model_id="risk-admission-stale-book-test",
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


def _seed_canonically_admissible_committed_exposure(
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


def test_fresh_economic_risk_pass_cannot_be_rebound_to_changed_paper_book(
    tmp_path,
) -> None:
    event = MarketEvent(
        event_id="event-economic-target",
        market_id="market-economic-target",
        selection_id="selection-economic-target",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-20T09:00:00+00:00",
        source_id="provider-a",
        sequence=1,
        sport="football",
    )
    competing_event = MarketEvent(
        event_id="event-economic-competitor",
        market_id="market-economic-competitor",
        selection_id="selection-economic-competitor",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-20T08:59:59+00:00",
        source_id="provider-a",
        sequence=1,
        sport="football",
    )
    goal = EconomicGoalContract(
        goal_id="stale-economic-goal",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("0.10"),
        max_capital_at_risk_fraction=Decimal("0.50"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=1,
        max_quote_age_seconds=Decimal("5"),
        minimum_data_quality=Decimal("0"),
    )
    policy = PaperRiskPolicy(economic_goal=goal)
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    canonical_append_economic = ledger.append_economic
    mutated = False

    target_leg = TicketLeg(
        event.event_id,
        event.market_id,
        event.selection_id,
        event.decimal_odds,
        sport=event.sport,
    )
    target_context = ProposedTicketRiskContext(
        legs=(target_leg,),
        quotes=(event,),
        provider_accounts=((event.source_id, "account-a"),),
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=event.observed_ts,
    )
    assert policy.evaluate(
        book,
        Decimal("1.00"),
        context=target_context,
    ).allowed

    def append_then_allocate(record, authority) -> str:
        nonlocal mutated
        digest = canonical_append_economic(record, authority)
        if not mutated:
            competing_leg = TicketLeg(
                competing_event.event_id,
                competing_event.market_id,
                competing_event.selection_id,
                competing_event.decimal_odds,
                sport=competing_event.sport,
            )
            competing_context = ProposedTicketRiskContext(
                legs=(competing_leg,),
                quotes=(competing_event,),
                provider_accounts=((competing_event.source_id, "account-a"),),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                proposal_ts=competing_event.observed_ts,
            )
            assert policy.evaluate(
                book,
                Decimal("1.00"),
                context=competing_context,
            ).allowed
            book.open_ticket(
                (competing_leg,),
                Decimal("1.00"),
                reason="simultaneous-economic-paper-allocation",
                placed_at=competing_event.observed_ts,
                provider_source_ids=(competing_event.source_id,),
                provider_accounts=((competing_event.source_id, "account-a"),),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
            )
            mutated = True
            assert not policy.evaluate(
                book,
                Decimal("1.00"),
                context=target_context,
            ).allowed
        return digest

    ledger.append_economic = append_then_allocate  # type: ignore[method-assign]
    context = AgentContext(
        book,
        replay_run_id="replay-economic-stale-book",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    agent = PaperValueAgent(
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

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="no longer passes canonical risk evaluation",
    ):
        agent.on_market_event(event, context)

    assert mutated is True
    assert book.balance == Decimal("99.00")
    assert len(book.tickets) == 1
    assert not policy.evaluate(
        book,
        Decimal("1.00"),
        context=target_context,
    ).allowed
    assert runtime.ledger.events() == ()
    assert len(tuple(ledger.verified_records())) == 1
    assert not (tmp_path / ".paper-value-risk-admissions").exists()


def test_fresh_general_risk_pass_cannot_be_rebound_to_changed_paper_book(tmp_path) -> None:
    event = MarketEvent(
        event_id="event-a",
        market_id="market-a",
        selection_id="selection-a",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-20T09:00:00+00:00",
        source_id="provider-a",
        sequence=1,
        sport="football",
    )
    book = PaperBook("100.00")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.02"),
        max_committed_fraction=Decimal("0.20"),
    )
    _seed_canonically_admissible_committed_exposure(book, policy)
    assert book.balance == Decimal("81.00")
    assert policy.evaluate(book, Decimal("1.00")).allowed

    runtime = _runtime(tmp_path, book)
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    canonical_append = ledger.append
    mutated = False

    def append_then_allocate(record) -> str:
        nonlocal mutated
        digest = canonical_append(record)
        if not mutated:
            # Keep the product authority exact while injecting the race only
            # after this action's durable decision has been committed.
            assert policy.evaluate(book, Decimal("1.00")).allowed
            book.open_ticket(
                (
                    TicketLeg(
                        "event-concurrent",
                        "market-concurrent",
                        "selection-concurrent",
                        Decimal("2.00"),
                        sport="football",
                    ),
                ),
                Decimal("1.00"),
                reason="simultaneous-paper-allocation",
                placed_at="2026-09-20T08:59:59+00:00",
            )
            mutated = True
            assert not policy.evaluate(book, Decimal("1.00")).allowed
        return digest

    ledger.append = append_then_allocate  # type: ignore[method-assign]
    context = AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    agent = PaperValueAgent(
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

    # Both 1.00 actions independently see committed=19.00 and can reach the 20.00
    # cap. The competing action wins the race after this action's first PASS. The
    # stale action must not then mint admission/execution authority for committed=21.
    with pytest.raises(
        PaperExecutionAdoptionError,
        match="no longer passes canonical risk evaluation",
    ):
        agent.on_market_event(event, context)

    assert mutated is True
    assert book.balance == Decimal("80.00")
    assert len(book.tickets) == 20
    assert not policy.evaluate(book, Decimal("1.00")).allowed
    assert runtime.ledger.events() == ()
    assert len(tuple(ledger.verified_records())) == 1

    admission_root = tmp_path / ".paper-value-risk-admissions"
    assert not admission_root.exists()
