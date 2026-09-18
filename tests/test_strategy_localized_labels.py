from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.gui import _STRATEGY_CHOICES, AutosportApp
from autosport.strategies import strategy_spec


@pytest.mark.parametrize(
    ("strategy_id", "expected_label", "legacy_english_label", "expected_agents"),
    (
        (
            "baseline-v1",
            "Базова стратегія тестового сценарію",
            "Fixture baseline",
            ("market-mirror", "paper-baseline"),
        ),
        (
            "observe-only-v1",
            "Лише спостереження",
            "Observe only",
            ("market-mirror",),
        ),
        (
            "research-replay-v1",
            "Типізований дослідницький повтор",
            "Typed research replay",
            ("market-mirror", "research-replay-pipeline"),
        ),
    ),
)
def test_strategy_status_uses_ukrainian_label_without_changing_identity(
    strategy_id: str,
    expected_label: str,
    legacy_english_label: str,
    expected_agents: tuple[str, ...],
) -> None:
    spec = strategy_spec(strategy_id)
    display = next(
        label for label, semantic_id in _STRATEGY_CHOICES.items()
        if semantic_id == strategy_id
    )
    app = SimpleNamespace(
        strategy_text=SimpleNamespace(get=lambda: display),
    )

    rendered = AutosportApp._strategy_status_text(app)

    assert display == f"Стратегія {strategy_id}"
    assert _STRATEGY_CHOICES[display] == strategy_id
    assert spec.strategy_id == strategy_id
    assert spec.agent_names == expected_agents
    assert spec.label == legacy_english_label
    assert strategy_id in rendered
    assert expected_label in rendered
    assert legacy_english_label not in rendered
