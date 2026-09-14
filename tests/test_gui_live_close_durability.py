from __future__ import annotations

import unittest
from types import SimpleNamespace

from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class GuiLiveCloseDurabilityTests(unittest.TestCase):
    def _app(self) -> WindowsAutosportApp:
        app = object.__new__(WindowsAutosportApp)
        app._closing = False
        app.recovery_worker = SimpleNamespace(busy=False)
        app.replay_worker = SimpleNamespace(busy=False)
        app.live_worker = SimpleNamespace(busy=True)
        app.session = _Session()
        app.status = _Value()
        app.live_status = _Value()
        app._logs = []
        app._bell_rang = False
        app._destroyed = False
        app._append_log = lambda text: app._logs.append(text)
        app.bell = lambda: setattr(app, "_bell_rang", True)
        app.destroy = lambda: setattr(app, "_destroyed", True)
        return app

    def test_packaged_gui_blocks_close_until_live_observation_is_terminal(self) -> None:
        app = self._app()
        session = app.session

        WindowsAutosportApp.close_app(app)

        self.assertFalse(app._closing)
        self.assertIs(app.session, session)
        self.assertFalse(session.closed)
        self.assertFalse(app._destroyed)
        self.assertTrue(app._bell_rang)
        self.assertIn("Live snapshot", app.status.value)
        self.assertIn("Закриття програми заблоковано", app.live_status.value)
        self.assertTrue(any("persistence boundary" in line for line in app._logs))

        app.live_worker.busy = False
        WindowsAutosportApp.close_app(app)

        self.assertTrue(app._closing)
        self.assertIsNone(app.session)
        self.assertTrue(session.closed)
        self.assertTrue(app._destroyed)


if __name__ == "__main__":
    unittest.main()
