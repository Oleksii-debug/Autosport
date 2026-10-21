"""Plan 6 §3А: WebView navigation must not falsely acknowledge lost UI state."""
from __future__ import annotations

from unittest.mock import patch

from autosport.windows_surface_contract import SURFACE_BY_KEY, load_surface_selection
from autosport.windows_webview_shell import AutosportWebController


def _choose_other(current: str) -> str:
    return next(key for key in SURFACE_BY_KEY if key != current)


def _select(controller: AutosportWebController, key: object, request: str) -> dict:
    return controller.dispatch({
        "request_id": request,
        "action_id": "surface.select",
        "payload": {"surface_key": key},
    })


def test_persistence_failure_does_not_change_current_or_restart_selection(tmp_path):
    controller = AutosportWebController(tmp_path)
    try:
        original = controller.surface_key
        next_screen = _choose_other(original)
        with patch(
            "autosport.windows_webview_shell.save_surface_selection",
            return_value=False,
        ) as save:
            refused = _select(controller, next_screen, "surface-persist-fail-1")
        save.assert_called_once_with(tmp_path, next_screen)
        assert refused["status"] == "rejected"
        assert "Не вдалося зберегти" in refused["message"]
        assert controller.surface_key == original
        assert load_surface_selection(tmp_path) == original
    finally:
        controller.close()


def test_successful_selection_is_durable_and_failed_followup_preserves_it(tmp_path):
    controller = AutosportWebController(tmp_path)
    try:
        selected = _choose_other(controller.surface_key)
        success = _select(controller, selected, "surface-persist-ok-1")
        assert success["status"] != "rejected", success
        assert controller.surface_key == selected
        assert load_surface_selection(tmp_path) == selected
        failed_target = _choose_other(selected)
        with patch(
            "autosport.windows_webview_shell.save_surface_selection",
            return_value=False,
        ):
            refusal = _select(controller, failed_target, "surface-persist-fail-2")
        assert refusal["status"] == "rejected"
        assert controller.surface_key == selected
        assert load_surface_selection(tmp_path) == selected
    finally:
        controller.close()


def test_malformed_type_does_not_call_persistence(tmp_path):
    controller = AutosportWebController(tmp_path)
    try:
        previous = controller.surface_key
        with patch("autosport.windows_webview_shell.save_surface_selection") as save:
            refusal = _select(controller, ["not-a-surface"], "surface-bad-type")
        assert refusal["status"] == "rejected"
        save.assert_not_called()
        assert controller.surface_key == previous
    finally:
        controller.close()
