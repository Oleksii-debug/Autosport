from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.model_invocation import (
    ModelAdapterDescriptor,
    ModelAdapterResponse,
    ModelBackendMode,
    ModelInvocationError,
    ModelInvocationPolicy,
    ModelInvocationRequest,
    invoke_optional_model,
)


SHA = "a" * 64
NOW = datetime(2026, 9, 21, 15, 12, tzinfo=timezone.utc)


class RegressingClock:
    def __init__(self) -> None:
        self._values = iter((Decimal("10"), Decimal("11"), Decimal("10.5")))

    def __call__(self) -> Decimal:
        return next(self._values)


@dataclass
class ImmediateAdapter:
    descriptor: ModelAdapterDescriptor

    def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        timeout_seconds: Decimal,
    ) -> ModelAdapterResponse:
        assert timeout_seconds == Decimal("1")
        return ModelAdapterResponse(text="ok", model_id=self.descriptor.model_id)


def test_monotonic_sample_regression_after_dispatch_fails_closed() -> None:
    descriptor = ModelAdapterDescriptor(
        mode=ModelBackendMode.LOCAL_OLLAMA,
        backend_id="ollama-local",
        endpoint_class="LOCAL_LOOPBACK_HTTP",
        model_id="qwen3:8b",
        config_sha256=SHA,
    )
    request = ModelInvocationRequest(
        invocation_id="rollback-after-dispatch",
        capability="optional-explanation",
        input_text="state",
        deadline_seconds=Decimal("2"),
    )
    policy = ModelInvocationPolicy(mode=ModelBackendMode.LOCAL_OLLAMA)

    with pytest.raises(ModelInvocationError, match="monotonic clock regressed"):
        invoke_optional_model(
            request,
            policy,
            {ModelBackendMode.LOCAL_OLLAMA: ImmediateAdapter(descriptor)},
            monotonic=RegressingClock(),
            wall_clock=lambda: NOW,
        )
