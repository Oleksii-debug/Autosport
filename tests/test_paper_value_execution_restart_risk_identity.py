from types import SimpleNamespace

from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
import autosport._paper_value_execution_authority as authority


EVENT_TS = "2026-09-20T09:00:00+00:00"


def test_restart_risk_rebuild_preserves_market_identity() -> None:
    runtime = object.__new__(PaperExecutionAdoptionRuntime)
    runtime.ledger = SimpleNamespace(events=lambda _run_id=None: ())

    class CapturingRiskPolicy:
        economic_goal = SimpleNamespace(bankroll_id="bankroll-1", currency="EUR")

        def __init__(self) -> None:
            self.context = None

        def evaluate(self, _book, _stake, *, context):
            self.context = context
            return SimpleNamespace(allowed=True)

    policy = CapturingRiskPolicy()
    agent = SimpleNamespace(risk_policy=policy)
    context = SimpleNamespace(
        paper_execution=runtime,
        paper_book=PaperBook("100.00"),
    )
    record = SimpleNamespace(decision_id="decision-1", observed_ts=EVENT_TS)
    durable_event = MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.50"),
        observed_ts=EVENT_TS,
        source_id="provider-a",
        sequence=1,
        sport="football",
        market_semantics_id="winner-v2",
        exchange_side="lay",
    )
    result = authority._first_execution_risk_authority(
        agent,
        context,
        record,
        None,
        durable_event,
        Decimal("1.00"),
        "run-1",
        {
            "bankroll_id": "bankroll-1",
            "currency": "EUR",
            "provider_account": ["provider-a", "account-a"],
        },
    )

    assert result == "fresh-risk-evaluation"
    leg = policy.context.legs[0]
    assert leg.event_id == durable_event.event_id
    assert leg.market_id == durable_event.market_id
    assert leg.selection_id == durable_event.selection_id
    assert leg.market_semantics_id == "winner-v2"
    assert leg.exchange_side == "lay"
    assert policy.context.quotes[0] is durable_event
