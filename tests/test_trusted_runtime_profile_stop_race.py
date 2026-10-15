from __future__ import annotations

import threading
from pathlib import Path

import autosport.product_gui_worker as worker_module
from autosport.product_gui_worker import ProductGuiWorker


_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


class _Runtime:
    def __init__(self) -> None:
        self._status = object()
        self.tick_entered = threading.Event()
        self.release_tick = threading.Event()
        self.stop_reason: str | None = None
        self.closed = False

    def start(self) -> object:
        return self._status

    def tick(self) -> object:
        self.tick_entered.set()
        assert self.release_tick.wait(2.0)
        return object()

    def request_stop(self, _reason: str) -> None:
        self.release_tick.set()

    def stop(self, reason: str) -> object:
        self.stop_reason = reason
        self.release_tick.set()
        return self._status

    def close(self) -> None:
        self.closed = True


def test_stop_acceptance_serializes_with_trusted_profile_issuance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _Runtime()
    profile = object()
    register_entered = threading.Event()
    allow_register = threading.Event()
    issue_called = threading.Event()
    revoke_called = threading.Event()
    cleared: list[object] = []

    def register(*_args, **_kwargs) -> None:
        register_entered.set()
        assert allow_register.wait(2.0)

    def issue(observed_runtime: object) -> object:
        assert observed_runtime is runtime
        issue_called.set()
        return profile

    def revoke(observed_profile: object) -> bool:
        assert observed_profile is profile
        revoke_called.set()
        return True

    def clear(observed_runtime: object) -> None:
        assert observed_runtime is runtime
        cleared.append(observed_runtime)

    monkeypatch.setattr(
        worker_module,
        "_register_started_product_runtime_origin",
        register,
    )
    monkeypatch.setattr(worker_module, "issue_trusted_runtime_code_profile", issue)
    monkeypatch.setattr(worker_module, "revoke_trusted_runtime_code_profile", revoke)
    monkeypatch.setattr(worker_module, "_clear_started_product_runtime_origin", clear)

    worker = ProductGuiWorker()
    # Drive the private lifecycle seam directly so the test can inject a deterministic
    # profiled builder without weakening the public constructor's closed-registry rule.
    with worker._lock:
        worker._busy = True

    run_thread = threading.Thread(
        target=worker._run,
        kwargs={
            "workspace": tmp_path,
            "source_factory": _FACTORY_SPEC,
            "expected_source_id": _PROVIDER_SOURCE_ID,
            "initial_bankroll": "10000",
            "poll_seconds": 60.0,
            "_profiled_runtime_builder": lambda *_args, **_kwargs: runtime,
        },
    )
    run_thread.start()
    assert register_entered.wait(2.0)

    # Registration/issuance and STOP acceptance must share one lifecycle lock. Before
    # this repair the registration callback ran outside the lock, allowing STOP to
    # return and then a trusted profile to be issued afterward.
    assert worker._lock.locked() is True

    stop_result: list[bool] = []
    stop_thread = threading.Thread(
        target=lambda: stop_result.append(worker.request_stop("operator_stop"))
    )
    stop_thread.start()

    allow_register.set()
    stop_thread.join(2.0)
    assert not stop_thread.is_alive()
    assert stop_result == [True]
    assert issue_called.is_set()
    assert revoke_called.is_set()

    run_thread.join(2.0)
    assert not run_thread.is_alive()
    assert worker.trusted_runtime_profile is None
    assert runtime.stop_reason == "operator_stop"
    assert runtime.closed is True
    assert cleared
