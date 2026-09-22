from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import autosport.gui as gui_module
import autosport.windows_gui as windows_gui_module
from autosport.gui import AutosportApp
from autosport.localization import text
from autosport.windows_gui import WindowsAutosportApp


class _Variable:
    def __init__(self) -> None:
        self._value = ""

    def set(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


class _RecoveryConfigurationHarness:
    def __init__(self) -> None:
        self._closing = False
        self._dataset_busy = False
        self._evidence_export_busy = False
        self._recovery_busy = False
        self.dataset_path = "fixture-dataset"
        self.replay_worker = SimpleNamespace(busy=False)
        self.live_worker = SimpleNamespace(busy=False)
        self.status = _Variable()
        self.logs: list[str] = []

    def _selected_replay_configuration(self):
        raise ValueError("invalid recovery configuration")

    def _append_log(self, message: str) -> None:
        self.logs.append(message)


class RecoveryConfigurationModalOrderingTests(TestCase):
    def _assert_persistent_state_precedes_modal(self, method, module) -> None:
        app = _RecoveryConfigurationHarness()
        observed: dict[str, object] = {}

        def showerror(*_args, **_kwargs) -> None:
            observed["status"] = app.status.get()
            observed["logs"] = tuple(app.logs)

        with patch.object(module.messagebox, "showerror", side_effect=showerror) as modal:
            method(app)

        expected = text("ui.status.recovery.configuration_rejected")
        self.assertEqual(modal.call_count, 1)
        self.assertEqual(observed["status"], expected)
        self.assertEqual(observed["logs"], (expected,))
        self.assertEqual(app.status.get(), expected)
        self.assertEqual(app.logs, [expected])

    def test_replay_gui_publishes_configuration_block_before_modal(self) -> None:
        app = _RecoveryConfigurationHarness()
        observed: dict[str, object] = {}

        def showerror(*_args, **_kwargs) -> None:
            observed["status"] = app.status.get()
            observed["logs"] = tuple(app.logs)

        with patch.object(
            gui_module.messagebox,
            "showerror",
            side_effect=showerror,
        ) as modal:
            AutosportApp.run_dataset(app)

        expected = text("ui.status.replay.configuration_rejected")
        self.assertEqual(modal.call_count, 1)
        self.assertEqual(observed["status"], expected)
        self.assertEqual(observed["logs"], (expected,))
        self.assertEqual(app.status.get(), expected)
        self.assertEqual(app.logs, [expected])

    def test_base_gui_publishes_recovery_block_before_modal(self) -> None:
        self._assert_persistent_state_precedes_modal(
            AutosportApp.repair_workspace,
            gui_module,
        )

    def test_windows_gui_publishes_recovery_block_before_modal(self) -> None:
        self._assert_persistent_state_precedes_modal(
            WindowsAutosportApp.repair_workspace,
            windows_gui_module,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
