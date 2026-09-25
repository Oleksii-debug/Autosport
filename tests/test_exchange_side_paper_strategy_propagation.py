from decimal import Decimal
from types import SimpleNamespace

from autosport.domain import MarketEvent, PaperTicket, TicketLeg
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.paper_strategy import PaperValueAgent


class _CapturingRiskPolicy:
    def __init__(self) -> None:
        self.economic_goal = SimpleNamespace(bankroll_id="bankroll-1", currency="EUR")
        self.captured_context = None

    def derive_goal_stake(self, _book, _expected_profit_per_unit):
        return Decimal("10")

    def evaluate(self, _book, _stake, *, context=None):
        self.captured_context = context
        return SimpleNamespace(allowed=False)


def _event(exchange_side: str) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.0"),
        observed_ts="2026-09-22T12:00:00+00:00",
        source_id="provider-1",
        sequence=1,
        sport="basketball",
        exchange_side=exchange_side,
        market_semantics_id="basketball:h2h:v1",
    )


def test_paper_value_agent_preserves_exchange_side_in_proposed_leg() -> None:
    event = _event("back")
    policy = _CapturingRiskPolicy()
    agent = PaperValueAgent(
        {
            event.quote_key: ForecastRecord(
                quote_key=event.quote_key,
                probability=Decimal("0.75"),
                model_id="model-1",
                model_version="1",
                strategy_version="paper-value-v1",
                model_training_cutoff_ts="2026-09-22T10:00:00+00:00",
                input_cutoff_ts="2026-09-22T11:59:00+00:00",
                generated_at="2026-09-22T11:59:30+00:00",
                uncertainty=Decimal("0.05"),
                evidence_hashes=("a" * 64,),
                market_snapshot_hash="b" * 64,
                provenance={"dataset": "market-semantics-composition"},
                forecast_id="forecast-1",
                market_semantics_id=event.market_semantics_id,
            )
        },
        minimum_expected_profit_per_unit="0",
        risk_policy=policy,
    )
    runtime = SimpleNamespace(ledger=SimpleNamespace(events=lambda: ()))
    context = SimpleNamespace(
        paper_book=PaperBook("100"),
        paper_execution=runtime,
        paper_provider_accounts=((event.source_id, "account-1"),),
        notes=[],
        decision_ledger=object(),
        replay_run_id="run-1",
    )

    agent.on_market_event(event, context)

    assert policy.captured_context is not None
    assert policy.captured_context.quotes == (event,)
    assert len(policy.captured_context.legs) == 1
    leg = policy.captured_context.legs[0]
    assert leg.exchange_side == "back"
    assert leg.market_semantics_id == event.market_semantics_id
    assert leg.quote_key == event.quote_key


def test_restart_matcher_rejects_same_selection_on_opposite_exchange_side() -> None:
    back_event = _event("back")
    ticket = PaperTicket(
        ticket_id="ticket-1",
        stake=Decimal("10"),
        legs=(
            TicketLeg(
                event_id=back_event.event_id,
                market_id=back_event.market_id,
                selection_id=back_event.selection_id,
                locked_odds=back_event.decimal_odds,
                sport=back_event.sport,
                exchange_side="back",
                market_semantics_id=back_event.market_semantics_id,
            ),
        ),
        placed_at=back_event.observed_ts,
    )

    assert PaperValueAgent._ticket_matches_event(ticket, back_event)
    assert not PaperValueAgent._ticket_matches_event(ticket, _event("lay"))
