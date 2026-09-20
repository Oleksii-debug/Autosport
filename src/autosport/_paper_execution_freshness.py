from __future__ import annotations

from datetime import timedelta

from . import _paper_execution_reality_legacy as _impl


def _conservative_milliseconds(delta: timedelta, name: str) -> int:
    """Return a conservative integer-ms telemetry value for safety comparisons.

    Positive fractional milliseconds round upward, so a true age of 500.001 ms
    cannot be accepted by a 500 ms bound. Exact millisecond boundaries remain
    unchanged. The returned integer remains telemetry; no provider truth is
    inferred from it.
    """
    microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if microseconds < 0:
        raise ValueError(f"{name} must be non-negative")
    return (microseconds + 999) // 1000


def _install() -> None:
    if getattr(_impl, "_autosport_conservative_milliseconds_installed", False):
        return
    _impl._milliseconds = _conservative_milliseconds
    _impl._autosport_conservative_milliseconds_installed = True


_install()
