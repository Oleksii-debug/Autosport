"""Source-level completeness of the native owner-contract review dialog.

An added EconomicGoal field must not crash the keyboard/UIA form with KeyError.
No UI or financial state is mutated here; real NVDA acceptance remains separate.
"""

from __future__ import annotations

from autosport.localization import text
from autosport.owner_economic_authority import OWNER_ECONOMIC_FORM_FIELDS
from autosport.windows_layout import OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS


def test_every_owner_form_field_has_unique_native_automation_id() -> None:
    ids = OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS
    assert not set(OWNER_ECONOMIC_FORM_FIELDS).difference(ids)
    assert all(type(value) is int and value > 0 for value in ids.values())
    assert len(set(ids.values())) == len(ids)


def test_every_native_owner_form_field_has_ukrainian_accessible_name() -> None:
    for field in OWNER_ECONOMIC_FORM_FIELDS:
        key = f"ui.windows.owner_authority.field.{field}"
        label = text(key)
        assert label and label != key, field
        assert any("\u0400" <= char <= "\u04ff" for char in label), field


def test_concentration_and_slippage_fields_do_not_fall_out_of_uia_map() -> None:
    required = {
        "max_event_concentration_fraction",
        "max_market_concentration_fraction",
        "max_provider_concentration_fraction",
        "max_sport_concentration_fraction",
        "max_turnover_fraction",
        "max_execution_slippage_fraction",
    }
    assert required <= set(OWNER_ECONOMIC_FORM_FIELDS)
    assert required <= set(OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS)
