from __future__ import annotations

import json

import pytest

import autosport.ingestion_health as health_module
import autosport.secret_redaction as secret_redaction
from autosport.ingestion_health import SourceHealthStore


_NOW = "2026-10-04T12:45:00+00:00"
_FALLBACK = "BaseException: exception details unavailable"


class _HostileError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("password=must-never-render")


def _store(tmp_path, monkeypatch: pytest.MonkeyPatch, name: str) -> SourceHealthStore:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    return SourceHealthStore(tmp_path / name / "source_health.json")


def test_source_health_failure_persists_redacted_diagnostic_and_truth(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "redacted")
    environment_secret = "synthetic-environment-secret-7f3d"
    monkeypatch.setenv("AUTOSPORT_PARLAYAPI_KEY", environment_secret)
    error = RuntimeError(
        "password=synthetic-password-91 "
        "token=synthetic-token-22 "
        "url=https://synthetic-user:synthetic-pass@example.test/feed"
        "?api_key=synthetic-query-secret-31 "
        f"environment={environment_secret}"
    )

    state = store.record_failure("fixture-source", now=_NOW, error=error)

    assert state.status == "failed"
    assert state.poll_count == 1
    assert state.total_failures == 1
    assert state.consecutive_failures == 1
    assert state.last_error_at == _NOW
    assert state.last_error is not None

    raw = store.path.read_text(encoding="utf-8")
    for secret in (
        "synthetic-password-91",
        "synthetic-token-22",
        "synthetic-user:synthetic-pass",
        "synthetic-query-secret-31",
        environment_secret,
    ):
        assert secret not in raw
    assert "[REDACTED]" in raw

    reopened = SourceHealthStore(store.path).get("fixture-source")
    assert reopened.status == "failed"
    assert reopened.poll_count == 1
    assert reopened.total_failures == 1
    assert reopened.last_error_at == _NOW
    assert reopened.last_error == state.last_error

    persisted = json.loads(raw)["sources"]["fixture-source"]
    assert persisted["last_error"] == state.last_error


def test_source_health_failure_survives_hostile_exception_stringification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "hostile-str")

    state = store.record_failure(
        "fixture-source",
        now=_NOW,
        error=_HostileError(),
    )

    assert state.status == "failed"
    assert state.poll_count == 1
    assert state.total_failures == 1
    assert state.last_error == "Exception: exception details unavailable"


def test_module_global_redactor_rebinding_fails_closed_without_dispatch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "module-rebind")
    secret = "AS-DURABLE-MODULE-REBIND-SENTINEL-82ca"
    calls: list[BaseException] = []

    def forged(exc: BaseException) -> str:
        calls.append(exc)
        return str(exc)

    monkeypatch.setattr(health_module, "safe_exception_text", forged)
    state = store.record_failure(
        "fixture-source",
        now=_NOW,
        error=RuntimeError(f"Authorization: Bearer {secret}"),
    )

    assert state.last_error == _FALLBACK
    assert calls == []
    assert secret not in store.path.read_text(encoding="utf-8")


def test_canonical_redactor_code_swap_fails_closed_before_dispatch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "code-swap")
    secret = "AS-DURABLE-CODE-SWAP-SENTINEL-41bc"
    canonical = health_module.safe_exception_text
    original_code = canonical.__code__

    def forged(exc: BaseException, **_kwargs) -> str:
        return str(exc)

    try:
        canonical.__code__ = forged.__code__
        state = store.record_failure(
            "fixture-source",
            now=_NOW,
            error=RuntimeError(f"Authorization: Bearer {secret}"),
        )
    finally:
        canonical.__code__ = original_code

    assert state.last_error == _FALLBACK
    assert secret not in store.path.read_text(encoding="utf-8")


def test_transitive_redactor_code_swap_fails_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "transitive-code-swap")
    secret = "AS-DURABLE-TRANSITIVE-SENTINEL-793e"
    canonical = secret_redaction.safe_exception_detail
    original_code = canonical.__code__

    def forged_detail(exc: BaseException, **_kwargs) -> str:
        return str(exc)

    try:
        canonical.__code__ = forged_detail.__code__
        state = store.record_failure(
            "fixture-source",
            now=_NOW,
            error=RuntimeError(f"Authorization: Bearer {secret}"),
        )
    finally:
        canonical.__code__ = original_code

    assert state.last_error == _FALLBACK
    assert secret not in store.path.read_text(encoding="utf-8")


def test_secret_module_redactor_rebinding_fails_closed_without_dispatch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "secret-module-rebind")
    secret = "AS-DURABLE-SECRET-MODULE-SENTINEL-c91d"
    calls: list[BaseException] = []

    def forged(exc: BaseException) -> str:
        calls.append(exc)
        return str(exc)

    monkeypatch.setattr(secret_redaction, "safe_exception_text", forged)
    state = store.record_failure(
        "fixture-source",
        now=_NOW,
        error=RuntimeError(f"Authorization: Bearer {secret}"),
    )

    assert state.last_error == _FALLBACK
    assert calls == []
    assert secret not in store.path.read_text(encoding="utf-8")


def test_rebinding_public_renderer_holder_cannot_redirect_method_closure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "renderer-holder-rebind")
    secret = "AS-DURABLE-HOLDER-SENTINEL-f155"
    calls: list[BaseException] = []

    def forged(exc: BaseException) -> str:
        calls.append(exc)
        return str(exc)

    monkeypatch.setattr(health_module, "_DURABLE_FAILURE_RENDERER", forged)
    state = store.record_failure(
        "fixture-source",
        now=_NOW,
        error=RuntimeError(f"Authorization: Bearer {secret}"),
    )

    assert calls == []
    assert state.last_error is not None
    assert secret not in state.last_error
    assert secret not in store.path.read_text(encoding="utf-8")


def test_rebinding_public_fallback_holder_cannot_change_fail_closed_literal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "fallback-holder-rebind")
    secret = "AS-DURABLE-FALLBACK-HOLDER-SENTINEL-b80a"

    monkeypatch.setattr(health_module, "_DURABLE_FAILURE_FALLBACK", secret)
    monkeypatch.setattr(
        health_module,
        "safe_exception_text",
        lambda exc: str(exc),
    )
    state = store.record_failure(
        "fixture-source",
        now=_NOW,
        error=RuntimeError(f"Authorization: Bearer {secret}"),
    )

    assert state.last_error == _FALLBACK
    assert secret not in store.path.read_text(encoding="utf-8")


def test_ordinary_failure_detail_remains_diagnostic(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "ordinary")

    state = store.record_failure(
        "fixture-source",
        now=_NOW,
        error=RuntimeError("temporary upstream outage"),
    )

    assert state.last_error == "RuntimeError: temporary upstream outage"
