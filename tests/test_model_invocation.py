from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from autosport.model_invocation import (
    ModelAdapterResponse,
    ModelInvocationError,
    ModelInvocationInput,
    invoke_optional_model,
)
from autosport.model_runtime_contract import (
    ModelBackendIdentity,
    ModelBackendMode,
    ModelEndpointClass,
    ModelInvocationRequest,
    ModelInvocationStatus,
    ModelRuntimePlan,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _backend(index: int, mode: ModelBackendMode = ModelBackendMode.LOCAL_OLLAMA) -> ModelBackendIdentity:
    endpoint = (
        ModelEndpointClass.LOCAL_LOOPBACK_HTTP
        if mode is ModelBackendMode.LOCAL_OLLAMA
        else ModelEndpointClass.OWNER_CONFIGURED_EXTERNAL_API
    )
    return ModelBackendIdentity(
        mode=mode,
        endpoint_class=endpoint,
        model_id=f"model-{index}",
        request_schema_id="chat-v1",
        config_sha256=(f"{index:x}" * 64)[:64],
    )


def _invocation(
    plan: ModelRuntimePlan,
    *,
    text: str = "bounded optional input",
    idempotent: bool = True,
) -> ModelInvocationInput:
    request = ModelInvocationRequest(
        request_id="request-1",
        plan_sha256=plan.plan_sha256,
        input_sha256=_sha(text),
        requested_at="2026-09-22T14:00:00Z",
        purpose_code="OPTIONAL_SUMMARY",
        idempotent=idempotent,
    )
    return ModelInvocationInput(request=request, input_text=text)


class _Clock:
    def __init__(self, *values: int) -> None:
        self._values = list(values)

    def __call__(self) -> int:
        assert self._values, "test clock exhausted"
        return self._values.pop(0)


class _Adapter:
    def __init__(self, identity: ModelBackendIdentity, *outcomes: object) -> None:
        self.identity = identity
        self.outcomes = list(outcomes)
        self.calls = 0
        self.timeouts: list[Decimal] = []

    def invoke(
        self,
        invocation: ModelInvocationInput,
        *,
        timeout_seconds: Decimal,
    ) -> ModelAdapterResponse:
        self.calls += 1
        self.timeouts.append(timeout_seconds)
        assert _sha(invocation.input_text) == invocation.request.input_sha256
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, ModelAdapterResponse)
        return outcome


def test_success_uses_only_canonical_runtime_evidence() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)
    adapter = _Adapter(backend, ModelAdapterResponse("answer", backend.model_id or ""))

    completion = invoke_optional_model(
        invocation,
        plan,
        {backend.backend_sha256: adapter},
        monotonic_ns=_Clock(100, 101, 102),
    )

    assert completion.status is ModelInvocationStatus.SUCCESS
    assert completion.response_text == "answer"
    assert completion.evidence.request_sha256 == invocation.request.request_sha256
    assert completion.evidence.attempted_backend_indices == (0,)
    assert completion.evidence.response_sha256 == _sha("answer")
    assert completion.evidence.domain_authority is False
    assert completion.evidence.provider_authority is False
    assert completion.evidence.risk_authority is False
    assert completion.evidence.execution_authority is False
    assert completion.evidence.settlement_authority is False
    assert completion.evidence.financial_authority is False
    assert adapter.calls == 1
    assert adapter.timeouts == [Decimal("0.099999999")]


def test_no_llm_performs_zero_model_attempts() -> None:
    disabled = ModelBackendIdentity(
        mode=ModelBackendMode.NO_LLM,
        endpoint_class=None,
        model_id=None,
        request_schema_id=None,
        config_sha256=None,
    )
    plan = ModelRuntimePlan(backends=(disabled,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)

    completion = invoke_optional_model(
        invocation,
        plan,
        {},
        monotonic_ns=_Clock(100),
    )

    assert completion.status is ModelInvocationStatus.POLICY_BLOCKED
    assert completion.evidence.failure_code == "NO_LLM_DISABLED"
    assert completion.evidence.attempted_backend_indices == ()
    assert completion.response_text is None


def test_cancel_before_first_attempt_uses_canonical_empty_attempt_evidence() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)

    completion = invoke_optional_model(
        invocation,
        plan,
        {},
        cancel_requested=lambda: True,
        monotonic_ns=_Clock(100, 101),
    )

    assert completion.status is ModelInvocationStatus.CANCELLED
    assert completion.evidence.failure_code == "CANCELLED_BEFORE_ATTEMPT"
    assert completion.evidence.attempted_backend_indices == ()


def test_monotonic_sample_rollback_fails_closed() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)
    adapter = _Adapter(backend, ModelAdapterResponse("answer", backend.model_id or ""))

    with pytest.raises(ModelInvocationError, match="monotonic clock regressed"):
        invoke_optional_model(
            invocation,
            plan,
            {backend.backend_sha256: adapter},
            monotonic_ns=_Clock(10, 11, 10),
        )


def test_idempotent_timeout_falls_back_only_to_explicit_next_backend() -> None:
    primary = _backend(1)
    fallback = _backend(2, ModelBackendMode.EXTERNAL_API)
    plan = ModelRuntimePlan(
        backends=(primary, fallback),
        deadline_ms=100,
        max_attempts=2,
    )
    invocation = _invocation(plan)
    first = _Adapter(primary, TimeoutError("primary timed out"))
    second = _Adapter(
        fallback,
        ModelAdapterResponse("fallback answer", fallback.model_id or ""),
    )

    completion = invoke_optional_model(
        invocation,
        plan,
        {
            primary.backend_sha256: first,
            fallback.backend_sha256: second,
        },
        monotonic_ns=_Clock(0, 1, 2, 3, 4),
    )

    assert completion.status is ModelInvocationStatus.SUCCESS
    assert completion.response_text == "fallback answer"
    assert completion.evidence.attempted_backend_indices == (0, 1)
    assert first.calls == 1
    assert second.calls == 1


def test_idempotent_single_backend_timeout_retries_same_explicit_backend() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=100, max_attempts=2)
    invocation = _invocation(plan)
    adapter = _Adapter(
        backend,
        TimeoutError("first timeout"),
        ModelAdapterResponse("recovered", backend.model_id or ""),
    )

    completion = invoke_optional_model(
        invocation,
        plan,
        {backend.backend_sha256: adapter},
        monotonic_ns=_Clock(0, 1, 2, 3, 4),
    )

    assert completion.status is ModelInvocationStatus.SUCCESS
    assert completion.evidence.attempted_backend_indices == (0, 0)
    assert adapter.calls == 2


def test_non_idempotent_timeout_never_retries_or_falls_back() -> None:
    primary = _backend(1)
    fallback = _backend(2, ModelBackendMode.EXTERNAL_API)
    plan = ModelRuntimePlan(
        backends=(primary, fallback),
        deadline_ms=100,
        max_attempts=2,
    )
    invocation = _invocation(plan, idempotent=False)
    first = _Adapter(primary, TimeoutError("do not retry"))
    second = _Adapter(
        fallback,
        ModelAdapterResponse("must not run", fallback.model_id or ""),
    )

    completion = invoke_optional_model(
        invocation,
        plan,
        {
            primary.backend_sha256: first,
            fallback.backend_sha256: second,
        },
        monotonic_ns=_Clock(0, 1, 2),
    )

    assert completion.status is ModelInvocationStatus.TIMEOUT
    assert completion.evidence.failure_code == "BACKEND_TIMEOUT"
    assert completion.evidence.attempted_backend_indices == (0,)
    assert first.calls == 1
    assert second.calls == 0


def test_late_response_is_discarded_without_output_authority() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=1, max_attempts=1)
    invocation = _invocation(plan)
    adapter = _Adapter(backend, ModelAdapterResponse("late", backend.model_id or ""))

    completion = invoke_optional_model(
        invocation,
        plan,
        {backend.backend_sha256: adapter},
        monotonic_ns=_Clock(0, 1, 1_000_001),
    )

    assert completion.status is ModelInvocationStatus.TIMEOUT
    assert completion.evidence.failure_code == "LATE_RESPONSE_DISCARDED"
    assert completion.evidence.response_sha256 is None
    assert completion.response_text is None


def test_missing_primary_never_uses_unplanned_adapter() -> None:
    primary = _backend(1)
    unrelated = _backend(2, ModelBackendMode.EXTERNAL_API)
    plan = ModelRuntimePlan(backends=(primary,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)
    unrelated_adapter = _Adapter(
        unrelated,
        ModelAdapterResponse("wrong route", unrelated.model_id or ""),
    )

    completion = invoke_optional_model(
        invocation,
        plan,
        {unrelated.backend_sha256: unrelated_adapter},
        monotonic_ns=_Clock(0, 1),
    )

    assert completion.status is ModelInvocationStatus.POLICY_BLOCKED
    assert completion.evidence.failure_code == "BACKEND_NOT_CONFIGURED"
    assert completion.evidence.attempted_backend_indices == ()
    assert unrelated_adapter.calls == 0


def test_cancel_after_response_discards_response_text() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)
    adapter = _Adapter(backend, ModelAdapterResponse("discard me", backend.model_id or ""))
    answers = iter((False, True))

    completion = invoke_optional_model(
        invocation,
        plan,
        {backend.backend_sha256: adapter},
        cancel_requested=lambda: next(answers),
        monotonic_ns=_Clock(0, 1, 2),
    )

    assert completion.status is ModelInvocationStatus.CANCELLED
    assert completion.evidence.failure_code == "CANCELLED_AFTER_RESPONSE"
    assert completion.evidence.attempted_backend_indices == (0,)
    assert completion.evidence.response_sha256 is None
    assert completion.response_text is None


def test_raw_adapter_exception_detail_is_not_persisted() -> None:
    backend = _backend(1)
    plan = ModelRuntimePlan(backends=(backend,), deadline_ms=100, max_attempts=1)
    invocation = _invocation(plan)
    secret = "SECRET_TOKEN_MUST_NOT_PERSIST"
    adapter = _Adapter(backend, RuntimeError(secret))

    completion = invoke_optional_model(
        invocation,
        plan,
        {backend.backend_sha256: adapter},
        monotonic_ns=_Clock(0, 1, 2),
    )

    assert completion.status is ModelInvocationStatus.UNAVAILABLE
    assert completion.evidence.failure_code == "BACKEND_FAILURE"
    assert secret not in json.dumps(completion.evidence.canonical_payload(), sort_keys=True)
    assert completion.response_text is None
