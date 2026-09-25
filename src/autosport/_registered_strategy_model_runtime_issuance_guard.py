"""Fail-closed dispatch guard for registered-strategy model runtime issuance.

The owning resolver and runtime remain in registered_strategy_model_runtime.  This
module only seals the package-visible positive issuance boundary against ordinary
module/class rebinding, following the existing package-installed guard pattern.
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

    def guarded_resolver(*args, **kwargs):
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
