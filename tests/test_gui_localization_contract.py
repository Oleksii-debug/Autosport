from __future__ import annotations

import inspect

from autosport.gui import (
    AUTOMATION_IDS,
    _LIVE_MODES,
    _SPEEDS,
    _STRATEGY_CHOICES,
    AutosportApp,
    strategy_id_from_display,
)
from autosport.localization import require_keys, text
from autosport.windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID


_CRITICAL_GUI_KEYS = {
    "ui.app.title",
    "ui.label.strategy",
    "ui.label.speed",
    "ui.label.live_mode",
    "ui.label.live_quotes",
    "ui.label.tickets",
    "ui.label.evaluation",
    "ui.label.log",
    "ui.button.research_plan",
    "ui.button.choose_dataset",
    "ui.button.run_replay",
    "ui.button.repair_workspace",
    "ui.button.live_refresh",
    "ui.speed.event_driven",
    "ui.speed.realtime",
    "ui.speed.10x",
    "ui.speed.100x",
    "ui.speed.1000x",
    "ui.live_mode.public_preview",
    "ui.live_mode.api_key",
    "ui.strategy.display",
    "ui.status.startup.ready",
    "ui.status.startup.recovery_required",
    "ui.status.dataset.none",
    "ui.status.research_plan.baseline",
    "ui.status.live.never",
    "ui.status.live_quotes.empty",
    "ui.status.evaluation.empty",
    "ui.accessibility.strategy.name",
    "ui.accessibility.strategy.description",
    "ui.accessibility.research_plan.name",
    "ui.accessibility.research_plan.description",
    "ui.accessibility.choose_dataset.name",
    "ui.accessibility.choose_dataset.description",
    "ui.accessibility.run_replay.name",
    "ui.accessibility.run_replay.description",
    "ui.accessibility.repair_workspace.name",
    "ui.accessibility.repair_workspace.description",
    "ui.accessibility.replay_speed.name",
    "ui.accessibility.replay_speed.description",
    "ui.accessibility.live_mode.name",
    "ui.accessibility.live_mode.description",
    "ui.accessibility.live_refresh.name",
    "ui.accessibility.live_refresh.description",
    "ui.accessibility.live_quotes.name",
    "ui.accessibility.live_quotes.description",
    "ui.accessibility.tickets.name",
    "ui.accessibility.tickets.description",
    "ui.accessibility.evaluation.name",
    "ui.accessibility.evaluation.description",
    "ui.accessibility.log.name",
    "ui.accessibility.log.description",
    "ui.accessibility.bankroll.name",
    "ui.accessibility.bankroll.description",
}


def test_critical_gui_catalog_is_complete_and_ukrainian_first() -> None:
    require_keys(_CRITICAL_GUI_KEYS)

    assert text("ui.app.title") == "Автоспорт — V1 лабораторія паперового моделювання для Windows"
    assert text("ui.button.choose_dataset") == "Вибрати набір даних"
    assert text("ui.button.run_replay") == "Запустити паперовий повтор"
    assert text("ui.button.repair_workspace") == "Відновити робочу область"
    assert text("ui.accessibility.strategy.name") == "Стратегія повтору"
    assert text("ui.accessibility.bankroll.name") == "Віртуальний банк"


def test_localized_combo_labels_keep_typed_semantic_payloads_stable() -> None:
    # Localization owns display text only. Typed values remain semantic authority.
    assert list(_SPEEDS.values()) == [0.0, 1.0, 10.0, 100.0, 1000.0]
    assert list(_LIVE_MODES.values()) == [True, False]
    assert set(_STRATEGY_CHOICES.values()) == {
        "baseline-v1",
        "observe-only-v1",
        "research-replay-v1",
    }
    for display, strategy_id in _STRATEGY_CHOICES.items():
        assert strategy_id_from_display(display) == strategy_id

    localized_speeds = {
        text("ui.speed.event_driven"): 0.0,
        text("ui.speed.realtime"): 1.0,
        text("ui.speed.10x"): 10.0,
        text("ui.speed.100x"): 100.0,
        text("ui.speed.1000x"): 1000.0,
    }
    localized_live_modes = {
        text("ui.live_mode.public_preview"): True,
        text("ui.live_mode.api_key"): False,
    }
    assert list(localized_speeds.values()) == list(_SPEEDS.values())
    assert list(localized_live_modes.values()) == list(_LIVE_MODES.values())


def test_accessibility_ids_and_keyboard_bindings_are_identity_fences() -> None:
    assert AUTOMATION_IDS == {
        "choose_dataset": 101,
        "run_replay": 102,
        "replay_speed": 103,
        "live_mode": 104,
        "live_refresh": 105,
        "strategy": 106,
        "research_plan": 107,
        "repair_workspace": 108,
        "tickets": 201,
        "log": 202,
        "live_quotes": 203,
        "evaluation": 204,
    }
    assert WINDOWS_BANKROLL_AUTOMATION_ID == 205

    build_source = inspect.getsource(AutosportApp._build)
    for binding in (
        "<Control-o>",
        "<Control-r>",
        "<Control-Shift-R>",
        "<Control-l>",
        "<F6>",
        "<F7>",
        "<F8>",
    ):
        assert binding in build_source
