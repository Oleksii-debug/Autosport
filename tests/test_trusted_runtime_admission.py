from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import autosport.trusted_runtime_code_profile as profile_module
from autosport.continuous_session import SessionState
from autosport.product_runtime import AutonomousProductRuntime
from autosport.trusted_runtime_admission import trusted_runtime_code_profile_admission
from autosport.trusted_runtime_code_profile import (
    TrustedRuntimeCodeProfileError,
    _clear_started_product_runtime_origin,
    _register_started_product_runtime_origin,
    issue_trusted_runtime_code_profile,
    revoke_trusted_runtime_code_profile,
)


_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


class _RuntimeLeaseStub:
    authority_active = True


class _StartTransitionStoreStub:
    @staticmethod
    def pending() -> None:
        return None


class _RunningCoordinator:
    @staticmethod
    def status() -> SimpleNamespace:
        return SimpleNamespace(state=SessionState.RUNNING)


class _RunningCollector:
    @staticmethod
    def status() -> dict[str, object | None]:
        return {"stopped_at": None, "stop_reason": None}


def _canonical_runtime(workspace: Path) -> AutonomousProductRuntime:
    runtime = object.__new__(AutonomousProductRuntime)
    runtime.workspace = workspace
    runtime.manifest = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)
    runtime.coordinator = _RunningCoordinator()
    runtime.collector = _RunningCollector()
    runtime._runtime_lease = _RuntimeLeaseStub()
    runtime._start_transition_store = _StartTransitionStoreStub()
    runtime._closed = False
    runtime._operation_fence = threading.RLock()
    return runtime


def _issued_profile(tmp_path: Path):
    runtime = _canonical_runtime(tmp_path)
    _register_started_product_runtime_origin(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )
    profile = issue_trusted_runtime_code_profile(runtime)
    return runtime, profile


def test_admission_serializes_product_stop_revocation(tmp_path: Path) -> None:
    runtime, profile = _issued_profile(tmp_path)
    revoke_entered = threading.Event()
    revoke_finished = threading.Event()
    revoke_result: list[bool] = []

    def revoke() -> None:
        revoke_entered.set()
        revoke_result.append(revoke_trusted_runtime_code_profile(profile))
        revoke_finished.set()

    worker = threading.Thread(target=revoke, daemon=False)
    with trusted_runtime_code_profile_admission(profile, workspace=tmp_path) as admitted:
        assert admitted is profile
        worker.start()
        assert revoke_entered.wait(2.0)
        # The revoker has reached the authority call but cannot cross its canonical
        # RLock while this admission owns the protected operation.
        assert revoke_finished.is_set() is False

    assert revoke_finished.wait(2.0)
    worker.join()
    assert revoke_result == [True]
    _clear_started_product_runtime_origin(runtime)


def test_admission_rejects_forged_or_revoked_profile(tmp_path: Path) -> None:
    runtime, profile = _issued_profile(tmp_path)
    forged = replace(profile)

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="not current and authoritative",
    ):
        with trusted_runtime_code_profile_admission(forged, workspace=tmp_path):
            raise AssertionError("forged profile must never enter admission")

    assert revoke_trusted_runtime_code_profile(profile) is True
    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="not current and authoritative",
    ):
        with trusted_runtime_code_profile_admission(profile, workspace=tmp_path):
            raise AssertionError("revoked profile must never enter admission")

    _clear_started_product_runtime_origin(runtime)
