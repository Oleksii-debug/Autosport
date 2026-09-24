from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import autosport.product_gui_worker as worker_module
import autosport.product_source as product_source_module
import autosport.trusted_runtime_code_profile as profile_module
from autosport.product_entrypoint import ProductEntrypointError
from autosport.product_gui_worker import ProductGuiWorker
from autosport.trusted_runtime_code_profile import (
    TrustedRuntimeCodeProfileError,
    is_authoritative_trusted_runtime_code_profile,
    issue_trusted_runtime_code_profile,
    require_authoritative_trusted_runtime_code_profile,
    revoke_trusted_runtime_code_profile,
)


_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


class _ProfileRuntime:
    def __init__(self, workspace: Path, source_id: str = _PROVIDER_SOURCE_ID) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(source_id=source_id)


class _BlockingRuntime:
    def __init__(self) -> None:
        self.tick_entered = threading.Event()
        self.release_tick = threading.Event()
        self.stop_reason: str | None = None
        self.closed = False
        self._status = object()

    def start(self) -> object:
        return self._status

    def tick(self) -> object:
        self.tick_entered.set()
        assert self.release_tick.wait(2.0)
        return object()

    def stop(self, reason: str) -> object:
        self.stop_reason = reason
        return self._status

    def close(self) -> None:
        self.closed = True


class _BlockingStartRuntime(_BlockingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def start(self) -> object:
        self.start_entered.set()
        assert self.release_start.wait(2.0)
        return self._status


def _patch_profile_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        profile_module,
        "AutonomousProductRuntime",
        _ProfileRuntime,
    )


def test_profile_is_origin_bound_and_not_provider_write_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)

    profile = issue_trusted_runtime_code_profile(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )

    assert require_authoritative_trusted_runtime_code_profile(
        profile,
        workspace=tmp_path,
    ) is profile
    assert profile.operator_source_id == "parlayapi-table-tennis"
    assert profile.factory_spec == _FACTORY_SPEC
    assert profile.provider_source_id == _PROVIDER_SOURCE_ID
    assert profile.provider_write_authorized is False
    assert profile.real_money_execution_authorized is False

    forged = replace(profile)
    assert forged == profile
    assert forged is not profile
    assert is_authoritative_trusted_runtime_code_profile(forged) is False
    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="not current and authoritative",
    ):
        require_authoritative_trusted_runtime_code_profile(forged)

    assert revoke_trusted_runtime_code_profile(profile) is True
    assert is_authoritative_trusted_runtime_code_profile(profile) is False


def test_profile_requires_exact_closed_registry_binding_before_issuance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="one exact product-shipped source binding",
    ):
        issue_trusted_runtime_code_profile(
            runtime,
            source_factory="external.module:factory",
            expected_provider_source_id=_PROVIDER_SOURCE_ID,
        )

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="runtime manifest source_id does not match",
    ):
        issue_trusted_runtime_code_profile(
            _ProfileRuntime(tmp_path, source_id="different:provider"),
            source_factory=_FACTORY_SPEC,
            expected_provider_source_id=_PROVIDER_SOURCE_ID,
        )





def test_registered_symbol_drift_fails_before_profile_or_runtime_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)

    monkeypatch.setattr(
        product_source_module,
        "create_parlay_product_source",
        lambda: SimpleNamespace(source_id=_PROVIDER_SOURCE_ID),
    )

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="callable identity has drifted",
    ):
        issue_trusted_runtime_code_profile(
            runtime,
            source_factory=_FACTORY_SPEC,
            expected_provider_source_id=_PROVIDER_SOURCE_ID,
        )

    source_constructed = False

    def forbidden_source(*_args, **_kwargs):
        nonlocal source_constructed
        source_constructed = True
        raise AssertionError("drifted registered factory must fail before source build")

    monkeypatch.setattr(worker_module, "_validated_source", forbidden_source)
    with pytest.raises(
        ProductEntrypointError,
        match="not product-owned by this build",
    ):
        worker_module._runtime_builder(
            tmp_path,
            _FACTORY_SPEC,
            "10000",
            expected_source_id=_PROVIDER_SOURCE_ID,
        )
    assert source_constructed is False


def test_profile_serializes_one_active_runtime_per_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    first = _ProfileRuntime(tmp_path)
    second = _ProfileRuntime(tmp_path)

    first_profile = issue_trusted_runtime_code_profile(
        first,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )
    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="workspace already has an active",
    ):
        issue_trusted_runtime_code_profile(
            second,
            source_factory=_FACTORY_SPEC,
            expected_provider_source_id=_PROVIDER_SOURCE_ID,
        )

    assert revoke_trusted_runtime_code_profile(first_profile) is True
    second_profile = issue_trusted_runtime_code_profile(
        second,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )
    assert is_authoritative_trusted_runtime_code_profile(second_profile)
    assert revoke_trusted_runtime_code_profile(second_profile) is True


def test_runtime_or_workspace_drift_revokes_positive_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)
    profile = issue_trusted_runtime_code_profile(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )

    runtime.manifest.source_id = "different:provider"
    assert is_authoritative_trusted_runtime_code_profile(profile) is False
    assert is_authoritative_trusted_runtime_code_profile(
        profile,
        workspace=tmp_path / "other",
    ) is False
    assert revoke_trusted_runtime_code_profile(profile) is True


def test_canonical_worker_issues_after_start_and_revokes_on_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _BlockingRuntime()
    issued: list[tuple[object, str, str]] = []
    revoked: list[object] = []
    profile = object()

    def build(
        _workspace: Path,
        _source_factory: str,
        _bankroll: str,
        *,
        expected_source_id: str | None = None,
    ) -> _BlockingRuntime:
        assert expected_source_id == _PROVIDER_SOURCE_ID
        return runtime

    def issue(
        value: object,
        *,
        source_factory: str,
        expected_provider_source_id: str,
    ) -> object:
        issued.append((value, source_factory, expected_provider_source_id))
        return profile

    monkeypatch.setattr(worker_module, "_CANONICAL_RUNTIME_BUILDER", build)
    monkeypatch.setattr(worker_module, "issue_trusted_runtime_code_profile", issue)
    monkeypatch.setattr(
        worker_module,
        "revoke_trusted_runtime_code_profile",
        lambda value: revoked.append(value) or True,
    )

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory=_FACTORY_SPEC,
        expected_source_id=_PROVIDER_SOURCE_ID,
        poll_seconds=60.0,
    )
    assert runtime.tick_entered.wait(2.0)

    assert issued == [(runtime, _FACTORY_SPEC, _PROVIDER_SOURCE_ID)]
    assert worker.trusted_runtime_profile is profile
    started = worker.poll()
    assert started is not None
    assert started.kind == "STARTED"

    assert worker.request_stop("operator_stop")
    runtime.release_tick.set()
    assert worker.join(2.0)

    assert runtime.stop_reason == "operator_stop"
    assert runtime.closed is True
    assert revoked == [profile]
    assert worker.trusted_runtime_profile is None


def test_stop_during_start_cannot_publish_trusted_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _BlockingStartRuntime()
    issued = False

    def build(
        _workspace: Path,
        _source_factory: str,
        _bankroll: str,
        *,
        expected_source_id: str | None = None,
    ) -> _BlockingStartRuntime:
        assert expected_source_id == _PROVIDER_SOURCE_ID
        return runtime

    def forbidden_issue(*_args, **_kwargs):
        nonlocal issued
        issued = True
        raise AssertionError("STOP won before trusted profile issuance")

    monkeypatch.setattr(worker_module, "_CANONICAL_RUNTIME_BUILDER", build)
    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        forbidden_issue,
    )

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory=_FACTORY_SPEC,
        expected_source_id=_PROVIDER_SOURCE_ID,
        poll_seconds=60.0,
    )
    assert runtime.start_entered.wait(2.0)

    assert worker.request_stop("operator_stop")
    runtime.release_start.set()
    assert worker.join(2.0)

    assert issued is False
    assert worker.trusted_runtime_profile is None
    assert runtime.stop_reason == "operator_stop"
    assert runtime.closed is True


def test_arbitrary_headless_builder_never_mints_trusted_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _BlockingRuntime()

    def forbidden_issue(*_args, **_kwargs):
        raise AssertionError("arbitrary source-factory mode must remain unprofiled")

    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        forbidden_issue,
    )
    worker = ProductGuiWorker(runtime_builder=lambda *_args: runtime)

    assert worker.start(
        workspace=tmp_path,
        source_factory="external.module:factory",
        poll_seconds=60.0,
    )
    assert runtime.tick_entered.wait(2.0)
    assert worker.trusted_runtime_profile is None

    assert worker.request_stop("operator_stop")
    runtime.release_tick.set()
    assert worker.join(2.0)
    assert worker.trusted_runtime_profile is None


def test_failed_runtime_start_cannot_publish_trusted_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FailingStartRuntime(_BlockingRuntime):
        def start(self) -> object:
            raise RuntimeError("synthetic start failure")

    runtime = _FailingStartRuntime()
    issued = False

    def build(
        _workspace: Path,
        _source_factory: str,
        _bankroll: str,
        *,
        expected_source_id: str | None = None,
    ) -> _FailingStartRuntime:
        assert expected_source_id == _PROVIDER_SOURCE_ID
        return runtime

    def forbidden_issue(*_args, **_kwargs):
        nonlocal issued
        issued = True
        raise AssertionError("profile issuance must follow successful runtime.start")

    monkeypatch.setattr(worker_module, "_CANONICAL_RUNTIME_BUILDER", build)
    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        forbidden_issue,
    )

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory=_FACTORY_SPEC,
        expected_source_id=_PROVIDER_SOURCE_ID,
        poll_seconds=1.0,
    )
    assert worker.join(2.0)

    assert issued is False
    assert worker.trusted_runtime_profile is None
    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "ERROR"
