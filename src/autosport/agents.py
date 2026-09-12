from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from .domain import MarketEvent, TicketLeg
from .paper import PaperBook


@dataclass(slots=True)
class AgentContext:
    paper_book: PaperBook
    latest_quotes: dict[str, MarketEvent] = field(default_factory=dict)
    event_count: int = 0
    notes: list[str] = field(default_factory=list)


class Agent(Protocol):
    name: str

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None: ...


class MarketMirrorAgent:
    name = "market-mirror"

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        context.latest_quotes[event.selection_id] = event
        context.event_count += 1


class PaperBaselineAgent:
    """Transparent baseline for evaluation. It only creates virtual tickets when fixtures explicitly opt in."""

    name = "paper-baseline"

    def __init__(self, stake: Decimal | str = "50") -> None:
        self.stake = Decimal(str(stake))
        self._used_signals: set[str] = set()

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        signal_id = str(event.metadata.get("paper_signal_id", ""))
        if not signal_id or signal_id in self._used_signals:
            return
        if event.metadata.get("paper_signal") is not True:
            return
        if self.stake > context.paper_book.balance:
            return
        context.paper_book.open_ticket(
            [TicketLeg(event.event_id, event.market_id, event.selection_id, event.decimal_odds)],
            self.stake,
            reason=f"fixture baseline signal {signal_id}",
            placed_at=event.observed_ts,
        )
        self._used_signals.add(signal_id)


class AgentOrchestrator:
    def __init__(self, agents: list[Agent], context: AgentContext) -> None:
        self.agents = list(agents)
        self.context = context

    def on_market_event(self, event: MarketEvent) -> None:
        for agent in self.agents:
            agent.on_market_event(event, self.context)
