from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.localization import text
from autosport.operator_source_registry import list_product_source_entries
from autosport.product_gui_worker import ProductGuiMessage
from autosport.product_windows_gui import (
    ProductWindowsAutosportApp,
    _localized_product_stop_reason,
)
from autosport.research_strategy import ResearchStrategyPlan


class _Var:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


class _Button:
    def __init__(self) -> None:
        self.states: list[tuple[str, ...]] = []

    def state(self, values) -> None:
        self.states.append(tuple(values))


class _CapturingWorker:
    def __init__(self, *, start_result: bool = True) -> None:
        self.busy = False
        self.start_result = start_result
        self.started_with = None
        self.stop_reasons: list[str] = []

    def start(self, **kwargs) -> bool:
        self.started_with = kwargs
        if self.start_result:
            self.busy = True
        return self.start_result

    def request_stop(self, reason: str) -> bool:
        self.stop_reasons.append(reason)
        return True


def _headless_app(tmp_path, *, worker=None):
    app = object.__new__(ProductWindowsAutosportApp)
    root = tmp_path / "autosport"
    app.workspace = root
    app._active_workspace = root
    app._active_strategy_id = "baseline-v1"
    app._active_research_plan = None
    app.session = SimpleNamespace(workspace=root)
    app.product_worker = worker or _CapturingWorker()
    app.dataset_worker = SimpleNamespace(busy=False)
    app.replay_worker = SimpleNamespace(busy=False)
    app.live_worker = SimpleNamespace(busy=False)
    app.evidence_export_worker = SimpleNamespace(busy=False)
    app.recovery_worker = SimpleNamespace(busy=False)
    app._recovery_blocked_workspace = None
    app._recovery_blocked_workspaces = set()
    app._recovery_view = None
    app._startup_economic_error = None
    app._product_close_pending = False
    app._product_last_stop = None
    app._product_runtime_workspace = None
    app._product_restore_strategy_id = None
    app._product_restore_research_plan = None
    app.product_status = _Var()
    app.status = _Var()
    app.bank = _Var()
    app.product_start_button = _Button()
    app.product_stop_button = _Button()
    app.__dict__["_closing"] = False
    app.bell = lambda: None
    app._append_log = lambda _message: None
    app._refresh_tickets = lambda: None
    app._bank_text = lambda: "bank"
    app.after = lambda _delay, _callback: None
    return app


def test_product_bridge_uses_canonical_session_teardown_and_exact_reopen() -> None:
    start_source = inspect.getsource(ProductWindowsAutosportApp.start_product_runtime)
    restore_source = inspect.getsource(
        ProductWindowsAutosportApp._restore_base_session_after_product
    )
    assert "self._hide_uncertain_economic_state(" in start_source
    assert "self._workspace_requires_recovery(workspace)" in start_source
    assert "list_product_source_entries()" in start_source
    assert "expected_source_id=expected_source_id" in start_source
    assert "self._product_restore_strategy_id" in restore_source
    assert "self._product_restore_research_plan" in restore_source
    assert "self._open_session(" in restore_source
    assert "AutosportSession(" not in restore_source


def test_product_runtime_start_uses_exact_active_strategy_workspace(
    tmp_path, monkeypatch
) -> None:
    worker = _CapturingWorker()
    app = _headless_app(tmp_path, worker=worker)
    strategy_workspace = tmp_path / "autosport" / "strategies" / "current"
    app._active_workspace = strategy_workspace
    app.session = SimpleNamespace(workspace=strategy_workspace)
    app._active_strategy_id = "captured-strategy"
    plan = object()
    app._active_research_plan = plan

    monkeypatch.setattr(
        "autosport.product_windows_gui.workspace_for_strategy",
        lambda root, strategy_id, research_plan: strategy_workspace,
    )
    monkeypatch.setattr(
        ProductWindowsAutosportApp,
        "_hide_uncertain_economic_state",
        lambda self, _message: True,
    )
    monkeypatch.setattr(
        ProductWindowsAutosportApp,
        "_set_product_controls_running",
        lambda self, _running: None,
    )
    source = list_product_source_entries()[0]
    monkeypatch.setenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", source.factory_spec)

    app.start_product_runtime()

    assert worker.started_with is not None
    assert worker.started_with["workspace"] == strategy_workspace
    assert worker.started_with["expected_source_id"] == source.expected_provider_source_id
    assert app._product_runtime_workspace == strategy_workspace
    assert app._product_restore_strategy_id == "captured-strategy"
    assert app._product_restore_research_plan is plan


def test_product_runtime_refuses_active_workspace_identity_drift(
    tmp_path, monkeypatch
) -> None:
    worker = _CapturingWorker()
    app = _headless_app(tmp_path, worker=worker)
    expected = tmp_path / "autosport" / "strategies" / "expected"
    app._active_workspace = tmp_path / "autosport" / "strategies" / "wrong"
    app.session = SimpleNamespace(workspace=app._active_workspace)
    app._active_strategy_id = "captured-strategy"
    app._active_research_plan = object()
    teardown_called = False

    monkeypatch.setattr(
        "autosport.product_windows_gui.workspace_for_strategy",
        lambda root, strategy_id, research_plan: expected,
    )

    def teardown(self, _message):
        nonlocal teardown_called
        teardown_called = True
        return True

    monkeypatch.setattr(
        ProductWindowsAutosportApp,
        "_hide_uncertain_economic_state",
        teardown,
    )

    app.start_product_runtime()

    assert worker.started_with is None
    assert teardown_called is False
    assert app.product_status.value == text(
        "ui.product_runtime.status.workspace_identity_mismatch"
    )


def test_product_stop_reason_projection_never_exposes_unknown_internal_token() -> None:
    assert _localized_product_stop_reason("operator_stop") == text(
        "ui.product_runtime.stop_reason.operator"
    )
    assert _localized_product_stop_reason("app_close") == text(
        "ui.product_runtime.stop_reason.app_close"
    )
    hostile = "provider:secret-internal-stop-token"
    projected = _localized_product_stop_reason(hostile)
    assert projected == text("ui.product_runtime.stop_reason.other")
    assert hostile not in projected


def test_product_start_actionability_tracks_exact_recovery_quarantine(tmp_path) -> None:
    app = _headless_app(tmp_path)
    runtime_workspace = tmp_path / "autosport" / "strategies" / "runtime"
    app._product_runtime_workspace = runtime_workspace
    app._set_replay_controls_busy = lambda _busy: None

    app._block_workspace_for_recovery(runtime_workspace)
    app._set_product_controls_running(False)

    assert app.product_start_button.states[-1] == ("disabled",)

    app._unblock_workspace_after_recovery(runtime_workspace)

    assert app.product_start_button.states[-1] == ("!disabled",)
    assert not app._workspace_requires_recovery(runtime_workspace)


def test_product_runtime_failure_quarantines_bound_runtime_workspace(tmp_path) -> None:
    app = _headless_app(tmp_path)
    runtime_workspace = tmp_path / "autosport" / "strategies" / "runtime"
    app._product_runtime_workspace = runtime_workspace

    app._apply_product_message(
        ProductGuiMessage(kind="ERROR", error_type="RuntimeError")
    )

    assert runtime_workspace in app._recovery_blocked_workspaces
    assert app.workspace not in app._recovery_blocked_workspaces


def test_product_session_restore_uses_frozen_start_configuration(
    tmp_path, monkeypatch
) -> None:
    app = _headless_app(tmp_path)
    target = tmp_path / "autosport" / "strategies" / "captured"
    captured_plan = object()
    app.session = None
    app._product_runtime_workspace = target
    app._product_restore_strategy_id = "captured-strategy"
    app._product_restore_research_plan = captured_plan
    app._active_strategy_id = "mutated-after-start"
    app._active_research_plan = object()
    calls = []

    def open_session(self, strategy_id, research_plan):
        calls.append((strategy_id, research_plan))
        self._active_workspace = target
        return SimpleNamespace(workspace=target)

    monkeypatch.setattr(ProductWindowsAutosportApp, "_open_session", open_session)
    monkeypatch.setattr(
        "autosport.product_windows_gui.workspace_for_strategy",
        lambda root, strategy_id, research_plan: target,
    )

    assert app._restore_base_session_after_product() is True
    assert calls == [("captured-strategy", captured_plan)]
    assert app._product_runtime_workspace is None
    assert app._product_restore_strategy_id is None


def test_product_restore_rejects_tampered_frozen_plan_before_opening_workspace(
    tmp_path, monkeypatch
) -> None:
    app = _headless_app(tmp_path)
    original_sha = "a" * 64
    tampered_sha = "b" * 64
    plan = ResearchStrategyPlan(
        (
            SimpleNamespace(
                decision_id="decision-1",
                decision_ts="2026-09-19T04:00:00Z",
                trigger_quote_key="quote-1",
            ),
        ),
        original_sha,
    )
    target = tmp_path / "autosport" / "strategies" / original_sha[:8]
    app.session = None
    app._active_workspace = target
    app._product_runtime_workspace = target
    app._product_restore_strategy_id = "captured-research-strategy"
    app._product_restore_research_plan = plan
    opened: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "autosport.product_windows_gui.workspace_for_strategy",
        lambda root, strategy_id, research_plan: Path(root)
        / "strategies"
        / research_plan.source_sha256[:8],
    )

    def open_session(self, strategy_id, research_plan):
        opened.append((strategy_id, research_plan))
        return SimpleNamespace(workspace=tmp_path / "unexpected")

    monkeypatch.setattr(ProductWindowsAutosportApp, "_open_session", open_session)

    # frozen=True is not an authority boundary against object.__setattr__.
    object.__setattr__(plan, "source_sha256", tampered_sha)

    assert app._restore_base_session_after_product() is False
    assert opened == []
    assert target in app._recovery_blocked_workspaces
    assert app._active_workspace == target


def test_start_failure_does_not_mask_reopen_failure(tmp_path, monkeypatch) -> None:
    worker = _CapturingWorker(start_result=False)
    app = _headless_app(tmp_path, worker=worker)
    source = list_product_source_entries()[0]
    monkeypatch.setenv("AUTOSPORT_PRODUCT_SOURCE_FACTORY", source.factory_spec)
    monkeypatch.setattr(
        ProductWindowsAutosportApp,
        "_hide_uncertain_economic_state",
        lambda self, _message: True,
    )

    def failed_restore(self):
        self.product_status.set(text("ui.product_runtime.status.base_session_reopen_failed"))
        self.status.set(text("ui.product_runtime.status.base_session_reopen_failed"))
        return False

    monkeypatch.setattr(
        ProductWindowsAutosportApp,
        "_restore_base_session_after_product",
        failed_restore,
    )

    app.start_product_runtime()

    assert app.product_status.value == text(
        "ui.product_runtime.status.base_session_reopen_failed"
    )


def test_close_keeps_accepted_stop_truth_when_runtime_callback_faults(tmp_path) -> None:
    class FaultingStopWorker(_CapturingWorker):
        def __init__(self):
            super().__init__()
            self.busy = True

        def request_stop(self, reason: str) -> bool:
            self.stop_reasons.append(reason)
            raise RuntimeError("runtime-specific stop callback failed")

    worker = FaultingStopWorker()
    app = _headless_app(tmp_path, worker=worker)

    app.close_app()

    assert worker.stop_reasons == ["app_close"]
    assert app._product_close_pending is True
    assert app.product_status.value == text("ui.product_runtime.status.close_wait")


def test_product_runtime_failure_keeps_workspace_fail_closed() -> None:
    apply_source = inspect.getsource(ProductWindowsAutosportApp._apply_product_message)
    poll_source = inspect.getsource(ProductWindowsAutosportApp._poll_product_worker)
    assert "self._product_runtime_target_workspace()" in apply_source
    assert "if self._product_last_stop is not None:" in poll_source
    assert "self._restore_base_session_after_product()" in poll_source
    assert "self._product_runtime_target_workspace()" in poll_source


def test_packaged_bridge_does_not_replace_trusted_worker_authority() -> None:
    from autosport import product_gui_worker

    worker_source = inspect.getsource(product_gui_worker.ProductGuiWorker._run)
    assert "_profiled_runtime_builder" in worker_source
    assert "issue_trusted_runtime_code_profile" in worker_source
    assert "expected_source_id is not None" in worker_source
