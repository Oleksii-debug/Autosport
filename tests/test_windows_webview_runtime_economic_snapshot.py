from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import inspect
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import autosport.product_gui_worker as worker_module

from autosport.causal_collector import GapState, SyncState
from autosport.continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionState,
)
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
    open_tickets = tuple(ticket for ticket in tickets if ticket.status == "open")
    if open_tickets:
        portfolio_scenario_count = 2
        portfolio_worst_case = Decimal("-10")
        portfolio_best_case = Decimal("10")
        portfolio_mean_case = Decimal("0")
    else:
        portfolio_scenario_count = 1
        portfolio_worst_case = Decimal("0")
        portfolio_best_case = Decimal("0")
        portfolio_mean_case = Decimal("0")
    return ProductGuiEconomicSnapshot(
        workspace=tmp_path,
        session_id="session-1",
        source_id="source-1",
        cycle_index=cycle_index,
        cycle_last_success_at="2026-10-03T08:00:00+00:00",
        paper_book_sha256="a" * 64,
        balance=Decimal("90"),
        committed_stake=sum(
            (ticket.stake for ticket in open_tickets),
            Decimal("0"),
        ),
        tickets=tickets,
        portfolio_mode="exact",
        portfolio_scenario_count=portfolio_scenario_count,
        portfolio_worst_case=portfolio_worst_case,
        portfolio_best_case=portfolio_best_case,
        portfolio_mean_case=portfolio_mean_case,
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


def test_runtime_snapshot_uses_trusted_resolved_workspace_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = Path("runtime-workspace")
    relative.mkdir()
    runtime = _runtime(relative)

    snapshot = _capture_runtime_economic_snapshot(runtime, _tick())

    assert snapshot.workspace == relative.resolve()
    assert snapshot.workspace.is_absolute()


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


def test_same_cycle_economic_snapshot_is_valid_for_degraded_provider_tick(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    controller._product_runtime_identity = (
        tmp_path,
        "session-1",
        "source-1",
    )
    controller._product_runtime_economic_snapshot = _snapshot(
        tmp_path,
        cycle_index=5,
    )

    assert controller._apply_product_runtime_economic_snapshot(
        _snapshot(tmp_path, cycle_index=5),
        cycle_index=5,
        session_id="session-1",
        source_id="source-1",
    )
    assert controller._product_runtime_economic_projection()["cycle_index"] == 5
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


def test_economic_snapshot_rejects_malformed_freshness_and_money(
    tmp_path: Path,
) -> None:
    valid = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="absolute Path"):
        replace(valid, workspace=Path("relative"))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(valid, cycle_last_success_at="2026-10-03T08:00:00")
    with pytest.raises(ValueError, match="finite Decimal"):
        replace(valid, balance=Decimal("NaN"))
    with pytest.raises(ValueError, match="SHA"):
        replace(valid, paper_book_sha256="not-a-sha")
    with pytest.raises(ValueError, match="committed stake"):
        replace(valid, committed_stake=Decimal("9"))
    with pytest.raises(ValueError, match="portfolio bounds"):
        replace(
            valid,
            portfolio_worst_case=Decimal("11"),
            portfolio_best_case=Decimal("10"),
        )


def test_runtime_snapshot_rejects_noncanonical_paperbook_path(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.coordinator.paper_book_path = tmp_path / "redirected-book.json"

    with pytest.raises(RuntimeError, match="PaperBook path"):
        _capture_runtime_economic_snapshot(runtime, _tick())


def test_unprofiled_exact_runtime_tick_cannot_mint_economic_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "_operation_fence", threading.RLock())
    worker = ProductGuiWorker(runtime_builder=lambda *_args: runtime)
    status = SimpleNamespace(session_id="session-1", source_id="source-1")
    calls = 0

    def tick(_self):
        nonlocal calls
        calls += 1
        if calls == 2:
            worker._stop_event.set()
        return _tick(cycle_index=calls)

    monkeypatch.setattr(
        AutonomousProductRuntime,
        "start",
        lambda _self: started_status,
    )
    monkeypatch.setattr(AutonomousProductRuntime, "tick", tick)
    monkeypatch.setattr(
        AutonomousProductRuntime,
        "stop",
        lambda _self, _reason: stopped_status,
    )
    monkeypatch.setattr(AutonomousProductRuntime, "close", lambda _self: None)

    worker._run(
        workspace=tmp_path,
        source_factory="test.module:source",
        expected_source_id=None,
        initial_bankroll="100",
        poll_seconds=0.001,
    )

    messages = []
    while True:
        message = worker.poll()
        if message is None:
            break
        messages.append(message)

    tick_messages = [message for message in messages if message.kind == "TICK"]
    assert len(tick_messages) == 1
    assert tick_messages[0].economic is None
    assert messages[-1].kind == "STOPPED"


def test_profile_revocation_during_tick_preserves_stop_not_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "_operation_fence", threading.RLock())
    worker = ProductGuiWorker()
    started_status = ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=SessionState.RUNNING,
        cycles_completed=0,
        last_success_at=None,
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )
    stopped_status = ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=SessionState.STOPPED,
        cycles_completed=0,
        last_success_at=None,
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )
    profile = object()

    monkeypatch.setattr(
        worker_module,
        "_register_started_product_runtime_origin",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        lambda _runtime: profile,
    )
    monkeypatch.setattr(
        worker_module,
        "revoke_trusted_runtime_code_profile",
        lambda _profile: True,
    )
    monkeypatch.setattr(
        worker_module,
        "_clear_started_product_runtime_origin",
        lambda _runtime: None,
    )
    monkeypatch.setattr(
        worker_module,
        "require_authoritative_trusted_runtime_code_profile",
        lambda _profile, **_kwargs: _profile,
    )
    monkeypatch.setattr(AutonomousProductRuntime, "start", lambda _self: status)

    def tick(_self):
        worker._stop_event.set()
        worker._stop_reason = "operator_stop"
        return _tick(cycle_index=1)

    monkeypatch.setattr(AutonomousProductRuntime, "tick", tick)
    monkeypatch.setattr(AutonomousProductRuntime, "stop", lambda _self, _reason: status)
    monkeypatch.setattr(AutonomousProductRuntime, "close", lambda _self: None)

    worker._run(
        workspace=tmp_path,
        source_factory="autosport.product_source:create_parlay_product_source",
        expected_source_id="source-1",
        initial_bankroll="100",
        poll_seconds=1,
        _profiled_runtime_builder=lambda *_args, **_kwargs: runtime,
    )

    messages = []
    while True:
        message = worker.poll()
        if message is None:
            break
        messages.append(message)

    assert [message.kind for message in messages] == ["STARTED", "STOPPED"]
    assert messages[-1].stop_reason == "operator_stop"
    assert all(message.kind != "ERROR" for message in messages)
    assert all(message.economic is None for message in messages)


def test_semantic_shell_exposes_readonly_economic_freshness_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/autosport/windows_web/index.html").read_text(encoding="utf-8")
    js = (root / "src/autosport/windows_web/app.js").read_text(encoding="utf-8")
    audit = (root / "src/autosport/windows_webview_audit.py").read_text(
        encoding="utf-8"
    )

    for control_id in (
        "product-runtime-economic-cycle",
        "product-runtime-economic-cycle-last-success",
        "product-runtime-paper-book-sha",
    ):
        assert f'id="{control_id}"' in html
        assert f'for="{control_id}"' in html
        assert f'byId("{control_id}")' in js
        assert f'"{control_id}": "input"' in audit
        assert f'"{control_id}",' in audit
    assert "runtimeEconomic.cycle_last_success_at" in js
    assert "runtimeEconomic.paper_book_sha256" in js


def test_terminal_durable_refresh_retires_runtime_snapshot_before_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path)
    controller._product_runtime_economic_snapshot = _snapshot(tmp_path)
    controller.strategy_id = "baseline-v1"

    class _RejectingSession:
        def __init__(self, *_args, **_kwargs) -> None:
            assert controller._product_runtime_economic_snapshot is None
            raise RuntimeError("focused retirement witness")

    monkeypatch.setattr(
        "autosport.windows_webview_shell.AutosportSession",
        _RejectingSession,
    )

    controller._refresh_economic_projection()

    assert controller._product_runtime_economic_snapshot is None
    assert tmp_path in controller._recovery_required_workspaces


def test_terminal_runtime_error_quarantines_visible_economics(
    tmp_path: Path,
) -> None:
    message = ProductGuiMessage(kind="ERROR", error_type="RuntimeError")
    controller = _controller(tmp_path, message)
    controller._product_runtime_economic_snapshot = _snapshot(tmp_path)

    controller._poll_workers()

    assert controller._product_runtime_economic_snapshot is None
    assert tmp_path in controller._recovery_required_workspaces
    assert "stale bank" not in controller.bank
    assert controller.tickets != ["stale ticket"]
    assert controller.evaluation != ["stale portfolio"]


def test_economic_projection_explicitly_denies_uncomputed_risk_and_real_money(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)
    empty = controller._product_runtime_economic_projection()
    assert empty["risk_policy_evaluated"] is False
    assert empty["real_money_authorized"] is False

    controller._product_runtime_economic_snapshot = _snapshot(tmp_path)
    current = controller._product_runtime_economic_projection()
    assert current["risk_policy_evaluated"] is False
    assert current["real_money_authorized"] is False


def test_runtime_worker_keeps_tick_and_snapshot_inside_one_operation_fence() -> None:
    source = inspect.getsource(ProductGuiWorker._run)

    assert "with runtime._operation_fence:" in source
    assert "tick = runtime.tick()" in source
    assert "economic = _economic_snapshot_builder(runtime, tick)" in source
    assert "runtime_profile is not None" in source
    assert "require_authoritative_trusted_runtime_code_profile" in source
    assert source.index("tick = runtime.tick()") < source.index(
        "economic = _economic_snapshot_builder(runtime, tick)"
    )
    assert _capture_runtime_economic_snapshot.__kwdefaults__ is None
    assert type(_capture_runtime_economic_snapshot.__defaults__) is tuple
    assert "AutosportSession" not in source


def test_terminal_session_close_failure_quarantines_visible_economics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, None)
    controller.product_worker = _IdleWorker()
    controller.strategy_id = "baseline-v1"
    controller.research_plan = None

    class _Book:
        balance = Decimal("100")
        committed_stake = Decimal("0")

    class _CloseFailSession:
        def __init__(self, workspace, *_args, **_kwargs) -> None:
            self.workspace = Path(workspace)
            self.strategy_id = "baseline-v1"
            self.book = _Book()

        def close(self) -> None:
            raise RuntimeError("close uncertainty")

    monkeypatch.setattr(
        "autosport.windows_webview_shell.AutosportSession",
        _CloseFailSession,
    )
    monkeypatch.setattr(
        "autosport.windows_webview_shell.ticket_lines",
        lambda _session: ["apparently healthy ticket"],
    )

    controller._refresh_economic_projection()

    assert tmp_path in controller._recovery_required_workspaces
    assert "100" not in controller.bank
    assert controller.tickets != ["apparently healthy ticket"]
    assert "віднов" in controller.status.casefold()
