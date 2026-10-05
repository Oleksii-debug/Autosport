from __future__ import annotations

import threading
from pathlib import Path

import pytest

from autosport.continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionState,
)
from autosport.product_entrypoint import ProductEntrypointError
from autosport.product_gui_worker import ProductGuiMessage, ProductGuiWorker


def _status(state: SessionState, *, cycles: int) -> ContinuousSessionStatus:
    return ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=state,
        cycles_completed=cycles,
        last_success_at="2026-09-21T07:51:00+00:00" if cycles else None,
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )


def _tick() -> ContinuousTickResult:
    return ContinuousTickResult(
        session_id="session-1",
        cycle_index=1,
        source_id="source-1",
        source_provider_unavailable=False,
        source_gap_states=(),
        source_sync_states=(),
        committed_delta_ids=("delta-1",),
        delivered_delta_ids=("delta-1",),
        affected_input_ids=("market-1",),
        registered_input_ids=(),
        retired_input_ids=(),
        full_refresh_required=False,
        invalidation_backlog=False,
        settled_ticket_ids=(),
        settlement_evidence_ids=(),
        last_success_at="2026-09-21T07:51:00+00:00",
    )


class _FakeRuntime:
    def __init__(self) -> None:
        self.tick_called = threading.Event()
        self.stop_reason: str | None = None
        self.closed = False

    def start(self) -> ContinuousSessionStatus:
        return _status(SessionState.RUNNING, cycles=0)

    def tick(self) -> ContinuousTickResult:
        self.tick_called.set()
        return _tick()

    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        self.stop_reason = reason
        return _status(SessionState.STOPPED, cycles=1)

    def close(self) -> None:
        self.closed = True


def _drain(worker: ProductGuiWorker):
    messages = []
    while True:
        message = worker.poll()
        if message is None:
            return messages
        messages.append(message)


def test_worker_drives_runtime_and_cooperatively_stops_without_sleep(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    captured: list[tuple[Path, str, str]] = []

    def build(workspace: Path, source_factory: str, bankroll: str):
        captured.append((workspace, source_factory, bankroll))
        return runtime

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        initial_bankroll="10000",
        poll_seconds=60,
    )
    assert worker._thread is not None
    assert worker._thread.daemon is False
    assert runtime.tick_called.wait(2)
    assert worker.request_stop("operator_stop")
    assert worker.join(2)

    messages = _drain(worker)
    assert [message.kind for message in messages] == ["STARTED", "TICK", "STOPPED"]
    assert messages[-1].stop_reason == "operator_stop"
    assert runtime.stop_reason == "operator_stop"
    assert runtime.closed is True
    assert captured == [(tmp_path, "provider.module:factory", "10000")]
    assert worker.busy is False


def test_worker_publishes_error_type_only_and_never_exception_detail(
    tmp_path: Path,
) -> None:
    secret = "provider-token-should-never-reach-ui"

    def fail_build(_workspace: Path, _source_factory: str, _bankroll: str):
        raise RuntimeError(secret)

    worker = ProductGuiWorker(runtime_builder=fail_build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        poll_seconds=60,
    )
    assert worker.join(2)

    messages = _drain(worker)
    assert len(messages) == 1
    assert messages[0].kind == "ERROR"
    assert messages[0].error_type == "RuntimeError"
    assert secret not in repr(messages[0])


@pytest.mark.parametrize(
    ("source_factory", "poll_seconds"),
    [
        ("", 30),
        (" provider.module:factory", 30),
        ("provider.module:factory ", 30),
        ("provider.module:factory", 0),
        ("provider.module:factory", -1),
        ("provider.module:factory", float("nan")),
    ],
)
def test_worker_rejects_ambiguous_configuration_before_thread_start(
    tmp_path: Path,
    source_factory: str,
    poll_seconds: float,
) -> None:
    worker = ProductGuiWorker(runtime_builder=lambda *_args: _FakeRuntime())

    with pytest.raises(ValueError):
        worker.start(
            workspace=tmp_path,
            source_factory=source_factory,
            poll_seconds=poll_seconds,
        )

    assert worker.busy is False


def test_trusted_source_identity_forbids_caller_injected_runtime_builder(
    tmp_path: Path,
) -> None:
    worker = ProductGuiWorker(runtime_builder=lambda *_args: _FakeRuntime())

    assert worker.start(
        workspace=tmp_path,
        source_factory="autosport.product_source:create_parlay_product_source",
        expected_source_id="parlayapi:table_tennis",
        poll_seconds=60,
    )
    assert worker.join(2)

    messages = _drain(worker)
    assert len(messages) == 1
    assert messages[0].kind == "ERROR"
    assert messages[0].error_type == "ProductEntrypointError"


class _WrongStartedSourceRuntime(_FakeRuntime):
    def start(self) -> ContinuousSessionStatus:
        return ContinuousSessionStatus(
            session_id="session-1",
            source_id="other:provider",
            state=SessionState.RUNNING,
            cycles_completed=0,
            last_success_at=None,
            last_error_code=None,
            last_full_refresh_at=None,
            settlement_evidence=(),
        )


class _WrongTickSourceRuntime(_FakeRuntime):
    def tick(self) -> ContinuousTickResult:
        self.tick_called.set()
        tick = _tick()
        return ContinuousTickResult(
            session_id=tick.session_id,
            cycle_index=tick.cycle_index,
            source_id="other:provider",
            source_provider_unavailable=tick.source_provider_unavailable,
            source_gap_states=tick.source_gap_states,
            source_sync_states=tick.source_sync_states,
            committed_delta_ids=tick.committed_delta_ids,
            delivered_delta_ids=tick.delivered_delta_ids,
            affected_input_ids=tick.affected_input_ids,
            registered_input_ids=tick.registered_input_ids,
            retired_input_ids=tick.retired_input_ids,
            full_refresh_required=tick.full_refresh_required,
            invalidation_backlog=tick.invalidation_backlog,
            settled_ticket_ids=tick.settled_ticket_ids,
            settlement_evidence_ids=tick.settlement_evidence_ids,
            last_success_at=tick.last_success_at,
        )


def test_runtime_status_identity_guard_rejects_started_source_drift() -> None:
    runtime = _WrongStartedSourceRuntime()

    from autosport.product_gui_worker import _require_runtime_status_identity

    with pytest.raises(ProductEntrypointError):
        _require_runtime_status_identity(
            runtime.start(),
            expected_source_id="source-1",
        )


def test_runtime_tick_identity_guard_rejects_source_drift() -> None:
    runtime = _WrongTickSourceRuntime()

    from autosport.product_gui_worker import _require_runtime_tick_identity

    with pytest.raises(ProductEntrypointError):
        _require_runtime_tick_identity(
            runtime.tick(),
            expected_source_id="source-1",
        )


def test_runtime_status_identity_guard_rejects_session_drift() -> None:
    from autosport.product_gui_worker import _require_runtime_status_identity

    with pytest.raises(ProductEntrypointError):
        _require_runtime_status_identity(
            _status(SessionState.STOPPED, cycles=1),
            expected_source_id="source-1",
            expected_session_id="different-session",
            expected_state=SessionState.STOPPED,
        )


def test_runtime_status_identity_guard_rejects_wrong_terminal_state() -> None:
    from autosport.product_gui_worker import _require_runtime_status_identity

    with pytest.raises(ProductEntrypointError):
        _require_runtime_status_identity(
            _status(SessionState.RUNNING, cycles=1),
            expected_source_id="source-1",
            expected_session_id="session-1",
            expected_state=SessionState.STOPPED,
        )


def test_runtime_tick_identity_guard_rejects_session_drift() -> None:
    from autosport.product_gui_worker import _require_runtime_tick_identity

    with pytest.raises(ProductEntrypointError):
        _require_runtime_tick_identity(
            _tick(),
            expected_source_id="source-1",
            expected_session_id="different-session",
        )


def test_runtime_status_identity_guard_rejects_string_subclass_source() -> None:
    from autosport.product_gui_worker import _require_runtime_status_identity

    class HostileSourceId(str):
        def __eq__(self, _other: object) -> bool:
            raise AssertionError("source subtype equality must not execute")

    status = _status(SessionState.RUNNING, cycles=0)
    object.__setattr__(status, "source_id", HostileSourceId("source-1"))

    with pytest.raises(ProductEntrypointError):
        _require_runtime_status_identity(
            status,
            expected_source_id="source-1",
            expected_state=SessionState.RUNNING,
        )


def test_runtime_tick_identity_guard_rejects_string_subclass_source() -> None:
    from autosport.product_gui_worker import _require_runtime_tick_identity

    class HostileSourceId(str):
        def __eq__(self, _other: object) -> bool:
            raise AssertionError("source subtype equality must not execute")

    tick = _tick()
    object.__setattr__(tick, "source_id", HostileSourceId("source-1"))

    with pytest.raises(ProductEntrypointError):
        _require_runtime_tick_identity(
            tick,
            expected_source_id="source-1",
            expected_session_id="session-1",
        )


def test_product_gui_message_rejects_kind_subclass_without_dispatch() -> None:
    class HostileKind(str):
        def __hash__(self) -> int:
            raise AssertionError("kind subtype hashing must not execute")

    with pytest.raises(ValueError):
        ProductGuiMessage(
            kind=HostileKind("ERROR"),
            error_type="RuntimeError",
        )


def test_product_gui_message_rejects_status_subclass() -> None:
    class StatusSubclass(ContinuousSessionStatus):
        pass

    status = _status(SessionState.RUNNING, cycles=0)
    subclass_status = StatusSubclass(
        session_id=status.session_id,
        source_id=status.source_id,
        state=status.state,
        cycles_completed=status.cycles_completed,
        last_success_at=status.last_success_at,
        last_error_code=status.last_error_code,
        last_full_refresh_at=status.last_full_refresh_at,
        settlement_evidence=status.settlement_evidence,
    )

    with pytest.raises(ValueError):
        ProductGuiMessage(kind="STARTED", status=subclass_status)


@pytest.mark.parametrize(
    "error_type",
    ["", "Runtime Error", "X" * 65],
)
def test_product_gui_message_rejects_unbounded_error_type(error_type: str) -> None:
    with pytest.raises(ValueError):
        ProductGuiMessage(kind="ERROR", error_type=error_type)


@pytest.mark.parametrize(
    "reason",
    ["", "Operator Stop", "UPPER", "x" * 65],
)
def test_product_gui_message_rejects_noncanonical_stop_reason(reason: str) -> None:
    with pytest.raises(ValueError):
        ProductGuiMessage(
            kind="STOPPED",
            status=_status(SessionState.STOPPED, cycles=1),
            stop_reason=reason,
        )
