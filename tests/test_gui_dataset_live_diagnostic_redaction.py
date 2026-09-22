from __future__ import annotations

from pathlib import Path

from autosport.dataset_worker import DatasetValidationMessage
from autosport.gui import AutosportApp
from autosport.live_observation import ObservationWorkerMessage
import autosport.gui as gui


_DATASET_SECRET = "Authorization: Bearer AUTOSPORT-DATASET-SECRET-SENTINEL"
_LIVE_SECRET = "X-Api-Key: AUTOSPORT-LIVE-SECRET-SENTINEL"


class _ValueSink:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set(self, value: str) -> None:
        self.values.append(value)


class _Button:
    def __init__(self) -> None:
        self.states: list[tuple[str, ...]] = []

    def state(self, values) -> None:
        self.states.append(tuple(values))


class _OneMessageWorker:
    def __init__(self, message) -> None:
        self._message = message

    def poll(self):
        return self._message


class _DatasetPollHarness:
    def __init__(self, error: str) -> None:
        self._closing = False
        self.dataset_worker = _OneMessageWorker(
            DatasetValidationMessage(error=error)
        )
        self._pending_dataset_path = Path("dataset-root")
        self.status = _ValueSink()
        self.log: list[str] = []
        self.busy_transitions: list[bool] = []

    def _set_replay_controls_busy(self, busy: bool) -> None:
        self.busy_transitions.append(busy)

    def _append_log(self, value: str) -> None:
        self.log.append(value)


class _LivePollHarness:
    def __init__(self, error: str) -> None:
        self._closing = False
        self.live_worker = _OneMessageWorker(
            ObservationWorkerMessage(error=error)
        )
        self.live_refresh_button = _Button()
        self.live_status = _ValueSink()
        self.status = _ValueSink()
        self.log: list[str] = []

    def _append_log(self, value: str) -> None:
        self.log.append(value)


def _dataset_render(
    app: _DatasetPollHarness,
    dialogs: list[tuple[str, str]],
) -> str:
    return "\n".join(
        app.log
        + app.status.values
        + [title for title, _message in dialogs]
        + [message for _title, message in dialogs]
    )


def _live_render(app: _LivePollHarness) -> str:
    return "\n".join(app.log + app.live_status.values + app.status.values)


def test_dataset_worker_raw_secret_never_reaches_operator_surfaces(monkeypatch) -> None:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
    app = _DatasetPollHarness(_DATASET_SECRET)

    AutosportApp._poll_dataset_worker(app)

    assert app._pending_dataset_path is None
    assert app.busy_transitions == [False]
    assert dialogs
    rendered = _dataset_render(app, dialogs)
    for forbidden in (
        "AUTOSPORT-DATASET-SECRET-SENTINEL",
        "Authorization",
        "Bearer",
    ):
        assert forbidden not in rendered
    assert "DATASET_VALIDATION_FAILURE" in rendered
    assert "Набір даних відхилено" in rendered

    dialogs.clear()
    other = _DatasetPollHarness("password=DIFFERENT-DATASET-DIAGNOSTIC")
    AutosportApp._poll_dataset_worker(other)
    rendered_other = _dataset_render(other, dialogs)

    assert rendered_other == rendered
    assert "DIFFERENT-DATASET-DIAGNOSTIC" not in rendered_other
    assert "password" not in rendered_other.casefold()


def test_live_worker_raw_secret_never_reaches_operator_surfaces() -> None:
    app = _LivePollHarness(_LIVE_SECRET)

    AutosportApp._poll_live_worker(app)

    assert app.live_refresh_button.states == [("!disabled",)]
    rendered = _live_render(app)
    for forbidden in (
        "AUTOSPORT-LIVE-SECRET-SENTINEL",
        "X-Api-Key",
    ):
        assert forbidden not in rendered
    assert "LIVE_OBSERVATION_FAILURE" in rendered
    assert "Помилка поточного знімка" in rendered

    other = _LivePollHarness("token=DIFFERENT-LIVE-DIAGNOSTIC")
    AutosportApp._poll_live_worker(other)
    rendered_other = _live_render(other)

    assert rendered_other == rendered
    assert "DIFFERENT-LIVE-DIAGNOSTIC" not in rendered_other
    assert "token=" not in rendered_other.casefold()
