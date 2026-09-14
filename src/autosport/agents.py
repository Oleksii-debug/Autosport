from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Protocol

from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .domain import MarketEvent, TicketLeg
from .paper import PaperBook


def validate_agent_names(agent_names: Iterable[object]) -> tuple[str, ...]:
    """Return one canonical ordered agent identity or fail closed on ambiguity."""

    names = tuple(agent_names)
    if not names:
        raise ValueError("agent composition must contain at least one agent")
    for name in names:
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("agent names must be non-empty canonical strings")
    if len(set(names)) != len(names):
        raise ValueError("agent composition contains duplicate agent names")
    return names


def agent_composition_sha256(agent_names: Iterable[object]) -> str:
    """Hash the exact ordered canonical agent composition used by one strategy runtime."""

    names = validate_agent_names(agent_names)
    canonical = json.dumps(
        {"schema_version": 1, "ordered_agent_names": list(names)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        self.stake = Decimal(str(stake))
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
        self._agents: tuple[Agent, ...] = tuple(agents)
        self._bound_agent_names = validate_agent_names(
            getattr(agent, "name", None) for agent in self._agents
        )
        self._bound_agent_composition_sha256 = agent_composition_sha256(self._bound_agent_names)
        self.context = context

    @property
    def agents(self) -> tuple[Agent, ...]:
        """Expose the bound runtime composition without a mutable list surface."""

        self._assert_bound_composition()
        return self._agents

    @property
    def agent_names(self) -> tuple[str, ...]:
        self._assert_bound_composition()
        return self._bound_agent_names

    @property
    def agent_composition_sha256(self) -> str:
        self._assert_bound_composition()
        return self._bound_agent_composition_sha256

    def _assert_bound_composition(self) -> None:
        current_names = validate_agent_names(
            getattr(agent, "name", None) for agent in self._agents
        )
        if current_names != self._bound_agent_names:
            raise RuntimeError(
                "agent runtime composition changed after provenance binding: "
                f"expected={self._bound_agent_names!r} actual={current_names!r}"
            )
        current_hash = agent_composition_sha256(current_names)
        if current_hash != self._bound_agent_composition_sha256:
            raise RuntimeError("agent runtime composition hash changed after provenance binding")

    def on_market_event(self, event: MarketEvent) -> None:
        self._assert_bound_composition()
        for agent in self._agents:
            agent.on_market_event(event, self.context)

    def finalize_replay(self) -> None:
        """Allow causal agents to fail closed on unconsumed replay-time work before outcomes unlock."""

        self._assert_bound_composition()
        for agent in self._agents:
            finalize = getattr(agent, "finalize_replay", None)
            if finalize is not None:
                finalize(self.context)
