"""Subordinate optional-model invocation executor.

The canonical availability, deadline, retry/fallback-plan and terminal evidence
contracts live in model_runtime_contract.  This module performs adapter calls under
those contracts and never creates a second provider, economic, risk, execution,
settlement, scientific-promotion, or UI authority.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

from .model_runtime_contract import (
    ModelBackendIdentity,
    ModelBackendMode,
    ModelInvocationEvidence,
    ModelInvocationRequest,
    ModelInvocationStatus,
    ModelRuntimeContractError,
    ModelRuntimePlan,
    finalize_model_invocation,
)

_NS = Decimal("1000000000")


class ModelInvocationError(ModelRuntimeContractError):
    """The subordinate invocation executor contract was violated."""


class ModelAdapterInvalidResponse(ValueError):
    """Adapter-normalized schema failure; raw provider detail stays out of evidence."""


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ModelInvocationError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ModelInvocationError(f"{name} must be valid UTF-8") from exc
    return value


def _monotonic_ns(clock: Callable[[], int]) -> int:
    value = clock()
    if type(value) is not int or value < 0:
        raise ModelInvocationError("monotonic clock must return a non-negative exact integer")
    return value


@dataclass(frozen=True, slots=True)
class ModelInvocationInput:
    """Transient prompt text bound to the canonical prompt-free request evidence."""

    request: ModelInvocationRequest
    input_text: str

    def __post_init__(self) -> None:
        if type(self.request) is not ModelInvocationRequest:
            raise ModelInvocationError("request must be exact ModelInvocationRequest")
        if type(self.input_text) is not str:
            raise ModelInvocationError("input_text must be str")
        try:
            self.input_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ModelInvocationError("input_text must be valid UTF-8") from exc
        if _hash_text(self.input_text) != self.request.input_sha256:
            raise ModelInvocationError("input_text does not match request input_sha256")


@dataclass(frozen=True, slots=True)
class ModelAdapterResponse:
    text: str
    model_id: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text:
            raise ModelAdapterInvalidResponse("response text is invalid")
        try:
            self.text.encode("utf-8", errors="strict")
            _canonical_text("response model_id", self.model_id)
        except (ModelInvocationError, UnicodeEncodeError) as exc:
            raise ModelAdapterInvalidResponse("response identity is invalid") from exc


class ModelAdapter(Protocol):
    identity: ModelBackendIdentity

    def invoke(
        self,
        invocation: ModelInvocationInput,
        *,
        timeout_seconds: Decimal,
    ) -> ModelAdapterResponse: ...


@dataclass(frozen=True, slots=True)
class ModelInvocationCompletion:
    """Transient response text paired with canonical non-authoritative evidence."""

    evidence: ModelInvocationEvidence
    response_text: str | None

    def __post_init__(self) -> None:
        if type(self.evidence) is not ModelInvocationEvidence:
            raise ModelInvocationError("evidence must be exact ModelInvocationEvidence")
        if self.evidence.status is ModelInvocationStatus.SUCCESS:
            if type(self.response_text) is not str or not self.response_text:
                raise ModelInvocationError("SUCCESS requires response_text")
            if _hash_text(self.response_text) != self.evidence.response_sha256:
                raise ModelInvocationError("response_text does not match response_sha256")
        elif self.response_text is not None:
            raise ModelInvocationError("non-SUCCESS completion cannot expose response_text")

    @property
    def status(self) -> ModelInvocationStatus:
        return self.evidence.status


def invoke_optional_model(
    invocation: ModelInvocationInput,
    plan: ModelRuntimePlan,
    adapters: Mapping[str, ModelAdapter],
    *,
    cancel_requested: Callable[[], bool] | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> ModelInvocationCompletion:
    """Execute only backends explicitly named by the canonical ModelRuntimePlan."""

    if type(invocation) is not ModelInvocationInput:
        raise ModelInvocationError("invocation must be exact ModelInvocationInput")
    if type(plan) is not ModelRuntimePlan:
        raise ModelInvocationError("plan must be exact ModelRuntimePlan")
    if invocation.request.plan_sha256 != plan.plan_sha256:
        raise ModelInvocationError("request plan identity mismatch")
    if not isinstance(adapters, Mapping):
        raise ModelInvocationError("adapters must be a mapping")
    if not callable(monotonic_ns):
        raise ModelInvocationError("monotonic_ns must be callable")
    cancel = cancel_requested or (lambda: False)
    if not callable(cancel):
        raise ModelInvocationError("cancel_requested must be callable")

    last_sample = _monotonic_ns(monotonic_ns)
    started = last_sample
    deadline_ns = plan.deadline_ms * 1_000_000
    attempted: list[int] = []

    def sample() -> int:
        nonlocal last_sample
        current = _monotonic_ns(monotonic_ns)
        if current < last_sample:
            raise ModelInvocationError("monotonic clock regressed")
        last_sample = current
        return current

    def cancelled() -> bool:
        value = cancel()
        if type(value) is not bool:
            raise ModelInvocationError("cancel_requested must return bool")
        return value

    def finish(
        status: ModelInvocationStatus,
        failure_code: str | None,
        *,
        completed: int,
        response_text: str | None = None,
    ) -> ModelInvocationCompletion:
        evidence = finalize_model_invocation(
            plan=plan,
            request=invocation.request,
            status=status,
            attempted_backend_indices=tuple(attempted),
            started_monotonic_ns=started,
            completed_monotonic_ns=completed,
            response_sha256=(
                _hash_text(response_text) if response_text is not None else None
            ),
            failure_code=failure_code,
        )
        return ModelInvocationCompletion(
            evidence=evidence,
            response_text=(
                response_text if status is ModelInvocationStatus.SUCCESS else None
            ),
        )

    if plan.backends[0].mode is ModelBackendMode.NO_LLM:
        return finish(
            ModelInvocationStatus.POLICY_BLOCKED,
            "NO_LLM_DISABLED",
            completed=started,
        )

    backend_index = 0
    while len(attempted) < plan.max_attempts:
        now = sample()
        if cancelled():
            return finish(
                ModelInvocationStatus.CANCELLED,
                "CANCELLED_BEFORE_ATTEMPT",
                completed=now,
            )
        if now - started >= deadline_ns:
            return finish(
                ModelInvocationStatus.TIMEOUT,
                "DEADLINE_EXHAUSTED_BEFORE_ATTEMPT",
                completed=now,
            )

        backend = plan.backends[backend_index]
        adapter = adapters.get(backend.backend_sha256)
        if adapter is None:
            status = (
                ModelInvocationStatus.POLICY_BLOCKED
                if not attempted
                else ModelInvocationStatus.UNAVAILABLE
            )
            code = "BACKEND_NOT_CONFIGURED" if not attempted else "FALLBACK_NOT_CONFIGURED"
            return finish(status, code, completed=now)

        identity = getattr(adapter, "identity", None)
        if type(identity) is not ModelBackendIdentity or identity != backend:
            status = (
                ModelInvocationStatus.POLICY_BLOCKED
                if not attempted
                else ModelInvocationStatus.UNAVAILABLE
            )
            code = "BACKEND_IDENTITY_MISMATCH" if not attempted else "FALLBACK_IDENTITY_MISMATCH"
            return finish(status, code, completed=now)

        remaining_ns = deadline_ns - (now - started)
        timeout_seconds = Decimal(remaining_ns) / _NS
        attempted.append(backend_index)
        try:
            response = adapter.invoke(invocation, timeout_seconds=timeout_seconds)
        except TimeoutError:
            completed = sample()
            if invocation.request.idempotent and len(attempted) < plan.max_attempts:
                if backend_index + 1 < len(plan.backends):
                    backend_index += 1
                continue
            return finish(
                ModelInvocationStatus.TIMEOUT,
                "BACKEND_TIMEOUT",
                completed=completed,
            )
        except (ConnectionError, OSError):
            completed = sample()
            if invocation.request.idempotent and len(attempted) < plan.max_attempts:
                if backend_index + 1 < len(plan.backends):
                    backend_index += 1
                continue
            return finish(
                ModelInvocationStatus.UNAVAILABLE,
                "BACKEND_UNAVAILABLE",
                completed=completed,
            )
        except ModelAdapterInvalidResponse:
            return finish(
                ModelInvocationStatus.INVALID_RESPONSE,
                "BACKEND_INVALID_RESPONSE",
                completed=sample(),
            )
        except Exception:
            return finish(
                ModelInvocationStatus.UNAVAILABLE,
                "BACKEND_FAILURE",
                completed=sample(),
            )

        completed = sample()
        if completed - started > deadline_ns:
            return finish(
                ModelInvocationStatus.TIMEOUT,
                "LATE_RESPONSE_DISCARDED",
                completed=completed,
            )
        if cancelled():
            return finish(
                ModelInvocationStatus.CANCELLED,
                "CANCELLED_AFTER_RESPONSE",
                completed=completed,
            )
        if type(response) is not ModelAdapterResponse or response.model_id != backend.model_id:
            return finish(
                ModelInvocationStatus.INVALID_RESPONSE,
                "BACKEND_RESPONSE_IDENTITY_INVALID",
                completed=completed,
            )
        return finish(
            ModelInvocationStatus.SUCCESS,
            None,
            completed=completed,
            response_text=response.text,
        )

    raise AssertionError("bounded canonical attempt loop exhausted")
