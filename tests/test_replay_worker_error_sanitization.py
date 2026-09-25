from __future__ import annotations

from typing import NoReturn

from autosport.replay_worker import OneShotReplayWorker, _terminal_error
from autosport.secret_redaction import REDACTED


_SECRET = "sk_live_do_not_leak_953"
_TYPE_CANARY = "ProviderCredentialCanary_953"


class _SecretBearingReplayError(RuntimeError):
    def __str__(self) -> str:
        return "api_key=" + _SECRET


class _UnrenderableReplayError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError(_SECRET)


def test_terminal_error_uses_builtin_type_and_redacts_exception_message() -> None:
    rendered = _terminal_error(_SecretBearingReplayError())

    assert rendered == "RuntimeError: api_key=" + REDACTED
    assert _SECRET not in rendered


def test_terminal_error_never_trusts_broken_exception_str() -> None:
    assert (
        _terminal_error(_UnrenderableReplayError())
        == "RuntimeError: exception details unavailable"
    )


def test_terminal_error_rejects_custom_identifier_type_name() -> None:
    unsafe_error_type = type(_TYPE_CANARY, (RuntimeError,), {})

    rendered = _terminal_error(unsafe_error_type())

    assert rendered == "RuntimeError"
    assert _TYPE_CANARY not in rendered


def test_replay_worker_error_redacts_secret_and_custom_type_name() -> None:
    worker = OneShotReplayWorker()

    def fail_replay() -> NoReturn:
        raise _SecretBearingReplayError()

    worker._run(fail_replay)  # noqa: SLF001 - deterministic worker-boundary regression
    message = worker.poll()

    assert message is not None
    assert message.result is None
    assert message.error == "RuntimeError: api_key=" + REDACTED
    assert _SECRET not in message.error
    assert "_SecretBearingReplayError" not in message.error
