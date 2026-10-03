from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from autosport.gui import AutosportApp
from autosport.localization import text


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


def test_research_plan_not_required_status_precedes_blocking_modal() -> None:
    app = object.__new__(AutosportApp)
    app.dataset_worker = SimpleNamespace(busy=False)
    app.replay_worker = SimpleNamespace(busy=False)
    app.status = _Value()
    app.strategy_text = SimpleNamespace(get=lambda: "synthetic")

    def assert_persistent_status_before_modal(*_args: object) -> None:
        assert app.status.value == text(
            "ui.status.research_plan.not_required_short",
            strategy_id="non-research-strategy",
        )

    with (
        patch(
            "autosport.gui.strategy_id_from_display",
            return_value="non-research-strategy",
        ),
        patch(
            "autosport.gui.strategy_spec",
            return_value=SimpleNamespace(requires_research_plan=False),
        ),
        patch(
            "autosport.gui.messagebox.showinfo",
            side_effect=assert_persistent_status_before_modal,
        ) as showinfo,
    ):
        AutosportApp.choose_research_plan(app)

    assert app.status.value == text(
        "ui.status.research_plan.not_required_short",
        strategy_id="non-research-strategy",
    )
    showinfo.assert_called_once()


def test_research_plan_validation_status_precedes_blocking_modal() -> None:
    app = object.__new__(AutosportApp)
    app.dataset_worker = SimpleNamespace(busy=False)
    app.replay_worker = SimpleNamespace(busy=False)
    app.status = _Value()
    app.strategy_text = SimpleNamespace(get=lambda: "synthetic")

    def assert_persistent_status_before_modal(*_args: object) -> None:
        assert app.status.value == text(
            "ui.status.research_plan.validation_failed"
        )

    with (
        patch(
            "autosport.gui.strategy_id_from_display",
            return_value="research-strategy",
        ),
        patch(
            "autosport.gui.strategy_spec",
            return_value=SimpleNamespace(requires_research_plan=True),
        ),
        patch(
            "autosport.gui.filedialog.askopenfilename",
            return_value="invalid-research-plan.json",
        ),
        patch(
            "autosport.gui.ResearchStrategyPlan.from_path",
            side_effect=ValueError("invalid research plan"),
        ),
        patch(
            "autosport.gui.messagebox.showerror",
            side_effect=assert_persistent_status_before_modal,
        ) as showerror,
    ):
        AutosportApp.choose_research_plan(app)

    assert app.status.value == text("ui.status.research_plan.validation_failed")
    showerror.assert_called_once()


def test_replay_configuration_status_precedes_blocking_modal() -> None:
    app = object.__new__(AutosportApp)
    app.dataset_worker = SimpleNamespace(busy=False)
    app.dataset_path = object()
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.status = _Value()

    def fail_configuration() -> tuple[str, object]:
        raise ValueError("invalid replay configuration")

    app._selected_replay_configuration = fail_configuration

    def assert_persistent_status_before_modal(*_args: object) -> None:
        assert app.status.value == text("ui.status.replay.configuration_rejected")

    with patch(
        "autosport.gui.messagebox.showerror",
        side_effect=assert_persistent_status_before_modal,
    ) as showerror:
        AutosportApp.run_dataset(app)

    assert app.status.value == text("ui.status.replay.configuration_rejected")
    showerror.assert_called_once()


def test_replay_dataset_required_status_precedes_blocking_modal() -> None:
    app = object.__new__(AutosportApp)
    app.dataset_worker = SimpleNamespace(busy=False)
    app.dataset_path = None
    app.status = _Value()

    expected = text("ui.info.replay.dataset_required")

    def assert_persistent_status_before_modal(*_args: object) -> None:
        assert app.status.value == expected

    with patch(
        "autosport.gui.messagebox.showinfo",
        side_effect=assert_persistent_status_before_modal,
    ) as showinfo:
        AutosportApp.run_dataset(app)

    assert app.status.value == expected
    showinfo.assert_called_once_with(text("ui.dialog.title"), expected)

