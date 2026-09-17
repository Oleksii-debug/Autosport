import inspect

import pytest

from autosport import windows_layout
from autosport.localization import CATALOG_VERSION, DEFAULT_LOCALE, catalog, text
from autosport.windows_layout import WINDOWS_SHELL_LOCALIZATION_KEYS


def test_windows_shell_catalog_extends_one_versioned_ukrainian_boundary():
    assert DEFAULT_LOCALE == "uk-UA"
    assert CATALOG_VERSION == 3
    assert WINDOWS_SHELL_LOCALIZATION_KEYS <= set(catalog())
    assert text("ui.windows.shell.frame.title") == "Навігація продукту"
    assert text("ui.windows.shell.screen.label") == "Екран:"
    assert text("ui.windows.shell.button.open") == "Перейти до робочої поверхні"
    assert text("ui.windows.shell.state.disabled", reason="тимчасово недоступно") == (
        "Вимкнено: тимчасово недоступно"
    )
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
