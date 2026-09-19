import inspect
from pathlib import Path

import pytest

import autosport.windows_layout as windows_layout
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    OWNER_ECONOMIC_FORM_FIELDS,
    OwnerEconomicAuthorityError,
    OwnerEconomicAuthorityService,
)
from autosport.windows_layout import (
    OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS,
    WINDOWS_SHELL_AUTOMATION_IDS,
    WINDOWS_SHELL_DETAILS_VISIBLE_ROWS,
    compact_surface_heights,
    configure_windows_product_shell_accessibility,
    install_compact_windows_layout,
    install_owner_economic_authority_surface,
    install_windows_product_shell,
    refresh_windows_shell_open_availability,
    _owner_economic_workspace,
    _owner_economic_write_blocker,
    _persist_initial_owner_economic_contract,
    _show_owner_economic_dialog,
    _surface_target_widget,
)


class _Widget:
    def __init__(self):
        self.height = None
        self.pady = None

    def configure(self, **kwargs):
        self.height = kwargs.get("height")

    def pack_configure(self, **kwargs):
        self.pady = kwargs.get("pady")


class _App:
    def __init__(self):
        self.live_quotes = _Widget()
        self.tickets = _Widget()
        self.evaluation = _Widget()
        self.log = _Widget()
        self.tickets_label = _Widget()
        self.evaluation_label = _Widget()
        self.log_label = _Widget()


def test_compact_surface_heights_keep_all_critical_scrolling_surfaces_visible():
    app = _App()

    compact_surface_heights(app)

    assert app.live_quotes.height == 2
    assert app.tickets.height == 3
    assert app.evaluation.height == 2
    assert app.log.height == 2
    assert app.tickets_label.pady == (6, 2)
    assert app.evaluation_label.pady == (6, 2)
    assert app.log_label.pady == (6, 2)
    assert WINDOWS_SHELL_DETAILS_VISIBLE_ROWS == 1


def test_windows_product_shell_has_stable_uia_ids_and_keyboard_navigation():
    assert WINDOWS_SHELL_AUTOMATION_IDS == {
        "navigation": 301,
        "state": 302,
        "open": 303,
        "details": 304,
        "owner_economic_open": 305,
        "owner_economic_status": 306,
        "owner_economic_readback": 307,
        "owner_economic_dialog_readback": 308,
    }
    assert OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS == {
        "readback": 308,
        "goal_id": 309,
        "bankroll_id": 310,
        "currency": 311,
        "max_stake_fraction": 312,
        "max_stake_amount": 313,
        "max_session_loss_fraction": 314,
        "max_day_loss_fraction": 315,
        "max_drawdown_fraction": 316,
        "max_capital_at_risk_fraction": 317,
        "max_risk_of_ruin": 318,
        "max_quote_age_seconds": 319,
        "minimum_data_quality": 320,
        "max_concurrent_positions": 321,
        "max_parlay_legs": 322,
        "automation_level": 323,
        "blocked_sports": 324,
        "blocked_providers": 325,
        "blocked_markets": 326,
        "emergency_stop": 327,
        "create": 328,
        "close": 329,
    }

    build_source = inspect.getsource(install_windows_product_shell)
    for binding in (
        "<F2>",
        "<Control-Alt-Left>",
        "<Control-Alt-Right>",
        "<<ComboboxSelected>>",
    ):
        assert binding in build_source

    accessibility_source = inspect.getsource(configure_windows_product_shell_accessibility)
    for automation_id in set(WINDOWS_SHELL_AUTOMATION_IDS) - {"owner_economic_dialog_readback"}:
        assert f'WINDOWS_SHELL_AUTOMATION_IDS["{automation_id}"]' in accessibility_source

    dialog_source = inspect.getsource(_show_owner_economic_dialog)
    assert "_persist_initial_owner_economic_contract(" in dialog_source
    assert "initial_write_blocker = _owner_economic_write_blocker(app, bound_workspace)" in dialog_source
    assert 'state=("disabled" if initial_write_blocker is not None else "normal")' in dialog_source
    assert 'OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["readback"]' in dialog_source
    for field in OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS:
        assert f'OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["{field}"]' in dialog_source or (
            field in OWNER_ECONOMIC_FORM_FIELDS
            and "OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS[field]" in dialog_source
        )

    owner_surface_source = inspect.getsource(install_owner_economic_authority_surface)
    assert 'owner_row = ttk.Frame(panel)' in owner_surface_source
    assert 'owner_economic_authority_button.pack(side="left"' in owner_surface_source
    assert 'owner_economic_authority_state.pack(side="left", fill="x", expand=True)' in owner_surface_source
    assert 'owner_economic_authority_readback = tk.Listbox(panel, height=1' in owner_surface_source

    install_source = inspect.getsource(install_compact_windows_layout)
    assert "refresh_windows_shell_open_availability(self)" in install_source


def test_shell_target_requires_a_real_widget_before_open_is_authorized():
    class SurfaceKey:
        def get(self):
            return "paper_bank"

    class OpenButton:
        def __init__(self):
            self.state = None

        def configure(self, **kwargs):
            self.state = kwargs["state"]

    class App:
        shell_surface_key = SurfaceKey()
        shell_open_button = OpenButton()

    app = App()

    # The shell is installed by the patched base build before WindowsAutosportApp
    # creates its late-bound bank_summary. Persisted Paper Bank must fail closed
    # at that first render rather than route to an unrelated control.
    assert _surface_target_widget(app, "paper_bank") is None
    refresh_windows_shell_open_availability(app)
    assert app.shell_open_button.state == "disabled"

    # The accessibility-configuration phase runs after the subclass build has
    # created bank_summary. Re-checking authority must make Open usable without
    # changing the persisted surface selection.
    bank_summary = object()
    app.bank_summary = bank_summary
    assert _surface_target_widget(app, "paper_bank") is bank_summary
    refresh_windows_shell_open_availability(app)
    assert app.shell_open_button.state == "normal"


def test_owner_economic_creation_is_closed_for_strategies_without_goal_aware_sizing():
    class App:
        workspace = "."

        def __init__(self, strategy_id):
            self.strategy_id = strategy_id

        def _selected_replay_configuration(self):
            return self.strategy_id, None

    for strategy_id in ("baseline-v1", "observe-only-v1"):
        workspace, blocked = _owner_economic_workspace(App(strategy_id))
        assert workspace is None
        assert blocked is not None


def test_owner_economic_write_blocker_fails_closed_for_exact_recovery_workspace(tmp_path):
    blocked_workspace = tmp_path / "strategies" / "research-replay-v1-plan"
    other_workspace = tmp_path / "strategies" / "research-replay-v1-other"

    class App:
        def __init__(self):
            self._closing = False
            self._recovery_blocked_workspaces = {blocked_workspace}

        def _workspace_requires_recovery(self, workspace):
            return workspace in self._recovery_blocked_workspaces

    app = App()

    assert _owner_economic_write_blocker(app, blocked_workspace) is not None
    assert _owner_economic_write_blocker(app, other_workspace) is None


def test_owner_economic_write_blocker_preserves_base_gui_recovery_quarantine(tmp_path):
    blocked_workspace = tmp_path / "strategies" / "research-replay-v1-plan"

    class App:
        def __init__(self):
            self._closing = False
            self._recovery_required_workspaces = {blocked_workspace}

    app = App()

    assert _owner_economic_write_blocker(app, blocked_workspace) is not None


def test_owner_economic_persistence_seam_keeps_quarantined_workspace_absent_and_other_workspace_independent(
    tmp_path, monkeypatch
):
    blocked_workspace = tmp_path / "blocked" / "strategies" / "research"
    other_workspace = tmp_path / "other" / "strategies" / "research"

    class App:
        def __init__(self, current_workspace: Path):
            self._closing = False
            self.current_workspace = current_workspace
            self._recovery_blocked_workspaces: set[Path] = set()

        def _workspace_requires_recovery(self, workspace):
            return Path(workspace) in self._recovery_blocked_workspaces

    monkeypatch.setattr(
        windows_layout,
        "_owner_economic_workspace",
        lambda app: (Path(app.current_workspace), None),
    )

    blocked_app = App(blocked_workspace)
    blocked_app._recovery_blocked_workspaces.add(blocked_workspace)
    blocked_service = OwnerEconomicAuthorityService(blocked_workspace)

    with pytest.raises(OwnerEconomicAuthorityError):
        _persist_initial_owner_economic_contract(
            blocked_app,
            bound_workspace=blocked_workspace,
            service=blocked_service,
            values=dict(INITIAL_OWNER_FORM_DEFAULTS),
            emergency_stop=False,
        )

    assert blocked_service.read_view().state == "absent"
    assert not blocked_service.store.path.exists()

    other_app = App(other_workspace)
    other_app._recovery_blocked_workspaces.add(blocked_workspace)
    other_service = OwnerEconomicAuthorityService(other_workspace)
    persisted = _persist_initial_owner_economic_contract(
        other_app,
        bound_workspace=other_workspace,
        service=other_service,
        values=dict(INITIAL_OWNER_FORM_DEFAULTS),
        emergency_stop=True,
    )

    assert persisted.state == "valid"
    assert persisted.contract is not None
    assert persisted.contract.emergency_stop is True
    assert other_service.store.path.exists()
    assert not blocked_service.store.path.exists()


@pytest.mark.parametrize("busy_source", ("closing", "dataset", "replay", "live", "recovery"))
def test_owner_economic_write_blocker_disables_creation_for_every_busy_writer(
    tmp_path, busy_source
):
    workspace = tmp_path / "strategies" / "research"

    class Worker:
        def __init__(self):
            self.busy = False

    class App:
        def __init__(self):
            self._closing = False
            self._dataset_busy = False
            self.replay_worker = Worker()
            self.live_worker = Worker()
            self.recovery_worker = Worker()

        def _workspace_requires_recovery(self, _workspace):
            return False

    app = App()
    if busy_source == "closing":
        app._closing = True
    elif busy_source == "dataset":
        app._dataset_busy = True
    elif busy_source == "replay":
        app.replay_worker.busy = True
    elif busy_source == "live":
        app.live_worker.busy = True
    else:
        app.recovery_worker.busy = True

    assert _owner_economic_write_blocker(app, workspace) is not None


def test_manual_calculation_workbench_localization_and_surface_contract():
    from autosport.localization import text
    from autosport.windows_surface_contract import SURFACE_BY_KEY
    from autosport.windows_manual_calculation import WORKBENCH_OPERATIONS

    assert text("ui.windows.manual_calculation.dialog.title") == "Автоспорт — ручні розрахунки"
    assert SURFACE_BY_KEY["manual_calculation"].phase == "active"
    assert SURFACE_BY_KEY["manual_calculation"].target_widget == "manual_calculation_button"
    assert len(WORKBENCH_OPERATIONS) == 7
    assert all(label for _, label in WORKBENCH_OPERATIONS)


def test_manual_calculation_workbench_has_no_persistent_or_execution_authority():
    import inspect
    from autosport.windows_manual_calculation import show_manual_calculation_workbench
    source = inspect.getsource(show_manual_calculation_workbench)
    assert "atomic_write_json" not in source
    assert "PaperBook" not in source
    assert "provider" not in source
    assert "real_money_execution=false" in source


def test_manual_calculation_workbench_input_and_error_contracts():
    from autosport.windows_manual_calculation import _calculation_call, _read_lines, _selection_odds

    class Input:
        def __init__(self, value):
            self.value = value
        def get(self, *_args):
            return self.value

    assert _read_lines(Input("1.90,2.10")) == ["1.90", "2.10"]
    assert _selection_odds(Input("a=1.90\nb=2.10")) == {"a": "1.90", "b": "2.10"}
    with pytest.raises(ValueError, match="Повторний"):
        _selection_odds(Input("a=1.90\na=2.10"))

    class Service:
        def odds_conversion(self, _value):
            raise ValueError("Infinity")
    with pytest.raises(ValueError):
        _calculation_call(Service(), "odds_conversion", Input("Infinity"))
