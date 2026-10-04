"""Route the canonical PAPER product loop through Wave M session authority.

The product runtime already owns lifecycle serialization and session coherence. This
composition layer changes only the final tick dispatch: the runtime must not return to
mutable ``coordinator.tick`` lookup after Wave M established the supported non-virtual
``tick_continuous_session`` authority boundary.
"""

from __future__ import annotations

from . import continuous_session_entry as _continuous_session_entry
from . import product_runtime as _product_runtime


def _install_guard() -> None:
    product_runtime = _product_runtime
    entry_module = _continuous_session_entry
    runtime_type = product_runtime.AutonomousProductRuntime
    runtime_dict = type.__getattribute__(runtime_type, "__dict__")
    serialized_runtime_operation = product_runtime._serialized_runtime_operation
    canonical_entry = entry_module.tick_continuous_session
    canonical_entry_code = getattr(canonical_entry, "__code__", None)
    canonical_coherent_status = runtime_dict["_coherent_status"]
    canonical_coherent_status_code = getattr(
        canonical_coherent_status,
        "__code__",
        None,
    )
    exact_getattr = getattr
    session_error = entry_module.ContinuousSessionError

    if (
        canonical_entry_code is None
        or canonical_coherent_status_code is None
    ):
        raise TypeError(
            "continuous-session product entry must expose executable authority"
        )

    def require_runtime_status_authority() -> None:
        if (
            runtime_dict.get("_coherent_status") is not canonical_coherent_status
            or exact_getattr(
                canonical_coherent_status,
                "__code__",
                None,
            )
            is not canonical_coherent_status_code
        ):
            raise session_error(
                "continuous-session status authority changed"
            )

    @serialized_runtime_operation
    def tick(self):
        # The product runtime consumes the exact import-time entry capability. A later
        # module/global rebind must fail closed rather than redirecting PAPER execution.
        if (
            entry_module.tick_continuous_session is not canonical_entry
            or getattr(canonical_entry, "__code__", None) is not canonical_entry_code
        ):
            raise session_error("continuous-session product entry authority changed")
        # Product tick needs lifecycle/source coherence only. Invoke the captured
        # class method non-virtually: an instance/class shadow must not bypass either
        # lifecycle coherence or the bounded-history contract.
        require_runtime_status_authority()
        canonical_coherent_status(
            self,
            include_settlement_history=False,
        )
        require_runtime_status_authority()
        result = canonical_entry(self.coordinator)
        if (
            entry_module.tick_continuous_session is not canonical_entry
            or getattr(canonical_entry, "__code__", None) is not canonical_entry_code
        ):
            raise session_error("continuous-session product entry authority changed")
        require_runtime_status_authority()
        return result

    type.__setattr__(runtime_type, "tick", tick)


_install_guard()
del _install_guard
# Do not retain mutable module-global aliases that the installed runtime tick could
# accidentally rediscover. The installed wrapper owns its exact references by closure.
del _continuous_session_entry
