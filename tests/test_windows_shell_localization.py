import inspect
import re

import pytest

from autosport import windows_layout, windows_surface_contract
from autosport.localization import CATALOG_VERSION, DEFAULT_LOCALE, catalog, text
from autosport.windows_layout import WINDOWS_SHELL_LOCALIZATION_KEYS
from autosport.windows_surface_contract import SURFACES, SURFACE_BY_KEY, surface_detail_lines


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

WINDOWS_SURFACE_CONTENT_FIELDS = {
    "title_uk": "title",
    "purpose_uk": "purpose",
    "primary_task_uk": "primary_task",
    "controls_uk": "controls",
    "focus_entry_uk": "focus_entry",
    "focus_exit_uk": "focus_exit",
    "accessibility_uk": "accessibility",
    "transient_states_uk": "states",
    "confirmation_uk": "confirmation",
    "authority_uk": "authority",
    "persistence_uk": "persistence",
}

_ASCII_PRESENTATION_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_ALLOWED_SHORTCUT_OR_PLATFORM_TOKENS = frozenset(
    {
        "Alt",
        "Control",
        "Ctrl",
        "F2",
        "F6",
        "F7",
        "F8",
        "Left",
        "R",
        "Right",
        "Shift",
        "Tab",
        "Tk",
        "UIA",
        "Windows",
    }
)


def _assert_no_unexplained_english(value: str) -> None:
    tokens = set(_ASCII_PRESENTATION_TOKEN.findall(value))
    unexpected = sorted(tokens - _ALLOWED_SHORTCUT_OR_PLATFORM_TOKENS)
    assert unexpected == [], f"unexplained English presentation tokens: {unexpected!r} in {value!r}"


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


def test_windows_public_shell_copy_does_not_present_v1_as_a_separate_stage():
    for key in (
        "ui.windows.shell.state.active",
        "ui.windows.surface.phase.active",
    ):
        assert "V1" not in text(key)


def test_windows_surface_phase_contract_is_stage_neutral():
    phases = {surface.phase for surface in SURFACES}
    assert phases <= {"active", "visible-disabled", "presentation-only"}
    assert "v1-active" not in inspect.getsource(windows_surface_contract)
    assert "Windows V1" not in inspect.getsource(windows_layout)


def test_windows_surface_content_is_resolved_from_central_catalog():
    public_catalog = catalog()
    expected_keys = set()

    for surface in SURFACES:
        for attribute, field in WINDOWS_SURFACE_CONTENT_FIELDS.items():
            key = f"ui.windows.surface.{surface.key}.{field}"
            expected_keys.add(key)
            value = getattr(surface, attribute)
            assert value == text(key)
            assert "V1" not in value

        blocked_key = f"ui.windows.surface.{surface.key}.blocked_reason"
        if surface.blocked_reason_uk is None:
            assert blocked_key not in public_catalog
        else:
            expected_keys.add(blocked_key)
            assert surface.blocked_reason_uk == text(blocked_key)
            assert "V1" not in surface.blocked_reason_uk

    assert expected_keys <= set(public_catalog)


def test_bookmakers_accounts_copy_distinguishes_read_capability_from_unwired_surface():
    surface = SURFACE_BY_KEY["bookmakers_accounts"]
    assert surface.phase == "visible-disabled"
    assert surface.blocked_reason_uk is not None
    blocked_reason = surface.blocked_reason_uk.casefold()
    assert "читання даних рахунку букмекера підтримується" in blocked_reason
    assert "поверхня windows ще не підключена" in blocked_reason
    assert "реальне виконання ставок вимкнене" in blocked_reason
    assert "модуль можливостей букмекера й рахунку" not in blocked_reason


def test_windows_surface_content_has_no_unexplained_english_presentation_tokens():
    for surface in SURFACES:
        for attribute in WINDOWS_SURFACE_CONTENT_FIELDS:
            _assert_no_unexplained_english(getattr(surface, attribute))
        if surface.blocked_reason_uk is not None:
            _assert_no_unexplained_english(surface.blocked_reason_uk)


def test_windows_surface_contract_contains_no_product_owned_presentation_copy():
    source = inspect.getsource(windows_surface_contract)

    # Stable surface identifiers/targets/phases stay in this language-neutral
    # contract. User/NVDA-facing copy must live behind autosport.localization.
    for legacy_literal in (
        "Головна / Огляд",
        "Market Mirror / Живі події",
        "Дослідження / Агенти",
        "Можливості",
        "Портфель",
        "Паперовий банк",
        "Квитки / Позиції",
        "Оцінювання / Навчання",
        "Букмекери / Акаунти",
        "Історія / Результати",
        "Налаштування",
        "Діагностика / Відновлення",
        "Довідка / Про програму",
        "Opportunity/portfolio planning runtime ще не активований у V1.",
        "Bookmaker capability/account runtime та supervised execution ще не активовані.",
        "Єдиний typed settings editor ще не реалізований",
    ):
        assert legacy_literal not in source
