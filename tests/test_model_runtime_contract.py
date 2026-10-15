from dataclasses import replace

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


CFG_A = "1" * 64
CFG_B = "2" * 64
INPUT = "3" * 64
RESPONSE = "4" * 64


def local_backend(*, config_sha256=CFG_A, model_id="qwen3:8b"):
    return ModelBackendIdentity(
        mode=ModelBackendMode.LOCAL_OLLAMA,
        endpoint_class=ModelEndpointClass.LOCAL_LOOPBACK_HTTP,
        model_id=model_id,
        request_schema_id="ollama-chat-stream-false-think-false-v1",
        config_sha256=config_sha256,
    )


def external_backend():
    return ModelBackendIdentity(
        mode=ModelBackendMode.EXTERNAL_API,
        endpoint_class=ModelEndpointClass.OWNER_CONFIGURED_EXTERNAL_API,
        model_id="external-model-v1",
        request_schema_id="external-chat-v1",
        config_sha256=CFG_B,
    )


def request(plan, *, idempotent=True):
    return ModelInvocationRequest(
        request_id="analysis-1",
        plan_sha256=plan.plan_sha256,
        input_sha256=INPUT,
        requested_at="2026-09-21T12:00:00Z",
        purpose_code="OPTIONAL_RESEARCH_SUMMARY",
        idempotent=idempotent,
    )



def test_enabled_backend_endpoint_class_is_mode_bound_and_typed():
    with pytest.raises(ModelRuntimeContractError, match="must use endpoint_class"):
        ModelBackendIdentity(
            mode=ModelBackendMode.LOCAL_OLLAMA,
            endpoint_class=ModelEndpointClass.OWNER_CONFIGURED_EXTERNAL_API,
            model_id="qwen3:8b",
            request_schema_id="ollama-chat-stream-false-think-false-v1",
            config_sha256=CFG_A,
        )

    with pytest.raises(ModelRuntimeContractError, match="must use endpoint_class"):
        ModelBackendIdentity(
            mode=ModelBackendMode.EXTERNAL_API,
            endpoint_class=ModelEndpointClass.LOCAL_LOOPBACK_HTTP,
            model_id="external-model-v1",
            request_schema_id="external-chat-v1",
            config_sha256=CFG_B,
        )

    with pytest.raises(ModelRuntimeContractError, match="must be ModelEndpointClass"):
        ModelBackendIdentity(
            mode=ModelBackendMode.LOCAL_OLLAMA,
            endpoint_class="local-loopback-http",
            model_id="qwen3:8b",
            request_schema_id="ollama-chat-stream-false-think-false-v1",
            config_sha256=CFG_A,
        )

def test_no_llm_is_first_class_zero_attempt_policy_block():
    plan = ModelRuntimePlan(
        backends=(
            ModelBackendIdentity(
                mode=ModelBackendMode.NO_LLM,
                endpoint_class=None,
                model_id=None,
                request_schema_id=None,
                config_sha256=None,
            ),
        ),
        deadline_ms=1000,
        max_attempts=1,
    )
    req = request(plan)
    evidence = finalize_model_invocation(
        plan=plan,
        request=req,
        status=ModelInvocationStatus.POLICY_BLOCKED,
        attempted_backend_indices=(),
        started_monotonic_ns=10,
        completed_monotonic_ns=10,
        failure_code="NO_LLM_DISABLED",
    )

    assert evidence.status is ModelInvocationStatus.POLICY_BLOCKED
    assert evidence.attempted_backend_indices == ()
    assert evidence.response_sha256 is None
    assert evidence.domain_authority is False
    assert evidence.execution_authority is False
    assert evidence.financial_authority is False


def test_no_llm_cannot_silently_fallback_to_external_or_succeed():
    disabled = ModelBackendIdentity(
        mode=ModelBackendMode.NO_LLM,
        endpoint_class=None,
        model_id=None,
        request_schema_id=None,
        config_sha256=None,
    )
    with pytest.raises(ModelRuntimeContractError, match="standalone mode"):
        ModelRuntimePlan(
            backends=(disabled, external_backend()), deadline_ms=1000, max_attempts=2
        )

    plan = ModelRuntimePlan(backends=(disabled,), deadline_ms=1000, max_attempts=1)
    req = request(plan)
    with pytest.raises(ModelRuntimeContractError, match="POLICY_BLOCKED"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.SUCCESS,
            attempted_backend_indices=(),
            started_monotonic_ns=0,
            completed_monotonic_ns=1,
            response_sha256=RESPONSE,
        )


def test_local_success_binds_non_secret_identity_and_carries_no_domain_authority():
    plan = ModelRuntimePlan(
        backends=(local_backend(),), deadline_ms=2500, max_attempts=2
    )
    req = request(plan)
    evidence = finalize_model_invocation(
        plan=plan,
        request=req,
        status=ModelInvocationStatus.SUCCESS,
        attempted_backend_indices=(0,),
        started_monotonic_ns=1_000_000,
        completed_monotonic_ns=2_000_000,
        response_sha256=RESPONSE,
    )

    payload = evidence.canonical_payload()
    assert payload["response_sha256"] == RESPONSE
    assert payload["authority"] == {
        "domain": False,
        "provider": False,
        "risk": False,
        "settlement": False,
        "execution": False,
        "financial": False,
    }
    assert "prompt" not in str(payload).lower()
    assert "response_text" not in str(payload).lower()
    assert len(evidence.evidence_sha256) == 64


def test_late_success_is_rejected_and_must_be_recorded_as_timeout_without_output():
    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=100, max_attempts=1)
    req = request(plan)

    with pytest.raises(ModelRuntimeContractError, match="late model SUCCESS"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.SUCCESS,
            attempted_backend_indices=(0,),
            started_monotonic_ns=1_000_000_000,
            completed_monotonic_ns=1_101_000_000,
            response_sha256=RESPONSE,
        )

    timeout = finalize_model_invocation(
        plan=plan,
        request=req,
        status=ModelInvocationStatus.TIMEOUT,
        attempted_backend_indices=(0,),
        started_monotonic_ns=1_000_000_000,
        completed_monotonic_ns=1_101_000_000,
        failure_code="DEADLINE_EXCEEDED",
    )
    assert timeout.response_sha256 is None


def test_external_fallback_exists_only_when_explicitly_listed():
    local_only = ModelRuntimePlan(
        backends=(local_backend(),), deadline_ms=1000, max_attempts=2
    )
    req_local = request(local_only)
    with pytest.raises(ModelRuntimeContractError, match="unavailable backend"):
        finalize_model_invocation(
            plan=local_only,
            request=req_local,
            status=ModelInvocationStatus.UNAVAILABLE,
            attempted_backend_indices=(0, 1),
            started_monotonic_ns=0,
            completed_monotonic_ns=10,
            failure_code="LOCAL_UNAVAILABLE",
        )

    explicit = ModelRuntimePlan(
        backends=(local_backend(), external_backend()),
        deadline_ms=1000,
        max_attempts=2,
    )
    req_explicit = request(explicit)
    evidence = finalize_model_invocation(
        plan=explicit,
        request=req_explicit,
        status=ModelInvocationStatus.SUCCESS,
        attempted_backend_indices=(0, 1),
        started_monotonic_ns=0,
        completed_monotonic_ns=10,
        response_sha256=RESPONSE,
    )
    assert evidence.attempted_backend_indices == (0, 1)


def test_fallback_path_cannot_skip_or_move_backwards():
    plan = ModelRuntimePlan(
        backends=(local_backend(), external_backend()), deadline_ms=1000, max_attempts=4
    )
    req = request(plan)
    for path in ((1,), (0, 1, 0)):
        with pytest.raises(ModelRuntimeContractError):
            finalize_model_invocation(
                plan=plan,
                request=req,
                status=ModelInvocationStatus.UNAVAILABLE,
                attempted_backend_indices=path,
                started_monotonic_ns=0,
                completed_monotonic_ns=10,
                failure_code="BACKEND_UNAVAILABLE",
            )


def test_non_idempotent_request_cannot_retry_or_fallback():
    plan = ModelRuntimePlan(
        backends=(local_backend(), external_backend()), deadline_ms=1000, max_attempts=3
    )
    req = request(plan, idempotent=False)
    with pytest.raises(ModelRuntimeContractError, match="must not retry or fallback"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.UNAVAILABLE,
            attempted_backend_indices=(0, 1),
            started_monotonic_ns=0,
            completed_monotonic_ns=10,
            failure_code="BACKEND_UNAVAILABLE",
        )


def test_attempt_count_is_bounded_even_for_idempotent_work():
    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=2)
    req = request(plan)
    with pytest.raises(ModelRuntimeContractError, match="max_attempts"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.TIMEOUT,
            attempted_backend_indices=(0, 0, 0),
            started_monotonic_ns=0,
            completed_monotonic_ns=10,
            failure_code="DEADLINE_EXCEEDED",
        )


def test_restart_cancellation_can_record_unknown_completion_without_output():
    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    req = request(plan)
    evidence = finalize_model_invocation(
        plan=plan,
        request=req,
        status=ModelInvocationStatus.CANCELLED,
        attempted_backend_indices=(0,),
        started_monotonic_ns=100,
        completed_monotonic_ns=200,
        failure_code="RESTART_UNKNOWN_COMPLETION",
        unknown_completion=True,
    )
    assert evidence.unknown_completion is True
    assert evidence.response_sha256 is None

    with pytest.raises(ModelRuntimeContractError, match="only for CANCELLED"):
        replace(evidence, status=ModelInvocationStatus.UNAVAILABLE)


def test_non_success_cannot_launder_response_digest_as_current_output():
    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    req = request(plan)
    with pytest.raises(ModelRuntimeContractError, match="must not retain a response digest"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.INVALID_RESPONSE,
            attempted_backend_indices=(0,),
            started_monotonic_ns=0,
            completed_monotonic_ns=1,
            response_sha256=RESPONSE,
            failure_code="SCHEMA_INVALID",
        )


def test_free_form_failure_detail_is_rejected_to_avoid_secret_log_capture():
    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    req = request(plan)
    with pytest.raises(ModelRuntimeContractError, match="free-form detail"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.UNAVAILABLE,
            attempted_backend_indices=(0,),
            started_monotonic_ns=0,
            completed_monotonic_ns=1,
            failure_code="Authorization: Bearer secret-value",
        )


def test_config_or_model_switch_changes_plan_and_evidence_identity():
    plan_a = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    plan_b = ModelRuntimePlan(
        backends=(local_backend(config_sha256=CFG_B),), deadline_ms=1000, max_attempts=1
    )
    plan_c = ModelRuntimePlan(
        backends=(local_backend(model_id="qwen3:14b"),), deadline_ms=1000, max_attempts=1
    )
    assert len({plan_a.plan_sha256, plan_b.plan_sha256, plan_c.plan_sha256}) == 3

    ev_a = finalize_model_invocation(
        plan=plan_a,
        request=request(plan_a),
        status=ModelInvocationStatus.SUCCESS,
        attempted_backend_indices=(0,),
        started_monotonic_ns=0,
        completed_monotonic_ns=1,
        response_sha256=RESPONSE,
    )
    ev_b = finalize_model_invocation(
        plan=plan_b,
        request=request(plan_b),
        status=ModelInvocationStatus.SUCCESS,
        attempted_backend_indices=(0,),
        started_monotonic_ns=0,
        completed_monotonic_ns=1,
        response_sha256=RESPONSE,
    )
    assert ev_a.evidence_sha256 != ev_b.evidence_sha256


def test_bool_cannot_smuggle_integer_deadline_or_attempt_index():
    with pytest.raises(ModelRuntimeContractError, match="positive integer"):
        ModelRuntimePlan(backends=(local_backend(),), deadline_ms=True, max_attempts=1)

    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    req = request(plan)
    with pytest.raises(ModelRuntimeContractError, match="non-negative exact integers"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.UNAVAILABLE,
            attempted_backend_indices=(True,),
            started_monotonic_ns=0,
            completed_monotonic_ns=1,
            failure_code="BACKEND_UNAVAILABLE",
        )


def test_request_must_bind_exact_plan():
    plan_a = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    plan_b = ModelRuntimePlan(
        backends=(local_backend(config_sha256=CFG_B),), deadline_ms=1000, max_attempts=1
    )
    req = request(plan_a)
    with pytest.raises(ModelRuntimeContractError, match="plan identity mismatch"):
        finalize_model_invocation(
            plan=plan_b,
            request=req,
            status=ModelInvocationStatus.UNAVAILABLE,
            attempted_backend_indices=(0,),
            started_monotonic_ns=0,
            completed_monotonic_ns=1,
            failure_code="BACKEND_UNAVAILABLE",
        )


def test_enabled_backend_can_be_policy_blocked_before_transport():
    plan = ModelRuntimePlan(backends=(local_backend(),), deadline_ms=1000, max_attempts=1)
    req = request(plan)
    evidence = finalize_model_invocation(
        plan=plan,
        request=req,
        status=ModelInvocationStatus.POLICY_BLOCKED,
        attempted_backend_indices=(),
        started_monotonic_ns=10,
        completed_monotonic_ns=10,
        failure_code="INVALID_CONFIGURATION",
    )
    assert evidence.status is ModelInvocationStatus.POLICY_BLOCKED
    assert evidence.attempted_backend_indices == ()

    with pytest.raises(ModelRuntimeContractError, match="before model transport"):
        finalize_model_invocation(
            plan=plan,
            request=req,
            status=ModelInvocationStatus.POLICY_BLOCKED,
            attempted_backend_indices=(0,),
            started_monotonic_ns=10,
            completed_monotonic_ns=11,
            failure_code="POLICY_DENIED",
        )
