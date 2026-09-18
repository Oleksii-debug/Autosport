from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .agents import AgentContext
from .decision_ledger import DecisionRecord
from .domain import MarketEvent, TicketLeg
from .forecasting import ForecastRecord, parse_iso_timestamp
from .price_truth import paper_quote_rejection_reason
from .probability import paper_value
from .risk import PaperRiskPolicy


@dataclass(frozen=True, slots=True)
class Forecast:
    """Legacy minimal forecast contract kept for backward compatibility."""

    quote_key: str
    probability: Decimal
    model_id: str
    as_of_ts: str


ForecastLike = Forecast | ForecastRecord


class PaperValueAgent:
    """Paper-only strategy agent. It can open virtual tickets but has no real-money execution capability."""

    name = "paper-value-strategy"

    def __init__(
        self,
        forecasts: dict[str, ForecastLike],
        stake: Decimal | str = "50",
        minimum_expected_profit_per_unit: Decimal | str = "0.05",
        risk_policy: PaperRiskPolicy | None = None,
    ) -> None:
        self.forecasts = forecasts
        self.stake = Decimal(str(stake))
        self.minimum_edge = Decimal(str(minimum_expected_profit_per_unit))
        self.risk_policy = risk_policy or PaperRiskPolicy()
        self._acted: set[str] = set()

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        if event.quote_key in self._acted or event.status != "open":
            return
        # Provider truth is binding.  Observational prices, positive-delay exchange
        # quotes, incomplete ladder caches, or quotes whose observed capacity is below
        # the configured paper stake must fail closed before virtual economics.
        if paper_quote_rejection_reason(event, self.stake) is not None:
            return
        forecast = self.forecasts.get(event.quote_key)
        if forecast is None:
            return
        if parse_iso_timestamp(forecast.as_of_ts) > parse_iso_timestamp(event.observed_ts):
            return
        estimate = paper_value(event.quote_key, forecast.probability, event.decimal_odds)
        if estimate.expected_profit_per_unit < self.minimum_edge:
            return
        risk = self.risk_policy.evaluate(context.paper_book, self.stake)
        if not risk.allowed:
            return
        ticket = context.paper_book.open_ticket(
            [TicketLeg(
                event.event_id,
                event.market_id,
                event.selection_id,
                event.decimal_odds,
                sport=event.sport,
            )],
            self.stake,
            reason=f"paper forecast {forecast.model_id}; EV/unit={estimate.expected_profit_per_unit}",
            placed_at=event.observed_ts,
        )
        self._acted.add(event.quote_key)
        if context.decision_ledger:
            payload = {
                "ticket_id": ticket.ticket_id,
                "quote_key": event.quote_key,
                "forecast_model": forecast.model_id,
                "probability": str(forecast.probability),
                "expected_profit_per_unit": str(estimate.expected_profit_per_unit),
                "stake": str(ticket.stake),
            }
            if isinstance(forecast, ForecastRecord):
                payload.update(
                    {
                        "forecast_id": forecast.forecast_id,
                        "forecast_hash": forecast.canonical_hash,
                        "model_version": forecast.model_version,
                        "strategy_version": forecast.strategy_version,
                        "model_training_cutoff_ts": forecast.model_training_cutoff_ts,
                        "input_cutoff_ts": forecast.input_cutoff_ts,
                        "generated_at": forecast.generated_at,
                        "uncertainty": str(forecast.uncertainty),
                        "evidence_hashes": list(forecast.evidence_hashes),
                        "market_snapshot_hash": forecast.market_snapshot_hash,
                    }
                )
            context.decision_ledger.append(
                DecisionRecord(
                    replay_run_id=context.replay_run_id,
                    agent=self.name,
                    observed_ts=event.observed_ts,
                    action="OPEN_PAPER_VALUE_TICKET",
                    payload=payload,
                    context_hash=context.market_context_hash(),
                )
            )
