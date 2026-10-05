from __future__ import annotations

import threading
from types import SimpleNamespace
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


@pytest.mark.parametrize(
    "reason",
    ["", "operator stop", "UPPER", "x" * 65],
)
def test_worker_rejects_noncanonical_stop_reason_before_state_change(
    tmp_path: Path,
    reason: str,
) -> None:
    worker = ProductGuiWorker(runtime_builder=lambda *_args: _FakeRuntime())

    with pytest.raises(ValueError):
        worker.request_stop(reason)

    assert worker.busy is False


def test_profiled_worker_rejects_relative_workspace_before_thread_start() -> None:
    worker = ProductGuiWorker()

    with pytest.raises(ValueError):
        worker.start(
            workspace="relative-workspace",
            source_factory="autosport.product_source:create_parlay_product_source",
            expected_source_id="parlayapi:table_tennis",
            poll_seconds=60,
        )

    assert worker.busy is False


def test_worker_rejects_workspace_path_subclass_before_fspath_dispatch(
    tmp_path: Path,
) -> None:
    concrete_path_type = type(tmp_path)

    class HostilePath(concrete_path_type):
        def __str__(self) -> str:
            raise AssertionError("workspace subtype conversion must not execute")

    hostile = HostilePath(tmp_path)
    worker = ProductGuiWorker()

    with pytest.raises(ValueError):
        worker.start(
            workspace=hostile,
            source_factory="autosport.product_source:create_parlay_product_source",
            expected_source_id="parlayapi:table_tennis",
            poll_seconds=60,
        )

    assert worker.busy is False


def test_runtime_status_guard_rejects_cycle_count_subclass() -> None:
    from autosport.product_gui_worker import _require_runtime_status_identity

    class HostileInt(int):
        def __lt__(self, _other: object) -> bool:
            raise AssertionError("cycle subtype comparison must not execute")

    status = _status(SessionState.RUNNING, cycles=0)
    object.__setattr__(status, "cycles_completed", HostileInt(0))

    with pytest.raises(ProductEntrypointError):
        _require_runtime_status_identity(
            status,
            expected_source_id="source-1",
            expected_state=SessionState.RUNNING,
        )


def test_runtime_tick_guard_rejects_collection_subclass() -> None:
    from autosport.product_gui_worker import _require_runtime_tick_identity

    class HostileTuple(tuple):
        def __len__(self) -> int:
            raise AssertionError("tuple subtype length must not execute")

    tick = _tick()
    object.__setattr__(
        tick,
        "committed_delta_ids",
        HostileTuple(tick.committed_delta_ids),
    )

    with pytest.raises(ProductEntrypointError):
        _require_runtime_tick_identity(
            tick,
            expected_source_id="source-1",
            expected_session_id="session-1",
        )


def test_runtime_tick_guard_rejects_invalid_cycle_index() -> None:
    from autosport.product_gui_worker import _require_runtime_tick_identity

    tick = _tick()
    object.__setattr__(tick, "cycle_index", 0)

    with pytest.raises(ProductEntrypointError):
        _require_runtime_tick_identity(
            tick,
            expected_source_id="source-1",
            expected_session_id="session-1",
        )


def test_runtime_status_guard_rejects_unbounded_last_success() -> None:
    from autosport.product_gui_worker import _require_runtime_status_identity

    status = _status(SessionState.RUNNING, cycles=1)
    object.__setattr__(status, "last_success_at", "x" * 129)

    with pytest.raises(ProductEntrypointError):
        _require_runtime_status_identity(
            status,
            expected_source_id="source-1",
            expected_state=SessionState.RUNNING,
        )


def test_worker_rejects_poll_seconds_subclass_before_float_dispatch(
    tmp_path: Path,
) -> None:
    class HostileFloat(float):
        def __float__(self) -> float:
            raise AssertionError("poll_seconds subtype conversion must not execute")

    worker = ProductGuiWorker(runtime_builder=lambda *_args: _FakeRuntime())

    with pytest.raises(ValueError):
        worker.start(
            workspace=tmp_path,
            source_factory="provider.module:factory",
            poll_seconds=HostileFloat(30.0),
        )

    assert worker.busy is False


@pytest.mark.parametrize(
    "bankroll",
    ["", " 10000", "10000 ", "x" * 129],
)
def test_worker_rejects_noncanonical_initial_bankroll(
    tmp_path: Path,
    bankroll: str,
) -> None:
    worker = ProductGuiWorker(runtime_builder=lambda *_args: _FakeRuntime())

    with pytest.raises(ValueError):
        worker.start(
            workspace=tmp_path,
            source_factory="provider.module:factory",
            initial_bankroll=bankroll,
            poll_seconds=60,
        )

    assert worker.busy is False


def test_profiled_builder_closes_runtime_on_invalid_manifest_source_identity(
    tmp_path: Path,
) -> None:
    from autosport.product_gui_worker import (
        _ProfiledSourceBinding,
        _capture_profiled_runtime_builder,
    )

    class Source:
        source_id = "source-1"
        stream_epoch = "epoch-1"
        workspace = tmp_path

        def fetch_catalog_page(self):
            return None

        def fetch_deltas(self):
            return ()

        def resolve_event(self):
            return None

    class HostileSourceId(str):
        pass

    class Runtime:
        def __init__(self, source: Source) -> None:
            self.workspace = tmp_path
            self.manifest = SimpleNamespace(source_id=HostileSourceId("source-1"))
            self.collector = SimpleNamespace(source=source)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    source = Source()
    runtime = Runtime(source)
    builder = _capture_profiled_runtime_builder(
        source_bindings=(
            _ProfiledSourceBinding(
                factory_spec="provider.module:factory",
                provider_source_id="source-1",
                factory=lambda: source,
            ),
        ),
        runtime_factory=lambda **_kwargs: runtime,
        runtime_type=Runtime,
        path_type=Path,
    )

    with pytest.raises(ProductEntrypointError):
        builder(
            tmp_path,
            "provider.module:factory",
            "10000",
            expected_source_id="source-1",
        )

    assert runtime.closed is True


def test_worker_keeps_mutation_fence_until_terminal_stop_is_consumed(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    worker = ProductGuiWorker(runtime_builder=lambda *_args: runtime)

    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        poll_seconds=60,
    )
    assert runtime.tick_called.wait(2)
    assert worker.request_stop("operator_stop")
    assert worker.join(2)

    # Thread completion alone is not terminal acknowledgement to the GUI.
    assert worker.busy is True

    first = worker.poll()
    second = worker.poll()
    terminal = worker.poll()
    assert first is not None and first.kind == "STARTED"
    assert second is not None and second.kind == "TICK"
    assert terminal is not None and terminal.kind == "STOPPED"
    assert worker.busy is False
