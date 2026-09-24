from __future__ import annotations

import pytest

from autosport.model_runtime_contract import (
    ModelBackendIdentity,
    ModelBackendMode,
    ModelEndpointClass,
    ModelInvocationRequest,
    ModelInvocationStatus,
    ModelRuntimeContractError,
    ModelRuntimePlan,
    finalize_model_invocation,
)


def _plan_and_request() -> tuple[ModelRuntimePlan, ModelInvocationRequest]:
    backend = ModelBackendIdentity(
        mode=ModelBackendMode.LOCAL_OLLAMA,
        endpoint_class=ModelEndpointClass.LOCAL_LOOPBACK_HTTP,
        model_id="model-1",
        request_schema_id="ollama-chat-v1",
        config_sha256="a" * 64,
    )
    plan = ModelRuntimePlan(
        backends=(backend,),
        deadline_ms=100,
        max_attempts=2,
    )
    request = ModelInvocationRequest(
        request_id="request-1",
        plan_sha256=plan.plan_sha256,
        input_sha256="b" * 64,
        requested_at="2026-09-22T14:00:00Z",
        purpose_code="OPTIONAL_SUMMARY",
        idempotent=True,
    )
    return plan, request


def test_enabled_plan_can_record_cancelled_before_first_transport_attempt() -> None:
    plan, request = _plan_and_request()

    evidence = finalize_model_invocation(
        plan=plan,
        request=request,
        status=ModelInvocationStatus.CANCELLED,
        attempted_backend_indices=(),
        started_monotonic_ns=10,
        completed_monotonic_ns=11,
        failure_code="CANCELLED_BEFORE_DISPATCH",
    )

    assert evidence.status is ModelInvocationStatus.CANCELLED
    assert evidence.attempted_backend_indices == ()
    assert evidence.failure_code == "CANCELLED_BEFORE_DISPATCH"


def test_enabled_plan_can_record_predispatch_timeout_only_after_deadline() -> None:
    plan, request = _plan_and_request()
    started = 10

    evidence = finalize_model_invocation(
        plan=plan,
        request=request,
        status=ModelInvocationStatus.TIMEOUT,
        attempted_backend_indices=(),
        started_monotonic_ns=started,
        completed_monotonic_ns=started + 100_000_000,
        failure_code="DEADLINE_EXHAUSTED_BEFORE_ATTEMPT",
    )

    assert evidence.status is ModelInvocationStatus.TIMEOUT
    assert evidence.attempted_backend_indices == ()
    assert evidence.elapsed_ns == 100_000_000

    with pytest.raises(
        ModelRuntimeContractError,
        match="pre-dispatch TIMEOUT requires the model deadline to be exhausted",
    ):
        finalize_model_invocation(
            plan=plan,
            request=request,
            status=ModelInvocationStatus.TIMEOUT,
            attempted_backend_indices=(),
            started_monotonic_ns=started,
            completed_monotonic_ns=started + 99_999_999,
            failure_code="DEADLINE_EXHAUSTED_BEFORE_ATTEMPT",
        )


@pytest.mark.parametrize(
    ("status", "extra"),
    (
        (ModelInvocationStatus.SUCCESS, {"response_sha256": "c" * 64}),
        (
            ModelInvocationStatus.UNAVAILABLE,
            {"failure_code": "BACKEND_UNAVAILABLE"},
        ),
        (
            ModelInvocationStatus.INVALID_RESPONSE,
            {"failure_code": "BACKEND_INVALID_RESPONSE"},
        ),
    ),
)
def test_transport_outcomes_still_require_a_real_attempt(
    status: ModelInvocationStatus,
    extra: dict[str, str],
) -> None:
    plan, request = _plan_and_request()

    with pytest.raises(
        ModelRuntimeContractError,
        match="enabled model plan requires at least one attempted backend",
    ):
        finalize_model_invocation(
            plan=plan,
            request=request,
            status=status,
            attempted_backend_indices=(),
            started_monotonic_ns=10,
            completed_monotonic_ns=11,
            **extra,
        )
