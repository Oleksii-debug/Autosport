from __future__ import annotations

from autosport.dataset_worker import OneShotDatasetValidationWorker, _safe_worker_error


_SECRET = "sk-live-do-not-leak"


class _SecretBearingError(RuntimeError):
    def __str__(self) -> str:
        return f"provider dataset failed api_key={_SECRET}"


class _UnrenderableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError(_SECRET)


def test_safe_worker_error_reuses_shared_secret_redaction() -> None:
    rendered = _safe_worker_error(_SecretBearingError())

    assert rendered.startswith("RuntimeError:")
    assert "provider dataset failed" in rendered
    assert "[REDACTED]" in rendered
    assert _SECRET not in rendered


def test_safe_worker_error_handles_hostile_stringification_without_leak() -> None:
    rendered = _safe_worker_error(_UnrenderableError())

    assert rendered == "RuntimeError: dataset validation failed; exception details unavailable"
    assert _SECRET not in rendered


def test_worker_terminal_message_never_publishes_raw_secret() -> None:
    worker = OneShotDatasetValidationWorker()

    def fail_validation() -> None:
        raise _SecretBearingError()

    worker._run(fail_validation)  # noqa: SLF001 - deterministic worker-boundary regression
    message = worker.poll()

    assert message is not None
    assert message.result is None
    assert message.error is not None
    assert message.error.startswith("RuntimeError:")
    assert "[REDACTED]" in message.error
    assert _SECRET not in message.error
