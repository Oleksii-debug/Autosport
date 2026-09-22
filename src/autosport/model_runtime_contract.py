"""Provider-neutral contract for optional AI/model-runtime availability.

This module deliberately owns no sports, provider, execution, settlement, risk, or
financial truth.  It only defines how optional model invocations are identified and
how their terminal availability state is recorded without persisting prompts,
responses, credentials, or provider secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final


MODEL_RUNTIME_SCHEMA: Final = "autosport.model_runtime_contract"
MODEL_RUNTIME_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")
_CODE_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MAX_DEADLINE_MS: Final = 300_000
_MAX_ATTEMPTS: Final = 8


class ModelRuntimeContractError(ValueError):
    """The optional-model runtime contract was violated."""


class ModelBackendMode(StrEnum):
    NO_LLM = "NO_LLM"
    LOCAL_OLLAMA = "LOCAL_OLLAMA"
    EXTERNAL_API = "EXTERNAL_API"


class ModelEndpointClass(StrEnum):
    LOCAL_LOOPBACK_HTTP = "local-loopback-http"
    OWNER_CONFIGURED_EXTERNAL_API = "owner-configured-external-api"


class ModelInvocationStatus(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CANCELLED = "CANCELLED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ModelRuntimeContractError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ModelRuntimeContractError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ModelRuntimeContractError(f"{name} must be valid UTF-8") from exc
    return value


def _code(value: object, name: str) -> str:
    text = _canonical_text(value, name)
    if _CODE_RE.fullmatch(text) is None:
        raise ModelRuntimeContractError(
            f"{name} must match {_CODE_RE.pattern} and contain no free-form detail"
        )
    return text


def _sha256(value: object, name: str) -> str:
    text = _canonical_text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise ModelRuntimeContractError(f"{name} must be canonical SHA-256 hex")
    return text


def _utc_timestamp(value: object, name: str) -> str:
    text = _canonical_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelRuntimeContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelRuntimeContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_positive_int(value: object, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0:
        raise ModelRuntimeContractError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ModelRuntimeContractError(f"{name} must be <= {maximum}")
    return value


def _strict_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ModelRuntimeContractError(f"{name} must be a non-negative integer")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ModelRuntimeContractError("model runtime payload is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelBackendIdentity:
    """Non-secret identity for one optional model backend configuration."""

    mode: ModelBackendMode
    endpoint_class: ModelEndpointClass | None
    model_id: str | None
    request_schema_id: str | None
    config_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ModelBackendMode):
            raise ModelRuntimeContractError("mode must be ModelBackendMode")

        if self.mode is ModelBackendMode.NO_LLM:
            if any(
                value is not None
                for value in (
                    self.endpoint_class,
                    self.model_id,
                    self.request_schema_id,
                    self.config_sha256,
                )
            ):
                raise ModelRuntimeContractError(
                    "NO_LLM backend must not bind endpoint/model/request configuration"
                )
            return

        if not isinstance(self.endpoint_class, ModelEndpointClass):
            raise ModelRuntimeContractError(
                "endpoint_class must be ModelEndpointClass"
            )
        expected_endpoint_class = {
            ModelBackendMode.LOCAL_OLLAMA: ModelEndpointClass.LOCAL_LOOPBACK_HTTP,
            ModelBackendMode.EXTERNAL_API: (
                ModelEndpointClass.OWNER_CONFIGURED_EXTERNAL_API
            ),
        }[self.mode]
        if self.endpoint_class is not expected_endpoint_class:
            raise ModelRuntimeContractError(
                f"{self.mode.value} must use endpoint_class="
                f"{expected_endpoint_class.value}"
            )
        _canonical_text(self.model_id, "model_id")
        _canonical_text(self.request_schema_id, "request_schema_id")
        _sha256(self.config_sha256, "config_sha256")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "endpoint_class": self.endpoint_class.value if self.endpoint_class is not None else None,
            "model_id": self.model_id,
            "request_schema_id": self.request_schema_id,
            "config_sha256": self.config_sha256,
        }

    @property
    def backend_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ModelRuntimePlan:
    """Owner-selected model mode/fallback/deadline contract for one invocation class."""

    backends: tuple[ModelBackendIdentity, ...]
    deadline_ms: int
    max_attempts: int

    def __post_init__(self) -> None:
        if type(self.backends) is not tuple or not self.backends:
            raise ModelRuntimeContractError("backends must be a non-empty tuple")
        if any(type(item) is not ModelBackendIdentity for item in self.backends):
            raise ModelRuntimeContractError("backends must contain exact ModelBackendIdentity values")
        _strict_positive_int(self.deadline_ms, "deadline_ms", maximum=_MAX_DEADLINE_MS)
        _strict_positive_int(self.max_attempts, "max_attempts", maximum=_MAX_ATTEMPTS)

        if any(item.mode is ModelBackendMode.NO_LLM for item in self.backends):
            if len(self.backends) != 1 or self.backends[0].mode is not ModelBackendMode.NO_LLM:
                raise ModelRuntimeContractError(
                    "NO_LLM is a standalone mode, not an implicit fallback backend"
                )
            if self.max_attempts != 1:
                raise ModelRuntimeContractError("NO_LLM plan must have max_attempts=1")

        backend_ids = [item.backend_sha256 for item in self.backends]
        if len(set(backend_ids)) != len(backend_ids):
            raise ModelRuntimeContractError("duplicate backend identity in fallback plan")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": MODEL_RUNTIME_SCHEMA,
            "schema_version": MODEL_RUNTIME_SCHEMA_VERSION,
            "backends": [item.canonical_payload() for item in self.backends],
            "deadline_ms": self.deadline_ms,
            "max_attempts": self.max_attempts,
        }

    @property
    def plan_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ModelInvocationRequest:
    """Prompt-free request evidence for optional model work."""

    request_id: str
    plan_sha256: str
    input_sha256: str
    requested_at: str
    purpose_code: str
    idempotent: bool

    def __post_init__(self) -> None:
        _canonical_text(self.request_id, "request_id")
        _sha256(self.plan_sha256, "plan_sha256")
        _sha256(self.input_sha256, "input_sha256")
        _utc_timestamp(self.requested_at, "requested_at")
        _code(self.purpose_code, "purpose_code")
        if type(self.idempotent) is not bool:
            raise ModelRuntimeContractError("idempotent must be an exact bool")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "plan_sha256": self.plan_sha256,
            "input_sha256": self.input_sha256,
            "requested_at": _utc_timestamp(self.requested_at, "requested_at"),
            "purpose_code": self.purpose_code,
            "idempotent": self.idempotent,
        }

    @property
    def request_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ModelInvocationEvidence:
    """Terminal, non-authoritative evidence about optional model availability/output."""

    request_sha256: str
    plan_sha256: str
    input_sha256: str
    status: ModelInvocationStatus
    attempted_backend_indices: tuple[int, ...]
    started_monotonic_ns: int
    completed_monotonic_ns: int
    response_sha256: str | None = None
    failure_code: str | None = None
    unknown_completion: bool = False

    def __post_init__(self) -> None:
        _sha256(self.request_sha256, "request_sha256")
        _sha256(self.plan_sha256, "plan_sha256")
        _sha256(self.input_sha256, "input_sha256")
        if not isinstance(self.status, ModelInvocationStatus):
            raise ModelRuntimeContractError("status must be ModelInvocationStatus")
        if type(self.attempted_backend_indices) is not tuple:
            raise ModelRuntimeContractError("attempted_backend_indices must be a tuple")
        for index in self.attempted_backend_indices:
            if type(index) is not int or index < 0:
                raise ModelRuntimeContractError(
                    "attempted_backend_indices must contain non-negative exact integers"
                )
        started = _strict_nonnegative_int(self.started_monotonic_ns, "started_monotonic_ns")
        completed = _strict_nonnegative_int(self.completed_monotonic_ns, "completed_monotonic_ns")
        if completed < started:
            raise ModelRuntimeContractError(
                "completed_monotonic_ns cannot precede started_monotonic_ns"
            )
        if self.response_sha256 is not None:
            _sha256(self.response_sha256, "response_sha256")
        if self.failure_code is not None:
            _code(self.failure_code, "failure_code")
        if type(self.unknown_completion) is not bool:
            raise ModelRuntimeContractError("unknown_completion must be an exact bool")

        if self.status is ModelInvocationStatus.SUCCESS:
            if self.response_sha256 is None:
                raise ModelRuntimeContractError("SUCCESS requires response_sha256")
            if self.failure_code is not None or self.unknown_completion:
                raise ModelRuntimeContractError(
                    "SUCCESS cannot carry failure_code or unknown_completion"
                )
        else:
            if self.response_sha256 is not None:
                raise ModelRuntimeContractError(
                    "non-SUCCESS evidence must not retain a response digest as usable output"
                )

        if self.status is ModelInvocationStatus.CANCELLED:
            if self.failure_code is None:
                raise ModelRuntimeContractError("CANCELLED requires a bounded failure_code")
        elif self.unknown_completion:
            raise ModelRuntimeContractError(
                "unknown_completion is allowed only for CANCELLED model work"
            )

        if self.status in {
            ModelInvocationStatus.TIMEOUT,
            ModelInvocationStatus.UNAVAILABLE,
            ModelInvocationStatus.INVALID_RESPONSE,
            ModelInvocationStatus.POLICY_BLOCKED,
        } and self.failure_code is None:
            raise ModelRuntimeContractError(f"{self.status.value} requires failure_code")

    @property
    def elapsed_ns(self) -> int:
        return self.completed_monotonic_ns - self.started_monotonic_ns

    @property
    def domain_authority(self) -> bool:
        return False

    @property
    def provider_authority(self) -> bool:
        return False

    @property
    def risk_authority(self) -> bool:
        return False

    @property
    def settlement_authority(self) -> bool:
        return False

    @property
    def execution_authority(self) -> bool:
        return False

    @property
    def financial_authority(self) -> bool:
        return False

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": MODEL_RUNTIME_SCHEMA,
            "schema_version": MODEL_RUNTIME_SCHEMA_VERSION,
            "request_sha256": self.request_sha256,
            "plan_sha256": self.plan_sha256,
            "input_sha256": self.input_sha256,
            "status": self.status.value,
            "attempted_backend_indices": list(self.attempted_backend_indices),
            "started_monotonic_ns": self.started_monotonic_ns,
            "completed_monotonic_ns": self.completed_monotonic_ns,
            "response_sha256": self.response_sha256,
            "failure_code": self.failure_code,
            "unknown_completion": self.unknown_completion,
            "authority": {
                "domain": False,
                "provider": False,
                "risk": False,
                "settlement": False,
                "execution": False,
                "financial": False,
            },
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.canonical_payload())


def _validate_attempt_path(
    plan: ModelRuntimePlan,
    request: ModelInvocationRequest,
    attempted_backend_indices: tuple[int, ...],
) -> None:
    if type(attempted_backend_indices) is not tuple:
        raise ModelRuntimeContractError("attempted_backend_indices must be a tuple")
    if plan.backends[0].mode is ModelBackendMode.NO_LLM:
        if attempted_backend_indices:
            raise ModelRuntimeContractError("NO_LLM mode must perform zero model attempts")
        return
    if not attempted_backend_indices:
        raise ModelRuntimeContractError("enabled model plan requires at least one attempted backend")
    for index in attempted_backend_indices:
        if type(index) is not int or index < 0:
            raise ModelRuntimeContractError(
                "attempted_backend_indices must contain non-negative exact integers"
            )
    if len(attempted_backend_indices) > plan.max_attempts:
        raise ModelRuntimeContractError("attempt path exceeds max_attempts")
    if not request.idempotent and len(attempted_backend_indices) != 1:
        raise ModelRuntimeContractError("non-idempotent model work must not retry or fallback")
    if attempted_backend_indices[0] != 0:
        raise ModelRuntimeContractError("first attempt must use the primary backend")

    previous = 0
    for index in attempted_backend_indices:
        if index >= len(plan.backends):
            raise ModelRuntimeContractError("attempt path references an unavailable backend")
        if plan.backends[index].mode is ModelBackendMode.NO_LLM:
            raise ModelRuntimeContractError("NO_LLM cannot appear in an attempted backend path")
        if index < previous or index > previous + 1:
            raise ModelRuntimeContractError(
                "fallback path may retry the current backend or advance one explicit backend"
            )
        previous = index


def finalize_model_invocation(
    *,
    plan: ModelRuntimePlan,
    request: ModelInvocationRequest,
    status: ModelInvocationStatus,
    attempted_backend_indices: tuple[int, ...],
    started_monotonic_ns: int,
    completed_monotonic_ns: int,
    response_sha256: str | None = None,
    failure_code: str | None = None,
    unknown_completion: bool = False,
) -> ModelInvocationEvidence:
    """Validate and freeze one terminal optional-model invocation result.

    Late SUCCESS is rejected rather than normalized.  Adapters must discard the late
    payload and publish TIMEOUT/CANCELLED evidence instead, so a response arriving
    after the end-to-end monotonic deadline can never become current model evidence.
    """

    if type(plan) is not ModelRuntimePlan:
        raise ModelRuntimeContractError("plan must be exact ModelRuntimePlan")
    if type(request) is not ModelInvocationRequest:
        raise ModelRuntimeContractError("request must be exact ModelInvocationRequest")
    if request.plan_sha256 != plan.plan_sha256:
        raise ModelRuntimeContractError("request plan identity mismatch")
    if not isinstance(status, ModelInvocationStatus):
        raise ModelRuntimeContractError("status must be ModelInvocationStatus")

    started = _strict_nonnegative_int(started_monotonic_ns, "started_monotonic_ns")
    completed = _strict_nonnegative_int(completed_monotonic_ns, "completed_monotonic_ns")
    if completed < started:
        raise ModelRuntimeContractError(
            "completed_monotonic_ns cannot precede started_monotonic_ns"
        )
    deadline_ns = plan.deadline_ms * 1_000_000
    elapsed_ns = completed - started

    enabled = plan.backends[0].mode is not ModelBackendMode.NO_LLM
    if status is ModelInvocationStatus.POLICY_BLOCKED and enabled:
        if attempted_backend_indices:
            raise ModelRuntimeContractError(
                "POLICY_BLOCKED must occur before model transport is attempted"
            )
    elif enabled and not attempted_backend_indices and status is ModelInvocationStatus.CANCELLED:
        pass
    elif enabled and not attempted_backend_indices and status is ModelInvocationStatus.TIMEOUT:
        if elapsed_ns < deadline_ns:
            raise ModelRuntimeContractError(
                "pre-dispatch TIMEOUT requires the model deadline to be exhausted"
            )
    else:
        _validate_attempt_path(plan, request, attempted_backend_indices)

    if plan.backends[0].mode is ModelBackendMode.NO_LLM:
        if status is not ModelInvocationStatus.POLICY_BLOCKED:
            raise ModelRuntimeContractError(
                "NO_LLM mode must terminate as POLICY_BLOCKED for optional AI work"
            )
        if failure_code != "NO_LLM_DISABLED":
            raise ModelRuntimeContractError(
                "NO_LLM POLICY_BLOCKED result must use NO_LLM_DISABLED"
            )

    if status is ModelInvocationStatus.SUCCESS and elapsed_ns > deadline_ns:
        raise ModelRuntimeContractError(
            "late model SUCCESS is forbidden; discard payload and record TIMEOUT"
        )

    return ModelInvocationEvidence(
        request_sha256=request.request_sha256,
        plan_sha256=plan.plan_sha256,
        input_sha256=request.input_sha256,
        status=status,
        attempted_backend_indices=attempted_backend_indices,
        started_monotonic_ns=started,
        completed_monotonic_ns=completed,
        response_sha256=response_sha256,
        failure_code=failure_code,
        unknown_completion=unknown_completion,
    )
