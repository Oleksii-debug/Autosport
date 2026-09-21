from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from autosport.model_invocation import (
    ModelAdapterDescriptor, ModelAdapterInvalidResponse, ModelAdapterResponse,
    ModelBackendMode, ModelInvocationError, ModelInvocationPolicy,
    ModelInvocationRequest, ModelInvocationResult, ModelInvocationStatus,
    ModelInvocationCompletion, invoke_optional_model,
)

SHA = "a" * 64
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

class Clock:
    def __init__(self, *values: str):
        self.values = [Decimal(v) for v in values]
        self.last = self.values[-1] if self.values else Decimal("0")
    def __call__(self):
        if self.values: self.last = self.values.pop(0)
        return self.last

class Wall:
    def __init__(self, *values: datetime):
        self.values = list(values); self.last = self.values[-1] if self.values else T0
    def __call__(self):
        if self.values: self.last = self.values.pop(0)
        return self.last

@dataclass
class Fake:
    descriptor: ModelAdapterDescriptor
    outcomes: list[object]
    def __post_init__(self): self.calls = []
    def invoke(self, request, *, timeout_seconds):
        self.calls.append(timeout_seconds); item = self.outcomes.pop(0)
        if isinstance(item, BaseException): raise item
        return item

def desc(mode=ModelBackendMode.LOCAL_OLLAMA, model="qwen3:8b", config=SHA):
    return ModelAdapterDescriptor(
        mode, "ollama-local" if mode is ModelBackendMode.LOCAL_OLLAMA else "api-vendor",
        "LOCAL_LOOPBACK_HTTP" if mode is ModelBackendMode.LOCAL_OLLAMA else "EXTERNAL_HTTPS_API",
        model, config,
    )

def req(**kw):
    v=dict(invocation_id="i1", capability="optional-explanation", input_text="Стан ринку", deadline_seconds=Decimal("2"), idempotent=True); v.update(kw)
    return ModelInvocationRequest(**v)

def pol(**kw):
    v=dict(mode=ModelBackendMode.LOCAL_OLLAMA, max_attempts=1); v.update(kw)
    return ModelInvocationPolicy(**v)

def run(r, p, a, mono=("0","0","0.2"), wall=(T0,T0), cancel=None):
    return invoke_optional_model(r,p,a,monotonic=Clock(*mono),wall_clock=Wall(*wall),cancel_requested=cancel)

def test_no_llm_is_first_class_and_never_calls_backend():
    a=Fake(desc(),[ModelAdapterResponse("unused","qwen3:8b")])
    c=run(req(),pol(mode=ModelBackendMode.NO_LLM),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("0",))
    assert c.result.status is ModelInvocationStatus.POLICY_BLOCKED and c.result.attempt_count==0 and a.calls==[]

def test_success_binds_identity_without_persisting_prompt():
    a=Fake(desc(),[ModelAdapterResponse("відповідь","qwen3:8b")]); c=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:a})
    assert c.result.status is ModelInvocationStatus.SUCCESS and c.result.backend_id=="ollama-local"
    assert c.result.model_id=="qwen3:8b" and c.result.config_sha256==SHA and c.response_text=="відповідь"
    assert "Стан ринку" not in json.dumps(c.result.payload(),ensure_ascii=False)

def test_external_requires_explicit_policy_and_no_hidden_fallback():
    local=Fake(desc(),[ModelAdapterResponse("local","qwen3:8b")]); cloud=Fake(desc(ModelBackendMode.EXTERNAL_API,"cloud-v1"),[ModelAdapterResponse("cloud","cloud-v1")])
    c=run(req(),pol(mode=ModelBackendMode.EXTERNAL_API),{ModelBackendMode.LOCAL_OLLAMA:local,ModelBackendMode.EXTERNAL_API:cloud},mono=("0",))
    assert c.result.reason_code=="EXTERNAL_API_DISABLED" and local.calls==cloud.calls==[]
    c=run(req(),pol(),{ModelBackendMode.EXTERNAL_API:cloud},mono=("0",))
    assert c.result.reason_code=="BACKEND_NOT_CONFIGURED" and cloud.calls==[]

def test_failures_are_typed_and_exception_secrets_are_not_evidence():
    for exc,status,reason in [
        (TimeoutError("token=SECRET"),ModelInvocationStatus.TIMEOUT,"BACKEND_TIMEOUT"),
        (ConnectionError("Bearer SECRET"),ModelInvocationStatus.UNAVAILABLE,"BACKEND_UNAVAILABLE"),
        (ModelAdapterInvalidResponse("SECRET"),ModelInvocationStatus.INVALID_RESPONSE,"BACKEND_INVALID_RESPONSE"),
        (RuntimeError("api_key=SECRET"),ModelInvocationStatus.UNAVAILABLE,"BACKEND_FAILURE"),
    ]:
        a=Fake(desc(),[exc]); c=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("0","0"))
        assert c.result.status is status and c.result.reason_code==reason and "SECRET" not in json.dumps(c.result.payload())

def test_late_response_is_discarded_by_monotonic_not_wall_clock():
    a=Fake(desc(),[ModelAdapterResponse("late","qwen3:8b")])
    c=run(req(deadline_seconds=Decimal("1")),pol(),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("10","10","11.001"),wall=(T0,T0-timedelta(hours=2)))
    assert c.result.reason_code=="LATE_RESPONSE_DISCARDED" and c.response_text is None

def test_idempotent_retry_stays_on_same_backend_and_uses_remaining_budget():
    a=Fake(desc(),[ConnectionError("x"),ModelAdapterResponse("ok","qwen3:8b")])
    c=run(req(),pol(max_attempts=3),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("0","0","0.5","0.75"))
    assert c.result.status is ModelInvocationStatus.SUCCESS and c.result.attempt_count==2
    assert a.calls==[Decimal("2"),Decimal("1.5")]

def test_non_idempotent_does_not_retry_and_deadline_does_not_fake_attempt():
    a=Fake(desc(),[TimeoutError("x"),ModelAdapterResponse("bad","qwen3:8b")])
    c=run(req(idempotent=False),pol(max_attempts=5),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("0","0"))
    assert c.result.attempt_count==1 and len(a.calls)==1
    b=Fake(desc(),[ConnectionError("x")]); c=run(req(deadline_seconds=Decimal("1")),pol(max_attempts=3),{ModelBackendMode.LOCAL_OLLAMA:b},mono=("0","0","1"))
    assert c.result.reason_code=="DEADLINE_EXHAUSTED_BEFORE_ATTEMPT" and c.result.attempt_count==1 and len(b.calls)==1

def test_cancel_before_or_after_response_never_publishes_success():
    a=Fake(desc(),[ModelAdapterResponse("ok","qwen3:8b")]); c=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("0",),cancel=lambda:True)
    assert c.result.status is ModelInvocationStatus.CANCELLED and a.calls==[]
    flags=iter((False,False,True)); b=Fake(desc(),[ModelAdapterResponse("ok","qwen3:8b")])
    c=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:b},mono=("0","0","0.1"),cancel=lambda:next(flags))
    assert c.result.reason_code=="CANCELLED_AFTER_RESPONSE" and c.response_text is None

def test_backend_identity_and_response_identity_fail_closed():
    wrong=Fake(desc(ModelBackendMode.EXTERNAL_API,"cloud-v1"),[ModelAdapterResponse("x","cloud-v1")])
    c=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:wrong},mono=("0",)); assert c.result.reason_code=="BACKEND_IDENTITY_MISMATCH" and wrong.calls==[]
    a=Fake(desc(),[ModelAdapterResponse("x","other")]); c=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:a}); assert c.result.reason_code=="BACKEND_RESPONSE_IDENTITY_INVALID"

def test_positive_result_cannot_be_forged_and_identity_changes_hash():
    with pytest.raises(ModelInvocationError,match="must be issued"):
        ModelInvocationResult(status=ModelInvocationStatus.SUCCESS)
    a=Fake(desc(),[ModelAdapterResponse("same","qwen3:8b")]); one=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:a}).result
    b=Fake(desc(model="qwen3:14b"),[ModelAdapterResponse("same","qwen3:14b")]); two=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:b}).result
    c=Fake(desc(config="b"*64),[ModelAdapterResponse("same","qwen3:8b")]); three=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:c}).result
    assert len({one.evidence_sha256,two.evidence_sha256,three.evidence_sha256})==3

def test_invalid_endpoint_retry_bounds_and_monotonic_regression_fail_closed():
    with pytest.raises(ModelInvocationError,match="LOCAL_LOOPBACK_HTTP"):
        ModelAdapterDescriptor(ModelBackendMode.LOCAL_OLLAMA,"x","EXTERNAL_HTTPS_API","qwen3:8b",SHA)
    with pytest.raises(ModelInvocationError,match="1 through 5"):
        ModelInvocationPolicy(ModelBackendMode.LOCAL_OLLAMA,max_attempts=6)
    a=Fake(desc(),[ModelAdapterResponse("ok","qwen3:8b")])
    with pytest.raises(ModelInvocationError,match="monotonic clock regressed"):
        run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:a},mono=("10","9"))


def test_monotonic_regression_between_dispatch_and_response_fails_closed():
    a=Fake(desc(),[ModelAdapterResponse("ok","qwen3:8b")])
    with pytest.raises(ModelInvocationError,match="monotonic clock regressed"):
        run(
            req(),
            pol(),
            {ModelBackendMode.LOCAL_OLLAMA:a},
            mono=("10","11","10.5"),
        )
    assert a.calls==[Decimal("1")]


def test_descriptor_requires_real_enum_not_string_alias():
    with pytest.raises(ModelInvocationError, match="adapter mode"):
        ModelAdapterDescriptor("LOCAL_OLLAMA", "x", "LOCAL_LOOPBACK_HTTP", "qwen3:8b", SHA)

def test_completion_cannot_rebind_or_expose_response_text():
    good_adapter=Fake(desc(),[ModelAdapterResponse("trusted","qwen3:8b")])
    success=run(req(),pol(),{ModelBackendMode.LOCAL_OLLAMA:good_adapter}).result
    with pytest.raises(ModelInvocationError, match="does not match"):
        ModelInvocationCompletion(success,"tampered")
    blocked=run(req(),pol(mode=ModelBackendMode.NO_LLM),{},mono=("0",)).result
    with pytest.raises(ModelInvocationError, match="non-SUCCESS"):
        ModelInvocationCompletion(blocked,"leak")
