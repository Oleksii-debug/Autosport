from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable, Protocol

from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .domain import MarketEvent, TicketLeg
from .market_mirror import MarketMirror
from .paper import PaperBook

if TYPE_CHECKING:
    from .paper_execution_adoption import PaperExecutionAdoptionRuntime


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


class _LatestQuotesView(Mapping[object, MarketEvent]):
    """Detached read compatibility over one provider-isolated MarketMirror snapshot.

    Iteration exposes the canonical provider-aware ``(source_id, quote_key)`` keys.
    Legacy callers may still look up one plain ``quote_key`` when it identifies exactly
    one provider in the captured snapshot. Ambiguous cross-provider lookups fail closed
    instead of silently choosing economic evidence from an arbitrary source.
    """

    def __init__(self, events: Iterable[MarketEvent]) -> None:
        self._by_source_quote: dict[tuple[str, str], MarketEvent] = {}
        self._by_quote: dict[str, MarketEvent] = {}
        self._ambiguous_quotes: set[str] = set()
        for event in events:
            provider_key = (event.source_id, event.quote_key)
            self._by_source_quote[provider_key] = event
            previous = self._by_quote.get(event.quote_key)
            if previous is None:
                self._by_quote[event.quote_key] = event
            elif previous.source_id != event.source_id:
                self._ambiguous_quotes.add(event.quote_key)

    def __getitem__(self, key: object) -> MarketEvent:
        if isinstance(key, tuple) and len(key) == 2:
            return self._by_source_quote[key]
        if isinstance(key, str):
            if key in self._ambiguous_quotes:
                raise ValueError(
                    "latest quote_key is ambiguous across providers; "
                    "use (source_id, quote_key): " + key
                )
            return self._by_quote[key]
        raise KeyError(key)

    def __iter__(self):
        return iter(self._by_source_quote)

    def __len__(self) -> int:
        return len(self._by_source_quote)


@dataclass(slots=True, init=False)
class AgentContext:
    paper_book: PaperBook
    event_count: int = 0
    replay_run_id: str = "unbound"
    decision_ledger: JsonlDecisionLedger | None = None
    paper_execution: "PaperExecutionAdoptionRuntime | None" = None
    paper_provider_accounts: tuple[tuple[str, str], ...] = ()
    notes: list[str] = field(default_factory=list)
    market_mirror: MarketMirror = field(default_factory=MarketMirror)

    def __init__(
        self,
        paper_book: PaperBook,
        latest_quotes: Mapping[object, MarketEvent] | None = None,
        event_count: int = 0,
        replay_run_id: str = "unbound",
        decision_ledger: JsonlDecisionLedger | None = None,
        paper_execution: "PaperExecutionAdoptionRuntime | None" = None,
        paper_provider_accounts: tuple[tuple[str, str], ...] = (),
        notes: list[str] | None = None,
        market_mirror: MarketMirror | None = None,
    ) -> None:
        """Build agent state while keeping ``MarketMirror`` the sole mutable quote store.

        ``latest_quotes`` remains an initialization compatibility boundary for existing
        replay/research callers. Its values are copied into the canonical mirror and the
        input mapping itself is never retained or mutated.
        """

        self.paper_book = paper_book
        self.event_count = event_count
        self.replay_run_id = replay_run_id
        self.decision_ledger = decision_ledger
        if paper_execution is not None:
            from .paper_execution_adoption import PaperExecutionAdoptionRuntime
            if not isinstance(paper_execution, PaperExecutionAdoptionRuntime):
                raise TypeError(
                    "paper_execution must be PaperExecutionAdoptionRuntime or None"
                )
            if paper_execution.book is not paper_book:
                raise ValueError(
                    "paper_execution must materialize into the AgentContext PaperBook"
                )
        if type(paper_provider_accounts) is not tuple:
            raise TypeError("paper_provider_accounts must be a canonical tuple")
        normalized_accounts: list[tuple[str, str]] = []
        for binding in paper_provider_accounts:
            if type(binding) is not tuple or len(binding) != 2:
                raise ValueError(
                    "paper_provider_accounts must contain (source_id, account_id) tuples"
                )
            source_id, account_id = binding
            if (
                type(source_id) is not str
                or not source_id
                or source_id.strip() != source_id
                or type(account_id) is not str
                or not account_id
                or account_id.strip() != account_id
            ):
                raise ValueError(
                    "paper_provider_accounts must contain canonical non-empty text"
                )
            normalized_accounts.append((source_id, account_id))
        canonical_accounts = tuple(normalized_accounts)
        if (
            canonical_accounts != tuple(sorted(canonical_accounts))
            or len(canonical_accounts) != len(set(canonical_accounts))
            or len({source_id for source_id, _ in canonical_accounts})
            != len(canonical_accounts)
        ):
            raise ValueError(
                "paper_provider_accounts must be sorted, unique, and source-scoped"
            )
        self.paper_execution = paper_execution
        self.paper_provider_accounts = canonical_accounts
        self.notes = [] if notes is None else notes
        if market_mirror is not None and not isinstance(market_mirror, MarketMirror):
            raise TypeError("market_mirror must be a MarketMirror")
        self.market_mirror = market_mirror if market_mirror is not None else MarketMirror()

        if latest_quotes is not None:
            if not isinstance(latest_quotes, Mapping):
                raise TypeError("latest_quotes must be a mapping")
            for event in latest_quotes.values():
                if not isinstance(event, MarketEvent):
                    raise TypeError("latest_quotes values must be MarketEvent instances")
                self.market_mirror.apply(event)

    @property
    def latest_quotes(self) -> Mapping[object, MarketEvent]:
        """Return a detached compatibility view over the canonical Market Mirror.

        Provider-aware tuple keys are the iterable identity. A unique legacy string
        quote key is accepted for read lookup so causal research callers survive the
        migration without restoring a second mutable live-quote dictionary.
        """

        return _LatestQuotesView(self.market_mirror.snapshot())

    def market_context_hash(self) -> str:
        projection = [event.to_dict() for event in self.market_mirror.snapshot()]
        canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Agent(Protocol):
    name: str

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None: ...


class MarketMirrorAgent:
    name = "market-mirror"

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        context.market_mirror.apply(event)
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
        # The baseline may preserve an explicit BACK identity because existing
        # PaperBook economics are BACK-compatible. Explicit LAY must remain
        # fail-closed until the canonical single-leg LAY liability/settlement
        # authority is integrated; dropping the side would silently relabel it.
        if event.exchange_side == "lay":
            return
        ticket = context.paper_book.open_ticket(
            [TicketLeg(
                event.event_id,
                event.market_id,
                event.selection_id,
                event.decimal_odds,
                sport=event.sport,
                exchange_side=event.exchange_side,
                market_semantics_id=event.market_semantics_id,
            )],
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
            # A callback can hold a reference to another bound agent. Revalidate
            # immediately so peer identity drift cannot reach the next callback.
            self._assert_bound_composition()

    def finalize_replay(self) -> None:
        """Allow causal agents to fail closed on unconsumed replay-time work before outcomes unlock."""

        self._assert_bound_composition()
        for agent in self._agents:
            finalize = getattr(agent, "finalize_replay", None)
            if finalize is not None:
                finalize(self.context)
                # Preserve the same callback boundary during finalization: one
                # agent must not mutate a later agent's identity and let it run.
                self._assert_bound_composition()
