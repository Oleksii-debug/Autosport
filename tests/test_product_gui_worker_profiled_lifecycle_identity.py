from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionState,
)
from autosport.product_entrypoint import ProductEntrypointError
from autosport.product_gui_worker import (
    ProductGuiWorker,
    _ProfiledSourceBinding,
    _capture_profiled_runtime_builder,
    _require_profiled_status_identity,
    _require_profiled_tick_identity,
)


def _status(
    state: SessionState = SessionState.RUNNING,
    *,
    source_id: str = "source-1",
    session_id: str = "session-1",
    cycles: int = 0,
) -> ContinuousSessionStatus:
    return ContinuousSessionStatus(
        session_id=session_id,
        source_id=source_id,
        state=state,
        cycles_completed=cycles,
        last_success_at=None,
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )


def _tick(
    *,
    source_id: str = "source-1",
    session_id: str = "session-1",
    cycle_index: int = 1,
) -> ContinuousTickResult:
    return ContinuousTickResult(
        session_id=session_id,
        cycle_index=cycle_index,
        source_id=source_id,
        source_provider_unavailable=False,
        source_gap_states=(),
        source_sync_states=(),
        committed_delta_ids=(),
        delivered_delta_ids=(),
        affected_input_ids=(),
        registered_input_ids=(),
        retired_input_ids=(),
        full_refresh_required=False,
        invalidation_backlog=False,
        settled_ticket_ids=(),
        settlement_evidence_ids=(),
        last_success_at=None,
    )


def test_profiled_status_requires_exact_configured_source() -> None:
    with pytest.raises(ProductEntrypointError):
        _require_profiled_status_identity(
            _status(source_id="other-source"),
            expected_source_id="source-1",
            expected_state=SessionState.RUNNING,
        )


def test_profiled_status_requires_one_lifecycle_session() -> None:
    with pytest.raises(ProductEntrypointError):
        _require_profiled_status_identity(
            _status(state=SessionState.STOPPED),
            expected_source_id="source-1",
            expected_session_id="other-session",
            expected_state=SessionState.STOPPED,
        )


def test_profiled_status_requires_exact_lifecycle_state() -> None:
    with pytest.raises(ProductEntrypointError):
        _require_profiled_status_identity(
            _status(state=SessionState.RUNNING),
            expected_source_id="source-1",
            expected_session_id="session-1",
            expected_state=SessionState.STOPPED,
        )


def test_profiled_tick_requires_exact_source_and_session() -> None:
    with pytest.raises(ProductEntrypointError):
        _require_profiled_tick_identity(
            _tick(source_id="other-source"),
            expected_source_id="source-1",
            expected_session_id="session-1",
        )
    with pytest.raises(ProductEntrypointError):
        _require_profiled_tick_identity(
            _tick(session_id="other-session"),
            expected_source_id="source-1",
            expected_session_id="session-1",
        )


def test_profiled_lifecycle_rejects_source_string_subclass_before_equality() -> None:
    class HostileSourceId(str):
        def __eq__(self, _other: object) -> bool:
            raise AssertionError("source subtype equality must not execute")

    status = _status()
    object.__setattr__(status, "source_id", HostileSourceId("source-1"))

    with pytest.raises(ProductEntrypointError):
        _require_profiled_status_identity(
            status,
            expected_source_id="source-1",
            expected_state=SessionState.RUNNING,
        )


def test_profiled_tick_rejects_invalid_cycle_index() -> None:
    with pytest.raises(ProductEntrypointError):
        _require_profiled_tick_identity(
            _tick(cycle_index=0),
            expected_source_id="source-1",
            expected_session_id="session-1",
        )


def test_profiled_worker_rejects_relative_workspace_before_thread_start() -> None:
    worker = ProductGuiWorker()

    with pytest.raises(ValueError):
        worker.start(
            workspace="relative-workspace",
            source_factory="autosport.product_source:create_parlay_product_source",
            expected_source_id="parlayapi:table_tennis",
            poll_seconds=30.0,
        )

    assert worker.busy is False


def test_profiled_worker_rejects_poll_seconds_subclass_before_float_dispatch(
    tmp_path: Path,
) -> None:
    class HostileFloat(float):
        def __float__(self) -> float:
            raise AssertionError("poll subtype conversion must not execute")

    worker = ProductGuiWorker()

    with pytest.raises(ValueError):
        worker.start(
            workspace=tmp_path,
            source_factory="autosport.product_source:create_parlay_product_source",
            expected_source_id="parlayapi:table_tennis",
            poll_seconds=HostileFloat(30.0),
        )

    assert worker.busy is False


@pytest.mark.parametrize("bankroll", ["", " 10000", "10000 ", "x" * 129])
def test_profiled_worker_rejects_noncanonical_bankroll(
    tmp_path: Path,
    bankroll: str,
) -> None:
    worker = ProductGuiWorker()

    with pytest.raises(ValueError):
        worker.start(
            workspace=tmp_path,
            source_factory="autosport.product_source:create_parlay_product_source",
            expected_source_id="parlayapi:table_tennis",
            initial_bankroll=bankroll,
            poll_seconds=30.0,
        )

    assert worker.busy is False


def test_profiled_builder_closes_runtime_on_manifest_source_subclass(
    tmp_path: Path,
) -> None:
    class Source:
        source_id = "source-1"
        stream_epoch = "epoch-1"
        workspace = tmp_path

        def fetch_catalog_page(self) -> None:
            return None

        def fetch_deltas(self) -> tuple[()]:
            return ()

        def resolve_event(self) -> None:
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


def test_profiled_worker_run_rejects_wrong_started_source_before_profile_issue(
    tmp_path: Path,
) -> None:
    class Runtime:
        def __init__(self) -> None:
            self.stop_reasons: list[str] = []
            self.closed = False

        def start(self) -> ContinuousSessionStatus:
            return _status(source_id="wrong-source")

        def stop(self, reason: str) -> ContinuousSessionStatus:
            self.stop_reasons.append(reason)
            return _status(
                state=SessionState.STOPPED,
                source_id="wrong-source",
            )

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()
    worker = ProductGuiWorker()
    worker._busy = True

    worker._run(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        expected_source_id="source-1",
        initial_bankroll="10000",
        poll_seconds=30.0,
        _profiled_runtime_builder=lambda *_args, **_kwargs: runtime,
    )

    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "ERROR"
    assert terminal.error_type == "ProductEntrypointError"
    assert runtime.stop_reasons == ["runtime_error"]
    assert runtime.closed is True
    assert worker.busy is False
