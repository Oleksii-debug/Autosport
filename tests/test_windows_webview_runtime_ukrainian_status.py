from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import inspect
import threading
from types import SimpleNamespace

from autosport.causal_collector import GapState, SyncState
from autosport.product_gui_worker import ProductGuiEconomicSnapshot
from autosport.windows_webview_shell import AutosportWebController, web_shell_index_path


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _QueuedProductWorker:
    def __init__(self, message: object, *, busy: bool = False) -> None:
        self._message = message
        self.busy = busy
        self.stop_reasons: list[str] = []

    def poll(self):
        message = self._message
        self._message = None
        return message

    def request_stop(self, reason: str = "operator_stop") -> bool:
        self.stop_reasons.append(reason)
        return True


def _runtime_status(
    *,
    cycles_completed: int,
    provider_unavailable: bool = False,
    source_last_success_at: str | None = None,
    source_gap_state: str | None = None,
    source_sync_state: str | None = None,
    source_state_projection_backlog: bool = False,
    invalidation_pending_count: int = 0,
    invalidation_full_refresh_required: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-1",
        source_id="source-1",
        cycles_completed=cycles_completed,
        source_provider_unavailable=provider_unavailable,
        source_last_success_at=source_last_success_at,
        source_gap_state=source_gap_state,
        source_sync_state=source_sync_state,
        source_state_projection_backlog=source_state_projection_backlog,
        invalidation_pending_count=invalidation_pending_count,
        invalidation_full_refresh_required=invalidation_full_refresh_required,
    )


def _economic_snapshot(tmp_path: Path, *, cycle_index: int = 7) -> ProductGuiEconomicSnapshot:
    return ProductGuiEconomicSnapshot(
        workspace=tmp_path,
        session_id="session-1",
        source_id="source-1",
        cycle_index=cycle_index,
        cycle_last_success_at="2026-10-03T08:00:00+00:00",
        paper_book_sha256="a" * 64,
        balance=Decimal("10000"),
        committed_stake=Decimal("0"),
        tickets=(),
        portfolio_mode="exact",
        portfolio_scenario_count=1,
        portfolio_worst_case=Decimal("0"),
        portfolio_best_case=Decimal("0"),
        portfolio_mean_case=Decimal("0"),
    )


def _controller(tmp_path: Path, message: object) -> AutosportWebController:
    if (
        getattr(message, "kind", None) == "TICK"
        and not hasattr(message, "economic")
    ):
        message.economic = _economic_snapshot(
            tmp_path,
            cycle_index=message.tick.cycle_index,
        )
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller._recovery_required_workspaces = set()
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    controller.product_worker = _QueuedProductWorker(message)
    controller.product_runtime_status = ""
    controller._product_runtime_identity = None
    controller._product_runtime_economic_snapshot = None
    controller.strategy_id = "baseline-v1"
    controller.bank = ""
    controller.tickets = []
    controller.evaluation = []
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    controller._refresh_owner_projection = lambda: None
    controller._refresh_economic_projection = lambda: None
    return controller


def _project(tmp_path: Path, message: object) -> str:
    controller = _controller(tmp_path, message)
    controller._poll_workers()
    return controller.product_runtime_status


def test_started_runtime_status_uses_ukrainian_operator_copy(tmp_path: Path) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=_runtime_status(cycles_completed=0),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "source-1" in status


def test_tick_runtime_status_does_not_expose_internal_english_terms(tmp_path: Path) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="TICK",
            status=None,
            tick=SimpleNamespace(
                session_id="session-1",
                source_id="source-1",
                cycle_index=7,
                source_provider_unavailable=False,
                source_gap_states=(GapState.NONE.value,),
                source_sync_states=(SyncState.READY.value,),
                full_refresh_required=False,
                invalidation_backlog=False,
                last_success_at="2026-10-03T08:00:00+00:00",
                committed_delta_ids=("delta-1", "delta-2"),
                settled_ticket_ids=("ticket-1",),
            ),
            stop_reason=None,
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "runtime:" not in status
    assert "delta " not in status
    assert "settlement " not in status
    assert "7" in status


def test_stopped_runtime_status_uses_ukrainian_operator_copy(tmp_path: Path) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="STOPPED",
            status=_runtime_status(cycles_completed=3),
            tick=None,
            stop_reason="operator_stop",
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "оператор" in status.casefold()


def test_error_runtime_status_localizes_copy_without_python_type_leakage(
    tmp_path: Path,
) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="ERROR",
            status=None,
            tick=None,
            stop_reason="runtime_error",
            error_type="RuntimeError",
        ),
    )

    assert "PAPER" not in status
    assert "RuntimeError" not in status
    assert "BaseException" not in status
    assert "віднов" in status.casefold()


def test_runtime_identity_projection_comes_from_canonical_started_status(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=_runtime_status(cycles_completed=0),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )

    controller._poll_workers()

    assert controller._product_runtime_identity_projection() == {
        "workspace": str(tmp_path),
        "session_id": "session-1",
        "source_id": "source-1",
    }


def test_runtime_identity_drift_quarantines_workspace_and_requests_stop(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=_runtime_status(cycles_completed=0),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )
    controller._poll_workers()

    drift_worker = _QueuedProductWorker(
        SimpleNamespace(
            kind="TICK",
            status=None,
            tick=SimpleNamespace(
                session_id="session-2",
                source_id="source-1",
                cycle_index=1,
                committed_delta_ids=(),
                settled_ticket_ids=(),
            ),
            stop_reason=None,
            error_type=None,
        ),
        busy=True,
    )
    controller.product_worker = drift_worker

    controller._poll_workers()

    assert tmp_path in controller._recovery_required_workspaces
    assert drift_worker.stop_reasons == ["runtime_error"]
    assert "віднов" in controller.product_runtime_status.casefold()
    assert controller._product_runtime_identity_projection()["session_id"] == "session-1"


def test_runtime_state_contract_projects_canonical_identity_fields() -> None:
    source = inspect.getsource(AutosportWebController.state)

    assert '"workspace": runtime_identity["workspace"]' in source
    assert '"session_id": runtime_identity["session_id"]' in source
    assert '"source_id": runtime_identity["source_id"]' in source


def test_runtime_identity_is_keyboard_readable_in_semantic_shell() -> None:
    index = web_shell_index_path()
    html = index.read_text(encoding="utf-8")
    javascript = index.with_name("app.js").read_text(encoding="utf-8")

    for control_id in (
        "product-runtime-workspace",
        "product-runtime-session-id",
        "product-runtime-source-id",
    ):
        assert f'<label for="{control_id}">' in html
        assert f'id="{control_id}" type="text" readonly' in html
        assert f'byId("{control_id}")' in javascript

    assert 'productRuntime.workspace || "—"' in javascript
    assert 'productRuntime.session_id || "—"' in javascript
    assert 'productRuntime.source_id || "—"' in javascript


def _tick_message(
    *,
    provider_unavailable: bool,
    gap_states: tuple[str, ...] = (GapState.NONE.value,),
    sync_states: tuple[str, ...] = (SyncState.READY.value,),
    full_refresh_required: bool = False,
    invalidation_backlog: bool = False,
    last_success_at: str | None = "2026-10-03T08:00:00+00:00",
) -> SimpleNamespace:
    return SimpleNamespace(
        kind="TICK",
        status=None,
        tick=SimpleNamespace(
            session_id="session-1",
            source_id="source-1",
            cycle_index=7,
            source_provider_unavailable=provider_unavailable,
            source_gap_states=gap_states,
            source_sync_states=sync_states,
            full_refresh_required=full_refresh_required,
            invalidation_backlog=invalidation_backlog,
            last_success_at=last_success_at,
            committed_delta_ids=(),
            settled_ticket_ids=(),
        ),
        stop_reason=None,
        error_type=None,
    )


def test_provider_unavailable_zero_event_cycle_is_not_rendered_as_healthy_empty(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        _tick_message(
            provider_unavailable=True,
            last_success_at="2026-10-03T07:59:00+00:00",
        ),
    )

    controller._poll_workers()
    projection = controller._product_runtime_source_projection()

    assert projection["provider_unavailable"] is True
    assert projection["attention_required"] is True
    assert projection["last_success_at"] == "2026-10-03T07:59:00+00:00"
    assert "недоступ" in projection["status"].casefold()
    assert "порожн" in projection["status"].casefold()
    assert "здоров" in projection["status"].casefold()


def test_source_gap_or_backlog_is_explicit_attention_not_clean_cycle(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        _tick_message(
            provider_unavailable=False,
            gap_states=(GapState.DETECTED.value,),
            sync_states=(SyncState.GAP_DETECTED.value,),
            invalidation_backlog=True,
        ),
    )

    controller._poll_workers()
    projection = controller._product_runtime_source_projection()

    assert projection["provider_unavailable"] is False
    assert projection["attention_required"] is True
    assert "прогалин" in projection["status"].casefold()
    assert "потребують уваги" in projection["status"].casefold()


def test_clean_source_snapshot_is_bounded_not_global_health_claim(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        _tick_message(provider_unavailable=False),
    )

    controller._poll_workers()
    projection = controller._product_runtime_source_projection()

    assert projection["provider_unavailable"] is False
    assert projection["attention_required"] is False
    assert "останній канонічний цикл" in projection["status"].casefold()
    assert "здоров" not in projection["status"].casefold()
    assert "готов" not in projection["status"].casefold()


def test_unknown_source_projection_fails_closed_and_requests_runtime_stop(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        _tick_message(
            provider_unavailable=False,
            gap_states=("UNKNOWN_GAP_STATE",),
        ),
    )
    controller.product_worker.busy = True

    controller._poll_workers()

    assert tmp_path in controller._recovery_required_workspaces
    assert controller.product_worker.stop_reasons == ["runtime_error"]
    assert "віднов" in controller.product_runtime_status.casefold()


def test_runtime_state_contract_projects_source_degradation_truth() -> None:
    source = inspect.getsource(AutosportWebController.state)

    assert '"source_status": runtime_source["status"]' in source
    assert '"source_provider_unavailable": runtime_source[' in source
    assert '"source_attention_required": runtime_source[' in source
    assert '"source_last_success_at": runtime_source["last_success_at"]' in source


def test_runtime_source_truth_is_keyboard_readable_in_semantic_shell() -> None:
    index = web_shell_index_path()
    html = index.read_text(encoding="utf-8")
    javascript = index.with_name("app.js").read_text(encoding="utf-8")

    for control_id in (
        "product-runtime-source-health",
        "product-runtime-source-last-success",
    ):
        assert f'<label for="{control_id}">' in html
        assert f'id="{control_id}" type="text" readonly' in html
        assert f'byId("{control_id}")' in javascript

    assert "productRuntime.source_status" in javascript
    assert "productRuntime.source_last_success_at" in javascript


def test_started_runtime_reprojects_durable_source_truth_before_first_new_tick(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=_runtime_status(
                cycles_completed=4,
                provider_unavailable=True,
                source_last_success_at="2026-10-03T07:59:00+00:00",
                source_gap_state=GapState.DETECTED.value,
                source_sync_state=SyncState.GAP_DETECTED.value,
            ),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )

    controller._poll_workers()
    projection = controller._product_runtime_source_projection()

    assert projection["provider_unavailable"] is True
    assert projection["attention_required"] is True
    assert projection["last_success_at"] == "2026-10-03T07:59:00+00:00"
    assert "недоступ" in projection["status"].casefold()


def test_started_runtime_without_prior_source_observation_remains_unknown(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=_runtime_status(cycles_completed=0),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )

    controller._poll_workers()
    projection = controller._product_runtime_source_projection()

    assert projection["provider_unavailable"] is False
    assert projection["attention_required"] is None
    assert projection["last_success_at"] == ""
    assert "ще не підтверджено" in projection["status"].casefold()
