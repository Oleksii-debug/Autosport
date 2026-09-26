from __future__ import annotations

from types import SimpleNamespace

import pytest

import autosport.product_entrypoint as entrypoint


_SECRET = "provider-secret-7f3a"
_TYPE_CANARY = "ProviderCredentialCanary_1943"


class _BaseExceptionRuntime:
    def __init__(self, *, tick_exit: bool = False, close_exit: bool = False) -> None:
        self.tick_exit = tick_exit
        self.close_exit = close_exit
        self.manifest = SimpleNamespace(source_id="source-a")
        self.workspace = "workspace-a"
        self.stop_calls: list[str] = []

    def start(self) -> object:
        return object()

    def tick(self) -> object:
        if self.tick_exit:
            raise SystemExit(_SECRET)
        return object()

    def stop(self, reason: str) -> object:
        self.stop_calls.append(reason)
        return object()

    def close(self) -> None:
        if self.close_exit:
            raise SystemExit(_SECRET)


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime: _BaseExceptionRuntime,
) -> None:
    monkeypatch.setattr(
        entrypoint,
        "_validated_source",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        entrypoint,
        "build_autonomous_product_runtime",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(entrypoint, "_print_record", lambda *_args, **_kwargs: None)


def test_command_sanitizes_pre_start_system_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_source(*_args: object, **_kwargs: object) -> object:
        raise SystemExit(_SECRET)

    monkeypatch.setattr(entrypoint, "_validated_source", fail_source)

    exit_code = entrypoint.run_product_command(
        workspace=entrypoint.Path("unused"),
        source_factory="unused:factory",
        initial_bankroll="10000",
        max_cycles=1,
        poll_seconds=0,
    )

    assert exit_code == 3
    output = capsys.readouterr().out
    assert _SECRET not in output
    assert '"error_type":"SystemExit"' in output
    assert '"error_code":"product_start_failed"' in output


def test_runtime_system_exit_is_wrapped_without_secret_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _BaseExceptionRuntime(tick_exit=True)
    _install_runtime(monkeypatch, runtime)

    exit_code = entrypoint.run_product_command(
        workspace=entrypoint.Path("unused"),
        source_factory="unused:factory",
        initial_bankroll="10000",
        max_cycles=2,
        poll_seconds=0,
    )

    assert exit_code == 4
    assert runtime.stop_calls == ["runtime_error"]
    output = capsys.readouterr().out
    assert _SECRET not in output
    assert '"error_type":"SystemExit"' in output
    assert '"error_code":"product_runtime_failed"' in output


def test_started_cleanup_system_exit_is_wrapped_without_secret_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _BaseExceptionRuntime(close_exit=True)
    _install_runtime(monkeypatch, runtime)

    exit_code = entrypoint.run_product_command(
        workspace=entrypoint.Path("unused"),
        source_factory="unused:factory",
        initial_bankroll="10000",
        max_cycles=1,
        poll_seconds=0,
    )

    assert runtime.stop_calls == ["max_cycles_reached"]
    assert exit_code == 4
    output = capsys.readouterr().out
    assert _SECRET not in output
    assert '"error_type":"SystemExit"' in output
    assert '"error_code":"product_runtime_failed"' in output


def test_pre_start_custom_exception_name_cannot_mint_public_error_type(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsafe_type = type(_TYPE_CANARY, (RuntimeError,), {})

    def fail_source(*_args: object, **_kwargs: object) -> object:
        raise unsafe_type("api_key=" + _SECRET)

    monkeypatch.setattr(entrypoint, "_validated_source", fail_source)

    exit_code = entrypoint.run_product_command(
        workspace=entrypoint.Path("unused"),
        source_factory="unused:factory",
        initial_bankroll="10000",
        max_cycles=1,
        poll_seconds=0,
    )

    assert exit_code == 3
    output = capsys.readouterr().out
    assert _TYPE_CANARY not in output
    assert _SECRET not in output
    assert '"error_type":"RuntimeError"' in output


def test_post_start_custom_exception_name_cannot_mint_public_error_type(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsafe_type = type(_TYPE_CANARY, (RuntimeError,), {})
    runtime = _BaseExceptionRuntime()
    _install_runtime(monkeypatch, runtime)

    def fail_tick() -> object:
        raise unsafe_type("api_key=" + _SECRET)

    monkeypatch.setattr(runtime, "tick", fail_tick)

    exit_code = entrypoint.run_product_command(
        workspace=entrypoint.Path("unused"),
        source_factory="unused:factory",
        initial_bankroll="10000",
        max_cycles=1,
        poll_seconds=0,
    )

    assert exit_code == 4
    output = capsys.readouterr().out
    assert _TYPE_CANARY not in output
    assert _SECRET not in output
    assert '"error_type":"RuntimeError"' in output


def test_product_runtime_error_rejects_caller_supplied_type_label() -> None:
    with pytest.raises(TypeError):
        entrypoint.ProductRuntimeError(_TYPE_CANARY)  # type: ignore[arg-type]
