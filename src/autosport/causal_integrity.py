from __future__ import annotations

from collections.abc import Mapping
from typing import Any


FORBIDDEN_FUTURE_KEYS = frozenset(
    {"final_result", "result", "winner", "outcome", "settled_outcome", "future_quote"}
)


def contains_forbidden_future_key(value: Any) -> bool:
    """Return whether a pre-outcome causal container contains future-result truth."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_FUTURE_KEYS:
                return True
            if contains_forbidden_future_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(contains_forbidden_future_key(child) for child in value)
    return False
