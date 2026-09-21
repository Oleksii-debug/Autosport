"""Fail-closed optional model invocation boundary.

This module owns model availability only. It grants no provider, economic, risk,
execution, settlement, scientific-promotion, or UI authority.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Mapping, Protocol

_ZERO = Decimal("0")
_NS = Decimal("1000000000")
_ISSUE = object()


class ModelInvocationError(ValueError):
    pass


class ModelAdapterInvalidResponse(ValueError):
    """Adapter-normalized schema failure; raw provider details stay outside evidence."""


class ModelBackendMode(StrEnum):
    NO_LLM = "NO_LLM"
    LOCAL_OLLAMA = "LOCAL_OLLAMA"
    EXTERNAL_API = "EXTERNAL_API"


class ModelInvocationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CANCELLED = "CANCELLED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ModelInvocationError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8", errors="strict")
    return value


def _sha(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ModelInvocationError(f"{name} must be SHA-256 hex")
    return text


def _digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _mono() -> Decimal:
    return Decimal(time.monotonic_ns()) / _NS


def _mono_value(clock: Callable[[], Decimal]) -> Decimal:
    value = clock()
    if type(value) is not Decimal or not value.is_finite():
        raise ModelInvocationError("monotonic clock must return finite Decimal seconds")
    return value


def _wall(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ModelInvocationError("wall clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ModelInvocationRequest:
    invocation_id: str
    capability: str
    input_text: str
    deadline_seconds: Decimal
    idempotent: bool = True

    def __post_init__(self) -> None:
        _text("invocation_id", self.invocation_id)
        _text("capability", self.capability)
        if type(self.input_text) is not str:
            raise ModelInvocationError("input_text must be str")
        self.input_text.encode("utf-8", errors="strict")
        if type(self.deadline_seconds) is not Decimal or not self.deadline_seconds.is_finite() or self.deadline_seconds <= _ZERO:
            raise ModelInvocationError("deadline_seconds must be a positive finite Decimal")
        if type(self.idempotent) is not bool:
            raise ModelInvocationError("idempotent must be bool")

    @property
    def input_sha256(self) -> str:
        return _hash_text(self.input_text)


@dataclass(frozen=True, slots=True)
class ModelAdapterDescriptor:
    mode: ModelBackendMode
    backend_id: str
    endpoint_class: str
    model_id: str
    config_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ModelBackendMode) or self.mode not in (ModelBackendMode.LOCAL_OLLAMA, ModelBackendMode.EXTERNAL_API):
            raise ModelInvocationError("adapter mode must be LOCAL_OLLAMA or EXTERNAL_API")
        _text("backend_id", self.backend_id)
        _text("model_id", self.model_id)
        _sha("config_sha256", self.config_sha256)
        expected = "LOCAL_LOOPBACK_HTTP" if self.mode is ModelBackendMode.LOCAL_OLLAMA else "EXTERNAL_HTTPS_API"
        if self.endpoint_class != expected:
            raise ModelInvocationError(f"endpoint_class must be {expected}")


@dataclass(frozen=True, slots=True)
class ModelInvocationPolicy:
    mode: ModelBackendMode
    max_attempts: int = 1
    external_api_enabled: bool = False
    retry_timeout: bool = True
    retry_unavailable: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ModelBackendMode):
            raise ModelInvocationError("mode must be ModelBackendMode")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ModelInvocationError("max_attempts must be an integer from 1 through 5")
        for name in ("external_api_enabled", "retry_timeout", "retry_unavailable"):
            if type(getattr(self, name)) is not bool:
                raise ModelInvocationError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class ModelAdapterResponse:
    text: str
    model_id: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text:
            raise ModelAdapterInvalidResponse("response text is invalid")
        self.text.encode("utf-8", errors="strict")
        try:
            _text("response model_id", self.model_id)
        except (ModelInvocationError, UnicodeEncodeError) as exc:
            raise ModelAdapterInvalidResponse("response model_id is invalid") from exc


class ModelAdapter(Protocol):
    descriptor: ModelAdapterDescriptor
    def invoke(self, request: ModelInvocationRequest, *, timeout_seconds: Decimal) -> ModelAdapterResponse: ...


@dataclass(frozen=True, slots=True, init=False)
class ModelInvocationResult:
    status: ModelInvocationStatus
    reason_code: str
    mode: ModelBackendMode
    invocation_id: str
    backend_id: str | None
    model_id: str | None
    config_sha256: str | None
    input_sha256: str
    output_sha256: str | None
    started_at: str
    completed_at: str
    attempt_statuses: tuple[ModelInvocationStatus, ...]
    evidence_sha256: str

    def __init__(self, *, _token: object | None = None, **values: object) -> None:
        if _token is not _ISSUE:
            raise ModelInvocationError("ModelInvocationResult must be issued by invoke_optional_model")
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, values[name])

    @property
    def attempt_count(self) -> int:
        return len(self.attempt_statuses)

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "mode": self.mode.value,
            "invocation_id": self.invocation_id,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "config_sha256": self.config_sha256,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempt_statuses": [s.value for s in self.attempt_statuses],
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class ModelInvocationCompletion:
    result: ModelInvocationResult
    response_text: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.result, ModelInvocationResult):
            raise ModelInvocationError("result must be ModelInvocationResult")
        if self.result.status is ModelInvocationStatus.SUCCESS:
            if type(self.response_text) is not str or not self.response_text:
                raise ModelInvocationError("SUCCESS requires response_text")
            if _hash_text(self.response_text) != self.result.output_sha256:
                raise ModelInvocationError("response_text does not match output_sha256")
        elif self.response_text is not None:
            raise ModelInvocationError("non-SUCCESS completion cannot expose response_text")


def invoke_optional_model(
    request: ModelInvocationRequest,
    policy: ModelInvocationPolicy,
    adapters: Mapping[ModelBackendMode, ModelAdapter],
    *,
    cancel_requested: Callable[[], bool] | None = None,
    monotonic: Callable[[], Decimal] = _mono,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ModelInvocationCompletion:
    """Invoke only the explicitly selected backend under one monotonic deadline."""
    if not isinstance(request, ModelInvocationRequest) or not isinstance(policy, ModelInvocationPolicy):
        raise ModelInvocationError("request/policy type is invalid")
    if not isinstance(adapters, Mapping):
        raise ModelInvocationError("adapters must be a mapping")
    cancel = cancel_requested or (lambda: False)
    if not callable(cancel) or not callable(monotonic) or not callable(wall_clock):
        raise ModelInvocationError("clock/cancellation hooks must be callable")

    started = _wall(wall_clock())
    start_mono = _mono_value(monotonic)
    last_mono = start_mono
    attempts: list[ModelInvocationStatus] = []

    def elapsed_monotonic() -> Decimal:
        nonlocal last_mono
        sample = _mono_value(monotonic)
        if sample < last_mono:
            raise ModelInvocationError("monotonic clock regressed")
        last_mono = sample
        return sample - start_mono

    def finish(status: ModelInvocationStatus, reason: str, desc: ModelAdapterDescriptor | None = None, output: str | None = None) -> ModelInvocationCompletion:
        completed = _wall(wall_clock())
        output_sha = _hash_text(output) if output is not None else None
        body = {
            "status": status.value, "reason_code": reason, "mode": policy.mode.value,
            "invocation_id": request.invocation_id,
            "backend_id": desc.backend_id if desc else None,
            "model_id": desc.model_id if desc else None,
            "config_sha256": desc.config_sha256 if desc else None,
            "input_sha256": request.input_sha256, "output_sha256": output_sha,
            "started_at": started, "completed_at": completed,
            "attempt_statuses": [s.value for s in attempts],
        }
        result_values = dict(body)
        result_values["status"] = status
        result_values["mode"] = policy.mode
        result_values["attempt_statuses"] = tuple(attempts)
        result = ModelInvocationResult(
            _token=_ISSUE, **result_values, evidence_sha256=_digest(body)
        )
        return ModelInvocationCompletion(result, output if status is ModelInvocationStatus.SUCCESS else None)

    if bool(cancel()):
        return finish(ModelInvocationStatus.CANCELLED, "CANCELLED_BEFORE_DISPATCH")
    if policy.mode is ModelBackendMode.NO_LLM:
        return finish(ModelInvocationStatus.POLICY_BLOCKED, "NO_LLM_MODE")
    if policy.mode is ModelBackendMode.EXTERNAL_API and not policy.external_api_enabled:
        return finish(ModelInvocationStatus.POLICY_BLOCKED, "EXTERNAL_API_DISABLED")

    adapter = adapters.get(policy.mode)
    if adapter is None:
        return finish(ModelInvocationStatus.UNAVAILABLE, "BACKEND_NOT_CONFIGURED")
    desc = getattr(adapter, "descriptor", None)
    if not isinstance(desc, ModelAdapterDescriptor) or desc.mode is not policy.mode:
        return finish(ModelInvocationStatus.POLICY_BLOCKED, "BACKEND_IDENTITY_MISMATCH")

    for index in range(policy.max_attempts):
        if bool(cancel()):
            return finish(ModelInvocationStatus.CANCELLED, "CANCELLED_BEFORE_ATTEMPT", desc)
        elapsed = elapsed_monotonic()
        remaining = request.deadline_seconds - elapsed
        if remaining <= _ZERO:
            return finish(ModelInvocationStatus.TIMEOUT, "DEADLINE_EXHAUSTED_BEFORE_ATTEMPT", desc)
        try:
            response = adapter.invoke(request, timeout_seconds=remaining)
        except TimeoutError:
            attempts.append(ModelInvocationStatus.TIMEOUT)
            if request.idempotent and policy.retry_timeout and index + 1 < policy.max_attempts:
                continue
            return finish(ModelInvocationStatus.TIMEOUT, "BACKEND_TIMEOUT", desc)
        except (ConnectionError, OSError):
            attempts.append(ModelInvocationStatus.UNAVAILABLE)
            if request.idempotent and policy.retry_unavailable and index + 1 < policy.max_attempts:
                continue
            return finish(ModelInvocationStatus.UNAVAILABLE, "BACKEND_UNAVAILABLE", desc)
        except ModelAdapterInvalidResponse:
            attempts.append(ModelInvocationStatus.INVALID_RESPONSE)
            return finish(ModelInvocationStatus.INVALID_RESPONSE, "BACKEND_INVALID_RESPONSE", desc)
        except Exception:
            attempts.append(ModelInvocationStatus.UNAVAILABLE)
            return finish(ModelInvocationStatus.UNAVAILABLE, "BACKEND_FAILURE", desc)

        elapsed = elapsed_monotonic()
        if elapsed > request.deadline_seconds:
            attempts.append(ModelInvocationStatus.TIMEOUT)
            return finish(ModelInvocationStatus.TIMEOUT, "LATE_RESPONSE_DISCARDED", desc)
        if bool(cancel()):
            attempts.append(ModelInvocationStatus.CANCELLED)
            return finish(ModelInvocationStatus.CANCELLED, "CANCELLED_AFTER_RESPONSE", desc)
        if type(response) is not ModelAdapterResponse or response.model_id != desc.model_id:
            attempts.append(ModelInvocationStatus.INVALID_RESPONSE)
            return finish(ModelInvocationStatus.INVALID_RESPONSE, "BACKEND_RESPONSE_IDENTITY_INVALID", desc)
        attempts.append(ModelInvocationStatus.SUCCESS)
        return finish(ModelInvocationStatus.SUCCESS, "SUCCESS", desc, response.text)

    raise AssertionError("bounded attempt loop exhausted")
