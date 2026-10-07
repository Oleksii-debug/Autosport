"""Non-virtual product entry for one autonomous PAPER runtime tick.

The supported ``autosport-product`` loop must not rediscover ``runtime.tick`` through
mutable instance/class lookup after Wave M has rooted the session path in a sealed
non-virtual entry.  This boundary captures the fully-composed runtime tick once and
invokes that exact callable directly.
"""

from __future__ import annotations

from .product_runtime import AutonomousProductRuntime, ProductCompositionError


def _build_product_runtime_tick_entry():
    runtime_type = AutonomousProductRuntime
    runtime_dict = type.__getattribute__(runtime_type, "__dict__")
    canonical_tick = runtime_dict["tick"]
    canonical_tick_code = getattr(canonical_tick, "__code__", None)
    canonical_inner = getattr(canonical_tick, "__wrapped__", None)
    canonical_inner_code = getattr(canonical_inner, "__code__", None)
    canonical_tick_closure = getattr(canonical_tick, "__closure__", None)
    canonical_inner_closure = getattr(canonical_inner, "__closure__", None)
    canonical_lookup = runtime_dict.get("__getattribute__")
    exact_getattr = getattr
    exact_tuple = tuple
    exact_type = type
    exact_zip = zip
    runtime_error = ProductCompositionError

    if canonical_tick_code is None or canonical_inner_code is None:
        raise TypeError("canonical product runtime tick must expose guarded executable code")
    if canonical_tick_closure is None or len(canonical_tick_closure) != 1:
        raise TypeError("canonical product runtime tick must expose one guarded target cell")
    canonical_target_cell = canonical_tick_closure[0]
    if canonical_target_cell.cell_contents is not canonical_inner:
        raise TypeError("canonical product runtime tick guarded target is inconsistent")
    if canonical_inner_closure is None:
        raise TypeError("canonical product runtime tick guard must expose authority cells")
    canonical_inner_targets = exact_tuple(
        cell.cell_contents for cell in canonical_inner_closure
    )

    def require_authority() -> None:
        if runtime_dict.get("tick") is not canonical_tick:
            raise runtime_error("canonical product runtime tick dispatch changed")
        if runtime_dict.get("__getattribute__") is not canonical_lookup:
            raise runtime_error("canonical product runtime lookup dispatch changed")
        if exact_getattr(canonical_tick, "__code__", None) is not canonical_tick_code:
            raise runtime_error("canonical product runtime tick wrapper executable changed")
        if exact_getattr(canonical_tick, "__wrapped__", None) is not canonical_inner:
            raise runtime_error("canonical product runtime tick wrapper target changed")
        if exact_getattr(canonical_tick, "__closure__", None) is not canonical_tick_closure:
            raise runtime_error("canonical product runtime tick wrapper closure changed")
        try:
            current_target = canonical_target_cell.cell_contents
        except ValueError as exc:
            raise runtime_error("canonical product runtime tick wrapper closure changed") from exc
        if current_target is not canonical_inner:
            raise runtime_error("canonical product runtime tick wrapper closure target changed")
        if exact_getattr(canonical_inner, "__code__", None) is not canonical_inner_code:
            raise runtime_error("canonical product runtime tick executable changed")
        if exact_getattr(canonical_inner, "__closure__", None) is not canonical_inner_closure:
            raise runtime_error("canonical product runtime tick inner closure changed")
        for cell, expected in exact_zip(canonical_inner_closure, canonical_inner_targets):
            try:
                current = cell.cell_contents
            except ValueError as exc:
                raise runtime_error("canonical product runtime tick inner closure changed") from exc
            if current is not expected:
                raise runtime_error("canonical product runtime tick inner closure target changed")

    def tick_autonomous_product_runtime(runtime):
        """Run one trusted runtime tick without mutable ``runtime.tick`` lookup."""

        if exact_type(runtime) is not runtime_type:
            raise TypeError("product runtime entry requires the canonical runtime")
        require_authority()
        result = canonical_tick(runtime)
        require_authority()
        return result

    return tick_autonomous_product_runtime


tick_autonomous_product_runtime = _build_product_runtime_tick_entry()
del _build_product_runtime_tick_entry
