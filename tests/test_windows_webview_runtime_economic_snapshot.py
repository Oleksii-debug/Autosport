from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import inspect
from pathlib import Path
import threading

from autosport.causal_collector import GapState, SyncState
from autosport.continuous_session import ContinuousTickResult
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.product_gui_worker import (
    ProductGuiEconomicSnapshot,
    ProductGuiEconomicTicket,
    ProductGuiMessage,
    ProductGuiWorker,
    _capture_runtime_economic_snapshot,
)
from autosport.product_runtime import AutonomousProductRuntime
from autosport.windows_webview_shell import AutosportWebController


class _Coordinator:
    def __init__(self, workspace: Path, *, session_id: str) -> None:
        self.workspace = workspace
        self.session_id = session_id
        self.paper_book_path = workspace / "paper_book.json"


class _Manifest:
    source_id = "source-1"
    initial_bankroll = "100"


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _QueuedProductWorker:
    def __init__(self, message: ProductGuiMessage, *, busy: bool = True) -> None:
        self._message = message
        self.busy = busy
        self.stop_requested = False
        self.stop_reasons: list[str] = []

    def poll(self):
        message = self._message
        self._message = None
        return message

    def request_stop(self, reason: str = "operator_stop") -> bool:
        if not self.busy:
            return False
        self.stop_requested = True
        self.stop_reasons.append(reason)
        return True


def _tick(*, cycle_index: int = 1) -> ContinuousTickResult:
    return ContinuousTickResult(
        session_id="session-1",
        cycle_index=cycle_index,
        source_id="source-1",
        source_provider_unavailable=False,
        source_gap_states=(GapState.NONE.value,),
        source_sync_states=(SyncState.READY.value,),
        committed_delta_ids=(),
        delivered_delta_ids=(),
        affected_input_ids=(),
        registered_input_ids=(),
        retired_input_ids=(),
        full_refresh_required=False,
        invalidation_backlog=False,
        settled_ticket_ids=(),
        settlement_evidence_ids=(),
        last_success_at="2026-10-03T08:00:00+00:00",
    )


def _runtime(tmp_path: Path) -> AutonomousProductRuntime:
    runtime = object.__new__(AutonomousProductRuntime)
    object.__setattr__(runtime, "workspace", tmp_path)
    object.__setattr__(runtime, "manifest", _Manifest())
    object.__setattr__(
        runtime,
        "coordinator",
        _Coordinator(tmp_path, session_id="session-1"),
    )
    return runtime


def _snapshot(
    tmp_path: Path,
    *,
    cycle_index: int = 1,
    tickets: tuple[ProductGuiEconomicTicket, ...] = (),
) -> ProductGuiEconomicSnapshot:
    return ProductGuiEconomicSnapshot(
        workspace=tmp_path,
        session_id="session-1",
        source_id="source-1",
        cycle_index=cycle_index,
        cycle_last_success_at="2026-10-03T08:00:00+00:00",
        paper_book_sha256="a" * 64,
        balance=Decimal("90"),
        committed_stake=Decimal("10"),
        tickets=tickets,
        portfolio_mode="exact",
        portfolio_scenario_count=2,
        portfolio_worst_case=Decimal("-10"),
        portfolio_best_case=Decimal("10"),
        portfolio_mean_case=Decimal("0"),
    )


def _controller(
    tmp_path: Path,
    message: ProductGuiMessage | None = None,
) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller._recovery_required_workspaces = set()
    controller.strategy_id = "baseline-v1"
    controller.research_plan = None
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    if message is None:
        message = ProductGuiMessage(
            kind="TICK",
            tick=_tick(),
            economic=_snapshot(tmp_path),
        )
    controller.product_worker = _QueuedProductWorker(message)
    controller._product_runtime_identity = None
    controller._product_runtime_economic_snapshot = None
    controller.product_runtime_status = ""
    controller.product_runtime_source_status = ""
    controller._product_runtime_source_provider_unavailable = None
    controller._product_runtime_source_attention_required = None
    controller._product_runtime_source_last_success_at = None
    controller.bank = "stale bank"
    controller.tickets = ["stale ticket"]
    controller.evaluation = ["stale portfolio"]
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    controller._refresh_owner_projection = lambda: None
    return controller


def test_runtime_snapshot_uses_one_exact_durable_paperbook_image(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    book.open_ticket(
        (
            TicketLeg(
                "event-1",
                "market-1",
                "selection-1",
                Decimal("2"),
                sport="soccer",
            ),
        ),
        Decimal("10"),
        placed_at="2026-10-03T07:59:00+00:00",
    )
    book_path = tmp_path / "paper_book.json"
    book.save(book_path)
    expected_payload = book_path.read_bytes()
    tick = _tick()

    snapshot = _capture_runtime_economic_snapshot(_runtime(tmp_path), tick)

    assert snapshot.workspace == tmp_path
    assert snapshot.session_id == tick.session_id
    assert snapshot.source_id == tick.source_id
    assert snapshot.cycle_index == tick.cycle_index
    assert snapshot.cycle_last_success_at == tick.last_success_at
    assert snapshot.paper_book_sha256 == sha256(expected_payload).hexdigest()
    assert snapshot.balance == Decimal("90")
    assert snapshot.committed_stake == Decimal("10")
    assert len(snapshot.tickets) == 1
    assert snapshot.tickets[0].status == "open"
    assert snapshot.tickets[0].stake == Decimal("10")
    assert snapshot.tickets[0].combined_odds == Decimal("2")
    assert snapshot.portfolio_mode == "exact"
    assert snapshot.portfolio_scenario_count == 2
    assert snapshot.portfolio_worst_case == Decimal("-10")
    assert snapshot.portfolio_best_case == Decimal("10")


def test_runtime_snapshot_without_paperbook_is_explicit_initial_state(
    tmp_path: Path,
) -> None:
    snapshot = _capture_runtime_economic_snapshot(_runtime(tmp_path), _tick())

    assert snapshot.paper_book_sha256 is None
    assert snapshot.balance == Decimal("100")
    assert snapshot.committed_stake == Decimal("0")
    assert snapshot.tickets == ()
    assert snapshot.portfolio_mode == "exact"
    assert snapshot.portfolio_scenario_count == 1


def test_tick_message_rejects_economic_identity_drift(tmp_path: Path) -> None:
    tick = _tick()
    valid = _snapshot(tmp_path)

    ProductGuiMessage(kind="TICK", tick=tick, economic=valid)

    for forged in (
        replace(valid, session_id="session-2"),
        replace(valid, source_id="source-2"),
        replace(valid, cycle_index=2),
    ):
        try:
            ProductGuiMessage(kind="TICK", tick=tick, economic=forged)
        except ValueError as exc:
            assert "does not match runtime tick identity" in str(exc)
        else:
            raise AssertionError("drifted economic snapshot was accepted")


def test_controller_applies_exact_runtime_snapshot_to_visible_economics(
    tmp_path: Path,
) -> None:
    ticket = ProductGuiEconomicTicket(
        ticket_id="ticket-1",
        status="open",
        stake=Decimal("10"),
        combined_odds=Decimal("2"),
        payout=Decimal("0"),
        legs=("event-1/market-1/selection-1@2",),
    )
    snapshot = _snapshot(tmp_path, tickets=(ticket,))
    controller = _controller(tmp_path)
    controller._product_runtime_identity = (
        tmp_path,
        "session-1",
        "source-1",
    )

    assert controller._apply_product_runtime_economic_snapshot(
        snapshot,
        cycle_index=1,
        session_id="session-1",
        source_id="source-1",
    )

    projection = controller._product_runtime_economic_projection()
    assert projection["available"] is True
    assert projection["cycle_index"] == 1
    assert projection["cycle_last_success_at"] == "2026-10-03T08:00:00+00:00"
    assert projection["paper_book_sha256"] == "a" * 64
    assert projection["balance"] == "90"
    assert projection["committed_stake"] == "10"
    assert projection["ticket_count"] == 1
    assert projection["open_ticket_count"] == 1
    assert "90" in controller.bank
    assert "ticket-1" not in controller.tickets[0]
    assert "OPEN" in controller.tickets[0]
    assert any("цикл 1" in line for line in controller.evaluation)
    assert any("не створює нового висновку" in line for line in controller.evaluation)


def test_missing_runtime_economic_snapshot_quarantines_instead_of_showing_stale_money(
    tmp_path: Path,
) -> None:
    message = ProductGuiMessage(kind="TICK", tick=_tick(), economic=None)
    controller = _controller(tmp_path, message)
    controller.product_worker.busy = True

    controller._poll_workers()

    assert tmp_path in controller._recovery_required_workspaces
    assert controller.product_worker.stop_reasons == ["runtime_error"]
    assert controller._product_runtime_economic_snapshot is None
    assert "stale bank" not in controller.bank
    assert controller.tickets != ["stale ticket"]
    assert controller.evaluation != ["stale portfolio"]


def test_runtime_tick_projects_fresh_economics_without_ui_thread_session_reopen(
    tmp_path: Path,
) -> None:
    message = ProductGuiMessage(
        kind="TICK",
        tick=_tick(cycle_index=4),
        economic=_snapshot(tmp_path, cycle_index=4),
    )
    controller = _controller(tmp_path, message)
    controller._refresh_economic_projection = lambda: (_ for _ in ()).throw(
        AssertionError("live TICK must not reopen AutosportSession on the UI thread")
    )

    controller._poll_workers()

    projection = controller._product_runtime_economic_projection()
    assert projection["cycle_index"] == 4
    assert projection["balance"] == "90"
    assert controller.product_runtime_status
    assert tmp_path not in controller._recovery_required_workspaces


def test_lower_runtime_economic_cycle_fails_closed(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.product_worker.busy = True
    controller._product_runtime_identity = (
        tmp_path,
        "session-1",
        "source-1",
    )
    prior = _snapshot(tmp_path, cycle_index=5)
    controller._product_runtime_economic_snapshot = prior

    assert not controller._apply_product_runtime_economic_snapshot(
        _snapshot(tmp_path, cycle_index=4),
        cycle_index=4,
        session_id="session-1",
        source_id="source-1",
    )

    assert tmp_path in controller._recovery_required_workspaces
    assert controller.product_worker.stop_reasons == ["runtime_error"]


def test_runtime_worker_keeps_tick_and_snapshot_inside_one_operation_fence() -> None:
    source = inspect.getsource(ProductGuiWorker._run)

    assert "with runtime._operation_fence:" in source
    assert "tick = runtime.tick()" in source
    assert "economic = _economic_snapshot_builder(runtime, tick)" in source
    assert source.index("tick = runtime.tick()") < source.index(
        "economic = _economic_snapshot_builder(runtime, tick)"
    )
    assert "AutosportSession" not in source
