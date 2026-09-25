"""Fail-closed dispatch guard for registered-strategy model runtime issuance.

The owning resolver and runtime remain in registered_strategy_model_runtime. This
module seals the package-visible positive issuance boundary against ordinary
module/class/dependency rebinding, following the existing package-installed guard
pattern.
"""

from __future__ import annotations

from . import registered_strategy_model_runtime as _runtime


def _install_guard() -> None:
    runtime_type = _runtime.RegisteredStrategyModelRuntime
    runtime_post_init = runtime_type.__post_init__
    runtime_post_init_code = getattr(runtime_post_init, "__code__", None)
    error_type = _runtime.RegisteredStrategyModelRuntimeError
    resolver = _runtime.resolve_registered_strategy_model
    resolver_code = getattr(resolver, "__code__", None)

    # The resolver wrapper closes over the implementation, but that implementation
    # still resolves these authority-bearing names through the module globals dict.
    # Snapshot them at package installation so a later ordinary module rebind cannot
    # redirect durable registry/artifact/scientific validation without changing the
    # resolver code object.
    dependency_names = (
        "ScientificRegistry",
        "FactoryArtifactStore",
        "MeanBaselineModel",
        "WalkForwardEvaluationConfig",
        "Path",
        "_registry_state_sha256",
        "_require_causal_entry",
        "_require_precedes",
        "_promotion_authority",
        "_matching_positive_experiment",
        "_require_publication_registry_prefix",
        "_decode_mean_baseline_artifact",
        "_text",
        "_sha256",
        "_instant",
    )
    canonical_dependencies = tuple(
        (name, getattr(_runtime, name)) for name in dependency_names
    )

    registry_type = _runtime.ScientificRegistry
    registry_schema_version = registry_type.SCHEMA_VERSION
    registry_dispatch = tuple(
        (name, getattr(registry_type, name))
        for name in (
            "__init__",
            "_read",
            "get",
            "causal_records",
            "causal_precedes",
            "champion_strategy",
            "reproducibility_bundle",
        )
    )
    artifact_store_type = _runtime.FactoryArtifactStore
    artifact_store_dispatch = tuple(
        (name, getattr(artifact_store_type, name))
        for name in ("__init__", "read", "publication_receipt")
    )
    evaluation_config_type = _runtime.WalkForwardEvaluationConfig
    evaluation_from_frozen_text_descriptor = evaluation_config_type.__dict__.get(
        "from_frozen_text"
    )
    if type(evaluation_from_frozen_text_descriptor) is not classmethod:
        raise error_type(
            "registered-strategy evaluation-config classmethod authority is unavailable"
        )
    evaluation_from_frozen_text_function = (
        evaluation_from_frozen_text_descriptor.__func__
    )
    evaluation_from_frozen_text_code = getattr(
        evaluation_from_frozen_text_function,
        "__code__",
        None,
    )

    def require_canonical_dispatch() -> None:
        if (
            _runtime.RegisteredStrategyModelRuntime is not runtime_type
            or _runtime.RegisteredStrategyModelRuntimeError is not error_type
            or runtime_type.__post_init__ is not runtime_post_init
            or getattr(runtime_type.__post_init__, "__code__", None)
            is not runtime_post_init_code
            or getattr(resolver, "__code__", None) is not resolver_code
        ):
            raise error_type(
                "registered-strategy runtime issuance authority changed"
            )
        for name, expected in canonical_dependencies:
            if getattr(_runtime, name, None) is not expected:
                raise error_type(
                    f"registered-strategy runtime dependency {name!r} changed"
                )
        if registry_type.SCHEMA_VERSION != registry_schema_version:
            raise error_type(
                "registered-strategy ScientificRegistry schema authority changed"
            )
        for name, expected in registry_dispatch:
            if getattr(registry_type, name, None) is not expected:
                raise error_type(
                    f"registered-strategy ScientificRegistry dispatch {name!r} changed"
                )
        for name, expected in artifact_store_dispatch:
            if getattr(artifact_store_type, name, None) is not expected:
                raise error_type(
                    f"registered-strategy artifact-store dispatch {name!r} changed"
                )
        current_evaluation_descriptor = evaluation_config_type.__dict__.get(
            "from_frozen_text"
        )
        if (
            current_evaluation_descriptor is not evaluation_from_frozen_text_descriptor
            or getattr(current_evaluation_descriptor, "__func__", None)
            is not evaluation_from_frozen_text_function
            or getattr(evaluation_from_frozen_text_function, "__code__", None)
            is not evaluation_from_frozen_text_code
        ):
            raise error_type(
                "registered-strategy evaluation-config dispatch changed"
            )

    def guarded_resolver(*args, **kwargs):
        require_canonical_dispatch()
        result = resolver(*args, **kwargs)
        if type(result) is not runtime_type:
            raise error_type(
                "registered-strategy runtime resolver returned non-canonical authority"
            )
        return result

    # Preserve useful introspection without functools.wraps/__wrapped__: exposing the
    # pre-guard resolver as a public unwrap target would recreate the bypass this
    # guard exists to close.
    guarded_resolver.__name__ = resolver.__name__
    guarded_resolver.__qualname__ = resolver.__qualname__
    guarded_resolver.__doc__ = resolver.__doc__
    guarded_resolver.__module__ = resolver.__module__

    _runtime.resolve_registered_strategy_model = guarded_resolver


_install_guard()
del _install_guard