"""Bounded research selector over separately labelled hypothetical returns.

This is an experimental selection rule, not a learner, store, reward authority,
or deployment decision. The caller must release outcomes causally and keep the
shadow observations out of executed-reward memory. No confidence claim is made.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from .transparent_bandit_policy import ActionEstimate


@dataclass(frozen=True, slots=True)
class ShadowSelectionConfig:
    window: int = 200
    minimum_observations: int = 100
    minimum_net_return: str = "0.02"
    cost_bps_per_stake: int = 100

    def __post_init__(self) -> None:
        if type(self.window) is not int or not 1 <= self.window <= 10000:
            raise ValueError("shadow window must be in 1..10000")
        if (type(self.minimum_observations) is not int
                or not 1 <= self.minimum_observations <= self.window):
            raise ValueError("minimum observations must be in 1..window")
        if (type(self.cost_bps_per_stake) is not int
                or not 0 <= self.cost_bps_per_stake <= 10000):
            raise ValueError("cost must be in 0..10000 basis points")
        if type(self.minimum_net_return) is not str or len(self.minimum_net_return) > 32:
            raise ValueError("minimum net return must be a decimal string")
        threshold = Decimal(self.minimum_net_return)
        if not threshold.is_finite() or not 0 <= threshold <= 1:
            raise ValueError("minimum net return must be in 0..1")

    def payload(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__slots__}


def choose_shadow_action(
    history: Mapping[str, Sequence[Decimal]], config: ShadowSelectionConfig,
) -> str:
    """Choose from trailing per-unit returns; WAIT is always a zero-cost option.

    Exact rational ranking reuses the canonical ActionEstimate convention.
    Hypothetical observations never update an executed BanditPolicyState.
    """
    if type(config) is not ShadowSelectionConfig:
        raise TypeError("config must be exact ShadowSelectionConfig")
    config.__post_init__()
    scores: dict[str, Fraction] = {}
    for name, rewards in history.items():
        ActionEstimate(name, 0, Decimal(0))  # canonical action identity validation
        if name == "WAIT":
            raise ValueError("WAIT is an implicit control, not a shadow bet")
        values = tuple(rewards)[-config.window:]
        for value in values:
            if (type(value) is not Decimal or not value.is_finite()
                    or not Decimal(-1) <= value <= Decimal(1000000)):
                raise ValueError("shadow returns must be finite per-unit Decimal returns")
        if len(values) < config.minimum_observations:
            continue
        scores[name] = (sum((Fraction(value) for value in values), Fraction(0))
                        / len(values) - Fraction(config.cost_bps_per_stake, 10000))
    if not scores:
        return "WAIT"
    selected = min(scores, key=lambda name: (-scores[name], name))
    # A tie with WAIT is abstention, including a zero configured threshold.
    return selected if scores[selected] > Fraction(Decimal(config.minimum_net_return)) else "WAIT"
