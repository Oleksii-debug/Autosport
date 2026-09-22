from decimal import Decimal
from types import SimpleNamespace

from autosport.domain import MarketEvent
from autosport.paper_strategy import Forecast, PaperValueAgent


class _CapturingRiskPolicy:
    def __init__(self, *, allowed: bool = False) -> None:
        self.economic_goal = SimpleNamespace(
            bankroll_id="bankroll-1",
            currency="EUR",
        )
        self.allowed = allowed
        self.derived_signal = None
        self.captured_risk_amount = None
        self.captured_context = None

    def derive_goal_stake(self, _book, expected_profit_per_unit):
        self.derived_signal = expected_profit_per_unit
        return Decimal("10")

    def evaluate(self, _book, risk_amount, *, context=None):
        self.captured_risk_amount = risk_amount
        self.captured_context = context
        return SimpleNamespace(allowed=self.allowed)


def _event(*, exchange_side: str | None = "lay", odds: str = "2.0") -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal(odds),
        observed_ts="2026-09-22T12:00:00+00:00",
        source_id="provider-1",
        sequence=1,
        sport="basketball",
        exchange_side=exchange_side,
    )


def _run(
    probability: str,
    *,
    risk_allowed: bool = False,
    exchange_side: str | None = "lay",
    odds: str = "2.0",
) -> tuple[_CapturingRiskPolicy, SimpleNamespace]:
    event = _event(exchange_side=exchange_side, odds=odds)
    policy = _CapturingRiskPolicy(allowed=risk_allowed)
    agent = PaperValueAgent(
        {
            event.quote_key: Forecast(
                quote_key=event.quote_key,
                probability=Decimal(probability),
                model_id="model-1",
                as_of_ts=event.observed_ts,
            )
        },
        minimum_expected_profit_per_unit="0",
        risk_policy=policy,
    )
    context = SimpleNamespace(
        paper_book=object(),
        paper_execution=SimpleNamespace(
            ledger=SimpleNamespace(events=lambda: ()),
        ),
        paper_provider_accounts=((event.source_id, "account-1"),),
        notes=[],
        decision_ledger=object(),
        replay_run_id="run-1",
    )

    agent.on_market_event(event, context)
    return policy, context


def test_negative_ev_lay_does_not_reach_risk_proposal_path() -> None:
    # For a lay at decimal odds 2.0, p=0.75 has EV/stake = 1 - p*odds = -0.50.
    policy, _context = _run("0.75")
    assert policy.derived_signal is None
    assert policy.captured_context is None


def test_positive_ev_lay_is_not_rejected_by_back_only_value_sign() -> None:
    # For a lay at decimal odds 2.0, p=0.25 has EV/stake = 1 - p*odds = +0.50.
    policy, _context = _run("0.25")
    assert policy.derived_signal == Decimal("0.50")
    assert policy.captured_context is not None


def test_positive_ev_lay_fails_closed_before_back_only_execution_bridge() -> None:
    policy, context = _run("0.25", risk_allowed=True)

    assert policy.derived_signal == Decimal("0.50")
    assert policy.captured_context is not None
    assert context.notes == [
        "paper-value material action withheld: canonical #623 paper "
        "execution bridge is BACK-only for LAY"
    ]

def test_lay_risk_uses_liability_not_order_stake_above_odds_two() -> None:
    # One unit of LAY order stake at odds 3 risks two units of bankroll liability.
    policy, _context = _run("0.25", odds="3.0")

    assert policy.derived_signal == Decimal("0.25")
    assert policy.captured_risk_amount == Decimal("20.0")
    assert policy.captured_context is not None


def test_back_and_legacy_risk_amount_remains_order_stake() -> None:
    back_policy, _ = _run("0.50", exchange_side="back", odds="3.0")
    legacy_policy, _ = _run("0.50", exchange_side=None, odds="3.0")

    assert back_policy.captured_risk_amount == Decimal("10")
    assert legacy_policy.captured_risk_amount == Decimal("10")
