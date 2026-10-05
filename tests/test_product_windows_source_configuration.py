from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.continuous_session import ContinuousSessionStatus, SessionState
from autosport.operator_source_configuration import (
    OperatorSourceConfiguration,
    OperatorSourceConfigurationError,
)
from autosport.operator_source_registry import resolve_product_source_entry
from autosport.product_gui_worker import ProductGuiMessage
from autosport.product_windows_gui import (
    ProductWindowsAutosportApp,
    _bind_product_workspace_environment,
)


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Worker:
    def __init__(self) -> None:
        self.busy = False
        self.started: list[dict[str, object]] = []

    def start(self, **kwargs: object) -> bool:
        self.started.append(kwargs)
        return True


def _configured() -> OperatorSourceConfiguration:
    entry = resolve_product_source_entry("parlayapi-table-tennis")
    return OperatorSourceConfiguration(source_id=entry.source_id, entry=entry)


def _start_surface(tmp_path: Path) -> SimpleNamespace:
    surface = SimpleNamespace()
    surface._closing = False
    surface._product_busy = False
    surface._product_operation_blocked = lambda: False
    surface._workspace_requires_recovery = lambda _workspace: False
    surface.workspace = tmp_path / "workspace"
    display = "Parlay API — настільний теніс"
    surface.product_source = _Value(display)
    surface._product_source_display_to_id = {
        display: "parlayapi-table-tennis"
    }
    surface._product_source_id_to_display = {
        "parlayapi-table-tennis": display
    }
    surface._product_provider_id_to_display = {
        "parlayapi:table_tennis": display
    }
    surface.product_status = _Value()
    surface.status = _Value()
    surface.product_worker = _Worker()
    surface.bell = lambda: None
    surface._hide_uncertain_economic_state = lambda _message: True
    surface._block_workspace_for_recovery = lambda _workspace: None
    surface._restore_base_session_after_product = lambda: True
    surface._set_product_controls_running = lambda _running: None
    surface._append_log = lambda _message: None
    surface.after = lambda *_args: None
    surface._product_last_stop = None
    surface._product_expected_provider_source_id = "parlayapi:table_tennis"
    surface._active_workspace = surface.workspace
    surface._recovery_view = None
    return surface


def test_missing_product_workspace_env_binds_to_gui_workspace(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()

    with patch.dict(os.environ, {}, clear=True):
        _bind_product_workspace_environment(workspace)

        assert os.environ["AUTOSPORT_PRODUCT_WORKSPACE"] == str(workspace)


def test_conflicting_product_workspace_fails_before_runtime_or_session_teardown(
    tmp_path: Path,
) -> None:
    surface = _start_surface(tmp_path)
    teardown_calls: list[str] = []
    surface._hide_uncertain_economic_state = lambda _message: (
        teardown_calls.append("teardown") or True
    )
    conflicting = (tmp_path / "other-workspace").resolve()

    with (
        patch.dict(
            os.environ,
            {"AUTOSPORT_PRODUCT_WORKSPACE": str(conflicting)},
            clear=True,
        ),
        patch(
            "autosport.product_windows_gui.load_operator_source_configuration",
            return_value=_configured(),
        ),
    ):
        ProductWindowsAutosportApp.start_product_runtime(surface)

    assert teardown_calls == []
    assert surface.product_worker.started == []
    assert "не збігається" in surface.product_status.value


def test_changed_gui_workspace_env_fails_before_runtime_side_effects(
    tmp_path: Path,
) -> None:
    surface = _start_surface(tmp_path)
    changed_gui_workspace = (tmp_path / "changed-gui-workspace").resolve()

    with (
        patch.dict(
            os.environ,
            {"AUTOSPORT_WORKSPACE": str(changed_gui_workspace)},
            clear=True,
        ),
        patch(
            "autosport.product_windows_gui.load_operator_source_configuration",
            return_value=_configured(),
        ),
    ):
        ProductWindowsAutosportApp.start_product_runtime(surface)

    assert surface.product_worker.started == []
    assert "не збігається" in surface.product_status.value


def test_start_requires_persisted_source_configuration(tmp_path: Path) -> None:
    surface = _start_surface(tmp_path)

    with patch(
        "autosport.product_windows_gui.load_operator_source_configuration",
        return_value=None,
    ):
        ProductWindowsAutosportApp.start_product_runtime(surface)

    assert surface.product_worker.started == []
    assert "Оберіть і збережіть джерело" in surface.product_status.value


def test_start_rejects_corrupt_source_configuration(tmp_path: Path) -> None:
    surface = _start_surface(tmp_path)

    with patch(
        "autosport.product_windows_gui.load_operator_source_configuration",
        side_effect=OperatorSourceConfigurationError("corrupt"),
    ):
        ProductWindowsAutosportApp.start_product_runtime(surface)

    assert surface.product_worker.started == []
    assert "пошкоджена" in surface.product_status.value


def test_start_rejects_unsaved_display_choice(tmp_path: Path) -> None:
    surface = _start_surface(tmp_path)
    surface.product_source.set("different-unsaved-choice")

    with patch(
        "autosport.product_windows_gui.load_operator_source_configuration",
        return_value=_configured(),
    ):
        ProductWindowsAutosportApp.start_product_runtime(surface)

    assert surface.product_worker.started == []
    assert "ще не збережено" in surface.product_status.value


def test_start_passes_closed_registry_identity_to_product_worker(
    tmp_path: Path,
) -> None:
    surface = _start_surface(tmp_path)
    configured = _configured()

    with patch(
        "autosport.product_windows_gui.load_operator_source_configuration",
        return_value=configured,
    ):
        ProductWindowsAutosportApp.start_product_runtime(surface)

    assert len(surface.product_worker.started) == 1
    call = surface.product_worker.started[0]
    assert call["workspace"] == surface.workspace
    assert call["source_factory"] == configured.entry.factory_spec
    assert (
        call["expected_source_id"]
        == configured.entry.expected_provider_source_id
    )
    assert call["initial_bankroll"] == "10000"
    assert surface._active_workspace == surface.workspace
    assert (
        surface._product_expected_provider_source_id
        == configured.entry.expected_provider_source_id
    )


def _started_message(source_id: str) -> ProductGuiMessage:
    return ProductGuiMessage(
        kind="STARTED",
        status=ContinuousSessionStatus(
            session_id="session-1",
            source_id=source_id,
            state=SessionState.RUNNING,
            cycles_completed=0,
            last_success_at=None,
            last_error_code=None,
            last_full_refresh_at=None,
            settlement_evidence=(),
        ),
    )


def test_started_status_uses_localized_source_label_not_machine_identity(
    tmp_path: Path,
) -> None:
    surface = _start_surface(tmp_path)
    logs: list[str] = []
    surface._append_log = logs.append

    ProductWindowsAutosportApp._apply_product_message(
        surface,
        _started_message("parlayapi:table_tennis"),
    )

    assert "Parlay API — настільний теніс" in surface.product_status.value
    assert "parlayapi:table_tennis" not in surface.product_status.value
    assert logs == [surface.product_status.value]


def test_unexpected_started_source_stops_and_quarantines_without_identity_leak(
    tmp_path: Path,
) -> None:
    surface = _start_surface(tmp_path)
    stop_reasons: list[str] = []
    blocked: list[Path] = []
    logs: list[str] = []
    surface.product_worker.request_stop = lambda reason: (
        stop_reasons.append(reason) or True
    )
    surface._block_workspace_for_recovery = blocked.append
    surface._append_log = logs.append

    ProductWindowsAutosportApp._apply_product_message(
        surface,
        _started_message("unexpected:provider"),
    )

    assert stop_reasons == ["source_identity_mismatch"]
    assert blocked == [surface.workspace]
    assert "unexpected:provider" not in surface.product_status.value
    assert logs == [surface.product_status.value]


def test_known_but_wrong_started_source_is_rejected_against_configured_identity(
    tmp_path: Path,
) -> None:
    surface = _start_surface(tmp_path)
    surface._product_provider_id_to_display["other:known"] = "Інше відоме джерело"
    stop_reasons: list[str] = []
    blocked: list[Path] = []
    surface.product_worker.request_stop = lambda reason: (
        stop_reasons.append(reason) or True
    )
    surface._block_workspace_for_recovery = blocked.append

    ProductWindowsAutosportApp._apply_product_message(
        surface,
        _started_message("other:known"),
    )

    assert stop_reasons == ["source_identity_mismatch"]
    assert blocked == [surface.workspace]
    assert "other:known" not in surface.product_status.value


def test_source_configuration_change_is_blocked_while_runtime_busy(
    tmp_path: Path,
) -> None:
    surface = SimpleNamespace(
        _closing=False,
        _product_busy=True,
        product_status=_Value(),
        status=_Value(),
        product_source=_Value("Parlay API — настільний теніс"),
        _product_source_display_to_id={
            "Parlay API — настільний теніс": "parlayapi-table-tennis"
        },
        _product_source_id_to_display={
            "parlayapi-table-tennis": "Parlay API — настільний теніс"
        },
        workspace=tmp_path / "workspace",
        bell=lambda: None,
        _append_log=lambda _message: None,
    )

    with patch(
        "autosport.product_windows_gui.save_operator_source_configuration"
    ) as save:
        ProductWindowsAutosportApp.save_product_source_configuration(surface)

    save.assert_not_called()
    assert "Не можна змінити джерело" in surface.product_status.value


def test_valid_source_save_uses_stable_registry_identity(tmp_path: Path) -> None:
    surface = SimpleNamespace(
        _closing=False,
        _product_busy=False,
        product_status=_Value(),
        status=_Value(),
        product_source=_Value("Parlay API — настільний теніс"),
        _product_source_display_to_id={
            "Parlay API — настільний теніс": "parlayapi-table-tennis"
        },
        _product_source_id_to_display={
            "parlayapi-table-tennis": "Parlay API — настільний теніс"
        },
        workspace=tmp_path / "workspace",
        bell=lambda: None,
        _append_log=lambda _message: None,
    )
    configured = _configured()

    with patch(
        "autosport.product_windows_gui.save_operator_source_configuration",
        return_value=configured,
    ) as save:
        ProductWindowsAutosportApp.save_product_source_configuration(surface)

    save.assert_called_once_with(
        surface.workspace,
        "parlayapi-table-tennis",
    )
    assert surface.product_source.value == "Parlay API — настільний теніс"
    assert "збережено" in surface.product_status.value


def test_product_stop_restores_prior_active_strategy_session(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    restored_session = object()
    research_plan = object()
    surface = SimpleNamespace(
        _product_close_pending=False,
        session=None,
        _active_strategy_id="research-plan-v1",
        _active_research_plan=research_plan,
        _recovery_view=object(),
        _active_workspace=tmp_path / "stale",
        workspace=tmp_path / "workspace",
        product_status=_Value(),
        status=_Value(),
        bank=_Value(),
        _open_session=lambda strategy_id, plan: (
            calls.append((strategy_id, plan)) or restored_session
        ),
        _bank_text=lambda: "bank",
        _refresh_tickets=lambda: None,
        _block_workspace_for_recovery=lambda _workspace: None,
        _append_log=lambda _message: None,
    )

    assert ProductWindowsAutosportApp._restore_base_session_after_product(surface)

    assert calls == [("research-plan-v1", research_plan)]
    assert surface.session is restored_session
    assert surface._recovery_view is None
