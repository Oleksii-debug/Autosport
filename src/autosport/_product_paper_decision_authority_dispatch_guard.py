"""Seal supported PAPER tick to the exact durable decision-authority resolver.

The owning ``ProductPaperDecisionCycle`` already reconstructs the exact durable
EconomicGoalContract + PaperRiskPolicy and verifies ProductDecisionActivation before a
supported tick may create economic decisions.  This guard closes the remaining Python
dispatch seam: rebinding ``_resolve_product_authority`` on an instance or on the class
must not turn caller code into positive START authority.

No second risk, activation, runtime, or decision authority is introduced.  The wrapper
only pins the already-composed product methods and forwards the freshly reconstructed
authority into the existing bounded decision cycle.
"""

from __future__ import annotations

from .product_paper_decision_cycle import (
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
    ProductPaperDecisionTickResult,
)


def _install_product_paper_decision_authority_dispatch_guard() -> None:
    cycle_class = ProductPaperDecisionCycle
    canonical_resolver = cycle_class.__dict__.get("_resolve_product_authority")
    canonical_require_running = cycle_class.__dict__.get("_require_running_runtime")
    skip_descriptor = cycle_class.__dict__.get("_decision_skip_reason")
    canonical_skip_reason = (
        skip_descriptor.__func__ if isinstance(skip_descriptor, staticmethod) else None
    )
    canonical_run_decision = cycle_class.__dict__.get("_run_decision_cycle")

    resolver_code = getattr(canonical_resolver, "__code__", None)
    require_running_code = getattr(canonical_require_running, "__code__", None)
    skip_reason_code = getattr(canonical_skip_reason, "__code__", None)
    run_decision_code = getattr(canonical_run_decision, "__code__", None)

    if (
        canonical_resolver is None
        or canonical_require_running is None
        or canonical_skip_reason is None
        or canonical_run_decision is None
        or resolver_code is None
        or require_running_code is None
        or skip_reason_code is None
        or run_decision_code is None
    ):
        raise RuntimeError(
            "canonical product PAPER decision authority dispatch is unavailable"
        )

    def tick(self: ProductPaperDecisionCycle) -> ProductPaperDecisionTickResult:
        if type(self) is not cycle_class:
            raise ProductPaperDecisionCycleError(
                "supported PAPER decision cycle must be the exact canonical class"
            )
        live_skip_descriptor = cycle_class.__dict__.get("_decision_skip_reason")
        if (
            cycle_class.__dict__.get("tick") is not tick
            or cycle_class.__dict__.get("_resolve_product_authority")
            is not canonical_resolver
            or getattr(canonical_resolver, "__code__", None) is not resolver_code
            or cycle_class.__dict__.get("_require_running_runtime")
            is not canonical_require_running
            or getattr(canonical_require_running, "__code__", None)
            is not require_running_code
            or live_skip_descriptor is not skip_descriptor
            or getattr(canonical_skip_reason, "__code__", None)
            is not skip_reason_code
            or cycle_class.__dict__.get("_run_decision_cycle")
            is not canonical_run_decision
            or getattr(canonical_run_decision, "__code__", None)
            is not run_decision_code
        ):
            raise ProductPaperDecisionCycleError(
                "supported PAPER decision tick dispatch changed"
            )

        if not self._cycle_lock.acquire(blocking=False):
            raise ProductPaperDecisionCycleError(
                "PAPER decision composition already has a product cycle in progress"
            )
        try:
            canonical_require_running(self)
            product_tick = self.runtime.tick()
            status = self.runtime.status()
            skip_reason = canonical_skip_reason(product_tick, status)
            if skip_reason is not None:
                return ProductPaperDecisionTickResult(
                    product_tick=product_tick,
                    decision=None,
                    skipped_reason=skip_reason,
                )
            authority = canonical_resolver(self)
            decision = canonical_run_decision(self, authority=authority)
            return ProductPaperDecisionTickResult(
                product_tick=product_tick,
                decision=decision,
                skipped_reason=None,
            )
        finally:
            self._cycle_lock.release()

    setattr(cycle_class, "tick", tick)


_install_product_paper_decision_authority_dispatch_guard()
del _install_product_paper_decision_authority_dispatch_guard
