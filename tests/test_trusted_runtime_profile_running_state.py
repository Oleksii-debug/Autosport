from __future__ import annotations

from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import autosport.trusted_runtime_code_profile as profile_module
from autosport.continuous_session import SessionState
from autosport.product_runtime import AutonomousProductRuntime


_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


class _RuntimeLeaseStub:
    authority_active = True


class _StartTransitionStoreStub:
    @staticmethod
    def pending() -> None:
        return None


class _Lifecycle:
    def __init__(self) -> None:
        self.state = SessionState.RUNNING


class _StatefulCoordinator:
    def __init__(self, lifecycle: _Lifecycle) -> None:
        self._lifecycle = lifecycle

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(state=self._lifecycle.state)


class _StatefulCollector:
    def __init__(self, lifecycle: _Lifecycle) -> None:
        self._lifecycle = lifecycle

    def status(self) -> dict[str, object | None]:
        stopped = self._lifecycle.state is SessionState.STOPPED
        return {
            "stopped_at": "2026-09-28T00:00:00+00:00" if stopped else None,
            "stop_reason": "test-stop" if stopped else None,
        }


def _stateful_runtime(
    workspace: Path,
) -> tuple[AutonomousProductRuntime, _Lifecycle]:
    lifecycle = _Lifecycle()
    runtime = object.__new__(AutonomousProductRuntime)
    runtime.workspace = workspace
    runtime.manifest = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)
    runtime.coordinator = _StatefulCoordinator(lifecycle)
    runtime.collector = _StatefulCollector(lifecycle)
    runtime._runtime_lease = _RuntimeLeaseStub()
    runtime._start_transition_store = _StartTransitionStoreStub()
    runtime._closed = False
    runtime._operation_fence = RLock()
    return runtime, lifecycle


def test_stopped_runtime_invalidates_profile_before_origin_cleanup(
    tmp_path: Path,
) -> None:
    runtime, lifecycle = _stateful_runtime(tmp_path)

    profile_module._register_started_product_runtime_origin(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )
    profile = profile_module.issue_trusted_runtime_code_profile(runtime)
    try:
        assert profile_module.is_authoritative_trusted_runtime_code_profile(
            profile,
            workspace=tmp_path,
        )

        # Reproduce the worker's normal terminal ordering: runtime.stop() makes the
        # durable session STOPPED before finally revokes the profile/origin record.
        lifecycle.state = SessionState.STOPPED

        assert not profile_module.is_authoritative_trusted_runtime_code_profile(
            profile,
            workspace=tmp_path,
        )
    finally:
        profile_module.revoke_trusted_runtime_code_profile(profile)
        profile_module._clear_started_product_runtime_origin(runtime)
