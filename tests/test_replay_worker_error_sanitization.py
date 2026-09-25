from __future__ import annotations

from typing import NoReturn

from autosport.replay_worker import OneShotReplayWorker, _terminal_error


_SECRET = r"C:\Users\Operator\.autosport\paper-book.json?token=sk-live-do-not-leak"


class _SecretBearingReplayError(RuntimeError):
    def __str__(self) -> str:
        return _SECRET


class _UnrenderableReplayError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError(_SECRET)


def test_terminal_error_keeps_type_but_drops_exception_message() -> None:
    rendered = _terminal_error(_SecretBearingReplayError())

    assert rendered == "_SecretBearingReplayError"
    assert _SECRET not in rendered
    assert "sk-live-do-not-leak" not in rendered
    assert "C:\\Users\\Operator" not in rendered


def test_terminal_error_never_calls_exception_str() -> None:
    assert _terminal_error(_UnrenderableReplayError()) == "_UnrenderableReplayError"


def test_terminal_error_rejects_unbounded_type_name() -> None:
    unsafe_error_type = type(f"Leaked-{_SECRET}", (RuntimeError,), {})

    assert _terminal_error(unsafe_error_type()) == "BaseException"


def test_replay_worker_error_does_not_propagate_raw_exception_payload() -> None:
    worker = OneShotReplayWorker()

    def fail_replay() -> NoReturn:
        raise _SecretBearingReplayError()

    worker._run(fail_replay)  # noqa: SLF001 - deterministic worker-boundary regression
    message = worker.poll()

    assert message is not None
    assert message.result is None
    assert message.error == "_SecretBearingReplayError"
    assert _SECRET not in message.error
