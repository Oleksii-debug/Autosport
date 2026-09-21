import unittest

from autosport.model_runtime import (
    ModelBackendMode,
    ModelEndpointClass,
    ModelInvocationDeadline,
    ModelInvocationResult,
    ModelInvocationState,
    ModelRuntimeConfig,
    ModelRuntimeContractError,
)


class ModelRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_digest = "a" * 64
        self.request_digest = "b" * 64
        self.response_digest = "c" * 64
        self.local = ModelRuntimeConfig(
            mode=ModelBackendMode.LOCAL_OLLAMA,
            endpoint_class=ModelEndpointClass.LOCAL,
            model_id="llama3.2:3b@sha256:fixture",
            config_digest=self.config_digest,
            timeout_seconds=2.5,
            max_attempts=2,
        )

    def test_no_llm_is_explicit_and_has_no_invocation_budget(self) -> None:
        config = ModelRuntimeConfig(
            mode=ModelBackendMode.NO_LLM,
            endpoint_class=ModelEndpointClass.NONE,
            model_id=None,
            config_digest=self.config_digest,
            timeout_seconds=None,
            max_attempts=0,
        )
        self.assertFalse(config.invocation_enabled)
        with self.assertRaisesRegex(ModelRuntimeContractError, "no model invocation deadline"):
            config.deadline(started_monotonic=10.0)

        blocked = ModelInvocationResult(
            state=ModelInvocationState.POLICY_BLOCKED,
            mode=ModelBackendMode.NO_LLM,
            endpoint_class=ModelEndpointClass.NONE,
            model_id=None,
            config_digest=self.config_digest,
            request_digest=self.request_digest,
            response_digest=None,
            started_monotonic=10.0,
            completed_monotonic=10.0,
            attempt_count=0,
            backend_path=(),
        )
        blocked.verify_against(config=config, deadline=None)

    def test_no_llm_rejects_hidden_backend_identity(self) -> None:
        with self.assertRaisesRegex(ModelRuntimeContractError, "must not bind a model_id"):
            ModelRuntimeConfig(
                mode=ModelBackendMode.NO_LLM,
                endpoint_class=ModelEndpointClass.NONE,
                model_id="surprise-external-model",
                config_digest=self.config_digest,
                timeout_seconds=None,
                max_attempts=0,
            )

    def test_deadline_uses_one_bounded_monotonic_budget(self) -> None:
        deadline = self.local.deadline(started_monotonic=100.0)
        self.assertEqual(deadline.expires_monotonic, 102.5)
        self.assertEqual(deadline.remaining(101.0), 1.5)
        self.assertEqual(deadline.remaining(103.0), 0.0)
        self.assertTrue(deadline.expired(102.5))
        with self.assertRaisesRegex(ModelRuntimeContractError, "moved backwards"):
            deadline.remaining(99.9)

    def test_late_success_is_rejected(self) -> None:
        deadline = self.local.deadline(started_monotonic=5.0)
        result = self._result(
            state=ModelInvocationState.SUCCESS,
            completed_monotonic=7.500001,
            response_digest=self.response_digest,
        )
        with self.assertRaisesRegex(ModelRuntimeContractError, "late SUCCESS"):
            result.verify_against(config=self.local, deadline=deadline)

    def test_success_binds_exact_backend_model_config_request_and_response(self) -> None:
        deadline = self.local.deadline(started_monotonic=5.0)
        result = self._result(
            state=ModelInvocationState.SUCCESS,
            completed_monotonic=7.0,
            response_digest=self.response_digest,
        )
        result.verify_against(config=self.local, deadline=deadline)

        switched = ModelInvocationResult(
            state=result.state,
            mode=result.mode,
            endpoint_class=result.endpoint_class,
            model_id="other-model",
            config_digest=result.config_digest,
            request_digest=result.request_digest,
            response_digest=result.response_digest,
            started_monotonic=result.started_monotonic,
            completed_monotonic=result.completed_monotonic,
            attempt_count=result.attempt_count,
            backend_path=result.backend_path,
        )
        with self.assertRaisesRegex(ModelRuntimeContractError, "model identity changed"):
            switched.verify_against(config=self.local, deadline=deadline)

    def test_cross_provider_fallback_is_forbidden(self) -> None:
        deadline = self.local.deadline(started_monotonic=5.0)
        fallback = self._result(
            state=ModelInvocationState.UNAVAILABLE,
            completed_monotonic=5.3,
            response_digest=None,
            backend_path=(
                ModelBackendMode.LOCAL_OLLAMA,
                ModelBackendMode.EXTERNAL_API,
            ),
        )
        with self.assertRaisesRegex(ModelRuntimeContractError, "cross-provider fallback is forbidden"):
            fallback.verify_against(config=self.local, deadline=deadline)

    def test_attempt_count_cannot_exceed_bounded_retry_budget(self) -> None:
        deadline = self.local.deadline(started_monotonic=5.0)
        result = self._result(
            state=ModelInvocationState.TIMEOUT,
            completed_monotonic=7.5,
            response_digest=None,
            attempt_count=3,
        )
        with self.assertRaisesRegex(ModelRuntimeContractError, "bounded retry budget"):
            result.verify_against(config=self.local, deadline=deadline)

    def test_timeout_cannot_carry_a_stale_model_response(self) -> None:
        with self.assertRaisesRegex(ModelRuntimeContractError, "must not carry a response_digest"):
            self._result(
                state=ModelInvocationState.TIMEOUT,
                completed_monotonic=7.5,
                response_digest=self.response_digest,
            )

    def test_invalid_response_may_bind_digest_without_becoming_success(self) -> None:
        deadline = self.local.deadline(started_monotonic=5.0)
        result = self._result(
            state=ModelInvocationState.INVALID_RESPONSE,
            completed_monotonic=5.2,
            response_digest=self.response_digest,
        )
        result.verify_against(config=self.local, deadline=deadline)
        self.assertIs(result.state, ModelInvocationState.INVALID_RESPONSE)

    def test_nonfinite_clock_and_malformed_digests_fail_closed(self) -> None:
        with self.assertRaisesRegex(ModelRuntimeContractError, "started_monotonic must be finite"):
            ModelInvocationDeadline.from_budget(
                started_monotonic=float("nan"),
                timeout_seconds=1.0,
            )
        with self.assertRaisesRegex(ModelRuntimeContractError, "lowercase sha256"):
            ModelRuntimeConfig(
                mode=ModelBackendMode.EXTERNAL_API,
                endpoint_class=ModelEndpointClass.EXTERNAL,
                model_id="api-model",
                config_digest="not-a-digest",
                timeout_seconds=1.0,
                max_attempts=1,
            )

    def test_stretched_deadline_cannot_legalize_late_success(self) -> None:
        stretched = ModelInvocationDeadline(
            started_monotonic=5.0,
            expires_monotonic=50.0,
        )
        result = self._result(
            state=ModelInvocationState.SUCCESS,
            completed_monotonic=8.0,
            response_digest=self.response_digest,
        )
        with self.assertRaisesRegex(ModelRuntimeContractError, "end-to-end budget"):
            result.verify_against(config=self.local, deadline=stretched)

    def test_raw_enum_values_and_fractional_retry_budget_fail_closed(self) -> None:
        with self.assertRaisesRegex(ModelRuntimeContractError, "mode must be"):
            ModelRuntimeConfig(
                mode="LOCAL_OLLAMA",  # type: ignore[arg-type]
                endpoint_class=ModelEndpointClass.LOCAL,
                model_id="model",
                config_digest=self.config_digest,
                timeout_seconds=1.0,
                max_attempts=1,
            )
        with self.assertRaisesRegex(ModelRuntimeContractError, "max_attempts must be an integer"):
            ModelRuntimeConfig(
                mode=ModelBackendMode.LOCAL_OLLAMA,
                endpoint_class=ModelEndpointClass.LOCAL,
                model_id="model",
                config_digest=self.config_digest,
                timeout_seconds=1.0,
                max_attempts=1.5,  # type: ignore[arg-type]
            )

    def _result(
        self,
        *,
        state: ModelInvocationState,
        completed_monotonic: float,
        response_digest: str | None,
        attempt_count: int = 1,
        backend_path: tuple[ModelBackendMode, ...] = (ModelBackendMode.LOCAL_OLLAMA,),
    ) -> ModelInvocationResult:
        return ModelInvocationResult(
            state=state,
            mode=ModelBackendMode.LOCAL_OLLAMA,
            endpoint_class=ModelEndpointClass.LOCAL,
            model_id=self.local.model_id,
            config_digest=self.config_digest,
            request_digest=self.request_digest,
            response_digest=response_digest,
            started_monotonic=5.0,
            completed_monotonic=completed_monotonic,
            attempt_count=attempt_count,
            backend_path=backend_path,
        )


if __name__ == "__main__":
    unittest.main()
