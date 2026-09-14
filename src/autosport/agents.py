from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .domain import MarketEvent, TicketLeg
from .paper import PaperBook


@dataclass(slots=True)
class AgentContext:
    paper_book: PaperBook
    latest_quotes: dict[str, MarketEvent] = field(default_factory=dict)
    event_count: int = 0
    replay_run_id: str = "unbound"
    decision_ledger: JsonlDecisionLedger | None = None
    notes: list[str] = field(default_factory=list)

    def market_context_hash(self) -> str:
        projection = {
            key: value.to_dict()
            for key, value in sorted(self.latest_quotes.items())
        }
        canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Agent(Protocol):
    name: str

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None: ...


class MarketMirrorAgent:
    name = "market-mirror"

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        context.latest_quotes[event.quote_key] = event
        context.event_count += 1


class PaperBaselineAgent:
    """Transparent baseline for evaluation. It only creates virtual tickets when fixtures explicitly opt in."""

    name = "paper-baseline"

    def __init__(self, stake: Decimal | str = "50") -> None:
        try:
            amount = Decimal(str(stake))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("paper baseline stake must be a finite decimal") from exc
        if not amount.is_finite():
            raise ValueError("paper baseline stake must be a finite decimal")
        if amount <= 0:
            raise ValueError("paper baseline stake must be positive")
        self.stake = amount
        self._used_signals: set[str] = set()

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        signal_id = str(event.metadata.get("paper_signal_id", ""))
        if not signal_id or signal_id in self._used_signals:
            return
        if event.status != "open":
            return
        if event.metadata.get("paper_signal") is not True or self.stake > context.paper_book.balance:
            return
        ticket = context.paper_book.open_ticket(
            [TicketLeg(event.event_id, event.market_id, event.selection_id, event.decimal_odds)],
            self.stake,
            reason=f"fixture baseline signal {signal_id}",
            placed_at=event.observed_ts,
        )
        if context.decision_ledger:
            context.decision_ledger.append(
                DecisionRecord(
                    replay_run_id=context.replay_run_id,
                    agent=self.name,
                    observed_ts=event.observed_ts,
                    action="OPEN_PAPER_TICKET",
                    payload={"ticket_id": ticket.ticket_id, "stake": str(ticket.stake), "quote_key": event.quote_key},
                    context_hash=context.market_context_hash(),
                )
            )
        self._used_signals.add(signal_id)


class AgentOrchestrator:
    def __init__(self, agents: list[Agent], context: AgentContext) -> None:
        self.agents = list(agents)
        self.context = context

    def on_market_event(self, event: MarketEvent) -> None:
        for agent in self.agents:
            agent.on_market_event(event, self.context)

    def finalize_replay(self) -> None:
        """Allow causal agents to fail closed on unconsumed replay-time work before outcomes unlock."""

        for agent in self.agents:
            finalize = getattr(agent, "finalize_replay", None)
            if finalize is not None:
                finalize(self.context)
