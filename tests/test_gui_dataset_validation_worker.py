from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.dataset import ReplayDataset
from autosport.dataset_worker import (
    DatasetValidationMessage,
    OneShotDatasetValidationWorker,
)
from autosport.gui import AutosportApp
from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _TerminalWorker:
    def __init__(self, message: DatasetValidationMessage) -> None:
        self.busy = True
        self._message = message

    def poll(self) -> DatasetValidationMessage | None:
        message = self._message
        self._message = None
        self.busy = False
        return message


class _Control:
    def __init__(self) -> None:
        self.operations: list[tuple[str, object]] = []

    def state(self, values) -> None:
        self.operations.append(("state", tuple(values)))

    def configure(self, **values) -> None:
        self.operations.append(("configure", values))


def _dataset(root: Path) -> ReplayDataset:
    return ReplayDataset(
        root=root,
        name="responsive corpus",
        sport="table_tennis",
        market_path=root / "markets.jsonl",
        results_path=root / "results.json",
        market_sha256="a" * 64,
        results_sha256="b" * 64,
    )


def _bare_app(
    *,
    dataset_worker,
    previous_dataset: Path | None = None,
) -> AutosportApp:
    app = object.__new__(AutosportApp)
    app._closing = False
    app.dataset_worker = dataset_worker
    app._pending_dataset_path = None
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.dataset_path = previous_dataset
    app.status = _Value()
    app.dataset_text = _Value("previous dataset summary")
    app.live_status = _Value()
    app._logs = []
    app._busy_states = []
    app._scheduled = []
    app._append_log = lambda text: app._logs.append(text)
    app._set_replay_controls_busy = lambda busy: app._busy_states.append(busy)
    app.after = lambda delay, callback: app._scheduled.append((delay, callback))
    return app


def test_choose_dataset_validates_off_tk_thread_and_publishes_only_terminal_result(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    selected = (tmp_path / "selected").absolute()
    worker = OneShotDatasetValidationWorker()
    app = _bare_app(dataset_worker=worker, previous_dataset=previous)
    caller_thread = threading.get_ident()
    validation_thread: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def blocking_load(path: Path) -> ReplayDataset:
        assert path == selected
        validation_thread.append(threading.get_ident())
        started.set()
        assert release.wait(2.0)
        return _dataset(selected)

    with (
        patch("autosport.gui.filedialog.askdirectory", return_value=str(selected)),
        patch("autosport.gui.load_dataset", side_effect=blocking_load),
        patch("autosport.gui.messagebox.showerror") as showerror,
    ):
        AutosportApp.choose_dataset(app)
        assert started.wait(1.0)

        # The selection handler has already returned while validation is still
        # deliberately blocked on its worker thread. The prior valid selection is
        # not replaced by an unverified path.
        assert validation_thread == [worker._thread.ident]
        assert validation_thread[0] != caller_thread
        assert app.dataset_path == previous
        assert app.dataset_text.value == "previous dataset summary"
        assert app._busy_states == [True]
        assert app._scheduled[0][0] == 100
        assert "фоновому процесі лише для читання" in app.status.value

        release.set()
        deadline = time.monotonic() + 2.0
        while app.dataset_path != selected and time.monotonic() < deadline:
            AutosportApp._poll_dataset_worker(app)
            time.sleep(0.01)

        assert app.dataset_path == selected
        assert app._busy_states == [True, False]
        assert "responsive corpus" in app.dataset_text.value
        assert app.status.value == "Набір даних перевірено. Можна запускати повтор."
        showerror.assert_not_called()


def test_terminal_validation_failure_preserves_last_known_good_dataset(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    selected = (tmp_path / "rejected").absolute()
    worker = _TerminalWorker(
        DatasetValidationMessage(error="ValueError: market dataset hash mismatch")
    )
    app = _bare_app(dataset_worker=worker, previous_dataset=previous)
    app._pending_dataset_path = selected

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_dataset_worker(app)

    assert app.dataset_path == previous
    assert app.dataset_text.value == "previous dataset summary"
    assert app._pending_dataset_path is None
    assert app._busy_states == [False]
    assert "не змінено" in app.status.value
    showerror.assert_called_once_with(
        "Автоспорт",
        "Набір даних відхилено: ValueError: market dataset hash mismatch",
        parent=app,
    )


def test_worker_start_process_control_failure_clears_pending_selection(
    tmp_path: Path,
) -> None:
    selected = (tmp_path / "selected").absolute()

    class _InterruptedWorker:
        busy = False

        def start(self, _task) -> bool:
            raise KeyboardInterrupt("worker setup interrupted")

    app = _bare_app(dataset_worker=_InterruptedWorker())
    with patch("autosport.gui.filedialog.askdirectory", return_value=str(selected)):
        try:
            AutosportApp.choose_dataset(app)
        except KeyboardInterrupt as exc:
            assert str(exc) == "worker setup interrupted"
        else:
            raise AssertionError("KeyboardInterrupt must remain process-control visible")

    assert app._pending_dataset_path is None
    assert app._busy_states == []


def test_terminal_result_for_different_folder_fails_closed(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    selected = (tmp_path / "selected").absolute()
    other = (tmp_path / "other").absolute()
    worker = _TerminalWorker(DatasetValidationMessage(result=_dataset(other)))
    app = _bare_app(dataset_worker=worker, previous_dataset=previous)
    app._pending_dataset_path = selected

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_dataset_worker(app)

    assert app.dataset_path == previous
    assert app.dataset_text.value == "previous dataset summary"
    assert "невідповідність ідентичності" in app.status.value
    showerror.assert_called_once()


def test_dataset_validation_blocks_replay_live_and_both_recovery_paths() -> None:
    app = _bare_app(dataset_worker=SimpleNamespace(busy=True))

    AutosportApp.run_dataset(app)
    assert "перевірка набору даних" in app.status.value

    AutosportApp.refresh_live_snapshot(app)
    assert "перевірка набору даних" in app.live_status.value

    AutosportApp.repair_workspace(app)
    assert "перевірка набору даних" in app.status.value

    windows_app = object.__new__(WindowsAutosportApp)
    windows_app._closing = False
    windows_app.dataset_worker = SimpleNamespace(busy=True)
    windows_app.status = _Value()
    WindowsAutosportApp.repair_workspace(windows_app)
    assert "перевірка набору даних" in windows_app.status.value


def test_replay_control_restore_cannot_enable_controls_while_dataset_is_busy() -> None:
    app = object.__new__(AutosportApp)
    app.dataset_worker = SimpleNamespace(busy=True)
    app.strategy = _Control()
    app.research_plan_button = _Control()
    app.choose_button = _Control()
    app.run_button = _Control()
    app.repair_button = _Control()
    app.speed = _Control()
    app.live_mode = _Control()
    app.live_refresh_button = _Control()

    AutosportApp._set_replay_controls_busy(app, False)

    assert app.strategy.operations == [("configure", {"state": "disabled"})]
    for control in (
        app.research_plan_button,
        app.choose_button,
        app.run_button,
        app.repair_button,
        app.live_refresh_button,
    ):
        assert control.operations == [("state", ("disabled",))]
    assert app.speed.operations == [("configure", {"state": "disabled"})]
    assert app.live_mode.operations == [("configure", {"state": "disabled"})]


def test_close_does_not_wait_for_read_only_dataset_validation() -> None:
    app = _bare_app(dataset_worker=SimpleNamespace(busy=True))
    app.session = None
    app.destroyed = False
    app.destroy = lambda: setattr(app, "destroyed", True)
    app.bell = lambda: (_ for _ in ()).throw(
        AssertionError("read-only dataset validation must not block close")
    )

    AutosportApp.close_app(app)

    assert app._closing
    assert app.destroyed
