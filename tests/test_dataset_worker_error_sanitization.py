from __future__ import annotations

from autosport.dataset_worker import OneShotDatasetValidationWorker, _safe_worker_error


_SECRET = "sk-live-do-not-leak"
_PRIVATE_PATH = r"C:\Users\owner\private\dataset-response.txt"


class _SecretBearingError(RuntimeError):
    stringify_calls = 0

    def __str__(self) -> str:
        type(self).stringify_calls += 1
        return f"provider dataset failed api_key={_SECRET} path={_PRIVATE_PATH}"


class _UnrenderableError(RuntimeError):
    stringify_calls = 0

    def __str__(self) -> str:
        type(self).stringify_calls += 1
        raise RuntimeError(_SECRET)


def test_safe_worker_error_never_renders_exception_detail() -> None:
    _SecretBearingError.stringify_calls = 0
    rendered = _safe_worker_error(_SecretBearingError())

    assert rendered == "RuntimeError: dataset validation failed"
    assert _SecretBearingError.stringify_calls == 0
    assert _SECRET not in rendered
    assert _PRIVATE_PATH not in rendered
    assert "provider dataset failed" not in rendered


def test_safe_worker_error_handles_hostile_stringification_without_calling_it() -> None:
    _UnrenderableError.stringify_calls = 0
    rendered = _safe_worker_error(_UnrenderableError())

    assert rendered == "RuntimeError: dataset validation failed"
    assert _UnrenderableError.stringify_calls == 0
    assert _SECRET not in rendered


def test_safe_worker_error_does_not_publish_arbitrary_runtime_error_body() -> None:
    response_body = "<html>private provider response body customer=owner</html>"
    rendered = _safe_worker_error(
        RuntimeError(f"{_PRIVATE_PATH}: {response_body}")
    )

    assert rendered == "RuntimeError: dataset validation failed"
    assert _PRIVATE_PATH not in rendered
    assert response_body not in rendered


def test_safe_worker_error_ignores_hostile_custom_exception_class_name() -> None:
    hostile_type = type(
        "api_key_sk_live_do_not_leak",
        (RuntimeError,),
        {},
    )
    rendered = _safe_worker_error(hostile_type("private body"))

    assert rendered == "RuntimeError: dataset validation failed"
    assert "api_key" not in rendered
    assert _SECRET not in rendered


def test_worker_terminal_message_never_publishes_raw_detail() -> None:
    _SecretBearingError.stringify_calls = 0
    worker = OneShotDatasetValidationWorker()

    def fail_validation() -> None:
        raise _SecretBearingError()

    worker._run(fail_validation)  # noqa: SLF001 - deterministic worker-boundary regression
    message = worker.poll()

    assert message is not None
    assert message.result is None
    assert message.error == "RuntimeError: dataset validation failed"
    assert _SecretBearingError.stringify_calls == 0
    assert _SECRET not in message.error
    assert _PRIVATE_PATH not in message.error
