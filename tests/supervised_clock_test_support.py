from __future__ import annotations

from collections.abc import Iterator

_DEFAULT_TRUSTED_TIME = "2026-09-19T08:00:03+00:00"
_sequence: Iterator[str] | None = None
_final = _DEFAULT_TRUSTED_TIME


def trusted_now() -> str:
    global _sequence
    if _sequence is None:
        return _final
    try:
        return next(_sequence)
    except StopIteration:
        _sequence = None
        return _final


def set_trusted_times(*values: str) -> None:
    global _sequence, _final
    if not values:
        raise ValueError("at least one trusted test instant is required")
    if any(type(value) is not str or not value for value in values):
        raise TypeError("trusted test instants must be non-empty strings")
    _final = values[-1]
    _sequence = iter(values)


def reset_trusted_time() -> None:
    set_trusted_times(_DEFAULT_TRUSTED_TIME)


def install_trusted_clock() -> None:
    from autosport import supervised_execution

    current = supervised_execution._trusted_now
    if current is trusted_now:
        return
    supervised_execution._trusted_now = trusted_now
