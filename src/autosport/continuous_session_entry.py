"""Non-virtual product entry for one continuous-session coordinator tick.

The coordinator keeps its ordinary ``tick`` method for compatibility and tests, but
an authority-bearing product call must not rediscover that method through mutable
instance/class attribute lookup.  This entry captures the already-installed Wave M
settlement guard exactly once and invokes that exact function object directly.
"""

from __future__ import annotations

# Importing the guard is intentionally part of this public boundary.  It installs the
# canonical settlement/validator/tick fences before the entry captures their slots.
from . import _continuous_session_settlement_dispatch_guard as _settlement_guard  # noqa: F401
from .continuous_session import ContinuousSessionCoordinator, ContinuousSessionError


def _build_product_tick_entry():
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    canonical_tick = coordinator_dict["tick"]
    canonical_lookup = coordinator_dict["__getattribute__"]
    canonical_tick_code = getattr(canonical_tick, "__code__", None)
    exact_getattr = getattr
    exact_type = type
    session_error = ContinuousSessionError

    if canonical_tick_code is None:
        raise TypeError("canonical continuous-session tick must expose executable code")

    def require_authority() -> None:
        if coordinator_dict.get("tick") is not canonical_tick:
            raise session_error("canonical continuous-session tick dispatch changed")
        if coordinator_dict.get("__getattribute__") is not canonical_lookup:
            raise session_error("canonical continuous-session lookup dispatch changed")
        if exact_getattr(canonical_tick, "__code__", None) is not canonical_tick_code:
            raise session_error("canonical continuous-session tick executable changed")

    def tick_continuous_session(coordinator, *args, **kwargs):
        """Run one trusted coordinator tick without mutable ``instance.tick`` lookup."""

        if exact_type(coordinator) is not coordinator_type:
            raise TypeError("continuous-session product entry requires the canonical coordinator")
        require_authority()
        result = canonical_tick(coordinator, *args, **kwargs)
        require_authority()
        return result

    return tick_continuous_session


tick_continuous_session = _build_product_tick_entry()
del _build_product_tick_entry
