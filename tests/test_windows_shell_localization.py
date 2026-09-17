import inspect

import pytest

from autosport import windows_layout, windows_surface_contract
from autosport.localization import CATALOG_VERSION, DEFAULT_LOCALE, catalog, text
from autosport.windows_layout import WINDOWS_SHELL_LOCALIZATION_KEYS
from autosport.windows_surface_contract import SURFACE_BY_KEY, surface_detail_lines


WINDOWS_SURFACE_LOCALIZATION_KEYS = frozenset(
    {
        "ui.windows.surface.phase.active",
        "ui.windows.surface.phase.disabled",
        "ui.windows.surface.phase.presentation",
        "ui.windows.surface.detail.purpose",
        "ui.windows.surface.detail.primary_task",
        "ui.windows.surface.detail.controls",
        "ui.windows.surface.detail.focus_entry",
        "ui.windows.surface.detail.focus_exit",
        "ui.windows.surface.detail.accessibility",
        "ui.windows.surface.detail.states",
        "ui.windows.surface.detail.confirmation",
        "ui.windows.surface.detail.truth_boundary",
        "ui.windows.surface.detail.restart",
        "ui.windows.surface.detail.blocked_reason",
    }
)


def test_windows_shell_catalog_extends_one_versioned_ukrainian_boundary():
    assert DEFAULT_LOCALE == "uk-UA"
    assert CATALOG_VERSION == 4
    assert WINDOWS_SHELL_LOCALIZATION_KEYS <= set(catalog())
    assert WINDOWS_SURFACE_LOCALIZATION_KEYS <= set(catalog())
    assert text("ui.windows.shell.frame.title") == "Навігація продукту"
    assert text("ui.windows.shell.screen.label") == "Екран:"
    assert text("ui.windows.shell.button.open") == "Перейти до робочої поверхні"
    assert text("ui.windows.shell.state.disabled", reason="тимчасово недоступно") == (
        "Вимкнено: тимчасово недоступно"
    )
    assert text("ui.windows.surface.detail.truth_boundary") == "Межа істини"
    # Existing v2 presentation truth is still reached through the same public API.
    assert text("ui.app.title").startswith("Автоспорт")


def test_windows_shell_catalog_is_fail_closed_and_immutable():
    with pytest.raises(KeyError, match="missing localization key"):
        text("ui.windows.shell.missing")
    with pytest.raises(KeyError, match="missing localization value 'reason'"):
        text("ui.windows.shell.state.disabled")
    with pytest.raises(TypeError):
        catalog()["ui.windows.shell.frame.title"] = "mutated"  # type: ignore[index]


def test_windows_shell_chrome_and_uia_strings_are_read_from_catalog():
    source = inspect.getsource(windows_layout)
    for key in WINDOWS_SHELL_LOCALIZATION_KEYS:
        assert key in source

    # These critical strings used to be literals in windows_layout.py. Keeping
    # them out of the shell source prevents a silent English/Ukrainian fallback
    # from bypassing the canonical catalog during later UI work.
    for literal in (
        "Навігація продукту",
        "Екран:",
        "Активна V1-поверхня",
        "Навігація екранами Автоспорт",
        "Стан вибраної поверхні",
        "Контракт вибраного екрана",
    ):
        assert literal not in source


def test_windows_surface_detail_labels_are_read_from_catalog():
    active = surface_detail_lines(SURFACE_BY_KEY["home_dashboard"])
    disabled = surface_detail_lines(SURFACE_BY_KEY["bookmakers_accounts"])

    assert active[0] == text("ui.windows.surface.phase.active")
    assert active[1].startswith(f"{text('ui.windows.surface.detail.purpose')}: ")
    assert active[-1].startswith(f"{text('ui.windows.surface.detail.restart')}: ")
    assert disabled[0] == text("ui.windows.surface.phase.disabled")
    assert disabled[-1].startswith(f"{text('ui.windows.surface.detail.blocked_reason')}: ")

    source = inspect.getsource(windows_surface_contract.surface_detail_lines)
    for key in WINDOWS_SURFACE_LOCALIZATION_KEYS:
        assert key in source

    for literal in (
        "СТАН: V1 — активна поверхня",
        "СТАН: видима, але функція вимкнена до активації capability",
        "СТАН: лише інформаційна поверхня",
        "Призначення:",
        "Основна дія:",
        "Підтвердження/скасування:",
        "Межа істини:",
        "Чому вимкнено:",
    ):
        assert literal not in source
