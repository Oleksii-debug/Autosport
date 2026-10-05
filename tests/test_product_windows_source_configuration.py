from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.operator_source_configuration import OperatorSourceConfiguration
from autosport.operator_source_registry import resolve_product_source_entry
from autosport.product_windows_gui import ProductWindowsAutosportApp


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
    surface.product_source = _Value("parlayapi-table-tennis")
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
    surface._active_workspace = surface.workspace
    surface._recovery_view = None
    return surface


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
        side_effect=RuntimeError("should be mapped by configured loader"),
    ):
        try:
            ProductWindowsAutosportApp.start_product_runtime(surface)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unexpected broad exception suppression")


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


def test_source_configuration_change_is_blocked_while_runtime_busy(
    tmp_path: Path,
) -> None:
    surface = SimpleNamespace(
        _closing=False,
        _product_busy=True,
        product_status=_Value(),
        status=_Value(),
        product_source=_Value("parlayapi-table-tennis"),
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
        product_source=_Value("parlayapi-table-tennis"),
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
    assert surface.product_source.value == "parlayapi-table-tennis"
    assert "збережено" in surface.product_status.value
