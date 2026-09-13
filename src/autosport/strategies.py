from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .agents import Agent, MarketMirrorAgent, PaperBaselineAgent


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Canonical strategy identity bound to the exact runtime agents it executes."""

    strategy_id: str
    label: str
    description: str
    agent_names: tuple[str, ...]
    opens_paper_tickets: bool


StrategyFactory = Callable[[], list[Agent]]


_STRATEGIES: dict[str, tuple[StrategySpec, StrategyFactory]] = {
    "baseline-v1": (
        StrategySpec(
            strategy_id="baseline-v1",
            label="Fixture baseline",
            description=(
                "Transparent deterministic paper baseline. It opens a fixed virtual stake only "
                "for causal market events explicitly marked with a fixture paper_signal."
            ),
            agent_names=("market-mirror", "paper-baseline"),
            opens_paper_tickets=True,
        ),
        lambda: [MarketMirrorAgent(), PaperBaselineAgent("50")],
    ),
    "observe-only-v1": (
        StrategySpec(
            strategy_id="observe-only-v1",
            label="Observe only",
            description=(
                "Deterministic no-action control strategy. It mirrors the causal market stream "
                "but never creates a paper ticket."
            ),
            agent_names=("market-mirror",),
            opens_paper_tickets=False,
        ),
        lambda: [MarketMirrorAgent()],
    ),
}


def available_strategies() -> tuple[StrategySpec, ...]:
    return tuple(item[0] for item in _STRATEGIES.values())


def strategy_spec(strategy_id: str) -> StrategySpec:
    try:
        return _STRATEGIES[strategy_id][0]
    except KeyError as exc:
        choices = ", ".join(sorted(_STRATEGIES))
        raise ValueError(f"unknown strategy_id {strategy_id!r}; available: {choices}") from exc


def build_strategy_agents(strategy_id: str) -> list[Agent]:
    # Resolve through the canonical registry first so an arbitrary label can never
    # masquerade as a different runtime implementation in experiment evidence.
    strategy_spec(strategy_id)
    return _STRATEGIES[strategy_id][1]()
