from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ModelRuntimeContractError(ValueError):
    """Raised when model-runtime evidence violates the fail-closed contract."""


class ModelBackendMode(str, Enum):
    NO_LLM = "NO_LLM"
    LOCAL_OLLAMA = "LOCAL_OLLAMA"
    EXTERNAL_API = "EXTERNAL_API"


class ModelEndpointClass(str, Enum):
    NONE = "NONE"
    LOCAL = "LOCAL"
    EXTERNAL = "EXTERNAL"


class ModelInvocationState(str, Enum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CANCELLED = "CANCELLED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


def _require_sha256(value: str, *, field: str) -> None:
    if len(value) != 64:
        raise ModelRuntimeContractError(f"{field} must be a lowercase sha256 hex digest")
    if value != value.lower() or any(character not in "0123456789abcdef" for character in value):
        raise ModelRuntimeContractError(f"{field} must be a lowercase sha256 hex digest")


def _require_finite(value: float, *, field: str) -> None:
    if not math.isfinite(value):
        raise ModelRuntimeContractError(f"{field} must be finite")


@dataclass(frozen=True)
class ModelInvocationDeadline:
    started_monotonic: float
    expires_monotonic: float

    def __post_init__(self) -> None:
        _require_finite(self.started_monotonic, field="started_monotonic")
        _require_finite(self.expires_monotonic, field="expires_monotonic")
        if self.expires_monotonic <= self.started_monotonic:
            raise ModelRuntimeContractError(
                "expires_monotonic must be later than started_monotonic"
            )

    @classmethod
    def from_budget(
        cls,
        *,
        started_monotonic: float,
        timeout_seconds: float,
    ) -> "ModelInvocationDeadline":
        _require_finite(started_monotonic, field="started_monotonic")
        _require_finite(timeout_seconds, field="timeout_seconds")
        if timeout_seconds <= 0:
            raise ModelRuntimeContractError("timeout_seconds must be positive")
        return cls(
            started_monotonic=started_monotonic,
            expires_monotonic=started_monotonic + timeout_seconds,
        )

    def remaining(self, now_monotonic: float) -> float:
        _require_finite(now_monotonic, field="now_monotonic")
        if now_monotonic < self.started_monotonic:
            raise ModelRuntimeContractError(
                "monotonic clock moved backwards before invocation start"
            )
        return max(0.0, self.expires_monotonic - now_monotonic)

    def expired(self, now_monotonic: float) -> bool:
        return self.remaining(now_monotonic) == 0.0


@dataclass(frozen=True)
class ModelRuntimeConfig:
    mode: ModelBackendMode
    endpoint_class: ModelEndpointClass
    model_id: str | None
    config_digest: str
    timeout_seconds: float | None
    max_attempts: int

    def __post_init__(self) -> None:
        _require_sha256(self.config_digest, field="config_digest")

        if self.mode is ModelBackendMode.NO_LLM:
            if self.endpoint_class is not ModelEndpointClass.NONE:
                raise ModelRuntimeContractError("NO_LLM must use endpoint_class=NONE")
            if self.model_id is not None:
                raise ModelRuntimeContractError("NO_LLM must not bind a model_id")
            if self.timeout_seconds is not None:
                raise ModelRuntimeContractError("NO_LLM must not define an invocation timeout")
            if self.max_attempts != 0:
                raise ModelRuntimeContractError("NO_LLM must use max_attempts=0")
            return

        expected_endpoint = (
            ModelEndpointClass.LOCAL
            if self.mode is ModelBackendMode.LOCAL_OLLAMA
            else ModelEndpointClass.EXTERNAL
        )
        if self.endpoint_class is not expected_endpoint:
            raise ModelRuntimeContractError(
                f"{self.mode.value} must use endpoint_class={expected_endpoint.value}"
            )
        if self.model_id is None or not self.model_id.strip():
            raise ModelRuntimeContractError(
                f"{self.mode.value} must bind a non-empty model_id"
            )
        if self.model_id != self.model_id.strip():
            raise ModelRuntimeContractError("model_id must not contain surrounding whitespace")
        if self.timeout_seconds is None:
            raise ModelRuntimeContractError("enabled model runtime must define timeout_seconds")
        _require_finite(self.timeout_seconds, field="timeout_seconds")
        if self.timeout_seconds <= 0:
            raise ModelRuntimeContractError("timeout_seconds must be positive")
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ModelRuntimeContractError("enabled model runtime must use max_attempts >= 1")

    @property
    def invocation_enabled(self) -> bool:
        return self.mode is not ModelBackendMode.NO_LLM

    def deadline(self, *, started_monotonic: float) -> ModelInvocationDeadline:
        if not self.invocation_enabled or self.timeout_seconds is None:
            raise ModelRuntimeContractError("NO_LLM has no model invocation deadline")
        return ModelInvocationDeadline.from_budget(
            started_monotonic=started_monotonic,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class ModelInvocationResult:
    state: ModelInvocationState
    mode: ModelBackendMode
    endpoint_class: ModelEndpointClass
    model_id: str | None
    config_digest: str
    request_digest: str
    response_digest: str | None
    started_monotonic: float
    completed_monotonic: float
    attempt_count: int
    backend_path: tuple[ModelBackendMode, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.config_digest, field="config_digest")
        _require_sha256(self.request_digest, field="request_digest")
        if self.response_digest is not None:
            _require_sha256(self.response_digest, field="response_digest")
        _require_finite(self.started_monotonic, field="started_monotonic")
        _require_finite(self.completed_monotonic, field="completed_monotonic")
        if self.completed_monotonic < self.started_monotonic:
            raise ModelRuntimeContractError(
                "completed_monotonic cannot precede started_monotonic"
            )
        if isinstance(self.attempt_count, bool) or self.attempt_count < 0:
            raise ModelRuntimeContractError("attempt_count must be a non-negative integer")

        if self.state is ModelInvocationState.SUCCESS:
            if self.response_digest is None:
                raise ModelRuntimeContractError("SUCCESS must bind response_digest")
        elif self.state is not ModelInvocationState.INVALID_RESPONSE:
            if self.response_digest is not None:
                raise ModelRuntimeContractError(
                    f"{self.state.value} must not carry a response_digest"
                )

    def verify_against(
        self,
        *,
        config: ModelRuntimeConfig,
        deadline: ModelInvocationDeadline | None,
    ) -> None:
        if self.config_digest != config.config_digest:
            raise ModelRuntimeContractError("model runtime config identity changed")
        if self.mode is not config.mode:
            raise ModelRuntimeContractError("backend mode changed during invocation")
        if self.endpoint_class is not config.endpoint_class:
            raise ModelRuntimeContractError("endpoint class changed during invocation")
        if self.model_id != config.model_id:
            raise ModelRuntimeContractError("model identity changed during invocation")

        if not config.invocation_enabled:
            if deadline is not None:
                raise ModelRuntimeContractError("NO_LLM cannot have an invocation deadline")
            if self.state is not ModelInvocationState.POLICY_BLOCKED:
                raise ModelRuntimeContractError(
                    "NO_LLM may only record POLICY_BLOCKED model invocation attempts"
                )
            if self.attempt_count != 0 or self.backend_path:
                raise ModelRuntimeContractError(
                    "NO_LLM cannot record a backend attempt or backend path"
                )
            return

        if deadline is None:
            raise ModelRuntimeContractError("enabled model runtime requires a deadline")
        if self.started_monotonic != deadline.started_monotonic:
            raise ModelRuntimeContractError(
                "invocation result is not bound to the supplied deadline start"
            )

        if self.state is ModelInvocationState.POLICY_BLOCKED:
            if self.attempt_count != 0 or self.backend_path:
                raise ModelRuntimeContractError(
                    "POLICY_BLOCKED cannot record an executed backend attempt"
                )
        else:
            if not 1 <= self.attempt_count <= config.max_attempts:
                raise ModelRuntimeContractError(
                    "attempt_count exceeds the configured bounded retry budget"
                )
            if self.backend_path != (config.mode,):
                raise ModelRuntimeContractError(
                    "backend_path must stay on the configured backend; cross-provider fallback is forbidden"
                )

        if (
            self.state is ModelInvocationState.SUCCESS
            and self.completed_monotonic > deadline.expires_monotonic
        ):
            raise ModelRuntimeContractError(
                "late SUCCESS must be discarded after the end-to-end deadline"
            )


class ModelRuntimeAdapter(Protocol):
    """Replaceable backend adapter; domain behavior must not depend on its availability."""

    config: ModelRuntimeConfig

    def invoke(
        self,
        request: bytes,
        *,
        deadline: ModelInvocationDeadline,
    ) -> ModelInvocationResult:
        ...
