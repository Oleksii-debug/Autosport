from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .agents import (
    Agent,
    MarketMirrorAgent,
    PaperBaselineAgent,
    agent_composition_sha256,
    validate_agent_names,
)
from .research_pipeline import ResearchDecisionPipeline
from .research_strategy import (
    RESEARCH_STRATEGY_ID,
    ResearchReplayAgent,
    ResearchStrategyPlan,
)
from .risk import PaperRiskPolicy


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Canonical strategy identity bound to the exact runtime agents it executes."""

    strategy_id: str
    label: str
    description: str
    agent_names: tuple[str, ...]
    opens_paper_tickets: bool
    requires_research_plan: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_names", validate_agent_names(self.agent_names))

    @property
    def agent_composition_sha256(self) -> str:
        return agent_composition_sha256(self.agent_names)


StrategyFactory = Callable[[ResearchStrategyPlan | None], list[Agent]]


def _build_research_strategy(plan: ResearchStrategyPlan | None) -> list[Agent]:
    if plan is None:
        raise ValueError("research-replay-v1 requires a research strategy plan")
    return [MarketMirrorAgent(), ResearchReplayAgent(plan)]


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
        lambda _plan: [MarketMirrorAgent(), PaperBaselineAgent("50")],
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
        lambda _plan: [MarketMirrorAgent()],
    ),
    RESEARCH_STRATEGY_ID: (
        StrategySpec(
            strategy_id=RESEARCH_STRATEGY_ID,
            label="Typed research replay",
            description=(
                "Deterministic paper-only research path binding causal replay quotes to typed "
                "evidence, ForecastRecord, critic, portfolio impact and PaperRiskPolicy."
            ),
            agent_names=("market-mirror", "research-replay-pipeline"),
            opens_paper_tickets=True,
            requires_research_plan=True,
        ),
        _build_research_strategy,
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


def validate_strategy_configuration(
    strategy_id: str,
    research_plan: ResearchStrategyPlan | None,
) -> StrategySpec:
    spec = strategy_spec(strategy_id)
    if spec.requires_research_plan and research_plan is None:
        raise ValueError(f"{strategy_id} requires --research-plan")
    if not spec.requires_research_plan and research_plan is not None:
        raise ValueError(f"{strategy_id} does not accept a research strategy plan")
    return spec


def experiment_strategy_id(
    strategy_id: str,
    research_plan: ResearchStrategyPlan | None,
) -> str:
    spec = validate_strategy_configuration(strategy_id, research_plan)
    if research_plan is None:
        return spec.strategy_id
    return research_plan.experiment_strategy_id


def build_strategy_agents(
    strategy_id: str,
    *,
    research_plan: ResearchStrategyPlan | None = None,
    risk_policy: PaperRiskPolicy | None = None,
) -> list[Agent]:
    spec = validate_strategy_configuration(strategy_id, research_plan)
    if risk_policy is not None and not isinstance(risk_policy, PaperRiskPolicy):
        raise TypeError("risk_policy must be a PaperRiskPolicy or None")
    goal_active = risk_policy is not None and risk_policy.economic_goal is not None
    if goal_active and spec.strategy_id == "baseline-v1":
        raise ValueError(
            "baseline-v1 does not have proven EconomicGoal-aware sizing semantics"
        )
    if spec.strategy_id == RESEARCH_STRATEGY_ID and risk_policy is not None:
        assert research_plan is not None
        agents = [
            MarketMirrorAgent(),
            ResearchReplayAgent(
                research_plan,
                ResearchDecisionPipeline(risk_policy=risk_policy),
            ),
        ]
    else:
        agents = _STRATEGIES[spec.strategy_id][1](research_plan)
    actual_names = validate_agent_names(getattr(agent, "name", None) for agent in agents)
    if actual_names != spec.agent_names:
        raise RuntimeError(
            "strategy runtime agent composition does not match canonical StrategySpec: "
            f"expected={spec.agent_names!r} actual={actual_names!r}"
        )
    return agents
