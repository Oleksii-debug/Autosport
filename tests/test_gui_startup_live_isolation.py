from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.gui import AutosportApp
from autosport.live_observation import observe_workspace_once
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.session import AutosportSession
from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _HeadlessAutosportApp(AutosportApp):
    """Exercise product startup without requiring a display server."""

    def title(self, _value: str) -> None:
        return None

    def geometry(self, _value: str) -> None:
        return None

    def minsize(self, _width: int, _height: int) -> None:
        return None

    def _build(self) -> None:
        self.shell_built = True

    def update_idletasks(self) -> None:
        return None

    def _configure_accessibility(self) -> None:
        self.accessibility_configured = True

    def protocol(self, _name: str, _callback) -> None:
        self.close_protocol_bound = True


class _HeadlessWindowsAutosportApp(WindowsAutosportApp):
    """Exercise Windows recovery composition without creating real Tk controls."""

    def title(self, _value: str) -> None:
        return None

    def geometry(self, _value: str) -> None:
        return None

    def minsize(self, _width: int, _height: int) -> None:
        return None

    def _build(self) -> None:
        self.shell_built = True
        self.tickets = SimpleNamespace(
            delete=lambda *_args: None,
            insert=lambda *_args: None,
        )

    def update_idletasks(self) -> None:
        return None

    def _configure_accessibility(self) -> None:
        self.accessibility_configured = True

    def protocol(self, _name: str, _callback) -> None:
        self.close_protocol_bound = True


class _Button:
    def __init__(self) -> None:
        self.states: list[tuple[str, ...]] = []

    def state(self, values) -> None:
        self.states.append(tuple(values))


class _ImmediateWorker:
    def __init__(self) -> None:
        self.busy = False
        self.result = None
        self.start_calls = 0

    def start(self, task) -> bool:
        self.start_calls += 1
        self.result = task()
        return True


class _NeverStartWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls = 0

    def start(self, _task) -> bool:
        self.start_calls += 1
        return True


class _OneQuoteProvider:
    source_id = "startup-live-isolation"

    def __init__(self, *, sequence: int, odds: str) -> None:
        self.sequence = sequence
        self.odds = Decimal(odds)

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        assert max_items > 0
        quote = ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="match-odds",
            provider_selection_id="player-a",
            decimal_odds=self.odds,
            observed_ts="2026-09-14T12:00:00+00:00",
            sequence=self.sequence,
        )
        return ProviderBatch(self.source_id, (quote,))


def _string_var(*_args, value: str = "", **_kwargs) -> _Value:
    return _Value(value)


def test_real_paperbook_corruption_does_not_block_real_live_market_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    # Establish real, valid shared Market Store and source-health state first.
    initial = observe_workspace_once(
        workspace,
        _OneQuoteProvider(sequence=1, odds="2.00"),
        max_items=10,
    )
    assert len(initial.current_quotes) == 1

    corrupt_paper = b"{ definitely-not-valid-json"
    paper_path = workspace / "paper_book.json"
    paper_path.write_bytes(corrupt_paper)

    # The real economic session now fails specifically while loading PaperBook,
    # after the same market/source-health prerequisites have opened successfully.
    with pytest.raises(json.JSONDecodeError):
        AutosportSession(workspace, "10000")

    # Live observation uses only market.db + source_health.json. It must continue
    # to mutate/read those real shared components without reading or repairing the
    # corrupt economic artifact.
    observed = observe_workspace_once(
        workspace,
        _OneQuoteProvider(sequence=2, odds="2.20"),
        max_items=10,
    )

    assert len(observed.current_quotes) == 1
    assert observed.current_quotes[0].decimal_odds == Decimal("2.20")
    assert paper_path.read_bytes() == corrupt_paper


def test_corrupt_economic_startup_keeps_shell_and_live_observation_reachable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", side_effect=ValueError("corrupt paper state")),
    ):
        app = _HeadlessAutosportApp()

    assert app.shell_built
    assert app.accessibility_configured
    assert app.close_protocol_bound
    assert app.session is None
    assert app._startup_economic_error == "ValueError: corrupt paper state"
    assert workspace in app._recovery_required_workspaces
    assert "Read-only live snapshot доступний" in app.status.value
    assert "недоступний до успішного recovery" in app.bank.value

    live_result = object()
    provider = object()
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = _ImmediateWorker()
    app.live_mode_text = _Value("Public preview — без ключа")
    app.live_status = _Value()
    app.live_refresh_button = _Button()
    scheduled: list[tuple[int, object]] = []
    app.after = lambda delay, callback: scheduled.append((delay, callback))

    with (
        patch("autosport.gui.ParlayApiTableTennisProvider", return_value=provider) as provider_factory,
        patch("autosport.gui.observe_workspace_once", return_value=live_result) as observe,
    ):
        AutosportApp.refresh_live_snapshot(app)

    assert app.live_worker.start_calls == 1
    assert app.live_worker.result is live_result
    provider_factory.assert_called_once_with(None, public_preview=True)
    observe.assert_called_once_with(workspace, provider, max_items=250)
    assert app.live_refresh_button.states == [("disabled",)]
    assert "read-only live observation" in app.status.value
    assert scheduled and scheduled[0][0] == 100

    replay_worker = _NeverStartWorker()
    app.replay_worker = replay_worker
    app.live_worker = SimpleNamespace(busy=False)
    app.dataset_path = tmp_path / "dataset"
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._append_log = lambda _text: None

    with (
        patch("autosport.gui.workspace_for_strategy", return_value=workspace),
        patch("autosport.gui.messagebox.showwarning") as showwarning,
    ):
        AutosportApp.run_dataset(app)

    assert replay_worker.start_calls == 0
    assert "непідтверджений terminal state" in app.status.value
    showwarning.assert_called_once()


def test_windows_startup_uses_native_recovery_quarantine_and_replay_unblocks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", side_effect=ValueError("corrupt paper state")),
    ):
        app = _HeadlessWindowsAutosportApp()

    assert app.shell_built
    assert app.session is None
    assert app._recovery_required_workspaces == set()
    assert app._workspace_requires_recovery(workspace)
    assert app._startup_economic_error == "ValueError: corrupt paper state"

    # Windows recovery already owns this exact lifecycle. A successful terminal
    # recovery clears its native quarantine; the base startup path must not leave
    # a second hidden quarantine that would make super().run_dataset() block forever.
    app._unblock_workspace_after_recovery(workspace)

    assert not app._workspace_requires_recovery(workspace)
    assert app._recovery_required_workspaces == set()

    # The diagnostic startup marker is intentionally latent in the Windows
    # subclass: Windows overrides bankroll/ticket projections and all recovery
    # gating. Prove that even while the marker remains for diagnostics, the exact
    # recovered workspace reaches the canonical replay worker rather than being
    # rejected by an inherited base quarantine.
    replay_worker = _NeverStartWorker()
    app.replay_worker = replay_worker
    app.live_worker = SimpleNamespace(busy=False)
    app.dataset_path = tmp_path / "dataset"
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    app._set_replay_controls_busy = lambda _busy: None
    app._refresh_tickets = lambda: None
    app._set_evaluation_lines = lambda _lines: None
    app._append_log = lambda _text: None
    app.after = lambda *_args: None

    with (
        patch("autosport.windows_gui.workspace_for_strategy", return_value=workspace),
        patch("autosport.gui.workspace_for_strategy", return_value=workspace),
    ):
        WindowsAutosportApp.run_dataset(app)

    assert app._startup_economic_error == "ValueError: corrupt paper state"
    assert replay_worker.start_calls == 1
    assert "Replay виконується" in app.status.value


def test_valid_economic_startup_preserves_existing_ready_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    session = SimpleNamespace(
        workspace=workspace,
        strategy_id="baseline-v1",
        book=SimpleNamespace(balance=10000, committed_stake=0),
    )

    with (
        patch("autosport.gui.tk.Tk.__init__", return_value=None),
        patch("autosport.gui.tk.StringVar", side_effect=_string_var),
        patch("autosport.gui.default_workspace", return_value=workspace),
        patch("autosport.gui.AutosportSession", return_value=session),
    ):
        app = _HeadlessAutosportApp()

    assert app.session is session
    assert app._startup_economic_error is None
    assert app._recovery_required_workspaces == set()
    assert app.status.value.startswith("Готово.")
    assert "10000" in app.bank.value
