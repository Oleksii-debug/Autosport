from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import inspect
import threading

import pytest

import autosport.product_entrypoint as product_entrypoint_module
import autosport.product_gui_worker as worker_module
import autosport.product_source as product_source_module
import autosport.trusted_runtime_code_profile as profile_module
from autosport.product_entrypoint import ProductEntrypointError
from autosport.product_gui_worker import ProductGuiWorker
from autosport.trusted_runtime_code_profile import (
    TrustedRuntimeCodeProfileError,
    _clear_started_product_runtime_origin,
    _register_started_product_runtime_origin,
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


def _register_profile_runtime(runtime: _ProfileRuntime) -> None:
    _register_started_product_runtime_origin(
        runtime,
        source_factory=_FACTORY_SPEC,
        expected_provider_source_id=_PROVIDER_SOURCE_ID,
    )


def test_profile_requires_canonical_started_origin_and_is_not_write_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="lacks canonical started product-code origin",
    ):
        issue_trusted_runtime_code_profile(runtime)

    _register_profile_runtime(runtime)
    profile = issue_trusted_runtime_code_profile(runtime)

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

    assert revoke_trusted_runtime_code_profile(profile) is True
    assert is_authoritative_trusted_runtime_code_profile(profile) is False
    _clear_started_product_runtime_origin(runtime)


def test_started_origin_requires_exact_closed_registry_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="one exact product-shipped source binding",
    ):
        _register_started_product_runtime_origin(
            runtime,
            source_factory="external.module:factory",
            expected_provider_source_id=_PROVIDER_SOURCE_ID,
        )

    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="runtime manifest source_id does not match",
    ):
        _register_started_product_runtime_origin(
            _ProfileRuntime(tmp_path, source_id="different:provider"),
            source_factory=_FACTORY_SPEC,
            expected_provider_source_id=_PROVIDER_SOURCE_ID,
        )


def test_registered_symbol_drift_fails_before_started_origin(
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
        _register_profile_runtime(runtime)


def test_runtime_builder_rechecks_factory_identity_after_source_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)
    built = False

    def changing_source(_factory: str, *, workspace: Path) -> object:
        assert workspace == tmp_path
        monkeypatch.setattr(
            product_source_module,
            "create_parlay_product_source",
            lambda: source,
        )
        return source

    def forbidden_build(**_kwargs) -> object:
        nonlocal built
        built = True
        raise AssertionError("runtime build must not follow factory-symbol drift")

    monkeypatch.setattr(worker_module, "_validated_source", changing_source)
    monkeypatch.setattr(
        worker_module,
        "build_autonomous_product_runtime",
        forbidden_build,
    )

    with pytest.raises(
        ProductEntrypointError,
        match="changed during source construction",
    ):
        worker_module._runtime_builder(
            tmp_path,
            _FACTORY_SPEC,
            "10000",
            expected_source_id=_PROVIDER_SOURCE_ID,
        )

    assert built is False


def test_profile_serializes_one_active_runtime_per_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    first = _ProfileRuntime(tmp_path)
    second = _ProfileRuntime(tmp_path)
    _register_profile_runtime(first)
    _register_profile_runtime(second)

    first_profile = issue_trusted_runtime_code_profile(first)
    with pytest.raises(
        TrustedRuntimeCodeProfileError,
        match="workspace already has an active",
    ):
        issue_trusted_runtime_code_profile(second)

    assert revoke_trusted_runtime_code_profile(first_profile) is True
    second_profile = issue_trusted_runtime_code_profile(second)
    assert is_authoritative_trusted_runtime_code_profile(second_profile)
    assert revoke_trusted_runtime_code_profile(second_profile) is True
    _clear_started_product_runtime_origin(first)
    _clear_started_product_runtime_origin(second)


def test_runtime_drift_revokes_positive_profile_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_profile_runtime(monkeypatch)
    runtime = _ProfileRuntime(tmp_path)
    _register_profile_runtime(runtime)
    profile = issue_trusted_runtime_code_profile(runtime)

    runtime.manifest.source_id = "different:provider"

    assert is_authoritative_trusted_runtime_code_profile(profile) is False
    assert is_authoritative_trusted_runtime_code_profile(
        profile,
        workspace=tmp_path / "other",
    ) is False
    assert revoke_trusted_runtime_code_profile(profile) is True
    _clear_started_product_runtime_origin(runtime)


def test_injected_builder_cannot_become_profiled_after_alias_rebind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _BlockingRuntime()
    builder_called = False
    registered = False
    issued = False

    def attacker_builder(
        _workspace: Path,
        _source_factory: str,
        _bankroll: str,
        *,
        expected_source_id: str | None = None,
    ) -> _BlockingRuntime:
        nonlocal builder_called
        builder_called = True
        assert expected_source_id == _PROVIDER_SOURCE_ID
        return runtime

    def forbidden_register(*_args, **_kwargs) -> None:
        nonlocal registered
        registered = True
        raise AssertionError("injected builder must never mint trusted origin")

    def forbidden_issue(*_args, **_kwargs) -> object:
        nonlocal issued
        issued = True
        raise AssertionError("injected builder must never mint trusted profile")

    # This reproduces the former self-authorizing alias attack: the attacker
    # controls both the mutable module alias and the constructor argument.
    monkeypatch.setattr(
        worker_module,
        "_CANONICAL_RUNTIME_BUILDER",
        attacker_builder,
    )
    monkeypatch.setattr(
        worker_module,
        "_PROFILED_RUNTIME_BUILDER",
        attacker_builder,
    )
    monkeypatch.setattr(
        worker_module,
        "_register_started_product_runtime_origin",
        forbidden_register,
    )
    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        forbidden_issue,
    )

    worker = ProductGuiWorker(runtime_builder=attacker_builder)
    assert worker.start(
        workspace=tmp_path,
        source_factory=_FACTORY_SPEC,
        expected_source_id=_PROVIDER_SOURCE_ID,
        poll_seconds=1.0,
    )
    assert worker.join(2.0)

    assert builder_called is False
    assert registered is False
    assert issued is False
    assert worker.trusted_runtime_profile is None
    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "ERROR"
    assert terminal.error_type == "ProductEntrypointError"


def test_profiled_worker_root_captures_transitive_dependencies_once(
    monkeypatch,
) -> None:
    kwdefaults = ProductGuiWorker._run.__kwdefaults__
    assert kwdefaults is not None
    captured = kwdefaults["_profiled_runtime_builder"]
    assert captured is worker_module._PROFILED_RUNTIME_BUILDER

    closure = inspect.getclosurevars(captured).nonlocals
    captured_bindings = closure["captured_bindings"]
    runtime_factory = closure["runtime_factory"]
    runtime_type = closure["runtime_type"]
    canonical_workspace = closure["canonical_workspace"]
    path_type = inspect.getclosurevars(canonical_workspace).nonlocals["path_type"]

    def attacker(*_args, **_kwargs):
        raise AssertionError("mutable module binding must not redirect profiled root")

    monkeypatch.setattr(worker_module, "_validated_source", attacker)
    monkeypatch.setattr(worker_module, "build_autonomous_product_runtime", attacker)
    monkeypatch.setattr(
        product_entrypoint_module,
        "_load_source_factory",
        attacker,
    )
    monkeypatch.setattr(worker_module, "_PROFILED_RUNTIME_BUILDER", attacker)
    monkeypatch.setattr(worker_module, "_CANONICAL_RUNTIME_BUILDER", attacker)

    after_builder = ProductGuiWorker._run.__kwdefaults__["_profiled_runtime_builder"]
    after = inspect.getclosurevars(after_builder).nonlocals
    after_workspace = inspect.getclosurevars(after["canonical_workspace"]).nonlocals

    assert after_builder is captured
    assert after["captured_bindings"] is captured_bindings
    assert after["runtime_factory"] is runtime_factory
    assert after["runtime_type"] is runtime_type
    assert after_workspace["path_type"] is path_type
    assert after["runtime_factory"] is not attacker


def test_profiled_builder_uses_exact_captured_factory_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = SimpleNamespace(
        source_id=_PROVIDER_SOURCE_ID,
        stream_epoch="test-stream",
        workspace=tmp_path,
        fetch_catalog_page=lambda *_args, **_kwargs: None,
        fetch_deltas=lambda *_args, **_kwargs: (),
        resolve_event=lambda *_args, **_kwargs: None,
    )
    factory_calls = 0

    def canonical_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        return source

    class _Runtime:
        def __init__(self) -> None:
            self.workspace = tmp_path
            self.manifest = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)
            self.collector = SimpleNamespace(source=source)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = _Runtime()
    runtime_calls = 0

    def runtime_factory(**kwargs) -> _Runtime:
        nonlocal runtime_calls
        runtime_calls += 1
        assert kwargs["workspace"] == tmp_path
        assert kwargs["source"] is source
        assert kwargs["initial_bankroll"] == "10000"
        return runtime

    binding = worker_module._ProfiledSourceBinding(
        factory_spec=_FACTORY_SPEC,
        provider_source_id=_PROVIDER_SOURCE_ID,
        factory=canonical_factory,
    )
    builder = worker_module._capture_profiled_runtime_builder(
        source_bindings=(binding,),
        runtime_factory=runtime_factory,
        runtime_type=_Runtime,
        path_type=Path,
    )

    def dynamic_loader_must_not_run(*_args, **_kwargs):
        raise AssertionError("profiled path must not use the dynamic source loader")

    monkeypatch.setattr(
        product_entrypoint_module,
        "_load_source_factory",
        dynamic_loader_must_not_run,
    )
    monkeypatch.setattr(
        worker_module,
        "_validated_source",
        dynamic_loader_must_not_run,
    )

    assert (
        builder(
            tmp_path,
            _FACTORY_SPEC,
            "10000",
            expected_source_id=_PROVIDER_SOURCE_ID,
        )
        is runtime
    )
    assert factory_calls == 1
    assert runtime_calls == 1


def test_profiled_builder_rejects_runtime_that_drops_exact_source_identity(
    tmp_path: Path,
) -> None:
    source = SimpleNamespace(
        source_id=_PROVIDER_SOURCE_ID,
        stream_epoch="test-stream",
        workspace=tmp_path,
        fetch_catalog_page=lambda *_args, **_kwargs: None,
        fetch_deltas=lambda *_args, **_kwargs: (),
        resolve_event=lambda *_args, **_kwargs: None,
    )

    class _Runtime:
        def __init__(self) -> None:
            self.workspace = tmp_path
            self.manifest = SimpleNamespace(source_id=_PROVIDER_SOURCE_ID)
            self.collector = SimpleNamespace(source=object())
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = _Runtime()
    binding = worker_module._ProfiledSourceBinding(
        factory_spec=_FACTORY_SPEC,
        provider_source_id=_PROVIDER_SOURCE_ID,
        factory=lambda: source,
    )
    builder = worker_module._capture_profiled_runtime_builder(
        source_bindings=(binding,),
        runtime_factory=lambda **_kwargs: runtime,
        runtime_type=_Runtime,
        path_type=Path,
    )

    with pytest.raises(
        ProductEntrypointError,
        match="does not retain the exact closed-registry source",
    ):
        builder(
            tmp_path,
            _FACTORY_SPEC,
            "10000",
            expected_source_id=_PROVIDER_SOURCE_ID,
        )

    assert runtime.closed is True


def test_arbitrary_headless_builder_never_enters_trusted_profile_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _BlockingRuntime()

    def forbidden_profile_path(*_args, **_kwargs):
        raise AssertionError("arbitrary source-factory mode must remain unprofiled")

    monkeypatch.setattr(
        worker_module,
        "_register_started_product_runtime_origin",
        forbidden_profile_path,
    )
    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        forbidden_profile_path,
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



def test_injected_builder_without_expected_identity_remains_unprofiled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FailingStartRuntime(_BlockingRuntime):
        def start(self) -> object:
            raise RuntimeError("synthetic start failure")

    runtime = _FailingStartRuntime()
    registered = False
    issued = False

    def build(
        _workspace: Path,
        _source_factory: str,
        _bankroll: str,
    ) -> _FailingStartRuntime:
        return runtime

    def forbidden_register(*_args, **_kwargs) -> None:
        nonlocal registered
        registered = True
        raise AssertionError("unprofiled builder cannot register trusted origin")

    def forbidden_issue(*_args, **_kwargs) -> object:
        nonlocal issued
        issued = True
        raise AssertionError("unprofiled builder cannot issue trusted profile")

    monkeypatch.setattr(
        worker_module,
        "_register_started_product_runtime_origin",
        forbidden_register,
    )
    monkeypatch.setattr(
        worker_module,
        "issue_trusted_runtime_code_profile",
        forbidden_issue,
    )

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="external.module:factory",
        poll_seconds=1.0,
    )
    assert worker.join(2.0)

    assert registered is False
    assert issued is False
    assert worker.trusted_runtime_profile is None
    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "ERROR"

