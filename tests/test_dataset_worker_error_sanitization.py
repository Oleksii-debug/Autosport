from __future__ import annotations

from autosport.dataset_worker import OneShotDatasetValidationWorker, _safe_worker_error


_SECRET = r"C:\Users\Operator\.autosport\provider.txt?token=sk-live-do-not-leak"


class _SecretBearingError(RuntimeError):
    def __str__(self) -> str:
        return _SECRET


class _UnrenderableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError(_SECRET)


def test_safe_worker_error_keeps_type_but_drops_exception_message() -> None:
    rendered = _safe_worker_error(_SecretBearingError())

    assert rendered == "_SecretBearingError"
    assert _SECRET not in rendered
    assert "sk-live-do-not-leak" not in rendered
    assert "C:\\Users\\Operator" not in rendered


def test_safe_worker_error_never_calls_exception_str() -> None:
    assert _safe_worker_error(_UnrenderableError()) == "_UnrenderableError"


def test_safe_worker_error_rejects_unbounded_type_name() -> None:
    unsafe_error_type = type(f"Leaked-{_SECRET}", (RuntimeError,), {})

    assert _safe_worker_error(unsafe_error_type()) == "BaseException"


def test_worker_error_message_does_not_propagate_raw_exception_payload() -> None:
    worker = OneShotDatasetValidationWorker()

    def fail_validation() -> None:
        raise _SecretBearingError()

    worker._run(fail_validation)  # noqa: SLF001 - deterministic worker-boundary regression
    message = worker.poll()

    assert message is not None
    assert message.result is None
    assert message.error == "_SecretBearingError"
    assert _SECRET not in message.error
