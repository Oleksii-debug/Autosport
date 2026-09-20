from __future__ import annotations

from pathlib import Path

import pytest

from autosport.economic_goal import AutomationLevel
from autosport.economic_goal_store import EconomicGoalStore
from autosport.workspace_lock import WorkspaceEconomicLock
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    OwnerEconomicAuthorityError,
    OwnerEconomicAuthorityService,
    build_initial_owner_contract,
)


def _values(**changes: str) -> dict[str, str]:
    return {**INITIAL_OWNER_FORM_DEFAULTS, **changes}


def test_owner_authority_absent_contract_can_be_confirmed_once_and_read_back_after_restart(
    tmp_path: Path,
) -> None:
    service = OwnerEconomicAuthorityService(tmp_path)
    absent = service.read_view()
    assert absent.state == "absent"
    assert absent.can_initialize is True

    persisted = service.initialize_from_form(
        _values(goal_id="owner-2026", bankroll_id="research-paper", currency="EUR"),
        emergency_stop=True,
        confirmed=True,
    )

    assert persisted.state == "valid"
    assert persisted.contract is not None
    assert persisted.contract.goal_id == "owner-2026"
    assert persisted.contract.currency == "EUR"
    assert persisted.contract.emergency_stop is True
    assert any("ревізія: 1" in line for line in persisted.lines_uk)
    assert any("Стеля автоматизації" in line and "аварійна STOP: так" in line for line in persisted.lines_uk)
    assert any("Максимальна концентрація постачальника" in line for line in persisted.lines_uk)
    assert any("Заборонені ринки" in line and line.endswith("немає") for line in persisted.lines_uk)

    restarted = OwnerEconomicAuthorityService(tmp_path).read_view()
    assert restarted.state == "valid"
    assert restarted.contract == persisted.contract
    assert restarted.lines_uk == persisted.lines_uk


def test_confirmation_cancel_never_creates_a_contract(tmp_path: Path) -> None:
    service = OwnerEconomicAuthorityService(tmp_path)

    with pytest.raises(OwnerEconomicAuthorityError, match="явне підтвердження"):
        service.initialize_from_form(_values(), emergency_stop=False, confirmed=False)

    assert service.read_view().state == "absent"
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()


def test_initial_form_rejects_unexpected_keys_without_silently_discarding_them(
    tmp_path: Path,
) -> None:
    service = OwnerEconomicAuthorityService(tmp_path)

    with pytest.raises(OwnerEconomicAuthorityError, match="Неприпустимі поля"):
        service.initialize_from_form(
            {**_values(), "unreviewed_limit": "0"},
            emergency_stop=False,
            confirmed=True,
        )

    assert service.read_view().state == "absent"
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_stake_fraction", "NaN"),
        ("max_day_loss_fraction", "Infinity"),
        ("max_quote_age_seconds", "1e2"),
        ("max_concurrent_positions", "0"),
    ),
)
def test_initial_form_rejects_nonfinite_or_noncanonical_values_without_writing(
    tmp_path: Path, field: str, value: str
) -> None:
    service = OwnerEconomicAuthorityService(tmp_path)

    with pytest.raises(OwnerEconomicAuthorityError):
        service.initialize_from_form(_values(**{field: value}), emergency_stop=False, confirmed=True)

    assert service.read_view().state == "absent"
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()


def test_existing_contract_is_read_only_and_duplicate_initialization_preserves_it(
    tmp_path: Path,
) -> None:
    service = OwnerEconomicAuthorityService(tmp_path)
    original = service.initialize_from_form(
        _values(goal_id="first-owner", max_stake_fraction="0.02"),
        emergency_stop=False,
        confirmed=True,
    )

    with pytest.raises(OwnerEconomicAuthorityError, match="лише коли збереження відсутнє"):
        service.initialize_from_form(
            _values(goal_id="replacement", max_stake_fraction="0.90"),
            emergency_stop=False,
            confirmed=True,
        )

    restored = EconomicGoalStore(tmp_path).load()
    assert restored == original.contract
    assert restored.goal_id == "first-owner"
    assert restored.max_stake_fraction == build_initial_owner_contract(
        _values(goal_id="first-owner"), emergency_stop=False
    ).max_stake_fraction


def test_corrupt_contract_is_visible_but_never_overwritten_by_owner_initialization(
    tmp_path: Path,
) -> None:
    path = tmp_path / EconomicGoalStore.FILE_NAME
    path.write_text("{broken", encoding="utf-8")
    service = OwnerEconomicAuthorityService(tmp_path)

    view = service.read_view()
    assert view.state == "corrupt"
    assert view.can_initialize is False
    before = path.read_text(encoding="utf-8")
    with pytest.raises(OwnerEconomicAuthorityError, match="не дає права на запис"):
        service.initialize_from_form(_values(), emergency_stop=False, confirmed=True)
    assert path.read_text(encoding="utf-8") == before


def test_owner_initialization_persists_only_authority_even_with_supervised_ceiling(
    tmp_path: Path,
) -> None:
    service = OwnerEconomicAuthorityService(tmp_path)
    persisted = service.initialize_from_form(
        _values(
            automation_level=str(int(AutomationLevel.SUPERVISED_EXECUTION)),
        ),
        emergency_stop=False,
        confirmed=True,
    )

    assert persisted.state == "valid"
    assert persisted.contract is not None
    assert persisted.contract.automation_level is AutomationLevel.SUPERVISED_EXECUTION
    expected_files = {
        EconomicGoalStore.FILE_NAME,
        WorkspaceEconomicLock.FILE_NAME,
    }
    allowed_lock_sidecars = {f".{name}.lock" for name in expected_files}
    actual_files = {path.name for path in tmp_path.iterdir()}
    assert actual_files - allowed_lock_sidecars == expected_files
    assert actual_files <= expected_files | allowed_lock_sidecars
